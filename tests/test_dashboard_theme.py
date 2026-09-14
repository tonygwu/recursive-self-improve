"""Readable themes and self-contained font distribution, without a network."""
import hashlib
import json
import re
from pathlib import Path

from self_improve import dashboard

STATIC = Path(dashboard.__file__).parent / "static"


def test_font_urls_resolve_to_intact_bundled_assets_and_licenses():
    manifest = json.loads((STATIC / "fonts/manifest.json").read_text())
    urls = re.findall(r'url\("([^"]+)"\)', (STATIC / "tokens.css").read_text())
    assert urls
    for url in urls:
        assert url.startswith("fonts/") and ".." not in url
        path = STATIC / url
        assert path.read_bytes()[:4] in (b"wOFF", b"wOF2")
        assert path.name in manifest
    for name, meta in manifest.items():
        raw = (STATIC / "fonts" / name).read_bytes()
        assert len(raw) == meta["bytes"]
        assert hashlib.sha256(raw).hexdigest() == meta["sha256"]
    for name in ("Inter-LICENSE.txt", "FiraMono-LICENSE.txt"):
        assert "SIL OPEN FONT LICENSE Version 1.1" in (STATIC / "fonts" / name).read_text()


def _contrast(a, b):
    def luminance(value):
        rgb = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * w for v, w in zip(linear, (.2126, .7152, .0722)))
    values = sorted((luminance(a), luminance(b)))
    return (values[1] + .05) / (values[0] + .05)


def test_all_theme_text_and_status_inks_retain_readable_contrast():
    css = (STATIC / "tokens.css").read_text()
    light = re.search(r":root \{(.*?)\n\}", css, re.S)[1]
    dark = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S)[1]
    system = re.search(r':root:not\(\[data-theme="light"\]\) \{(.*?)\n  \}', css, re.S)[1]
    def colors(block):
        return dict(re.findall(r"(--c-[\w-]+):\s*(#[0-9a-f]{6});", block))
    assert colors(system) == colors(dark)
    for theme in (colors(light), colors(light) | colors(dark)):
        surfaces = [theme[k] for k in ("--c-bg-canvas", "--c-bg-surface", "--c-bg-surface-raised", "--c-bg-subtle", "--c-accent-subtle")]
        for key in ("--c-text-primary", "--c-text-secondary", "--c-text-muted", "--c-accent"):
            for surface in surfaces:
                assert _contrast(theme[key], surface) >= 4.5, (key, surface)
        for key in (k for k in theme if k.startswith("--c-status-") and not k.endswith("-subtle")):
            for surface in [theme["--c-bg-surface"], theme.get(key + "-subtle", theme["--c-bg-surface"])]:
                assert _contrast(theme[key], surface) >= 4.5, (key, surface)
        assert _contrast(theme["--c-border-focus"], theme["--c-bg-surface"]) >= 3
