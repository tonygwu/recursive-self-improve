"""Measure instruction context for a repository (PRD section 8a).

Pure filesystem reads: no database, network, or FastAPI dependency, and no writes.

Count root CLAUDE.md and AGENTS.md, .claude/rules/*.md, .claude/skills/*/SKILL.md,
and supported transitive @-imports. Other Markdown is reported separately in
``other_md`` and never added to instruction ``total_bytes``.

``always_loaded`` includes root instructions, their imports, and unscoped rules.
Skills are on demand; a rule whose frontmatter declares paths, globs, glob,
applies_to, or appliesTo is scoped. Both contribute to total instruction bytes
without being counted as always loaded. Global instructions are not read unless
explicitly imported by the repository's instructions.

Deduplicate candidates by realpath, report aliases in ``deduplicated``, and count
one symlink target once. Bytes come from st_size; UTF-8 character counts are also
reported. ``approx_tokens`` uses CHARS_PER_TOKEN and is an estimate, with bytes
as a fallback when characters are unavailable.

Report missing imports, unreadable files, cycles, and each applied cap with what
it omitted. Missing and non-Git paths return a named status and reason. Results
are not memoized: edits must be visible on the next measurement.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from self_improve import routing

#: Claude Code follows ``@``-imports at most 5 hops deep; matched here so the
#: measurement matches what an agent actually loads.
DEFAULT_MAX_IMPORT_DEPTH = 5

#: Bytes of a single instruction file read while scanning it for ``@``-imports.
#: The file's full ``st_size`` is always counted; only the *scan* is capped, and
#: a cap that bites is reported in ``caps_applied``.
DEFAULT_MAX_IMPORT_SCAN_BYTES = 1 << 20  # 1 MiB

#: Maximum files visited by the ``other_md`` walk; omissions are reported.
DEFAULT_MAX_OTHER_MD_FILES = 20_000

#: Rough characters per token, used only for the ``approx_tokens`` fields.
CHARS_PER_TOKEN = 4

#: Directories the ``other_md`` walk never descends into. These hold no
#: instruction files and can dominate walk cost. The pruned count is reported.
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "site-packages",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        "dist",
        "build",
    }
)

# A candidate @-import: an "@" at the start of a line or after whitespace,
# followed by a path token. Qualification happens in _import_spec().
_IMPORT_RE = re.compile(r"(?:^|(?<=\s))@(?P<spec>\S+)")

# Inline code spans are blanked before the scan: `@AGENTS.md` inside backticks
# is prose about an import, not an import.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")

# A YAML frontmatter key that scopes a rule file to particular paths.
_SCOPE_KEY_RE = re.compile(
    r"^(?:paths|globs?|applies_?to)\s*:", re.IGNORECASE | re.MULTILINE
)

_TRAILING_PUNCT = ".,;:!?)]}\"'"


class _Candidate:
    """One file queued for measurement.

    ``chain`` is the realpaths of the files that imported it, so a repeat visit
    can be classified as a genuine cycle rather than a diamond (two files
    importing one third file).
    """

    __slots__ = ("path", "kind", "always_loaded", "depth", "origin", "spec", "chain")

    def __init__(
        self,
        path: Path,
        kind: str,
        always_loaded: bool,
        depth: int,
        origin: str,
        spec: str = "",
        chain: tuple[str, ...] = (),
    ) -> None:
        self.path = path
        self.kind = kind
        self.always_loaded = always_loaded
        self.depth = depth
        self.origin = origin
        self.spec = spec
        self.chain = chain


# ----------------------------------------------------------------------------
# @-import scanning
# ----------------------------------------------------------------------------


def _import_spec(raw: str) -> str:
    """The import path in ``raw``, or "" if ``raw`` is not an import.

    An ``@`` token qualifies only when it ends in ``.md`` (after trailing
    sentence punctuation is stripped) or starts with ``~/``, ``./``, ``../`` or
    ``/``. That keeps ``@anthropic-ai/claude-code``, ``@claude`` and a Python
    ``@decorator`` out of the import graph, where treating them as imports
    would fill ``missing_imports`` with noise. The cost is that an extensionless
    import such as ``@AGENTS`` is missed; Claude Code needs a real file path
    there, so such a token would not resolve for an agent either.
    """
    spec = raw.rstrip(_TRAILING_PUNCT)
    if not spec:
        return ""
    if spec.lower().endswith(".md"):
        return spec
    if spec.startswith(("~/", "./", "../", "/")):
        return spec
    return ""


def find_imports(text: str) -> list[str]:
    """Every ``@``-import path in ``text``, in order, duplicates kept.

    Fenced code blocks and inline code spans are skipped: an agent does not
    import a path that a document is quoting.
    """
    specs: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        for match in _IMPORT_RE.finditer(_INLINE_CODE_RE.sub(" ", line)):
            spec = _import_spec(match.group("spec"))
            if spec:
                specs.append(spec)
    return specs


def _resolve_import(spec: str, from_file: Path) -> Path:
    """Resolve ``spec`` relative to the file that contains it (``~`` expanded)."""
    path = Path(spec).expanduser()
    if not path.is_absolute():
        path = from_file.parent / path
    return path


def _is_path_scoped_rule(text: str) -> bool:
    """True if a rule file's YAML frontmatter scopes it to particular paths."""
    if not text.startswith("---"):
        return False
    rest = text.split("\n", 1)[1] if "\n" in text else ""
    end = rest.find("\n---")
    block = rest if end == -1 else rest[:end]
    return bool(_SCOPE_KEY_RE.search(block))


# ----------------------------------------------------------------------------
# Repo shape
# ----------------------------------------------------------------------------


def _find_git_root(start: Path) -> str:
    """The nearest ancestor (inclusive) holding a ``.git`` entry, else ""."""
    current = start
    for _ in range(64):  # bounded: never walk an unbounded parent chain
        if os.path.lexists(current / ".git"):
            return str(current)
        parent = current.parent
        if parent == current:
            return ""
        current = parent
    return ""


def _topology(repo: Path) -> dict[str, Any]:
    """The instruction-file topology, via the same detector the writer uses.

    ``routing.detect_topology`` decides where a proposal may be written, so
    reusing it keeps the dashboard's answer and the writer's answer identical.
    It raises on a missing directory or a CLAUDE.md that is not UTF-8; both are
    reported here rather than raised, because this function never raises.
    """
    try:
        topo = routing.detect_topology(repo)
    except routing.TopologyError as exc:
        return {"label": "unknown", "error": str(exc)}
    return {
        "label": routing.topology_label(topo),
        "error": "",
        "agents_md_exists": topo.agents_md_exists,
        "claude_md_exists": topo.claude_md_exists,
        "claude_md_is_symlink": topo.claude_md_is_symlink,
        "claude_md_symlink_to_agents": topo.claude_md_symlink_to_agents,
        "claude_md_is_stub": topo.claude_md_is_stub,
        "write_target_general": str(topo.write_target_general),
        "write_target_claude_specific": str(topo.write_target_claude_specific),
    }


def _seed_candidates(repo: Path) -> list[_Candidate]:
    """The in-force files PRD 8a names, before imports are followed.

    Skills are matched one level deep (``.claude/skills/*/SKILL.md``), the same
    glob ``routing.instruction_target_paths`` uses. A skill nested deeper is
    missed, which undercounts rather than overstates.
    """
    claude_md = repo / "CLAUDE.md"
    agents_md = repo / "AGENTS.md"
    roots = [
        _Candidate(claude_md, "claude_md", True, 0, "repo root"),
        _Candidate(agents_md, "agents_md", True, 0, "repo root"),
    ]
    # A CLAUDE.md symlink is measured second, so the counted entry is the real
    # file (AGENTS.md) and the link is what shows up in ``deduplicated``.
    if claude_md.is_symlink():
        roots.reverse()
    seeds = list(roots)
    rules_dir = repo / ".claude" / "rules"
    for rule in sorted(rules_dir.glob("*.md")):
        seeds.append(_Candidate(rule, "rule", True, 0, ".claude/rules"))
    skills_dir = repo / ".claude" / "skills"
    for skill in sorted(skills_dir.glob("*/SKILL.md")):
        seeds.append(_Candidate(skill, "skill", False, 0, ".claude/skills"))
    return [s for s in seeds if os.path.lexists(s.path)]


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def _blank_result(repo_path: str, resolved: Path, status: str, reason: str) -> dict:
    """A full-shaped result for a path that could not be measured.

    Every key the ok path returns is present, so a caller never has to guess
    whether a key exists.
    """
    return {
        "repo_path": str(repo_path),
        "resolved_path": str(resolved),
        "status": status,
        "reason": reason,
        "exists": status not in ("missing",),
        "is_git_repo": False,
        "git_root": "",
        "has_instruction_files": False,
        "files": [],
        "file_count": 0,
        "total_bytes": 0,
        "always_loaded_bytes": 0,
        "on_demand_bytes": 0,
        "always_loaded_file_count": 0,
        "approx_tokens": 0,
        "always_loaded_approx_tokens": 0,
        "missing_imports": [],
        "import_cycles": [],
        "deduplicated": [],
        "unreadable": [],
        "topology": {"label": "unknown", "error": reason},
        "other_md": {
            "scanned": False,
            "file_count": 0,
            "bytes": 0,
            "largest": [],
            "pruned_dir_count": 0,
            "skipped_dir_names": sorted(SKIP_DIR_NAMES),
            "stat_errors": [],
            "stat_error_count": 0,
            "note": "not scanned: " + reason,
        },
        "caps_applied": [],
        "notes": [reason] if reason else [],
    }


def context_weight(
    repo_path: str,
    *,
    scan_other_md: bool = True,
    max_import_depth: int = DEFAULT_MAX_IMPORT_DEPTH,
    max_import_scan_bytes: int = DEFAULT_MAX_IMPORT_SCAN_BYTES,
    max_other_md_files: int = DEFAULT_MAX_OTHER_MD_FILES,
) -> dict:
    """Measure the instruction context ``repo_path`` loads into a session.

    Returns a dict. The load-bearing keys:

    ``total_bytes``
        Bytes of the in-force instruction files only. Never includes the
        repo's other markdown; see the module docstring.
    ``always_loaded_bytes``
        The subset that enters **every** session: CLAUDE.md, AGENTS.md, their
        transitive ``@``-imports, and unscoped rule files.
    ``files``
        One entry per in-force file, largest first, each with ``path``,
        ``bytes``, ``always_loaded``, ``kind``, ``chars``, ``origin`` and
        symlink information. A symlink and its target appear once.
    ``status`` / ``reason``
        ``ok``, ``missing``, ``not_a_directory`` or ``not_a_git_checkout``.
        The last still carries a full measurement; the directory is simply not
        inside a git checkout.
    ``other_md``
        The repo's remaining markdown, counted separately and never added in.

    Never raises for a path problem. ``scan_other_md=False`` skips the
    repo-wide walk, which is the only expensive part.
    """
    raw = Path(repo_path).expanduser()
    resolved = Path(os.path.abspath(raw))

    if not os.path.lexists(resolved):
        return _blank_result(
            repo_path, resolved, "missing", f"path does not exist: {resolved}"
        )
    if not resolved.is_dir():
        return _blank_result(
            repo_path, resolved, "not_a_directory", f"path is not a directory: {resolved}"
        )

    repo_real = os.path.realpath(resolved)
    git_root = _find_git_root(resolved)
    status = "ok" if git_root else "not_a_git_checkout"
    reason = (
        ""
        if git_root
        else f"no .git found at {resolved} or any parent; measured anyway"
    )

    counted: dict[str, dict] = {}  # realpath -> file entry
    missing_imports: list[dict] = []
    import_cycles: list[dict] = []
    deduplicated: list[dict] = []
    unreadable: list[dict] = []
    caps_applied: list[dict] = []
    notes: list[str] = []
    if reason:
        notes.append(reason)

    queue: list[_Candidate] = _seed_candidates(resolved)
    while queue:
        cand = queue.pop(0)
        path = cand.path
        real = os.path.realpath(path)

        if real in counted:
            entry = counted[real]
            record = {
                "path": str(path),
                "same_file_as": entry["path"],
                "kind": cand.kind,
                "origin": cand.origin,
            }
            if cand.kind == "import" and real in cand.chain:
                import_cycles.append(record)
            else:
                deduplicated.append(record)
            # A file reached once on demand and once always-loaded IS always
            # loaded; upgrading can only be found by re-visiting it.
            if cand.always_loaded and not entry["always_loaded"]:
                entry["always_loaded"] = True
                entry["origin"] += f"; also {cand.origin}"
            continue

        if not os.path.isfile(path):
            if cand.kind == "import":
                missing_imports.append(
                    {
                        "spec": cand.spec,
                        "imported_by": cand.chain[-1] if cand.chain else "",
                        "resolved": str(path),
                        "reason": "imported file does not exist",
                    }
                )
            else:
                notes.append(f"{path} is not a regular file; not counted")
            continue

        size = os.stat(path).st_size
        text: str | None = None
        truncated_read = size > max_import_scan_bytes
        try:
            with open(path, "rb") as handle:
                data = handle.read(max_import_scan_bytes)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                # A cap can cut a multi-byte character in half. That is the
                # cap's doing, not a file that is not UTF-8, and calling it the
                # latter would report the wrong fault.
                if truncated_read and exc.start >= len(data) - 4:
                    text = data[: exc.start].decode("utf-8")
                else:
                    raise
        except UnicodeDecodeError as exc:
            unreadable.append(
                {
                    "path": str(path),
                    "reason": f"not UTF-8: {exc}",
                    "consequence": "counted in bytes; @-imports not followed",
                }
            )
        except OSError as exc:
            unreadable.append(
                {
                    "path": str(path),
                    "reason": f"unreadable: {exc}",
                    "consequence": "counted in bytes; @-imports not followed",
                }
            )
        if truncated_read and text is not None:
            caps_applied.append(
                {
                    "cap": "max_import_scan_bytes",
                    "limit": max_import_scan_bytes,
                    "path": str(path),
                    "cut": f"{size - max_import_scan_bytes} bytes of {path} were "
                    "not scanned for @-imports (the full size is still counted)",
                }
            )

        always_loaded = cand.always_loaded
        if cand.kind == "rule" and text is not None and _is_path_scoped_rule(text):
            always_loaded = False

        entry = {
            "path": str(path),
            "bytes": size,
            "chars": None if text is None or truncated_read else len(text),
            "always_loaded": always_loaded,
            "kind": cand.kind,
            "origin": cand.origin,
            "depth": cand.depth,
            "real_path": real,
            "is_symlink": path.is_symlink(),
            "symlink_target": real if path.is_symlink() else "",
            "inside_repo": real.startswith(repo_real + os.sep),
        }
        counted[real] = entry

        if text is None:
            continue
        specs = find_imports(text)
        if not specs:
            continue
        if cand.depth >= max_import_depth:
            caps_applied.append(
                {
                    "cap": "max_import_depth",
                    "limit": max_import_depth,
                    "path": str(path),
                    "cut": f"{len(specs)} @-import(s) at depth {cand.depth} not "
                    f"followed: {', '.join(specs)}",
                }
            )
            continue
        for spec in specs:
            queue.append(
                _Candidate(
                    _resolve_import(spec, path),
                    "import",
                    always_loaded,
                    cand.depth + 1,
                    f"@{spec} in {path.name}",
                    spec=spec,
                    chain=cand.chain + (real,),
                )
            )

    files = sorted(counted.values(), key=lambda f: (-f["bytes"], f["path"]))
    total_bytes = sum(f["bytes"] for f in files)
    always_bytes = sum(f["bytes"] for f in files if f["always_loaded"])

    def _tokens(entries: list[dict]) -> int:
        return sum(
            (f["chars"] if f["chars"] is not None else f["bytes"]) for f in entries
        ) // CHARS_PER_TOKEN

    other_md = _walk_other_md(
        resolved,
        counted_realpaths=set(counted),
        enabled=scan_other_md,
        max_files=max_other_md_files,
        caps_applied=caps_applied,
    )

    if not files:
        notes.append(
            "no instruction files at all — a lesson routed here creates the "
            "first one (PRD 8a: an empty state, not a zero)"
        )

    return {
        "repo_path": str(repo_path),
        "resolved_path": str(resolved),
        "status": status,
        "reason": reason,
        "exists": True,
        "is_git_repo": bool(git_root),
        "git_root": git_root,
        "has_instruction_files": bool(files),
        "files": files,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "always_loaded_bytes": always_bytes,
        "on_demand_bytes": total_bytes - always_bytes,
        "always_loaded_file_count": sum(1 for f in files if f["always_loaded"]),
        "approx_tokens": _tokens(files),
        "always_loaded_approx_tokens": _tokens(
            [f for f in files if f["always_loaded"]]
        ),
        "missing_imports": missing_imports,
        "import_cycles": import_cycles,
        "deduplicated": deduplicated,
        "unreadable": unreadable,
        "topology": _topology(resolved),
        "other_md": other_md,
        "caps_applied": caps_applied,
        "notes": notes,
    }


def _walk_other_md(
    repo: Path,
    *,
    counted_realpaths: set[str],
    enabled: bool,
    max_files: int,
    caps_applied: list[dict],
) -> dict[str, Any]:
    """Count the repo's markdown that is NOT in force, reported separately.

    This number exists so the two can be compared, never so they can be added:
    PRD 8a's whole point is that the second number ranks repos by transcript
    size. Directory symlinks are not followed, so a symlinked tree is never
    walked twice.
    """
    result: dict[str, Any] = {
        "scanned": enabled,
        "file_count": 0,
        "bytes": 0,
        "largest": [],
        "pruned_dir_count": 0,
        "skipped_dir_names": sorted(SKIP_DIR_NAMES),
        "stat_errors": [],
        "stat_error_count": 0,
        "note": (
            "markdown in the repo that no session loads; reported beside "
            "total_bytes and never added into it"
        ),
    }
    if not enabled:
        result["note"] = "not scanned: scan_other_md=False"
        return result

    seen: list[tuple[int, str]] = []
    files_seen = 0
    stopped_at = ""
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        pruned = [d for d in dirnames if d in SKIP_DIR_NAMES]
        result["pruned_dir_count"] += len(pruned)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            files_seen += 1
            if files_seen > max_files:
                stopped_at = dirpath
                break
            if os.path.realpath(full) in counted_realpaths:
                continue
            try:
                size = os.stat(full).st_size
            except OSError as exc:
                # A broken symlink has no size. Say so; do not drop it quietly.
                if len(result["stat_errors"]) < 20:
                    result["stat_errors"].append({"path": full, "reason": str(exc)})
                result["stat_error_count"] += 1
                continue
            result["file_count"] += 1
            result["bytes"] += size
            seen.append((size, full))
        if stopped_at:
            break

    if stopped_at:
        caps_applied.append(
            {
                "cap": "max_other_md_files",
                "limit": max_files,
                "path": stopped_at,
                "cut": f"stopped the other-markdown walk at {stopped_at} after "
                f"{max_files} files; other_md.bytes is a floor, not a total",
            }
        )
        result["note"] += f" (TRUNCATED at {max_files} files; the count is a floor)"

    seen.sort(key=lambda item: (-item[0], item[1]))
    result["largest"] = [{"path": p, "bytes": b} for b, p in seen[:5]]
    return result
