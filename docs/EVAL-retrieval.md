# Retrieval evaluation

The harness scores semantic search, a keyword proxy, and their union against frozen judgments.
It measures document retrieval for duplicate detection. It does not measure the miner's final decision.

Run the invented public example:

```sh
uv run selfimprove eval-retrieval --synthetic
uv run selfimprove eval-retrieval --synthetic --json
```

The output identifies the dataset, version, content hash, and embedding model.
Synthetic scores exercise the harness. They provide no evidence about real-history retrieval quality.

For private benchmarks, use an explicitly selected frozen bundle and expected
manifest as described in [DATA_BOUNDARY](DATA_BOUNDARY.md).

The corpus stores document text and content-derived document IDs.
Qrels store source text, relevance judgments, reasons, and provenance groups.
They must remain together. The bundle manifest also binds the labeled incident set.
Changing any member requires a new version and a new manifest.

The semantic arm calls the production search implementation over a temporary database seeded from the frozen corpus.
It excludes live instruction files and the query document itself.
The keyword arm uses deterministic BM25 as a proxy for lexical retrieval.
Production agents invent search patterns, so this proxy does not reproduce their behavior.
The union measures the recall available from both rankings.

The report includes recall, precision over judged results, reciprocal rank, and a recall ceiling at each configured cutoff.
Borderline judgments are excluded by default and included in a separate sensitivity analysis.
Results are also split by judgment provenance. Candidate pools selected with the tested embedding model are not independent gold evidence.

Tests in `tests/test_retrieval_eval.py` exercise metric arithmetic and malformed inputs with invented data.
Tests in `tests/test_private_retrieval_eval.py` preserve the original numerical regressions and require an explicitly selected private bundle.
Missing or modified selected datasets fail. No command substitutes invented examples for a private benchmark.
