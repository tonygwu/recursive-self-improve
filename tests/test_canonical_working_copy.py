"""Choose an automation checkout without relying on usage frequency.

Synthetic layouts cover repo-0, clone sets beginning at repo-1, and a busy
checkout elsewhere. Preserve the operator's separate interactive workspace."""

from __future__ import annotations

from self_improve.routing import canonical_working_copy


def _never(_path: str) -> bool:
    """isdir stub: nothing exists, so sibling discovery can never fire."""
    return False


class TestRepoZeroPreference:
    def test_repo_zero_wins_over_other_clones(self) -> None:
        paths = [
            "/Users/t/Code/demo-app/repo-3",
            "/Users/t/Code/demo-app/repo-0",
            "/Users/t/Code/demo-app/repo-7",
        ]
        assert canonical_working_copy(paths, isdir=_never) == "/Users/t/Code/demo-app/repo-0"

    def test_repo_zero_wins_regardless_of_evidence_order(self) -> None:
        """Order of the evidence paths must not decide the write target."""
        a = ["/Users/t/Code/demo-app/repo-0", "/Users/t/Code/demo-app/repo-9"]
        b = list(reversed(a))
        assert canonical_working_copy(a, isdir=_never) == canonical_working_copy(b, isdir=_never)

    def test_lowest_numbered_clone_when_no_repo_zero(self) -> None:
        """Choose the lowest numbered clone when the fixture set starts at repo-1."""
        paths = ["/Users/t/Code/demo-library/repo-5", "/Users/t/Code/demo-library/repo-1", "/Users/t/Code/demo-library/repo-2"]
        assert canonical_working_copy(paths, isdir=_never) == "/Users/t/Code/demo-library/repo-1"

    def test_numeric_not_lexicographic_ordering(self) -> None:
        """repo-2 beats repo-10 — string sort would get this backwards."""
        paths = ["/Users/t/Code/demo-app/repo-10", "/Users/t/Code/demo-app/repo-2"]
        assert canonical_working_copy(paths, isdir=_never) == "/Users/t/Code/demo-app/repo-2"

    def test_repo_n_beats_non_repo_n_even_when_listed_later(self) -> None:
        paths = ["/Users/t/Apps/old-service", "/Users/t/Code/demo-app/repo-4"]
        assert canonical_working_copy(paths, isdir=_never) == "/Users/t/Code/demo-app/repo-4"

    def test_trailing_slash_does_not_defeat_the_match(self) -> None:
        paths = ["/Users/t/Code/demo-app/repo-3/", "/Users/t/Code/demo-app/repo-0/"]
        assert canonical_working_copy(paths, isdir=_never) == "/Users/t/Code/demo-app/repo-0/"


class TestFallbackIsUnchanged:
    def test_no_repo_n_keeps_first_path(self) -> None:
        """Without the convention in play, behaviour is what it always was."""
        paths = ["/Users/t/Apps/old-service", "/Users/t/Documents/demo-scratch"]
        assert canonical_working_copy(paths, isdir=_never) == "/Users/t/Apps/old-service"

    def test_single_candidate_is_returned(self) -> None:
        assert canonical_working_copy(["/Users/t/Code/x"], isdir=_never) == "/Users/t/Code/x"

    def test_empty_candidates_returns_empty(self) -> None:
        assert canonical_working_copy([], isdir=_never) == ""

    def test_blank_paths_are_ignored(self) -> None:
        assert canonical_working_copy(["", "/Users/t/Code/x"], isdir=_never) == "/Users/t/Code/x"


class TestSiblingDiscovery:
    """Adopt a verified repo-0 sibling that is absent from the fixture evidence.

    The evidence names repo-3 and repo-5. The resolver confirms that the repo-0
    directory belongs to the same repository before it becomes the write target.
    """

    def test_adopts_sibling_repo_zero_when_identity_matches(self) -> None:
        seen = {"/Users/t/Code/demo-app/repo-0": True}
        got = canonical_working_copy(
            ["/Users/t/Code/demo-app/repo-3", "/Users/t/Code/demo-app/repo-5"],
            project_key="remote:github.com/t/tn",
            resolver=lambda p: "remote:github.com/t/tn",
            isdir=seen.get,
        )
        assert got == "/Users/t/Code/demo-app/repo-0"

    def test_refuses_sibling_whose_identity_differs(self) -> None:
        """A same-named directory is not the same repo. Never write into it."""
        seen = {"/Users/t/Code/demo-app/repo-0": True}
        got = canonical_working_copy(
            ["/Users/t/Code/demo-app/repo-3"],
            project_key="remote:github.com/t/tn",
            resolver=lambda p: "remote:github.com/t/SOMETHING-ELSE",
            isdir=seen.get,
        )
        assert got == "/Users/t/Code/demo-app/repo-3"

    def test_no_sibling_on_disk_falls_back_to_lowest_clone(self) -> None:
        got = canonical_working_copy(
            ["/Users/t/Code/demo-library/repo-2", "/Users/t/Code/demo-library/repo-1"],
            project_key="k",
            resolver=lambda p: "k",
            isdir=_never,
        )
        assert got == "/Users/t/Code/demo-library/repo-1"

    def test_no_resolver_means_no_sibling_adoption(self) -> None:
        """Without a way to verify identity we do not guess — we stay put."""
        seen = {"/Users/t/Code/demo-app/repo-0": True}
        got = canonical_working_copy(["/Users/t/Code/demo-app/repo-3"], isdir=seen.get)
        assert got == "/Users/t/Code/demo-app/repo-3"

    def test_resolver_failure_does_not_take_down_routing(self) -> None:
        """A broken git checkout must not turn a write target into a crash."""

        def boom(_p: str) -> str:
            raise OSError("git exploded")

        seen = {"/Users/t/Code/demo-app/repo-0": True}
        got = canonical_working_copy(
            ["/Users/t/Code/demo-app/repo-3"],
            project_key="k",
            resolver=boom,
            isdir=seen.get,
        )
        assert got == "/Users/t/Code/demo-app/repo-3"

    def test_sibling_not_consulted_when_repo_zero_already_present(self) -> None:
        """No subprocess should run when the answer is already in the evidence."""
        calls: list[str] = []

        def counting(p: str) -> str:
            calls.append(p)
            return "k"

        got = canonical_working_copy(
            ["/Users/t/Code/demo-app/repo-0", "/Users/t/Code/demo-app/repo-3"],
            project_key="k",
            resolver=counting,
            isdir=lambda _p: True,
        )
        assert got == "/Users/t/Code/demo-app/repo-0"
        assert calls == []


class TestPrimaryProjectUsesTheConvention:
    """routing.primary_project() honours the same rule when it falls back."""

    def test_projects_list_prefers_repo_zero(self) -> None:
        from self_improve.routing import primary_project

        learning = {"id": "x", "projects": ["/c/tn/repo-4", "/c/tn/repo-0"]}
        assert primary_project(learning) == "/c/tn/repo-0"

    def test_stored_write_target_still_wins(self) -> None:
        """A backfilled primary_project_path is authoritative; do not re-derive."""
        from self_improve.routing import primary_project

        learning = {
            "id": "x",
            "primary_project_path": "/c/tn/repo-7",
            "projects": ["/c/tn/repo-0"],
        }
        assert primary_project(learning) == "/c/tn/repo-7"

    def test_still_raises_when_there_is_no_project(self) -> None:
        import pytest

        from self_improve.routing import RoutingError, primary_project

        with pytest.raises(RoutingError):
            primary_project({"id": "x", "projects": []})


class TestKnownClonesFromTheDatabase:
    """Find related working copies outside the candidate's directory tree.

    The invented fixture has a checkout with no repo-N sibling. Other known
    paths let the resolver locate its clone set by repository identity.
    """

    def test_known_repo_zero_wins_over_a_non_clone_evidence_path(self) -> None:
        got = canonical_working_copy(
            ["/Users/t/Apps/old-service"],
            also_consider=["/Users/t/Code/demo-app/repo-0", "/Users/t/Code/demo-app/repo-3"],
            isdir=lambda _p: True,
        )
        assert got == "/Users/t/Code/demo-app/repo-0"

    def test_known_clones_that_no_longer_exist_are_not_chosen(self) -> None:
        """A deleted checkout is not a write target."""
        got = canonical_working_copy(
            ["/Users/t/Apps/old-service"],
            also_consider=["/Users/t/Code/demo-app/repo-0"],
            isdir=_never,
        )
        assert got == "/Users/t/Apps/old-service"

    def test_evidence_still_wins_ties_against_known_clones(self) -> None:
        """Same clone number in both lists: prefer the one we have evidence for."""
        got = canonical_working_copy(
            ["/Users/t/EvidenceExample/repo-2"],
            also_consider=["/Users/t/other/repo-2"],
            isdir=lambda _p: True,
        )
        assert got == "/Users/t/EvidenceExample/repo-2"

    def test_a_known_non_clone_path_never_displaces_evidence(self) -> None:
        got = canonical_working_copy(
            ["/Users/t/Apps/old-service"],
            also_consider=["/Users/t/Documents/some-other-copy"],
            isdir=lambda _p: True,
        )
        assert got == "/Users/t/Apps/old-service"


# ---------------------------------------------------------------------------
# repo-prod: the checkout automation RUNS FROM and must never WRITE INTO
# ---------------------------------------------------------------------------
#
# These two ideas are easy to conflate and the consequences differ:
#   repo-0     the checkout automation WRITES TO  (proposals land here)
#   repo-prod  the checkout automation RUNS FROM  (launchd executes it)
# repo-prod's working tree must stay clean, so a write into it both breaks
# that invariant and puts an unreviewed edit into tomorrow night's run.
#
# This path is reachable in normal operation, not hypothetical: llm.py runs
# every headless call with cwd = the running checkout, so nightly miner
# sessions index under repo-prod, and miner.py stores that raw cwd as
# primary_project_path without consulting canonical_working_copy.

import pytest

from self_improve.routing import (
    RoutingError,
    canonical_working_copy as cwc,
    is_never_write,
    primary_project,
    redirect_never_write,
)

_SET = "/Users/t/Code/self-improve"


def _clone_set_on_disk(path: str) -> bool:
    """repo-0 and repo-prod both exist and both are git checkouts."""
    return path in {
        f"{_SET}/repo-0",
        f"{_SET}/repo-0/.git",
        f"{_SET}/repo-1",
        f"{_SET}/repo-prod",
    }


class TestProductionCheckoutIsNeverAWriteTarget:
    def test_repo_prod_is_recognised_and_repo_n_is_not(self):
        assert is_never_write(f"{_SET}/repo-prod")
        assert is_never_write(f"{_SET}/repo-prod/")  # trailing slash
        assert not is_never_write(f"{_SET}/repo-0")
        assert not is_never_write(f"{_SET}/repo-1")

    def test_a_lone_repo_prod_redirects_to_its_repo_0_sibling(self):
        """Sabotage: delete the `is_never_write` branch in canonical_working_copy.

        The fallthrough `return best` then hands back repo-prod, because
        _clone_index('repo-prod') is None and the sibling probe skips
        None-index candidates. That is the live bug this test pins.
        """
        got = cwc([f"{_SET}/repo-prod"], isdir=_clone_set_on_disk)
        assert got == f"{_SET}/repo-0"

    def test_repo_prod_never_wins_even_when_it_is_the_first_candidate(self):
        got = cwc(
            [f"{_SET}/repo-prod", f"{_SET}/repo-1"], isdir=_clone_set_on_disk
        )
        assert got == f"{_SET}/repo-0"

    def test_no_sibling_means_refuse_rather_than_guess(self):
        """An empty answer makes the caller refuse. Guessing a directory is worse."""
        assert cwc([f"{_SET}/repo-prod"], isdir=_never) == ""

    def test_a_sibling_that_is_not_a_git_checkout_is_not_adopted(self):
        """A same-named directory is never written into. Only a real checkout."""
        no_git = lambda p: p in {f"{_SET}/repo-0", f"{_SET}/repo-prod"}  # noqa: E731
        assert redirect_never_write(f"{_SET}/repo-prod", isdir=no_git) == ""

    def test_primary_project_redirects_a_stored_repo_prod_path(self, tmp_path):
        """A real clone set on disk: repo-prod beside a git-checkout repo-0."""
        prod = tmp_path / "repo-prod"
        prod.mkdir()
        zero = tmp_path / "repo-0"
        (zero / ".git").mkdir(parents=True)
        learning = {"id": "L1", "primary_project_path": str(prod)}
        assert primary_project(learning) == str(zero)

    def test_primary_project_raises_when_repo_prod_has_no_sibling(self, tmp_path):
        prod = tmp_path / "repo-prod"
        prod.mkdir()
        learning = {"id": "L2", "primary_project_path": str(prod)}
        with pytest.raises(RoutingError) as exc:
            primary_project(learning)
        assert "must never be written to" in str(exc.value)

    def test_ordinary_paths_are_untouched(self):
        """The guard must not perturb the existing convention."""
        assert redirect_never_write(f"{_SET}/repo-0") == f"{_SET}/repo-0"
        assert cwc([f"{_SET}/repo-1", f"{_SET}/repo-0"], isdir=_never) == f"{_SET}/repo-0"


class TestTheSiblingMustBeTheSameRepo:
    """Verify repository identity before redirecting repo-prod to repo-0.

    Two unrelated checkouts can share a parent and have clone-style names.
    A .git directory alone does not establish that the sibling has the same
    upstream; the resolver must confirm the match before it is selected.
    """

    def test_a_sibling_with_a_different_remote_is_refused(self, tmp_path):
        prod = tmp_path / "repo-prod"
        (prod / ".git").mkdir(parents=True)
        zero = tmp_path / "repo-0"
        (zero / ".git").mkdir(parents=True)

        def other_repo(path):
            return "github:999" if path.endswith("repo-0") else "github:1"

        assert redirect_never_write(
            str(prod), resolver=other_repo, project_key="github:1"
        ) == "", "a sibling belonging to a DIFFERENT repo was adopted"

    def test_a_sibling_with_the_same_remote_is_adopted(self, tmp_path):
        prod = tmp_path / "repo-prod"
        (prod / ".git").mkdir(parents=True)
        zero = tmp_path / "repo-0"
        (zero / ".git").mkdir(parents=True)

        assert redirect_never_write(
            str(prod), resolver=lambda p: "github:1", project_key="github:1"
        ) == str(zero)

    def test_with_no_resolver_it_still_works_but_only_on_the_weaker_evidence(self, tmp_path):
        """primary_project has no project_key to check against. The weaker
        rule stays available, and the caller that HAS identity uses it."""
        prod = tmp_path / "repo-prod"
        (prod / ".git").mkdir(parents=True)
        zero = tmp_path / "repo-0"
        (zero / ".git").mkdir(parents=True)
        assert redirect_never_write(str(prod)) == str(zero)

    def test_a_resolver_that_raises_is_not_fatal(self, tmp_path):
        """A broken checkout must downgrade, never take routing down."""
        prod = tmp_path / "repo-prod"
        (prod / ".git").mkdir(parents=True)
        zero = tmp_path / "repo-0"
        (zero / ".git").mkdir(parents=True)

        def boom(_path):
            raise RuntimeError("git is on fire")

        assert redirect_never_write(
            str(prod), resolver=boom, project_key="github:1"
        ) == ""
