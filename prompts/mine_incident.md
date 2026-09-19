# Incident audit

You are auditing one incident from an AI coding session (Claude Code or Codex).
A deterministic filter_incidents flagged this moment as a possible recurring mistake,
correction, or friction point. Decide whether it encodes a REAL, durable
learning that should become an instruction-file rule, and if so, write that
rule.

## Incident

- Signal type: {{signal_type}}
- Project: {{project}}

Redacted conversation window (strict JSON, chronological order):

{{window}}

## Instructions currently in force for this project

{{in_force_instructions}}

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

## Duplicate check

Read the in-force instructions above carefully. If the lesson of this incident
is ALREADY covered by an existing line there, set `duplicate_of_existing_rule`
to that existing line, quoted verbatim (copy the exact text). Otherwise set it
to null. A duplicate can still be `is_real_learning: true` — the duplicate flag
records that the lesson is already encoded.

## Output

Reply with ONLY this strict JSON object — no prose before or after, no markdown
fence, no trailing commentary:

{
  "is_real_learning": true,
  "incident_summary": "one- or two-sentence factual summary of what went wrong in this incident",
  "generalized_rule": "the one-line rule, following the style guide above",
  "why": "one sentence: the concrete failure this rule prevents",
  "scope_guess": "global",
  "category": "short kebab-case category, e.g. verification, time-handling, tooling",
  "duplicate_of_existing_rule": null,
  "confidence": 0.8
}

Field constraints (violations make the whole response unusable):

- `is_real_learning`: boolean. false when the incident is a one-off, transient
  user-preference noise, something models already do right unprompted, or not
  actually a mistake. When false, the string fields may be empty strings but
  every key must still be present.
- `incident_summary`, `generalized_rule`, `why`, `category`: strings.
- `scope_guess`: exactly one of "global", "project", "skill", "hook",
  "codex_global".
- `duplicate_of_existing_rule`: a string quoting the matching existing
  instruction line verbatim, or null when no existing line covers the lesson.
- `confidence`: number between 0 and 1.
