# Contributing

Use invented transcripts and temporary databases when developing this project.
The normal test suite needs no personal session history, provider account, API
key, or installed production service.

## Set up and test

Install Python 3.12 or newer, Git, [uv](https://docs.astral.sh/uv/), and Node.js.
CI uses uv 0.10.10 and Node.js 24. From your development checkout:

```sh
uv sync --extra dashboard --frozen
uv run pytest -q -ra
uv run pytest -q tests/test_data_boundary.py && printf 'DATA_BOUNDARY_OK\n'
```

Keep the dashboard extra installed for the full suite. Without it, API tests
skip. Node.js runs the dashboard behavior harness. The suite includes real
embedding checks and downloads the public
[potion-base-8M model](https://huggingface.co/minishlab/potion-base-8M) on first
use. Text embedding runs locally. Subsequent runs can use the model cache.
Private-dataset checks, the real-sandbox probe, and installed-provider startup
checks require separate opt-ins and are normally skipped; inspect the reasons
printed by `-ra`. `SELFIMPROVE_PROVIDER_PREFLIGHT=1` permits the installed-provider
test to run the configured CLIs with `--version`. Normal pipeline tests use
temporary executables that refuse model invocations.

The default model has a full Hub commit pin. CI records its file hashes and
runtime versions, then uses that cache offline. Vector caches and evaluation
reports carry the same model identity. See [EMBEDDING_MODELS](docs/EMBEDDING_MODELS.md)
for local models, expected content hashes, and reproduction commands. The Python
lock alone does not freeze model weights.

For a disposable product rehearsal:

```sh
uv run python examples/dashboard_demo.py
```

The demo uses invented records in a temporary database. The regular pipeline's
`run --dry-run` command writes operational state and is unsuitable as a test
rehearsal. Do not point tests or demo decisions at a live database.

## Prepare a change

Keep changes on a review branch. Add a behavioral regression when fixing a bug,
and run the affected tests followed by the full suite before committing.
Describe the problem, resulting behavior, and actual verification in the pull
request. If a check cannot run, state the missing requirement and its scope.

Preserve synthetic regression fixtures that exercise useful behavior. Keep real
corpus text, qrels, labels, session references, operational exports, and backups
outside Git. Redaction alone does not make such records suitable for a fixture
or issue. Follow [DATA_BOUNDARY](docs/DATA_BOUNDARY.md) for private reproduction
and [SECRET_SCAN](docs/SECRET_SCAN.md) for the separate content-audit process.
Review the staged diff before sharing it.

Changes to packaging must also pass the installed-wheel checks in
[DISTRIBUTION](docs/DISTRIBUTION.md). Changes to approval, delivery, or automatic
application must preserve the contracts and targeted checks in [AGENTS](AGENTS.md).

## Continuous integration

[Checks](.github/workflows/checks.yml) runs on pushes and pull requests. It tests
Linux with Python 3.12 and macOS with Python 3.14, installs the dashboard extra,
runs the full suite and boundary acceptance command, and builds and installs a
wheel for the synthetic demo outside the checkout. Linux also scans the current
tracked source with checksum-verified Gitleaks 8.30.1.

The workflow uses a read-only repository token for checkout, does not persist
Git credentials, and needs no repository secrets. Actions use verified full
commit pins, following [GitHub's guidance](https://docs.github.com/en/actions/reference/security/secure-use).
The uv action has an explicit tool version and disables cache persistence;
see [uv's integration guide](https://docs.astral.sh/uv/guides/integration/github/).
The runner discards temporary builds, model cache, and private scan reports.
Reports are not uploaded as artifacts.

CI's current-source scan does not classify confidential content by itself. The selected release scope and limits are in [RESEARCH_PREVIEW](docs/RESEARCH_PREVIEW.md).
The macOS scheduler and authenticated provider execution are outside these
synthetic checks; Windows is not in the CI matrix.

Contributions are covered by this repository's [MIT license](LICENSE).
For vulnerabilities, use the reporting process in [SECURITY](SECURITY.md).
