"""Regression tests for business-logo resolution in app/shared/pdf.py.

Context (2026-10-23): a customer invoice for the week of 19 Oct was sent with
NO logo. The cause was not a rendering bug -- `user.logo_path` is stored
relative to the app root ("uploads/logos/<uuid>.png") but the old code
resolved it against the process CWD, and the uploads/ directory had been
left behind by the repo rename, so `os.path.exists()` returned False and
the PDF shipped without an image.

These tests lock in the three behaviours that fix it:
  1. A relative logo_path resolves against the APP ROOT, not the CWD.
  2. When the stored file is genuinely missing, the configured seed asset
     (app.logo_fallback_path) is used, and a warning is logged -- so a lost
     upload can never again silently drop the logo.
  3. A business that never uploaded a logo does NOT get the fallback.

NOTE: these tests pin the fallback to the repo's NEUTRAL asset
(assets/logos/logo-fallback.png) rather than reading it from the ambient
config.yaml. The live deployment config points at the operator's own brand
asset, which is deliberately not tracked in git -- so a test that depended
on it would break for anyone who clones the repo.
"""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.shared import pdf as pdf_mod
from app.shared.pdf import _resolve_logo_path

NEUTRAL_FALLBACK = pdf_mod._REPO_ROOT / "assets" / "logos" / "logo-fallback.png"


@pytest.fixture(autouse=True)
def pin_fallback(monkeypatch):
    """Force the fallback to the repo's tracked neutral asset."""
    monkeypatch.setattr(pdf_mod, "_logo_fallback_from_config",
                        lambda: str(NEUTRAL_FALLBACK))
    yield


def _user(logo_path):
    return SimpleNamespace(id=69, logo_path=logo_path)


def test_relative_path_resolves_against_app_root_not_cwd(tmp_path, monkeypatch):
    """A relative logo_path must resolve against the app root even when the
    process CWD is somewhere else entirely."""
    assert NEUTRAL_FALLBACK.exists(), "seed logo asset missing from the repo"

    monkeypatch.chdir(tmp_path)
    resolved = _resolve_logo_path(_user("uploads/logos/whatever.png"))
    # The stored file does not exist, so we expect the fallback -- but the
    # important part is that resolution is CWD-independent.
    assert resolved is not None
    assert resolved == NEUTRAL_FALLBACK


def test_absolute_path_is_used_as_is(tmp_path):
    target = tmp_path / "logo.png"
    target.write_bytes(b"not-really-a-png-but-it-exists")
    assert _resolve_logo_path(_user(str(target))) == target


def test_no_logo_path_never_uses_fallback():
    """A business with no uploaded logo must not get the seed asset."""
    assert _resolve_logo_path(_user(None)) is None
    assert _resolve_logo_path(_user("")) is None


def test_missing_file_falls_back_and_warns(caplog):
    """The stored file is gone -> fall back to the seed AND say so loudly."""
    assert NEUTRAL_FALLBACK.exists()

    with caplog.at_level(logging.WARNING, logger="app.shared.pdf"):
        resolved = _resolve_logo_path(_user("uploads/logos/gone-missing.png"))

    assert resolved == NEUTRAL_FALLBACK
    assert any("not found" in r.message for r in caplog.records), (
        "expected a warning naming the missing logo file"
    )


def test_existing_stored_file_wins_over_fallback():
    """When the uploaded file IS present it is preferred over the seed."""
    stored = pdf_mod._REPO_ROOT / "assets" / "logos" / "logo-fallback.png"
    resolved = _resolve_logo_path(_user(str(stored)))
    assert resolved == stored


def test_tracked_fallback_asset_is_neutral():
    """The repo's default fallback must be the NEUTRAL asset, not a brand."""
    tracked = (pdf_mod._REPO_ROOT / "assets" / "logos" / "logo-fallback.png")
    assert tracked.exists()
    assert tracked.stat().st_size < 50_000, (
        "the neutral fallback should be a small placeholder, not a real logo"
    )
