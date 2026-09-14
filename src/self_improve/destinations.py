"""Read-only resolution of the file or git ref an instruction edit will change."""
from __future__ import annotations

from pathlib import Path
import subprocess
import hashlib


class DestinationError(ValueError):
    pass


def _git(directory: Path, *args: str, missing_repo: bool = False) -> str | None:
    result = subprocess.run(['git', '--no-optional-locks', '-C', str(directory), *args], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    if missing_repo and result.returncode == 128 and 'not a git repository' in result.stderr.lower():
        return None
    raise DestinationError(f"git {' '.join(args)} failed: {result.stderr.strip()}")


def resolve_destination(cfg, target_path: str, target_kind: str) -> dict:
    """Resolve now and retain the destination, even if configuration later changes.

    The existing writer classifies by git membership, not the target-kind label.
    A global instruction file inside a repository therefore uses branch delivery
    too. This reader neither initializes snapshots nor changes a file or git ref.
    """
    raw = Path(target_path)
    if not raw.is_absolute():
        raise DestinationError('The proposal does not name an absolute target.')
    try:
        target = raw.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DestinationError('The proposal has an unreadable target.') from exc
    directory = target.parent
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    root = _git(directory, 'rev-parse', '--show-toplevel', missing_repo=True)
    destination = {'target_path': str(target), 'target_kind': target_kind, 'mode': 'file',
                   'branch_name': '', 'repo_root': '', 'git_common_dir': '', 'relative_path': ''}
    if root is not None and Path(root).resolve() != cfg.state_path('snapshots').resolve():
        root = Path(root).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise DestinationError('The target escapes its repository.') from exc
        branch = cfg.project_branch_name
        _git(root, 'check-ref-format', '--branch', branch)
        common = _git(directory, 'rev-parse', '--git-common-dir')
        destination.update(mode='git_branch', branch_name=branch, repo_root=str(root),
                           git_common_dir=str((directory / common).resolve()), relative_path=relative.as_posix())
    return destination


def destination_identity(destination: dict) -> dict:
    if destination['mode'] == 'git_branch':
        return {k: destination[k] for k in ('mode', 'git_common_dir', 'branch_name', 'relative_path')}
    return {'mode': 'file', 'target_path': destination['target_path']}


def read_destination(destination: dict) -> dict:
    """Read the actual delivery branch or file, preserving its exact UTF-8 bytes."""
    if destination['mode'] == 'file':
        path = Path(destination['target_path'])
        try:
            raw = path.read_bytes()
            exists = True
        except FileNotFoundError:
            raw, exists = b'', False
        base_ref = ''
    elif destination['mode'] == 'git_branch':
        root = destination['repo_root']
        ref = 'refs/heads/' + destination['branch_name']
        resolved = subprocess.run(['git', '--no-optional-locks', '-C', root, 'rev-parse', '--verify', '--quiet', ref], capture_output=True)
        if resolved.returncode == 1:
            base_ref = _git(Path(root), 'rev-parse', '--verify', 'HEAD')
        elif resolved.returncode == 0:
            base_ref = resolved.stdout.decode().strip()
        else:
            raise DestinationError(resolved.stderr.decode(errors='replace').strip())
        entry = _git(Path(root), 'ls-tree', '-z', base_ref, '--', ':(literal)' + destination['relative_path'])
        exists = bool(entry)
        if exists:
            metadata, path = entry.rstrip('\0').split('\t', 1)
            mode, kind, oid = metadata.split()
            if kind != 'blob' or mode not in {'100644', '100755'} or path != destination['relative_path']:
                raise DestinationError('The delivery branch target is not a regular file.')
            result = subprocess.run(['git', '--no-optional-locks', '-C', root, 'cat-file', 'blob', oid], capture_output=True)
            if result.returncode:
                raise DestinationError(result.stderr.decode(errors='replace').strip())
            raw = result.stdout
        else:
            raw = b''
    else:
        raise DestinationError(f"Unknown destination mode {destination['mode']!r}.")
    return {'content': raw.decode('utf-8'), 'exists': exists,
            'content_hash': hashlib.sha256(raw).hexdigest(), 'base_ref': base_ref}
