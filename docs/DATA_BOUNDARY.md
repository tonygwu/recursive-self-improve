# Private data boundary

Git contains code, schemas, prompts, synthetic fixtures and safe dataset manifests.
Operational sessions, incidents, rules, proposals and decisions remain in external
SQLite state. Real corpus text, qrels, labels, exports and backups belong outside
all checkouts and installed packages. No hosted data service is required.

Redaction is not publication clearance. Qrels contain source text as well as
judgments. Preserve corpus, qrels and labels together in one frozen release.
Do not publish generated specs derived from personal evidence.

## Frozen datasets

A bundle contains `manifest.json`, `retrieval/corpus.jsonl`,
`retrieval/qrels.yaml` and `labeled/*.yaml`. Its manifest binds a stable dataset
ID, release version, schema versions, member sizes and SHA-256 hashes.
A changed member requires a new version and manifest. Hashes detect drift;
they do not authenticate a publisher or grant permission to share data.

Freeze prepared private inputs into a new private directory:

```sh
uv run python -m self_improve.data_boundary freeze   --corpus /absolute/private/corpus.jsonl   --qrels /absolute/private/qrels.yaml   --labels /absolute/private/labeled   --destination /absolute/private/dataset-v1   --dataset-id my-private-evaluation --version 1
uv run python -m self_improve.data_boundary verify   /absolute/private/dataset-v1 --manifest /absolute/private/expected-manifest.json
```

Retain the expected manifest separately with the reproduction record. A manifest
can be shared only after its own content review. The retained
`evals/manifests/trajectory-eval-v1.json` is a metadata-only benchmark pin;
the corresponding private payload is not distributed.

## Reproduction and exports

```sh
uv run selfimprove eval-retrieval --synthetic --json
uv run selfimprove eval-retrieval   --dataset /absolute/private/dataset-v1   --manifest /absolute/private/expected-manifest.json --json
```

The private command verifies the dataset before scoring. Missing members, changed
hashes, incompatible schemas or a mismatched expected release fail clearly.
There is no fallback to invented examples. Keep result files with the private
dataset and record [embedding identity](EMBEDDING_MODELS.md).

Exporting a new unjudged corpus requires an explicit private destination:

```sh
uv run selfimprove --config /absolute/private/config.toml eval-retrieval   --refresh-corpus --private-destination /absolute/private/new-corpus.jsonl
```

The destination must be new and outside source/package fixtures. The exporter
reads configured data; it does not create judgments. Review and freeze corpus,
qrels and labels together before treating the export as a benchmark.

## Backups and maintenance

Keep verified private backups before destructive maintenance. The
`backup-checkout` command backs up tracked files, not the live database.
Use a consistent SQLite backup for runtime state; do not pair a fresh database
file with stale WAL/SHM sidecars.

`rebuild-state --dry-run --export PATH` previews counts without migrating.
An actual rebuild deletes derived state after verifying its new private export,
and rolls the transaction back on failure. Its export preserves otherwise
unrecoverable rows; it is not a complete database backup. Inspect help and the
preview before deliberately running an actual rebuild.

`run --dry-run` is different: it writes scan and incident state. It is not a
read-only test rehearsal. Use invented fixtures and temporary databases for tests.
