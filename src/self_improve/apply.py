"""Eval-gated apply with shadow snapshots and recoverable inverse rollback.

Every write to a real instruction file goes through this module and nowhere
else (AGENTS.md hard rule). The write protocol is:

1. Policy gate: only new ``gated_pass`` proposals in an explicitly enabled
   target class may be applied automatically. Mandatory human-review actions,
   prior rollbacks, and originating review-only runs cannot be overridden.
2. Snapshot "before" into the shadow git repo at ``cfg.state_path("snapshots")``.
3. Persist the exact source, policy revision, destination, and prepared
   before/after content and Git commit before changing an instruction target.
4. Patch:
   - non-git targets (global CLAUDE.md, ~/.codex/AGENTS.md, skills): read the
     file (missing -> ""), ``apply_unified_diff``, atomic tmp+rename write
     preserving the file mode;
   - targets inside a git repo: commit the patched content to the branch
     ``cfg.project_branch_name`` using plumbing under a TEMP index
     (``GIT_INDEX_FILE``) so the working tree, index, and current branch are
     never touched — asserted afterwards.
5. Snapshot "after" (for git targets: the prepared commit content).
6. Update the proposal row, operation result, and application event atomically.
   The separate worker reconciles interrupted automatic, approved, and inverse writes.

``PatchConflict`` marks the proposal ``held`` with the conflict detail and
writes nothing. ``rollback`` reverses the selected recorded contribution while
preserving unrelated current content. Its durable inverse checkpoint supports
file replacement/removal and Git ref recovery. A changed or ambiguous block
conflicts instead of restoring a whole old snapshot over later edits.

Fail-loud: every git subprocess call checks its returncode and raises
``GitError`` with stderr attached; expected-miss return codes (e.g. "ref does
not exist") are individually allow-listed per call, never blanket-ignored.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from self_improve.config import Config
from self_improve.store import AUTO_APPLY_STATUSES, Store, new_id, utc_now_iso

try:
    from self_improve.propose import PatchConflict, apply_unified_diff
except ImportError as exc:  # fail loud: apply is unusable without propose
    raise ImportError(
        "self_improve.apply requires self_improve.propose to export "
        "apply_unified_diff(content: str, diff: str) -> str and PatchConflict; "
        f"import failed: {exc}"
    ) from exc


class ApplyError(Exception):
    """Raised for apply/rollback protocol violations (never silently skipped)."""


class GitError(ApplyError):
    """Raised when a git subprocess exits with an unexpected returncode."""


# Proposal statuses that the policy gate lets through to an actual write.
#: Re-exported, NOT re-declared. `store.AUTO_APPLY_STATUSES` is the trust
#: model's own list (PRD S5.1) and this module decides what reaches the
#: operator's files, so a second copy could drift in the direction where a
#: rule gets written that the trust model never cleared.
APPLIABLE_STATUSES = AUTO_APPLY_STATUSES

# Commit identity for commits this module creates in TARGET repos (the shadow
# snapshots repo gets the same identity via `git config` at init). Passed via
# env so apply works even in repos with no user.name/email configured, and so
# self-improve authorship is visible in `git log`.
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "self-improve",
    "GIT_AUTHOR_EMAIL": "self-improve",
    "GIT_COMMITTER_NAME": "self-improve",
    "GIT_COMMITTER_EMAIL": "self-improve",
}

# Sentinel: snapshot() reads the target from disk unless content is given.
_READ_DISK = object()


# ----------------------------------------------------------------------
# git subprocess plumbing (fail loud)
# ----------------------------------------------------------------------


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    ok_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess:
    """Run git in cwd; raise GitError unless returncode is allow-listed.

    ``ok_returncodes`` beyond 0 are for calls where a specific nonzero code is
    an expected *outcome* (e.g. ``rev-parse --verify -q`` -> 1 for a missing
    ref); the caller must branch on ``proc.returncode`` explicitly.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        input=input_bytes,
        capture_output=True,
    )
    if proc.returncode not in ok_returncodes:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(
            f"git {' '.join(args)} in {cwd} failed with rc={proc.returncode}: {stderr}"
        )
    return proc


def _git_text(args: list[str], cwd: Path, **kwargs) -> str:
    """Run git and return stripped stdout decoded as UTF-8 (rc must be 0)."""
    return _run_git(args, cwd, **kwargs).stdout.decode("utf-8").strip()


# ----------------------------------------------------------------------
# shadow snapshots repo
# ----------------------------------------------------------------------


def snapshots_repo(cfg: Config) -> Path:
    """Return the shadow snapshot git repo path, initializing it on first use."""
    repo = cfg.state_path("snapshots")
    if not (repo / ".git").is_dir():
        repo.mkdir(parents=True, exist_ok=True)
        _run_git(["init", "-q"], repo)
        _run_git(["config", "user.name", "self-improve"], repo)
        _run_git(["config", "user.email", "self-improve"], repo)
    return repo


def mirror_rel_path(target: Path) -> str:
    """Mirror path of a target inside the snapshots repo.

    The absolute path minus its leading '/' — e.g.
    ``/Users/x/.claude/CLAUDE.md`` -> ``Users/x/.claude/CLAUDE.md``.
    Symlinks are NOT resolved: the path the system was asked to edit is the
    identity, per the lstat-first convention.
    """
    abspath = os.path.abspath(str(target))
    rel = abspath.lstrip("/")
    if not rel:
        raise ApplyError(f"refusing to mirror root path: {target!r}")
    return rel


def snapshot(cfg: Config, target: Path, label: str, *, content=_READ_DISK) -> str:
    """Commit the target's current content (or its absence) to the shadow repo.

    ``content``: by default read from disk (a missing file records absence);
    pass ``bytes`` to snapshot explicit content (used for git-branch targets,
    where the logical content lives at the branch tip, not the working tree),
    or ``None`` to record absence explicitly.

    Returns the snapshot commit sha. Commits are made even when nothing
    changed (``--allow-empty``) so every snapshot call yields an auditable sha.
    """
    repo = snapshots_repo(cfg)
    rel = mirror_rel_path(target)
    mirror_abs = repo / rel
    if content is _READ_DISK:
        target = Path(target)
        content = target.read_bytes() if target.exists() else None
    if content is None:
        if mirror_abs.exists():
            _run_git(["rm", "-q", "--", rel], repo)
    else:
        if not isinstance(content, bytes):
            raise ApplyError(
                f"snapshot content must be bytes or None, got {type(content).__name__}"
            )
        mirror_abs.parent.mkdir(parents=True, exist_ok=True)
        mirror_abs.write_bytes(content)
        _run_git(["add", "--", rel], repo)
    _run_git(
        ["commit", "-q", "--allow-empty", "-m", f"{label}: {target}"], repo
    )
    return _git_text(["rev-parse", "HEAD"], repo)


def snapshot_content(cfg: Config, commit_sha: str, target: Path) -> bytes | None:
    """Content of target's mirror at a snapshot commit; None if absent there."""
    repo = snapshots_repo(cfg)
    spec = f"{commit_sha}:{mirror_rel_path(target)}"
    probe = _run_git(["cat-file", "-e", spec], repo, ok_returncodes=(0, 1, 128))
    if probe.returncode != 0:
        return None
    return _run_git(["show", spec], repo).stdout


# ----------------------------------------------------------------------
# target classification
# ----------------------------------------------------------------------


def _detect_git_toplevel(cfg: Config, target: Path) -> Path | None:
    """Toplevel of the git repo containing target, or None for non-git targets.

    Detection: ``git -C <dir> rev-parse --show-toplevel`` succeeds AND the
    toplevel is not the shadow snapshots repo. <dir> is the deepest EXISTING
    ancestor of the target's parent (a brand-new file's directory may not
    exist yet). "not a git repository" (rc 128) is the expected miss; any
    other failure raises.
    """
    directory = Path(os.path.abspath(str(target))).parent
    while not directory.is_dir():
        if directory == directory.parent:
            break
        directory = directory.parent
    proc = _run_git(
        ["-C", str(directory), "rev-parse", "--show-toplevel"],
        Path.cwd(),
        ok_returncodes=(0, 128),
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        if "not a git repository" in stderr.lower():
            return None
        raise GitError(
            f"git rev-parse --show-toplevel in {directory} failed rc=128: {stderr.strip()}"
        )
    toplevel = Path(proc.stdout.decode("utf-8").strip())
    if toplevel.resolve() == snapshots_repo(cfg).resolve():
        return None
    return toplevel


# ----------------------------------------------------------------------
# writers
# ----------------------------------------------------------------------


def _atomic_write(target: Path, data: bytes) -> None:
    """Write bytes via tmp+rename in the target's directory, preserving mode.

    An existing file keeps its exact mode bits; a newly created file gets
    0o644 (mkstemp's 0o600 would surprise for shared instruction files —
    deliberate choice, surfaced here).
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o7777 if target.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".self-improve.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class _WorktreeState:
    """Observable state that a git-branch apply must leave untouched."""

    head_sha: str
    head_ref: str            # symbolic ref, or "(detached)"
    file_bytes: bytes | None  # working-tree content of the target; None if absent


#: HEAD in a repo that has no commits yet. Constant rather than per-call so
#: the before/after equality that proves the working tree was untouched would
#: still hold. That is DEFENSIVE, not load-bearing, and no test covers it: a
#: repo with no commits raises in `_branch_base` before any comparison
#: happens. Sabotaging the constant to vary per call left the suite green,
#: which is the honest state of it.
NO_COMMITS = "(no commits)"


def _capture_worktree(toplevel: Path, target: Path) -> _WorktreeState:
    # `rev-parse HEAD` FAILS in a repo with no commits, and this function runs
    # before _branch_base — so `git init` and nothing else died here with a
    # raw git error, and the clear "target repo has no commits; cannot base
    # branch" message a few lines further on could never be reached. A guard
    # that reads well and never fires is worse than no guard: it makes the
    # error look like a bug in git.
    head = _run_git(
        ["rev-parse", "--verify", "-q", "HEAD"], toplevel, ok_returncodes=(0, 1)
    )
    head_sha = (
        head.stdout.decode("utf-8").strip() if head.returncode == 0 else NO_COMMITS
    )
    proc = _run_git(["symbolic-ref", "-q", "HEAD"], toplevel, ok_returncodes=(0, 1))
    head_ref = proc.stdout.decode("utf-8").strip() if proc.returncode == 0 else "(detached)"
    target = Path(target)
    file_bytes = target.read_bytes() if target.exists() else None
    return _WorktreeState(head_sha=head_sha, head_ref=head_ref, file_bytes=file_bytes)


def _assert_worktree_untouched(
    before: _WorktreeState, after: _WorktreeState, target: Path
) -> None:
    if before != after:
        raise ApplyError(
            f"git-branch apply modified the working tree or HEAD for {target}: "
            f"before={before!r} after={after!r}"
        )


def _branch_base(toplevel: Path, branch: str) -> tuple[str, bool]:
    """(base commit sha, branch_exists) for a branch-targeted commit.

    Base is the branch tip if the branch exists, else HEAD. A repo with no
    commits at all cannot take a branch commit and raises.
    """
    ref = f"refs/heads/{branch}"
    proc = _run_git(
        ["rev-parse", "--verify", "-q", ref], toplevel, ok_returncodes=(0, 1)
    )
    if proc.returncode == 0:
        return proc.stdout.decode("utf-8").strip(), True
    head = _run_git(["rev-parse", "--verify", "-q", "HEAD"], toplevel, ok_returncodes=(0, 1))
    if head.returncode != 0:
        raise ApplyError(
            f"target repo {toplevel} has no commits; cannot base branch {branch!r}"
        )
    return head.stdout.decode("utf-8").strip(), False


def _content_at(toplevel: Path, commit: str, relpath: str) -> bytes | None:
    """File content at <commit>:<relpath>, or None if the path is absent there."""
    spec = f"{commit}:{relpath}"
    probe = _run_git(["cat-file", "-e", spec], toplevel, ok_returncodes=(0, 1, 128))
    if probe.returncode != 0:
        return None
    return _run_git(["show", spec], toplevel).stdout


def _refuse_checked_out_branch(toplevel, branch):
    listing = _git_text(['worktree', 'list', '--porcelain'], toplevel)
    if 'branch refs/heads/' + branch in listing.splitlines():
        raise DeliveryBlocked('BranchCheckedOut',
            f"Delivery branch {branch!r} is checked out in a working copy. Switch that copy to another branch before retrying.")


def _prepare_branch_commit(
    toplevel: Path,
    branch: str,
    base: str,
    branch_exists: bool,
    relpath: str,
    data: bytes | None,
    message: str,
) -> str:
    """Prepare an immutable commit object without changing refs/heads/<branch>.

    Pure plumbing under a TEMP index (GIT_INDEX_FILE): the real index, the
    working tree, and the current branch are never touched. ``data=None``
    removes the path (used by rollback of a file the branch created).
    The caller records this object before publishing its ref with compare-and-swap.
    """
    _refuse_checked_out_branch(toplevel, branch)
    entry = _git_text(['ls-tree', '-z', base, '--', ':(literal)' + relpath], toplevel)
    mode = entry.split(' ', 1)[0] if entry else '100644'
    if mode not in {'100644', '100755'}:
        raise ApplyError(f'target {relpath} is not a regular file in the delivery branch')
    env = {**os.environ, **_GIT_IDENTITY_ENV}
    with tempfile.TemporaryDirectory(prefix="self-improve-index-") as tmpdir:
        env["GIT_INDEX_FILE"] = str(Path(tmpdir) / "index")
        _run_git(["read-tree", base], toplevel, env=env)
        if data is None:
            _run_git(["update-index", "--force-remove", "--", relpath], toplevel, env=env)
        else:
            blob = _git_text(
                ["hash-object", "-w", "--stdin"], toplevel, input_bytes=data, env=env
            )
            _run_git(
                ["update-index", "--add", "--cacheinfo", f"{mode},{blob},{relpath}"],
                toplevel,
                env=env,
            )
        tree = _git_text(["write-tree"], toplevel, env=env)
        commit = _git_text(
            ["commit-tree", tree, "-p", base, "-m", message], toplevel, env=env
        )
    return commit


def _commit_to_branch(toplevel, branch, base, branch_exists, relpath, data, message):
    commit = _prepare_branch_commit(toplevel, branch, base, branch_exists, relpath, data, message)
    ref = f"refs/heads/{branch}"
    _refuse_checked_out_branch(toplevel, branch)
    _run_git(["update-ref", ref, commit, base if branch_exists else ""], toplevel)
    return commit


# ----------------------------------------------------------------------
# ledger helpers
# ----------------------------------------------------------------------


def _record_event(store: Store, proposal_id: str, event: str, actor: str, note: str) -> None:
    store.insert(
        "proposal_events",
        {
            "id": new_id(),
            "proposal_id": proposal_id,
            "ts": utc_now_iso(),
            "event": event,
            "actor": actor,
            "note": note,
        },
    )


def _hold(
    store: Store,
    proposal: dict,
    reason_code: str,
    detail: str,
    *,
    snapshot_before: str = "",
) -> dict:
    """Mark a proposal held (with taxonomy-coded reason) and return the outcome."""
    updates: dict = {"status": "held"}
    if snapshot_before:
        # A before-snapshot taken prior to a conflict is still a real audit
        # artifact; record it rather than orphaning the sha.
        updates["snapshot_commit_before"] = snapshot_before
    from .store import DECIDED_STATUSES

    # Refusing execution must not undo a person's decision or an application.
    if proposal["status"] not in DECIDED_STATUSES:
        store.update("proposals", "id", proposal["id"], updates)
        _record_event(store, proposal["id"], "held", "auto", f"{reason_code}: {detail}")
    return {
        "proposal_id": proposal["id"],
        "outcome": "held",
        "reason": reason_code,
        "detail": detail,
        "target_path": proposal["target_path"],
        "mode": "",
        "snapshot_commit_before": snapshot_before,
        "snapshot_commit_after": "",
        "branch_commit": "",
    }


# ----------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------


def _refuse_unreconciled_delivery(store, cfg, target_path, *, destination=None, except_target='', except_operation=''):
    from .commands import COMMAND_MIGRATION, _parsed
    from .delivery_records import DeliveryRecordError, validated_cancellation
    from .destinations import resolve_destination
    if not store.query_one('SELECT name FROM schema_migrations WHERE name=?', (COMMAND_MIGRATION,)):
        return
    pending = []
    for row in store.query("SELECT * FROM command_targets WHERE state!='completed'"):
        checkpoint = _parsed(row, 'checkpoint_json')
        try:
            cancelled = validated_cancellation(checkpoint, row['state'])
        except DeliveryRecordError as exc:
            raise DeliveryBlocked('InvalidCheckpoint', f"Target {row['id']}: {exc}") from exc
        if row['id'] != except_target and 'delivery' in checkpoint and not cancelled:
            pending.append(row)
    from .operations import operations_available, operation_status
    if operations_available(store):
        for row in store.query("SELECT id FROM instruction_operations WHERE state!='completed'"):
            operation = operation_status(store, row['id'])
            if operation['id'] == except_operation or 'delivery' not in operation['checkpoint']:
                continue
            if operation['state'] == 'cancelled' and validated_cancellation(operation['checkpoint'], operation['state']):
                continue
            pending.append({'id': operation['id'], 'destination_json': json.dumps(operation['record']['destination'])})
    if not pending:
        return
    destination = destination or resolve_destination(cfg, target_path, 'global_claude_md')
    def resource(dest):
        if dest['mode'] == 'git_branch':
            return ('git_branch', dest['git_common_dir'], dest['branch_name'])
        return ('file', dest['target_path'])
    for row in pending:
        if resource(_parsed(row, 'destination_json')) == resource(destination):
            raise DeliveryBlocked('ReconciliationRequired',
                f"delivery target {row['id']} needs worker reconciliation before another instruction write")


def _operation_checkpoint(stage, operation_id):
    """Fault boundary after a durable operation record and around its write."""


def _execution_store(store, cfg):
    if store.db_path.resolve() != cfg.state_path('state.db').resolve():
        raise DeliveryBlocked('StoreMismatch', 'Instruction execution requires the configured state database, not a redirected copy.')
    if store.conn.in_transaction:
        raise ApplyError('Instruction execution requires a Store with no pending transaction; commit the source work explicitly first.')


def apply_proposal(store: Store, cfg: Config, proposal: dict) -> dict:
    """Apply automatically after durable preparation; repeated calls reuse it."""
    from .delivery_lock import instruction_write_lock
    from .operations import require_operations, operation_status, SOURCE_FIELDS
    from .commands import _json, _hash
    from .execution_policy import automatic_permission, policy_snapshot
    from .destinations import resolve_destination, read_destination
    _execution_store(store, cfg)
    with instruction_write_lock(cfg):
        require_operations(store)
        existing = store.query_one('SELECT id FROM instruction_operations WHERE operation_key=?', ('auto_apply:' + proposal['id'],))
        if existing:
            operation = operation_status(store, existing['id'])
            source = operation['record']['proposal']
            if any(proposal.get(k, '') != source[k] for k in SOURCE_FIELDS if k != 'status'):
                raise ApplyError(f"proposal {proposal['id']} changed since operation preparation")
            if operation['state'] == 'completed':
                current = store.query_one('SELECT * FROM proposals WHERE id=?', (source['id'],))
                if current is None or any(current[k] != source[k] for k in SOURCE_FIELDS if k != 'status'):
                    raise ApplyError(f"proposal {proposal['id']} changed after application")
                if current['status'] != 'applied':
                    if proposal.get('status') != current['status']:
                        raise ApplyError(f"proposal {proposal['id']} changed after application")
                    with store.transaction(write=True):
                        return _hold(store, current, 'previous_operation_complete',
                                     'This proposal already had an application. Reapplication requires a new reviewed command.')
            return _automatic_outcome(_resume_write_operation(store, cfg, existing['id']))
        with store.transaction(write=True):
            current = store.query_one('SELECT * FROM proposals WHERE id=?', (proposal['id'],))
            if current is None or any(proposal.get(k, '') != current[k] for k in SOURCE_FIELDS):
                raise ApplyError(f"proposal {proposal['id']} changed or is missing; reload before attempting execution")
            decision = automatic_permission(store, cfg, current)
            learning = store.query_one('SELECT status FROM learnings WHERE id=?', (current['learning_id'],))
            if not decision['allowed']:
                return _hold(store, current, decision['reason'], decision['detail'])
            if learning is None or learning['status'] == 'rejected':
                return _hold(store, current, 'lesson_rejected', 'This lesson is missing or rejected.')
            target = Path(current['target_path'])
            # Retain the existing refusal for symlinked targets and uncommitted
            # repositories before read-only destination resolution normalizes them.
            root = _detect_git_toplevel(cfg, target)
            if target.resolve() != target:
                raise ApplyError(f'target {target} escapes its git toplevel or follows a symlink')
            if root is not None:
                _branch_base(root, cfg.project_branch_name)
            destination = resolve_destination(cfg, str(target), current['target_kind'])
            _validate_recorded_destination(cfg, destination)
            _refuse_unreconciled_delivery(store, cfg, str(target), destination=destination)
            base = read_destination(destination)
            try:
                apply_unified_diff(base['content'], current['diff_unified'])
            except PatchConflict as exc:
                before = snapshot(cfg, target, 'before', content=base['content'].encode('utf-8') if base['exists'] else None)
                return _hold(store, current, 'patch_conflict', str(exc), snapshot_before=before)
            oid = new_id()
            checkpoint = _prepare_write_checkpoint(cfg, destination, base, current['diff_unified'],
                         f"self-improve: automatic proposal {current['id']} operation {oid}")
            policy = policy_snapshot(store)['classes'][decision['target_class']]
            record = {'version': 1, 'kind': 'auto_apply', 'proposal': current, 'destination': destination,
                      'authorization': {'actor': 'auto', 'target_class': decision['target_class'], 'policy_revision': policy['revision']}}
            stamp = utc_now_iso()
            store.insert('instruction_operations', {'id': oid, 'operation_key': 'auto_apply:' + current['id'],
                         'kind': 'auto_apply', 'proposal_id': current['id'], 'record_json': _json(record),
                         'record_hash': _hash(record), 'checkpoint_json': _json(checkpoint), 'state': 'running',
                         'created_at': stamp, 'updated_at': stamp})
        _operation_checkpoint('prepared', oid)
        return _automatic_outcome(_resume_write_operation(store, cfg, oid))


def _automatic_outcome(operation):
    if operation['state'] in {'blocked', 'failed'}:
        raise DeliveryBlocked(operation['error_code'], operation['error_detail'])
    return operation['result']


def resume_write_operation(store, cfg, operation_id):
    """Explicitly retry or reconcile one operation, reusing its frozen record."""
    from .delivery_lock import instruction_write_lock
    _execution_store(store, cfg)
    with instruction_write_lock(cfg):
        return _resume_write_operation(store, cfg, operation_id)


def _resume_write_operation(store, cfg, operation_id):
    from .commands import _json
    from .operations import operation_status, SOURCE_FIELDS
    from .execution_policy import automatic_permission, policy_snapshot
    from .destinations import DestinationError
    operation = operation_status(store, operation_id)
    if operation['state'] in {'completed', 'cancelled'}:
        return operation
    if operation['kind'] == 'rollback':
        return _resume_rollback_operation(store, cfg, operation)
    record, checkpoint = operation['record'], operation['checkpoint']
    source, destination = record['proposal'], record['destination']
    target = {'destination': destination, 'diff_unified': source['diff_unified']}
    with store.transaction(write=True):
        store.update('instruction_operations', 'id', operation_id, {'state': 'running',
                     'attempts': operation['attempts'] + 1, 'updated_at': utc_now_iso()})
    try:
        delivery = _check_delivery_checkpoint(cfg, target, checkpoint)
        _validate_recorded_destination(cfg, destination)
        _refuse_unreconciled_delivery(store, cfg, destination['target_path'], destination=destination, except_operation=operation_id)
        with store.transaction(write=True):
            delivered = _observe_prepared_write(destination, delivery)
            current = store.query_one('SELECT * FROM proposals WHERE id=?', (source['id'],))
            unchanged = current is not None and all(current[k] == source[k] for k in SOURCE_FIELDS)
            if not delivered:
                if _operation_cancellation_requested(store, operation_id):
                    _cancel_instruction_operation(store, operation, checkpoint)
                    return operation_status(store, operation_id)
                learning = store.query_one('SELECT status FROM learnings WHERE id=?', (source['learning_id'],))
                permission = automatic_permission(store, cfg, current) if unchanged else {'allowed': False, 'reason': 'revision_changed', 'detail': 'The proposal changed after preparation.'}
                if learning is None or learning['status'] == 'rejected':
                    permission = {'allowed': False, 'reason': 'lesson_rejected', 'detail': 'This lesson is missing or rejected.'}
                policy = policy_snapshot(store)['classes'][record['authorization']['target_class']]
                if permission['allowed'] and policy['revision'] != record['authorization']['policy_revision']:
                    permission = {'allowed': False, 'reason': 'policy_changed', 'detail': 'The automatic policy changed after preparation.'}
                if not permission['allowed']:
                    result = _hold(store, current, permission['reason'], permission['detail']) if unchanged else {
                        'proposal_id': source['id'], 'outcome': 'held', 'reason': permission['reason'], 'detail': permission['detail']}
                    checkpoint['cancellation'] = {'no_delivery_observed': True, 'at': utc_now_iso()}
                    store.update('instruction_operations', 'id', operation_id, {'state': 'cancelled', 'checkpoint_json': _json(checkpoint),
                                 'result_json': _json(result), 'error_code': '', 'error_detail': '', 'updated_at': utc_now_iso()})
                    return operation_status(store, operation_id)
                _publish_prepared_write(destination, delivery, operation_id, _operation_checkpoint)
            after = snapshot(cfg, Path(destination['target_path']), 'after', content=delivery['after_content'].encode('utf-8'))
            stamp = utc_now_iso()
            result = {'proposal_id': source['id'], 'outcome': 'applied', 'reason': '', 'detail': '',
                      'target_path': destination['target_path'], 'mode': destination['mode'],
                      'snapshot_commit_before': delivery['snapshot_before'], 'snapshot_commit_after': after,
                      'branch_commit': delivery['branch_commit'], 'operation_id': operation_id,
                      'proposal_status_preserved': not unchanged}
            checkpoint['result'] = {'snapshot_after': after, 'branch_commit': delivery['branch_commit'], 'mode': destination['mode'], 'completed_at': stamp}
            _operation_checkpoint('before_ack', operation_id)
            if unchanged:
                store.update('proposals', 'id', source['id'], {'status': 'applied', 'applied_at': stamp,
                             'snapshot_commit_before': delivery['snapshot_before'], 'snapshot_commit_after': after})
            _record_event(store, source['id'], 'applied', 'auto', _json({'operation_id': operation_id,
                'mode': destination['mode'], 'branch': destination['branch_name'], 'base': delivery['base_ref'],
                'branch_commit': delivery['branch_commit'], 'snapshot_before': delivery['snapshot_before'],
                'snapshot_after': after, 'proposal_status_preserved': not unchanged}))
            store.update('instruction_operations', 'id', operation_id, {'state': 'completed', 'checkpoint_json': _json(checkpoint),
                         'result_json': _json(result), 'error_code': '', 'error_detail': '', 'updated_at': stamp})
    except (DeliveryBlocked, PatchConflict, DestinationError, OSError, UnicodeError, GitError) as exc:
        _record_instruction_failure(store, operation, exc)
    return operation_status(store, operation_id)


def _operation_cancellation_requested(store, operation_id):
    row = store.query_one('SELECT * FROM instruction_operations WHERE id=?', (operation_id,))
    return bool(row and row.get('cancel_requested', 0))


def _cancel_instruction_operation(store, operation, checkpoint):
    """Inside the writer transaction, after proving no target write occurred."""
    from .commands import _json
    from .operations import SOURCE_FIELDS
    source = operation['record']['proposal']
    result = {'proposal_id': source['id'], 'outcome': 'held', 'reason': 'cancelled_by_user',
              'detail': 'The operator cancelled this unwritten instruction operation.'}
    if operation['kind'] == 'auto_apply':
        current = store.query_one('SELECT * FROM proposals WHERE id=?', (source['id'],))
        if current and all(current[k] == source[k] for k in SOURCE_FIELDS):
            result = _hold(store, current, result['reason'], result['detail'])
    stamp = utc_now_iso()
    checkpoint['cancellation'] = {'no_delivery_observed': True, 'at': stamp}
    store.update('instruction_operations', 'id', operation['id'], {'state': 'cancelled',
                 'checkpoint_json': _json(checkpoint), 'result_json': _json(result),
                 'error_code': '', 'error_detail': '', 'updated_at': stamp})


def _record_instruction_failure(store, operation, exc):
    from .commands import _json
    from .destinations import DestinationError
    state = 'blocked' if isinstance(exc, (DeliveryBlocked, PatchConflict, DestinationError)) else 'failed'
    code = exc.code if isinstance(exc, DeliveryBlocked) else type(exc).__name__
    failures = operation['failures'] + [{'at': utc_now_iso(), 'attempt': operation['attempts'] + 1, 'state': state,
                                       'error_code': code, 'error_detail': str(exc)}]
    with store.transaction(write=True):
        # A new cancellation can arrive outside a write transaction during this
        # attempt. Leave one reconciliation pass claimable instead of losing it.
        pending_control = not operation['cancel_requested'] and _operation_cancellation_requested(store, operation['id'])
        store.update('instruction_operations', 'id', operation['id'], {'state': 'queued' if pending_control else state,
                     'error_code': code, 'error_detail': str(exc), 'failures_json': _json(failures), 'updated_at': utc_now_iso()})


def rollback(store: Store, cfg: Config, proposal_id: str, actor: str = "user") -> dict:
    """Record and execute one inverse operation; the web records the same intent."""
    from .delivery_lock import instruction_write_lock
    from .operations import require_operations
    from .rollback import application_source, committed_or_prepared_rollback, rollback_preview, create_rollback_operation
    from .commands import CommandError
    if actor not in {'user', 'auto'}:
        raise ApplyError('rollback actor must be user or auto')
    _execution_store(store, cfg)
    with instruction_write_lock(cfg):
        require_operations(store)
        try:
            with store.transaction(write=True):
                source = application_source(store, cfg, proposal_id)
                from .resolutions import completed
                resolution=completed(store,source)
                if resolution:return resolution
                existing = committed_or_prepared_rollback(store,source)
                oid = existing['id'] if existing else create_rollback_operation(store, rollback_preview(store,cfg,proposal_id), actor=actor)
            return _automatic_outcome(_resume_write_operation(store, cfg, oid))
        except CommandError as exc:
            raise DeliveryBlocked(exc.code, str(exc)) from exc


def _rollback_applications(store, source, *, require_unchanged):
    from .rollback import latest_application
    latest = {member['proposal_id']: latest_application(store, member['proposal_id'])
              for member in source['affected_members']}
    if require_unchanged:
        for member in source['affected_members']:
            event = latest[member['proposal_id']]
            if event is None or event['id'] != member['applied_event_id']:
                raise DeliveryBlocked('ApplicationChanged', 'A selected proposal has a newer application. Review that revision before rollback.')
    return latest


def _resume_rollback_operation(store, cfg, operation):
    from .commands import _json
    from .operations import operation_status
    from .destinations import DestinationError
    oid, record, checkpoint = operation['id'], operation['record'], operation['checkpoint']
    preview = record['rollback']; source = preview['source']; dest = record['destination']
    target = {'destination': dest, 'diff_unified': preview['diff_unified']}
    with store.transaction(write=True):
        store.update('instruction_operations', 'id', oid, {'state': 'running', 'attempts': operation['attempts']+1, 'updated_at': utc_now_iso()})
    try:
        if 'delivery' not in checkpoint:
            from .destinations import read_destination
            with store.transaction(write=True):
                if _operation_cancellation_requested(store, oid):
                    _cancel_instruction_operation(store, operation, checkpoint)
                    return operation_status(store, oid)
            _validate_recorded_destination(cfg, dest)
            _refuse_unreconciled_delivery(store, cfg, dest['target_path'], destination=dest, except_operation=oid)
            with store.transaction(write=True):
                _rollback_applications(store, source, require_unchanged=True)
                base = read_destination(dest)
                if base['content_hash'] != preview['base']['content_hash'] or base['exists'] != preview['base']['exists']:
                    raise DeliveryBlocked('TargetChanged', 'The target changed after the rollback preview. Review a new inverse change.')
                checkpoint = _prepare_write_checkpoint(cfg,dest,base,preview['diff_unified'],
                    f"self-improve: rollback {source['application_id']} operation {oid}",
                    after_content=preview['after_content'],after_exists=preview['after_exists'])
                store.update('instruction_operations','id',oid,{'checkpoint_json':_json(checkpoint)})
            _operation_checkpoint('prepared',oid)
        delivery = _check_delivery_checkpoint(cfg, target, checkpoint, inverse_source=source)
        _validate_recorded_destination(cfg, dest)
        _refuse_unreconciled_delivery(store, cfg, dest['target_path'], destination=dest, except_operation=oid)
        with store.transaction(write=True):
            delivered = _observe_prepared_write(dest, delivery)
            if not delivered and _operation_cancellation_requested(store, oid):
                _cancel_instruction_operation(store, operation, checkpoint)
                return operation_status(store, oid)
            latest = _rollback_applications(store, source, require_unchanged=not delivered)
            if not delivered:
                _publish_prepared_write(dest, delivery, oid, _operation_checkpoint)
            sha = snapshot(cfg, Path(dest['target_path']), 'rollback', content=_delivery_after_bytes(delivery))
            stamp = utc_now_iso()
            affected = [m['proposal_id'] for m in source['affected_members']]
            result = {'proposal_id': operation['proposal_id'], 'outcome': 'rolled_back', 'reason': '', 'detail': '',
                      'target_path': dest['target_path'], 'mode': dest['mode'], 'snapshot_commit_before': source['snapshot_before'],
                      'snapshot_commit_rollback': sha, 'branch_commit': delivery['branch_commit'],
                      'restored_absent': not delivery['after_exists'], 'affected_proposal_ids': affected,
                      'application_id': source['application_id'], 'operation_id': oid}
            checkpoint['result'] = {'snapshot_after': sha, 'branch_commit': delivery['branch_commit'], 'mode': dest['mode'], 'completed_at': stamp}
            _operation_checkpoint('before_ack', oid)
            for member in source['affected_members']:
                pid = member['proposal_id']; event = latest[pid]
                current = store.query_one('SELECT status FROM proposals WHERE id=?', (pid,))
                if event and event['id'] == member['applied_event_id'] and current['status'] == 'applied':
                    store.update('proposals', 'id', pid, {'status': 'rolled_back'})
                _record_event(store, pid, 'rolled_back', record['authorization']['actor'], _json({
                    'operation_id': oid, 'application_id': source['application_id'], 'applied_event_id': member['applied_event_id'],
                    'mode': dest['mode'], 'branch': dest['branch_name'], 'branch_commit': delivery['branch_commit'],
                    'restored_from': source['snapshot_before'], 'restored_absent': result['restored_absent'],
                    'snapshot_rollback': sha, 'affected_proposal_ids': affected}))
            store.update('instruction_operations', 'id', oid, {'state': 'completed', 'checkpoint_json': _json(checkpoint),
                         'result_json': _json(result), 'error_code': '', 'error_detail': '', 'updated_at': stamp})
    except (DeliveryBlocked, PatchConflict, DestinationError, OSError, UnicodeError, GitError) as exc:
        _record_instruction_failure(store, operation, exc)
    return operation_status(store, oid)


# ----------------------------------------------------------------------
# Revision-bound command delivery. No model calls run in this write lane.
# ----------------------------------------------------------------------

class DeliveryBlocked(ApplyError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def _delivery_checkpoint(stage, target_id):
    """Fault-injection boundary: persistent preparation precedes any target mutation."""


def _validate_recorded_destination(cfg, destination):
    from dataclasses import replace
    from .destinations import resolve_destination, destination_identity
    from .routing import is_never_write
    target = Path(destination['target_path'])
    if (target.resolve() != target or any(is_never_write(str(p)) for p in (target, *target.parents))
            or target.is_relative_to(Path(cfg.production_repo_path).expanduser().resolve())):
        raise DeliveryBlocked('DestinationChanged', 'The recorded path changed or is a prohibited production checkout. Review a corrected target.')
    fixed = replace(cfg, project_branch_name=destination['branch_name']) if destination['mode'] == 'git_branch' else cfg
    actual = resolve_destination(fixed, str(target), destination['target_kind'])
    if destination_identity(actual) != destination_identity(destination):
        raise DeliveryBlocked('DestinationChanged', 'The recorded destination identity is no longer available. No alternative checkout or delivery mode was selected.')


def _authorized_members(store, cfg, command_id, target_id, *, delivered=False):
    from .commands import command_status, load_revision
    command = command_status(store, command_id)
    if command['action'] != 'approve' or command['actor'] != 'user' or command['state'] != 'running':
        raise DeliveryBlocked('AuthorizationRevoked', 'This command no longer authorizes instruction delivery.')
    members = store.query('SELECT * FROM command_members WHERE target_id=? ORDER BY proposal_id', (target_id,))
    if not members:
        raise DeliveryBlocked('MissingAuthorization', 'The target has no reviewed members.')
    for member in members:
        reviewed=load_revision(store, member['revision_id'])['snapshot']
        frozen=reviewed['proposal']
        if not delivered and reviewed.get('recovery'):
            from .commands import CommandError
            try:
                from .recovery_jobs import origin
                live=store.query_one('SELECT * FROM proposals WHERE id=?',(member['proposal_id'],))
                if origin(store,live)!=reviewed['recovery']:
                    raise DeliveryBlocked('RecoveryChanged','The recovery source changed after approval.')
            except CommandError as exc:
                raise DeliveryBlocked(exc.code,str(exc)) from exc
        if not delivered and reviewed.get('reapplication'):
            from .commands import CommandError
            try:
                from .reapplications import ensure_pending,origin
                ensure_pending(store,cfg,reviewed['reapplication'])
                live=store.query_one('SELECT * FROM proposals WHERE id=?',(member['proposal_id'],))
                if origin(store,live)!=reviewed['reapplication']:
                    raise DeliveryBlocked('ReapplicationChanged','The reapplication origin changed after approval.')
            except CommandError as exc:
                raise DeliveryBlocked(exc.code,str(exc)) from exc
        if not delivered and reviewed.get('resolution'):
            from .commands import CommandError
            try:
                from .resolutions import ensure_pending,origin
                ensure_pending(store,reviewed['resolution'])
                live=store.query_one('SELECT * FROM proposals WHERE id=?',(member['proposal_id'],))
                if origin(store,live)!=reviewed['resolution']:
                    raise DeliveryBlocked('ResolutionChanged','The resolution origin changed after approval.')
            except CommandError as exc:
                raise DeliveryBlocked(exc.code,str(exc)) from exc
        if delivered:
            continue  # The frozen edit is already observed; preserve later decisions at acknowledgement.
        current = store.query_one('SELECT * FROM proposals WHERE id=?', (member['proposal_id'],))
        learning = store.query_one('SELECT status FROM learnings WHERE id=?', (frozen['learning_id'],))
        if current is None or current['status'] != 'approved_user' or learning is None or (learning['status'] == 'rejected' and not reviewed.get('resolution')):
            raise DeliveryBlocked('AuthorizationRevoked', f"Proposal {member['proposal_id']} is no longer approved for delivery.")
        fields = ('learning_id', 'target_path', 'target_kind', 'action', 'diff_unified')
        if any(current[k] != frozen[k] for k in fields):
            raise DeliveryBlocked('RevisionChanged', f"Proposal {member['proposal_id']} changed after approval. Review the new content.")
        from .rejections import rejection_reason
        rejection = rejection_reason(store, cfg, current)
        if rejection:
            raise DeliveryBlocked(rejection['code'], rejection['detail'])
    return members


def _target_rejection(store, cfg, target_id):
    from .commands import load_revision
    from .rejections import context, rejection_reason
    ctx = context(store)
    for m in store.query('SELECT * FROM command_members WHERE target_id=?', (target_id,)):
        snapshot = load_revision(store, m['revision_id'])['snapshot']
        reason = rejection_reason(store, cfg, snapshot['proposal'], ctx=ctx,
                                  learning=snapshot['learning'], destination=snapshot['destination'])
        if reason:
            return reason
    return None


def _cancel_rejected_target(store, command_id, target, checkpoint, reason):
    from .commands import _json
    checkpoint['suppression'] = reason
    _cancel_unwritten_target(store, command_id, target, checkpoint)
    store.update('command_targets', 'id', target['id'], {'checkpoint_json':_json(checkpoint),
                 'error_code':reason['code'], 'error_detail':reason['detail']})


def _delivery_hash(content):
    import hashlib
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _cancellation_requested(store, command_id):
    row = store.query_one('SELECT * FROM commands WHERE id=?', (command_id,))
    return bool(row and row.get('cancel_requested', 0))


def _cancel_unwritten_target(store, command_id, target, checkpoint):
    """Called in the worker transaction after no current delivery was observed."""
    from .commands import _json, load_revision
    from .store import DECIDED_STATUSES, actor_for
    stamp = utc_now_iso()
    checkpoint['cancellation'] = {'no_delivery_observed': True, 'at': stamp}
    store.update('command_targets', 'id', target['id'], {'state': 'cancelled', 'checkpoint_json': _json(checkpoint),
                 'error_code': '', 'error_detail': ''})
    for member in store.query('SELECT * FROM command_members WHERE target_id=?', (target['id'],)):
        current = store.query_one('SELECT * FROM proposals WHERE id=?', (member['proposal_id'],))
        if current is None or current['status'] != 'approved_user':
            continue  # Never replace a rejection or another later decision.
        frozen = load_revision(store, member['revision_id'])['snapshot']['proposal']
        original = frozen['status']
        status = original if original not in DECIDED_STATUSES else 'pending'
        store.update('proposals', 'id', member['proposal_id'], {'status': status})
        _record_event(store, member['proposal_id'], 'approval_cancelled', actor_for('approval_cancelled'),
                      _json({'command_id': command_id, 'target_id': target['id'], 'returned_to_review': True}))


def _prepare_command_delivery(store, cfg, command_id, target):
    from .commands import _json
    from .destinations import read_destination
    destination = target['destination']
    checkpoint = target['checkpoint']
    _validate_recorded_destination(cfg, destination)
    _refuse_unreconciled_delivery(store, cfg, destination['target_path'], destination=destination, except_target=target['id'])
    with store.transaction(write=True):
        if _cancellation_requested(store, command_id):
            _cancel_unwritten_target(store, command_id, target, checkpoint)
            return None
        rejection = _target_rejection(store, cfg, target['id'])
        if rejection:
            _cancel_rejected_target(store, command_id, target, checkpoint, rejection)
            return None
        _authorized_members(store, cfg, command_id, target['id'])
        preview = checkpoint.get('review_preview')
        if not preview:
            raise DeliveryBlocked('FreshReviewRequired', 'This historical approval has no reviewed target base. Cancel its unwritten work and approve a fresh complete preview.')
        base = read_destination(destination)
        if base['content_hash'] != preview['before_hash'] or base['exists'] != preview['before_exists']:
            raise DeliveryBlocked('TargetChanged', 'The target content changed after approval. Review a new preview before delivery.')
        prepared = _prepare_write_checkpoint(cfg, destination, base, target['diff_unified'],
                   f"self-improve: approved command {command_id} target {target['id']}")
        delivery = prepared['delivery']
        if delivery['after_hash'] != preview['after_hash']:
            raise DeliveryBlocked('PreviewChanged', 'The prepared edit differs from the approved preview.')
        checkpoint.update(prepared)
        store.update('command_targets', 'id', target['id'], {'state': 'running', 'checkpoint_json': _json(checkpoint)})
    _delivery_checkpoint('prepared', target['id'])
    if delivery['branch_commit']:
        _delivery_checkpoint('commit_prepared', target['id'])
    return checkpoint


def _check_delivery_checkpoint(cfg, target, checkpoint, *, inverse_source=None):
    from .delivery_records import DeliveryRecordError, validated_delivery
    try:
        delivery = validated_delivery(checkpoint, target['diff_unified'], inverse_source=inverse_source)
    except DeliveryRecordError as exc:
        raise DeliveryBlocked('InvalidCheckpoint', str(exc)) from exc
    kind = _git_text(['cat-file', '-t', delivery['snapshot_before']], snapshots_repo(cfg))
    before_bytes = snapshot_content(cfg, delivery['snapshot_before'], Path(target['destination']['target_path']))
    expected = delivery['before_content'].encode('utf-8') if delivery['before_exists'] else None
    if kind != 'commit' or before_bytes != expected:
        raise DeliveryBlocked('InvalidCheckpoint', 'The execution checkpoint does not match its before snapshot.')
    if target['destination']['mode'] == 'git_branch':
        root = Path(target['destination']['repo_root'])
        commit = delivery['branch_commit']
        parents = _git_text(['rev-list', '--parents', '-n', '1', commit], root).split()
        content = _content_at(root, commit, target['destination']['relative_path'])
        if parents != [commit, delivery['base_ref']] or content != _delivery_after_bytes(delivery):
            raise DeliveryBlocked('InvalidCheckpoint', 'The prepared branch commit does not match its recorded parent and approved content.')
    return delivery


def _deliver_command_target(store, cfg, command_id, target):
    from .commands import _json
    checkpoint = target['checkpoint']
    if 'delivery' not in checkpoint:
        with store.transaction(write=True):
            if _cancellation_requested(store, command_id):
                _cancel_unwritten_target(store, command_id, target, checkpoint)
                return
        checkpoint = _prepare_command_delivery(store, cfg, command_id, target)
        if checkpoint is None:
            return
    delivery = _check_delivery_checkpoint(cfg, target, checkpoint)
    destination = target['destination']
    _validate_recorded_destination(cfg, destination)
    path = Path(destination['target_path'])
    with store.transaction(write=True):
        delivered = _observe_prepared_write(destination, delivery)
        if _cancellation_requested(store, command_id) and not delivered:
            _cancel_unwritten_target(store, command_id, target, checkpoint)
            return
        if not delivered:
            rejection = _target_rejection(store, cfg, target['id'])
            if rejection:
                _cancel_rejected_target(store, command_id, target, checkpoint, rejection)
                return
        members = _authorized_members(store, cfg, command_id, target['id'], delivered=delivered)
        if not delivered:
            if not checkpoint.get('review_preview'):
                raise DeliveryBlocked('FreshReviewRequired', 'This historical approval has no reviewed target base. Cancel its unwritten work and approve a fresh complete preview.')
            _publish_prepared_write(destination, delivery, target['id'], _delivery_checkpoint)
        after_sha = snapshot(cfg, path, 'after', content=delivery['after_content'].encode('utf-8'))
        checkpoint['result'] = {'snapshot_after': after_sha, 'branch_commit': delivery['branch_commit'],
                                'mode': destination['mode'], 'completed_at': utc_now_iso()}
        _delivery_checkpoint('before_ack', target['id'])
        store.update('command_targets', 'id', target['id'], {'state': 'completed', 'checkpoint_json': _json(checkpoint),
                     'error_code': '', 'error_detail': ''})
        for member in members:
            from .commands import load_revision
            frozen = load_revision(store, member['revision_id'])['snapshot']['proposal']
            current = store.query_one('SELECT * FROM proposals WHERE id=?', (member['proposal_id'],))
            if (current and current['status']=='approved_user'
                    and all(current[k]==frozen[k] for k in ('learning_id','target_path','target_kind','action','diff_unified'))):
                store.update('proposals', 'id', member['proposal_id'], {'status': 'applied',
                             'applied_at': checkpoint['result']['completed_at'],
                             'snapshot_commit_before': delivery['snapshot_before'], 'snapshot_commit_after': after_sha})
            from .store import actor_for
            _record_event(store, member['proposal_id'], 'applied', actor_for('applied'), _json({
                'command_id': command_id, 'target_id': target['id'], 'revision_id': member['revision_id'],
                'mode': destination['mode'], 'branch': destination['branch_name'],
                'base': delivery['base_ref'], 'branch_commit': delivery['branch_commit'],
                'snapshot_before': delivery['snapshot_before'], 'snapshot_after': after_sha}))
            reviewed=load_revision(store,member['revision_id'])['snapshot']
            if reviewed.get('resolution'):
                from .resolutions import acknowledge
                acknowledge(store,reviewed['resolution'],command_id,target,checkpoint)


def execute_next_command(store, cfg):
    """Claim and deliver one command under the same lock as nightly and CLI.

    The process lock is the live owner. A saved running row alone is not a
    lease: after a crash, the next owner reconciles its checkpoints first.
    Each target commits separately so partial family outcomes remain visible.
    """
    from .commands import COMMAND_STATES, command_status, require_control_schema, _json
    from .delivery_lock import instruction_write_lock
    from .destinations import DestinationError
    if store.db_path.resolve() != cfg.state_path('state.db').resolve():
        raise DeliveryBlocked('StoreMismatch', 'The executing worker must use the configured state database. A redirected reader is not an execution target.')
    with instruction_write_lock(cfg):
        require_control_schema(store)
        from .operations import require_operations
        require_operations(store)
        pending = store.query_one("SELECT id FROM instruction_operations WHERE state IN ('running','queued') ORDER BY CASE WHEN state='running' THEN 0 ELSE 1 END, created_at, id LIMIT 1")
        if pending:
            return _resume_write_operation(store, cfg, pending['id'])
        with store.transaction(write=True):
            row = store.query_one("SELECT * FROM commands WHERE action='approve' AND state IN ('queued','running') "
                                  "ORDER BY CASE WHEN state='running' THEN 0 ELSE 1 END, created_at, id LIMIT 1")
            if row is None:
                return None
            command = command_status(store, row['id'])
            store.update('commands', 'id', row['id'], {'state': 'running', 'updated_at': utc_now_iso(),
                         'claimed_by': f'local:{os.getpid()}', 'claim_token': new_id(), 'attempts': row['attempts'] + 1})
        for target in command['targets']:
            if target['state'] in {'completed', 'cancelled', 'blocked', 'failed'}:
                continue
            try:
                _deliver_command_target(store, cfg, command['id'], target)
            except (DeliveryBlocked, PatchConflict, DestinationError, OSError, UnicodeError, GitError) as exc:
                state = 'blocked' if isinstance(exc, (DeliveryBlocked, PatchConflict, DestinationError)) else 'failed'
                code = exc.code if isinstance(exc, DeliveryBlocked) else type(exc).__name__
                with store.transaction(write=True):
                    store.update('command_targets', 'id', target['id'], {'state': state, 'error_code': code, 'error_detail': str(exc)})
        with store.transaction(write=True):
            targets = store.query('SELECT state, error_code FROM command_targets WHERE command_id=?', (command['id'],))
            counts = {s: sum(t['state'] == s for t in targets) for s in sorted(COMMAND_STATES)}
            # A control request can requeue an earlier failure while this pass
            # handles a later target. Leave it claimable for reconciliation.
            state = ('queued' if counts['queued'] or counts['running']
                     else 'failed' if counts['failed'] else 'blocked' if counts['blocked']
                     else 'cancelled' if counts['cancelled'] else 'completed')
            store.update('commands', 'id', command['id'], {'state': state, 'updated_at': utc_now_iso(),
                         'claimed_by': '', 'claim_token': '', 'lease_expires_at': '',
                         'result_json': _json({'targets': counts, 'failed_by_cause': {k: sum(t['error_code'] == k for t in targets)
                             for k in sorted({t['error_code'] for t in targets if t['error_code']})}})})
        return command_status(store, command['id'])


def _prepare_write_checkpoint(cfg, destination, base, diff, message, *, after_content=None, after_exists=True):
    """The same prepared content/ref protocol serves automatic and reviewed edits."""
    from .commands import _hash
    after = apply_unified_diff(base['content'], diff) if after_content is None else after_content
    before_sha = snapshot(cfg, Path(destination['target_path']), 'before',
                          content=base['content'].encode('utf-8') if base['exists'] else None)
    delivery = {'version': 1, 'before_content': base['content'], 'before_exists': base['exists'],
                'before_hash': base['content_hash'], 'after_content': after, 'after_hash': _delivery_hash(after),
                'base_ref': base['base_ref'], 'branch_exists': False, 'branch_commit': '', 'snapshot_before': before_sha}
    if after_content is not None:
        delivery.update(version=2, after_exists=after_exists)
    if destination['mode'] == 'git_branch':
        root = Path(destination['repo_root'])
        branch_base, branch_exists = _branch_base(root, destination['branch_name'])
        if branch_base != base['base_ref']:
            raise DeliveryBlocked('TargetChanged', 'The delivery branch changed while preparing the edit.')
        delivery['branch_exists'] = branch_exists
        delivery['branch_commit'] = _prepare_branch_commit(root, destination['branch_name'], branch_base,
            branch_exists, destination['relative_path'], _delivery_after_bytes(delivery), message)
    return {'delivery': delivery, 'delivery_hash': _hash(delivery)}


def _observe_prepared_write(destination, delivery):
    """True means published; False means the exact before-state still exists."""
    from .destinations import read_destination
    if destination['mode'] == 'file':
        current = read_destination(destination)
        delivered = current['content_hash'] == delivery['after_hash'] and current['exists'] == delivery.get('after_exists', True)
        untouched = current['content_hash'] == delivery['before_hash'] and current['exists'] == delivery['before_exists']
    else:
        root = Path(destination['repo_root'])
        tip, exists = _branch_base(root, destination['branch_name'])
        candidate = delivery['branch_commit']
        delivered = _run_git(['merge-base', '--is-ancestor', candidate, tip], root, ok_returncodes=(0, 1)).returncode == 0
        untouched = tip == delivery['base_ref'] and exists == delivery['branch_exists']
    if not delivered and not untouched:
        raise DeliveryBlocked('TargetChanged', 'The target matches neither the prepared before nor after state. No content or ref was overwritten.')
    return delivered


def _publish_prepared_write(destination, delivery, record_id, checkpoint_hook):
    path = Path(destination['target_path'])
    if destination['mode'] == 'file':
        data = _delivery_after_bytes(delivery)
        if data is None:
            path.unlink()
            checkpoint_hook('file_removed', record_id)
        else:
            _atomic_write(path, data)
            checkpoint_hook('file_replaced', record_id)
    else:
        root = Path(destination['repo_root'])
        _refuse_checked_out_branch(root, destination['branch_name'])
        before = _capture_worktree(root, path)
        _run_git(['update-ref', 'refs/heads/' + destination['branch_name'], delivery['branch_commit'],
                  delivery['base_ref'] if delivery['branch_exists'] else ''], root)
        _assert_worktree_untouched(before, _capture_worktree(root, path), path)
        checkpoint_hook('ref_updated', record_id)


def _delivery_after_bytes(delivery):
    return delivery['after_content'].encode('utf-8') if delivery.get('after_exists', True) else None
