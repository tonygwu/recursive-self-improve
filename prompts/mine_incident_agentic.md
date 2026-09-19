# Incident root-cause audit (agentic)

You are auditing one incident from an AI coding session (Claude Code or Codex).
The deterministic filter-incidents stage flagged one moment as a possible recurring mistake,
correction, or friction point. You are in a sandbox directory containing:

- `transcript.md` — the FULL session, redacted, one section per event. Each
  section is headed `## [<line_no>] <role>/<kind> <timestamp>` (plus
  `tool=<name>` and an `ERROR` marker where applicable). The bracketed number
  is the line number in the original transcript file — grep for `[<n>]` to
  jump to a section. Secrets appear as `[REDACTED:<type>]` placeholders; long
  bodies end with an explicit `[truncated N chars]` marker.
- `environment.md` — the project, session flags (source, headless, subagent),
  session date range, the instruction files that were in force for this
  project, and the incident pointer.
- `learnings.jsonl` — EVERY learning already in the miner's database (one
  JSON object per line: id, status, rule_text, why, category, scope,
  evidence_count, project_count). Statuses: `applied` (already written into an
  instruction file), `proposed`/`candidate` (awaiting review), `rejected`
  (the user said no — permanently).

## The incident

- Signal type: {{signal_type}}
- Project: {{project}}
- The signal fired at the section headed `## [{{start_line}}]` in
  `transcript.md`, on this (redacted) trigger text:

{{matched_text}}

## Turn budget (hard constraint)

Your session is capped at {{max_turns}} tool-use turns, and hitting that cap
mid-exploration discards EVERYTHING you found — a truncated session produces no
output at all. Explore efficiently: environment.md first, then the incident
section, then targeted Grep jumps rather than sequential reading. Plan to stop
exploring by turn {{explore_budget}} so the remaining turns are free to write
the final JSON. The moment your remaining questions stop changing the verdict,
STOP exploring and emit it. A slightly less-explored but delivered analysis
always beats a perfect one that never arrives.

## How to investigate

Start by reading `environment.md`, then the section `## [{{start_line}}]` in
`transcript.md` with enough surrounding context to understand the moment. Then
use Read/Grep/Glob to explore BACKWARD through the session to the root cause:
the visible failure is often 20+ turns downstream of an original
misunderstanding, missing context, or environment quirk. A correction from the
user usually means something earlier set the assistant up to fail — find that
earlier thing. Trace error messages, repeated tool failures, and the first
appearance of the mistaken assumption. Do not stop at the proximate symptom.

Then judge whether there is a real, generalizable lesson. Most incidents are
one-offs — transient noise, a user preference of the moment, a mistake any
capable model would not repeat — so `is_real_learning: false` is the common,
CORRECT answer. Only a durable lesson that would have prevented or materially
shortened THIS incident, and that a capable model would not already follow
unprompted, deserves a rule.

## Rule style guide (mandatory)

- ONE line. Bold lead phrase as the memory anchor, e.g.
  `**Never trust exit 0 alone** — read one full raw response before parsing.`
- Falsifiable and concrete: for any specific action a reader can tell whether
  the rule was followed or violated. No aspirations ("be careful", "consider").
- Prefer negative framing: "Never X" / "X, not Y" over "try to Y".
- Traceable to THIS incident: the rule, in force at the time, would have
  prevented or materially shortened it. Do not import lessons from elsewhere.
- NOT something capable models already do right unprompted. Apply the test
  "would removing this line cause a mistake?" — if no, `is_real_learning` is false.
- Generalize past incidental specifics (file names, one-off typos) but never
  past the incident's actual mechanism.

## Summary style guide

Write `incident_summary` for an operator scanning the dashboard, using one or
two short sentences. Use plain language and active verbs.

- Start with the agent's action and its observed consequence. Name the concrete
  mistake, not a broad category such as "verification failure".
- Add a cause only when the retained evidence supports it. If the cause is unknown,
  say what the available window does not show. Do not infer intent or invent a cause.
- Distinguish a reported result from a verified result. A successful exit code,
  a proposed edit or a model's assertion alone does not prove success.
- Include a tool name, error or identifier only when it explains the mechanism.
  Omit incidental paths, session IDs and chronology that do not change the lesson.
  Preserve redaction markers. Do not reconstruct omitted private details.
- Keep the factual incident summary separate from `generalized_rule`. Use `why`
  for the concrete failure the rule can prevent, not a claim of measured benefit.
- For a negative verdict, describe the observed event and why it supports no
  durable lesson. Do not force a failure narrative onto a successful recovery.
- Apply this guide to the summary you produce now. Do not amend an existing rule only to change its wording.
  Follow the duplicate-check instructions below.

Invented examples; use the incident's own evidence rather than copying them:

- If the output explicitly reports three failed tests, but the agent reports a
  pass after checking only the exit code: "The agent reported that tests passed
  because the command exited successfully, although its output listed three
  failing tests. It did not inspect that output."
- If the window records a lost setting but omits the reason for replacing its
  file: "The agent replaced the configuration file and removed an existing
  setting. The retained window does not show why it chose that replacement."
- If a timeout resolves on the first retry without a correction or repeated
  mistake: "The tool completed after one timeout and retry. The retained events
  show no correction or repeated mistake that requires a new rule."

## Duplicate check — instruction files

If the lesson of this incident is ALREADY covered by a line in an in-force
instruction file, set `duplicate_of_existing_rule` to that line, quoted
verbatim (copy the exact text). Otherwise set it to null. A duplicate can still
be `is_real_learning: true` — the flag records that the lesson is already
encoded, not that the incident was unreal.

**`environment.md` is NOT a complete copy of those files.** Each one is cut at
4,000 characters with a `[truncated N chars]` marker. Instructions beyond
that limit remain in the source file. Reading the truncated copy alone can
miss an existing rule and report a duplicate as new.

So: skim `environment.md` for the shape of what is in force, but confirm with
the search below, which indexes the WHOLE of every in-force file and returns
matches as `kind: "rule_unit"`. Treating the truncated copy as authoritative is
a known source of duplicates reaching the review queue.

## Dedup against the learnings database (mandatory when is_real_learning is true)

Once you have drafted your rule, check whether the miner already knows this
lesson. Two tools:

- Semantic: run the read-only embedding search (this exact command is
  allowlisted; the database cannot be modified through it):

      {{search_cli}} search-learnings "<your drafted rule text>" --top 8

  It prints `{"results": [...], "meta": {...}}`. Each result carries a
  `cosine` score and a `kind`, and the two kinds mean different things:

  - `"kind": "learning"` — a lesson already mined into the database. Its `id`
    is the ONLY thing that may go in `dedup_target_id`.
  - `"kind": "rule_unit"` — a line already living in an instruction file,
    with the file in `source_file`. This is the lesson being ALREADY WRITTEN.
    Put its text in `duplicate_of_existing_rule` and leave `dedup_target_id`
    as the empty string. A `rule:`-prefixed id is not a learning id and will
    be rejected.

  `meta` reports what the `--top` cap cut: `ranked`, `returned`, `cut`, and
  `returned_kinds`. If `returned_kinds.in_force_rules` is 0 while
  `meta.corpus.in_force_rules` is large, near-duplicate learnings are
  occupying every slot — re-query with more distinctive wording before
  concluding that nothing covers your rule.
- Keyword: Grep `learnings.jsonl` for distinctive terms of your rule.

Then set `dedup_decision`:

- `"new"` — nothing in the database covers this lesson. `dedup_target_id`
  must be the empty string.
- `"duplicate"` — an existing learning (any status) already expresses this
  lesson; your incident is another observation of it. Set `dedup_target_id`
  to that learning's `id`. Evidence will be credited to it; no new row is
  created. If the matching learning is `rejected`, still report it — the
  system records the observation and drops it (the user already declined).
- `"amend"` — an existing learning covers this lesson but YOUR incident shows
  it needs correcting or broadening (wrong scope, missing trigger, a second
  mechanism). Set `dedup_target_id`, and provide the full replacement text in
  `amended_rule_text` / `amended_why` (same style guide; the amended rule
  must still cover the original incident's mechanism, not just yours).

Judge semantically — phrasing may differ wildly while the lesson is the same.
But do NOT stretch: two rules that merely share vocabulary (both mention
"tests", both say "never") are different lessons; marking a genuinely
different lesson as duplicate silently destroys it. When unsure between
`duplicate` and `new`, choose `new` — the deterministic belt-check downstream
catches near-identical text, while a wrong `duplicate` is unrecoverable.
`dedup_target_id` must be an id copied EXACTLY from search output or
learnings.jsonl — never constructed.

## Output

Your FINAL message must be ONLY this strict JSON object — no prose before or
after, no markdown fence, no trailing commentary:

{
  "is_real_learning": true,
  "incident_summary": "one- or two-sentence factual summary of what went wrong in this incident, including the root cause you traced",
  "generalized_rule": "the one-line rule, following the style guide above",
  "why": "one sentence: the concrete failure this rule prevents",
  "scope_guess": "global",
  "category": "short kebab-case category, e.g. verification, time-handling, tooling",
  "duplicate_of_existing_rule": null,
  "confidence": 0.8,
  "dedup_decision": "new",
  "dedup_target_id": "",
  "amended_rule_text": "",
  "amended_why": "",
  "violated_existing_rule": "",
  "path_globs": []
}

Field constraints (violations make the whole response unusable):

- `is_real_learning`: boolean. false when the incident is a one-off, transient
  user-preference noise, something models already do right unprompted, or not
  actually a mistake. When false, the string fields may be empty strings but
  every key must still be present.
- `incident_summary`, `generalized_rule`, `why`, `category`: strings.
- `scope_guess`: exactly one of "global", "project", "skill", "hook",
  "rule_path", "codex_global".
- `duplicate_of_existing_rule`: a string quoting the matching existing
  instruction line verbatim, or null when no existing line covers the lesson.
- `confidence`: number between 0 and 1.
- `dedup_decision`: exactly one of "new", "duplicate", "amend". When
  `is_real_learning` is false, use "new".
- `dedup_target_id`: the empty string for "new"; a learning id copied exactly
  from search output / learnings.jsonl for "duplicate" and "amend". A
  nonexistent id makes the whole response unusable.
- `amended_rule_text`, `amended_why`: full replacement text for "amend";
  empty strings otherwise.
- `violated_existing_rule`: quote an in-force rule that the incident violated;
  use the empty string when none was violated or `is_real_learning` is false.
  This differs from `duplicate_of_existing_rule`, which identifies a lesson
  already covered by an instruction.
- `path_globs`: a list of non-empty relative glob strings when a real learning
  has `scope_guess: "rule_path"`, such as `["src/**/*.py"]`. Use that scope
  only when the rule applies to those paths. The list must be non-empty for
  such a learning; use `[]` for every other scope and for a negative verdict.

No other keys are allowed; every key above must be present exactly once.

## The "nothing to learn here" case — STILL JSON

An incident can hold no durable lesson. Report that verdict as JSON, with
the reasoning in `incident_summary`. A prose-only response cannot be parsed;
the investigation then produces no usable result.

So when the verdict is negative, put the reasoning in `incident_summary` and
emit exactly this shape — the same keys, nothing else, no prose around it.
This example is invented; replace its summary with your own evidence:

{
  "is_real_learning": false,
  "incident_summary": "The signal matched an invented status message. The surrounding events show no correction or repeated failure, so this example provides no durable lesson.",
  "generalized_rule": "",
  "why": "",
  "scope_guess": "global",
  "category": "",
  "duplicate_of_existing_rule": null,
  "confidence": 0.0,
  "dedup_decision": "new",
  "dedup_target_id": "",
  "amended_rule_text": "",
  "amended_why": "",
  "violated_existing_rule": "",
  "path_globs": []
}

Whatever your verdict: your final message starts with `{` and ends with `}`.

**Emit every key above, including the last two, even when their value is empty.**
`violated_existing_rule` is `""` unless an in-force rule was written AND ignored.
`path_globs` is `[]` unless `scope_guess` is `rule_path`. Do not drop a key just
because its value is empty — a trailing `"": ` or `[]` is expected and correct.
