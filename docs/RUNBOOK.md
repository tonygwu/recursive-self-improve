# Deliberate local operations

Use an explicit private configuration and keep operational state outside Git.
Start with [the workflow guide](WORKFLOW.md) for review, delivery and rollback.
Existing installations need the [explicit database upgrade](UPGRADING.md).

```sh
uv run selfimprove --config /absolute/private/config.toml status
uv run selfimprove --config /absolute/private/config.toml dashboard
uv run selfimprove --config /absolute/private/config.toml worker --once
```

The dashboard records decisions. The instruction worker executes one authorized
command or reconciles one interrupted operation without model calls. Inspect the
result and any named failure before retrying.

Mining is a separate, deliberate action:

```sh
uv run selfimprove --config /absolute/private/config.toml run --review-only --max-cheap-calls 10
```

It reads configured histories, writes state and spends model calls. Mining and
gate budgets are separate. Review-only holds instruction changes; removing that
flag does not grant automatic-class consent. Do not use `run --dry-run` as a
read-only rehearsal because it writes scan and incident state.

For a disposable demonstration:

```sh
uv run python examples/dashboard_demo.py
```

For gate inspection, select the private state directory explicitly:

```sh
SI_STATE_DIR=/absolute/private/state ops/inspect-gate-verdicts.sh RUN_ID
```

Replace RUN_ID with the intended identifier. Inspect trial outputs, error causes
and served-model identity. A harness failure is not a rule verdict. Inspection
output can contain private evidence; do not paste it into public issues.

To refresh recorded instruction inventories and rule availability without model calls:

```sh
uv run selfimprove --config /absolute/private/config.toml observe-availability
```

This command reads known working copies and configured global instruction files.
It writes observations to the selected state database. It does not write instruction
targets. The Project page reads those retained observations. Availability does not
establish that a session loaded a rule or that the rule improved its behavior.

## Schema maintenance

Stop the dashboard, both workers and any scheduled run before upgrading a schema.
The command does not manage services. Preview the selected database first:

```sh
uv run selfimprove --config /absolute/private/config.toml upgrade-state --dry-run
uv run selfimprove --config /absolute/private/config.toml upgrade-state \
    --backup /absolute/private/backups/schema-upgrade-new
```

The backup directory must be new and outside Git. It holds a complete SQLite
backup, including committed WAL data, and a checksummed manifest. The command
verifies that backup before it applies the pending migrations in one transaction.
A failure preserves the before-state and any completed backup, so choose a new
backup directory for another attempt. Replaying a completed upgrade changes
nothing. Use `--initialize` only for a new installation with no database. The
command never restores a backup automatically. See [upgrading](UPGRADING.md).

## Explicitly requested model jobs

```sh
uv run selfimprove --config /absolute/private/config.toml jobs --check
uv run selfimprove --config /absolute/private/config.toml jobs --once
```

The model worker runs only jobs you requested from the dashboard, such as mining
a selected incident or regenerating an evaluation. It spends model calls against
a frozen per-job budget. `--check` is a read-only preflight and calls no model.
Completed steps replay without new calls. An interrupted step of unknown outcome
blocks rather than guessing a verdict.

## Optional managed services

Nothing here runs unattended unless you install it yourself. Installation starts
the selected service, and the model service can spend calls for jobs already
requested. Both preflights are read-only.

```sh
uv run selfimprove --config /absolute/private/config.toml worker --check
uv run selfimprove --config /absolute/private/config.toml service delivery install
uv run selfimprove --config /absolute/private/config.toml service delivery status
uv run selfimprove --config /absolute/private/config.toml service delivery uninstall
```

Installation records the selected config, database, uv executable and runtime PATH.
A missing database, a pending upgrade or a changed database selection prevents
startup. Registration alone does not prove the worker is healthy.

## Native session reports

```sh
uv run selfimprove --config /absolute/private/config.toml session-hook-settings --provider claude
```

This prints quoted hook settings. It does not install them. Installed hooks send
bounded session metadata to `record-session-event`, which writes retained reports.
A report says what the hook observed at session start. Exact loaded bytes, file
revision and continuity remain unknown, so a report is not a receipt that any
instruction reached the model.

Back up a database consistently before maintenance. The tracked-checkout backup
tool does not back up SQLite. Read [the data boundary](DATA_BOUNDARY.md) before
rebuild or export operations.
