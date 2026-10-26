# Optional gated deployment

`ops/deploy-prod.sh` fast-forwards a production checkout to `origin/main`, but
only after the full test suite passes against that exact commit and the live
database already carries its schema. The production checkout is
`cfg.production_repo_path`. In a source checkout it defaults to a sibling named
`repo-prod`. A wheel install defaults to a folder inside the state directory,
where the staging clone cannot work, so set the path explicitly outside it.
Automation runs from that checkout, and nobody edits it by hand.

Nothing installs this job for you. It is a macOS launchd job, and it is useful
only if you run the nightly pipeline from a separate production checkout.

## Schedule

| Job | Label | Time | Script |
| --- | --- | --- | --- |
| Gated deploy | `com.tonygwu.self-improve.deploy` | 01:00 daily | `<production checkout>/ops/deploy-prod.sh` |
| Pipeline | `com.tonygwu.self-improve` | 02:30 daily | `<production checkout>/ops/run-nightly.sh` |

The deploy runs first, so a commit that passes the gate is exercised by the same
night's pipeline. Both jobs take the same lock, `<state>/run.lock`, with the same
mkdir-and-stale-PID protocol. A deploy therefore cannot swap code under a running
pipeline. If a deploy overruns into 02:30, the nightly skips that night. A skipped
night is the safe failure. `tests/test_deploy_prod.py` pins the shared lock path
so the two scripts cannot drift apart.

## What the gate checks, in order

1. **Production preconditions.** The checkout is a Git checkout on `main` with a
   clean tree. A dirty production tree is refused immediately.
2. **Fast-forward only.** The candidate must descend from the deployed commit.
   A diverged checkout is refused, never merged or reset.
3. **Staging clone.** `<parent of the production checkout>/.deploy-stage` is
   reset to the candidate and cleaned, keeping only its `.venv`. Its `origin` URL
   must equal the production checkout's, so the suite cannot pass for a different
   repository. The tests run in this clone, never in production. The stage must
   not sit inside the state directory, because the test suite's hermeticity guard
   forbids reads there and every test that reads its own source would fail.
4. **Full suite.** `uv run --locked --extra dashboard pytest -q`. The dashboard extra is
   required, because without it the API tests skip and the run looks green.
   pytest exits 0 when it collects nothing, so the log must also contain a
   `N passed` line.
5. **Schema.** The candidate's code runs `uv run --locked selfimprove upgrade-state --dry-run`
   against the live database, read-only. That command exits 0 whether or not
   upgrades are pending, so the gate reads the JSON `pending` list. A non-empty
   list blocks the deploy and names the migrations.
6. **Fast-forward and verify.** `git merge --ff-only` to the tested SHA, then a
   fresh read of `HEAD` and `git status` confirms the result.
7. **Dependency sync.** `uv sync --locked --project <production checkout> --extra dashboard`
   surfaces a broken resolve before the nightly. A sync failure does not roll the
   checkout back, because the code is already the code that passed.

Browser verifiers are not part of the gate. They need a free port and an
installed Chromium, and a flaky browser should not block an unattended deploy of
a headless pipeline. Run them by hand as `AGENTS.md` describes.

## Self-modification guard

The script lives inside the checkout it updates, and bash reads a script from
disk while it runs. A fast-forward that rewrites `ops/deploy-prod.sh` mid-run
could make bash resume at the wrong byte offset. The script therefore copies
itself to a temporary `si-deploy-prod.XXXXXX` file and re-executes from there
before it touches Git. The exit trap removes the copy. The log line
`deploy: executing <path> (production checkout <root>)` shows which file ran,
and a test asserts that it is never the checkout's own copy.

## Status and history

Both files live under `<state>/deploy/`:

- `last-deploy.json` holds the most recent attempt.
- `history.jsonl` holds one line per attempt and is append-only.

Each record carries `started_at` and `finished_at` in UTC. Every attempt ends in
exactly one status from a closed vocabulary:

| Status | Exit | Meaning |
| --- | --- | --- |
| `deployed` | 0 | The production checkout moved to the tested commit |
| `noop_already_current` | 0 | Already at the candidate |
| `failed_lock` | 0 | The pipeline lock is held; not a deployment failure |
| `blocked_dirty_tree` | 1 | The production checkout has local modifications |
| `blocked_not_fast_forward` | 1 | The candidate does not descend from the deployed commit |
| `blocked_tests_failed` | 1 | The suite failed or reported no passing tests |
| `blocked_pending_migrations` | 1 | The live schema is behind the candidate's code |
| `failed_precondition` | 1 | The production checkout or its remote is unusable |
| `failed_stage` | 1 | The staging clone could not be prepared or identified |
| `failed_merge` | 1 | The fast-forward or its verification failed |

The full pytest output of the last gate run is `<state>/logs/deploy-tests.log`.
launchd's own capture is `<state>/logs/deploy.stdout.log` and `deploy.stderr.log`.

## Installing

The script must exist inside the production checkout before launchd can run it,
and that checkout only advances through a deploy. Break the circle once by hand:

```bash
# 1. One manual fast-forward, so the production checkout has the script.
git -C /absolute/path/to/repo-prod pull --ff-only origin main

# 2. Render, check and load the job.
uv run python -c "
from self_improve.config import load_config
from self_improve import launchd
print(launchd.render_deploy_plist(load_config()))
" > ~/Library/LaunchAgents/com.tonygwu.self-improve.deploy.plist
plutil -lint ~/Library/LaunchAgents/com.tonygwu.self-improve.deploy.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tonygwu.self-improve.deploy.plist
```

The first deploy also creates the staging clone and its environment, so it takes
longer than later runs.

## Running it by hand, and turning it off

```bash
# One attempt now, with the same gates. It refuses while the pipeline runs.
bash /absolute/path/to/repo-prod/ops/deploy-prod.sh

# Force one scheduled run through launchd.
launchctl kickstart gui/$(id -u)/com.tonygwu.self-improve.deploy

# Stop automatic deployment. The production checkout stays where it is.
launchctl bootout gui/$(id -u)/com.tonygwu.self-improve.deploy
```

## Schema and archive ordering

The gate refuses code whose migrations the live database lacks. The other
direction matters too. An ordinary Store open refuses a database whose
`schema_migrations` names a migration the code does not define, so the nightly,
`status`, `rollback` and `upgrade-state` all fail on a database that is ahead of
the deployed code. Apply a migration only together with, or after, deploying the
code that defines it. If a deploy is blocked, fix the deploy rather than
upgrading the database from a newer checkout.

Some format changes are not migrations, so the schema check cannot see them.
Each retained instruction-text archive records the redaction rules version that
wrote it, and code that does not list that version in `instruction_text.RULES`
refuses the archive. After the live database holds archives written under a
rules version, never point older code at it. The deployer only fast-forwards, so
it cannot move the production checkout backwards by itself; a manual reset can.

## Limits

- The gate measures the suite at the candidate commit on one machine. It does not
  establish that the nightly pipeline will succeed, which also depends on quota,
  providers and transcripts.
- A failing `origin/main` blocks every deploy until it is fixed. The script does
  not search backwards for a passing ancestor, so read `last-deploy.json`.
- Migrations are never applied automatically.
- The script restarts no service after a deploy. If you install the instruction
  or model worker as a service, restart it yourself after a deploy.
