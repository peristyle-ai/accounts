"""Guard rails for the open-source scrub of this repository.

Context (2026-10-24): `config.yaml` was TRACKED in git while holding a live
database password and a live Flask SECRET_KEY. The `.gitignore` rule for it
existed, but the file had been committed before the rule did, and git keeps
tracking a file it has already committed -- so the ignore was inert.

These tests make the scrub self-enforcing. If anyone re-adds a secret-bearing
file, the suite fails at commit time rather than at publication time.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_tracked_files():
    out = subprocess.run(
        ['git', '-C', str(REPO_ROOT), 'ls-files'],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.split('\n')


@pytest.mark.skipif(not (REPO_ROOT / '.git').exists(), reason='not a git checkout')
def test_config_yaml_is_not_tracked():
    """config.yaml holds credentials; it must never be committed."""
    tracked = _git_tracked_files()
    assert 'config.yaml' not in tracked, (
        'config.yaml is tracked in git - it holds the DB password and '
        'SECRET_KEY. Run: git rm --cached config.yaml'
    )


@pytest.mark.skipif(not (REPO_ROOT / '.git').exists(), reason='not a git checkout')
def test_env_is_not_tracked():
    """.env holds every credential in one place; it must never be committed."""
    assert '.env' not in _git_tracked_files(), '.env is tracked in git - remove it'


@pytest.mark.skipif(not (REPO_ROOT / '.git').exists(), reason='not a git checkout')
def test_config_yaml_example_is_tracked():
    """The template IS the public artefact - it must be present."""
    assert 'config.yaml.example' in _git_tracked_files(), (
        'config.yaml.example should be tracked so a fresh install can copy it'
    )


def test_config_yaml_example_has_no_real_credentials():
    """The template must ship placeholders only."""
    import yaml
    text = (REPO_ROOT / 'config.yaml.example').read_text()
    cfg = yaml.safe_load(text)

    secret = (cfg.get('app') or {}).get('secret_key') or ''
    assert secret.startswith('change-me'), (
        f'config.yaml.example must not ship a real secret_key (got {secret[:12]}...)'
    )

    # A password may be present but must be empty or an obvious placeholder.
    password = (cfg.get('database') or {}).get('password')
    assert password in (None, '', 'your_password_here'), (
        'config.yaml.example must not ship a real database password'
    )

    # No peristyle identifiers: real ABN, real employer id, real member no.
    for token in ('53000000022', '123456789', '123456789', '12345678',
                  'peristyle', 'Peristyle', 'app-host'):
        assert token not in text, (
            f'config.yaml.example contains a Peristyle-specific value: {token!r}'
        )


def test_placeholder_secret_keys_are_refused_at_startup():
    """A fresh install must not run with the published placeholder key."""
    from app import _PLACEHOLDER_SECRET_KEYS
    assert 'change-me-generate-a-random-32-byte-hex-key' in _PLACEHOLDER_SECRET_KEYS
    assert 'dev-secret-key' in _PLACEHOLDER_SECRET_KEYS


def test_secret_key_env_wins_over_config_yaml(monkeypatch):
    """The environment must win, so the secret can live in .env only."""
    monkeypatch.setenv('SECRET_KEY', 'a' * 64)
    from app import create_app
    app = create_app(None)
    assert app.config['SECRET_KEY'] == 'a' * 64


def test_placeholder_secret_key_is_rejected(monkeypatch):
    """A config.yaml carrying the published placeholder must not boot."""
    monkeypatch.delenv('SECRET_KEY', raising=False)
    import tempfile
    from pathlib import Path as P
    with tempfile.TemporaryDirectory() as tmp:
        cfg = P(tmp) / 'config.yaml'
        cfg.write_text(
            'database:\n  host: localhost\n  port: 5432\n  name: x\n  user: x\n'
            'app:\n  secret_key: "change-me-generate-a-random-32-byte-hex-key"\n'
        )
        from app import create_app
        with pytest.raises(RuntimeError, match='SECRET_KEY'):
            create_app(str(cfg))
