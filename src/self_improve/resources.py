"""Paths for source checkouts and ordinary installed wheels.

The source tree owns the editable assets. Wheel builds copy those same bytes
under the package; an installed library is never treated as a source checkout.
"""
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
_candidate = PACKAGE_ROOT.parent.parent
SOURCE_ROOT = (
    _candidate
    if PACKAGE_ROOT.parent.name == "src" and (_candidate / "pyproject.toml").is_file()
    else None
)


def bundled_path(*parts: str) -> Path:
    """Locate a shipped asset; missing files remain errors at their reader."""
    root = SOURCE_ROOT if SOURCE_ROOT is not None else PACKAGE_ROOT / "_assets"
    return root.joinpath(*parts)
