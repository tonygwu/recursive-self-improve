"""Shared interpretation of recorded eval outcomes, without web dependencies."""

import json

EVAL_INFRA_ERRORS = frozenset({"agent_error"})
EVAL_JUDGED_ERRORS = frozenset({"graded_fail", "asked_operator"})


def no_trial_ran(row, *, taxonomy=None) -> bool:
    """Zero successes is not enough: every recorded attempt must be an agent error."""
    if row is None:
        return False
    if taxonomy is None:
        try:
            taxonomy = json.loads(row["error_taxonomy_json"] or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"eval_results {row['id']!r}: error_taxonomy_json is not valid JSON") from exc
    if not isinstance(taxonomy, dict):
        raise ValueError(f"eval_results {row['id']!r}: error_taxonomy_json is not an object")
    attempted, succeeded = row["attempted"], row["succeeded"]
    return (type(attempted) is int and attempted > 0 and succeeded == 0
            and bool(taxonomy) and set(taxonomy) <= EVAL_INFRA_ERRORS
            and all(type(n) is int and n > 0 for n in taxonomy.values())
            and sum(taxonomy.values()) == attempted)
