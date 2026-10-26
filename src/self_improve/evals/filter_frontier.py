"""Independent frontier/Ultra reference labels using authenticated Codex CLI.

The router chooses the account and frontier model. Ultra is a native Codex
setting: the router's cross-provider effort enum deliberately excludes it.
Retained CLI turn-context telemetry must match both; a clamp is a failure.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import quota_router
from quota_router.capability import fresh_listing, lookup, read_table, table_path
from quota_router.config import load_config as load_router_config

from ..data_boundary import DataBoundaryError
from .filter_dataset import case_context, digest, redact
from .filter_models import atomic_json, reference_request
from .filter_reference import execute

PROFILE = 'frontier-ultra'
TRANSPORT = 'quota-router-codex-cli/3'
TIMEOUT = 240
# The vendor does not publish dollar telemetry here. These are fixed experiment
# accounting rates (the earlier reference's uncached rates), NOT Codex pricing.
ACCOUNTING = {'input_per_million': 5, 'output_per_million': 25,
              'basis': 'fixed reference-token accounting estimate; not vendor pricing or an invoice'}
_AUTH_OVERRIDES = ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL',
                   'OPENAI_ORG_ID', 'OPENAI_ORGANIZATION', 'OPENAI_PROJECT_ID',
                   'CODEX_APP_TOOLS_PIPE_PATH', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE',
                   'CODEX_SESSION_ID', 'CODEX_THREAD_ID')


def eligible_accounts(env, cfg):
    """Intersect configured accounts with fresh frontier + native Ultra listings."""
    if 'codex' not in cfg.allowed_providers:
        return {}
    path = table_path(env)
    table, error = read_table(path)
    if error:
        raise DataBoundaryError(error)
    accounts = load_router_config(env=env, cwd=str(Path.cwd())).enabled_accounts()
    eligible = {}
    for account in accounts:
        if account.provider != 'codex':
            continue
        model = lookup(table, account.id, 'frontier', now_s=time.time(), path=path)
        listing = fresh_listing(table, account.id, now_s=time.time(), path=path)
        if (getattr(model, 'listed', False) and getattr(listing, 'efforts', None) and
                'ultra' in dict(listing.efforts).get(model.id, ())):
            eligible[account.id] = model.id
    return eligible


def select(cfg, *, model=None, exclude=(), selector=None, record=True):
    env = dict(os.environ)
    eligible = eligible_accounts(env, cfg)
    only = [a for a,m in eligible.items() if (model is None or m == model) and a not in exclude]
    if not only:
        raise DataBoundaryError('no fresh frontier/Ultra Codex account; refresh router capabilities')
    kwargs = dict(capability='frontier', only=only, env=env, record=record, timeout_ms=10000)
    if model:
        kwargs['model'] = model
    if exclude:
        kwargs.update(exclude=list(exclude), no_sticky=True)
    selection = (selector or quota_router.select_account)(**kwargs)
    if (selection.contract_version != 1 or type(selection.fits) is not bool or
            type(selection.meets_policy) is not bool):
        raise DataBoundaryError('invalid frontier router contract')
    if (not selection.fits or not selection.meets_policy or selection.account not in only or
            selection.provider != 'codex' or selection.capability != 'frontier' or
            selection.model is None or selection.model.id != eligible[selection.account] or
            not selection.model.listed or selection.model.provider != 'codex'):
        raise DataBoundaryError('router did not supply an eligible frontier/Ultra account')
    return selection


def profile(output, cfg):
    path = output / 'reference-frontier-ultra-profile.json'
    if path.exists():
        result = json.loads(path.read_text())
        if (result.get('profile') != PROFILE or result.get('transport') != TRANSPORT or
                result.get('effort') != 'ultra' or not isinstance(result.get('model'), str) or
                result.get('accounting') != ACCOUNTING):
            raise DataBoundaryError('frontier profile changed; preserve the original experiment')
        return result
    selection = select(cfg, record=False)
    result = dict(profile=PROFILE, model=selection.model.id, capability='frontier', effort='ultra',
                  transport=TRANSPORT, timeout_seconds=TIMEOUT, accounting=ACCOUNTING,
                  router_effort='unsupported; native CLI Ultra verified from turn_context')
    atomic_json(path, result)
    return result


def request(window, settings):
    payload = reference_request(window)
    payload.update(model=settings['model'], reasoning_effort='ultra')
    payload['messages'][0]['content'] = (
        'Apply the labeling rubric to the JSON evidence below. Return only the requested JSON label. '
        'Make this judgment independently: do not delegate, call tools, inspect files, or ask questions.\n'
        + payload['messages'][0]['content'])
    return payload


def read_rollout(stdout, env):
    started = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    ids = [e.get('thread_id') for e in started if e.get('type') == 'thread.started']
    if len(ids) != 1 or not isinstance(ids[0], str) or not re.fullmatch(r'[0-9a-f-]{36}', ids[0]):
        return []
    # Select only this invocation's exact thread; never infer identity from mtime.
    home = Path(env.get('CODEX_HOME') or Path.home() / '.codex')
    paths = list((home / 'sessions').glob(f'**/rollout-*{ids[0]}.jsonl'))
    if len(paths) != 1:
        return []
    return [json.loads(line) for line in paths[0].read_text().splitlines() if line.strip()]


def parse_cli(raw, expected_model, expected_system=None):
    fail = lambda status: dict(status=status, retryable=False)
    try:
        events = [json.loads(line) for line in raw['stdout'].splitlines() if line.strip()]
        if any(not isinstance(e, dict) for e in events):
            return fail('invalid_response')
        errors = [e for e in events if e.get('type') in ('error', 'turn.failed')]
        if raw['returncode'] or errors:
            text = (raw.get('stderr', '') + json.dumps(errors)).lower()
            if any(s in text for s in ('usage limit', 'rate limit', 'quota', 'limit reached')):
                return dict(status='quota_exhausted', retryable=True)
            return fail('cli_failed')
        started = [e.get('thread_id') for e in events if e.get('type') == 'thread.started']
        complete = [e for e in events if e.get('type') == 'turn.completed']
        if len(started) != 1 or len(complete) != 1:
            return fail('invalid_response')
        items = [e['item'] for e in events if e.get('type') == 'item.completed']
        if any(i.get('type') not in ('agent_message', 'reasoning') for i in items):
            return fail('unexpected_tool_use')
        messages = [i['text'] for i in items if i.get('type') == 'agent_message']
        if len(messages) != 1:
            return fail('invalid_response')
        rollout = raw['rollout']
        identities = [e['payload'] for e in rollout if e.get('type') == 'session_meta']
        contexts = [e['payload'] for e in rollout if e.get('type') == 'turn_context']
        if len(identities) != 1 or identities[0].get('id') != started[0] or not contexts:
            return fail('missing_identity')
        if (expected_system is not None and
                identities[0].get('base_instructions', {}).get('text') != expected_system):
            return fail('instruction_mismatch')
        if any(c.get('model') != expected_model for c in contexts):
            return fail('model_mismatch')
        if any(c.get('effort') != 'ultra' for c in contexts):
            return fail('effort_mismatch')
        if any(e.get('type') == 'response_item' and e.get('payload', {}).get('type') in
               ('function_call', 'custom_tool_call', 'web_search_call') for e in rollout):
            return fail('unexpected_tool_use')
        usage = complete[0]['usage']
        if any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('input_tokens','output_tokens')):
            return fail('invalid_usage')
        answer = json.loads(messages[0])
        if not isinstance(answer, dict):
            return fail('invalid_label')
        return dict(status='ok', label=answer, served_model=expected_model, served_effort='ultra',
                    thread_id=started[0],
                    identity_scope='CLI turn_context telemetry; backend revision not independently reported',
                    usage=usage, retryable=False, billing_basis=ACCOUNTING['basis'])
    except (ValueError, KeyError, TypeError, AttributeError):
        return fail('invalid_response')


def accounting_charge(usage):
    return (usage['input_tokens'] * ACCOUNTING['input_per_million'] +
            usage['output_tokens'] * ACCOUNTING['output_per_million']) / 1_000_000


def invoke(payload, output, cache_key, cfg, *, exclude=(), selector=None, executor=None):
    if payload.get('reasoning_effort') != 'ultra':
        raise DataBoundaryError('frontier reference requires native Ultra')
    try:
        selection = select(cfg, model=payload['model'], exclude=exclude, selector=selector)
    except DataBoundaryError as exc:
        return dict(status='quota_unavailable', error=str(exc), retryable=False), 0.0
    pick = redact(selection.to_dict())
    if isinstance(pick.get('exec', {}).get('env'), dict):
        pick['exec']['env'] = {k: v if k == 'CODEX_HOME' else '<omitted>' for k,v in pick['exec']['env'].items()}
    atomic_json(output / f'{cache_key}.pick.json', pick)
    env = dict(os.environ)
    env.pop('CODEX_HOME', None)
    env.update(selection.exec_env)
    for key in (*selection.exec_unset_env, *_AUTH_OVERRIDES):
        env.pop(key, None)
    audit = dict(account=selection.account, cli_provider='codex', router_pick=f'{cache_key}.pick.json',
                 router_pick_sha=digest(pick), requested_effort='ultra', capability='frontier', accounting=ACCOUNTING)
    argv = [*selection.exec_argv_prefix, cfg.codex_path, 'exec', '--json', '--ignore-user-config',
            '--ignore-rules', '--skip-git-repo-check', '--sandbox', 'read-only', '-m', payload['model']]
    config = {'model_reasoning_effort':'ultra', 'model_provider':'openai', 'forced_login_method':'chatgpt',
              'approval_policy':'never', 'web_search':'disabled', 'project_doc_max_bytes':0,
              'developer_instructions':'', 'memories.use_memories':False,
              'features.shell_tool':False, 'features.unified_exec':False, 'features.apps':False,
              'features.plugins':False, 'features.hooks':False, 'features.multi_agent':False,
              'features.multi_agent_v2':False, 'features.browser_use':False, 'features.computer_use':False,
              'features.image_generation':False, 'features.goals':False}
    for key,value in config.items():
        argv.extend(['-c', f'{key}={json.dumps(value)}'])
    raw = dict(transport=TRANSPORT, returncode=None, stdout='', stderr='', rollout=[],
               requested_model=payload['model'], requested_effort='ultra')
    cost = None
    try:
        with tempfile.TemporaryDirectory(prefix='frontier-reference-', dir=output) as cwd:
            instructions = Path(cwd) / 'rubric.txt'
            instructions.write_text(payload['system'])
            instructions.chmod(0o600)
            argv.extend(['-c', f'model_instructions_file={json.dumps(str(instructions))}', '-'])
            rc, stdout, stderr = (executor or execute)(argv, prompt=payload['messages'][0]['content'],
                                                       cwd=cwd, env=env, timeout=TIMEOUT)
        raw.update(returncode=rc, stdout=stdout, stderr=stderr)
        raw['rollout'] = read_rollout(stdout, env)
        result = parse_cli(raw, payload['model'], payload['system'])
        if result['status'] == 'ok':
            cost = accounting_charge(result['usage'])
    except subprocess.TimeoutExpired as exc:
        decode = lambda x: x.decode(errors='replace') if isinstance(x,bytes) else x or ''
        raw.update(stdout=decode(exc.stdout), stderr=decode(exc.stderr))
        result = dict(status='timeout_unknown', retryable=False)
    except (OSError, ValueError) as exc:
        result = dict(status='transport_error', error_type=type(exc).__name__, retryable=False)
    atomic_json(output / f'{cache_key}.response.json', raw)
    return {**result, **audit, 'response_sha':digest(raw)}, cost


def verified_labels(path, data, sha, *, category_only=False):
    """Recompute labels from raw evidence; no scored cache-only success."""
    from .filter_metrics import validate_labels
    artifact = json.loads(path.read_text())
    settings = artifact.get('reference_settings', {})
    if (artifact.get('dataset_sha') != sha or artifact.get('provider') != 'reference' or
            settings.get('profile') != PROFILE or settings.get('effort') != 'ultra' or
            artifact.get('reference_profile') != PROFILE):
        raise DataBoundaryError('not a matching frontier/Ultra reference result')
    cases = {c['id']:c for c in data['cases']}
    labels = []
    for anchor,record in artifact['records'].items():
        if anchor not in cases:
            raise DataBoundaryError('reference anchor missing from frozen dataset')
        if record['status'] != 'ok' and not (category_only and record['status'] == 'invalid_label'):
            continue
        expected_request = request(case_context(data,cases[anchor]),settings)
        if record.get('input_sha') != digest(expected_request):
            raise DataBoundaryError('reference input differs from frozen case')
        name = record.get('raw_response','')
        if not name or Path(name).name != name or not name.endswith('.response.json'):
            raise DataBoundaryError('invalid reference response path')
        raw = json.loads((path.parent/name).read_text())
        delivered = [content.get('text') for event in raw.get('rollout', [])
                     if event.get('type') == 'response_item' and event.get('payload',{}).get('role') == 'user'
                     for content in event['payload'].get('content', [])]
        if expected_request['messages'][0]['content'] not in delivered:
            raise DataBoundaryError('reference transcript did not receive the frozen input')
        parsed = parse_cli(raw, settings['model'], expected_request['system'])
        if (digest(raw) != record.get('response_sha') or parsed['status'] != 'ok' or
                any(parsed.get(k) != record.get(k) for k in ('label','served_model','served_effort','usage'))):
            raise DataBoundaryError('reference cache differs from verified raw evidence')
        pick_name = record.get('router_pick','')
        if not pick_name or Path(pick_name).name != pick_name:
            raise DataBoundaryError('invalid router evidence path')
        pick = json.loads((path.parent/pick_name).read_text())
        decision = pick.get('decision',{})
        if (digest(pick) != record.get('router_pick_sha') or
                decision.get('model',{}).get('id') != settings['model'] or
                decision.get('fits') is not True or decision.get('meets_policy') is not True or
                pick.get('capability',{}).get('requested') != 'frontier'):
            raise DataBoundaryError('reference router evidence differs')
        labels.append({**parsed['label'], 'id':anchor, 'provenance':'reference',
                       'reference_model':parsed['served_model'], 'reference_effort':'ultra',
                       'reference_thread_id':parsed['thread_id']})
    if category_only:
        from .filter_metrics import LABELS
        decisions = {}
        for row in labels:
            if row.get('label') not in LABELS or not isinstance(row.get('rationale'),str) or not row['rationale'].strip():
                continue
            try:
                validate_labels([row],data)
                row['full_schema_valid'] = True
            except (DataBoundaryError, TypeError, KeyError):
                row['full_schema_valid'] = False
                row['signals'] = None  # No invented signal or signal-agreement credit.
            decisions[row['id']] = row
        return decisions
    return validate_labels(labels, data)


def agreement(runs, expected_ids):
    """Three-category consistency; missing calls are not negative votes."""
    if len(runs) != 3:
        raise DataBoundaryError('agreement requires three independent runs')
    complete = sorted(set(expected_ids).intersection(*map(set,runs)))
    missing = sorted(set(expected_ids)-set(complete))
    unanimous, pairs, disagreements, no_majority, signals, signal_cases = 0, 0, [], [], 0, 0
    majority = {}
    for anchor in complete:
        threads = [r[anchor].get('reference_thread_id') for r in runs]
        if any(threads) and (not all(threads) or len(set(threads)) != 3):
            raise DataBoundaryError('agreement runs reuse an underlying reference thread')
        values = [r[anchor]['label'] for r in runs]
        counts = Counter(values)
        winner,count = counts.most_common(1)[0]
        unanimous += count == 3
        pairs += sum(values[i] == values[j] for i,j in ((0,1),(0,2),(1,2)))
        if all(isinstance(r[anchor].get('signals'),list) for r in runs):
            signal_cases += 1
            signals += len({tuple(sorted(r[anchor]['signals'])) for r in runs}) == 1
        if count != 3:
            disagreements.append(anchor)
        if count < 2:
            no_majority.append(anchor)
        else:
            majority[anchor] = winner
    n = len(complete)
    fraction = lambda x,d: dict(count=x, denominator=d, rate=x/d if d else None)
    totals = Counter(r[a]['label'] for r in runs for a in complete)
    chance = sum((count/(3*n))**2 for count in totals.values()) if n else None
    kappa = (pairs/(3*n)-chance)/(1-chance) if n and chance < 1 else None
    return dict(complete_cases=n, expected_cases=len(set(expected_ids)), missing_ids=missing,
                per_run_missing=[len(set(expected_ids)-set(r)) for r in runs],
                unanimous=fraction(unanimous,n), pairwise=fraction(pairs,3*n),
                signals_unanimous=fraction(signals,signal_cases), disagreement_ids=disagreements,
                per_run_labels=[dict(Counter(r[a]['label'] for a in complete)) for r in runs],
                unanimous_labels=dict(Counter(runs[0][a]['label'] for a in complete if
                                               len({r[a]['label'] for r in runs}) == 1)),
                fleiss_kappa=kappa,
                pairwise_by_run={f'{i}-{j}':fraction(sum(runs[i][a]['label'] == runs[j][a]['label']
                                                        for a in complete),n)
                                 for i,j in ((0,1),(0,2),(1,2))},
                no_majority_ids=no_majority, majority_labels=majority,
                note='Consistency of provisional model judgments, not accuracy or human ground truth.')
