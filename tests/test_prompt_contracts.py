"""Check the examples a mining agent reads against its response parser."""

import json
import re
from pathlib import Path

from self_improve.miner import (
    MINE_SCOPES,
    normalize_mine_payload,
    render_prompt,
    validate_mine_json,
)


def _agentic_prompt():
    return render_prompt(
        Path(__file__).resolve().parents[1] / "prompts/mine_incident_agentic.md",
        {
            "signal_type": "correction",
            "project": "/demo/project",
            "start_line": "1",
            "matched_text": "Invented correction",
            "search_cli": "/demo/bin/selfimprove",
            "max_turns": "20",
            "explore_budget": "15",
        },
    )


def test_agentic_prompt_examples_are_complete_valid_responses():
    text = _agentic_prompt()
    examples = [
        json.JSONDecoder().raw_decode(text[match.start():])[0]
        for match in re.finditer(r"(?m)^\{\n", text)
    ]
    assert len(examples) == 2
    assert {example["is_real_learning"] for example in examples} == {True, False}
    for example in examples:
        normalized, defaulted = normalize_mine_payload(example.copy(), agentic=True)
        assert defaulted == [], "the prompt example teaches omission of output fields"
        assert validate_mine_json(normalized, agentic=True) == []


def test_agentic_prompt_scope_choices_match_the_response_parser():
    declaration = re.search(
        r'- `scope_guess`: exactly one of (.*?)\.\n', _agentic_prompt(), re.DOTALL
    )
    assert declaration is not None
    scopes = re.findall(r'"([a-z_]+)"', declaration.group(1))
    assert len(scopes) == len(set(scopes))
    assert set(scopes) == set(MINE_SCOPES)
