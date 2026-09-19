"""Invented mixed-provider receipts through each exact generated hook command."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
root_repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root_repo))
import uvicorn
from self_improve.dashboard.app import create_app
from self_improve.native_loads import hook_settings, CODEX_EVENTS
from tests.test_session_context import env as context_env, meta, turn
from tests.test_scan_observations import scan, write_claude, c_user, ts, PROJECT, SID
from tests.test_scan_occurrences import write_codex, x_user
from tests.test_native_loads import config_file, event as claude_event
from tests.test_codex_session_reports import event as codex_event

out = root_repo / 'reports/dashboard-parity'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='si-codex-reports-') as temp:
    root = Path(temp).resolve()
    generator = context_env.__wrapped__(root)
    fixture = next(generator)
    try:
        write_claude(fixture, [c_user('Invented Claude activity', ts(2), fixture.repo)])
        write_codex(fixture, [meta(fixture.repo), turn(fixture.repo), x_user('Invented Codex activity', ts(2))])
        assert scan(fixture, 'mixed-native-browser').files_succeeded == 2
        config = config_file(fixture)
        target = Path(fixture.repo) / 'CLAUDE.md'
        target.write_text('Invented instruction text.\n')
        for provider, count in [('claude', 3), ('codex', 21)]:
            fragment = hook_settings(config_path=config, python_path=sys.executable, provider=provider)
            for i in range(count):
                kind = 'InstructionsLoaded' if provider == 'claude' else CODEX_EVENTS[i % len(CODEX_EVENTS)]
                payload = claude_event(fixture) if provider == 'claude' else codex_event(fixture, kind)
                command = fragment['hooks'][kind][0]['hooks'][0]['command']
                result = subprocess.run(command, shell=True, cwd=root, input=json.dumps(payload), text=True, capture_output=True)
                assert result.returncode == 0, result.stderr
                assert result.stdout == result.stderr == ''
        manifest = {'db':str(fixture.store.db_path), 'project_key':PROJECT, 'session_id':SID,
                    'target':str(target), 'target_text':target.read_text(), 'snapshot':list(fixture.store.conn.iterdump())}
        (out / 'codex-reports-manifest.json').write_text(json.dumps(manifest))
        uvicorn.run(create_app(fixture.cfg), host='127.0.0.1', port=8876)
    finally:
        generator.close()
