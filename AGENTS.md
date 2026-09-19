# recursive-self-improve development

The Python package remains `self_improve` and the CLI remains `selfimprove`.
Read [preview scope](docs/RESEARCH_PREVIEW.md), [workflow](docs/WORKFLOW.md), and
[data boundary](docs/DATA_BOUNDARY.md) before changing behavior.

## Verification

- Install the optional API dependencies: `uv sync --extra dashboard --frozen`.
- Run the full suite before committing: `uv run --extra dashboard pytest -q -ra`.
- Run boundary acceptance: `uv run pytest -q tests/test_data_boundary.py`.
- Package changes must pass `uv run pytest -q tests/test_distribution.py`.
- Inspector changes must pass `uv run pytest -q tests/test_gate_inspection.py`.
  `ops/inspect-gate-verdicts.sh` uses the read-only Store and selected private state.
- Upgrade-guide changes must pass `uv run --extra dashboard pytest -q
  tests/test_upgrade_guide.py tests/test_state_upgrade.py`. The published
  procedure in `docs/UPGRADING.md` is executed by that test, so keep exactly one
  heredoc in it and keep its required-migration set current.
- Browser contracts live in `tests/browser/`. Each verifier owns one contract and
  runs against its own temporary fixture server. Serve the fixture, run the
  verifier, then stop the fixture. They require a free port, an installed
  Playwright browser, invented state and zero model calls.
- Use temporary invented histories, databases and instruction targets in tests.
  Do not read personal configuration, session trees, sibling checkouts or live state.
  Preserve every runtime/safety regression; use subprocesses for import isolation.

## Storage and execution

- All database access goes through `store.py`. Readers use `Store(read_only=True)`
  and cannot migrate. Workers and dashboard startup do not perform migrations.
- Keep live state in external SQLite. Corpus, qrels and labels are one private,
  versioned, checksummed bundle. Qrels also contain source text. Never silently
  substitute synthetic inputs for a missing or invalid private benchmark.
- Verify an external backup before removing private data. Preserve the only
  surviving evidence for an incident. Do not rewrite history or install services
  as an incidental development action.
- Redact excerpts before storage/model prompts, then inspect the rendered prompt.
  Redaction is not publication clearance. Bind evaluation identity to the actual
  served model and retained evidence, not a successful process exit.
- `run --dry-run` writes scan and incident state. Use the disposable dashboard demo
  for a rehearsal without personal history.

## Decisions and delivery

Automatic target classes start off. Only new gated-pass proposals in an explicitly
enabled class can apply automatically. Special manual-only cases remain manual.
Use `automatic_permission` and `waiting_proposals` from `execution_policy.py`.
The legacy `auto_apply` field never grants consent.

An approval binds the complete reviewed base: every new approval supplies its
`preview_revision`, and a changed target requires a fresh review. A rejection
binds only its member revisions, so it must not carry `preview_revision`.
Rule families, recurrence measurements and native session reports are read-only
evidence. None of them grants approval or delivery authority.

Two workers exist and neither migrates at startup. `worker` executes approved
instruction commands without model calls. `jobs` executes only explicitly
requested model jobs, against a frozen per-job budget, under its own lock.
Completed steps replay without new calls; an interrupted step of unknown outcome
blocks instead of inventing a verdict.

The dashboard records exact-revision intent. It does not import the file writer.
Instruction writes stay in `apply.py` and the explicit worker, with snapshots,
locks and durable file/ref checkpoints. Preserve stale-review rejection,
idempotency, interruption recovery, scoped rejection and conflict-aware rollback.
Do not erase evidence of an observed write when later decisions change.

Keep Python imports, CLI names, state paths and delivery-branch conventions stable.
Derive repository roots from source/script locations. Preserve unrelated edits
when writing or reversing an instruction change. Do not touch the production
checkout, live database or installed schedules during tests.
