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
