# LLM adapter fixtures

The envelope layouts originated from tool-output shape checks. Public fixtures
use placeholder identities and invented quota, usage, cost, and timing values.
The earlier measured versions are retained in the verified external backup
recorded in [DATA_BOUNDARY](../../../docs/DATA_BOUNDARY.md).

These are adapter inputs, not benchmark outcomes or live account measurements.
Real provider and model names remain where they exercise identity validation.

- `quotapick_pick_claude.json`: contract-1 selection, a nonempty environment
  overlay, ranked candidates, excluded candidates with null usage, and warnings.
- `quotapick_pick_codex.json`: a Codex selection with an empty environment overlay.
- `quotapick_pick_policy_wait.json`: a fitting account that misses policy until a
  fractional epoch-seconds timestamp. Tests align it with their own fake clock.
- `claude_envelope_ok.json`: successful result with two model-usage entries,
  separate cache counters, null fields, and nested usage details.
- `codex_envelope_ok.jsonl`: a completed turn whose stream has no served-model
  field. Tests assert the explicit invented input-token count (1,200).
- `codex_rollout_snippet.jsonl`: session and turn context that supply the model
  identity missing from the stdout stream.

Quota fixtures are parsed through the installed library's real
`Selection.from_payload`; tests also drive `select_account` with injected usage
snapshots and no persistence. The dependency revision is pinned in `pyproject.toml`
and `uv.lock`. Updating that pin requires checking the adapter contract again.
