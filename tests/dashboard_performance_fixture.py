"""Invented larger Store for dashboard latency; real scan and availability producers."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid

from self_improve import project_measurements as pm
from self_improve.dashboard.app import create_app
from self_improve.execution_policy import set_class_policy
from self_improve.llm import LLMRunner
from self_improve.redact import redact_text
from self_improve.store import Store
from tests import conftest as boundary
from tests.test_apply import OLD, make_diff, init_git_repo
from tests.test_dashboard_queries import _run
from tests.test_project_measurements import cohort, calculate
from tests.test_scan_observations import PROJECT, make_repo, scan
from tests.test_scan_occurrences import write_codex, x_call, x_user, x_tokens
from tests.test_session_context import env as context_env, meta

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/dashboard-parity/performance'
NOW = datetime(2030, 2, 2, tzinfo=timezone.utc)
TOKEN = 'retained performance needle 49999'


def install_guard():
    def audit(event, args):
        boundary._audit(event, args)
        if boundary._violations:
            raise AssertionError('Performance fixture attempted private-resource access')
    boundary._armed = True
    sys.addaudithook(audit)
    def no_models(*args, **kwargs):
        raise AssertionError('Performance fixtures cannot call models')
    LLMRunner._execute = no_models


def hashes():
    paths = list((ROOT / 'src/self_improve').rglob('*'))
    paths += [ROOT / 'tests/dashboard_performance_fixture.py', ROOT / 'tests/measure_dashboard_performance.py',
              ROOT / 'tests/browser/performance_demo.py', ROOT / 'tests/browser/verify_dashboard_performance.py']
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.is_file() and p.suffix in {'.py', '.js', '.css', '.html', '.svg'}}


def snapshot(db_path):
    digest = hashlib.sha256()
    with __import__('contextlib').closing(Store(db_path, read_only=True)) as store:
        for line in store.conn.iterdump():
            digest.update(line.encode()); digest.update(b'\n')
        assert store.query_one('SELECT COUNT(*) n FROM llm_calls')['n'] == 0
    return digest.hexdigest()


def unchanged(manifest):
    assert snapshot(manifest['db']) == manifest['snapshot']
    for path, digest in manifest['targets'].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, path
    assert hashes() == manifest['source_hashes'], 'Runtime or verifier changed during measurement'


def app(env):
    return create_app(env.cfg, db_path=env.store.db_path, clock=lambda: NOW)


@contextmanager
def build(root):
    started = time.monotonic()
    assert redact_text(TOKEN) == TOKEN, "Search fixture text must survive product redaction"
    generator = context_env.__wrapped__(root)
    env = next(generator)
    env.cfg = replace(env.cfg, claude_history_path=str(root / 'absent-history'),
                      production_repo_path=str(root / 'absent-production'))
    try:
        print('PERF_PREP cohort', flush=True)
        revision, key = cohort(env)
        # The cohort needs class consent to prepare its actual fixture delivery.
        # Dashboard acceptance starts with every automatic class off.
        set_class_policy(env.store, 'project', False)
        measurement = calculate(env, revision, key)
        assert measurement['computable'] and measurement['after']['eligible_lines'] == 80
        env.store.insert('runs', {'id': 'perf-measurement', 'started': '2030-02-01T00:00:00Z'})
        env.store.commit()
        with env.store.transaction(write=True):
            measurement_id = pm.record_project_measurement(env.store, run_id='perf-measurement', measurement=measurement)
        projects = []
        sessions = []
        months = ['2029-08', '2029-09', '2029-10', '2029-11', '2029-12', '2030-01', '2030-02']
        for project in range(100):
            repo = make_repo(root / 'work' / f'project-{project:03}', f'https://example.test/performance/project-{project:03}.git')
            init_git_repo(Path(repo), 'AGENTS.md', OLD)
            projects.append(repo)
            for number in range(20):
                ident = project * 20 + number
                when = f'{months[ident % 7]}-01T00:00:00Z'
                sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f'performance/session/{ident}'))
                records = [meta(repo, when, sid), x_call(when, f'call-{ident}'), x_user("no, that's wrong", when)]
                records += [x_tokens(when) for _ in range(97)]
                sessions.append(write_codex(env, records, name=f'rollout-performance-{ident:04}.jsonl'))
        print('PERF_PREP real scan: 2,000 files / 200,000 lines', flush=True)
        stats = scan(env, 'performance-scan')
        assert stats.files_succeeded == 2000 and stats.files_failed == 0, stats
        assert stats.measurement['lines_observed'] == 200000, stats.measurement
        print('PERF_PREP retained rows', flush=True)
        store = env.store
        for n in range(5000):
            store.insert('learnings', {'id': f'perf-rule-{n:05}', 'title': f'Invented performance rule {n:05}',
                'rule_text': f'Inspect invented result {n} before changing instructions.', 'why': 'Invented retained reasoning. ' * 8,
                'incident_summary': f'Ten retained fixture examples for rule {n}.', 'created_at': '2030-01-25T00:00:00Z',
                'last_seen': '2030-02-01T00:00:00Z', 'status': 'candidate', 'evidence_count': 10, 'project_count': 10})
        for n in range(10000):
            store.insert('proposals', {'id': f'perf-proposal-{n:05}', 'learning_id': f'perf-rule-{n % 5000:05}',
                'target_path': str(Path(projects[n % 100]) / 'AGENTS.md'), 'target_kind': 'project_agents_md',
                'action': 'add', 'status': 'pending' if n < 100 else 'superseded',
                'created_at': '2030-01-26T00:00:00Z', 'diff_unified': make_diff(OLD, OLD + f'Invented proposal {n}.\n')})
        for n in range(50000):
            project = (n // 5000 + n % 100) % 100
            session = project * 20 + n % 20
            token = TOKEN if n == 49999 else f'Invented occurrence {n}'
            store.insert('incidents', {'id': f'perf-incident-{n:05}', 'session_file': str(sessions[session]),
                'session_id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'performance/session/{session}')),
                'project_path': projects[project], 'project_key': f'remote:example.test/performance/project-{project:03}',
                'signal_type': 'correction', 'matched_text': token, 'window_json': json.dumps([{'role':'user','text':('Invented context. ' * 40) + token}]),
                'status': 'mined', 'ts': '2030-02-01T00:00:00Z', 'created_at': '2030-02-01T00:00:00Z'})
            store.insert('incident_learnings', {'incident_id': f'perf-incident-{n:05}', 'learning_id': f'perf-rule-{n % 5000:05}'})
        for n in range(250):
            when = NOW - timedelta(days=250-n)
            stats = {'run_id': f'perf-run-{n:03}', 'review_only': True, 'budget_limits': {'cheap': 80, 'expensive': 12},
                     'scan': {'files_attempted': 8, 'files_succeeded': 8, 'files_failed': 0},
                     'mine': {'attempted': 8, 'succeeded': 8, 'failed': 0},
                     'cluster': {'candidates': 8, 'mode': 'agentic_passthrough'},
                     'gate': {'attempted': 0, 'gated_pass': 0, 'gated_fail': 0, 'ungated': 0, 'inconclusive': 0, 'failed': 0, 'refused': 0},
                     'apply': {'attempted': 0, 'applied': 0, 'held': 0, 'failed': 0}}
            _run(store, run_id=f'perf-run-{n:03}', started=when.isoformat(), finished=(when+timedelta(minutes=1)).isoformat(), stats=stats)
        store.commit()
        counts = {table: store.query_one(f'SELECT COUNT(*) n FROM {table}')['n'] for table in
                  ['learnings','proposals','incidents','sessions','runs','scan_lines','scan_occurrences','project_stats','rule_availability_observations']}
        counts['projects'] = store.query_one('SELECT COUNT(DISTINCT project_key) n FROM sessions')['n']
        counts['active_physical_lines'] = store.query_one('SELECT COUNT(*) n FROM scan_lines WHERE active=1')['n']
        counts['active_signals'] = store.query_one("SELECT COUNT(*) n FROM scan_occurrences WHERE active=1 AND kind='signal'")['n']
        assert counts['active_physical_lines'] == 200160 and counts['active_signals'] == 2030, counts
        assert counts['sessions'] == 2040 and counts['projects'] == 101, counts
        targets = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (root / 'work').glob('*/AGENTS.md')}
        manifest = {'db': str(store.db_path), 'project_key': PROJECT, 'large_project': 'remote:example.test/performance/project-000',
                    'rule_id': 'perf-rule-00000', 'measurement_id': measurement_id, 'run_id': 'perf-run-249',
                    'counts': counts, 'targets': targets, 'source_hashes': hashes(), 'snapshot': snapshot(store.db_path),
                    'store_bytes': {p.name: p.stat().st_size for p in store.db_path.parent.glob('state.db*')},
                    'base_revision': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                    'python': sys.version, 'platform': platform.platform(), 'preparation_seconds': round(time.monotonic()-started,3),
                    'limits': 'Application-cold only; OS cache not flushed. Synthetic retained history and real scanner occurrence counts are distinct.'}
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        print('PERF_FIXTURE_READY', json.dumps(counts), flush=True)
        yield env, manifest
    finally:
        generator.close()
