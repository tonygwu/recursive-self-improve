# Install and verify a package

The repository is named recursive-self-improve. For compatibility, the Python
distribution remains `self-improve`, imports remain `self_improve` and the command
remains `selfimprove`. State paths and delivery branches are unchanged.

Use Python 3.12 or newer, Git and uv. The locked router dependency uses public
HTTPS at an immutable MIT-licensed commit. Installation needs network access but
does not require GitHub SSH credentials or provider accounts.

From the checkout:

```sh
uv sync --extra dashboard --frozen
uv build --out-dir /absolute/private/build-output
uv run pytest -q tests/test_distribution.py
```

Install the resulting wheel into a fresh environment outside the checkout:

```sh
uv venv /absolute/private/wheel-env
uv pip install --python /absolute/private/wheel-env/bin/python   '/absolute/private/build-output/REPLACE_WITH_WHEEL.whl[dashboard]'
cd /absolute/private
/absolute/private/wheel-env/bin/selfimprove eval-retrieval --synthetic --json
```

Replace the wheel placeholder with the actual build filename. The retrieval demo
uses invented documents and a pinned public model. The first run downloads model
files; later runs can use the cache offline. See [model identity](EMBEDDING_MODELS.md).
Wheel dependency ranges can resolve a different runtime from the source lock;
the identity record makes that difference visible.

The wheel includes all package modules, prompts, regression scenarios, synthetic
retrieval assets, dashboard static files, fonts and their license notices.
Tests compare every runtime asset to the source and exercise imports outside the
editable checkout. Private generated evaluation specs must use an explicit
external output directory. An installed package is never an export destination.

The dashboard demo entry script is in the source distribution:

```sh
uv run python examples/dashboard_demo.py
```

It runs the real dashboard with an invented temporary database and no model calls.
A regular installed dashboard uses your selected existing private state:
`selfimprove --config /absolute/private/config.toml dashboard`.

Distribution verification establishes installation and software behavior with
synthetic inputs. It does not establish private benchmark quality, real-provider
effectiveness or production-service readiness.
