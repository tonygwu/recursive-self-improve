"""Core eval harness: sandboxed trials for behavior-regression evals.

A *trial* materializes an EvalSpec's workspace into a fresh sandbox tmp
directory, optionally writes a sandbox-local CLAUDE.md carrying the rule
under test (the real config dirs are never touched), invokes an injected
``agent_runner`` with the scenario prompt, and grades the outcome with
either a code grader (shell command run in the sandbox, exit 0 = pass) or
an injected model grader. All LLM/agent invocations are injected callables,
so the harness is fully testable without real calls; production wiring
passes an llm-backed runner that execs the agent with cwd=sandbox.

Progress is always attempted/succeeded/failed with an error taxonomy
(never bare counts). Taxonomy keys:

- ``graded_fail``  — the grader ran cleanly and judged the trial a failure
- ``agent_error``  — the injected agent runner raised or broke its contract
- ``grader_error`` — the grader itself crashed (distinct from a graded fail)
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

GRADER_CODE = "code"
GRADER_MODEL = "model"

# Required top-level keys of an eval spec YAML document, in canonical order.
SPEC_KEYS = ("id", "title", "scenario_prompt", "workspace_files", "success_criteria", "grader")

# Spec ids become filenames and tmp-dir prefixes; keep them path-safe.
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Explicit caps for the model-grader final-state summary. Truncation is
# always marked inline ("[truncated N chars]"), never silent.
_SUMMARY_MAX_CHARS_PER_FILE = 4000
_SUMMARY_MAX_CHARS_AGENT_OUTPUT = 20000

_CLAUDE_MD_HEADER = "# Sandbox project instructions\n\n"


class SpecError(Exception):
    """Raised for malformed eval specs: missing, unknown, or ill-typed keys."""


@dataclass(frozen=True)
class EvalSpec:
    """One behavior-regression eval, loaded from YAML (see `load_spec`)."""

    id: str
    title: str
    scenario_prompt: str
    workspace_files: dict[str, str]  # sandbox-relative path -> file content
    success_criteria: str
    grader: dict  # {"type": "code", "check": sh} | {"type": "model", "rubric": str}


@dataclass(frozen=True)
class TrialStats:
    """attempted/succeeded/failed + error taxonomy for one `run_trials` arm."""

    attempted: int
    succeeded: int
    failed: int
    errors: dict[str, int] = field(default_factory=dict)  # taxonomy key -> count
    transcripts_dir: str = ""  # parent dir holding per-trial sandboxes + artifacts


def _validate_spec_dict(data: object, origin: str) -> None:
    """Validate a raw spec mapping. Raises SpecError on any deviation."""
    if not isinstance(data, dict):
        raise SpecError(f"{origin}: eval spec must be a mapping, got {type(data).__name__}")
    missing = [k for k in SPEC_KEYS if k not in data]
    unknown = [k for k in data if k not in SPEC_KEYS]
    if missing:
        raise SpecError(f"{origin}: missing required spec keys {missing}")
    if unknown:
        raise SpecError(f"{origin}: unknown spec keys {unknown} (allowed: {list(SPEC_KEYS)})")
    for key in ("id", "title", "scenario_prompt", "success_criteria"):
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise SpecError(f"{origin}: spec key {key!r} must be a non-empty string, got {value!r}")
    if not _ID_RE.match(data["id"]):
        raise SpecError(
            f"{origin}: spec id {data['id']!r} must match {_ID_RE.pattern} "
            "(it becomes a filename)"
        )
    ws = data["workspace_files"]
    if not isinstance(ws, dict):
        raise SpecError(f"{origin}: workspace_files must be a mapping, got {type(ws).__name__}")
    for rel, content in ws.items():
        if not isinstance(rel, str) or not isinstance(content, str):
            raise SpecError(
                f"{origin}: workspace_files entries must be str path -> str content, "
                f"got {rel!r} -> {type(content).__name__}"
            )
        if rel.startswith(("/", "~")) or ".." in Path(rel).parts:
            raise SpecError(
                f"{origin}: workspace_files path {rel!r} must be sandbox-relative "
                "(no absolute paths, no '..')"
            )
    grader = data["grader"]
    if not isinstance(grader, dict) or grader.get("type") not in (GRADER_CODE, GRADER_MODEL):
        raise SpecError(
            f"{origin}: grader must be a mapping with type 'code' or 'model', got {grader!r}"
        )
    required_key = "check" if grader["type"] == GRADER_CODE else "rubric"
    if not isinstance(grader.get(required_key), str) or not grader[required_key].strip():
        raise SpecError(
            f"{origin}: grader type {grader['type']!r} requires non-empty string "
            f"key {required_key!r}, got {grader.get(required_key)!r}"
        )
    extra = [k for k in grader if k not in ("type", required_key)]
    if extra:
        raise SpecError(f"{origin}: grader has unknown keys {extra}")


def spec_from_dict(data: dict, origin: str) -> EvalSpec:
    """Build a validated EvalSpec from a raw mapping. Raises SpecError."""
    _validate_spec_dict(data, origin)
    return EvalSpec(**{k: data[k] for k in SPEC_KEYS})


def spec_to_dict(spec: EvalSpec) -> dict:
    """Canonical YAML-serializable dict form of a spec (key order = SPEC_KEYS)."""
    return {k: getattr(spec, k) for k in SPEC_KEYS}


def load_spec(path: str | Path) -> EvalSpec:
    """Load and validate an eval spec YAML file. Missing/unknown keys raise."""
    p = Path(path)
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return spec_from_dict(data, origin=str(p))


# How much of the tail decides whether the agent ended by asking. Agents ask
# rhetorical questions mid-reasoning and then get on with the work, so only the
# END counts.
_ASK_TAIL_CHARS = 300


def classify_failure(agent_output: str) -> str:
    """Distinguish "did the wrong thing" from "stopped and asked a human".

    A rule that prescribes asking cannot be graded by a headless eval: obeying
    it means producing nothing, which the grader scores as a failure. Recording
    that as ``graded_fail`` would treat a request for human input as evidence
    that the rule caused incorrect behavior.

    Returns ``asked_operator`` when the tail of the output ends in a question,
    else ``graded_fail``.
    """
    tail = (agent_output or "").rstrip()[-_ASK_TAIL_CHARS:].rstrip()
    if tail.endswith("?"):
        return "asked_operator"
    return "graded_fail"


def _materialize_workspace(spec: EvalSpec, sandbox: Path) -> None:
    """Write workspace_files into the sandbox. Paths were validated at load;
    re-checked here defensively so nothing can escape the sandbox."""
    sandbox_resolved = sandbox.resolve()
    for rel, content in spec.workspace_files.items():
        dest = (sandbox / rel).resolve()
        if not dest.is_relative_to(sandbox_resolved):
            raise SpecError(f"workspace_files path {rel!r} escapes the sandbox")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _write_rule_claude_md(sandbox: Path, rule_text: str) -> None:
    """Write (or append to) the SANDBOX-local CLAUDE.md carrying the rule.

    Never touches any real config dir — the path is always inside the trial
    sandbox created by `run_trials`.
    """
    block = _CLAUDE_MD_HEADER + rule_text.rstrip("\n") + "\n"
    claude_md = sandbox / "CLAUDE.md"
    if claude_md.exists():  # spec workspace shipped its own CLAUDE.md: append
        existing = claude_md.read_text(encoding="utf-8")
        claude_md.write_text(existing.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    else:
        claude_md.write_text(block, encoding="utf-8")


def _truncate_marked(text: str, limit: int) -> str:
    """Cap text at limit chars with an explicit inline truncation marker."""
    if len(text) <= limit:
        return text
    cut = len(text) - limit
    return text[:limit] + f"\n[truncated {cut} chars]"


def _final_state_summary(sandbox: Path, agent_output: str) -> str:
    """Summarize the trial outcome for a model grader: the agent's final
    output plus every sandbox file (content capped with explicit markers)."""
    parts = [
        "AGENT FINAL OUTPUT:",
        _truncate_marked(agent_output, _SUMMARY_MAX_CHARS_AGENT_OUTPUT),
        "",
        "SANDBOX FILES:",
    ]
    for f in sorted(sandbox.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(sandbox)
        size = f.stat().st_size
        try:
            content = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            parts.append(f"--- {rel} ({size} bytes) ---\n[binary or non-utf8 file, omitted]")
            continue
        parts.append(f"--- {rel} ({size} bytes) ---\n"
                     + _truncate_marked(content, _SUMMARY_MAX_CHARS_PER_FILE))
    return "\n".join(parts)


def _grade(
    spec: EvalSpec,
    sandbox: Path,
    agent_output: str,
    model_grader: Callable[[str, str], bool] | None,
    grader_timeout_seconds: float,
) -> tuple[bool, dict]:
    """Run the spec's grader. Returns (passed, detail). Raising here is a
    grader crash, which `run_trials` counts as 'grader_error'."""
    if spec.grader["type"] == GRADER_CODE:
        proc = subprocess.run(
            spec.grader["check"],
            shell=True,
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=grader_timeout_seconds,
        )
        detail = {
            "returncode": proc.returncode,
            "stdout": _truncate_marked(proc.stdout, _SUMMARY_MAX_CHARS_PER_FILE),
            "stderr": _truncate_marked(proc.stderr, _SUMMARY_MAX_CHARS_PER_FILE),
        }
        return proc.returncode == 0, detail
    # model grader (presence of model_grader is validated before any trial runs)
    summary = _final_state_summary(sandbox, agent_output)
    assert model_grader is not None  # enforced in run_trials
    verdict = model_grader(spec.grader["rubric"], summary)
    if not isinstance(verdict, bool):
        raise TypeError(
            f"model_grader must return bool, got {type(verdict).__name__}: {verdict!r}"
        )
    return verdict, {"summary_chars": len(summary)}


from ..llm import BudgetExhausted  # noqa: E402  (module-level import kept near use)


def run_trials(
    spec: EvalSpec,
    rule_text: str | None,
    agent_runner: Callable[[str, Path], str],
    n: int,
    model_grader: Callable[[str, str], bool] | None = None,
    *,
    work_dir: Path | None = None,
    grader_timeout_seconds: float = 600.0, checkpoint=None, history=None,
) -> TrialStats:
    """Run n independent sandboxed trials of spec and grade each one.

    Each trial gets a fresh sandbox tmp dir with the spec's workspace_files
    materialized; when rule_text is given, a sandbox-local CLAUDE.md carries
    it (real config is never touched). ``agent_runner(scenario_prompt,
    sandbox_dir)`` returns the agent's final output text.

    Outcomes per trial: pass, 'graded_fail' (grader judged it a failure),
    'agent_error' (runner raised / returned non-str), or 'grader_error'
    (grader crashed — distinct from a graded failure). Per-trial artifacts
    (sandbox, agent_output.txt, result.json) are retained under the returned
    ``transcripts_dir``.

    ``work_dir`` overrides the artifact root (default: fresh mkdtemp);
    ``grader_timeout_seconds`` bounds the code grader's subprocess.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if spec.grader["type"] == GRADER_MODEL and model_grader is None:
        raise ValueError(
            f"spec {spec.id!r} uses a model grader but no model_grader callable was given"
        )
    if work_dir is None:
        work = Path(tempfile.mkdtemp(prefix=f"selfimprove-eval-{spec.id}-"))
    else:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)

    succeeded = 0
    errors: dict[str, int] = {}
    for i in range(n):
        def execute_trial():
            trial_dir = work / f"trial-{i:02d}"
            sandbox = trial_dir / "sandbox"
            sandbox.mkdir(parents=True, exist_ok=False)  # fresh dir per trial, or raise
            _materialize_workspace(spec, sandbox)
            if rule_text is not None:
                _write_rule_claude_md(sandbox, rule_text)

            outcome = "pass"
            error = ""
            detail: dict = {}
            agent_output: str | None = None
            try:
                out = agent_runner(spec.scenario_prompt, sandbox)
                if not isinstance(out, str):
                    raise TypeError(f"agent_runner returned {type(out).__name__}, expected str")
                agent_output = out
            except BudgetExhausted:
                # NOT agent_error. Running out of budget says nothing about the
                # rule under test, and burying it here is how it becomes a verdict:
                # ab.rerun_applied maps an arm dominated by agent_error to
                # `inconclusive`, which reads as "we re-tested this applied rule and
                # could not tell" about a rule that was never tested at all.
                # AGENTS.md: never let a harness failure be recorded as a verdict.
                raise
            except Exception as exc:
                outcome = "agent_error"
                error = f"{type(exc).__name__}: {exc}"
            else:
                (trial_dir / "agent_output.txt").write_text(agent_output, encoding="utf-8")
                try:
                    passed, detail = _grade(
                        spec, sandbox, agent_output, model_grader, grader_timeout_seconds
                    )
                except Exception as exc:
                    outcome = "grader_error"
                    error = f"{type(exc).__name__}: {exc}"
                else:
                    outcome = "pass" if passed else classify_failure(agent_output)

            with open(trial_dir / "result.json", "w", encoding="utf-8") as fh:
                json.dump(
                    {"trial": i, "outcome": outcome, "error": error, "grader": detail},
                    fh, ensure_ascii=False, indent=2,
                )

            return {"outcome":outcome,"error":error,"grader":detail}
        if history is not None:
            history.started(i, str(work / f'trial-{i:02d}'),spec=spec_to_dict(spec),
                            rule_text=rule_text,grader_timeout=grader_timeout_seconds)
        try:
            record=(execute_trial() if checkpoint is None else checkpoint(
                f'trial:{i}',{'spec':spec_to_dict(spec),'rule_text':rule_text,'work_dir':str(work)},execute_trial))
        except BaseException as exc:
            if history is not None:
                history.failed(i, exc)
            raise
        if history is not None:
            history.finished(i, record)
        if record['outcome']=='pass':succeeded+=1
        else:errors[record['outcome']]=errors.get(record['outcome'],0)+1

    stats = TrialStats(
        attempted=n,
        succeeded=succeeded,
        failed=n - succeeded,
        errors=errors,
        transcripts_dir=str(work),
    )
    with open(work / "stats.json", "w", encoding="utf-8") as fh:
        json.dump(
            {"spec_id": spec.id, "rule_present": rule_text is not None,
             "attempted": stats.attempted, "succeeded": stats.succeeded,
             "failed": stats.failed, "errors": stats.errors},
            fh, ensure_ascii=False, indent=2,
        )
    return stats
