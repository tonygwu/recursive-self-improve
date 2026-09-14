"""Exercise built wheel contents and imports outside the editable checkout."""
from __future__ import annotations

import subprocess
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    output = tmp_path_factory.mktemp("distribution")
    subprocess.run([sys.executable, "-m", "hatchling", "build", "-t", "wheel",
                    "-d", str(output)], cwd=ROOT, check=True, capture_output=True)
    return next(output.glob("*.whl"))


def test_wheel_preserves_every_public_runtime_asset(wheel):
    with zipfile.ZipFile(wheel) as archive:
        for directory in ("prompts", "evals/synthetic", "evals/regression"):
            files = [p for p in (ROOT / directory).rglob("*") if p.is_file()]
            assert files, directory
            for source in files:
                member = "self_improve/_assets/" + source.relative_to(ROOT).as_posix()
                assert member in archive.namelist(), f"missing runtime asset: {member}"
                assert archive.read(member) == source.read_bytes()
        metadata_name = next(n for n in archive.namelist() if n.endswith("/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        assert metadata["License-Expression"] == "MIT"
        license_name = metadata_name.removesuffix("METADATA") + "licenses/LICENSE"
        assert archive.read(license_name) == (ROOT / "LICENSE").read_bytes()
        # Same-origin fonts and license notices must survive installation.
        # Checking the actual wheel catches assets omitted by build filters.
        static = ROOT / "src/self_improve/dashboard/static"
        for source in static.rglob("*"):
            if source.is_file():
                member = source.relative_to(ROOT / "src").as_posix()
                assert archive.read(member) == source.read_bytes(), member


def test_wheel_retains_the_anonymous_pinned_router_dependency(wheel):
    with zipfile.ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(archive.read(
            next(n for n in archive.namelist() if n.endswith("/METADATA"))))
    router = [r for r in metadata.get_all("Requires-Dist") if r.startswith("llm-quota-router")]
    assert len(router) == 1
    assert "git+https://github.com/tonygwu/llm-quota-router@ce138e1467127d07fa5974d64eaa337b0e913f1b" in router[0]


def test_wheel_runs_without_checkout_or_personal_history(wheel, tmp_path):
    installed = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    # A fresh interpreter prevents the editable package already imported by
    # pytest from satisfying this test. The installed module path is asserted.
    result = subprocess.run([sys.executable, "-I", "-c", r'''
import contextlib, io, json, os, sys, sysconfig
from pathlib import Path
site, work = map(Path, sys.argv[1:])
sys.path.insert(0, str(site))
home = Path.home()
forbidden = tuple(str(home / name) for name in (".claude", ".codex", ".self-improve"))
def audit(event, args):
    if event in {"open", "sqlite3.connect", "os.listdir", "os.scandir", "os.mkdir"}:
        for arg in args:
            if isinstance(arg, (str, Path)) and str(arg).startswith(forbidden):
                raise AssertionError("installed demo accessed personal state")
sys.addaudithook(audit)
import self_improve
assert Path(self_improve.__file__).is_relative_to(site)
from self_improve import cli, pipeline
from self_improve.config import Config
from self_improve.data_boundary import SYNTHETIC_DATASET, load_dataset, freeze_dataset, DataBoundaryError, private_destination
cfg = Config()
assert Path(cfg.mine_search_cli).parent == Path(sysconfig.get_path("scripts"))
assert f"Bash({cfg.mine_search_cli} search-learnings:*)" in cfg.mine_agent_allowed_tools
assert pipeline.PROMPTS_DIR.is_relative_to(site)
assert (pipeline.PROMPTS_DIR / "mine_incident_agentic.md").is_file()
assert list(pipeline.SEED_REGRESSION_DIR.glob("seed-*.yaml"))
assert SYNTHETIC_DATASET.is_relative_to(site)
manifest = load_dataset(SYNTHETIC_DATASET, kind="synthetic")
cli.load_config = lambda *a: (_ for _ in ()).throw(AssertionError("personal config read"))
output = io.StringIO()
with contextlib.redirect_stdout(output):
    assert cli.main(["eval-retrieval", "--synthetic", "--json"]) == 0
result = json.loads(output.getvalue())
assert result["dataset"]["kind"] == "synthetic"
assert result["corpus_meta"]["docs"] == 6 and result["qrel_counts"]["total"] == 6
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    assert cli.main(["eval-retrieval", "--dataset", str(work / "missing")]) == 2
    assert cli.main(["eval-retrieval", "--dataset", str(SYNTHETIC_DATASET)]) == 2
private = work / "private-dataset"
frozen = freeze_dataset(SYNTHETIC_DATASET / "retrieval/corpus.jsonl", SYNTHETIC_DATASET / "retrieval/qrels.yaml",
                       SYNTHETIC_DATASET / "labeled", private, dataset_id="invented-install-check", version=1)
assert load_dataset(private, kind="private") == frozen
try:
    private_destination(SYNTHETIC_DATASET / "new-export.jsonl")
except DataBoundaryError:
    pass
else:
    raise AssertionError("installed package accepted a private export")
print("INSTALLED_WHEEL_OK")
''', str(installed), str(tmp_path)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "INSTALLED_WHEEL_OK"


@pytest.mark.parametrize("destination", [None, ROOT / "evals/regression"])
def test_generated_evals_require_private_output_before_calling_a_model(destination):
    from self_improve.evals.regression import generate_spec
    from self_improve.evals.harness import SpecError
    def forbidden(*args):
        pytest.fail("unsafe generated-eval output reached the model")
    with pytest.raises(SpecError, match="out_dir|checkout"):
        generate_spec({"id": "invented", "rule_text": "Do a check", "why": "Find errors",
                       "incident_summary": "Invented example"}, forbidden,
                      ROOT / "prompts", out_dir=destination)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_generated_eval_cannot_overwrite_a_linked_checkout_file(tmp_path, link_kind):
    from self_improve.evals.regression import generate_spec
    from self_improve.evals.harness import SpecError, load_spec
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: /unused/invented-worktree")
    original = checkout / "fixture.yaml"
    original.write_text("keep this fixture")
    output = tmp_path / "private"
    output.mkdir()
    target = output / "invented.yaml"
    if link_kind == "symlink":
        target.symlink_to(original)
    else:
        target.hardlink_to(original)
    response = {"title": "Invented eval", "scenario_prompt": "Write an output file.",
                "workspace_files": {"input.txt": "example"}, "success_criteria": "Output exists",
                "grader": {"type": "code", "check": "test -f output.txt"}}
    def generate():
        return generate_spec({"id": "invented", "rule_text": "Write output", "why": "Finish",
                              "incident_summary": "Invented evidence"}, lambda _: response,
                             ROOT / "prompts", out_dir=output)
    if link_kind == "symlink":
        with pytest.raises(SpecError, match="checkout"):
            generate()
    else:
        spec = generate()
        assert load_spec(target) == spec
        assert not target.samefile(original)
    assert original.read_text() == "keep this fixture"
