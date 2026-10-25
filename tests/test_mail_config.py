"""Tests for the mail-server configuration resolver.

The bug this guards against
---------------------------
`app/api/payroll.py` used to resolve the mail server with:

    mail_api = _ca.config.get(
        'MAIL_SERVER_API',
        os.environ.get('MAIL_SERVER_API', 'https://mail.example.com:5003'),
    )

`example.com` is a reserved documentation domain (RFC 2606) that can
never be a real mail server. As a silent fallback it meant a deployment
that had not set MAIL_SERVER_API dialled a host that cannot exist, and
the failure surfaced as an opaque per-recipient delivery error rather
than a configuration error.

The resolver in app/shared/mail_config.py has no default at all: it
raises MailServerNotConfigured naming the variable to set, and refuses
a configuration still pointing at a documentation domain.

These are pure-function tests -- no database, no Flask app.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.shared.mail_config import (  # noqa: E402
    MailServerNotConfigured,
    host_of,
    is_placeholder_host,
    resolve_mail_server,
)


GOOD = {
    'MAIL_SERVER_API': 'https://mail.corp-host:5003',
    'MAIL_SERVER_USER': 'mailbox@example.test',
    'MAIL_SERVER_PASSWORD': 'secret',
}


class TestHostOf:
    def test_extracts_hostname(self):
        assert host_of('https://mail.example.org:5003/path') == 'mail.example.org'

    def test_lowercases(self):
        assert host_of('https://MAIL.Example.COM') == 'mail.example.com'

    def test_unparseable_returns_empty(self):
        assert host_of('not a url') == ''


class TestIsPlaceholderHost:
    @pytest.mark.parametrize('url', [
        'https://mail.example.com:5003',
        'https://example.com',
        'https://mail.example.org',
        'https://mail.example.net',
        'https://mail.invalid',
        'https://mail.corp-host.test:5003',
        'https://mail.corp-host.example:5003',
    ])
    def test_documentation_domains_are_placeholders(self, url):
        assert is_placeholder_host(url) is True

    @pytest.mark.parametrize('url', [
        'https://mail.corp-host:5003',
        'https://203.0.113.5:5003',   # RFC 5737 TEST-NET-3
        'https://localhost:5003',
        'https://mail.example.com.au:5003',
        'https://mail.corp-host.internal:5003',
    ])
    def test_real_hosts_are_not_placeholders(self, url):
        assert is_placeholder_host(url) is False


class TestResolveMailServer:
    def test_resolves_a_complete_configuration(self):
        s = resolve_mail_server(GOOD, {})
        assert s.api == 'https://mail.corp-host:5003'
        assert s.user == 'mailbox@example.test'
        assert s.password == 'secret'
        assert s.ca_bundle == ''

    def test_strips_trailing_slash_from_api(self):
        cfg = dict(GOOD, MAIL_SERVER_API='https://mail.corp-host:5003/')
        assert resolve_mail_server(cfg, {}).api == 'https://mail.corp-host:5003'

    def test_reads_ca_bundle(self):
        cfg = dict(GOOD, MAIL_SERVER_CA_BUNDLE='/etc/ssl/mail.crt')
        assert resolve_mail_server(cfg, {}).ca_bundle == '/etc/ssl/mail.crt'

    def test_config_takes_precedence_over_environ(self):
        env = dict(GOOD, MAIL_SERVER_API='https://env.example.test')
        assert resolve_mail_server(GOOD, env).api == GOOD['MAIL_SERVER_API']

    def test_falls_back_to_environ(self):
        assert resolve_mail_server({}, GOOD).api == GOOD['MAIL_SERVER_API']

    # -- the failure modes that used to be silent --------------------------

    def test_unset_api_raises_and_names_the_variable(self):
        with pytest.raises(MailServerNotConfigured) as exc:
            resolve_mail_server({}, {})
        assert 'MAIL_SERVER_API' in str(exc.value)

    def test_unset_api_says_it_is_unset_not_placeholder(self):
        """The resolver must not have a DEFAULT host at all.

        If it fell back to a placeholder, the unset case would be reported
        as "still points at the documentation placeholder" -- true, but it
        would mean the resolver invents a host instead of demanding one.
        The wording distinguishes the two.
        """
        with pytest.raises(MailServerNotConfigured) as exc:
            resolve_mail_server({}, {})
        msg = str(exc.value)
        assert 'is not set' in msg, msg
        assert 'placeholder' not in msg, msg

    def test_placeholder_api_is_refused(self):
        cfg = dict(GOOD, MAIL_SERVER_API='https://mail.example.com:5003')
        with pytest.raises(MailServerNotConfigured) as exc:
            resolve_mail_server(cfg, {})
        msg = str(exc.value)
        assert 'MAIL_SERVER_API' in msg
        assert 'mail.example.com' in msg

    def test_non_https_api_is_refused(self):
        cfg = dict(GOOD, MAIL_SERVER_API='http://mail.corp-host:5003')
        with pytest.raises(MailServerNotConfigured) as exc:
            resolve_mail_server(cfg, {})
        assert 'https' in str(exc.value)

    def test_missing_user_raises(self):
        cfg = dict(GOOD)
        cfg.pop('MAIL_SERVER_USER')
        with pytest.raises(MailServerNotConfigured) as exc:
            resolve_mail_server(cfg, {})
        assert 'MAIL_SERVER_USER' in str(exc.value)

    def test_missing_password_raises(self):
        cfg = dict(GOOD)
        cfg.pop('MAIL_SERVER_PASSWORD')
        with pytest.raises(MailServerNotConfigured) as exc:
            resolve_mail_server(cfg, {})
        assert 'MAIL_SERVER_PASSWORD' in str(exc.value)

    def test_empty_string_is_treated_as_unset(self):
        cfg = dict(GOOD, MAIL_SERVER_API='')
        with pytest.raises(MailServerNotConfigured):
            resolve_mail_server(cfg, {})

    def test_no_placeholder_default_is_ever_returned(self):
        """The resolver must never invent a host."""
        with pytest.raises(MailServerNotConfigured):
            resolve_mail_server({}, {})
