# Embedding models and reproducible retrieval

The default model is `minishlab/potion-base-8M` at Hub commit
`bf8b056651a2c21b8d2565580b8569da283cab23`.
The [pinned model files](https://huggingface.co/minishlab/potion-base-8M/tree/bf8b056651a2c21b8d2565580b8569da283cab23)
are public and carry an MIT license. Text embedding runs locally.

## Identity and loading

`embedding_revision` must be a full lowercase Hub commit ID. Branch names, tags,
and missing revisions fail before download. The loader first resolves that
revision with the [Hub download API](https://huggingface.co/docs/huggingface_hub/guides/download),
then passes the local snapshot to model2vec's public loader. This also supports
offline use after the pinned snapshot has been downloaded.

Every resolved model records its relative file names, byte lengths, and SHA-256
hashes. A canonical JSON inventory supplies `files_sha256`. The cache identity
also includes the source revision, identity schema version, encoding policy, and
installed versions of model2vec, NumPy, tokenizers, and safetensors. A change to
any of these inputs requires new cached vectors.

Retrieval JSON includes this record as `embedding_model_identity`. Text reports
show the source revision, file-inventory hash, and cache identity. Results created
before this record existed are explicitly marked as having an unrecorded identity.
The evaluator binds every stage to the same file-inventory hash and fails if the
model changes between corpus embedding, query ranking, or the threshold sweep.

## Freeze and reproduce

From the source checkout, install the locked environment and record a synthetic
result without personal history:

```sh
uv sync --extra dashboard --frozen
uv run selfimprove eval-retrieval --synthetic --json
```

Retain the code revision, lockfile, result JSON, and model files with the private
benchmark records. Copy `embedding_model_identity.files_sha256` from that result
into a TOML file outside Git:

```toml
embedding_model = "minishlab/potion-base-8M"
embedding_revision = "bf8b056651a2c21b8d2565580b8569da283cab23"
embedding_model_sha256 = "REPLACE_WITH_THE_64_CHARACTER_FILES_SHA256"
```

The placeholder deliberately fails validation. With the recorded digest filled
in, reproduce using the explicit file:

```sh
HF_HUB_OFFLINE=1 uv run selfimprove \
  --config /absolute/path/to/private/model.toml eval-retrieval \
  --dataset /absolute/path/to/private/dataset \
  --manifest evals/manifests/trajectory-eval-v1.json --json
```

Select the manifest that belongs to the dataset. See [DATA_BOUNDARY](DATA_BOUNDARY.md)
for freezing corpus, qrels, and labels together. Missing explicit evaluation
configuration, missing models, and mismatched model or dataset hashes fail; they
do not produce a benchmark using defaults. Omitting `embedding_model_sha256`
records the resolved content without enforcing an earlier content hash. A Hub
commit pin identifies the remote revision; the optional expected hash also checks
the bytes present in the local cache.

## Local models and cache transition

Set `embedding_model` to an existing model directory. Use an absolute path, or
prefix a relative path with `./`, so a missing directory cannot be confused with
a Hub repository name. `embedding_revision` applies only to Hub models and is
ignored for a local directory. `embedding_model_sha256` works for either source.

Keep a dedicated, frozen model directory. The inventory includes all regular
files, including model metadata and alternate model formats; it excludes `.git`
and `.cache` metadata. Extra files can therefore change the identity even if the
loader does not use them. File symlinks are hashed by their target bytes, as in
the Hub cache. Directory symlinks are refused. Model files are rechecked before
and after loading, with no timestamp shortcut.

Relocated local directories with identical contents and runtimes share an identity.
The identity record omits their absolute paths, although the legacy
`embedding_model` result field retains the configured model name or path. Keep
private benchmark output outside Git. Matching identities track inputs; they do
not promise identical floating-point results across every hardware platform.

Existing name-only embedding rows remain in SQLite. They are not relabelled or
reused under a pinned identity. Ordinary writers recompute vectors as needed;
read-only search computes missing vectors in memory. Search also checks the text
hash so an edited learning cannot retain an old vector. This transition requires
no schema migration or database deletion and does not run the live pipeline.
