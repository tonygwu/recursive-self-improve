# Public test fixtures

These files exercise protocol shapes with replacement text and placeholder
identities. They are not a private evaluation dataset or a sample of model quality.

- `claude/`: interactive and headless messages, subagent paths, tool results,
  generated messages, unknown event types, and deliberately truncated records.
  Session identifiers use repeated digits; paths use a `redacted` placeholder.
- `codex/`: multiple sessions in one rollout, string and object source metadata,
  archived schemas, tool outputs, encrypted-field placeholders, and truncated
  records. Identifiers use repeated letters and replacement text is explicit.
- `llm/`: invented values inside provider-envelope and quota-router schemas.
  See its [provenance record](llm/README.md).

Malformed and split lines are deliberate. Do not normalize the JSONL as a bulk
formatting step. Keep schema fields, event order, linkage, and error cases when
changing a fixture. Replace any personal text or identifiers before proposing a
new example. Redaction or a matching filename alone is not a publication review.

Frozen private benchmarks belong outside Git and require explicit opt-in.
See [DATA_BOUNDARY](../../docs/DATA_BOUNDARY.md).

Inline Python and JavaScript fixtures follow the same boundary. Reviewed
operational counts, times, and project references have invented replacements;
their corresponding arithmetic and display assertions use those replacements.
The dashboard DOM harness includes deliberately unsupported states to exercise
fallback behavior. It is a constructed input set, not a database export.

Retain useful public technical evidence: parser field names, CLI option shapes,
code-fix timestamps used by compatibility tests, and reproducible model
calibration pairs. A number or date alone does not classify content as private.
