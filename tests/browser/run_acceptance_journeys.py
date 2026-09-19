"""Run the isolated PRD journeys and queue regression sequentially with the supplied server helper."""
from pathlib import Path
import argparse
import errno
import hashlib
import importlib.util
import json
import shlex
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'reports/dashboard-parity/journeys'
JOURNEYS = {
    'J1': ('overview_demo.py', 'verify_overview.py', 'Weekly audit'),
    'J2': ('historical_approvals_demo.py', 'verify_historical_approvals.py', 'Review to delivery'),
    'J3': ('evidence_browser_demo.py', 'verify_evidence_browser.py', 'Four diagnosis and recovery paths'),
    'J4': ('quality_dashboard_demo.py', 'verify_quality_dashboard.py', 'Quality judgment and class consent'),
    'J5': ('navigation_layout_demo.py', 'verify_navigation_layout.py', 'Project context and copy navigation'),
    'J6': ('recurrence_demo.py', 'verify_recurrence.py', 'Retained recurrence in Projects and Evals'),
    'J2Q': ('overview_demo.py', 'verify_review_shortcuts.py', 'Ordinary Review shortcut focus and queue completion'),
    'J2R': ('delivery_recovery_demo.py', 'verify_delivery_recovery.py', 'Delivery states, partial retry and reviewed rollback resolution'),
    'J2I': ('ordinary_rollback_demo.py', 'verify_ordinary_rollback.py', 'Ordinary ready inverse through native intent, separate worker and replay'),
    'J2M': ('model_jobs_demo.py', 'verify_model_jobs.py', 'Bounded model jobs through failure, restart, cancellation and manual Review'),
}


def source_hashes():
    paths = [path for path in (ROOT / 'src/self_improve').rglob('*')
             if path.suffix in {'.py', '.js', '.css', '.html', '.svg'}]
    paths += [ROOT / 'tests/browser' / name for pair in JOURNEYS.values() for name in pair[:2]]
    paths.append(ROOT / 'tests/browser/keyboard_actions.py')
    paths.append(ROOT / 'tests/browser/visual_checks.py')
    paths.append(Path(__file__).resolve())
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-helper', type=Path, required=True,
                        help='Path to the webapp-testing with_server.py helper')
    parser.add_argument('--only', choices=list(JOURNEYS), nargs='+', default=list(JOURNEYS))
    args = parser.parse_args()
    helper = args.server_helper.resolve()
    if not helper.is_file():
        parser.error('The supplied server helper does not exist')
    missing = [name for name in ('fastapi', 'uvicorn') if importlib.util.find_spec(name) is None]
    if missing:
        parser.error('Missing dashboard dependencies: ' + ', '.join(missing) +
                     '. Run with uv run --extra dashboard --with playwright python '
                     'tests/browser/run_acceptance_journeys.py --server-helper PATH (and any --only selection).')
    OUT.mkdir(parents=True, exist_ok=True)
    tag = '-'.join(args.only)
    result = {'base_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'source_hashes': source_hashes(), 'journeys': [], 'status': 'attempted'}
    summary = OUT / (tag + '-result.json')
    def save():
        summary.write_text(json.dumps(result, indent=2) + '\n')
    save()
    for key in args.only:
        fixture, verifier, purpose = JOURNEYS[key]
        with socket.socket() as connection:
            connection.settimeout(1)
            available = connection.connect_ex(('127.0.0.1', 8876)) == errno.ECONNREFUSED
        if not available:
            raise RuntimeError('Port 8876 is not available; refusing to reuse or stop another server')
        log = OUT / (key + '.log')
        server_log = OUT / (key + '-server.log')
        server = shlex.join([sys.executable, 'tests/browser/' + fixture])
        server = 'exec ' + server + ' > ' + shlex.quote(str(server_log)) + ' 2>&1'
        command = [sys.executable, str(helper), '--server', server, '--port', '8876', '--timeout', '90', '--',
                   'uv', 'run', '--offline', '--with', 'playwright', 'python', 'tests/browser/' + verifier]
        record = {'id': key, 'purpose': purpose, 'fixture': fixture, 'verifier': verifier,
                  'status': 'attempted', 'log': str(log.relative_to(ROOT))}
        result['journeys'].append(record); save()
        print(f'{key}: {purpose}', flush=True)
        started = time.monotonic()
        with log.open('w') as output:
            process = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
        record.update(returncode=process.returncode, harness_seconds=round(time.monotonic() - started, 3),
                      status='succeeded' if process.returncode == 0 else 'failed')
        save()
        print(f'{key}: {record["status"]}, exit {process.returncode}; {record["log"]}', flush=True)
        if process.returncode:
            result['status'] = 'failed'; save()
            return process.returncode
        if source_hashes() != result['source_hashes']:
            result['status'] = 'failed'; result['reason'] = 'Source changed during the journey'; save()
            raise RuntimeError(result['reason'])
    result['status'] = 'succeeded'; save()
    print(f'ACCEPTANCE_JOURNEYS_OK: {len(result["journeys"])} journeys; task timings remain in the individual logs', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
