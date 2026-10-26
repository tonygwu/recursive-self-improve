# Delivery targets

Self-improve uses two different paths for each global instruction surface:

- The **load path** is the file or folder that Claude Code or Codex reads.
  The flat keys `global_claude_md`, `codex_global_agents_md` and `skills_dir`
  name these paths. Observation, rule availability, the miner's in-force
  corpus and hook settings (`settings.json` beside `global_claude_md`) use them.
- The **delivery path** is the file or folder that self-improve writes an
  approved edit to. The optional `[targets]` table names these paths.

The two paths are often the same file. They differ when a load path is a
symlink into a checkout that automation must never write, such as a
`repo-prod` live install. Delivery into such a checkout is refused. Point the
delivery path at a working clone of the source repository instead.

## Format

The table lives in the same untracked `config.toml` as the other keys
(`~/.self-improve/config.toml` by default, or `selfimprove --config PATH`).
See [the example](../examples/config.example.toml).

```toml
[targets]
global_public  = "/home/you/self-improve/skills/context/global/AGENTS.md"
codex_global   = "/home/you/self-improve/skills/context/global/AGENTS.md"
global_private = "/home/you/.claude/rules/personal.md"
skills_dir     = "/home/you/self-improve/skills/skills"
```

| Key | Proposal kind | Meaning | Default when unset |
|---|---|---|---|
| `global_public` | `global_claude_md` | Shared rules that every harness loads. This file can be public. | `global_claude_md` |
| `codex_global` | `codex_global` | Codex-only global rules. It can be the same file as `global_public`. | `codex_global_agents_md` |
| `global_private` | `global_private` | Personal or machine-specific rules. Never publish this file. | Not configured |
| `skills_dir` | `skill` | The folder where new skills are created and whose existing `*/SKILL.md` files recovery can edit. | `skills_dir` |

Every key is optional. Without a `[targets]` table, every delivery path is
the load path, which was the behavior before this table existed.

The loader refuses an unknown key, a value that is not an absolute path, a
file whose parent folder does not exist, a `skills_dir` that is not an existing
folder, and any path with a `repo-prod` component. Code reads the table only
through `Config.delivery_path(kind)`.

## Choosing a destination: scope and routing

The miner picks one `scope_guess` per lesson. The shared guidance between the
`scope-guidance` markers in both `prompts/mine_incident*.md` files defines the
choices; `routing.route()` then maps the guess to a target:

| Scope | Target kind | Written to |
|---|---|---|
| `global` | `global_claude_md` | `delivery_path("global_claude_md")`, shared and possibly public |
| `personal` | `global_private` | `delivery_path("global_private")`, private to one person or machine |
| `project` | `project_agents_md` / `project_claude_md` | the learning's repository |
| `skill` | `skill` | a new skill under `delivery_path("skill")` |

`personal` is checked before the project-count promotion, so a personal
lesson seen in many projects never reaches the shared file, provided
`global_private` names a different file. Nothing yet refuses a `global_private`
path that names the shared file, so check it. When
`global_private` is not configured, routing raises `RoutingError` (counted as
`propose_RoutingError`) rather than publish the rule. The private kind shares
the `global` execution-policy class.

At mining time `skill` always proposes a new skill: the learnings table has no
column to name an existing one, and adding it needs a migration. An existing
skill is reachable through corrected-target recovery instead: the options list
every `*/SKILL.md` under `delivery_path("skill")` as "Existing skill · NAME",
except one missing from the delivery base (the `self-improve/rules` branch tip,
or `HEAD`) and anything inside `instruction_excluded_dirs`, which are reported
as unavailable. The generated `recover_rule` edit always waits for manual Review. Rule
availability treats an edit inside an existing skill as a marked bullet, and
only a newly created skill file as one whole-file rule.

Two checks measure the boundary:

- `uv run --extra dashboard pytest -q tests/test_routing_personal.py` covers
  the deterministic table, including the promotion order.
- `uv run selfimprove eval-scope --dataset evals/scope/boundary.yaml --out NEW_DIR [--repeat N]`
  asks the configured cheap model class to choose a scope for 32 invented
  lessons using the exact shipped guidance. It makes cases x repeat calls
  through a new disposable Store in `NEW_DIR`, never the configured state
  database, and reports accuracy over answered calls, failure causes,
  per-boundary results, the confusion matrix and the served models. It does
  not measure full mining or routing overrides.

## Delivery into a Git clone

If a delivery path is inside a Git repository, the writer does not change the
working tree. It commits the edit to the branch `project_branch_name`
(default `self-improve/rules`) under a temporary index. The working tree, index
and checked-out branch stay unchanged. Delivery refuses while that branch is
checked out in any working copy, so keep the clone on its default branch.

The branch starts from the clone's `HEAD` when it does not exist yet. Fetch and
fast-forward the clone's default branch before a review session if upstream
has moved; otherwise proposals are previewed against older content.

A person publishes delivered edits. For example:

```sh
git -C /home/you/self-improve/skills log --oneline main..self-improve/rules
git -C /home/you/self-improve/skills push origin self-improve/rules
# Open a pull request, review it, merge it, then refresh the live install.
```

If the source repository has its own edit protocol, follow it when you merge.
For example, a skills repository can require a context claim before anyone
changes its shared global instructions. Self-improve commits only to its own
clone's delivery branch; the merge is where that protocol applies.

## Changing the live configuration

The deployed checkout reads the configuration. A `[targets]` table makes an
older loader fail with "Unknown config key 'targets'". Add the table only after
the deployment that contains this loader has reached the production checkout.

## Verification

```sh
uv run --extra dashboard pytest -q tests/test_delivery_targets.py tests/test_config.py tests/test_routing.py tests/test_sandbox.py tests/test_propose.py tests/test_review_preview.py tests/test_recovery_jobs.py tests/test_dashboard_queries.py
```
