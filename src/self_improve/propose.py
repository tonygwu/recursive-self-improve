"""Proposal construction: itemized unified diffs against instruction files.

Construction rules:

- Adds are ALWAYS single bullets appended under a ``## Learned rules
  (self-improve)`` section (created at end of file if absent), formatted::

      - **<bold lead>.** <rule> <!-- si:<learning-id> -->

  The trailing HTML comment marker is how machine-authored lines are later
  distinguished from human-authored ones (``is_machine_line``). Whole-file
  rewrites are never proposed.

- Deletions: only a line carrying an ``si:`` marker may get action
  ``delete``; deleting an unmarked (human-authored) line becomes action
  ``delete_human_line``, which the apply policy sends to the review queue.

- Edits (amend-of-applied): ``build_edit_proposal`` replaces exactly the line
  carrying the learning's ``si:`` marker with the re-rendered bullet (same
  marker id). ``edit`` is deliberately NOT in ``cfg.review_queue_actions`` —
  the edited rule re-gates like any proposal before it can auto-apply.

- Path-scoped rule files: ``render_rule_file`` emits YAML frontmatter
  (``paths:`` globs) + the marker-tagged bullet; action ``new_rule_file``
  diffs it against empty content, mirroring ``new_skill``.

- Global budget guard: an add aimed at ``cfg.global_claude_md`` whose current
  content is at/over ``cfg.global_claude_md_line_budget`` lines is demoted to
  a ``new_skill`` proposal, with the demotion reason recorded on the proposal.

- ``apply_unified_diff`` is STRICT: any context/delete mismatch, malformed
  hunk, or count disagreement raises :class:`PatchConflict` (apply.py depends
  on this fail-loud behavior; there is no fuzz).

Newline convention (recorded, never silent): diffs are line-based; applied
content is always ``'\\n'``-joined and newline-terminated when non-empty. If a
target file lacked a trailing newline, the proposal records
``normalized_trailing_newline: True`` — the one byte-level normalization this
module performs.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from self_improve import routing
from self_improve.config import Config
from self_improve.routing import RouteDecision

SECTION_HEADER = "## Learned rules (self-improve)"

# Machine-authored-line marker: <!-- si:<learning-id> -->
#
# Do not redact rendered diffs or applied files: generic secret patterns can
# alter the row IDs that identify lines for edit, deletion, and rollback.
# Evidence entering a model prompt follows the separate redacted path in
# miner.py and render.py.
MARKER_RE = re.compile(r"<!--\s*si:([A-Za-z0-9][A-Za-z0-9_-]*)\s*-->")

_HEADING_RE = re.compile(r"^#{1,6}\s")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class ProposalError(Exception):
    """Raised when a proposal cannot be built (bad learning data, drift...)."""


class PatchConflict(Exception):
    """Raised by apply_unified_diff on any mismatch or malformed diff."""


# ----------------------------------------------------------------------------
# Marker-based line classification.
# ----------------------------------------------------------------------------


def line_marker_id(line: str) -> str | None:
    """The si:<id> marker id carried by ``line``, or None if unmarked."""
    m = MARKER_RE.search(line)
    return m.group(1) if m else None


def is_machine_line(line: str) -> bool:
    """True iff ``line`` carries an si: marker (machine-authored)."""
    return line_marker_id(line) is not None


# ----------------------------------------------------------------------------
# Content builders.
# ----------------------------------------------------------------------------


def _learning_id(learning: dict) -> str:
    lid = str(learning.get("id") or "")
    if not lid:
        raise ProposalError("learning has no 'id'")
    return lid


def _bold_lead(learning: dict) -> str:
    """Bullet lead: the title (sans trailing '.'), else rule_text's head."""
    title = str(learning.get("title") or "").strip().rstrip(".")
    if title:
        return title
    words = str(learning.get("rule_text") or "").strip().split()
    if not words:
        raise ProposalError(
            f"learning {learning.get('id', '<no id>')!r} has neither title nor rule_text"
        )
    return " ".join(words[:8]).rstrip(".,;:")


def format_bullet(learning: dict) -> str:
    """Render the single-bullet form of a learning, marker included."""
    lid = _learning_id(learning)
    rule = str(learning.get("rule_text") or "").strip()
    if not rule:
        raise ProposalError(f"learning {lid!r} has empty rule_text")
    return f"- **{_bold_lead(learning)}.** {rule} <!-- si:{lid} -->"


def append_learned_rule(content: str, bullet: str) -> str:
    """Append ``bullet`` under SECTION_HEADER, creating the section at EOF.

    Deterministic placement: after the section's last non-blank line; an
    empty section gets one blank line between header and bullet. Returns
    newline-terminated content.
    """
    lines = content.splitlines()
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.rstrip() == SECTION_HEADER), None
    )
    if header_idx is None:
        new_lines = list(lines)
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines += [SECTION_HEADER, "", bullet]
    else:
        end = next(
            (j for j in range(header_idx + 1, len(lines)) if _HEADING_RE.match(lines[j])),
            len(lines),
        )
        body = [j for j in range(header_idx + 1, end) if lines[j].strip()]
        if body:
            insert_at = body[-1] + 1
            new_lines = lines[:insert_at] + [bullet] + lines[insert_at:]
        else:
            # Empty section: normalize its body to one blank line + bullet.
            new_lines = lines[: header_idx + 1] + ["", bullet] + lines[end:]
    return "\n".join(new_lines) + "\n"


def _path_globs(learning: dict) -> list[str]:
    """The learning's path globs (miner key ``path_globs``; DB ``path_globs_json``).

    Strict: malformed JSON, a non-list, non-string/empty elements, or an empty
    glob list all raise ProposalError — a rule file scoped to no paths would
    silently apply nowhere (or everywhere, depending on the reader).
    """
    lid = _learning_id(learning)
    globs = learning.get("path_globs")
    if globs is None and learning.get("path_globs_json") is not None:
        try:
            globs = json.loads(learning["path_globs_json"])
        except json.JSONDecodeError as exc:
            raise ProposalError(
                f"learning {lid!r}: path_globs_json is not valid JSON: {exc}"
            ) from exc
    if not isinstance(globs, list) or not all(
        isinstance(g, str) and g.strip() for g in globs
    ):
        raise ProposalError(
            f"learning {lid!r}: path_globs must be a list of non-empty strings, "
            f"got {globs!r}"
        )
    if not globs:
        raise ProposalError(
            f"learning {lid!r}: rule_path learning has no path globs"
        )
    return globs


def render_rule_file(learning: dict) -> str:
    """Full <project>/.claude/rules/<slug>.md content for a new_rule_file proposal.

    YAML frontmatter declaring the path globs, then the marker-tagged bullet.
    Globs are emitted as JSON strings (a JSON string is a safe YAML scalar, so
    quotes/globs never need YAML escaping logic here).
    """
    globs = _path_globs(learning)
    lines = ["---", "paths:"]
    lines += [f"  - {json.dumps(g)}" for g in globs]
    lines += ["---", "", format_bullet(learning)]
    return "\n".join(lines) + "\n"


def render_skill_md(learning: dict) -> str:
    """Full SKILL.md content for a new-skill proposal (marker included)."""
    lid = _learning_id(learning)
    slug = routing.skill_slug(learning)
    rule = str(learning.get("rule_text") or "").strip()
    if not rule:
        raise ProposalError(f"learning {lid!r} has empty rule_text")
    why = str(learning.get("why") or "").strip()
    description = " ".join((why or rule).split())  # one line
    title = str(learning.get("title") or "").strip() or _bold_lead(learning)
    lines = [
        "---",
        f"name: {slug}",
        f"description: {json.dumps(description)}",  # JSON string == safe YAML scalar
        "---",
        "",
        f"# {title}",
        "",
        format_bullet(learning),
    ]
    if why:
        lines += ["", "## Why", "", why]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# Unified diff: generation and strict application.
# ----------------------------------------------------------------------------


def make_unified_diff(old: str, new: str, path: str) -> str:
    """Unified diff (3 context lines) from ``old`` to ``new`` for ``path``.

    Header convention: ``--- /dev/null`` when ``old`` is empty (new file),
    else ``--- <path>``; ``+++ <path>`` always. Returns "" when old == new.
    """
    diff_lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="/dev/null" if old == "" else path,
            tofile=path,
            n=3,
            lineterm="",
        )
    )
    if not diff_lines:
        return ""
    return "\n".join(diff_lines) + "\n"


def apply_unified_diff(content: str, diff: str) -> str:
    """Strictly apply ``diff`` (as produced by make_unified_diff) to ``content``.

    Every context and delete line must match the source exactly, hunks must be
    in order and their declared counts must match their bodies — any deviation
    raises :class:`PatchConflict`. Never fuzzes, never guesses.

    Returned content is '\\n'-joined and newline-terminated when non-empty
    (see the module-level newline convention).
    """
    dlines = diff.splitlines()
    if not dlines:
        raise PatchConflict("empty diff")
    if len(dlines) < 2 or not dlines[0].startswith("--- ") or not dlines[1].startswith("+++ "):
        raise PatchConflict("malformed diff: missing ---/+++ headers")

    src = content.splitlines()
    out: list[str] = []
    pos = 0  # index into src of the next unconsumed line
    i = 2
    saw_hunk = False
    while i < len(dlines):
        m = _HUNK_RE.match(dlines[i])
        if not m:
            raise PatchConflict(f"malformed hunk header at diff line {i + 1}: {dlines[i]!r}")
        saw_hunk = True
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        # "-l,0" means insertion AFTER source line l (0-based index l);
        # "-l,n" (n>0) starts AT 1-based line l (0-based index l-1).
        hunk_at = old_start if old_count == 0 else old_start - 1
        if hunk_at < pos:
            raise PatchConflict(f"hunks out of order or overlapping at diff line {i + 1}")
        if hunk_at > len(src):
            raise PatchConflict(
                f"hunk start {old_start} beyond end of source ({len(src)} lines)"
            )
        out.extend(src[pos:hunk_at])
        pos = hunk_at
        i += 1
        consumed_old = 0
        produced_new = 0
        while i < len(dlines) and not dlines[i].startswith("@@"):
            ln = dlines[i]
            if ln.startswith("\\"):
                raise PatchConflict(
                    "'\\ No newline at end of file' markers are not supported; "
                    "diffs in this system are newline-normalized"
                )
            if ln == "":
                raise PatchConflict(
                    f"malformed diff line {i + 1}: empty (expected ' ', '+' or '-' prefix)"
                )
            tag, text = ln[0], ln[1:]
            if tag == " ":
                if pos >= len(src) or src[pos] != text:
                    found = src[pos] if pos < len(src) else "<end of file>"
                    raise PatchConflict(
                        f"context mismatch at source line {pos + 1}: "
                        f"expected {text!r}, found {found!r}"
                    )
                out.append(src[pos])
                pos += 1
                consumed_old += 1
                produced_new += 1
            elif tag == "-":
                if pos >= len(src) or src[pos] != text:
                    found = src[pos] if pos < len(src) else "<end of file>"
                    raise PatchConflict(
                        f"delete mismatch at source line {pos + 1}: "
                        f"expected {text!r}, found {found!r}"
                    )
                pos += 1
                consumed_old += 1
            elif tag == "+":
                out.append(text)
                produced_new += 1
            else:
                raise PatchConflict(f"unknown diff line prefix {tag!r} at diff line {i + 1}")
            i += 1
        if consumed_old != old_count:
            raise PatchConflict(
                f"hunk declared {old_count} source lines but consumed {consumed_old}"
            )
        if produced_new != new_count:
            raise PatchConflict(
                f"hunk declared {new_count} result lines but produced {produced_new}"
            )
    if not saw_hunk:
        raise PatchConflict("diff has headers but no hunks")
    out.extend(src[pos:])
    if not out:
        return ""
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------
# Proposal construction.
# ----------------------------------------------------------------------------


def _find_delete_line(learning: dict, content: str, target: str) -> int:
    """Index (in splitlines) of the single line a delete proposal removes.

    The learning must carry ``delete_line`` (exact line text) or
    ``delete_marker_id`` (si: marker id). Zero matches means the target
    drifted; multiple matches are ambiguous — both raise (fail loud).
    """
    lines = content.splitlines()
    delete_line = learning.get("delete_line")
    if delete_line is not None:
        matches = [i for i, ln in enumerate(lines) if ln == delete_line]
        what = f"line {delete_line!r}"
    else:
        marker_id = learning.get("delete_marker_id")
        if not marker_id:
            raise ProposalError(
                "delete action requires 'delete_line' or 'delete_marker_id' on the learning"
            )
        matches = [i for i, ln in enumerate(lines) if line_marker_id(ln) == marker_id]
        what = f"marker si:{marker_id}"
    if not matches:
        raise ProposalError(f"delete target not found in {target}: {what} (content drifted?)")
    if len(matches) > 1:
        raise ProposalError(f"delete target ambiguous in {target}: {len(matches)} lines match {what}")
    return matches[0]


def build_proposal(
    learning: dict, route: RouteDecision, current_content: str, cfg: Config
) -> dict:
    """Build one proposal dict (with ``diff_unified``) for a routed learning.

    Handles actions ``add``, ``delete`` (auto-reclassified to
    ``delete_human_line`` for unmarked lines), ``new_skill``,
    ``new_rule_file`` (path-glob-scoped rule file; diff against empty), and
    ``convert_to_hook`` (empty diff; review queue). An ``add`` against the
    global CLAUDE.md at/over the line budget is demoted to ``new_skill`` with
    ``demotion_reason`` recorded. Edits of already-applied rules go through
    :func:`build_edit_proposal`, not here.

    ``current_content`` must be the target file's current content ("" for a
    file that does not exist yet). Raises :class:`ProposalError` on malformed
    learnings, drifted content, or a duplicate si: marker.
    """
    lid = _learning_id(learning)
    action = route.action
    target = Path(route.target_path)
    kind = route.target_kind
    demotion_reason = ""

    # --- global line-budget guard (adds only) ---
    if action == "add" and target == Path(cfg.global_claude_md):
        n_lines = len(current_content.splitlines())
        if n_lines >= cfg.global_claude_md_line_budget:
            demotion_reason = (
                f"global CLAUDE.md has {n_lines} lines >= budget "
                f"{cfg.global_claude_md_line_budget}; demoted add to new_skill"
            )
            action = "new_skill"
            kind = "skill"
            target = routing.skill_md_path(cfg, learning)
            current_content = ""  # the skill file is new; diff is against empty

    proposal: dict = {
        "learning_id": lid,
        "target_path": str(target),
        "target_kind": kind,
        "action": action,
        "diff_unified": "",
        "marker": f"si:{lid}",
        "demotion_reason": demotion_reason,
        "normalized_trailing_newline": False,
        "note": "",
    }

    if action == "convert_to_hook":
        proposal["note"] = "hook conversion: review queue only; no diff applied in MVP"
        return proposal

    if action == "add":
        if any(line_marker_id(ln) == lid for ln in current_content.splitlines()):
            raise ProposalError(f"learning {lid} marker already present in {target}")
        new_content = append_learned_rule(current_content, format_bullet(learning))
    elif action == "new_skill":
        if current_content:
            raise ProposalError(
                f"new_skill target already has content: {target} "
                "(refusing to diff against non-empty file)"
            )
        new_content = render_skill_md(learning)
    elif action == "new_rule_file":
        if current_content:
            raise ProposalError(
                f"new_rule_file target already has content: {target} "
                "(refusing to diff against non-empty file)"
            )
        new_content = render_rule_file(learning)
    elif action == "delete":
        idx = _find_delete_line(learning, current_content, str(target))
        lines = current_content.splitlines()
        if not is_machine_line(lines[idx]):
            # Human-authored line: reclassify; apply policy queues it for review.
            proposal["action"] = "delete_human_line"
            proposal["note"] = "deleting a human-authored (unmarked) line: review queue"
        new_lines = lines[:idx] + lines[idx + 1 :]
        new_content = "\n".join(new_lines) + "\n" if new_lines else ""
    else:
        raise ProposalError(f"unsupported route action {action!r}")

    proposal["diff_unified"] = make_unified_diff(current_content, new_content, str(target))
    if current_content and not current_content.endswith("\n"):
        proposal["normalized_trailing_newline"] = True
        note = "target lacked a trailing newline; applied content is newline-terminated"
        proposal["note"] = f"{proposal['note']}; {note}" if proposal["note"] else note
    return proposal


def build_edit_proposal(
    learning: dict,
    target_path: str | Path,
    current_content: str,
    cfg: Config,
    *,
    target_kind: str = "",
) -> dict:
    """Build an in-place EDIT proposal for a learning that was already applied.

    Used by the pipeline's amend-of-applied branch: the learning's rule
    already lives in ``target_path`` as a marker-tagged bullet, and its
    rule_text was amended at mine time, so the fix is to replace exactly the
    line carrying ``<!-- si:<learning-id> -->`` with the re-rendered bullet
    (:func:`format_bullet` keeps the SAME marker id).

    Fail-loud contract:
    - marker missing from ``current_content`` -> ProposalError (the file may
      have been hand-edited or the rule removed; never guess a line);
    - marker on more than one line -> ProposalError (ambiguous);
    - re-rendered bullet identical to the existing line -> ProposalError (an
      empty diff would fail at apply time anyway; better to fail here with
      the real reason).

    ``cfg`` is accepted for signature symmetry with :func:`build_proposal`.
    No line-budget guard applies to an in-place edit.
    ``target_kind`` is the proposals-table kind of the PRIOR applied proposal
    — routing is skipped for edits, so the caller carries it over.
    """
    lid = _learning_id(learning)
    del cfg  # deliberate: no config knob influences an in-place edit
    target = Path(target_path)
    lines = current_content.splitlines()
    matches = [i for i, ln in enumerate(lines) if line_marker_id(ln) == lid]
    if not matches:
        raise ProposalError(
            f"edit target marker si:{lid} not found in {target} "
            "(file hand-edited or rule removed? content drifted from the "
            "applied proposal)"
        )
    if len(matches) > 1:
        raise ProposalError(
            f"edit target ambiguous in {target}: {len(matches)} lines carry "
            f"marker si:{lid}"
        )
    bullet = format_bullet(learning)
    idx = matches[0]
    if lines[idx] == bullet:
        raise ProposalError(
            f"edit for learning {lid} produces no change in {target} "
            "(amended bullet identical to the existing line)"
        )
    new_lines = lines[:idx] + [bullet] + lines[idx + 1 :]
    new_content = "\n".join(new_lines) + "\n"

    proposal: dict = {
        "learning_id": lid,
        "target_path": str(target),
        "target_kind": target_kind,
        "action": "edit",
        "diff_unified": make_unified_diff(current_content, new_content, str(target)),
        "marker": f"si:{lid}",
        "demotion_reason": "",
        "normalized_trailing_newline": False,
        "note": "in-place edit of applied rule at its si: marker line",
    }
    if current_content and not current_content.endswith("\n"):
        proposal["normalized_trailing_newline"] = True
        proposal["note"] += (
            "; target lacked a trailing newline; applied content is "
            "newline-terminated"
        )
    return proposal
