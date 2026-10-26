# Pre-mining filtering evaluation

`selfimprove eval-filter` is opt-in evaluation tooling. It measures how well the
pre-mining filter picks transcript events worth mining, and compares it with a
model-based challenger. It never changes production filtering, starts the miner,
gates proposals, installs workers, changes scanner configuration or touches the
runtime database. `eval-filter` dispatches before the ordinary CLI opens state.

The tooling works on your own private transcripts, so every dataset, label and
result stays in a private directory outside Git. A missing private benchmark
never falls back to synthetic examples.

## What the benchmark measures

Three arms see the same frozen events and the same queue admission rules:

| Arm | Language signals | Structural signals | Queue admission |
| --- | --- | --- | --- |
| `current` | Existing detectors on each appended block | Existing detectors on each appended block | Actual `scan_all`, caps, timestamp deduplication and configured mining order |
| `regex-context` | Existing detectors on the target's bounded context | Same incremental structural results | Same scanner and ordering |
| `jev` | Five probability questions: four language categories and insufficient context | Same incremental structural results | Cached decisions replayed through the same scanner and ordering |

The three structural categories are repeated error, friction loop and instruction
edit. A probability of at least 0.5 admits a semantic category. An
insufficient-context probability of at least 0.5 becomes a referral that consumes
queue budget. The threshold is fixed, not tuned on the test set. API errors,
missing predictions, interrupted requests and invalid model identities are
missing evidence, never negative classifications. Cross-session error promotions
lack an exact anchor; they are counted separately and cannot earn episode recall.

The matched arms see 20 preceding and 10 following events, chronological and
role-labelled. Text is redacted before truncation. The anchor gets first claim on
a 20,000-byte text budget, each event has a 6,000-byte limit, and every truncation
is explicit. The serialized request must fit 30,000 bytes. These are screening
inputs, not the full transcript that the agentic miner reads.

Native parsers freeze normalized events once. Detector signatures before and after
redaction must agree for every append block, or that session is excluded with a
cause. The replay schedules a scan after each 200-source-line block, using actual
byte offsets in disposable normalized JSONL files. This is a reconstructed
schedule, not an observation of historical nightly scans. Frozen parser, config
and source-module hashes are verified before scoring, and replay refuses changed
source implementations.

## Sampling and judgments

The default bundle selects up to 60 nonempty sessions ordered by file modification
time, balanced by provider, then up to 1,000 anchors. Modification time only
chooses the sample; timestamps and queue order still come from event data.
Native session identity deduplicates copies within a provider. Denied sessions
and events remain excluded. Eligible human and assistant text is sampled
independently of regex hits, alongside structural trigger anchors.

Sampling is uniform without replacement within each session and current-hit or
no-hit stratum, with roughly equal allocation and a census of small strata. The
bundle records each stratum population, its allocation, each inclusion
probability and any unrepresented strata. The population for weighted metrics is
eligible anchors in the selected sessions, not a complete history. A
provider-stratified 20% session split is reserved for development, so windows
from one session cannot cross into test. The pilot's first 50 cases are fixed
membership; request-order seeds 0, 1 and 2 change order, not membership.

Each judgment carries `id`, `label`, `signals`, `episode`, `acceptable_anchors`,
`rationale` and `provenance` (`reference` or `human`). Labels are:

- `worth_mining`: concrete reusable evidence at the target. Its own ID must be an
  acceptable anchor, and a shared earliest-evidence ID identifies the episode.
- `not_worth_mining`: ordinary work, quoted examples, negation, noise or no
  concrete lesson. Episode and acceptable anchors are empty.
- `insufficient_context`: kept as uncertainty, outside the binary precision and
  recall denominator, and never silently turned into a negative.

The reference judge sees only the role-labelled window and rubric, without regex
hits, strata, challenger predictions or mining results. Model labels are
provisional until a person reviews them. The review export includes uncertain and
missing labels, disagreements and a deterministic 20% audit stratified by
provider, split and hit or no-hit.

## Reference and challenger routes

`--reference-profile frontier-ultra`, the default, labels through a quota-selected
Codex frontier model at Ultra effort, using existing subscription login. The first
selection freezes the concrete model for all later calls. The adapter runs the
CLI in a disposable read-only directory, ignores user config, rules and project
docs, disables optional tools, and invalidates any result that shows tool use. CLI
telemetry must name the frozen model, Ultra effort and the exact rubric.
`--reference-profile opus` keeps the earlier Claude labels and filenames.

Three independent reference runs, with order seeds 0, 1 and 2, give unanimous
agreement, mean pairwise agreement, per-run exclusions and three-way splits.
Agreement measures consistency, not accuracy, and a majority does not replace
human review.

`--jev-gateway` selects the challenger route. Each route pins one model and
refuses a response that names another; none falls back to a different model.

- `typesafe` (default) pins `jev-1.13.0` on the direct TypeSafe API and needs
  `TYPESAFE_API_KEY`.
- `typesafe-1.14` pins paid `jev-1.14.0` on the same API and key.
- `zen-free` requests `jev-1.13-free` anonymously through OpenCode Zen. Zen
  reports a gateway alias, not an exact backend version, and records keep that
  limitation.

Each route has its own cache identities and output filenames in one shared ledger.

## Reproduction

Set `ROOT` to a new private directory under your state directory, never inside a
checkout. Keep one experiment's commands in one directory, so labeling, retries and
every order run share one ledger.

```sh
ROOT="$HOME/.self-improve/filter-eval/NEW-EXPERIMENT"
uv run selfimprove eval-filter prepare --output "$ROOT/dataset" --count 1000 --sessions 60
uv run selfimprove eval-filter baseline --dataset "$ROOT/dataset" --output "$ROOT/baseline.json"

# Inspect one raw reference result before scaling.
uv run selfimprove eval-filter reference --dataset "$ROOT/dataset" --output "$ROOT/calls" --limit 1 --order-seed 0
for order in 0 1 2; do
  uv run selfimprove eval-filter reference --dataset "$ROOT/dataset" --output "$ROOT/calls" --limit 50 --order-seed "$order"
done
uv run selfimprove eval-filter agreement --dataset "$ROOT/dataset" --limit 50 \
  --reference-results "$ROOT/calls/reference-frontier-ultra-order-0.json" \
  "$ROOT/calls/reference-frontier-ultra-order-1.json" "$ROOT/calls/reference-frontier-ultra-order-2.json" \
  --output "$ROOT/frontier-agreement.json"

uv run selfimprove eval-filter jev --jev-gateway zen-free --dataset "$ROOT/dataset" --output "$ROOT/calls" --limit 1 --order-seed 0
for order in 0 1 2; do
  uv run selfimprove eval-filter jev --jev-gateway zen-free --dataset "$ROOT/dataset" --output "$ROOT/calls" --limit 50 --order-seed "$order"
done

uv run selfimprove eval-filter review --dataset "$ROOT/dataset" \
  --labels "$ROOT/calls/reference-frontier-ultra-labels-order-0.json" \
  --predictions "$ROOT/calls/jev-zen-free-order-0.json" --output "$ROOT/review.json"
uv run selfimprove eval-filter compare --dataset "$ROOT/dataset" --labels "$ROOT/reviewed-labels.json" \
  --predictions "$ROOT/calls/jev-zen-free-order-0.json" --output "$ROOT/comparison-order-0.json"
```

The reviewed labels file is `{"dataset_sha": "<manifest sha>", "rows": [...]}`.
The review export is an inspection document with nested draft rows; merge each
reviewed `draft` row into the flat labels file and keep the other provisional
rows. Repeat comparisons for each order and inspect the spread. `--split dev`
selects development results. No report silently selects a winner. Raising
`--limit` reuses completed calls with matching input identity.

Reports contain weighted anchor precision and recall, queue-item precision,
referral rate, observed episode recall, false-positive and false-negative IDs,
per-source and per-signal breakdowns, admitted queues, cap and deduplication
counts, matched-budget operating points and paired session-bootstrap intervals.
`population_estimates_valid` stays false while labels, paired predictions or
sampled strata are incomplete. Observed-episode recall is not population episode
recall, because sampling anchors does not enumerate all episodes. Exclusions are
paired across every arm.

## Failure and budget behavior

A single-writer private ledger caps paid calls at no more than $50 per experiment.
It reserves and fsyncs before each request, charges actual usage only when it is
verified, and keeps the full reservation for failed or unknown calls. Creating a
new ledger starts a new budget; do not use one to evade an agreed ceiling.
Codex reports tokens rather than a price, so frontier calls use a fixed
accounting rate that the records name; it is not a price quote or invoice.

Jev calls have a 30-second timeout and at most two retries for transient failures.
Reference calls have a process-group deadline and at most two retries for
OAuth-refresh or quota failures. Router refusal stops the batch without a call.
Timeouts, malformed output and wrong-model responses stop reference execution
without automatic retry. After inspecting one schema-invalid label, use
`--continue-after-invalid-label EXACT_CASE_ID` to continue; that response stays
missing for scoring. Never use this flag to remove a valid disagreement or a
negative judgment. A crash with an outstanding reservation is not reissued
automatically. Missing credentials never start a provider fallback.

## Acceptance and limits

Run `uv run --extra dashboard pytest -q tests/test_filter_eval.py tests/test_filter_reference.py tests/test_filter_frontier.py tests/test_filter_zen.py tests/test_cli_smoke.py tests/test_scan_occurrences.py`,
then the full suite. The tests use invented inputs, including paraphrased
corrections, quotations, negation, instruction injection, duplicate timestamps,
append boundaries, truncation, symmetric failures, interrupted reservations,
served-model mismatch, tampered responses and redaction at the final rendered
prompt.

A benchmark is not a judged effectiveness result until label coverage and human
audit are complete. Unit tests and vendor benchmarks do not show that a model
beats the existing filter. Keep production on its existing filter until an
independent, reviewed comparison supports a change.
