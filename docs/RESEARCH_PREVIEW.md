# v0.1.2 research preview

recursive-self-improve supports a deliberately bounded local workflow:

1. Inspect recorded runs, rules and supporting evidence.
2. Review complete proposed edits against their exact revisions.
3. Approve explicitly and run the instruction worker.
4. Inspect the recorded file or delivery-branch result.
5. Reject by target or lesson, or request a conflict-aware rollback.

Automatic classes start off. This preview installs no unattended service.
The dashboard has no authentication and serves only on loopback. Existing code
for broader experiments remains available; its presence is not a completion claim.

## Added inspection features

- Every screen follows the v2 design. Review gains a full detail view with the
  proposed edit, its source evidence, and paired trials together. A command
  palette and grouped project navigation reach the same read-only URLs.
- Recurrence measurements count signals before and after an application, for one
  detector version and one observed scope. They require matching evidence, known
  session starts, and physical-line exposure. They are associations. They do not
  establish runtime receipt or causation, and insufficient coverage stays unavailable.
- Quality samples record applied contributions and append-only human judgments.
  Automatic target classes stay off until explicitly enabled, per class.
- Native session reports retain what a Claude Code or Codex hook reported at
  session start. Exact loaded bytes, revision, and continuity remain unknown.
  Creating a session is not a receipt that any instruction was loaded.
- Rule families group related rules for display only. Grouping grants no
  approval or delivery authority.
- Instruction inventories now also cover legacy commands, managed files,
  embedded policy fields, and plugin instructions. A retained field does not
  establish effective policy or that a rule was available at runtime.
- Retained instruction text keeps complete redacted file contents from the same
  surface the inventory observed.
- Run shows queue backlog and earlier related deliveries. Call caps are a budget,
  not a throughput measurement.
- Project detail separates sessions, contributing lessons, current proposals,
  and retained deliveries for one canonical repository identity.
- Rule availability compares delivered revisions with observed files. It does
  not prove that a particular agent session loaded or followed a rule.
- Monthly trends count timestamped signals per eligible physical transcript
  line. Detector versions, missing coverage, and observed zeros stay distinct.
  Fewer than 20 known sessions leaves the primary rate unavailable.
- Evaluation history retains the frozen rule, settings, specifications, prompts,
  scenarios, trials, and requested/served models. Historical results without
  these links remain explicitly unlinked. Infrastructure failures do not count
  as rule failures. Every configured gate scenario runs within the existing budget.

Existing databases require an [explicit upgrade](UPGRADING.md). Historical state
does not acquire missing evidence merely because the schema changes.

## Verification scope

Release verification uses invented histories, scratch instruction targets and
stubbed provider responses. It exercises stale approval, scoped rejection,
interrupted delivery and retry, rollback and a Review queue reaching empty.
These are software-contract checks. They do not measure real model effectiveness.

The repository ships disposable browser fixtures and verifiers for the v2 screens,
under `tests/browser/`. Each pairs a temporary fixture server with a Playwright
check of one contract, such as pagination, exact-source links, reload, keyboard
focus, disclosures, both themes and widths, forced colors, 200% zoom, and
regeneration cost previews. They use invented data, make GET requests only, and
verify that browsing leaves the temporary database and targets unchanged. They
call no model and read no personal history. See [contributing](../CONTRIBUTING.md)
for the commands.

The retrieval demo uses a pinned public embedding model over invented documents.
Private benchmarks require an explicitly selected frozen dataset. No private
benchmark or operational account is included in this repository.

The README PNGs are reviewed native Figma exports with invented text. The
dashboard now implements that design, but the exports are not screenshots of it.
They do not establish implemented behavior or measured results.

## Short backlog

- Per-session instruction receipt and continuity. Reports are retained; what a
  session actually loaded is still unmeasured.
- Validated benefit statistics. Recurrence measurements are observational only.
- Complete Figma parity. The v2 design is implemented across every screen, and
  the remaining differences are recorded rather than declared closed.
- Real-provider effectiveness. No measurement here uses a real mining corpus.

The preview does not depend on finishing those items. Registry publication and
production cutover are separate release actions.
