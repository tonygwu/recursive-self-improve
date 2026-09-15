# v0.1.1 research preview

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

- Project detail separates sessions, contributing lessons, current proposals,
  and retained deliveries for one canonical repository identity.
- Instruction inventories record files, ownership, and potential loading paths
  for each working copy, including configured global instructions and skills.
- Rule availability compares delivered revisions with observed files. It does
  not prove that a particular agent session loaded or followed a rule.
- Monthly trends count timestamped signals per eligible physical transcript
  line. Detector versions, missing coverage, and observed zeros stay distinct.
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

The committed Evals browser fixture covers pagination, exact-source links, reload,
keyboard focus, disclosures, both themes, and regeneration cost previews. It uses
invented data and verifies that browsing leaves its temporary database unchanged.
See [contributing](../CONTRIBUTING.md) for the commands.

The retrieval demo uses a pinned public embedding model over invented documents.
Private benchmarks require an explicitly selected frozen dataset. No private
benchmark or operational account is included in this repository.

The README PNGs are reviewed native Figma exports with invented text. They show
design intent. They do not establish implemented behavior or measured results.

## Short backlog

- Complete J3 diagnosis and per-session instruction receipt and continuity.
- Validated recurrence and benefit statistics.
- Human quality assessments, automatic-policy controls, and inferred display families.
- Command palette and full seven-screen Figma parity.
- Large-history performance and safe rebuild of retained execution history.

The preview does not depend on finishing those items. Registry publication and
production cutover are separate release actions.
