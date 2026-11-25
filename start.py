"""Explicit local launcher, credential setup and database maintenance commands."""

import argparse
import getpass
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from backend.config import WEB_DIST, AppConfig, AuthConfig
from backend.infrastructure.authfile import DEFAULT_AUTH_FILE, AuthFileError, load_auth, save_auth
from backend.infrastructure.backup import create_backup, list_backups, daily_backup
from backend.infrastructure.binding import load_binding
from backend.infrastructure.database import (
    DEFAULT_BINDING,
    DEFAULT_DATABASE,
    DatabaseError,
    DatabaseSettings,
    absolute_path,
    adopt_database,
    initialize_database,
    inspect_database,
    read_identity,
    upgrade_database,
)
from backend.infrastructure.restore import check_backup, restore


def _identity_args(parser: argparse.ArgumentParser, *, local: bool = False) -> None:
    parser.add_argument('--database', type=Path, required=local, default=None)
    # Formal commands read both from the published binding; a local sandbox derives them
    # from its own file. `adopt-database` still demands all three explicitly.
    parser.add_argument('--uuid', default=None)
    parser.add_argument('--generation', default=None, type=int)
    if local:
        parser.add_argument('--official-database', type=Path)
        parser.add_argument('--writable-sandbox', action='store_true')


def _sandbox_identity(path: Path) -> tuple[str, int]:
    """Read the sandbox's own identity; a standalone database needs no formal ledger."""
    if not path.is_file():
        raise DatabaseError(f'{path} does not exist; run init-database --role sandbox first')
    try:
        conn = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise DatabaseError(f'cannot open {path} read-only for identity check') from exc
    try:
        identity = read_identity(conn)
    finally:
        conn.close()
    if identity.instance_role != 'sandbox':
        raise DatabaseError('a writable local sandbox requires a sandbox-role database, '
                            'never the formal ledger')
    return identity.database_uuid, identity.generation


def _local_settings(args: argparse.Namespace) -> DatabaseSettings:
    """Build a local session; a standalone sandbox never needs the formal binding."""
    try:
        bound = load_binding(DEFAULT_BINDING)
    except DatabaseError:
        bound = None
    path = absolute_path(args.database)
    if bound is not None:
        official = bound.path
        if args.official_database is not None and absolute_path(args.official_database) != official:
            raise DatabaseError('official path conflicts with binding')
        uuid, generation = args.uuid or bound.uuid, args.generation or bound.generation
    else:
        # No formal ledger exists yet. Nothing about the sandbox depends on one: its
        # identity comes from the sandbox file itself, and the default formal path is
        # only compared with so the two can never be the same file.
        official = absolute_path(args.official_database or DEFAULT_DATABASE)
        if args.uuid is not None and args.generation is not None:
            uuid, generation = args.uuid, args.generation
        else:
            uuid, generation = _sandbox_identity(path)
    return DatabaseSettings(path=path, expected_uuid=uuid,
                            generation=args.generation or generation, official_path=official,
                            mode='local', writable_sandbox=args.writable_sandbox)


def _server_settings(args: argparse.Namespace) -> DatabaseSettings:
    binding = load_binding(DEFAULT_BINDING)
    return DatabaseSettings(path=args.database or binding.path,
                            expected_uuid=args.uuid if args.uuid is not None else binding.uuid,
                            generation=args.generation if args.generation is not None else binding.generation,
                            official_path=binding.path, mode='server')


def _restore_settings(args: argparse.Namespace) -> DatabaseSettings:
    """Formal settings for restore; --database keeps the binding published beside that file.

    A missing binding still permits `restore-check` to inspect a backup, but a real restore
    then stops at the pre-restore backup: adopt-database owns rebuilding a lost binding.
    """
    binding_path = DEFAULT_BINDING
    try:
        binding = load_binding(DEFAULT_BINDING)
        path, uuid, generation = binding.path, binding.uuid, binding.generation
    except DatabaseError:
        path, uuid, generation = absolute_path(DEFAULT_DATABASE), str(uuid4()), 1
    if args.database is not None:
        # An explicit target carries its own binding beside it, exactly like adopt-database.
        path = absolute_path(args.database)
        binding_path = path.parent / DEFAULT_BINDING.name
        try:
            beside = load_binding(binding_path)
        except DatabaseError:
            pass
        else:
            uuid, generation = beside.uuid, beside.generation
    return DatabaseSettings(path=path, expected_uuid=uuid, generation=generation,
                            official_path=path, binding_path=binding_path, mode='server')


def main() -> int:
    parser = argparse.ArgumentParser(description='Stock Manager database and loopback launcher')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init-database', help='explicitly create a new database with Alembic')
    init.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    init.add_argument('--role', choices=('formal', 'sandbox'), default='formal')
    init.add_argument('--official-database', type=Path)
    init.add_argument('--confirm', action='store_true')
    for mode in ('server', 'local'):
        serve = commands.add_parser(mode, help=f'run {mode} backend on loopback')
        _identity_args(serve, local=mode == 'local')
        serve.add_argument('--port', type=int, default=9091)
        serve.add_argument('--near-stop-bps', type=int, choices=range(0,1001), default=200, metavar='0-1000')
        serve.add_argument('--market-refresh-seconds', type=int, choices=range(10,301), default=20, metavar='10-300')
        serve.add_argument('--no-market-refresh', action='store_true', help='disable automatic network refresh')
        serve.add_argument('--auth-file', type=Path, default=DEFAULT_AUTH_FILE)
        serve.add_argument('--static-dir', type=Path, default=WEB_DIST,
                           help='built browser application directory (default frontend/dist)')
        serve.add_argument('--allow-missing-web-build', action='store_true',
                           help='start the API only, reporting the missing browser build')
    password = commands.add_parser('set-password', help='create or replace the local operator credential file')
    password.add_argument('--auth-file', type=Path, default=DEFAULT_AUTH_FILE)
    password.add_argument('--username', default='owner')
    password.add_argument('--host', action='append', dest='hosts', metavar='HOST:PORT')
    password.add_argument('--origin', action='append', dest='origins', metavar='SCHEME://HOST:PORT')
    password.add_argument('--port', type=int, default=9091)
    password.add_argument('--insecure-cookie', action='store_true',
                          help='only for loopback testing without TLS')
    check = commands.add_parser('check-database', help='validate formal identity and writer access; no migration')
    _identity_args(check)
    backup = commands.add_parser('backup', help='create a new consistent formal database backup directory')
    _identity_args(backup)
    backup.add_argument('--destination', required=True, type=Path)
    daily = commands.add_parser('backup-daily', help='verified daily snapshot and conservative retention')
    _identity_args(daily)
    daily.add_argument('--destination', type=Path, default=None)
    adopt = commands.add_parser('adopt-database', help='explicitly bind an existing formal database')
    _identity_args(adopt)
    adopt.add_argument('--confirm', action='store_true')
    upgrade = commands.add_parser('upgrade-database', help='backup and explicitly migrate bound formal database')
    _identity_args(upgrade)
    upgrade.add_argument('--destination', type=Path, required=True)
    upgrade.add_argument('--confirm', action='store_true')
    restore_check = commands.add_parser('restore-check',
                                       help='verify a backup directory before any restore')
    restore_check.add_argument('--source', type=Path, required=True)
    restore_check.add_argument('--database', type=Path, default=None,
                              help='formal database path (default: the published binding)')
    restore_command = commands.add_parser('restore',
                                         help='replace the formal database with a verified backup')
    restore_command.add_argument('--source', type=Path, required=True)
    restore_command.add_argument('--database', type=Path, default=None,
                                 help='formal database path (default: the published binding)')
    restore_command.add_argument('--report', type=Path, default=None,
                                 help='also write the JSON report to this file')
    restore_command.add_argument('--confirm', action='store_true')
    take = commands.add_parser('takeover', help='review or publish an accepted sandbox migration offline')
    take.add_argument('--target', type=Path, required=True)
    take.add_argument('--report', type=Path, required=True)
    take.add_argument('--approval')
    take.add_argument('--isolated', action='store_true')
    rollback = commands.add_parser('takeover-rollback', help='recover interrupted takeover offline')
    rollback.add_argument('--confirm', action='store_true')
    rollback.add_argument('--isolated', action='store_true')
    status = commands.add_parser('backup-status', help='list the backups published in a directory')
    status.add_argument('--destination', type=Path, required=True)
    commands.add_parser('client', help='browser only; no local database or backend')
    args = parser.parse_args()
    try:
        if args.command == 'client':
            print('Client mode is browser-only; open the private HTTPS address of the formal '
                  'service on the company machine.', file=sys.stderr)
            return 2
        if args.command == 'init-database':
            identity = initialize_database(args.database, role=args.role, confirm=args.confirm,
                                           official_path=args.official_database)
            print(json.dumps(asdict(identity)))
            return 0
        if args.command == 'set-password':
            from backend.security.passwords import hash_password
            password_value = getpass.getpass('Password: ')
            if not password_value or password_value != getpass.getpass('Repeat: '):
                raise AuthFileError('passwords do not match')
            hosts = tuple(args.hosts) if args.hosts else (f'127.0.0.1:{args.port}', f'localhost:{args.port}')
            save_auth(args.auth_file, AuthConfig(
                username=args.username, password_hash=hash_password(password_value),
                allowed_hosts=hosts, allowed_origins=tuple(args.origins or ()),
                cookie_secure=not args.insecure_cookie))
            print(f'credentials written to {args.auth_file}')
            return 0
        if args.command == 'adopt-database':
            if args.database is None or args.uuid is None or args.generation is None:
                raise DatabaseError('adoption requires explicit path, UUID and generation')
            config = DatabaseSettings(path=args.database, official_path=args.database,
                                      expected_uuid=args.uuid, generation=args.generation)
            print(json.dumps(asdict(adopt_database(config, confirm=args.confirm))))
            return 0
        if args.command == 'takeover':
            from backend.infrastructure.takeover import takeover
            print(json.dumps(takeover(_restore_settings(argparse.Namespace(database=None)), args.target,
                                      args.report, approval=args.approval, isolated=args.isolated)))
            return 0
        if args.command == 'takeover-rollback':
            from backend.infrastructure.takeover import rollback_pending
            print(json.dumps(rollback_pending(DEFAULT_BINDING, confirm=args.confirm, isolated=args.isolated)))
            return 0
        if args.command == 'backup-status':
            print(json.dumps(list_backups(args.destination), indent=2, ensure_ascii=False))
            return 0
        if args.command == 'restore-check':
            verdict = check_backup(_restore_settings(args), args.source)
            print(json.dumps({key: verdict[key] for key in ('ok', 'reason', 'counts', 'checks')},
                             indent=2, ensure_ascii=False))
            return 0 if verdict['ok'] else 1
        if args.command == 'restore':
            report = restore(_restore_settings(args), args.source, confirm=args.confirm,
                             destination_report=args.report)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        local = args.command == 'local'
        config = _local_settings(args) if local else _server_settings(args)
        if args.command == 'upgrade-database':
            upgrade_database(config, args.destination, confirm=args.confirm)
            return 0
        if args.command == 'backup-daily':
            print(json.dumps(daily_backup(config,args.destination or config.path.parent/'backups')))
            return 0
        if args.command == 'backup':
            print(create_backup(config, args.destination))
            return 0
        if args.command == 'check-database':
            with inspect_database(config) as db:
                print(json.dumps(asdict(db.identity)))
            return 0

        import uvicorn

        from backend.main import create_app, web_root

        auth = load_auth(args.auth_file)
        root = web_root(args.static_dir)
        if root is None and not args.allow_missing_web_build:
            raise AuthFileError(f'built browser application missing at {args.static_dir}; '
                                'run npm ci && npm run build in frontend/, or pass '
                                '--allow-missing-web-build to serve the API alone')
        settings = AppConfig(
            near_stop_bps=args.near_stop_bps,
            market_auto_refresh=not args.no_market_refresh,
            market_refresh_seconds=args.market_refresh_seconds,
            static_dir=root, require_write_identity=True, auth=auth)
        server = uvicorn.Server(uvicorn.Config(create_app(settings, database=config),
                                               host='127.0.0.1', port=args.port, workers=1,
                                               lifespan='on', proxy_headers=False,
                                               server_header=False))
        server.run()
        return 0 if server.started else 1
    except (DatabaseError, AuthFileError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print(f'Operation refused: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
