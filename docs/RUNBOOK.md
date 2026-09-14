# Deliberate local operations

Use an explicit private configuration and keep operational state outside Git.
Start with [the workflow guide](WORKFLOW.md) for review, delivery and rollback.

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

Back up a database consistently before maintenance. The tracked-checkout backup
tool does not back up SQLite. Read [the data boundary](DATA_BOUNDARY.md) before
rebuild or export operations. This preview installs no unattended service.
