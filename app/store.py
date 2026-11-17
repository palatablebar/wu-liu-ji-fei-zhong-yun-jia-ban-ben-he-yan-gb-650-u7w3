"""SQLite 存取：运行清单、运价版本、运单、隔离区、计费重行、账单行。

只放数据访问，不放业务规则。每次调用开一个连接，用完即关。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  input_name TEXT NOT NULL DEFAULT '',
  input_sha256 TEXT NOT NULL DEFAULT '',
  rules_version TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '',
  rows_in INTEGER NOT NULL DEFAULT 0,
  rows_ok INTEGER NOT NULL DEFAULT 0,
  rows_quarantined INTEGER NOT NULL DEFAULT 0,
  amount_total TEXT NOT NULL DEFAULT '0.00',
  rate_digest TEXT NOT NULL DEFAULT '',
  stages TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rate_sets (
  carrier TEXT PRIMARY KEY,
  rate_set_version INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_cards (
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  version_id TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT NOT NULL,
  first_weight_kg TEXT NOT NULL,
  first_price TEXT NOT NULL,
  step_weight_kg TEXT NOT NULL,
  step_price TEXT NOT NULL,
  min_charge TEXT NOT NULL,
  volume_divisor INTEGER NOT NULL,
  fuel_pct TEXT NOT NULL,
  remote_fee TEXT NOT NULL,
  insurance_pct TEXT NOT NULL,
  currency TEXT NOT NULL,
  rate_set_version INTEGER NOT NULL,
  PRIMARY KEY (carrier, service, zone, version_id)
);
CREATE TABLE IF NOT EXISTS waybills (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL,
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  pickup_utc TEXT NOT NULL,
  weight_kg TEXT NOT NULL,
  length_cm TEXT NOT NULL,
  width_cm TEXT NOT NULL,
  height_cm TEXT NOT NULL,
  declared_value TEXT NOT NULL,
  pieces INTEGER NOT NULL,
  codes TEXT NOT NULL DEFAULT '',
  raw_weight TEXT NOT NULL DEFAULT '',
  weight_unit TEXT NOT NULL DEFAULT 'kg',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS quarantine (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL DEFAULT '',
  code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  raw TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, line_no, code)
);
CREATE TABLE IF NOT EXISTS weight_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  actual_kg TEXT NOT NULL DEFAULT '0.000',
  volumetric_kg TEXT NOT NULL DEFAULT '0.000',
  source TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  steps INTEGER NOT NULL DEFAULT 0,
  codes TEXT NOT NULL DEFAULT '',
  weight_unit TEXT NOT NULL DEFAULT 'kg',
  raw_weight TEXT NOT NULL DEFAULT '',
  conversion_factor TEXT NOT NULL DEFAULT '1',
  conversion_expr TEXT NOT NULL DEFAULT '',
  carrier_control_lb TEXT NOT NULL DEFAULT '',
  carrier_control_kg TEXT NOT NULL DEFAULT '',
  tier_basis TEXT NOT NULL DEFAULT 'contract_kg',
  rate_set_version INTEGER NOT NULL DEFAULT 0,
  rate_digest TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  recalc_seq INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS weight_events (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL DEFAULT '',
  raw_weight TEXT NOT NULL DEFAULT '',
  weight_unit TEXT NOT NULL DEFAULT '',
  actual_kg TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '',
  carrier_control_lb TEXT NOT NULL DEFAULT '',
  carrier_control_kg TEXT NOT NULL DEFAULT '',
  version_id TEXT NOT NULL DEFAULT '',
  rate_set_version INTEGER NOT NULL DEFAULT 0,
  rate_digest TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no, seq)
);
CREATE TABLE IF NOT EXISTS charge_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00',
  remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00',
  insurance_amount TEXT NOT NULL DEFAULT '0.00',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  codes TEXT NOT NULL DEFAULT '',
  locked INTEGER NOT NULL DEFAULT 0,
  locked_at TEXT NOT NULL DEFAULT '',
  weight_revision INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (run_id, waybill_no)
);
"""

# 旧库增量列：init_db 建表后逐列补齐，已存在则跳过（SQLite 无 IF NOT EXISTS 加列语法）。
# 每条 = (表名, 列名, 列定义)
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("weight_lines", "weight_unit", "TEXT NOT NULL DEFAULT 'kg'"),
    ("weight_lines", "raw_weight", "TEXT NOT NULL DEFAULT ''"),
    ("weight_lines", "conversion_factor", "TEXT NOT NULL DEFAULT '1'"),
    ("weight_lines", "conversion_expr", "TEXT NOT NULL DEFAULT ''"),
    ("weight_lines", "carrier_control_lb", "TEXT NOT NULL DEFAULT ''"),
    ("weight_lines", "carrier_control_kg", "TEXT NOT NULL DEFAULT ''"),
    ("weight_lines", "tier_basis", "TEXT NOT NULL DEFAULT 'contract_kg'"),
    ("weight_lines", "rate_set_version", "INTEGER NOT NULL DEFAULT 0"),
    ("weight_lines", "rate_digest", "TEXT NOT NULL DEFAULT ''"),
    ("weight_lines", "revision", "INTEGER NOT NULL DEFAULT 1"),
    ("weight_lines", "recalc_seq", "INTEGER NOT NULL DEFAULT 0"),
    ("charge_lines", "locked", "INTEGER NOT NULL DEFAULT 0"),
    ("charge_lines", "locked_at", "TEXT NOT NULL DEFAULT ''"),
    ("charge_lines", "weight_revision", "INTEGER NOT NULL DEFAULT 1"),
    ("waybills", "raw_weight", "TEXT NOT NULL DEFAULT ''"),
    ("waybills", "weight_unit", "TEXT NOT NULL DEFAULT 'kg'"),
)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate_columns(conn)


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with session() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    got = rows(sql, params)
    return got[0] if got else None


def execute(sql: str, params: tuple = ()) -> None:
    with session() as conn:
        conn.execute(sql, params)


def executemany(sql: str, seq) -> None:
    with session() as conn:
        conn.executemany(sql, list(seq))


# --- 运行清单 -------------------------------------------------------------

RUN_COLUMNS = (
    "input_name", "input_sha256", "rules_version", "profile",
    "rows_in", "rows_ok", "rows_quarantined", "amount_total",
    "rate_digest", "stages", "notes", "created_at", "updated_at",
)


def get_run(run_id: str) -> dict | None:
    return one("SELECT * FROM runs WHERE run_id = ?", (run_id,))


def list_runs(limit: int = 20) -> list[dict]:
    return rows("SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?", (limit,))


def upsert_run(run_id: str, data: dict) -> None:
    with session() as conn:
        existing = conn.execute("SELECT run_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if existing:
            fields = [c for c in RUN_COLUMNS if c in data]
            assignments = ", ".join(f"{c} = ?" for c in fields)
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                tuple(data[c] for c in fields) + (run_id,),
            )
        else:
            payload = {c: data.get(c, "") for c in RUN_COLUMNS}
            payload["rows_in"] = int(payload.get("rows_in") or 0)
            payload["rows_ok"] = int(payload.get("rows_ok") or 0)
            payload["rows_quarantined"] = int(payload.get("rows_quarantined") or 0)
            cols = ("run_id",) + RUN_COLUMNS
            conn.execute(
                f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (run_id,) + tuple(payload[c] for c in RUN_COLUMNS),
            )


def add_stage(run_id: str, stage: str, updated_at: str) -> None:
    run = get_run(run_id)
    if not run:
        return
    parts = [p for p in (run.get("stages") or "").split(",") if p]
    if stage not in parts:
        parts.append(stage)
    execute(
        "UPDATE runs SET stages = ?, updated_at = ? WHERE run_id = ?",
        (",".join(parts), updated_at, run_id),
    )


# --- 运价版本 -------------------------------------------------------------

CARD_COLUMNS = (
    "carrier", "service", "zone", "version_id", "effective_from", "effective_to",
    "first_weight_kg", "first_price", "step_weight_kg", "step_price", "min_charge",
    "volume_divisor", "fuel_pct", "remote_fee", "insurance_pct", "currency",
    "rate_set_version",
)


def replace_rate_cards(cards: list[dict], rate_set_version: int, updated_at: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM rate_cards")
        conn.executemany(
            f"INSERT INTO rate_cards ({', '.join(CARD_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in CARD_COLUMNS)})",
            [tuple(card[c] for c in CARD_COLUMNS) for card in cards],
        )
        carriers = sorted({card["carrier"] for card in cards})
        conn.execute("DELETE FROM rate_sets")
        conn.executemany(
            "INSERT INTO rate_sets (carrier, rate_set_version, updated_at) VALUES (?, ?, ?)",
            [(c, rate_set_version, updated_at) for c in carriers],
        )


def rate_cards() -> list[dict]:
    return rows("SELECT * FROM rate_cards ORDER BY carrier, service, zone, effective_from")


def rate_set_version(carrier: str) -> int:
    got = one("SELECT rate_set_version FROM rate_sets WHERE carrier = ?", (carrier,))
    return int(got["rate_set_version"]) if got else 0


def max_rate_set_version() -> int:
    got = one("SELECT MAX(rate_set_version) AS v FROM rate_sets")
    return int(got["v"]) if got and got["v"] is not None else 0


# --- 运单 / 隔离区 / 计费行 ------------------------------------------------

def replace_waybills(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM waybills WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO waybills (run_id, line_no, waybill_no, carrier, service, zone, "
            "pickup_utc, weight_kg, length_cm, width_cm, height_cm, declared_value, pieces, "
            "codes, raw_weight, weight_unit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["line_no"], it["waybill_no"], it["carrier"], it["service"],
                    it["zone"], it["pickup_utc"], it["weight_kg"], it["length_cm"],
                    it["width_cm"], it["height_cm"], it["declared_value"], it["pieces"],
                    it["codes"], it.get("raw_weight", ""), it.get("weight_unit", "kg"),
                )
                for it in items
            ],
        )


def waybills(run_id: str) -> list[dict]:
    return rows("SELECT * FROM waybills WHERE run_id = ? ORDER BY line_no", (run_id,))


def upsert_waybill(run_id: str, item: dict) -> None:
    """上游磅重更新：按业务键 (run_id, waybill_no) 幂等覆盖重量相关列，不新增行。"""
    columns = (
        "line_no", "carrier", "service", "zone", "pickup_utc", "weight_kg", "length_cm",
        "width_cm", "height_cm", "declared_value", "pieces", "codes", "raw_weight",
        "weight_unit",
    )
    cols = [c for c in columns if c in item]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
    with session() as conn:
        conn.execute(
            f"INSERT INTO waybills (run_id, waybill_no, {', '.join(cols)}) "
            f"VALUES (?, ?, {placeholders}) "
            f"ON CONFLICT(run_id, waybill_no) DO UPDATE SET {updates}",
            (run_id, item["waybill_no"]) + tuple(item[c] for c in cols),
        )


def replace_quarantine(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM quarantine WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO quarantine (run_id, line_no, waybill_no, code, detail, raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, it["line_no"], it["waybill_no"], it["code"], it["detail"], it["raw"])
                for it in items
            ],
        )


def quarantine(run_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM quarantine WHERE run_id = ? ORDER BY line_no, code", (run_id,)
    )


WEIGHT_LINE_COLUMNS = (
    "waybill_no", "version_id", "actual_kg", "volumetric_kg", "source", "billable_kg",
    "steps", "codes", "weight_unit", "raw_weight", "conversion_factor", "conversion_expr",
    "carrier_control_lb", "carrier_control_kg", "tier_basis", "rate_set_version",
    "rate_digest", "revision", "recalc_seq",
)


def replace_weight_lines(run_id: str, items: list[dict]) -> None:
    """整批重写计费重行。重算路径改用 upsert_weight_lines 以保留 revision/审计列。"""
    placeholders = ", ".join("?" for _ in WEIGHT_LINE_COLUMNS)
    with session() as conn:
        conn.execute("DELETE FROM weight_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            f"INSERT INTO weight_lines (run_id, {', '.join(WEIGHT_LINE_COLUMNS)}) "
            f"VALUES (?, {placeholders})",
            [(run_id,) + tuple(it.get(c, _weight_default(c)) for c in WEIGHT_LINE_COLUMNS)
             for it in items],
        )


_WEIGHT_DEFAULTS: dict[str, object] = {
    "version_id": "", "source": "", "codes": "", "weight_unit": "kg", "raw_weight": "",
    "conversion_factor": "1", "conversion_expr": "", "carrier_control_lb": "",
    "carrier_control_kg": "", "tier_basis": "contract_kg", "rate_digest": "",
    "actual_kg": "0.000", "volumetric_kg": "0.000", "billable_kg": "0.000",
    "steps": 0, "rate_set_version": 0, "revision": 1, "recalc_seq": 0,
}


def _weight_default(column: str):
    return _WEIGHT_DEFAULTS.get(column, "")


def upsert_weight_line(run_id: str, item: dict) -> None:
    """单行幂等写：存在则按主键更新审计与结果列，不存在则插入。重算不新增重复行。"""
    cols = [c for c in WEIGHT_LINE_COLUMNS if c in item]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
    with session() as conn:
        conn.execute(
            f"INSERT INTO weight_lines (run_id, {', '.join(cols)}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(run_id, waybill_no) DO UPDATE SET {updates}",
            (run_id,) + tuple(item[c] for c in cols),
        )


def weight_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM weight_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))


def weight_line(run_id: str, waybill_no: str) -> dict | None:
    return one(
        "SELECT * FROM weight_lines WHERE run_id = ? AND waybill_no = ?",
        (run_id, waybill_no),
    )


def insert_weight_event(run_id: str, item: dict) -> None:
    """追加一条重量事件（首算/上游更新/重算）。只追加不更新，历史不可改。"""
    columns = (
        "waybill_no", "seq", "event_type", "raw_weight", "weight_unit", "actual_kg",
        "billable_kg", "carrier_control_lb", "carrier_control_kg", "version_id",
        "rate_set_version", "rate_digest", "reason", "created_at",
    )
    placeholders = ", ".join("?" for _ in columns)
    with session() as conn:
        conn.execute(
            f"INSERT INTO weight_events (run_id, {', '.join(columns)}) "
            f"VALUES (?, {placeholders})",
            (run_id,) + tuple(item.get(c, "") for c in columns),
        )


def weight_events(run_id: str, waybill_no: str = "") -> list[dict]:
    if waybill_no:
        return rows(
            "SELECT * FROM weight_events WHERE run_id = ? AND waybill_no = ? "
            "ORDER BY waybill_no, seq",
            (run_id, waybill_no),
        )
    return rows(
        "SELECT * FROM weight_events WHERE run_id = ? ORDER BY waybill_no, seq", (run_id,)
    )


def next_weight_seq(run_id: str, waybill_no: str) -> int:
    got = one(
        "SELECT COALESCE(MAX(seq), 0) AS seq FROM weight_events "
        "WHERE run_id = ? AND waybill_no = ?",
        (run_id, waybill_no),
    )
    return int(got["seq"]) + 1 if got else 1


CHARGE_LINE_COLUMNS = (
    "waybill_no", "version_id", "base_amount", "step_amount", "remote_amount",
    "fuel_amount", "insurance_amount", "total_amount", "codes", "locked", "locked_at",
    "weight_revision",
)


def replace_charge_lines(run_id: str, items: list[dict]) -> None:
    """整批重写账单行：已锁定（已核）的运单金额原样保留，绝不被重算覆盖。"""
    placeholders = ", ".join("?" for _ in CHARGE_LINE_COLUMNS)
    with session() as conn:
        locked = {
            r["waybill_no"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM charge_lines WHERE run_id = ? AND locked = 1", (run_id,)
            ).fetchall()
        }
        conn.execute("DELETE FROM charge_lines WHERE run_id = ?", (run_id,))
        payload = []
        for it in items:
            kept = locked.get(it["waybill_no"])
            if kept is not None:
                payload.append((run_id,) + tuple(kept[c] for c in CHARGE_LINE_COLUMNS))
            else:
                payload.append(
                    (run_id,) + tuple(it.get(c, _charge_default(c)) for c in CHARGE_LINE_COLUMNS)
                )
        # 已锁定但本次未出现在新结果里的行（例如运单被隔离）仍需保留
        incoming = {it["waybill_no"] for it in items}
        for waybill_no, kept in locked.items():
            if waybill_no not in incoming:
                payload.append((run_id,) + tuple(kept[c] for c in CHARGE_LINE_COLUMNS))
        conn.executemany(
            f"INSERT INTO charge_lines (run_id, {', '.join(CHARGE_LINE_COLUMNS)}) "
            f"VALUES (?, {placeholders})",
            payload,
        )


_CHARGE_DEFAULTS: dict[str, object] = {
    "version_id": "", "codes": "", "locked_at": "",
    "base_amount": "0.00", "step_amount": "0.00", "remote_amount": "0.00",
    "fuel_amount": "0.00", "insurance_amount": "0.00", "total_amount": "0.00",
    "locked": 0, "weight_revision": 1,
}


def _charge_default(column: str):
    return _CHARGE_DEFAULTS.get(column, "")


def lock_charge_line(run_id: str, waybill_no: str, locked_at: str) -> bool:
    """把一票账单标记为已核。返回是否实际发生了状态变化。"""
    with session() as conn:
        cur = conn.execute(
            "UPDATE charge_lines SET locked = 1, locked_at = ? "
            "WHERE run_id = ? AND waybill_no = ? AND locked = 0",
            (locked_at, run_id, waybill_no),
        )
        return cur.rowcount > 0


def locked_waybills(run_id: str) -> set[str]:
    return {r["waybill_no"] for r in rows(
        "SELECT waybill_no FROM charge_lines WHERE run_id = ? AND locked = 1", (run_id,)
    )}


def charge_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM charge_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))


def latest_run_id() -> str:
    got = one("SELECT run_id FROM runs ORDER BY created_at DESC, run_id DESC LIMIT 1")
    return got["run_id"] if got else ""
