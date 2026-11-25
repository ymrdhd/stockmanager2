#!/usr/bin/env python3
"""One-off Stock Manager 1.0 -> 2.0 migration CLI.

Subcommands::

    probe    --stock-db PATH [--finance-db PATH]
    dry-run  --stock-db PATH [--finance-db PATH] --target PATH [--report-file PATH]
    migrate  --stock-db PATH [--finance-db PATH] --target PATH [--confirm]
             [--target-role sandbox] [--report-file PATH]
    report   (--target PATH | --report-file PATH)

Every source database is opened read-only; the target is always an isolated
sandbox database.  Nothing here is a backup tool and nothing touches the
formally bound ledger.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.application.migration import (  # noqa: E402
    Refused,
    SourceError,
    probe,
    rehearsal,
    report_from_file,
    report_from_target,
    run_migration,
    write_report,
)


def _utf8_streams() -> None:
    """Emit UTF-8 regardless of the Windows console code page.

    The report carries Chinese account names and difference details; without
    this the piped output is encoded with cp936 and a UTF-8 reader fails.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - already wrapped
                pass


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_legacy",
        description="Migrate a retired Stock Manager 1.0 database into an isolated "
                    "Stock Manager 2.0 sandbox database.")
    sub = parser.add_subparsers(dest="command", required=True)

    def sources(target: argparse.ArgumentParser) -> None:
        target.add_argument("--stock-db", required=True, type=Path,
                            help="legacy stock database (opened read-only)")
        target.add_argument("--finance-db", type=Path,
                            help="legacy finance database (opened read-only)")

    probe_cmd = sub.add_parser("probe", help="describe the legacy sources without writing")
    sources(probe_cmd)

    dry_cmd = sub.add_parser("dry-run", help="rehearse the migration, write nothing")
    sources(dry_cmd)
    dry_cmd.add_argument("--target", required=True, type=Path)
    dry_cmd.add_argument("--target-role", default="sandbox")
    dry_cmd.add_argument("--report-file", type=Path)

    migrate_cmd = sub.add_parser("migrate", help="migrate into an isolated sandbox target")
    sources(migrate_cmd)
    migrate_cmd.add_argument("--target", required=True, type=Path)
    migrate_cmd.add_argument("--target-role", default="sandbox",
                             help="must stay 'sandbox'; a formal ledger is never a target")
    migrate_cmd.add_argument("--confirm", action="store_true",
                             help="required: acknowledge the writes into the sandbox target")
    migrate_cmd.add_argument("--report-file", type=Path,
                             help="also write the reconciliation report JSON here")

    report_cmd = sub.add_parser("report", help="re-read a migrated target or a prior report")
    report_cmd.add_argument("--target", type=Path)
    report_cmd.add_argument("--report-file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            _emit(probe(args.stock_db, args.finance_db))
            return 0
        if args.command == "dry-run":
            report = rehearsal(args.stock_db, args.finance_db, args.target)
            if args.report_file:
                write_report(report, args.report_file)
            _emit(report)
            return 0
        if args.command == "migrate":
            report = run_migration(args.stock_db, args.finance_db, args.target,
                                   confirm=args.confirm, target_role=args.target_role)
            if args.report_file:
                write_report(report, args.report_file)
            _emit(report)
            return 0 if not report["blocks_formal_takeover"] else 2
        if args.command == "report":
            if args.target:
                report = report_from_target(args.target)
            elif args.report_file:
                report = report_from_file(args.report_file)
            else:
                parser.error("report needs --target or --report-file")
                return 2
            _emit(report)
            return 0
    except (Refused, SourceError) as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - the CLI must never swallow a failure
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
