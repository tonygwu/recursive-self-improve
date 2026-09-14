"""Opt-in probe of installed CLIs; not part of the synthetic test environment."""
import os

import pytest


@pytest.mark.reads_real_home
def test_the_installed_providers_can_start():
    if os.environ.get("SELFIMPROVE_PROVIDER_PREFLIGHT") != "1":
        pytest.skip("installed provider probe requires SELFIMPROVE_PROVIDER_PREFLIGHT=1")
    from self_improve.config import load_config
    from self_improve.pipeline import provider_preflight

    got = provider_preflight(load_config())
    assert got["any_usable"] is True, got["unusable"]
