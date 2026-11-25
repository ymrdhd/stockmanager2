"""Legacy 1.0 -> 2.0 migration: read-only probes, idempotency, and reconciliation.

Every fixture is generated from ``tests/fixtures/legacy/build.py`` into ``tmp_path``;
no real legacy database and no committed ``.db`` file is involved.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.application.migration import (  # noqa: E402
    Refused,
    probe,
    rehearsal,
    report_from_file,
    report_from_target,
    run_migration,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "legacy"


def _builder():
    spec = importlib.util.spec_from_file_location("legacy_build", FIXTURE_DIR / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _builder()


@pytest.fixture
def sources(tmp_path, builder) -> tuple[Path, Path]:
    folder = tmp_path / "legacy"
    return builder.build(folder)


def digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), path.stat().st_mtime_ns


def query(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        return conn.execute(sql, params).fetchall()


def migrate(sources: tuple[Path, Path], target: Path) -> dict:
    stock, finance = sources
    return run_migration(stock, finance, target, confirm=True)


# --------------------------------------------------------------------------- #
# 1. probe is strictly read-only
# --------------------------------------------------------------------------- #
def test_probe_is_read_only_and_reports_tables(sources):
    stock, finance = sources
    before = digest(stock), digest(finance)

    summary = probe(stock, finance)

    assert (digest(stock), digest(finance)) == before
    assert summary["source"]["stock_sha256"] == before[0][0]
    assert summary["source"]["finance_sha256"] == before[1][0]
    assert summary["source"]["unchanged"] is True
    assert summary["source"]["finance_unchanged"] is True

    stock_tables = summary["source"]["stock_tables"]
    assert stock_tables["kv_store"] == 8
    assert stock_tables["capital_history"] == 2
    assert stock_tables["daily_snapshot"] == 2
    assert stock_tables["daily_stock_snapshot"] == 2
    finance_tables = summary["source"]["finance_tables"]
    assert finance_tables["finance_account"] == 8
    assert finance_tables["finance_transaction"] == 7
    assert finance_tables["finance_category"] == 5

    assert summary["source"]["detected_schema"]["stock"].startswith("legacy-stock-1.0")
    assert summary["source"]["detected_schema"]["finance"] == "legacy-finance-v21"
    assert summary["finance"]["version"] == 21
    assert summary["finance"]["expected_version"] == 21

    mapping = summary["stock"]["mapping"]
    assert mapping["reconstructable"] >= 4
    assert mapping["missing_price"] >= 1
    assert mapping["missing_fee"] >= 1
    assert "fee or commission/stamp_tax/transfer_fee/other_fee" in mapping["columns"]


# --------------------------------------------------------------------------- #
# 2. dry-run never writes the target
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_create_or_touch_target(sources, tmp_path):
    target = tmp_path / "sandbox.sqlite"

    report = rehearsal(sources[0], sources[1], target)

    assert not target.exists()
    assert report["dry_run"] is True
    assert report["mode"] == "dry-run"
    assert report["stock"]["migrated_trades"] == 4
    assert report["finance"]["net_worth_fen"] is None
    assert report["finance"]["known_cash_and_manual_net_fen"] == 7_953_500
    assert report["idempotency"]["previous_run"] is False


# --------------------------------------------------------------------------- #
# 3. a complete migration lands the expected facts
# --------------------------------------------------------------------------- #
def test_migration_rebuilds_trades_positions_and_net_worth(sources, tmp_path):
    target = tmp_path / "sandbox.sqlite"
    report = migrate(sources, target)

    stock = report["stock"]
    assert stock["source_trades"] == 9
    assert stock["migrated_trades"] == 4
    assert stock["baseline_used"] is True
    assert "daily_stock_snapshot" in stock["baseline_coverage"]
    assert "daily_snapshot" in stock["baseline_coverage"]

    positions = {row["code"]: row for row in stock["positions"]}
    assert positions["588000"]["quantity"] == 500
    assert positions["588000"]["remaining_cost_fen"] == 50_000
    assert positions["510300"]["quantity"] == 1_100
    assert positions["510300"]["remaining_cost_fen"] == 431_500
    assert positions["159915"]["quantity"] == 2_000
    assert positions["159915"]["remaining_cost_fen"] == 420_000
    # the unverified legacy cache claims 1200 shares; the rebuilt ledger wins
    assert positions["510300"]["account"] == "华泰证券"

    assert stock["securities_cash_fen"] is None
    assert stock["known_securities_cash_fen"] == 6_654_500
    assert stock["unknown_cash_accounts"] == ["中信证券"]
    assert query(target, "SELECT count(*) FROM trade_executions "
                         "WHERE status='effective'")[0][0] == 4
    # the rejected sell is kept as an audit row, never silently dropped
    pending_rows = query(target, "SELECT pending_reason FROM trade_executions "
                                 "WHERE status='pending'")
    assert [row[0] for row in pending_rows] == ["insufficient_lots"]
    assert query(target, "SELECT count(*) FROM opening_positions")[0][0] == 1

    finance = report["finance"]
    assert finance["accounts"] == 7
    assert finance["ordinary_assets_fen"] == 1_630_000
    assert finance["liabilities_fen"] == 331_000
    assert finance["securities_equity_fen"] is None
    assert finance["securities_market_value_fen"] is None
    assert finance["net_worth_fen"] is None
    assert finance["known_cash_and_manual_net_fen"] == 7_953_500
    assert finance["bills_informational_only"] is True
    periods = {row["period"]: row for row in finance["months"]}
    assert periods["2025-11-23"]["expense_fen"] == 8_800
    assert periods["2025-11-23"]["income_fen"] == 1_500_000
    assert periods["2025-12-23"]["expense_fen"] == 25_900
    assert periods["2025-12-23"]["fees_fen"] == 500
    assert periods["2026-01-23"]["expense_fen"] == 13_500

    # errors are separated: legacy calculation defects never hide behind tool errors
    assert report["migration_tool_errors"] == []
    kinds = {item["kind"] for item in report["legacy_calculation_errors"]}
    assert "capital_delta_mixed_with_cash_flow" in kinds
    assert "price_silently_dropped_by_legacy_ledger" in kinds
    assert "summary_row_in_trade_history" in kinds

    totals = {item["reason"]: item["count"] for item in report["skipped"]}
    assert totals["capital_history_is_not_a_cash_flow"] == 1
    assert totals["ledger_history_preferred_over_cache"] == 1


# --------------------------------------------------------------------------- #
# 4. re-running over the same source is idempotent
# --------------------------------------------------------------------------- #
def test_second_run_is_idempotent(sources, tmp_path):
    target = tmp_path / "sandbox.sqlite"
    first = migrate(sources, target)
    trades_after_first = query(
        target, "SELECT count(*) FROM trade_executions WHERE status='effective'")[0][0]
    rows_after_first = query(target, "SELECT count(*) FROM trade_executions")[0][0]
    bills_after_first = query(target, "SELECT count(*) FROM finance_transactions")[0][0]
    balances_after_first = query(target, "SELECT count(*) FROM balance_adjustments")[0][0]

    second = migrate(sources, target)

    assert second["idempotency"]["previous_run"] is True
    assert second["stock"]["migrated_trades"] == 0
    assert second["idempotency"]["skipped"] == [{"reason": "already_migrated", "count": 31}]
    assert trades_after_first == 4
    assert query(target, "SELECT count(*) FROM trade_executions "
                         "WHERE status='effective'")[0][0] == trades_after_first
    assert query(target, "SELECT count(*) FROM trade_executions")[0][0] == rows_after_first
    assert query(target, "SELECT count(*) FROM finance_transactions")[0][0] == bills_after_first
    assert query(target, "SELECT count(*) FROM balance_adjustments")[0][0] == \
        balances_after_first
    assert query(target, "SELECT count(*) FROM opening_positions")[0][0] == 1
    assert second["stock"]["positions"] == first["stock"]["positions"]
    assert second["stock"]["securities_cash_fen"] == first["stock"]["securities_cash_fen"]
    assert second["finance"]["net_worth_fen"] == first["finance"]["net_worth_fen"]


# --------------------------------------------------------------------------- #
# 5. incomplete rows are parked with a reason, never zero-filled
# --------------------------------------------------------------------------- #
def test_pending_rows_keep_reasons_and_never_zero_fill(sources, tmp_path):
    target = tmp_path / "sandbox.sqlite"
    report = migrate(sources, target)
    stock = report["stock"]
    pending = {item["reason"]: item["count"] for item in stock["pending_trades"]}

    assert pending["unknown_fees"] == 1
    assert pending["missing_price"] == 1
    assert pending["fee_estimated_only"] == 1
    assert pending["merged_summary_row"] == 1
    assert pending["insufficient_lots"] == 1
    assert sum(pending.values()) == 5
    assert report["blocks_formal_takeover"] is True

    reasons = {item["reason"] for item in stock["unknown_fees"]}
    assert any("no fee" in reason for reason in reasons)
    assert any("estimate" in reason for reason in reasons)

    # the fee-less and price-less rows never became executions at zero price or zero fees
    assert query(target, "SELECT count(*) FROM trade_executions WHERE fees_fen IS NULL")[0][0] == 0
    assert query(target, "SELECT count(*) FROM trade_executions WHERE price<=0")[0][0] == 0
    codes = {row[0] for row in query(
        target, "SELECT i.code FROM trade_executions t JOIN instruments i ON i.id=t.instrument_id "
                "WHERE t.status='effective'")}
    assert "510500" not in codes
    assert "600519" not in codes
    # 600519 only exists as a rejected sell: the fee-less buy never became a fill
    blocked = {row[0] for row in query(
        target, "SELECT i.code FROM trade_executions t JOIN instruments i ON i.id=t.instrument_id "
                "WHERE t.status<>'effective'")}
    assert blocked == {"600519"}


# --------------------------------------------------------------------------- #
# 6. the duplicated securities asset is counted exactly once
# --------------------------------------------------------------------------- #
def test_duplicate_stock_asset_is_excluded(sources, tmp_path):
    target = tmp_path / "sandbox.sqlite"
    report = migrate(sources, target)
    finance = report["finance"]

    duplicates = finance["excluded_duplicate_assets"]
    assert len(duplicates) == 1
    assert duplicates[0]["legacy_type"] == "STOCK_ASSET"
    assert duplicates[0]["balance_fen"] == 5_000_000
    assert duplicates[0]["reason"] == "duplicate_of_securities_equity"

    names = {row[0] for row in query(target, "SELECT name FROM accounts")}
    assert "股票资产(旧)" not in names
    assert "华泰证券" in names
    # net worth counts the securities equity once: ordinary + securities - liabilities
    assert finance["known_cash_and_manual_net_fen"] == (
        finance["ordinary_assets_fen"] + report["stock"]["known_securities_cash_fen"]
        - finance["liabilities_fen"])
    assert finance["net_worth_fen"] is None
    assert finance["known_cash_and_manual_net_fen"] == 7_953_500
    assert any(item["kind"] == "excluded_duplicate_asset" for item in report["differences"])


# --------------------------------------------------------------------------- #
# 7. strategy cache becomes labelled evidence, never trusted positions
# --------------------------------------------------------------------------- #
def test_strategy_evidence_is_verified_or_explained(sources, tmp_path):
    target = tmp_path / "sandbox.sqlite"
    report = migrate(sources, target)

    rows = query(target,
                 "SELECT index_id,etf_code,verified,reason,price_basis,frozen_atr "
                 "FROM strategy_evidence_legacy ORDER BY index_id")
    assert len(rows) == 2
    by_index = {row[0]: row for row in rows}

    verified = by_index["CSI300"]
    assert verified[2] == 1
    assert verified[3] and "verified" in verified[3].lower()
    assert verified[1] == "510300"
    assert verified[4] == "forward_adjusted_watchlist"
    assert verified[5] is not None

    unverified = by_index["CSI500"]
    assert unverified[2] == 0
    assert unverified[3]
    for reason in ("signal_date_missing", "entry_price_missing", "shares_missing",
                   "frozen_atr_missing", "etf_mapping_missing"):
        assert reason in unverified[3]

    assert query(target, "SELECT count(*) FROM strategy_positions")[0][0] == 0
    assert report["strategy"]["strategy_positions_rows"] == 0
    assert report["strategy"]["cache_copied_as_positions"] is False
    assert report["strategy"]["main_positions"] == 1
    assert report["strategy"]["valid_frozen_atr"] == 1
    assert report["strategy"]["unverifiable"]
    assert any(item["kind"] == "price_basis_mixed" for item in report["differences"])
    assert any(item["kind"] == "strategy_evidence_only" for item in report["differences"])


# --------------------------------------------------------------------------- #
# 8. the formal ledger is never a migration target
# --------------------------------------------------------------------------- #
def test_official_target_is_refused(sources, tmp_path):
    official = tmp_path / "official.sqlite"
    with pytest.raises(Refused):
        migrate(sources, official)
    assert not official.exists()

    with pytest.raises(Refused):
        run_migration(sources[0], sources[1], tmp_path / "sandbox.sqlite",
                      confirm=True, target_role="formal")

    # a write always needs an explicit confirmation
    with pytest.raises(Refused):
        run_migration(sources[0], sources[1], tmp_path / "sandbox.sqlite", confirm=False)


# --------------------------------------------------------------------------- #
# reporting entry points
# --------------------------------------------------------------------------- #
def test_report_can_be_read_back(sources, tmp_path):
    target = tmp_path / "sandbox.sqlite"
    report = migrate(sources, target)
    destination = tmp_path / "reconciliation.json"
    destination.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    from_target = report_from_target(target)
    assert from_target["reported_role"] == "sandbox"
    assert from_target["stock"]["migrated_trades"] == 4
    assert from_target["strategy"]["strategy_positions_rows"] == 0
    assert json.loads((tmp_path / "reconciliation.json").read_text(encoding="utf-8")) == report
    assert report_from_file(destination) == report


# --------------------------------------------------------------------------- #
# a changed source never overwrites facts that are already migrated
# --------------------------------------------------------------------------- #
def test_changed_source_is_reported_not_overwritten(sources, tmp_path):
    target = tmp_path / "sandbox.sqlite"
    migrate(sources, target)
    trades_before = query(target, "SELECT count(*) FROM trade_executions")[0][0]

    stock = sources[0]
    with closing(sqlite3.connect(f"{stock.as_uri()}?mode=rw", uri=True)) as conn:
        conn.execute("INSERT INTO kv_store(key,value) VALUES('annual_return_history','[]')")
        conn.commit()

    report = migrate(sources, target)

    assert report["stock"]["migrated_trades"] == 0
    assert query(target, "SELECT count(*) FROM trade_executions")[0][0] == trades_before
    kinds = {item["kind"] for item in report["migration_tool_errors"]}
    assert "changed_source_not_overwritten" in kinds
    diffs = {item["kind"]: item for item in report["differences"]}
    assert diffs["source_changed_since_last_run"]["blocks_takeover"] is True
    assert report["blocks_formal_takeover"] is True


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def cli(*args: str) -> tuple[int, str]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "migrate_legacy.py"), *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
    return completed.returncode, completed.stdout


def test_cli_probe_dry_run_and_migrate(sources, tmp_path):
    stock, finance = sources
    code, output = cli("probe", "--stock-db", str(stock), "--finance-db", str(finance))
    assert code == 0
    assert json.loads(output)["source"]["stock_tables"]["kv_store"] == 8

    code, output = cli("dry-run", "--stock-db", str(stock), "--finance-db", str(finance),
                       "--target", str(tmp_path / "sandbox.sqlite"))
    assert code == 0
    assert json.loads(output)["dry_run"] is True
    assert not (tmp_path / "sandbox.sqlite").exists()

    code, output = cli("migrate", "--stock-db", str(stock), "--finance-db", str(finance),
                       "--target", str(tmp_path / "sandbox.sqlite"),
                       "--report-file", str(tmp_path / "report.json"))
    assert code == 3
    assert (tmp_path / "sandbox.sqlite").exists() is False

    code, output = cli("migrate", "--stock-db", str(stock), "--finance-db", str(finance),
                       "--target", str(tmp_path / "sandbox.sqlite"), "--confirm",
                       "--report-file", str(tmp_path / "report.json"))
    # 2 means "migrated, but the report blocks formal takeover"
    assert code == 2
    assert json.loads(output)["blocks_formal_takeover"] is True
    assert (tmp_path / "report.json").exists()

    code, output = cli("report", "--target", str(tmp_path / "sandbox.sqlite"))
    assert code == 0
    assert json.loads(output)["reported_role"] == "sandbox"

    code, output = cli("report", "--report-file", str(tmp_path / "report.json"))
    assert code == 0
    assert json.loads(output)["stock"]["migrated_trades"] == 4
