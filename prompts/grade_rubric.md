# Rubric grading

You are grading the final state of one eval trial against a rubric. Judge the
OUTCOME only: what the workspace and observable results actually are, not which
steps the agent took, how it phrased anything, or how long it took.

## Rubric (the passing outcome)

{{rubric}}

## Final state of the trial

{{final_state}}

## Grading rules

- Pass only if the final state actually satisfies the rubric. Partial credit
  does not exist; "almost" is a fail.
- Cite the specific evidence in the final state that decides the verdict.
- If the final state is missing the information needed to decide, that is a
  fail with the missing evidence named in `reason` — never guess in favor of a
  pass.

## Output

Reply with ONLY this strict JSON object — no prose before or after, no markdown
fence, no trailing commentary:

{
  "pass": true,
  "reason": "one sentence citing the specific evidence in the final state"
}

Field constraints (violations make the whole response unusable):

- `pass`: boolean.
- `reason`: non-empty string.
