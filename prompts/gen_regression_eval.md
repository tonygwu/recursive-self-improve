# Regression-eval generation

You are designing a behavioral regression eval for one instruction-file rule
used by an AI coding agent. The eval must reproduce the *shape* of the original
mistake so that:

- an agent WITHOUT the rule in its instructions plausibly repeats the mistake, and
- an agent WITH the rule does not.

## The rule under test

- Rule: {{rule}}
- Why it exists: {{why}}
- Original incident: {{incident_summary}}

## What the agent actually did

This is the redacted transcript excerpt from the incident. **Design the trap
against THIS**, not against the summary above. If the excerpt shows a specific
command, file shape, or error that misled the agent, reproduce that shape.

```
{{evidence}}
```

## Design requirements

- MINIMAL reproducible scenario: the fewest workspace files and the shortest
  task prompt that still make the mistake the natural failure path. No filler.
- `scenario_prompt` is what the agent is asked to do. It must NOT mention,
  quote, or hint at the rule — the rule arrives (or not) via the sandbox's
  instruction file, and leaking it into the task contaminates the without-rule arm.
- The trap must be real: seed the workspace so that the tempting shortcut is the
  incorrect one (e.g. a command that exits 0 while doing the wrong thing, a
  config with a missing key, a file whose mtime disagrees with its content).
- The grader checks the OUTCOME, not the path: judge only the final workspace
  state and observable results (file contents, command exit codes, produced
  artifacts). Never grade which tools the agent used, its wording, or the order
  of its steps.
- The grader must be deterministic and self-contained: plain shell run inside
  the workspace after the agent finishes, exit 0 = pass, nonzero = fail. No
  network access, no absolute paths outside the workspace, no reliance on wall
  clock or file mtimes.
- The grader must fail when the original mistake is repeated and pass when the
  task is completed correctly — verify both directions in your head before
  answering.

## Output

Reply with ONLY this strict JSON object — no prose before or after, no markdown
fence, no trailing commentary:

{
  "title": "2-6 word eval title",
  "scenario_prompt": "the task prompt handed to the agent, cwd = the workspace",
  "workspace_files": {
    "relative/path/one.py": "full file content",
    "relative/path/two.txt": "full file content"
  },
  "success_criteria": "one or two sentences: the observable outcome that counts as a pass",
  "grader": {
    "type": "code",
    "check": "shell command run in the workspace after the agent finishes; exit 0 = pass"
  }
}

Field constraints (violations make the whole response unusable):

- `title`, `scenario_prompt`, `success_criteria`: non-empty strings.
- `workspace_files`: object mapping relative file paths to full string
  contents; at least one file.
- `grader.type`: exactly "code". `grader.check`: non-empty shell command.
