"""Explicit local launcher and database maintenance commands."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys

from backend.infrastructure.backup import create_backup
from backend.infrastructure.binding import load_binding
from backend.infrastructure.database import (
    DEFAULT_DATABASE, DEFAULT_BINDING, DatabaseError, DatabaseSettings, initialize_database, open_database, adopt_database, upgrade_database,
    absolute_path,
)


def _identity_args(parser: argparse.ArgumentParser, *, local: bool = False) -> None:
    parser.add_argument('--database', type=Path, required=local, default=None)
    parser.add_argument('--uuid', required=local)
    parser.add_argument('--generation', required=local, type=int)
    if local:
        parser.add_argument('--official-database', type=Path)
        parser.add_argument('--writable-sandbox', action='store_true')


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
        serve.add_argument('--market-refresh-seconds', type=int, choices=range(10,301), default=20, metavar='10-300')
        serve.add_argument('--no-market-refresh', action='store_true', help='disable automatic network refresh')
    check = commands.add_parser('check-database', help='validate formal identity and writer access; no migration')
    _identity_args(check)
    backup = commands.add_parser('backup', help='create a new consistent formal database backup directory')
    _identity_args(backup)
    backup.add_argument('--destination', required=True, type=Path)
    adopt = commands.add_parser('adopt-database', help='explicitly bind an existing formal database')
    _identity_args(adopt)
    adopt.add_argument('--confirm', action='store_true')
    upgrade = commands.add_parser('upgrade-database', help='backup and explicitly migrate bound formal database')
    _identity_args(upgrade)
    upgrade.add_argument('--destination', type=Path, required=True)
    upgrade.add_argument('--confirm', action='store_true')
    commands.add_parser('client', help='browser only; no local database or backend')
    args = parser.parse_args()
    try:
        if args.command == 'client':
            print('Client mode is browser-only; remote access is not implemented.', file=sys.stderr)
            return 2
        if args.command == 'init-database':
            identity = initialize_database(args.database, role=args.role, confirm=args.confirm,
                                           official_path=args.official_database)
            print(json.dumps(asdict(identity)))
            return 0
        if args.command == 'adopt-database':
            if args.database is None or args.uuid is None or args.generation is None:
                raise DatabaseError('adoption requires explicit path, UUID and generation')
            config = DatabaseSettings(path=args.database, official_path=args.database,
                                      expected_uuid=args.uuid, generation=args.generation)
            print(json.dumps(asdict(adopt_database(config, confirm=args.confirm))))
            return 0
        binding = load_binding(DEFAULT_BINDING)
        local = args.command == 'local'
        if local and args.official_database is not None and absolute_path(args.official_database) != binding.path:
            raise DatabaseError('official path conflicts with binding')
        config = DatabaseSettings(path=args.database or binding.path, expected_uuid=args.uuid if args.uuid is not None else binding.uuid,
                                  generation=args.generation if args.generation is not None else binding.generation,
                                  official_path=binding.path, mode='local' if local else 'server',
                                  writable_sandbox=args.writable_sandbox if local else False)
        if args.command == 'upgrade-database':
            upgrade_database(config, args.destination, confirm=args.confirm)
            return 0
        if args.command == 'backup':
            print(create_backup(config, args.destination))
        elif args.command == 'check-database':
            with open_database(config) as db:
                print(json.dumps(asdict(db.identity)))
        else:
            import uvicorn
            from backend.main import create_app
            from backend.config import AppConfig

            server = uvicorn.Server(uvicorn.Config(create_app(AppConfig(market_auto_refresh=not args.no_market_refresh,
                                                   market_refresh_seconds=args.market_refresh_seconds), database=config), host='127.0.0.1',
                                                   port=args.port, workers=1, lifespan='on'))
            server.run()
            return 0 if server.started else 1
        return 0
    except (DatabaseError, OSError, sqlite3.Error) as exc:
        print(f'Database operation refused: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
