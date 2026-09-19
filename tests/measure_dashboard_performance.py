"""Measure real dashboard API reads over the published invented population."""
import argparse
import cProfile
import json
import math
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from tests.dashboard_performance_fixture import OUT, TOKEN, app, build, install_guard, unchanged
from self_improve.scan_observations import logical_session_key
from self_improve.mining_history import digest
import uuid


def valid_review_preview(data):
    """Validate the real selected source binding, not merely a nonempty response."""
    if [m['proposal_id'] for m in data['members']] != ['perf-proposal-00000'] or not data['ready']:
        return False
    packet = data['evidence_identity']
    sources = {i['id']: i for m in data['members'] for i in m['snapshot']['evidence']}
    records = packet['records']
    return (packet['profile'] == 'review-evidence/1' and packet['preview_revision'] == data['revision']
            and len(records) == len(sources) == 10
            and {r['incident_id'] for r in records} == set(sources)
            and all(r['source_revision'] == digest(sources[r['incident_id']])
                    and r['identity']['project']['key'] == sources[r['incident_id']]['project_key']
                    and r['identity']['session']['provider'] == 'codex' for r in records))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=20, help='Non-default values are diagnostic only')
    parser.add_argument('--only', help='One named endpoint for diagnosis')
    parser.add_argument('--profile', action='store_true', help='Profile one extra read per endpoint after timing')
    args = parser.parse_args()
    if args.samples < 1: parser.error('--samples must be positive')
    install_guard()
    result = {'status': 'attempted', 'samples': args.samples, 'diagnostic_only': args.samples != 20 or bool(args.only), 'endpoints': []}
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / ('api-' + (args.only or 'all') + f'-{args.samples}.json')
    def save(): dest.write_text(json.dumps(result, indent=2) + '\n')
    try:
        with tempfile.TemporaryDirectory(prefix='si-performance-ui-') as directory:
            with build(Path(directory).resolve()) as (env, manifest):
                result['manifest'] = manifest; save()
                q = urlencode({'project_key': manifest['project_key']})
                cases = {
                    'overview': ('/api/overview', lambda d: d['inbox']['count'] == 100),
                    'rules': ('/api/rules/browse', lambda d: d['counts']['all_members'] == 5001),
                    'rule-search': ('/api/rules/browse?' + urlencode({'query': TOKEN}), lambda d: d['counts']['members'] == 1),
                    'rule-detail': ('/api/rules/' + manifest['rule_id'], lambda d: bool(d)),
                    'review': ('/api/review-queue', lambda d: d['count'] == 100),
                    'review-preview': ('/api/review-preview?proposal_ids=perf-proposal-00000', valid_review_preview),
                    'projects': ('/api/projects', lambda d: d['count'] == 101),
                    'project-detail': ('/api/project-detail?' + q, lambda d: d['project_key'] == manifest['project_key']),
                    'exposure': ('/api/project-exposure?' + urlencode({'project_key':manifest['large_project'],'start':'2029-08-01T00:00:00Z','end':'2030-03-01T00:00:00Z'}), lambda d: d['eligible_lines'] == 2000 and d['occurrences'] == 20),
                    'trends': ('/api/incident-rate?end_month=2030-02', lambda d: bool(d)),
                    'recurrence': ('/api/project-measurements?' + q, lambda d: d['count'] == 1),
                    'evidence-search': ('/api/evidence?' + urlencode({'query':TOKEN}), lambda d: d['pagination']['count'] == 2 and {(r['kind'],r['source_id']) for r in d['rows']} == {('incident','perf-incident-49999'),('session',logical_session_key('codex',str(uuid.uuid5(uuid.NAMESPACE_URL,'performance/session/179'))))}),
                    'run': ('/api/runs/' + manifest['run_id'], lambda d: d['run']['id'] == manifest['run_id']),
                }
                if args.only:
                    if args.only not in cases: parser.error('Unknown --only endpoint')
                    cases = {args.only: cases[args.only]}
                for name, (url, validate) in cases.items():
                    record = {'name': name, 'url': url, 'reads': []}
                    result['endpoints'].append(record); save()
                    current_app = app(env)
                    with TestClient(current_app) as client:
                        count = [0]
                        def traced(statement): count[0] += 1
                        client.portal.call(current_app.state.store.conn.set_trace_callback, traced)
                        for sample in range(args.samples + 1):
                            count[0] = 0; start = time.perf_counter()
                            response = client.get(url)
                            seconds = time.perf_counter() - start
                            record['reads'].append({'kind': 'first' if sample == 0 else 'warm', 'seconds': seconds,
                                                    'sql_statements': count[0], 'bytes': len(response.content), 'status': response.status_code})
                            save()
                            assert response.status_code == 200, (name, response.text[:2000])
                            assert validate(response.json()), (name, response.json())
                        times = [r['seconds'] for r in record['reads'][1:]]
                        record.update(first_seconds=record['reads'][0]['seconds'], warm_p95_seconds=sorted(times)[math.ceil(.95*len(times))-1])
                        record['budget_pass'] = record['first_seconds'] <= 5 and record['warm_p95_seconds'] <= 2
                        if name == 'evidence-search':
                            cache = current_app.state.evidence_search_cache
                            record['retained_search_bytes_upper_bound'] = cache.retained_bytes
                            record['search_cache_capacity_bytes'] = cache.max_bytes
                            assert 0 < cache.retained_bytes <= cache.max_bytes
                        if args.profile:
                            profile = cProfile.Profile()
                            client.portal.call(profile.enable)
                            try: client.get(url)
                            finally: client.portal.call(profile.disable)
                            profile.dump_stats(str(OUT / (name + '.prof')))
                        save()
                    print('PERF_API', name, json.dumps({k:v for k,v in record.items() if k not in {'reads','url'}}), flush=True)
                unchanged(manifest)
                result['unchanged'] = True
                result['model_calls'] = 0
                result['budget_failures'] = [r['name'] for r in result['endpoints'] if not r['budget_pass']]
                result['status'] = 'failed' if result['budget_failures'] else 'succeeded'
                save()
    except Exception as error:
        result.update(status='failed', error=type(error).__name__+': '+str(error))
        save()
        raise
    print('PERF_API_RESULT', result['status'], 'failures', result['budget_failures'], flush=True)
    return 1 if result['budget_failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
