"""Delivery targets: where self-improve WRITES, separate from where harnesses LOAD.

The optional ``[targets]`` table in config.toml names the files and directory
that receive instruction edits. The flat ``global_claude_md``,
``codex_global_agents_md`` and ``skills_dir`` keys keep naming what the harness
loads; observation, the in-force corpus and hooks read those. Every tree here
is temporary; no test reads the operator's home or state.
"""
from pathlib import Path

import pytest

from self_improve import routing
from self_improve.config import Config, ConfigError, load_config
from self_improve.destinations import resolve_destination
from tests.test_apply import init_git_repo


def _write(tmp_path, body):
    path = tmp_path / 'config.toml'
    path.write_text(body)
    return path


def _loads(tmp_path):
    """A config whose load paths live in a temporary fake home."""
    home = tmp_path / 'home'
    (home / '.claude' / 'skills').mkdir(parents=True)
    (home / '.codex').mkdir()
    return (f'state_dir = "{tmp_path / "state"}"\n'
            f'global_claude_md = "{home / ".claude" / "CLAUDE.md"}"\n'
            f'codex_global_agents_md = "{home / ".codex" / "AGENTS.md"}"\n'
            f'skills_dir = "{home / ".claude" / "skills"}"\n'), home


def test_without_a_targets_table_delivery_uses_the_load_paths(tmp_path):
    flat, home = _loads(tmp_path)
    cfg = load_config(_write(tmp_path, flat))
    assert cfg.delivery_path('global_claude_md') == str(home / '.claude' / 'CLAUDE.md')
    assert cfg.delivery_path('codex_global') == str(home / '.codex' / 'AGENTS.md')
    assert cfg.delivery_path('skill') == str(home / '.claude' / 'skills')
    assert cfg.delivery_path('global_private') == ''
    assert Config().delivery_path('global_claude_md') == Config().global_claude_md


def test_a_targets_table_sets_delivery_without_changing_load_paths(tmp_path):
    flat, home = _loads(tmp_path)
    clone = tmp_path / 'skills-clone'
    (clone / 'context' / 'global').mkdir(parents=True)
    (clone / 'skills').mkdir()
    private = home / '.claude' / 'rules'
    private.mkdir()
    body = flat + (
        '[targets]\n'
        f'global_public = "{clone / "context/global/AGENTS.md"}"\n'
        f'codex_global = "{clone / "context/global/AGENTS.md"}"\n'
        f'global_private = "{private / "personal.md"}"\n'
        f'skills_dir = "{clone / "skills"}"\n')
    cfg = load_config(_write(tmp_path, body))
    assert cfg.delivery_path('global_claude_md') == str(clone / 'context/global/AGENTS.md')
    assert cfg.delivery_path('codex_global') == str(clone / 'context/global/AGENTS.md')
    assert cfg.delivery_path('global_private') == str(private / 'personal.md')
    assert cfg.delivery_path('skill') == str(clone / 'skills')
    # Load paths are untouched.
    assert cfg.global_claude_md == str(home / '.claude' / 'CLAUDE.md')
    assert cfg.skills_dir == str(home / '.claude' / 'skills')


def test_a_partial_targets_table_keeps_the_other_defaults(tmp_path):
    flat, home = _loads(tmp_path)
    clone = tmp_path / 'clone'
    clone.mkdir()
    cfg = load_config(_write(tmp_path, flat + f'[targets]\nglobal_public = "{clone / "AGENTS.md"}"\n'))
    assert cfg.delivery_path('global_claude_md') == str(clone / 'AGENTS.md')
    assert cfg.delivery_path('codex_global') == str(home / '.codex' / 'AGENTS.md')


def test_an_unknown_target_key_is_refused(tmp_path):
    flat, _ = _loads(tmp_path)
    with pytest.raises(ConfigError, match="Unknown targets key 'global'"):
        load_config(_write(tmp_path, flat + f'[targets]\nglobal = "{tmp_path / "x.md"}"\n'))


@pytest.mark.parametrize('value', ['"relative/AGENTS.md"', '""', '7'])
def test_a_target_must_be_an_absolute_path_string(tmp_path, value):
    flat, _ = _loads(tmp_path)
    with pytest.raises(ConfigError, match='global_public'):
        load_config(_write(tmp_path, flat + f'[targets]\nglobal_public = {value}\n'))


def test_a_target_whose_parent_is_missing_is_refused(tmp_path):
    flat, _ = _loads(tmp_path)
    with pytest.raises(ConfigError, match='parent directory does not exist'):
        load_config(_write(tmp_path, flat + f'[targets]\nglobal_public = "{tmp_path / "nope" / "AGENTS.md"}"\n'))


def test_a_skills_target_must_be_an_existing_directory(tmp_path):
    flat, _ = _loads(tmp_path)
    with pytest.raises(ConfigError, match='skills_dir'):
        load_config(_write(tmp_path, flat + f'[targets]\nskills_dir = "{tmp_path / "absent"}"\n'))


def test_a_target_inside_a_never_write_checkout_is_refused(tmp_path):
    flat, _ = _loads(tmp_path)
    prod = tmp_path / 'skills' / 'repo-prod' / 'context' / 'global'
    prod.mkdir(parents=True)
    with pytest.raises(ConfigError, match='repo-prod'):
        load_config(_write(tmp_path, flat + f'[targets]\nglobal_public = "{prod / "AGENTS.md"}"\n'))
    with pytest.raises(ConfigError, match='repo-prod'):
        Config(targets=(('global_public', str(prod / 'AGENTS.md')),))


def test_a_targets_value_that_is_not_a_table_is_refused(tmp_path):
    flat, _ = _loads(tmp_path)
    with pytest.raises(ConfigError, match='targets'):
        load_config(_write(tmp_path, 'targets = "x"\n' + flat))


def test_an_unknown_delivery_kind_raises():
    with pytest.raises(ConfigError, match='hook'):
        Config().delivery_path('hook')


def test_replace_keeps_targets(tmp_path):
    import dataclasses
    cfg = Config(targets=(('global_public', str(tmp_path / 'A.md')),))
    assert dataclasses.replace(cfg, state_dir=str(tmp_path)).delivery_path('global_claude_md') == str(tmp_path / 'A.md')


def _learning(**extra):
    base = {'id': 'L1', 'rule_text': 'Do the thing.', 'project_count': 1,
            'source': 'claude', 'scope_guess': 'global', 'projects': [], 'title': 'thing'}
    base.update(extra)
    return base


def test_routing_writes_to_delivery_targets_and_hooks_keep_the_load_path(tmp_path):
    home = tmp_path / 'home'
    clone = tmp_path / 'clone'
    cfg = Config(global_claude_md=str(home / '.claude/CLAUDE.md'),
                 codex_global_agents_md=str(home / '.codex/AGENTS.md'),
                 skills_dir=str(home / '.claude/skills'),
                 targets=(('global_public', str(clone / 'context/global/AGENTS.md')),
                          ('codex_global', str(clone / 'context/global/AGENTS.md')),
                          ('skills_dir', str(clone / 'skills'))))
    assert routing.route(_learning(), cfg).target_path == clone / 'context/global/AGENTS.md'
    assert routing.route(_learning(scope_guess='codex_global'), cfg).target_path == clone / 'context/global/AGENTS.md'
    skill = routing.route(_learning(scope_guess='skill'), cfg).target_path
    assert skill.parent.parent == clone / 'skills' and skill.name == 'SKILL.md'
    assert routing.route(_learning(scope_guess='hook'), cfg).target_path == home / '.claude/settings.json'
    assert routing.route(_learning(violated_existing_rule='x'), cfg).target_path == home / '.claude/settings.json'


def test_a_global_target_in_a_clone_resolves_to_branch_delivery_in_that_clone(tmp_path):
    """The live symlinked global resolves into a never-write checkout. A
    configured clone instead resolves to branch delivery inside the clone."""
    (tmp_path / 'skills' / 'context' / 'global').mkdir(parents=True)
    clone = init_git_repo(tmp_path / 'skills', 'context/global/AGENTS.md', '# Global\n')
    target = clone / 'context/global/AGENTS.md'
    cfg = Config(state_dir=str(tmp_path / 'state'), targets=(('global_public', str(target)),))
    destination = resolve_destination(cfg, cfg.delivery_path('global_claude_md'), 'global_claude_md')
    assert destination['mode'] == 'git_branch'
    assert destination['repo_root'] == str(clone.resolve())
    assert destination['relative_path'] == 'context/global/AGENTS.md'
    assert destination['branch_name'] == cfg.project_branch_name
    assert not any(routing.is_never_write(str(p)) for p in (Path(destination['target_path']), *Path(destination['target_path']).parents))


def test_the_sandbox_protects_load_and_delivery_targets(tmp_path):
    from self_improve.sandbox import instruction_paths_to_protect
    cfg = Config(state_dir=str(tmp_path / 'state'),
                 targets=(('global_public', str(tmp_path / 'clone/AGENTS.md')),
                          ('global_private', str(tmp_path / 'private/personal.md')),
                          ('skills_dir', str(tmp_path / 'clone/skills'))))
    protected = instruction_paths_to_protect(cfg)
    for p in (cfg.global_claude_md, str(tmp_path / 'clone/AGENTS.md'),
              str(tmp_path / 'private/personal.md'), str(tmp_path / 'clone/skills')):
        assert p in protected


def test_targets_survive_the_json_round_trip_used_by_frozen_job_plans(tmp_path):
    import json
    from dataclasses import asdict
    cfg = Config(targets=(('global_public', str(tmp_path / 'A.md')),))
    thawed = Config(**json.loads(json.dumps(asdict(cfg))))
    assert thawed.targets == cfg.targets
    assert thawed.delivery_path('global_claude_md') == str(tmp_path / 'A.md')
