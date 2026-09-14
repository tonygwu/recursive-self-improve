# Cluster merge

You are consolidating instruction-file rules for an AI coding agent. The rules
below were mined independently from separate incidents and flagged as
*candidates* for being the same lesson because their embeddings are similar.
Similarity is not identity: your FIRST job is to decide whether they truly are
one lesson.

## The decision (required)

- `"merge"` — the rules teach the same lesson: same failure mechanism, same
  corrective behavior. Merge them into ONE canonical rule that covers every
  listed incident without losing any distinct failure mode. If one candidate
  is already the best formulation, keep it and tighten it rather than
  inventing a new phrasing.
- `"keep_separate"` — you MUST choose this when the rules are genuinely
  different lessons that merely share vocabulary (e.g. both mention "model"
  or "timestamps" but prescribe different behaviors against different failure
  modes). Merging distinct lessons destroys evidence: each rule's incidents
  stop being traceable to it, and the blended rule prevents neither original
  failure. When in doubt, keep them separate — a declined merge costs one
  extra rule line; a wrong merge silently loses a lesson.

## Candidate rules (with their evidence)

{{rules}}

## Rule style guide (mandatory for a merged rule)

- ONE line. Bold lead phrase as the memory anchor, e.g.
  `**Never trust exit 0 alone** — read one full raw response before parsing.`
- Falsifiable and concrete: for any specific action a reader can tell whether
  the rule was followed or violated. No aspirations ("be careful", "consider").
- Prefer negative framing: "Never X" / "X, not Y" over "try to Y".
- Must remain traceable to the listed incidents: the merged rule, in force at
  the time, would have prevented each of them. If you cannot write one rule
  that would have prevented every incident, that is the signal to answer
  `"keep_separate"`.
- NOT something capable models already do right unprompted. Apply the test
  "would removing this line cause a mistake?".
- Generalize only as far as the shared mechanism of the incidents; do not widen
  the rule beyond what the evidence supports.

## Output

Reply with ONLY one of these two strict JSON objects — no prose before or
after, no markdown fence, no trailing commentary. The `decision` field is
REQUIRED in both; a response without it is unusable.

If the rules are one lesson:

{
  "decision": "merge",
  "generalized_rule": "the one canonical rule line, following the style guide",
  "why": "one sentence: the concrete failure this rule prevents",
  "title": "2-6 word title naming the lesson",
  "category": "short kebab-case category, e.g. verification, time-handling, tooling",
  "scope_guess": "global"
}

If the rules are distinct lessons:

{
  "decision": "keep_separate",
  "reason": "one sentence: why these are different lessons despite similar wording"
}

Field constraints (violations make the whole response unusable):

- `decision`: exactly `"merge"` or `"keep_separate"`.
- With `"merge"`: `generalized_rule`, `why`, `title`, `category` are non-empty
  strings; `scope_guess` is exactly one of "global", "project", "skill",
  "hook", "codex_global".
- With `"keep_separate"`: `reason` is a non-empty string.
