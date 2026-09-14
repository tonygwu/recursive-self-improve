"""Config surface tests.

Not about defaults — about the config staying honest: every key it offers must
actually do something, or say why it does not.
"""

import pytest



# Config keys that nothing in src/ reads. Each needs a reason, because a knob
# that silently does nothing is the same class of defect this project treats as
# a bug everywhere else: it looks configurable and is not.
INTENTIONALLY_UNREAD = {
    "claude_history_path": (
        "history.jsonl was planned as a cheap corrections source and never "
        "implemented; the key documents the intent"
    ),
    "quotapick_path": (
        "ops/run-nightly.sh hardcodes its own default and does not read "
        "config.py, so nothing reads this today"
    ),
    "llm_concurrency": (
        "LLM calls are serial; no dispatcher reads this. Setting it changes "
        "nothing"
    ),
}


def test_every_config_key_is_read_somewhere_or_documented_as_unread():
    """A config key nothing reads is a silent no-op, not a feature.

    Found three by audit on 2026-08-17. The point of this test is not the three
    - it is that the next one gets caught when it is added rather than years
    later by someone wondering why their setting did nothing.
    """
    import dataclasses
    import pathlib
    import re

    from self_improve.config import Config

    src = "\n".join(
        p.read_text()
        for p in pathlib.Path("src/self_improve").rglob("*.py")
        if p.name != "config.py"
    )
    prompts_dir = pathlib.Path("prompts")
    if prompts_dir.exists():
        src += "\n".join(p.read_text() for p in prompts_dir.glob("*.md"))

    unread = {
        f.name
        for f in dataclasses.fields(Config)
        if not re.search(rf"\b{re.escape(f.name)}\b", src)
    }
    undocumented = unread - set(INTENTIONALLY_UNREAD)
    assert not undocumented, (
        f"config keys nothing reads and nothing explains: {sorted(undocumented)}. "
        "Either wire them up or add them to INTENTIONALLY_UNREAD with a reason."
    )
    # And the converse: a key listed here that IS now read should be removed
    # from the list, so the list cannot rot into a permanent excuse.
    stale = set(INTENTIONALLY_UNREAD) - unread
    assert not stale, (
        f"these are now read in src/ and should leave INTENTIONALLY_UNREAD: {sorted(stale)}"
    )


# Prompt files that nothing in src/ loads, with the reason.
UNLOADED_PROMPTS = {
    "grade_rubric.md": (
        "wraps a spec's rubric with 'judge the OUTCOME, not the path' framing "
        "for the MODEL grader. The pipeline never uses that path: "
        "prompts/gen_regression_eval.md requires grader.type == 'code', and "
        "pipeline.py calls regression.gate() without a model_grader. The "
        "harness supports model grading for hand-written specs and tests it "
        "with an injected fake, so this is the prompt to load if that is ever "
        "wired up"
    ),
}


def test_every_prompt_file_is_loaded_or_documented_as_unloaded():
    """A prompt nobody loads is a file people will edit expecting an effect.

    Same reasoning as the config keys above: dead surface that looks live is
    worse than no surface. Found `grade_rubric.md` this way.
    """
    import pathlib
    import re

    prompts = pathlib.Path("prompts")
    # Was `return`. A silent pass when the directory is missing means the test
    # reports success for a repo it never looked at.
    assert prompts.exists(), (
        f"prompts/ not found from cwd {pathlib.Path.cwd()}; this test scans "
        "the repo and cannot report success without reading it"
    )
    sources = list(pathlib.Path("src/self_improve").rglob("*.py"))
    assert len(sources) > 10, f"only {len(sources)} source files found; wrong cwd?"
    src = "\n".join(p.read_text() for p in sources)
    files = list(prompts.glob("*.md"))
    assert files, "no prompt files found; the scanner would pass on anything"
    unloaded = {
        f.name
        for f in files
        if not re.search(rf"{re.escape(f.name)}|{re.escape(f.stem)}", src)
    }
    undocumented = unloaded - set(UNLOADED_PROMPTS)
    assert not undocumented, (
        f"prompt files nothing loads and nothing explains: {sorted(undocumented)}"
    )
    stale = set(UNLOADED_PROMPTS) - unloaded
    assert not stale, f"now loaded, remove from UNLOADED_PROMPTS: {sorted(stale)}"


def test_a_carve_out_action_that_no_code_writes_is_refused():
    """Reject configured review actions outside the proposal-action vocabulary.

    An unknown value cannot match proposals.action. The intentional misspelling
    must raise ConfigError when Config is constructed. Mandatory review checks
    in execution_policy apply independently of this configurable action list.
    """
    from self_improve.config import Config, ConfigError
    from self_improve.store import PROPOSAL_ACTIONS

    assert set(Config().review_queue_actions) <= PROPOSAL_ACTIONS, (
        "the shipped default names an action nothing writes"
    )
    with pytest.raises(ConfigError, match="delete_human_lien|not an action"):
        Config(review_queue_actions=("convert_to_hook", "delete_human_lien"))


def test_every_action_the_code_assigns_is_in_the_declared_vocabulary():
    """Scan the modules that build proposals, and assert the scan found some."""
    import ast
    from pathlib import Path

    from self_improve import pipeline, propose, routing
    from self_improve.store import PROPOSAL_ACTIONS

    assigned: set[str] = set()
    for module in (propose, routing, pipeline):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # {"action": "add"}
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant) and key.value == "action"
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                    ):
                        assigned.add(value.value)
            # action="add"
            if isinstance(node, ast.keyword) and node.arg == "action":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    assigned.add(node.value.value)
    assert len(assigned) >= 5, f"the scanner found only {assigned}; it is broken"
    unknown = assigned - PROPOSAL_ACTIONS
    assert unknown == set(), f"code writes actions the vocabulary does not declare: {sorted(unknown)}"
