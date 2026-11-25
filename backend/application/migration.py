"""One-off, read-only migration of the retired Stock Manager 1.0 databases.

Design rules encoded here (see docs/DATA_CONTRACTS.md and the brief §14/§15):

* source databases are only ever opened through a ``mode=ro`` SQLite URI;
* every rebuilt fact goes through the single authoritative 2.0 services
  (``TradeService`` / ``PortfolioService`` / ``FinanceService``), never through
  a direct ``INSERT`` into ``trade_executions``;
* a record with a missing price or fee is *parked* with a reason - it is never
  zero-filled and never guessed;
* cash is only migrated from real flows.  The legacy ``capital_history`` total
  delta is a *calculation error* of the old application, so it is reported as
  such and never turned into a cash movement;
* request ids derive from ``source sha256 + source record id`` so a second run
  cannot double-post, and a *changed* source is never silently applied on top
  of an existing migration.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.application.finance import BalanceInput, BillInput, FinanceService, TransferInput, month_start
from backend.application.portfolio import OpeningInput, PortfolioService
from backend.application.trading.service import (
    AccountInput,
    CashInput,
    InstrumentInput,
    TradeError,
    TradeInput,
    TradeService,
)
from backend.infrastructure.database import (
    DEFAULT_DATABASE,
    DatabaseError,
    DatabaseSettings,
    Identity,
    initialize_database,
    open_database,
    read_identity,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")

#: The legacy stock database is a kv_store plus four narrow tables, no version row.
STOCK_SCHEMA = "legacy-stock-1.0.kv_store+capital_history"

#: ``FINANCE_SCHEMA_VERSION`` in subroutine/finance/migrations.py of the old app.
FINANCE_SCHEMA = 21

LEGACY_STOCK_TABLES = ("kv_store", "daily_snapshot", "capital_history", "daily_stock_snapshot")

#: ACCOUNT_TYPES -> the only seven kinds 2.0 accepts.
ACCOUNT_KIND = {
    "SECURITIES_CASH": "securities",
    "BANK": "bank",
    "CASH": "cash",
    "WECHAT": "payment",
    "ALIPAY": "payment",
    "CREDIT_CARD": "credit_card",
    "CONSUMER_CREDIT": "other_liability",
    "PAYABLE": "other_liability",
    "MORTGAGE": "other_liability",
    "CONSUMER_LOAN": "other_liability",
    "PERSONAL_LOAN": "other_liability",
    "CAR_LOAN": "other_liability",
    "SECURITIES_MARGIN": "other_liability",
    "OTHER_LIABILITY": "other_liability",
    "PROPERTY": "other_asset",
    "VEHICLE": "other_asset",
    "RECEIVABLE": "other_asset",
    "FUND_ASSET": "other_asset",
    "OTHER_ASSET": "other_asset",
}

#: Legacy accounts whose balance is the *same* securities equity, not new assets.
DUPLICATE_OF_SECURITIES = {"STOCK_ASSET"}
#: The legacy aggregate display row cannot be created in 2.0.
NOT_CREATABLE = {"TOTAL_ASSET"}

BUY_ACTIONS = {"买入", "加仓", "BUY", "buy"}
SELL_ACTIONS = {"卖出", "减仓", "清仓", "减仓/清仓", "减仓清仓", "SELL", "sell"}
SUMMARY_ACTIONS = {"合并", "MERGED", "merge"}

NATURE_KIND = {"INCOME": "income", "EXPENSE": "expense", "FEE": "fee"}
NATURES_WITHOUT_BILL_EQUIVALENT = {
    "INTERNAL_TRANSFER", "LOAN_IN", "LOAN_OUT", "DEBT_REPAYMENT", "INVESTMENT_TRADE",
    "VALUATION_CHANGE", "ADJUSTMENT", "UNCLASSIFIED", "REFUND", "REIMBURSEMENT",
}

CATEGORY_BY_NATURE = {"INCOME": "other_income", "EXPENSE": "other_expense", "FEE": "fees"}

STRATEGY_KEY = "index_strategy:v1:position:"
STRATEGY_VERSION = "index-strategy-atr-v9.2"
MIGRATION_PROTOCOL = "09B-rc1"
UNADJUSTED_BASIS = "unadjusted_etf_price"

#: Migration writes are stamped against a clock that always postdates the facts.



class Refused(RuntimeError):
    """A safety check failed; the migration must stop instead of guessing."""


class SourceError(RuntimeError):
    """The legacy source cannot be read or is not a legacy database."""


def reference_clock() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# read-only source access
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def open_source(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a legacy database strictly read-only (``mode=ro`` + ``query_only``)."""
    path = Path(path)
    if not path.is_file():
        raise SourceError(f"legacy database not found: {path}")
    if Path(str(path)+"-wal").exists() and Path(str(path)+"-wal").stat().st_size:
        raise SourceError("source must be an offline consistent copy without active WAL")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise SourceError(f"cannot open legacy database read-only: {path}") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SourceError("source integrity check failed")
        yield conn
    finally:
        conn.close()


def tables_with_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    result: dict[str, int] = {}
    for name in names:
        if name.startswith("sqlite_"):
            continue
        try:
            result[name] = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        except sqlite3.Error:  # pragma: no cover - defensive
            result[name] = -1
    return result


def detect_stock_schema(conn: sqlite3.Connection, tables: dict[str, int]) -> dict[str, Any]:
    present = [name for name in LEGACY_STOCK_TABLES if name in tables]
    kv_keys = [row[0] for row in conn.execute("SELECT key FROM kv_store ORDER BY key")] \
        if "kv_store" in tables else []
    return {
        "detected": STOCK_SCHEMA if len(present) == len(LEGACY_STOCK_TABLES) else "unknown",
        "tables": present,
        "missing": [name for name in LEGACY_STOCK_TABLES if name not in tables],
        "kv_keys": kv_keys,
    }


def detect_finance_schema(conn: sqlite3.Connection, tables: dict[str, int]) -> dict[str, Any]:
    version = module = None
    if "finance_meta" in tables:
        meta = dict(conn.execute("SELECT key,value FROM finance_meta").fetchall())
        version = meta.get("schema_version")
        module = meta.get("module_version")
    return {
        "detected": f"legacy-finance-v{version}" if version else "unknown",
        "version": int(version) if version and str(version).isdigit() else None,
        "expected_version": FINANCE_SCHEMA,
        "module_version": module,
        "tables": sorted(name for name in tables if name.startswith("finance_")),
    }


# --------------------------------------------------------------------------- #
# value mapping
# --------------------------------------------------------------------------- #
def market_for(code: str) -> str:
    if code.startswith(("6", "5", "9")):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def instrument_kind(code: str) -> str:
    if code.startswith(("51", "52", "56", "58", "15", "16", "18")):
        return "etf"
    return "stock"


def parse_legacy_time(value: Any) -> datetime | None:
    """Legacy rows hold naive Shanghai wall-clock strings."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed


def yuan_to_fen(value: Any) -> int | None:
    """Exact conversion only; anything not a finite number stays unknown."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(round(number * 100))


def price_units(value: Any) -> int | None:
    """Yuan price -> the ledger's 1/10000-yuan integer scale.

    ``yuan_to_fen`` returns *fen*; a traded price carries four decimals, so the
    two conversions are deliberately separate and must never be mixed.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return int(round(number * 10_000))


def units_to_price(units: int) -> str:
    """Exact 4-decimal price string from the 1/10000-yuan integer scale."""
    return f"{units // 10_000}.{units % 10_000:04d}"


def whole_quantity(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    rounded = int(round(number))
    return rounded if abs(number - rounded) < 1e-6 else None


def fees_of(trade: dict[str, Any]) -> tuple[int | None, str]:
    """Return ``(fees_fen, quality)`` with quality in recorded/estimated/missing."""
    recorded_source = str(trade.get("fee_source") or "").upper()
    estimated = "ESTIMATE" in recorded_source
    if "fee" in trade and trade.get("fee") is not None:
        amount = yuan_to_fen(trade.get("fee"))
        if amount is None or amount < 0:
            return None, "missing"
        return amount, "estimated" if estimated else "recorded"
    names = ("commission", "stamp_tax", "transfer_fee", "other_fee")
    present = [name for name in names if trade.get(name) is not None]
    if not present:
        return None, "missing"
    parts = [yuan_to_fen(trade.get(name)) for name in present]
    if any(part is None for part in parts):
        return None, "missing"
    return sum(part for part in parts if part is not None), "estimated" if estimated else "recorded"


def request_id(source_sha: str, kind: str, source_id: str) -> str:
    """Deterministic idempotency key: same source row, same request."""
    seed = f"{source_sha}:{kind}:{source_id}".encode()
    return "mig-" + kind[:8] + "-" + hashlib.sha256(seed).hexdigest()[:48]


# --------------------------------------------------------------------------- #
# legacy readers
# --------------------------------------------------------------------------- #
def _kv(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone()
    if row is None or row[0] in (None, ""):
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def read_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    value = _kv(conn, "trade_history", [])
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def read_portfolio(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    value = _kv(conn, "portfolio", [])
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def read_transfer_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    value = _kv(conn, "transfer_data", {}) or {}
    history = value.get("history") if isinstance(value, dict) else None
    return [row for row in history if isinstance(row, dict)] if isinstance(history, list) else []


def read_strategy_positions(conn: sqlite3.Connection) -> list[tuple[str, dict[str, Any]]]:
    result: list[tuple[str, dict[str, Any]]] = []
    for key, raw in conn.execute(
            "SELECT key,value FROM kv_store WHERE key LIKE ? ORDER BY key",
            (STRATEGY_KEY + "%",)):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {"_unreadable": str(raw)[:200]}
        result.append((key[len(STRATEGY_KEY):],
                       payload if isinstance(payload, dict) else {"_unreadable": str(payload)[:200]}))
    return result


def read_watchlist_codes(conn: sqlite3.Connection) -> set[str]:
    value = _kv(conn, "stock_watchlist", [])
    codes = {str(row["code"]) for row in value
             if isinstance(row, dict) and row.get("code")} if isinstance(value, list) else set()
    return codes


def load_legacy_accounts(conn_finance: sqlite3.Connection | None) -> list[dict[str, Any]]:
    if conn_finance is None:
        return []
    return [dict(row) for row in conn_finance.execute(
        "SELECT id,name,account_type,institution,currency,opening_balance_fen,"
        "current_balance_fen,is_own_account,include_in_balance_sheet,is_active,"
        "created_at,updated_at FROM finance_account ORDER BY id").fetchall()]


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def probe(stock_db: Path, finance_db: Path | None = None) -> dict[str, Any]:
    """Describe both legacy sources without modifying either file."""
    stock_db = Path(stock_db).resolve()
    before = (stock_db.stat().st_mtime, stock_db.stat().st_size)
    stock_sha = sha256_of(stock_db)
    with open_source(stock_db) as conn:
        stock_tables = tables_with_counts(conn)
        stock_schema = detect_stock_schema(conn, stock_tables)
        trades = read_trades(conn)
        portfolio = read_portfolio(conn)
        transfers = read_transfer_history(conn)
    after = (stock_db.stat().st_mtime, stock_db.stat().st_size)
    report: dict[str, Any] = {
        "source": {
            "stock_path": str(stock_db),
            "stock_sha256": stock_sha,
            "stock_size": after[1],
            "stock_tables": stock_tables,
            "finance_sha256": None,
            "finance_size": None,
            "finance_tables": {},
            "detected_schema": {"stock": stock_schema["detected"], "finance": None},
            "unchanged": before == after,
        },
        "stock": {
            "detected_schema": stock_schema["detected"],
            "missing_tables": stock_schema["missing"],
            "kv_keys": stock_schema["kv_keys"],
            "source_trades": len(trades),
            "source_portfolio_entries": len(portfolio),
            "source_cash_flows": len(transfers),
            "source_capital_history_rows": stock_tables.get("capital_history", 0),
            "mapping": mapping_feasibility(trades),
        },
    }
    if finance_db is not None:
        finance_db = Path(finance_db)
        fbefore = (finance_db.stat().st_mtime, finance_db.stat().st_size)
        with open_source(finance_db) as conn:
            finance_tables = tables_with_counts(conn)
            finance_schema = detect_finance_schema(conn, finance_tables)
            accounts = dict(conn.execute(
                "SELECT account_type,count(*) FROM finance_account GROUP BY account_type"
            ).fetchall()) if "finance_account" in finance_tables else {}
            natures = {f"{row[0]}/{row[1]}": row[2] for row in conn.execute(
                "SELECT nature,status,count(*) FROM finance_transaction "
                "GROUP BY nature,status").fetchall()} if "finance_transaction" in finance_tables else {}
        fafter = (finance_db.stat().st_mtime, finance_db.stat().st_size)
        report["source"].update({
            "finance_path": str(finance_db),
            "finance_sha256": sha256_of(finance_db),
            "finance_size": fafter[1],
            "finance_tables": finance_tables,
            "detected_schema": {"stock": stock_schema["detected"],
                                "finance": finance_schema["detected"]},
            "finance_unchanged": fbefore == fafter,
        })
        report["finance"] = {
            "detected_schema": finance_schema["detected"],
            "version": finance_schema["version"],
            "expected_version": finance_schema["expected_version"],
            "module_version": finance_schema["module_version"],
            "account_types": accounts,
            "transaction_natures": natures,
            "mapping": {
                "mappable": sorted(k for k in accounts if k in ACCOUNT_KIND),
                "excluded_duplicate": sorted(k for k in accounts
                                             if k in DUPLICATE_OF_SECURITIES),
                "not_creatable": sorted(k for k in accounts if k in NOT_CREATABLE),
                "unsupported_types": sorted(k for k in accounts if k not in ACCOUNT_KIND
                                            and k not in DUPLICATE_OF_SECURITIES
                                            and k not in NOT_CREATABLE),
                "bill_natures_supported": sorted(k for k in NATURE_KIND),
                "bill_natures_without_equivalent": sorted(
                    {key.split("/")[0] for key in natures} & NATURES_WITHOUT_BILL_EQUIVALENT),
            },
        }
    return report


def mapping_feasibility(trades: list[dict[str, Any]]) -> dict[str, Any]:
    missing_price = sum(1 for t in trades if price_units(t.get("price")) is None)
    missing_fee = sum(1 for t in trades if fees_of(t)[0] is None)
    estimated = sum(1 for t in trades if fees_of(t)[1] == "estimated")
    summary = sum(1 for t in trades if str(t.get("action") or "") in SUMMARY_ACTIONS)
    complete = sum(1 for t in trades
                   if price_units(t.get("price")) is not None and fees_of(t)[0] is not None
                   and str(t.get("action") or "") not in SUMMARY_ACTIONS)
    return {
        "columns": ["code", "action", "qty", "price", "trade_date", "fee or "
                    "commission/stamp_tax/transfer_fee/other_fee", "fee_source"],
        "reconstructable": complete,
        "missing_price": missing_price,
        "missing_fee": missing_fee,
        "estimated_fee": estimated,
        "summary_rows": summary,
    }


# --------------------------------------------------------------------------- #
# target guards and target database
# --------------------------------------------------------------------------- #
SELF_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS migration_runs(
        id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, stock_sha256 TEXT NOT NULL,
        finance_sha256 TEXT, stock_path TEXT NOT NULL, finance_path TEXT,
        mode TEXT NOT NULL, target_role TEXT NOT NULL, status TEXT NOT NULL,
        started_at TEXT NOT NULL, finished_at TEXT, migrated INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0, pending INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS migration_ledger(
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_kind TEXT NOT NULL,
        source_sha256 TEXT NOT NULL, source_id TEXT NOT NULL, target_table TEXT NOT NULL,
        target_id TEXT, mapping TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
        UNIQUE(source_kind, source_sha256, source_id))""",
    """CREATE TABLE IF NOT EXISTS migration_skips(
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, category TEXT NOT NULL,
        reason TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS strategy_evidence_legacy(
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, source_key TEXT NOT NULL,
        index_id TEXT NOT NULL DEFAULT '', etf_code TEXT NOT NULL DEFAULT '',
        strategy_version TEXT NOT NULL DEFAULT '', lot_role TEXT NOT NULL DEFAULT 'main',
        signal_date TEXT, entry_date TEXT, entry_price TEXT, frozen_atr TEXT,
        shares INTEGER NOT NULL DEFAULT 0, add_count INTEGER NOT NULL DEFAULT 0,
        adds TEXT NOT NULL DEFAULT '[]', cooldown_until TEXT,
        price_basis TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0
        CHECK(verified IN (0,1)), reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS legacy_finance_snapshots(
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, snapshot_date TEXT NOT NULL UNIQUE,
        total_assets_fen INTEGER NOT NULL DEFAULT 0,
        total_liabilities_fen INTEGER NOT NULL DEFAULT 0,
        net_worth_fen INTEGER NOT NULL DEFAULT 0,
        verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
        reason TEXT NOT NULL DEFAULT '')""",
)


def target_guard(path: Path, target_role: str) -> Path:
    """Refuse any path that could be (or alias) the single formal ledger."""
    path = Path(path).resolve()
    if target_role != "sandbox":
        raise Refused(
            "migration target must be an isolated sandbox database "
            "(--target-role sandbox); the formal ledger is never a migration target")
    if "official" in path.name.lower():
        raise Refused("migration target path must not be an official database path")
    if DEFAULT_DATABASE.exists() and path == DEFAULT_DATABASE.resolve():
        raise Refused("migration target must not be the configured formal database")
    binding = path.parent / "official.json"
    if binding.is_file():
        try:
            bound = json.loads(binding.read_text(encoding="utf-8")).get("path")
        except (OSError, ValueError):
            bound = None
        if bound and Path(bound).resolve() == path:
            raise Refused("migration target is the formally bound database")
    if path.exists():
        if not path.is_file():
            raise Refused("migration target must be a database file")
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
            try:
                identity = read_identity(conn)
            except DatabaseError as exc:
                raise Refused(
                    "existing target is not a Stock Manager 2.0 database; refusing to "
                    "treat an unrelated file as a migration target") from exc
        if identity.instance_role == "formal":
            raise Refused("migration target holds the formal role; use an isolated sandbox")
    return path


@contextmanager
def target_database(path: Path) -> Iterator[tuple[Any, Identity, Path]]:
    """Open the migration sandbox, creating it through ``initialize_database``."""
    path = Path(path).resolve()
    binding = path.parent / "official.json"
    if path.exists():
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
            identity = read_identity(conn)
        if identity.instance_role != "sandbox":
            raise Refused("migration target must hold the sandbox role")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        identity = initialize_database(path, role="sandbox", confirm=True,
                                       binding_path=binding)
    settings = DatabaseSettings(path, identity.database_uuid, identity.generation,
                                mode="local", writable_sandbox=True, binding_path=binding)
    with open_database(settings) as db:
        yield db, identity, path


@contextmanager
def rehearsal_database() -> Iterator[tuple[Any, Identity, Path]]:
    """A rehearsal writes into a throwaway sandbox, never into ``--target``."""
    with tempfile.TemporaryDirectory(prefix="sm2-migration-") as folder:
        path = Path(folder).resolve() / "rehearsal.sqlite"
        binding = Path(folder).resolve() / "official.json"
        identity = initialize_database(path, role="sandbox", confirm=True,
                                       binding_path=binding)
        settings = DatabaseSettings(path, identity.database_uuid, identity.generation,
                                    mode="local", writable_sandbox=True,
                                    binding_path=binding)
        with open_database(settings) as db:
            yield db, identity, path


def ensure_self_schema(db) -> None:
    with db.transaction() as conn:
        for statement in SELF_SCHEMA:
            conn.execute(statement)


# --------------------------------------------------------------------------- #
# bookkeeping helpers
# --------------------------------------------------------------------------- #
def already_migrated(db, kind: str, sha: str, source_id: str) -> dict | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT target_table,target_id FROM migration_ledger "
            "WHERE source_kind=? AND source_sha256=? AND source_id=?",
            (kind, sha, source_id)).fetchone()
    return {"target_table": row[0], "target_id": row[1]} if row else None


def record(db, run: _Run, kind: str, source_id: str, target_table: str,
           target_id: str | None, mapping: dict[str, Any]) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO migration_ledger VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid4().hex, run.run_id, kind, run.stock_sha, source_id, target_table,
             target_id, json.dumps(mapping, ensure_ascii=False, sort_keys=True),
             datetime.now(UTC).isoformat()))


def record_skip(db, run: _Run, category: str, reason: str, source_id: str = "",
                detail: dict[str, Any] | None = None) -> None:
    run.skips.append({"category": category, "reason": reason,
                      "source_id": source_id, "detail": detail or {}})
    with db.transaction() as conn:
        conn.execute("INSERT INTO migration_skips VALUES(?,?,?,?,?,?)",
                     (uuid4().hex, run.run_id, category, reason, source_id,
                      json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)))


class _Run:
    """Mutable migration state; every skip and difference is preserved."""

    def __init__(self, *, stock_sha: str, finance_sha: str | None, run_id: str,
                 stock_path: Path, finance_path: Path | None, mode: str,
                 target_role: str) -> None:
        self.stock_sha = stock_sha
        self.finance_sha = finance_sha
        self.run_id = run_id
        self.stock_path = stock_path
        self.finance_path = finance_path
        self.mode = mode
        self.target_role = target_role
        self.skips: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.differences: list[dict[str, Any]] = []
        self.legacy_errors: list[dict[str, Any]] = []
        self.tool_errors: list[dict[str, Any]] = []
        self.duplicate_assets: list[dict[str, Any]] = []
        self.unknown_fees: list[dict[str, Any]] = []
        self.accounts: dict[str, dict[str, Any]] = {}
        self.instruments: dict[str, str] = {}
        self.source_trades = 0
        self.migrated_trades = 0
        self.baseline_used = False
        self.baseline_coverage: list[str] = []
        self.bills_informational_only = False
        self.unverifiable: dict[str, int] = {}
        self.account_dates: dict[str, list[tuple[datetime, str]]] = {}
        self.stock_dates: list[tuple[datetime, str]] = []

    def skip(self, db, category: str, reason: str, source_id: str = "",
             detail: dict[str, Any] | None = None) -> None:
        record_skip(db, self, category, reason, source_id, detail)

    def pending_item(self, reason: str, detail: dict[str, Any]) -> None:
        self.pending.append({"reason": reason, **detail})

    def difference(self, kind: str, detail: str, blocks: bool = False) -> None:
        self.differences.append({"kind": kind, "detail": detail, "blocks_takeover": blocks})

    def legacy_error(self, kind: str, detail: str) -> None:
        self.legacy_errors.append({"kind": kind, "detail": detail})

    def tool_error(self, kind: str, detail: str) -> None:
        self.tool_errors.append({"kind": kind, "detail": detail})


# --------------------------------------------------------------------------- #
# accounts and instruments
# --------------------------------------------------------------------------- #
def migrate_accounts(db, run: _Run, svc: TradeService,
                     legacy_accounts: list[dict[str, Any]]) -> str | None:
    """Create 2.0 accounts; return the primary securities account name."""
    primary: str | None = None
    seen_securities = False
    for account in legacy_accounts:
        legacy_type = str(account["account_type"])
        name = account["name"]
        source_id = f"account:{account['id']}"
        if account["currency"] != "CNY" or not account["is_own_account"] or not account["is_active"]:
            run.difference("account_scope_or_currency_unresolved", f"{name}: currency/ownership/inactive state needs review", True)
            run.skip(db,"account","account_scope_or_currency_unresolved",source_id)
            continue
        existing = already_migrated(db, "account", run.stock_sha, source_id)
        if existing is not None:
            run.accounts[name] = {"target_id": existing["target_id"],
                                  "kind": ACCOUNT_KIND.get(legacy_type),
                                  "legacy_type": legacy_type, "skipped": True}
            run.skip(db, "account", "already_migrated", source_id)
            if legacy_type == "SECURITIES_CASH" and not seen_securities:
                primary, seen_securities = name, True
            continue
        if legacy_type in NOT_CREATABLE:
            run.skip(db, "account", "legacy_aggregate_account_not_creatable", source_id,
                     {"name": name})
            run.legacy_error("aggregate_account_present",
                             f"{name} is a legacy {legacy_type} display row; nothing to migrate")
            continue
        if legacy_type in DUPLICATE_OF_SECURITIES:
            run.duplicate_assets.append({
                "account": name, "legacy_type": legacy_type,
                "balance_fen": abs(int(account["current_balance_fen"])),
                "reason": "duplicate_of_securities_equity"})
            run.skip(db, "account", "duplicate_of_securities_equity", source_id,
                     {"name": name})
            run.difference(
                "excluded_duplicate_asset",
                f"{name} ({legacy_type}) repeats the securities equity already held by the "
                f"ledger; migrating it would count the same assets twice")
            continue
        kind = ACCOUNT_KIND.get(legacy_type)
        if kind is None:
            run.skip(db, "account", f"unsupported_account_type:{legacy_type}", source_id,
                     {"name": name})
            run.difference("unsupported_account_type",
                           f"{name} has legacy type {legacy_type} with no 2.0 equivalent",
                           True)
            continue
        candidates = list(run.account_dates.get(str(account['id']), []))
        created_at = parse_legacy_time(account['created_at'])
        if created_at is not None:
            candidates.append((created_at, 'finance_account.created_at'))
        if kind == 'securities' and not seen_securities:
            candidates.extend(run.stock_dates)
        if not candidates:
            run.skip(db, 'account', 'account_effective_date_unknown', source_id, {'name': name})
            run.difference('account_effective_date_unknown', f'{name}: no dated account evidence; manual confirmation required', True)
            continue
        effective, date_source = min(candidates, key=lambda item: item[0])
        if effective > reference_clock():
            run.skip(db, 'account', 'account_effective_date_future', source_id, {'name': name})
            run.difference('account_effective_date_future', f'{name}: only future evidence; manual confirmation required', True)
            continue
        if created_at is None or effective < created_at:
            run.difference('account_effective_date_from_fact', f'{name}: {effective.isoformat()} from {date_source}; earliest observed fact, not a proven opening date', True)
        created = svc.create_account(AccountInput(
            request_id=request_id(run.stock_sha, "acct", source_id),
            name=name, kind=kind, included=bool(account["include_in_balance_sheet"]),
            effective_at=effective))
        record(db, run, "account", source_id, "accounts", created["id"],
               {"legacy_type": legacy_type, "kind": kind, "name": name, "effective_at": effective.isoformat(), "effective_source": date_source})
        run.accounts[name] = {"target_id": created["id"], "kind": kind,
                              "legacy_type": legacy_type}
        if kind == "securities" and not seen_securities:
            primary, seen_securities = name, True
    if primary is None:
        if not run.stock_dates and not any(a['account_type']=='SECURITIES_CASH' for a in legacy_accounts):
            return None
        if not run.stock_dates or any(a['account_type']=='SECURITIES_CASH' for a in legacy_accounts):
            run.difference('securities_account_date_unresolved', 'No confirmed securities account mapping/effective date; manual review required', True)
            return None
        effective, date_source = min(run.stock_dates, key=lambda item: item[0])
        if effective > reference_clock():
            run.difference('securities_account_date_future', 'Only future stock evidence; manual review required', True)
            return None
        run.difference('securities_account_date_inferred', f'{effective.isoformat()} from {date_source}; account mapping must be confirmed', True)
        run.difference(
            "no_legacy_securities_account",
            "the legacy finance database holds no SECURITIES_CASH account; a single "
            "ledger securities account was created to receive the migrated trades")
        created = svc.create_account(AccountInput(
            request_id=request_id(run.stock_sha, "acct", "securities-account:created"),
            name="证券账户", kind="securities", effective_at=effective))
        record(db, run, "account", "securities-account:created", "accounts", created["id"],
               {"kind": "securities", "name": "证券账户", "origin": "created_by_migration", "effective_at": effective.isoformat(), "effective_source": date_source})
        run.accounts["证券账户"] = {"target_id": created["id"], "kind": "securities",
                                    "legacy_type": "SECURITIES_CASH"}
        primary = "证券账户"
    securities = [name for name, row in run.accounts.items() if row["kind"] == "securities"]
    if len(securities) > 1:
        run.difference(
            "multiple_securities_accounts",
            f"{len(securities)} securities accounts were created; the legacy kv_store "
            f"trade history carries no account column, so every migrated execution is "
            f"booked on {primary} and the others hold no trading facts", True)
    return primary


def migrate_instruments(db, run: _Run, svc: TradeService, codes: set[str]) -> None:
    for code in sorted(codes):
        source_id = f"instrument:{code}"
        existing = already_migrated(db, "instrument", run.stock_sha, source_id)
        if existing is not None:
            run.instruments[code] = existing["target_id"]
            run.skip(db, "instrument", "already_migrated", source_id)
            continue
        created = svc.create_instrument(InstrumentInput(
            request_id=request_id(run.stock_sha, "inst", source_id),
            code=code, market=market_for(code), kind=instrument_kind(code)))
        record(db, run, "instrument", source_id, "instruments", created["id"],
               {"code": code, "market": market_for(code), "kind": instrument_kind(code)})
        run.instruments[code] = created["id"]


# --------------------------------------------------------------------------- #
# verified opening baseline and cash
# --------------------------------------------------------------------------- #
def confirmed_opening_quantity(conn_source: sqlite3.Connection, code: str) -> int | None:
    row = conn_source.execute(
        "SELECT opening_quantity FROM daily_stock_snapshot WHERE code=? "
        "ORDER BY date LIMIT 1", (code,)).fetchone()
    return whole_quantity(row[0]) if row is not None else None


def migrate_openings(db, run: _Run, portfolio_svc: PortfolioService,
                     conn_source: sqlite3.Connection, portfolio: list[dict[str, Any]],
                     traded_codes: set[str], securities_name: str | None,
                     account_id: str | None) -> None:
    """A cached lot is only a baseline when an independent legacy table confirms it."""
    if account_id is None:
        return
    for entry in portfolio:
        code = str(entry.get("code") or "").strip()
        if not code:
            continue
        source_id = f"opening:{code}"
        if code in traded_codes:
            run.skip(db, "position", "ledger_history_preferred_over_cache", source_id,
                     {"code": code})
            run.difference(
                "cached_lot_not_used_as_baseline",
                f"{code} has execution history in the legacy ledger; its cached lot is not "
                f"used as a baseline (a fabricated baseline would hide real trades)")
            continue
        lots = [lot for lot in entry.get("lots") or [] if isinstance(lot, dict)]
        quantity = sum(whole_quantity(lot.get("qty")) or 0 for lot in lots)
        if quantity <= 0:
            run.difference("unverifiable_opening_quantity", code, True)
            run.skip(db, "position", "unverifiable_opening_quantity", source_id,
                     {"code": code})
            continue
        confirmed = confirmed_opening_quantity(conn_source, code)
        if confirmed != quantity:
            run.skip(db, "position", "opening_not_confirmed_by_daily_snapshot", source_id,
                     {"code": code, "cached": quantity, "confirmed": confirmed})
            run.difference(
                "unverified_opening_position",
                f"{code}: cached {quantity} shares vs {confirmed} confirmed by "
                f"daily_stock_snapshot; no baseline was migrated", True)
            continue
        cost_fen = 0
        for lot in lots:
            lot_quantity = whole_quantity(lot.get("qty")) or 0
            cost = lot.get("cost")
            if cost is None:
                cost_fen = -1
                break
            unit = yuan_to_fen(cost)
            if unit is None:
                cost_fen = -1
                break
            cost_fen += unit * lot_quantity
        if cost_fen < 0:
            run.skip(db, "position", "opening_cost_unknown", source_id, {"code": code})
            run.difference("unverified_opening_cost",
                           f"{code}: the cached lot cost is not a usable number; the "
                           f"baseline carries no remaining cost", True)
            continue
        at = parse_legacy_time(lots[0].get("buy_date")) or parse_legacy_time(
            entry.get("buy_date"))
        if at is None:
            run.skip(db, "position", "opening_date_unknown", source_id, {"code": code})
            run.difference("opening_date_unknown", code, True)
            continue
        if already_migrated(db, "opening", run.stock_sha, source_id) is not None:
            run.skip(db, "position", "already_migrated", source_id)
            run.baseline_used = True
            continue
        run.difference("opening_fees_unverified", f"{code}: cached cost does not independently prove remaining fees; confirm statement", True)
        created = portfolio_svc.opening(OpeningInput(
            request_id=request_id(run.stock_sha, "opening", source_id),
            account_id=account_id, instrument_id=run.instruments[code],
            quantity=quantity, cost_fen=cost_fen, fees_fen=0,
            occurred_at=at.replace(hour=15, minute=0, second=0, microsecond=0),
            note=(f"legacy verified baseline: {quantity} shares confirmed by "
                  f"daily_stock_snapshot on {at.date().isoformat()}")))
        record(db, run, "opening", source_id, "opening_positions", created["id"],
               {"code": code, "quantity": quantity, "cost_fen": cost_fen})
        run.baseline_used = True
        run.baseline_coverage.append(
            f"position {code}: {quantity} shares, remaining cost {cost_fen} fen, "
            f"confirmed by daily_stock_snapshot")


def migrate_cash(db, run: _Run, svc: TradeService, conn_source: sqlite3.Connection,
                 account_id: str | None) -> None:
    """Only real flows and one observed opening balance; never a capital delta."""
    if account_id is None:
        return
    snapshot = conn_source.execute(
        "SELECT date,cash_balance FROM daily_snapshot WHERE cash_balance IS NOT NULL "
        "ORDER BY date LIMIT 1").fetchone()
    if snapshot is not None:
        at = parse_legacy_time(snapshot[0])
        amount = yuan_to_fen(snapshot[1])
        source_id = f"cash-opening:{snapshot[0]}"
        if at is None or amount is None or amount < 0:
            run.difference("unverifiable_opening_cash", str(snapshot[0]), True)
            run.skip(db, "cash", "unverifiable_opening_cash", source_id,
                     {"date": snapshot[0], "cash_balance": snapshot[1]})
        elif already_migrated(db, "cash", run.stock_sha, source_id) is not None:
            run.skip(db, "cash", "already_migrated", source_id)
            run.baseline_used = True
        else:
            created = svc.cash(CashInput(
                request_id=request_id(run.stock_sha, "cash", source_id),
                account_id=account_id, kind="opening", amount_fen=amount,
                occurred_at=at.replace(hour=8, minute=0),
                note=f"legacy opening securities cash observed on {snapshot[0]}"))
            record(db, run, "cash", source_id, "cash_movements", created["id"],
                   {"kind": "opening", "amount_fen": amount})
            run.baseline_used = True
            run.baseline_coverage.append(
                f"cash: opening {amount} fen observed in daily_snapshot on {snapshot[0]}")
    for index, entry in enumerate(sorted(read_transfer_history(conn_source),
                                        key=lambda item: str(item.get("date")))):
        source_id = f"cash-flow:{entry.get('date')}:{index}"
        at = parse_legacy_time(entry.get("date"))
        net = yuan_to_fen(entry.get("net_cf"))
        if at is None or net is None or net == 0:
            run.skip(db, "cash", "unverifiable_cash_flow", source_id, {"entry": entry})
            run.difference("unverifiable_cash_flow",
                           f"transfer_data entry {entry!r} has no usable date or amount", True)
            continue
        if already_migrated(db, "cash", run.stock_sha, source_id) is not None:
            run.skip(db, "cash", "already_migrated", source_id)
            continue
        kind = "deposit" if net > 0 else "withdrawal"
        created = svc.cash(CashInput(
            request_id=request_id(run.stock_sha, "cash", source_id),
            account_id=account_id, kind=kind, amount_fen=net,
            occurred_at=at.replace(hour=10, minute=0),
            note=f"legacy transfer_data flow on {entry.get('date')}"))
        record(db, run, "cash", source_id, "cash_movements", created["id"],
               {"kind": kind, "amount_fen": net, "date": entry.get("date")})
    report_capital_history(db, run, conn_source)


def report_capital_history(db, run: _Run, conn_source: sqlite3.Connection) -> None:
    """The legacy cumulative-capital delta is a known 1.0 calculation error."""
    rows = conn_source.execute(
        "SELECT date,total_capital,transfer_in,transfer_out,source FROM capital_history "
        "ORDER BY date").fetchall()
    if not rows:
        return
    delta = yuan_to_fen((rows[-1][1] or 0) - (rows[0][1] or 0))
    flows = sum(yuan_to_fen(entry.get("net_cf")) or 0
                for entry in read_transfer_history(conn_source))
    run.skip(db, "capital", "capital_history_is_not_a_cash_flow", "capital_history",
             {"rows": len(rows), "delta_fen": delta, "recorded_flows_fen": flows})
    if delta != flows:
        run.legacy_error(
            "capital_delta_mixed_with_cash_flow",
            f"capital_history moved {delta} fen between {rows[0][0]} and {rows[-1][0]} "
            f"while transfer_data records {flows} fen of real flows; 1.0 wrote accumulated "
            f"capital changes into the same table and reconciled transfers into the same "
            f"columns, so the delta must never be posted as a cash movement")
        run.difference(
            "capital_delta_not_posted",
            f"legacy cumulative capital delta {delta} fen differs from the {flows} fen of "
            f"recorded real flows; only the real flows were migrated")


# --------------------------------------------------------------------------- #
# trades
# --------------------------------------------------------------------------- #
def trade_source_id(index: int, trade: dict[str, Any]) -> str:
    return (f"trade:{index}:{trade.get('code')}:{trade.get('action')}:"
            f"{trade.get('trade_date') or trade.get('date')}")


def migrate_trades(db, run: _Run, svc: TradeService, trades: list[dict[str, Any]],
                   account_id: str | None) -> None:
    """Rebuild executions through ``TradeService.submit`` only."""
    if not trades:
        return
    if account_id is None:
        run.difference("no_securities_account",
                       "no ledger account could be resolved for the legacy trades", True)
        return
    ordered = sorted(enumerate(trades), key=lambda item: (
        parse_legacy_time(item[1].get("trade_date") or item[1].get("date"))
        or datetime(1970, 1, 1, tzinfo=SHANGHAI), item[0]))
    for index, trade in ordered:
        source_id = trade_source_id(index, trade)
        code = str(trade.get("code") or "").strip()
        action = str(trade.get("action") or "").strip()
        at = parse_legacy_time(trade.get("trade_date") or trade.get("date"))
        quantity = whole_quantity(trade.get("qty"))
        price_fen = price_units(trade.get("price"))
        if action in SUMMARY_ACTIONS:
            run.skip(db, "trade", "merged_summary_row_not_an_execution", source_id,
                     {"code": code})
            run.pending_item("merged_summary_row", {"code": code, "source_id": source_id})
            run.legacy_error(
                "summary_row_in_trade_history",
                f"{code}: a 合并/MERGED row is a portfolio summary, not a fill")
            continue
        side = ("buy" if action in BUY_ACTIONS
                else "sell" if action in SELL_ACTIONS else None)
        if side is None:
            run.skip(db, "trade", f"unknown_trade_action:{action}", source_id, {"code": code})
            run.pending_item("unknown_trade_action",
                             {"code": code, "action": action, "source_id": source_id})
            continue
        if code not in run.instruments:
            run.skip(db, "trade", "unknown_instrument", source_id, {"code": code})
            run.pending_item("unknown_instrument", {"code": code, "source_id": source_id})
            continue
        if at is None:
            run.skip(db, "trade", "missing_execution_time", source_id, {"code": code})
            run.pending_item("missing_execution_time",
                             {"code": code, "source_id": source_id})
            run.legacy_error("execution_time_unparseable",
                             f"{code}: the legacy row has no usable trade date or time")
            continue
        if quantity is None:
            run.skip(db, "trade", "missing_quantity", source_id, {"code": code})
            run.pending_item("missing_quantity", {"code": code, "source_id": source_id})
            continue
        if not price_fen:
            run.skip(db, "trade", "missing_price", source_id, {"code": code})
            run.pending_item("missing_price",
                             {"code": code, "trade_date": at.date().isoformat(),
                              "source_id": source_id})
            run.legacy_error(
                "price_silently_dropped_by_legacy_ledger",
                f"{code} on {at.date().isoformat()}: 1.0 skipped rows with price<=0 inside "
                f"compute_trade_ledger instead of reporting them")
            continue
        fee_fen, quality = fees_of(trade)
        if quality == "missing":
            run.skip(db, "trade", "unknown_fees", source_id, {"code": code})
            run.pending_item("unknown_fees",
                             {"code": code, "trade_date": at.date().isoformat(),
                              "source_id": source_id})
            run.unknown_fees.append({
                "code": code, "trade_date": at.date().isoformat(),
                "reason": "no fee, commission, stamp tax, transfer fee or other fee recorded"})
            continue
        if quality == "estimated":
            run.skip(db, "trade", "fee_only_estimated", source_id, {"code": code})
            run.pending_item("fee_estimated_only",
                             {"code": code, "trade_date": at.date().isoformat(),
                              "source_id": source_id})
            run.unknown_fees.append({
                "code": code, "trade_date": at.date().isoformat(),
                "reason": "fee_source marks the fee as an estimate, not a record"})
            run.legacy_error(
                "estimated_fee_presented_as_recorded",
                f"{code} on {at.date().isoformat()}: an estimated fee is not a recorded "
                f"cost and must not be reconstructed as one")
            continue
        if already_migrated(db, "trade", run.stock_sha, source_id) is not None:
            run.skip(db, "trade", "already_migrated", source_id)
            continue
        try:
            created = svc.submit(TradeInput(
                request_id=request_id(run.stock_sha, "trade", source_id),
                account_id=account_id, instrument_id=run.instruments[code], side=side,
                quantity=quantity, price=units_to_price(price_fen), fees_fen=fee_fen,
                executed_at=at, trading_day=at.date(),
                note=f"legacy {action} {code} {trade.get('name') or ''}".strip()))
        except TradeError as exc:
            reason = str(exc) or "service_rejected"
            run.skip(db, "trade", f"service_rejected:{reason}", source_id, {"code": code})
            run.pending_item(reason, {"code": code, "trade_date": at.date().isoformat(),
                                      "source_id": source_id})
            run.tool_error("trade_rejected", f"{source_id} ({code}): {exc}")
            continue
        record(db, run, "trade", source_id, "trade_executions", created["id"],
               {"code": code, "side": side, "quantity": quantity,
                "price_units": price_fen, "fees_fen": fee_fen,
                "trading_day": at.date().isoformat()})
        if created.get("status") == "effective":
            run.migrated_trades += 1
        else:
            run.pending_item(created.get("pending_reason") or "pending",
                             {"code": code, "trade_date": at.date().isoformat(),
                              "source_id": source_id})


# --------------------------------------------------------------------------- #
# finance
# --------------------------------------------------------------------------- #
def migrate_finance(db, run: _Run, svc: FinanceService,
                    conn_finance: sqlite3.Connection,
                    legacy_accounts: list[dict[str, Any]]) -> None:
    migrate_balances(db, run, svc, legacy_accounts)
    migrate_bills(db, run, svc, conn_finance, legacy_accounts)
    migrate_finance_snapshots(db, run, conn_finance)


def _manual_target(run: _Run, account: dict[str, Any]) -> dict[str, Any] | None:
    target = run.accounts.get(account["name"])
    if target is None or target.get("target_id") is None:
        return None
    if target["kind"] == "securities":
        return None
    return target


def migrate_balances(db, run: _Run, svc: FinanceService,
                     legacy_accounts: list[dict[str, Any]]) -> None:
    """A manual balance is a confirmed observation; bills stay informational."""
    for account in legacy_accounts:
        target = _manual_target(run, account)
        if target is None:
            continue
        source_id = f"balance:{account['id']}"
        if already_migrated(db, "balance", run.stock_sha, source_id) is not None:
            run.skip(db, "balance", "already_migrated", source_id)
            continue
        amount = abs(int(account["current_balance_fen"]))
        at = parse_legacy_time(account['updated_at'])
        if at is None:
            run.skip(db, 'balance', 'balance_date_unknown', source_id, {'name': account['name']})
            run.difference('balance_date_unknown', f"{account['name']}: observation timestamp missing; balance not posted", True)
            continue
        created = svc.balance(BalanceInput(
            request_id=request_id(run.stock_sha, "balance", source_id),
            account_id=target["target_id"], after_fen=amount, occurred_at=at,
            note="legacy confirmed manual balance", expected_balance_id=None))
        record(db, run, "balance", source_id, "balance_adjustments", created["id"],
               {"amount_fen": amount, "kind": target["kind"]})
        target["balance_id"] = created["id"]
        target["balance_fen"] = amount


def migrate_bills(db, run: _Run, svc: FinanceService,
                  conn_finance: sqlite3.Connection,
                  legacy_accounts: list[dict[str, Any]]) -> None:
    id_to_account = {row["id"]: row for row in legacy_accounts}
    rows = conn_finance.execute(
        "SELECT id,occurred_at,nature,amount_fen,source_account_id,target_account_id,"
        "category_id,description,status FROM finance_transaction "
        "ORDER BY occurred_at,id").fetchall()
    informational = False
    for row in rows:
        (transaction_id, occurred_at, nature, amount, source_account, target_account,
         category_id, description, status) = row
        source_id = f"bill:{transaction_id}"
        if str(status).upper() != "POSTED":
            run.skip(db, "bill", "legacy_transaction_not_posted", source_id,
                     {"status": status})
            continue
        if nature == 'INTERNAL_TRANSFER':
            endpoints = [id_to_account.get(i) for i in (source_account, target_account)]
            targets = [run.accounts.get(a['name']) if a else None for a in endpoints]
            at = parse_legacy_time(occurred_at)
            if all(t and t['kind'] != 'securities' for t in targets) and at is not None and type(amount) is int and amount > 0:
                ids = [t['target_id'] for t in targets]
                with db.read() as conn:
                    balances = [conn.execute("SELECT id,occurred_at FROM balance_adjustments WHERE account_id=? AND status='effective' ORDER BY occurred_at DESC,rowid DESC LIMIT 1", (i,)).fetchone() for i in ids]
                if all(b and datetime.fromisoformat(b[1]) >= at for b in balances):
                    if already_migrated(db, 'transfer', run.stock_sha, source_id):
                        run.skip(db, 'transfer', 'already_migrated', source_id)
                        continue
                    created = svc.transfer(TransferInput(request_id=request_id(run.stock_sha,'transfer',source_id),
                        from_account_id=ids[0],to_account_id=ids[1],amount_fen=amount,occurred_at=at,
                        from_included=True,to_included=True,from_balance_id=balances[0][0],to_balance_id=balances[1][0],
                        note=(description or 'legacy transfer, included in observed balance')[:2000]))
                    record(db,run,'transfer',source_id,'finance_transfers',created['id'],{'amount_fen':amount})
                    continue
        if nature in NATURES_WITHOUT_BILL_EQUIVALENT:
            run.skip(db, "bill", f"nature_without_bill_equivalent:{nature}", source_id,
                     {"description": description})
            run.difference(
                "bill_nature_without_equivalent",
                f"legacy transaction #{transaction_id} ({nature}) has no safe 2.0 bill "
                f"equivalent; it is reported, not posted", True)
            continue
        kind = NATURE_KIND.get(str(nature))
        if kind is None:
            run.difference("unknown_bill_nature", f"{transaction_id}: {nature}", True)
            run.skip(db, "bill", f"unknown_bill_nature:{nature}", source_id,
                     {"description": description})
            continue
        account = id_to_account.get(source_account or target_account or 0)
        target = run.accounts.get(account["name"]) if account else None
        if target is None or target.get("target_id") is None:
            run.difference("bill_account_not_migrated", f"{transaction_id}", True)
            run.skip(db, "bill", "bill_account_not_migrated", source_id,
                     {"account_id": source_account})
            continue
        if target["kind"] == "securities":
            run.skip(db, "bill", "bill_on_securities_account", source_id,
                     {"account": account["name"] if account else None})
            run.difference(
                "bill_on_securities_account",
                f"legacy transaction #{transaction_id} is booked on a securities account; "
                f"2.0 keeps securities cash inside the ledger, so the bill was not posted", True)
            continue
        if already_migrated(db, "bill", run.stock_sha, source_id) is not None:
            run.skip(db, "bill", "already_migrated", source_id)
            informational = True
            continue
        created = svc.bill(BillInput(
            request_id=request_id(run.stock_sha, "bill", source_id),
            account_id=target["target_id"], kind=kind,
            category_id=CATEGORY_BY_NATURE[nature], amount_fen=int(amount),
            occurred_at=parse_legacy_time(occurred_at),
            note=(description or "legacy bill")[:2000]))
        record(db, run, "bill", source_id, "finance_transactions", created["id"],
               {"nature": nature, "kind": kind, "amount_fen": int(amount)})
        informational = True
    if informational:
        run.bills_informational_only = True
        run.difference(
            "bills_informational_only",
            "the legacy manual balance already included the spending it recorded "
            "(current_balance_fen was updated by the old service on every bill), so the "
            "migrated bills are detail rows and never deduct the balance again")


def migrate_finance_snapshots(db, run: _Run, conn_finance: sqlite3.Connection) -> None:
    """Legacy balance-sheet snapshots are kept as labelled evidence, not authority."""
    if conn_finance.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='finance_balance_sheet_snapshot'").fetchone() is None:
        return
    rows = conn_finance.execute(
        "SELECT snapshot_date,total_assets_fen,total_liabilities_fen,net_worth_fen "
        "FROM finance_balance_sheet_snapshot ORDER BY snapshot_date").fetchall()
    for snapshot in rows:
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO legacy_finance_snapshots VALUES(?,?,?,?,?,?,?,?)",
                (uuid4().hex, run.run_id, str(snapshot[0]), int(snapshot[1]),
                 int(snapshot[2]), int(snapshot[3]), 1,
                 "legacy balance-sheet snapshot kept as evidence; 2.0 net worth comes from "
                 "balance adjustments plus the securities valuation"))
    if rows:
        run.difference(
            "legacy_snapshot_reference_only",
            f"{len(rows)} legacy balance-sheet snapshots are preserved for comparison but "
            f"do not drive the 2.0 net worth")


# --------------------------------------------------------------------------- #
# strategy evidence
# --------------------------------------------------------------------------- #
def migrate_strategy(db, run: _Run, conn_source: sqlite3.Connection,
                     instruments_by_code: dict[str, str]) -> None:
    """Store only verifiable evidence; never a trusted position."""
    watchlist = read_watchlist_codes(conn_source)
    for index_id, payload in read_strategy_positions(conn_source):
        source_id = f"strategy:{index_id}"
        etf = str(payload.get("etf_code") or "").strip()
        signal_date = str(payload.get("signal_date") or "").strip()
        version = str(payload.get("strategy_version") or "")
        adds = [add for add in payload.get("adds") or [] if isinstance(add, dict)]
        reasons: list[str] = []
        if not signal_date:
            reasons.append("signal_date_missing")
        else:
            try:
                date.fromisoformat(signal_date)
            except ValueError:
                reasons.append("signal_date_unparseable")
        entry_units = price_units(payload.get("entry_price"))
        if entry_units is None:
            reasons.append("entry_price_missing")
        shares = whole_quantity(payload.get("shares"))
        if shares is None:
            reasons.append("shares_missing")
        frozen_units = price_units(payload.get("signal_atr"))
        if payload.get("signal_atr") is None:
            reasons.append("frozen_atr_missing")
        elif frozen_units is None:
            reasons.append("frozen_atr_invalid")
        if version != STRATEGY_VERSION:
            reasons.append(f"strategy_version_mismatch:{version or 'empty'}")
        if not etf:
            reasons.append("etf_mapping_missing")
        elif etf not in instruments_by_code:
            reasons.append("etf_not_in_ledger")
        if payload.get("_unreadable"):
            reasons.append("cache_payload_unreadable")
        price_basis = ("forward_adjusted_watchlist" if etf in watchlist else UNADJUSTED_BASIS)
        if price_basis != UNADJUSTED_BASIS:
            run.difference(
                "price_basis_mixed",
                f"{index_id}: the strategy series is unadjusted (fqt=0) while the watchlist "
                f"channel holds forward-adjusted prices; ATR evidence must only be frozen "
                f"from the unadjusted series")
        verified = 0 if reasons else 1
        lot_role = "main" if shares else "sub"
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO strategy_evidence_legacy VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid4().hex, run.run_id, STRATEGY_KEY + index_id, index_id, etf, version,
                 lot_role, signal_date or None, payload.get("entry_date"),
                 units_to_price(entry_units) if entry_units else None,
                 units_to_price(frozen_units) if frozen_units else None,
                 shares or 0, len(adds), json.dumps(adds, ensure_ascii=False),
                 payload.get("cooldown_until"), price_basis, verified,
                 ", ".join(reasons) or "verified against the rebuilt ledger",
                 datetime.now(UTC).isoformat()))
        if not verified:
            key = ", ".join(reasons)
            run.unverifiable[key] = run.unverifiable.get(key, 0) + 1
        run.skip(db, "strategy", "strategy_cache_not_copied_as_position", source_id,
                 {"index_id": index_id, "verified": bool(verified)})
    run.difference(
        "strategy_evidence_only",
        "legacy strategy caches are stored in strategy_evidence_legacy as evidence; no "
        "strategy position is created, so strategy_positions stays empty until a verified "
        "signal rebuilds it")


# --------------------------------------------------------------------------- #
# report assembly
# --------------------------------------------------------------------------- #
def read_target_state(db) -> dict[str, Any]:
    with db.read() as conn:
        conn.row_factory = sqlite3.Row

        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in conn.execute(sql).fetchall()]

        state = {
            "accounts": rows("SELECT * FROM accounts ORDER BY id"),
            "trades": rows("SELECT * FROM trade_executions ORDER BY executed_at,rowid"),
            "lots": rows(
                "SELECT l.*,COALESCE(t.account_id,o.account_id) AS account_id,"
                "COALESCE(t.instrument_id,o.instrument_id) AS instrument_id "
                "FROM trade_lots l LEFT JOIN trade_executions t ON t.id=l.open_execution_id "
                "LEFT JOIN opening_positions o ON o.id=l.opening_id"),
            "cash": rows("SELECT m.* FROM effective_cash_movements m JOIN cash_movements raw ON raw.id=m.id ORDER BY m.occurred_at,raw.rowid"),
            "balances": rows("SELECT * FROM balance_adjustments WHERE status='effective' "
                             "ORDER BY occurred_at,rowid"),
            "bills": rows("SELECT * FROM finance_transactions WHERE status='effective' "
                          "ORDER BY occurred_at,rowid"),
            "instruments": {row["id"]: row for row in rows("SELECT * FROM instruments")},
            "evidence": rows("SELECT * FROM strategy_evidence_legacy ORDER BY index_id"),
            "runs": rows("SELECT * FROM migration_runs ORDER BY started_at"),
            "openings": rows("SELECT * FROM opening_positions"),
        }
        state["strategy_positions"] = conn.execute(
            "SELECT count(*) FROM strategy_positions").fetchone()[0]
    return state


def cash_of(state: dict[str, Any], account_id: str) -> int | None:
    facts=[row for row in state["cash"] if row["account_id"] == account_id]
    bases=[row for row in facts if row['kind']=='opening']
    if len(bases)!=1 or any(row['kind']!='opening' and row['occurred_at']<=bases[0]['occurred_at'] for row in facts):
        return None
    return sum(row['amount_fen'] for row in facts)


def known_cash(state, securities):
    balances=[cash_of(state,row['id']) for row in securities]
    return sum(balances) if all(value is not None for value in balances) else None


def latest_balance(state: dict[str, Any], account_id: str) -> int | None:
    rows = [row for row in state["balances"] if row["account_id"] == account_id]
    return rows[-1]["after_fen"] if rows else None


def positions_of(state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    securities = {row["id"]: row for row in state["accounts"] if row["kind"] == "securities"}
    positions: list[dict[str, Any]] = []
    by_code: dict[str, int] = {}
    for account_id, account in securities.items():
        buckets: dict[str, dict[str, int]] = {}
        for lot in state["lots"]:
            if lot["account_id"] != account_id or lot["status"] != "open":
                continue
            if not lot["remaining_qty"]:
                continue
            instrument = state["instruments"].get(lot["instrument_id"])
            if instrument is None:
                continue
            bucket = buckets.setdefault(instrument["code"],
                                        {"quantity": 0, "remaining_cost_fen": 0})
            bucket["quantity"] += lot["remaining_qty"]
            bucket["remaining_cost_fen"] += lot["remaining_cost_fen"]
        for code, bucket in sorted(buckets.items()):
            positions.append({"account": account["name"], "code": code,
                              "quantity": bucket["quantity"],
                              "remaining_cost_fen": bucket["remaining_cost_fen"]})
            by_code[code] = by_code.get(code, 0) + bucket["quantity"]
    return positions, by_code


def stock_section(run: _Run, state: dict[str, Any]) -> dict[str, Any]:
    positions, by_code = positions_of(state)
    securities = [row for row in state["accounts"] if row["kind"] == "securities"]
    counts: dict[str, int] = {}
    for item in run.pending:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    return {
        "source_trades": run.source_trades,
        "migrated_trades": run.migrated_trades,
        "pending_trades": [{"reason": reason, "count": count}
                           for reason, count in sorted(counts.items())],
        "pending_trades_detail": run.pending,
        "positions": positions,
        "securities_cash_fen": known_cash(state,securities),
        "known_securities_cash_fen": sum(value for row in securities if (value:=cash_of(state,row["id"])) is not None),
        "unknown_cash_accounts": [row["name"] for row in securities if cash_of(state,row["id"]) is None],
        "baseline_used": run.baseline_used,
        "baseline_coverage": "; ".join(run.baseline_coverage) or "no baseline was required",
        "unknown_fees": run.unknown_fees,
        "_quantity_by_code": by_code,
    }


def finance_section(run: _Run, state: dict[str, Any]) -> dict[str, Any]:
    securities = [row for row in state["accounts"] if row["kind"] == "securities"]
    ordinary = [row for row in state["accounts"]
                if row["kind"] != "securities" and row["asset_class"] == "asset"]
    liabilities = [row for row in state["accounts"]
                   if row["kind"] != "securities" and row["asset_class"] == "liability"]
    assets_fen = 0
    assets_known = True
    for account in ordinary:
        value = latest_balance(state, account["id"])
        if value is None:
            assets_known = False
        else:
            assets_fen += value
    liabilities_fen = 0
    liabilities_known = True
    for account in liabilities:
        value = latest_balance(state, account["id"])
        if value is None:
            liabilities_known = False
        else:
            liabilities_fen += value
    cash_fen = known_cash(state,securities)
    if any(latest_balance(state, row["id"]) is not None for row in securities):
        run.difference("securities_manual_balance_ignored",
                       "securities accounts carry ledger cash; a manual balance on them "
                       "would double count")
    months: dict[str, dict[str, int]] = {}
    for bill in state["bills"]:
        period = month_start(datetime.fromisoformat(bill["occurred_at"]).astimezone(SHANGHAI).date()).isoformat()
        bucket = months.setdefault(period, {"income_fen": 0, "expense_fen": 0,
                                            "fees_fen": 0})
        key = {"income": "income_fen", "expense": "expense_fen",
               "fee": "fees_fen"}[bill["kind"]]
        bucket[key] += bill["amount_fen"]
    month_rows = [{"period": period,
                   "income_fen": bucket["income_fen"],
                   "expense_fen": bucket["expense_fen"],
                   "fees_fen": bucket["fees_fen"],
                   "net_fen": bucket["income_fen"] - bucket["expense_fen"]
                                - bucket["fees_fen"]}
                  for period, bucket in sorted(months.items())]
    positions, _ = positions_of(state)
    market_value = None if positions else 0
    equity = cash_fen + market_value if cash_fen is not None and market_value is not None else None
    known = assets_known and liabilities_known and equity is not None
    return {
        "accounts": len(state["accounts"]),
        "ordinary_assets_fen": assets_fen if assets_known else None,
        "liabilities_fen": liabilities_fen if liabilities_known else None,
        "securities_equity_fen": equity,
        "securities_cash_fen": cash_fen,
        "securities_market_value_fen": market_value,
        "net_worth_fen": assets_fen - liabilities_fen + equity if known else None,
        "known_cash_and_manual_net_fen": assets_fen-liabilities_fen+sum(value for row in securities if (value:=cash_of(state,row["id"])) is not None) if assets_known and liabilities_known else None,
        "quality": "valid" if known else "derived_unverified",
        "months": month_rows,
        "excluded_duplicate_assets": run.duplicate_assets,
        "bills_informational_only": run.bills_informational_only,
    }


def strategy_section(run: _Run, state: dict[str, Any]) -> dict[str, Any]:
    evidence = state["evidence"]
    open_codes = {state["instruments"][lot["instrument_id"]]["code"]
                  for lot in state["lots"]
                  if lot["status"] == "open" and lot["instrument_id"] in state["instruments"]}
    unverifiable: dict[str, int] = dict(run.unverifiable)
    for row in evidence:
        if not row["verified"]:
            unverifiable[row["reason"] or "unverifiable"] = \
                unverifiable.get(row["reason"] or "unverifiable", 0) + (0 if row["reason"]
                                                                       in run.unverifiable
                                                                       else 1)
    return {
        "main_positions": sum(1 for row in evidence
                              if row["verified"] and row["lot_role"] == "main"),
        "sub_positions": sum(int(row["add_count"]) for row in evidence if row["verified"]),
        "matched_lots": sum(1 for row in evidence
                            if row["verified"] and row["etf_code"] in open_codes),
        "valid_frozen_atr": sum(1 for row in evidence if row["verified"] and row["frozen_atr"]),
        "unverifiable": [{"reason": reason, "count": count}
                         for reason, count in sorted(unverifiable.items())],
        "evidence_rows": len(evidence),
        "strategy_positions_rows": state["strategy_positions"],
        "cache_copied_as_positions": False,
    }


def build_report(run: _Run, state: dict[str, Any], source: dict[str, Any],
                 previous_run: bool) -> dict[str, Any]:
    stock = stock_section(run, state)
    stock.pop("_quantity_by_code", None)
    finance = finance_section(run, state)
    strategy = strategy_section(run, state)
    if stock["unknown_cash_accounts"]:
        run.difference("securities_cash_baseline_missing", "unknown cash accounts: "+", ".join(stock["unknown_cash_accounts"]), True)
    if finance["securities_market_value_fen"] is None:
        run.difference(
            "securities_market_value_unverified",
            "the legacy sources carry no trustworthy 2.0 quote series; equity and net worth "
            "are unavailable until the open holdings have verified quotes", True)
    if strategy["strategy_positions_rows"]:
        run.difference("strategy_positions_present",
                       "the target ledger already holds strategy positions; the migration "
                       "never creates them", True)
    skip_counts: dict[str, int] = {}
    for item in run.skips:
        skip_counts[item["reason"]] = skip_counts.get(item["reason"], 0) + 1
    blocked = (bool(run.tool_errors) or bool(run.pending)
               or any(item["blocks_takeover"] for item in run.differences))
    return {
        "source": source,
        "stock": stock,
        "finance": finance,
        "strategy": strategy,
        "differences": run.differences,
        "legacy_calculation_errors": run.legacy_errors,
        "migration_tool_errors": run.tool_errors,
        "skipped": [{"reason": reason, "count": count}
                    for reason, count in sorted(skip_counts.items())],
        "idempotency": {
            "source_sha256": run.stock_sha,
            "finance_sha256": run.finance_sha,
            "previous_run": previous_run,
            "skipped": [{"reason": reason, "count": count}
                        for reason, count in sorted(skip_counts.items())
                        if reason == "already_migrated"],
        },
        "mode": run.mode,
        "blocks_formal_takeover": blocked,
    }


def source_summary(stock_path: Path, finance_path: Path | None,
                   conn_source: sqlite3.Connection,
                   conn_finance: sqlite3.Connection | None) -> dict[str, Any]:
    stock_tables = tables_with_counts(conn_source)
    stock_schema = detect_stock_schema(conn_source, stock_tables)
    finance_tables = tables_with_counts(conn_finance) if conn_finance is not None else {}
    return {
        "stock_sha256": sha256_of(stock_path),
        "finance_sha256": sha256_of(finance_path) if finance_path else None,
        "stock_tables": stock_tables,
        "finance_tables": finance_tables,
        "detected_schema": {
            "stock": stock_schema["detected"],
            "finance": (detect_finance_schema(conn_finance, finance_tables)["detected"]
                        if conn_finance is not None else None),
        },
        "stock_path": str(stock_path.resolve()),
        "finance_path": str(finance_path.resolve()) if finance_path else None,
        "stock_size": Path(stock_path).stat().st_size,
        "finance_size": Path(finance_path).stat().st_size if finance_path else None,
    }


def previous_completed(db) -> dict[str, Any] | None:
    with db.read() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM migration_runs WHERE status='completed' "
            "ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _prepare_legacy(conn_source: sqlite3.Connection,
                    conn_finance: sqlite3.Connection | None) -> dict[str, Any]:
    trades = read_trades(conn_source)
    portfolio = read_portfolio(conn_source)
    accounts = load_legacy_accounts(conn_finance)
    codes = {str(row.get("code")) for row in trades if row.get("code")}
    codes |= {str(row.get("code")) for row in portfolio if row.get("code")}
    return {"trades": trades, "portfolio": portfolio, "accounts": accounts,
            "codes": {code for code in codes if code}}


def _execute(db, run: _Run, conn_source: sqlite3.Connection,
             conn_finance: sqlite3.Connection | None, legacy: dict[str, Any]) -> None:
    trade_svc = TradeService(db, reference_clock)
    portfolio_svc = PortfolioService(db, reference_clock)
    finance_svc = FinanceService(db, reference_clock)
    ensure_self_schema(db)
    # Earliest observed dates are evidence, not arbitrary calendar defaults.
    def add_date(dest, value, source):
        at = parse_legacy_time(value)
        if at is not None:
            dest.append((at, source))
    for row in legacy['trades']:
        if row.get('action') in BUY_ACTIONS | SELL_ACTIONS:
            add_date(run.stock_dates, row.get('trade_date') or row.get('date'), 'trade_history.execution')
    for row in legacy['portfolio']:
        for lot in row.get('lots') or []:
            if isinstance(lot, dict):
                add_date(run.stock_dates, lot.get('buy_date') or row.get('buy_date'), 'portfolio.lot.buy_date')
    for row in conn_source.execute('SELECT date FROM daily_snapshot WHERE cash_balance IS NOT NULL'):
        add_date(run.stock_dates, row[0], 'daily_snapshot.cash_balance')
    for row in read_transfer_history(conn_source):
        add_date(run.stock_dates, row.get('date'), 'transfer_data.date')
    if conn_finance is not None:
        for row in conn_finance.execute("SELECT occurred_at,source_account_id,target_account_id FROM finance_transaction WHERE status='POSTED'"):
            for account_id in (row[1], row[2]):
                if account_id is not None:
                    add_date(run.account_dates.setdefault(str(account_id), []), row[0], 'finance_transaction.occurred_at')
    securities = migrate_accounts(db, run, trade_svc, legacy["accounts"])
    securities_id = run.accounts[securities]["target_id"] if securities else None
    migrate_instruments(db, run, trade_svc, legacy["codes"])
    migrate_openings(db, run, portfolio_svc, conn_source, legacy["portfolio"],
                     {str(row.get("code")) for row in legacy["trades"]},
                     securities, securities_id)
    migrate_cash(db, run, trade_svc, conn_source, securities_id)
    migrate_trades(db, run, trade_svc, legacy["trades"], securities_id)
    if conn_finance is not None:
        migrate_finance(db, run, finance_svc, conn_finance, legacy["accounts"])
    migrate_strategy(db, run, conn_source, run.instruments)
    _, actual = positions_of(read_target_state(db))
    for entry in legacy['portfolio']:
        code = str(entry.get('code') or '')
        quantities = [whole_quantity(lot.get('qty')) for lot in entry.get('lots') or [] if isinstance(lot, dict)]
        if not code or not quantities or any(q is None for q in quantities):
            continue
        expected = sum(quantities)
        found = actual.get(code, 0)
        if expected != found:
            run.difference('source_target_quantity_unreconciled', f'{code}: legacy cached {expected}, migrated ledger {found}; unexplained difference requires review', True)


def _finish(db, run: _Run, source: dict[str, Any], *, previous_run: bool,
            dry_run: bool, target: Path, started: datetime,
            write_run: bool) -> dict[str, Any]:
    state = read_target_state(db)
    report = build_report(run, state, source, previous_run)
    # Use the same authoritative projection as the application, at one cutoff.
    # Legacy cache totals remain separate evidence rather than replacing facts.
    cutoff = reference_clock()
    service = FinanceService(db, reference_clock)
    overview = service.overview(cutoff)
    periods = [service.month(date.fromisoformat(row['period']), cutoff)
               for row in report['finance']['months']]
    with db.read() as conn:
        mapped = conn.execute("SELECT status,count(*) FROM trade_executions GROUP BY status").fetchall()
        fees = conn.execute("SELECT count(*),sum(fees_fen) FROM trade_executions WHERE status='effective'").fetchone()
    report['formal_projection'] = {
        'valuation_at': overview['as_of'], 'ledger_revision': overview['ledger_revision'],
        'quote_revision': overview['quote_revision'],
        'overview': {key: overview[key] for key in ('quality','asset_categories','securities_cash_fen',
            'securities_market_value_fen','securities_equity_fen','total_assets_fen','total_liabilities_fen',
            'net_worth_fen','reasons','accounts')},
        'months': [{key: value for key, value in item.items() if key != 'items'} for item in periods],
        'trade_counts': dict(mapped), 'effective_trade_count': fees[0],
        'effective_trade_fees_fen': fees[1],
        'source_valuation_at': None,
        'source_valuation_status': 'unverified_same_cutoff',
    }
    # We cannot certify a source-to-target valuation without source price/time proof.
    if overview['quality'] != 'valid' or report['formal_projection']['source_valuation_at'] is None:
        run.difference('formal_valuation_not_reconciled',
                       'formal projection available; source valuation at the same cutoff is not verified', True)
        report['blocks_formal_takeover'] = True
    if sha256_of(run.stock_path) != run.stock_sha or (run.finance_path and sha256_of(run.finance_path) != run.finance_sha):
        run.difference('source_changed_during_run', 'source checksum changed during migration; discard this sandbox from takeover', True)
        report['blocks_formal_takeover'] = True
    report["target_identity"] = {"uuid": db.identity.database_uuid, "generation": db.identity.generation,
                                 "schema_version": db.identity.schema_version}
    report["run_id"] = run.run_id
    report["target"] = str(Path(target).resolve())
    report["dry_run"] = dry_run
    report['migration_protocol'] = MIGRATION_PROTOCOL
    if write_run:
        with db.transaction() as conn:
            conn.execute("UPDATE migration_runs SET status='completed',finished_at=?,migrated=?,skipped=?,pending=?,summary=? WHERE id=? AND status='running'",
                         (datetime.now(UTC).isoformat(), run.migrated_trades, len(run.skips), len(run.pending),
                          json.dumps(report, ensure_ascii=False, sort_keys=True), run.run_id))
    return report


def _open_sources(stock_db: Path, finance_db: Path | None):
    """Yield both read-only source connections plus a closer."""
    stock_ctx = open_source(stock_db)
    conn_source = stock_ctx.__enter__()
    finance_ctx = open_source(finance_db) if finance_db else None
    conn_finance = finance_ctx.__enter__() if finance_ctx else None
    return conn_source, conn_finance, (stock_ctx, finance_ctx)


def run_migration(stock_db: Path, finance_db: Path | None, target: Path, *,
                  confirm: bool, target_role: str = "sandbox") -> dict[str, Any]:
    """Migrate into an isolated sandbox target and return the reconciliation report."""
    stock_db = Path(stock_db).resolve()
    finance_db = Path(finance_db).resolve() if finance_db else None
    if not confirm:
        raise Refused("migration writes are refused without --confirm")
    target = target_guard(target, target_role)
    started = datetime.now(UTC)
    conn_source, conn_finance, contexts = _open_sources(stock_db, finance_db)
    try:
        source = source_summary(stock_db, finance_db, conn_source, conn_finance)
        run = _Run(stock_sha=source["stock_sha256"], finance_sha=source["finance_sha256"],
                   run_id=uuid4().hex, stock_path=stock_db, finance_path=finance_db,
                   mode="migrate", target_role=target_role)
        if source['detected_schema']['stock'] != STOCK_SCHEMA or (finance_db and source['detected_schema']['finance'] != f'legacy-finance-v{FINANCE_SCHEMA}'):
            raise Refused('unsupported legacy schema; inspect read-only probe before migration')
        legacy = _prepare_legacy(conn_source, conn_finance)
        run.source_trades = len(legacy["trades"])
        with target_database(target) as (db, identity, real):
            ensure_self_schema(db)
            with db.read() as conn:
                interrupted = conn.execute("SELECT id FROM migration_runs WHERE status!='completed' LIMIT 1").fetchone()
                orphaned = conn.execute("SELECT 1 FROM migration_ledger l LEFT JOIN migration_runs r ON r.id=l.run_id WHERE r.id IS NULL LIMIT 1").fetchone()
            if interrupted or orphaned:
                raise Refused('interrupted or orphaned migration; preserve target and rehearse into a new sandbox')
            previous = previous_completed(db)
            if previous and json.loads(previous['summary']).get('migration_protocol') != MIGRATION_PROTOCOL:
                raise Refused('migration protocol mismatch; use a fresh sandbox')
            same_source = (previous is not None
                           and previous["stock_sha256"] == run.stock_sha
                           and previous["finance_sha256"] == run.finance_sha)
            if previous is not None and not same_source:
                run.difference(
                    "source_changed_since_last_run",
                    f"the target already holds a completed migration of "
                    f"{previous['stock_sha256'][:12]}; the current source is "
                    f"{run.stock_sha[:12]} — a rehearsal or manual review is required "
                    f"before any further write", True)
                run.tool_error(
                    "changed_source_not_overwritten",
                    "refusing to merge facts from a different source checksum into a "
                    "target that already holds a completed migration")
                return _finish(db, run, source, previous_run=True, dry_run=False,
                               target=real, started=started, write_run=False)
            with db.transaction() as conn:
                conn.execute('INSERT INTO migration_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (run.run_id, 'stock+finance' if run.finance_sha else 'stock', run.stock_sha,
                              run.finance_sha, str(run.stock_path), str(run.finance_path) if run.finance_path else None,
                              run.mode, run.target_role, 'running', started.isoformat(), None, 0, 0, 0,
                              json.dumps({'migration_protocol': MIGRATION_PROTOCOL, 'blocks_formal_takeover': True})))
            _execute(db, run, conn_source, conn_finance, legacy)
            if previous:
                old = json.loads(previous['summary'])
                for item in old.get('differences', []):
                    if item not in run.differences:
                        run.differences.append(item)
                for item in old.get('stock', {}).get('pending_trades_detail', []):
                    if item not in run.pending:
                        run.pending.append(item)
                for key, items in [('legacy_calculation_errors', run.legacy_errors), ('migration_tool_errors', run.tool_errors)]:
                    for item in old.get(key, []):
                        if item not in items:
                            items.append(item)
            return _finish(db, run, source, previous_run=previous is not None,
                           dry_run=False, target=real, started=started, write_run=True)
    finally:
        stock_ctx, finance_ctx = contexts
        if finance_ctx is not None:
            finance_ctx.__exit__(None, None, None)
        stock_ctx.__exit__(None, None, None)


def rehearsal(stock_db: Path, finance_db: Path | None, target: Path) -> dict[str, Any]:
    """Run the whole migration against a throwaway sandbox; ``--target`` is untouched."""
    stock_db = Path(stock_db).resolve()
    finance_db = Path(finance_db).resolve() if finance_db else None
    target_guard(target, "sandbox")
    started = datetime.now(UTC)
    conn_source, conn_finance, contexts = _open_sources(stock_db, finance_db)
    try:
        source = source_summary(stock_db, finance_db, conn_source, conn_finance)
        run = _Run(stock_sha=source["stock_sha256"], finance_sha=source["finance_sha256"],
                   run_id=uuid4().hex, stock_path=stock_db, finance_path=finance_db,
                   mode="dry-run", target_role="sandbox")
        if source['detected_schema']['stock'] != STOCK_SCHEMA or (finance_db and source['detected_schema']['finance'] != f'legacy-finance-v{FINANCE_SCHEMA}'):
            raise Refused('unsupported legacy schema; inspect read-only probe before migration')
        legacy = _prepare_legacy(conn_source, conn_finance)
        run.source_trades = len(legacy["trades"])
        with rehearsal_database() as (db, identity, path):
            _execute(db, run, conn_source, conn_finance, legacy)
            return _finish(db, run, source, previous_run=False, dry_run=True,
                           target=target, started=started, write_run=False)
    finally:
        stock_ctx, finance_ctx = contexts
        if finance_ctx is not None:
            finance_ctx.__exit__(None, None, None)
        stock_ctx.__exit__(None, None, None)


def report_from_target(target: Path) -> dict[str, Any]:
    """Read a migrated sandbox and rebuild the report from its own records."""
    target = Path(target).resolve()
    if not target.is_file():
        raise Refused("reporting requires an existing migrated database")
    with closing(sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)) as conn:
        identity = read_identity(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT summary,status FROM migration_runs ORDER BY started_at DESC,rowid DESC "
            "LIMIT 1").fetchone()
        if row is None:
            raise Refused("the database holds no recorded migration run")
        report = json.loads(row[0])
        if row[1] != 'completed':
            report['blocks_formal_takeover'] = True
            report['migration_status'] = row[1]
            report['reason'] = 'migration incomplete; preserve target and use a new sandbox'
        counts = tables_with_counts(conn)
        evidence = conn.execute(
            "SELECT count(*) FROM strategy_evidence_legacy").fetchone()[0]
        strategy_positions = conn.execute(
            "SELECT count(*) FROM strategy_positions").fetchone()[0]
        projection = report.get('formal_projection') or {}
        ledger_revision = conn.execute('SELECT ledger_revision FROM ledger_state').fetchone()[0]
        quote_revision = conn.execute('SELECT quote_revision FROM quote_state').fetchone()[0]
        if (projection.get('ledger_revision'), projection.get('quote_revision')) != (ledger_revision, quote_revision):
            report['blocks_formal_takeover'] = True
            report['projection_stale'] = True
    report["reported_from"] = str(target)
    report["reported_role"] = identity.instance_role
    report["reported_tables"] = counts
    strategy = report.setdefault("strategy", {})
    strategy["strategy_positions_rows"] = strategy_positions
    strategy["evidence_rows"] = evidence
    return report


def report_from_file(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise Refused("report file does not exist")
    return json.loads(path.read_text(encoding="utf-8"))


def write_report(report: dict[str, Any], destination: Path) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return destination
