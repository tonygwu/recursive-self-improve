"""Delivery-branch reads refuse targets that are not regular files."""
from pathlib import Path

import pytest

from self_improve.config import Config
from self_improve.destinations import DestinationError, read_destination, resolve_destination
from tests.test_apply import init_git_repo, run_git


@pytest.mark.parametrize('replacement', ['symlink', 'directory'])
def test_a_delivery_branch_target_that_is_not_a_regular_file_is_refused(tmp_path, replacement):
    """The working tree holds a regular AGENTS.md, but the delivery branch
    holds a symlink or a directory at that path. Reading it for a write must
    refuse, or a delivery could follow the link into another file. This
    refusal had no test."""
    cfg = Config(state_dir=str(tmp_path / 'state'))
    repo = init_git_repo(tmp_path / 'repo', 'AGENTS.md', '# Rules\n')
    branch = cfg.project_branch_name
    run_git(['checkout', '-q', '-b', branch], repo)
    run_git(['rm', '-q', 'AGENTS.md'], repo)
    if replacement == 'symlink':
        (repo / 'AGENTS.md').symlink_to('elsewhere.md')
    else:
        (repo / 'AGENTS.md').mkdir()
        (repo / 'AGENTS.md' / 'inner.md').write_text('x\n')
    run_git(['add', '-A'], repo)
    run_git(['commit', '-q', '-m', 'replace target on the delivery branch'], repo)
    run_git(['checkout', '-q', '-'], repo)
    assert (repo / 'AGENTS.md').read_text() == '# Rules\n'

    destination = resolve_destination(cfg, str(repo / 'AGENTS.md'), 'project_agents_md')
    assert destination['mode'] == 'git_branch' and destination['relative_path'] == 'AGENTS.md'
    with pytest.raises(DestinationError, match='not a regular file'):
        read_destination(destination)


def test_a_regular_delivery_branch_target_is_read_exactly(tmp_path):
    cfg = Config(state_dir=str(tmp_path / 'state'))
    repo = init_git_repo(tmp_path / 'repo', 'AGENTS.md', '# Rules\n')
    destination = resolve_destination(cfg, str(repo / 'AGENTS.md'), 'project_agents_md')
    read = read_destination(destination)
    assert (read['content'], read['exists']) == ('# Rules\n', True)
