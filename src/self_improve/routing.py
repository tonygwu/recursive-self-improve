"""Routing: decide which instruction file a learning's edit targets.

Two responsibilities:

- ``detect_topology()`` inspects a project directory's AGENTS.md / CLAUDE.md
  arrangement using **lstat semantics** (a symlink is detected before it is
  followed, and content is never written *through* the link — that would
  double-write the same file). Three topologies exist in the wild:
  independent files, CLAUDE.md-symlink→AGENTS.md, and a thin stub pointer
  (a CLAUDE.md that is just ``@AGENTS.md``).

- ``route()`` implements the design plan's decision table (first match wins):

  0. non-empty ``violated_existing_rule`` → ``convert_to_hook`` against
     settings.json, regardless of scope_guess AND project count: the rule was
     already written and was ignored anyway, so adding more advisory prose
     anywhere is pointless — deterministic enforcement is the fix. (Review
     queue; ``convert_to_hook`` is in ``cfg.review_queue_actions``.)
  1. ``project_count >= cfg.global_promotion_min_projects`` OR
     ``scope_guess == "global"``  → global ``~/.claude/CLAUDE.md``
  2. ``scope_guess == "hook"``    → ``convert_to_hook`` against settings.json
     (review queue; no diff is applied in the MVP)
  3. ``scope_guess == "skill"``   → new skill dir under ``cfg.skills_dir``
  4. ``scope_guess == "rule_path"`` → ``new_rule_file`` at
     ``<project>/.claude/rules/<slug>.md`` (path-glob-scoped rule file)
  5. ``scope_guess == "codex_global"`` OR (source codex AND cross-project,
     i.e. ``project_count >= 2``) → ``~/.codex/AGENTS.md``
  6. otherwise → the learning's primary project file via ``detect_topology()``.

  The order above is the contract: an explicit ``scope_guess`` of ``hook`` /
  ``skill`` / ``rule_path`` / ``codex_global`` does NOT outrank the
  project-count promotion in rule 1. That is a deliberate, documented
  consequence of the table order. Only rule 0 (enforcement gap) outranks
  everything.

Fail-loud policy: a learning with no resolvable primary project, an unparseable
``projects_json``, or a project directory that no longer exists raises
(:class:`RoutingError` / :class:`TopologyError`) — routing never guesses a
target path.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from self_improve.config import Config

# Exact content written when a CLAUDE.md stub must be created alongside a new
# AGENTS.md (the "neither file exists" topology). apply.py uses this constant.
STUB_CONTENT = "@AGENTS.md\n"

# A CLAUDE.md is a "stub" iff it is a regular file, < STUB_MAX_BYTES, and every
# non-empty line is just a pointer to AGENTS.md (e.g. "@AGENTS.md").
STUB_MAX_BYTES = 200
_POINTER_LINE_RE = re.compile(
    r"^(?:@|(?:see|read|import)\s+@?)?\.?/?AGENTS\.md\s*\.?$", re.IGNORECASE
)


class RoutingError(Exception):
    """Raised when a learning cannot be routed (no project, bad slug, ...)."""


class TopologyError(Exception):
    """Raised when a project directory cannot be inspected."""


@dataclass(frozen=True)
class Topology:
    """AGENTS.md / CLAUDE.md arrangement of one project directory.

    ``write_target_general`` is always the project's AGENTS.md (the general
    file in every topology). ``write_target_claude_specific`` is CLAUDE.md
    only when CLAUDE.md exists as an independent regular file; in the
    symlink / stub / missing topologies Claude-specific content ALSO goes to
    AGENTS.md — the field then holds the AGENTS.md path (never ``None`` in
    practice; the Optional type is part of the shared contract).

    ``needs_claude_md_stub`` is True when no CLAUDE.md entry exists at all:
    apply.py should create one containing exactly :data:`STUB_CONTENT`.
    """

    project_dir: Path
    agents_md_exists: bool
    claude_md_exists: bool
    claude_md_is_symlink: bool
    claude_md_symlink_to_agents: bool
    claude_md_is_stub: bool
    write_target_general: Path
    write_target_claude_specific: Path | None
    needs_claude_md_stub: bool


@dataclass(frozen=True)
class RouteDecision:
    """Where one learning's proposal is aimed.

    ``target_kind`` uses the proposals-table vocabulary
    (global_claude_md | project_agents_md | project_claude_md | codex_global |
    skill | hook | rule_file); ``action`` uses
    add | convert_to_hook | new_skill | new_rule_file here (delete flows are
    constructed by the pruning caller, edit flows by the pipeline's
    amend-of-applied branch — not by ``route()``).
    """

    target_path: Path
    target_kind: str
    action: str
    reason: str = ""
    topology: Topology | None = None


def _is_pointer_stub(text: str) -> bool:
    """True iff every non-empty line of ``text`` is an AGENTS.md pointer."""
    lines = [ln.strip() for ln in text.splitlines()]
    non_empty = [ln for ln in lines if ln]
    if not non_empty:
        return False
    return all(_POINTER_LINE_RE.match(ln) for ln in non_empty)


def detect_topology(project_dir: Path) -> Topology:
    """Inspect ``project_dir``'s instruction-file topology (lstat first).

    Raises :class:`TopologyError` if the directory does not exist or a
    CLAUDE.md candidate cannot be decoded as UTF-8 (never guesses).
    Never creates or modifies any file.
    """
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        raise TopologyError(f"project directory does not exist: {project_dir}")
    agents = project_dir / "AGENTS.md"
    claude = project_dir / "CLAUDE.md"

    agents_exists = os.path.lexists(agents)
    claude_exists = os.path.lexists(claude)

    # lstat FIRST: a symlink is classified as a symlink, never read as content
    # and never written through (that would double-write its target).
    claude_is_symlink = claude.is_symlink()
    symlink_to_agents = False
    if claude_is_symlink:
        symlink_to_agents = os.path.realpath(claude) == os.path.realpath(agents)

    claude_is_stub = False
    if claude_exists and not claude_is_symlink and claude.is_file():
        if os.lstat(claude).st_size < STUB_MAX_BYTES:
            try:
                text = claude.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise TopologyError(f"CLAUDE.md is not UTF-8 text: {claude}: {exc}") from exc
            claude_is_stub = _is_pointer_stub(text)

    # General content ALWAYS goes to AGENTS.md. Claude-specific content goes
    # to CLAUDE.md only when it is an independent regular file; a symlink
    # (wherever it points) or a stub means AGENTS.md is the single real file.
    independent_claude = claude_exists and not claude_is_symlink and not claude_is_stub
    claude_specific = claude if independent_claude else agents

    return Topology(
        project_dir=project_dir,
        agents_md_exists=agents_exists,
        claude_md_exists=claude_exists,
        claude_md_is_symlink=claude_is_symlink,
        claude_md_symlink_to_agents=symlink_to_agents,
        claude_md_is_stub=claude_is_stub,
        write_target_general=agents,
        write_target_claude_specific=claude_specific,
        needs_claude_md_stub=not claude_exists,
    )


# ----------------------------------------------------------------------------
# Learning-dict accessors (shared with propose.py).
# ----------------------------------------------------------------------------


def scope_guess(learning: dict) -> str:
    """The learning's scope guess (miner key ``scope_guess``; DB column ``scope``)."""
    return str(learning.get("scope_guess") or learning.get("scope") or "")


_REPO_N_RE = re.compile(r"^repo-(\d+)$")

# Checkouts automation RUNS FROM and must never WRITE INTO. `repo-prod` is the
# production checkout: the launchd nightly executes it, and its working tree is
# required to stay clean, so a proposal committed there would both break that
# invariant and put an unreviewed edit into the checkout that runs tomorrow
# night. This is a different question from `repo-0`, which is the checkout
# automation writes TO. A path landing here is redirected, never written.
#
# A scanned session can originate in the production checkout. Its raw cwd can
# reach primary_project_path before canonical working-copy selection, so routing
# must handle production paths explicitly.
_NEVER_WRITE_NAMES = ("repo-prod",)


def _clone_index(path: str) -> int | None:
    """``N`` if this path is a ``repo-N`` working copy, else ``None``."""
    m = _REPO_N_RE.match(os.path.basename(path.rstrip("/")))
    return int(m.group(1)) if m else None


def is_never_write(path: str) -> bool:
    """True when ``path`` is a checkout automation must never write into."""
    return os.path.basename(str(path).rstrip("/")) in _NEVER_WRITE_NAMES


def redirect_never_write(
    path: str, *, isdir=os.path.isdir, resolver=None, project_key: str = ""
) -> str:
    """Send a never-write checkout to its ``repo-0`` sibling, or to ``""``.

    The sibling must share the parent directory and contain a ``.git`` directory.
    If the caller supplies both a resolver and a project key, its identity must
    also match. Without those arguments, this helper checks only the directory
    topology. A failed check returns ``""`` so the caller can refuse the target.
    """
    if not path or not is_never_write(path):
        return path
    sibling = os.path.join(os.path.dirname(str(path).rstrip("/")), "repo-0")
    if not (isdir(sibling) and isdir(os.path.join(sibling, ".git"))):
        return ""
    # Check a candidate checkout against the caller's known repository identity.
    # A shared parent directory and clone naming pattern do not prove identity.
    if resolver is not None and project_key:
        try:
            if resolver(sibling) != project_key:
                return ""
        except Exception:  # noqa: BLE001 - a broken checkout downgrades, never raises
            return ""
    return sibling


def canonical_working_copy(
    paths,
    *,
    also_consider=(),
    project_key: str = "",
    resolver=None,
    isdir=os.path.isdir,
) -> str:
    """The working copy that automated commits and writes belong in.

    Operator convention: **``repo-0`` is the automation target.** The other
    clones of a repo are where work is actually happening, so writing into one
    of them collides with whatever is in flight there. Order of preference:

    1. a ``repo-0`` among the candidates;
    2. a ``repo-0`` sibling *on disk* that no session ever ran in. Evidence can
       mention only interactive working copies. This sibling is adopted only when
       ``resolver(sibling)`` returns ``project_key``, so a same-named directory
       belonging to a different repo can never be written into;
    3. the lowest-numbered ``repo-N`` (some clone sets start at ``repo-1``);
    4. failing all that, the first candidate — the historical behaviour.

    Note what is deliberately *not* a tie-break: how heavily a copy is used.
    The operator can reserve a less-used clone for automation and keep the
    busiest checkout for interactive work.

    ``also_consider`` holds other working copies of the *same* repo that the
    caller already knows about (in practice: every ``sessions.project_path``
    sharing this ``project_key``). It exists for the checkout whose path gives
    no hint that it belongs to a clone set. Repository identity, rather than
    directory naming, establishes that relationship. These are ranked alongside
    the evidence but can only ever win by being
    a lower-numbered clone, and are dropped when they no longer exist on disk;
    evidence paths are trusted as given, since a missing one is reported loudly
    by the caller's own guard rather than silently swapped out here.

    Never raises: a broken checkout downgrades to the next preference rather
    than taking routing down with it.
    """
    candidates = [p for p in paths if p]
    if not candidates:
        return ""
    # A never-write checkout (repo-prod) can never be the answer. Redirect it
    # to its repo-0 sibling and let the redirected path compete normally; a
    # redirect that fails yields "" and drops out. If EVERY candidate was a
    # never-write checkout with no sibling, the caller gets "" and refuses.
    if any(is_never_write(p) for p in candidates):
        candidates = [
            r
            for r in (
                redirect_never_write(
                    p, isdir=isdir, resolver=resolver, project_key=project_key
                )
                for p in candidates
            )
            if r
        ]
        # dedupe, preserving order: several never-write paths can redirect to
        # the same repo-0.
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            return ""
    known = [p for p in also_consider if p and p.rstrip("/") not in
             {c.rstrip("/") for c in candidates} and isdir(p)]
    candidates = candidates + known

    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (
            (0, idx) if (idx := _clone_index(pair[1])) is not None else (1, 0),
            pair[0],
        ),
    )
    best = ranked[0][1]
    if _clone_index(best) == 0:
        return best

    # No repo-0 in the evidence. Look for one next to a clone we do know about.
    if resolver is not None and project_key:
        seen = {p.rstrip("/") for p in candidates}
        for _, path in ranked:
            if _clone_index(path) is None:
                continue
            sibling = os.path.join(os.path.dirname(path.rstrip("/")), "repo-0")
            if sibling in seen or not isdir(sibling):
                continue
            try:
                if resolver(sibling) == project_key:
                    return sibling
            except Exception:  # noqa: BLE001 - a bad checkout is not fatal here
                continue
    return best


def primary_project(learning: dict) -> str:
    """First project path of the learning; raises RoutingError if none.

    Accepts ``primary_project`` (str), ``projects`` (list of str), or
    ``projects_json`` (strict-JSON list of str, as stored in the learnings
    table). Malformed JSON raises — never silently yields no project.
    """
    p = learning.get("primary_project")
    if isinstance(p, str) and p:
        return p
    # Canonical-identity era: projects_json holds repo KEYS, which are not
    # paths. The real working copy to write into lives here.
    p = learning.get("primary_project_path")
    if isinstance(p, str) and p:
        if is_never_write(p):
            # miner.py stores the incident's RAW cwd here, and the nightly runs
            # from repo-prod, so this path is reachable in normal operation.
            redirected = redirect_never_write(p)
            if not redirected:
                raise RoutingError(
                    f"learning {learning.get('id', '<no id>')!r} is routed to "
                    f"{p!r}, which is a production checkout that must never be "
                    "written to, and no repo-0 sibling was found next to it"
                )
            return redirected
        return p
    projects = learning.get("projects")
    if projects is None and learning.get("projects_json"):
        try:
            projects = json.loads(learning["projects_json"])
        except json.JSONDecodeError as exc:
            raise RoutingError(
                f"learning {learning.get('id', '<no id>')!r}: "
                f"projects_json is not valid JSON: {exc}"
            ) from exc
    if isinstance(projects, list) and projects:
        # Same convention as the backfill: prefer repo-0, then the lowest
        # clone. No sibling probe here -- this path has no identity to check a
        # sibling against, and adopting an unverified directory is exactly the
        # mistake the probe's identity gate exists to prevent.
        chosen = canonical_working_copy([p for p in projects if isinstance(p, str)])
        if chosen:
            return chosen
    raise RoutingError(
        f"learning {learning.get('id', '<no id>')!r} has no primary project "
        "(need primary_project, projects, or projects_json)"
    )


def skill_slug(learning: dict) -> str:
    """Deterministic skill dir name from the learning's title (else rule_text).

    First 8 lowercase alphanumeric words joined by '-'. Raises RoutingError
    when neither field yields a usable slug.
    """
    base = str(learning.get("title") or "").strip() or str(learning.get("rule_text") or "").strip()
    words = re.findall(r"[a-z0-9]+", base.lower())
    if not words:
        raise RoutingError(
            f"learning {learning.get('id', '<no id>')!r}: cannot derive a skill "
            f"slug from title/rule_text {base!r}"
        )
    return "-".join(words[:8])


def skill_md_path(cfg: Config, learning: dict) -> Path:
    """SKILL.md path for a new-skill proposal: <skills_dir>/<slug>/SKILL.md."""
    return Path(cfg.skills_dir) / skill_slug(learning) / "SKILL.md"


def rule_file_path(learning: dict) -> Path:
    """Rule-file path for a rule_path learning: <project>/.claude/rules/<slug>.md.

    The slug reuses :func:`skill_slug` (same derivation, same failure mode).
    Raises RoutingError when the learning has no primary project or the
    project directory no longer exists — routing never aims at a directory
    that is not there.
    """
    project = primary_project(learning)
    if not Path(project).is_dir():
        raise RoutingError(
            f"learning {learning.get('id', '<no id>')!r}: rule_path project "
            f"directory does not exist: {project}"
        )
    # Same guard as the AGENTS.md branch: .claude/rules/*.md is a project write
    # too, and it reaches here by a different route. Writing a rules directory
    # into ~/Downloads or Box cloud storage is no better than an AGENTS.md.
    if not _inside_git_repo(Path(project)):
        raise RoutingError(
            f"learning {learning.get('id', '<no id>')!r}: refusing to write a "
            f"rule file into {project} — it is not a git repository. "
            "A session's cwd is not evidence that the directory is a project."
        )
    return Path(project) / ".claude" / "rules" / f"{skill_slug(learning)}.md"


def instruction_target_paths(project_paths: list[str], cfg: Config) -> list[str]:
    """Every instruction file currently in force, given the projects we've seen.

    Takes project paths rather than a Store so the READ-ONLY search path can
    call it with its own connection — search.py runs inside the sandboxed
    miner's Bash allowlist and must never hold a writable handle.

    Order is stable (globals first) so callers that truncate get a predictable
    corpus rather than a filesystem-order-dependent one.
    """
    paths = [cfg.global_claude_md, cfg.codex_global_agents_md]
    for proj in project_paths:
        if not proj:
            continue
        base = Path(proj)
        for name in ("AGENTS.md", "CLAUDE.md"):
            candidate = base / name
            if candidate.exists():
                paths.append(str(candidate))
    skills = Path(cfg.skills_dir)
    if skills.is_dir():
        paths.extend(sorted(str(s) for s in skills.glob("*/SKILL.md")))
    return paths


def violated_existing_rule(learning: dict) -> str:
    """The in-force instruction line this learning's incident violated ('' if none).

    Miner key ``violated_existing_rule``; DB column of the same name. Distinct
    from ``duplicate_of``: a duplicate means the LESSON is already written; a
    violation means the rule was written AND ignored.
    """
    return str(learning.get("violated_existing_rule") or "")


def route(learning: dict, cfg: Config) -> RouteDecision:
    """Apply the routing decision table to one learning (first match wins)."""
    scope = scope_guess(learning)
    project_count = int(learning.get("project_count", 0))
    source = str(learning.get("source", ""))

    violated = violated_existing_rule(learning)
    if violated.strip():
        # Rule 0 — enforcement gap: the rule exists in force and was still
        # ignored, so another advisory line (anywhere) is not the fix;
        # deterministic enforcement is. Outranks every other rule, including
        # the project-count promotion.
        return RouteDecision(
            target_path=Path(cfg.global_claude_md).parent / "settings.json",
            target_kind="hook",
            action="convert_to_hook",
            reason=(
                "violated_existing_rule non-empty: in-force rule was ignored; "
                "advisory text failed, convert to deterministic hook "
                "(review queue)"
            ),
        )

    if project_count >= cfg.global_promotion_min_projects or scope == "global":
        if project_count >= cfg.global_promotion_min_projects:
            reason = (
                f"project_count {project_count} >= "
                f"global_promotion_min_projects {cfg.global_promotion_min_projects}"
            )
        else:
            reason = "scope_guess == 'global'"
        return RouteDecision(
            target_path=Path(cfg.global_claude_md),
            target_kind="global_claude_md",
            action="add",
            reason=reason,
        )

    if scope == "hook":
        return RouteDecision(
            target_path=Path(cfg.global_claude_md).parent / "settings.json",
            target_kind="hook",
            action="convert_to_hook",
            reason="scope_guess == 'hook': review queue only; no diff applied in MVP",
        )

    if scope == "skill":
        return RouteDecision(
            target_path=skill_md_path(cfg, learning),
            target_kind="skill",
            action="new_skill",
            reason="scope_guess == 'skill'",
        )

    if scope == "rule_path":
        return RouteDecision(
            target_path=rule_file_path(learning),
            target_kind="rule_file",
            action="new_rule_file",
            reason="scope_guess == 'rule_path': path-glob-scoped rule file",
        )

    if scope == "codex_global" or (source == "codex" and project_count >= 2):
        reason = (
            "scope_guess == 'codex_global'"
            if scope == "codex_global"
            else f"source codex and cross-project (project_count {project_count} >= 2)"
        )
        return RouteDecision(
            target_path=Path(cfg.codex_global_agents_md),
            target_kind="codex_global",
            action="add",
            reason=reason,
        )

    project = primary_project(learning)
    # A session's cwd does not establish a project repository. Use git
    # rev-parse so repository subdirectories qualify while unrelated folders
    # cannot become instruction-file targets.
    if not _inside_git_repo(Path(project)):
        raise RoutingError(
            f"learning {learning.get('id', '<no id>')!r}: refusing to write an "
            f"instruction file into {project} — it is not a git repository. "
            "A session's cwd is not evidence that the directory is a project."
        )
    topo = detect_topology(Path(project))
    if bool(learning.get("claude_specific", False)):
        target = topo.write_target_claude_specific
        assert target is not None  # detect_topology always sets a Path
    else:
        target = topo.write_target_general
    kind = "project_claude_md" if target.name == "CLAUDE.md" else "project_agents_md"
    return RouteDecision(
        target_path=target,
        target_kind=kind,
        action="add",
        reason=f"project-scoped ({topology_label(topo)}) in {project}",
        topology=topo,
    )


def _inside_git_repo(path: Path) -> bool:
    """True when ``path`` is inside a git work tree (subdirectories included)."""
    import subprocess

    if not path.is_dir():
        return False
    try:
        out = subprocess.run(
            ("git", "rev-parse", "--is-inside-work-tree"),
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def topology_label(topo: Topology) -> str:
    """Short human-readable label for a Topology (used in route reasons)."""
    if topo.claude_md_is_symlink:
        return "CLAUDE.md symlink"
    if topo.claude_md_is_stub:
        return "CLAUDE.md stub"
    if not topo.claude_md_exists and not topo.agents_md_exists:
        return "no instruction files yet"
    return "independent files"
