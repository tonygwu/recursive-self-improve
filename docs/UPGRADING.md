# Upgrade an existing preview installation

Version 0.1.2 adds observational recurrence measurements, human quality
judgments, native session context and loading reports, display-only rule
families, retained instruction text, and queue history. Its schema identifiers
are `0025_project_measurements`, `0029_quality_evidence`, `0030_session_context`,
`0031_native_load_reports`, `0032_rule_families`, `0033_instruction_text`, and
`0034_queue_history`.

Version 0.1.1 added rule availability, instruction inventories, and retained
evaluation attempts, as `0024_rule_availability`, `0028_instruction_inventory`,
and `0023_eval_attempts`. An installation still on 0.1.0 applies both sets in one
upgrade. Identifiers do not specify execution order. The Store applies missing
migrations in its declared order.

Dashboard readers and both workers do not migrate at startup. An old database
can show unavailable evidence or refuse new evaluation and inventory operations.
The disposable demo creates its own current schema and needs no upgrade.

## The supported command

`selfimprove upgrade-state` performs this upgrade. It makes no model calls and
writes no instruction target. Inspect the pending work first:

```sh
uv run selfimprove --config /absolute/private/config.toml upgrade-state --dry-run
```

Apply it only after the command has written and verified a new private backup:

```sh
uv run selfimprove --config /absolute/private/config.toml upgrade-state \
    --backup /absolute/private/backups/before-0.1.2
```

The backup directory must be new and outside every Git checkout. Use
`--database /absolute/private/copy.db` to upgrade one selected copy instead of the
configured state. Use `--initialize` only when no database exists yet. Stop the
dashboard, workers, and scheduled runs first; this command does not manage them.

The equivalent inline procedure below remains published and tested. Either path
is sufficient. Use the command unless you need to inspect each step.

## Back up and upgrade

Stop the dashboard, workers, and scheduled runs that use the selected database.
Install the updated code and frozen dependencies with `uv sync --extra dashboard --frozen`.
Choose an existing private backup directory outside every Git checkout.
Replace both example paths in this command with your configuration and a new backup filename.

```sh
uv run python - /absolute/private/config.toml /absolute/private/backups/before-0.1.2.db <<'PY'
from contextlib import closing
from pathlib import Path
import sys
from self_improve.config import load_config
from self_improve.data_boundary import private_destination
from self_improve.store import Store

config_path = Path(sys.argv[1])
if not config_path.is_file():
    raise SystemExit('The explicit configuration file does not exist.')
cfg = load_config(config_path)
database = cfg.state_path('state.db').resolve()
backup = Path(sys.argv[2]).expanduser()
if not database.is_file():
    raise SystemExit('The configured database does not exist.')
if backup.exists() or backup.is_symlink() or not backup.parent.is_dir():
    raise SystemExit('Choose a new backup file in an existing private directory.')
backup = private_destination(backup)

with closing(Store(database, read_only=True)) as source:
    with closing(Store(backup)) as saved:
        source.conn.backup(saved.conn)
        if [row['quick_check'] for row in saved.query('PRAGMA quick_check')] != ['ok']:
            raise SystemExit('Backup integrity failed. The source was not upgraded.')
        before = list(source.conn.iterdump())
        if before != list(saved.conn.iterdump()):
            raise SystemExit('Backup contents differ. The source was not upgraded.')
print('Verified private backup:', backup)

with closing(Store(database, migrate=True)) as upgraded:
    required = {'0023_eval_attempts', '0024_rule_availability', '0025_project_measurements',
                '0028_instruction_inventory', '0029_quality_evidence', '0030_session_context',
                '0031_native_load_reports', '0032_rule_families', '0033_instruction_text',
                '0034_queue_history'}
    installed = {row['name'] for row in upgraded.query('SELECT name FROM schema_migrations')}
    if not required <= installed:
        raise SystemExit('Required migrations are missing.')
print('PREVIEW_UPGRADE_OK')
PY
```

The backup uses SQLite's backup operation, including committed WAL content.
It does not copy database and sidecar files separately. The command compares
the backup contents before opening the source for migration. Keep writers
stopped until it prints `PREVIEW_UPGRADE_OK`.

Restart the dashboard and workers with the same explicit configuration.
Run `observe-availability` to collect fresh file observations when needed.
The [runbook](RUNBOOK.md) describes that command's reads and writes.

## Retained evidence and rollback limits

The upgrade preserves existing rows. It does not invent historical prompts,
model identities, rule availability, or evaluation-attempt links. New evaluations
retain their source evidence. Older unlinked results remain visible separately.

State rebuild now preserves retained execution history instead of refusing it.
It keeps command, operation, evaluation-attempt and quality roots together with
the source rows they reference, and it keeps `runs` and `llm_calls` untouched.
A rebuild still requires a new private backup directory outside Git, and it
verifies that backup before any deletion. A source transcript that changed or
became unreadable cannot qualify as rebuildable. Preview first with
`rebuild-state --dry-run --export /absolute/private/new-backup`.

The backup represents the state before this upgrade. Restoring it after further
work loses newer decisions and evidence. Stop all state users before recovery.
Keep instruction-file changes and database recovery separate. The existing
conflict-aware instruction rollback remains the supported way to reverse a delivery.
