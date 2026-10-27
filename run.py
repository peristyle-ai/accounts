#!/usr/bin/env python3
"""
Entry point for the Accounting System application.

Initializes Flask and starts both the API server (port 5061) and the HMI
server (port 5062) — either together (default) or one at a time.

Fail-fast on port conflict: if either port is already bound when we try to
bind, the whole process exits with code 2 so systemd's StartLimitBurst=10
stops the restart-loop. Otherwise the daemon threads die cleanly (rc=0),
which `Restart=always` happily restarts — that catch-all is preserved for
the 2026-08-27 daemon-thread-crash case.

Clean shutdown: SIGTERM/SIGINT triggers server.shutdown() on both servers
so ports are released promptly, avoiding the race where systemd's restart
sends SIGTERM to a process that takes its sweet time releasing sockets.
"""

import errno
import os
import signal
import socket
import sys
import argparse
from threading import Thread

from dotenv import load_dotenv

load_dotenv()

from config import accounts_config

from app import create_app
from app.models import db
from werkzeug.serving import make_server


# Exit code we use for "port collision" so systemd's StartLimitBurst caps
# the retry spam and operators see the service land in 'failed' state.
EXIT_PORT_IN_USE = 2


def init_database(app, apply_schema=False):
    """Ensure the tables exist, then seed the default settings.

    apply_schema=False (the normal boot path): db.create_all() only.
    It skips tables that already exist, so it never touches a live
    schema beyond adding a column the models gained.

    apply_schema=True (`run.py --init-db`): schema/init.sql is applied
    FIRST, then db.create_all() fills in anything init.sql does not
    define. The order matters -- with create_all() first, every
    `CREATE TABLE IF NOT EXISTS` in init.sql is a no-op, so the inline
    constraints (uq_pay_event_bank_ref), the table comments and the
    trigger functions were silently skipped and the installed schema
    did not match the schema the models describe.

    Applying init.sql needs the DB OWNER's credentials: it creates
    schemas, tables, functions and triggers. The runtime account
    normally has DML rights only, so this is an install-time operation,
    not something a service restart may attempt.
    """
    if apply_schema:
        # Imported here (not at module scope) so that init_db reads
        # DATABASE_URL after load_dotenv() has run.
        import init_db as _init_db
        _init_db.init_db(mode="schema")

    with app.app_context():
        db.create_all()
        print("Database tables created successfully.")
        from app.models.system_setting import seed_defaults
        seed_defaults(app)
        print("Default settings seeded.")


def _make_flask_server(host, port, app, label):
    """Bind+listen ourselves, then hand the socket fd to werkzeug so it
    skips its own bind_and_activate — and thus its `sys.exit(1)` failure
    path. That silent exit was turning EADDRINUSE into an infinite restart
    loop (systemd Restart=always + StartLimitBurst never tripping, because
    bind conflicts yielded exit code 1 with no operator-visible failure
    state). Pre-binding lets us fail loud with exit code 2.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(64)  # werkzeug default request_queue_size; doesn't matter much since threaded=True
    except OSError as e:
        sock.close()
        if e.errno == errno.EADDRINUSE:
            print(
                f"FATAL [{label}]: port {port} already in use by another "
                f"process. Run `ss -tlnp 'sport = :{port}'` to find the "
                f"holder. Exiting {EXIT_PORT_IN_USE} so systemd stops "
                f"auto-restarting.",
                file=sys.stderr,
                flush=True,
            )
            os._exit(EXIT_PORT_IN_USE)
        raise
    sock_fd = sock.fileno()
    # Hand the bound+listening socket to werkzeug via its `fd=` param.
    # When fd is passed, BaseWSGIServer.__init__ skips its own bind/activate
    # and just wraps our socket via socket.fromfd.
    server = make_server(host, port, app, threaded=True, fd=sock_fd)
    # Close our local handle; werkzeug's fromfd duplicated the fd, our copy
    # is no longer needed.
    sock.close()
    return server


def _serve(server, label):
    """Block on the WSGI server until shutdown() is called. Exits non-zero
    if the port is yanked out from under us mid-serve."""
    print(f"Starting {label} server on {server.host}:{server.port}", flush=True)
    try:
        server.serve_forever()
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(
                f"FATAL [{label}]: port {server.port} became in-use during "
                f"serve_forever. Exiting {EXIT_PORT_IN_USE}.",
                file=sys.stderr,
                flush=True,
            )
            os._exit(EXIT_PORT_IN_USE)
        raise
    print(f"{label} server stopped.", flush=True)


def _install_signal_handlers(servers):
    """SIGTERM/SIGINT → server.shutdown() on every server. Called from the
    main thread before threading starts so port release is prompt."""
    def _shutdown_all(*_):
        for s in servers:
            try:
                s.shutdown()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, _shutdown_all)
    signal.signal(signal.SIGINT, _shutdown_all)


def main():
    parser = argparse.ArgumentParser(description='Accounting System')
    parser.add_argument('--config', '-c', default='config.yaml',
                        help='Path to configuration file')
    parser.add_argument('--init-db', action='store_true',
                        help='Initialize database tables only')
    parser.add_argument('--api-only', action='store_true',
                        help='Run API server only')
    parser.add_argument('--hmi-only', action='store_true',
                        help='Run HMI server only')
    parser.add_argument('--api-port', type=int, default=None,
                        help='API server port. Overrides app.api_port from '
                             'config.yaml. Default: app.api_port in '
                             'config.yaml, else 5061.')
    parser.add_argument('--hmi-port', type=int, default=None,
                        help='HMI server port. Overrides app.hmi_port from '
                             'config.yaml. Default: app.hmi_port in '
                             'config.yaml, else 5062.')
    parser.add_argument('--host', default=None,
                        help='Host address to bind. Resolution order: '
                             '--host flag > API_HOST env var > app.host in '
                             'config.yaml > 127.0.0.1. Set to 0.0.0.0 to '
                             'bind on all interfaces.')

    args = parser.parse_args()

    config_path = args.config
    config_exists = os.path.exists(config_path)
    if not config_exists:
        print(
            f"WARNING: config file '{config_path}' not found.\n"
            f"         Continuing with built-in defaults (schema 'public',\n"
            f"         default ports). Copy config.yaml.example to config.yaml\n"
            f"         and edit it, or pass --config <path>."
        )

    app = create_app(config_path if config_exists else None)

    # Ports: CLI flag > config.yaml (app.api_port / app.hmi_port) > default.
    # One source of truth (config.yaml), so the example file, the docs and
    # the running service cannot drift apart.
    api_port = args.api_port or accounts_config.get('app', 'api_port', default=5061)
    hmi_port = args.hmi_port or accounts_config.get('app', 'hmi_port', default=5062)

    if args.init_db:
        # Install-time operation: needs the DB owner's credentials.
        init_database(app, apply_schema=True)
        print("Database initialization complete.")
        return

    # Normal boot: make sure the tables exist, but do NOT attempt to
    # install the schema. The service account has DML rights, not DDL.
    init_database(app)

    # Bind address resolution (2026-10-28): the --host flag, the API_HOST
    # env var and the app.host config key were ALL documented -- in
    # run.py --help, in systemd/accounts-standalone.service and in
    # coding_agents/SPEC_AND_REQUIREMENTS.md -- but only the --host flag
    # was ever read. A doc that describes a contract the code does not
    # honour is worse than no doc: an operator who set API_HOST=0.0.0.0
    # to reach the API from the LAN got a service that still bound to
    # 127.0.0.1. Now all three are honoured, most specific first.
    host = (args.host
            or os.environ.get('API_HOST')
            or accounts_config.get('app', 'host', default='127.0.0.1'))

    if args.api_only:
        server = _make_flask_server(host, api_port, app, 'API')
        _install_signal_handlers([server])
        _serve(server, 'API')
        return

    if args.hmi_only:
        server = _make_flask_server(host, hmi_port, app, 'HMI')
        _install_signal_handlers([server])
        _serve(server, 'HMI')
        return

    # Both servers — bind BEFORE starting threads so a port conflict fails
    # the main process (with exit 2) instead of crashing a daemon thread.
    api_server = _make_flask_server(host, api_port, app, 'API')
    hmi_server = _make_flask_server(host, hmi_port, app, 'HMI')
    servers = [api_server, hmi_server]

    _install_signal_handlers(servers)

    api_thread = Thread(target=_serve, args=(api_server, 'API'), daemon=True)
    hmi_thread = Thread(target=_serve, args=(hmi_server, 'HMI'), daemon=True)
    api_thread.start()
    hmi_thread.start()

    print("Both servers started. SIGTERM/SIGINT for clean shutdown.", flush=True)

    api_thread.join()
    hmi_thread.join()


if __name__ == '__main__':
    main()
