"""Complete, read-only previews over an explicit selection of proposal revisions."""
from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
import hashlib

from .commands import CommandError, MAX_MEMBERS, _hash, review_snapshot
from .destinations import DestinationError, destination_identity, read_destination
from .propose import PatchConflict, apply_unified_diff, make_unified_diff


def combine_edits(before: str, diffs: list[str], path: str) -> tuple[str, str]:
    """Merge distinct changes to common source lines, independent of context overlap.

    Each input must apply strictly to the same base. Equal replacements collapse;
    intersecting replacements and different insertions at one point conflict.
    The result is deterministic regardless of member order.
    """
    original = before.splitlines()
    edits = set()
    for diff in set(diffs):
        after = apply_unified_diff(before, diff).splitlines()
        for tag, start, end, nstart, nend in SequenceMatcher(None, original, after, autojunk=False).get_opcodes():
            if tag != 'equal':
                edits.add((start, end, tuple(after[nstart:nend])))
    ordered = sorted(edits)
    for i, (start, end, replacement) in enumerate(ordered):
        for other_start, other_end, _ in ordered[i + 1:]:
            if other_start > end:
                break
            overlap = max(start, other_start) < min(end, other_end)
            same_insertion = start == end == other_start == other_end
            inside = (start == end and other_start < start < other_end) or (other_start == other_end and start < other_start < end)
            if overlap or same_insertion or inside:
                raise PatchConflict('Selected proposals contain conflicting alternatives for the same lines. Select one or request regeneration.')
    result, position = [], 0
    for start, end, replacement in ordered:
        result.extend(original[position:start])
        result.extend(replacement)
        position = end
    result.extend(original[position:])
    content = '\n'.join(result) + ('\n' if result else '')
    combined = make_unified_diff(before, content, path)
    if not combined:
        raise PatchConflict('The selected proposals make no content change.')
    return content, combined


def prepare_targets(cfg, members: list[dict]) -> list[dict]:
    """Project the selected frozen patches onto today's actual destinations."""
    groups = {}
    for member in members:
        dest = member['snapshot']['destination']
        key = _hash(destination_identity(dest))
        groups.setdefault(key, []).append(member)
    targets = []
    for key, group in sorted(groups.items()):
        dest = group[0]['snapshot']['destination']
        target = {'target_key': key, 'destination': dest, 'proposal_ids': sorted(m['proposal_id'] for m in group),
                  'state': 'ready', 'error_code': '', 'detail': '', 'diff_unified': '', 'before_content': '',
                  'before_hash': '', 'after_hash': '', 'before_exists': False, 'base_ref': '', 'budget': None}
        try:
            from .resolutions import validate_selection
            validate_selection(group)
            from .reapplications import validate_selection as validate_reapplications
            validate_reapplications(group)
            if len({m['snapshot']['destination']['target_kind'] for m in group}) != 1:
                raise PatchConflict('Selected proposals disagree about the target class.')
            base = read_destination(dest)
            target.update(before_content=base['content'], before_hash=base['content_hash'],
                          before_exists=base['exists'], base_ref=base['base_ref'])
            after, diff = combine_edits(base['content'], [m['snapshot']['proposal']['diff_unified'] for m in group], dest['target_path'])
            target.update(diff_unified=diff, after_hash=hashlib.sha256(after.encode()).hexdigest())
            if Path(cfg.global_claude_md).expanduser().resolve() == Path(dest['target_path']):
                limit = cfg.global_claude_md_line_budget
                target['budget'] = {'before': len(base['content'].splitlines()), 'after': len(after.splitlines()),
                                    'limit': limit, 'over': len(after.splitlines()) > limit}
        except CommandError as exc:
            target.update(state='conflict', error_code=exc.code, detail=str(exc))
        except PatchConflict as exc:
            target.update(state='conflict', error_code='ConflictingEdits', detail=str(exc))
        except (DestinationError, OSError, UnicodeError) as exc:
            target.update(state='unavailable', error_code='TargetUnavailable', detail=str(exc))
        targets.append(target)
    return targets


def preview_revision(members: list[dict], targets: list[dict]) -> str:
    return _hash({'members': [{'proposal_id': m['proposal_id'], 'revision': m['revision']} for m in members],
                  'targets': targets})


def preview_selection(store, cfg, proposal_ids: list[str]) -> dict:
    """Call inside one read transaction. Empty/oversized selections fail explicitly."""
    if not 1 <= len(proposal_ids) <= MAX_MEMBERS or len(set(proposal_ids)) != len(proposal_ids) or any(not p for p in proposal_ids):
        raise CommandError('InvalidMembers', f'Select 1–{MAX_MEMBERS} unique proposal IDs.')
    members = [review_snapshot(store, pid, cfg) for pid in sorted(proposal_ids)]
    targets = prepare_targets(cfg, members)
    return {'members': members, 'targets': targets, 'revision': preview_revision(members, targets),
            'ready': all(t['state'] == 'ready' for t in targets)}
