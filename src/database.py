"""
SQLite Star Schema + governance/audit store.

Tables
------
Dimensions : dim_product_catalog, dim_production_process, dim_sop_documentation, dim_date
Fact       : fact_operations_quality
Governance : audit_sla_runs
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pandas as pd

from src import config

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dim_product_catalog (
    product_id        TEXT PRIMARY KEY,
    sku_code          TEXT NOT NULL UNIQUE,
    product_category  TEXT NOT NULL,
    spec_min_mm       REAL NOT NULL,
    spec_max_mm       REAL NOT NULL,
    CHECK (spec_min_mm < spec_max_mm)
);

CREATE TABLE IF NOT EXISTS dim_production_process (
    process_id        TEXT PRIMARY KEY,
    process_name      TEXT NOT NULL,
    production_line   TEXT NOT NULL,
    target_cycle_sec  REAL NOT NULL CHECK (target_cycle_sec > 0)
);

CREATE TABLE IF NOT EXISTS dim_sop_documentation (
    sop_id              TEXT PRIMARY KEY,
    sop_title           TEXT NOT NULL,
    version             TEXT NOT NULL,
    review_status       TEXT NOT NULL CHECK (review_status IN ('Approved', 'In Review', 'Expired')),
    owner_role          TEXT NOT NULL,
    last_reviewed_date  TEXT NOT NULL,
    approval_sla_days   INTEGER NOT NULL,
    actual_review_days  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_date (
    date_key      INTEGER PRIMARY KEY,
    full_date     TEXT NOT NULL UNIQUE,
    year          INTEGER NOT NULL,
    quarter       INTEGER NOT NULL,
    month         INTEGER NOT NULL,
    week_of_year  INTEGER NOT NULL,
    day_name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_operations_quality (
    inspection_id          TEXT PRIMARY KEY,
    batch_id               TEXT NOT NULL,
    inspection_date        TEXT NOT NULL,
    product_id             TEXT NOT NULL REFERENCES dim_product_catalog(product_id),
    process_id             TEXT NOT NULL REFERENCES dim_production_process(process_id),
    sop_id                 TEXT NOT NULL REFERENCES dim_sop_documentation(sop_id),
    operator_id            TEXT,
    measurement_mm         REAL NOT NULL,
    cycle_time_sec         REAL,
    defect_flag            INTEGER NOT NULL DEFAULT 0,
    first_pass_yield_flag  INTEGER NOT NULL DEFAULT 1,
    validated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_fact_batch ON fact_operations_quality(batch_id);
CREATE INDEX IF NOT EXISTS ix_fact_date  ON fact_operations_quality(inspection_date);

CREATE TABLE IF NOT EXISTS audit_sla_runs (
    run_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp         TEXT NOT NULL,
    batch_filename        TEXT NOT NULL,
    total_records         INTEGER NOT NULL,
    valid_records         INTEGER NOT NULL,
    invalid_records       INTEGER NOT NULL,
    health_score_pct      REAL NOT NULL,
    sla_status            TEXT NOT NULL CHECK (sla_status IN ('PASS', 'BREACH')),
    lkg_preserved_flag    INTEGER NOT NULL DEFAULT 0,
    email_triggered_flag  INTEGER NOT NULL DEFAULT 0,
    quarantine_file_path  TEXT,
    error_breakdown_json  TEXT
);
"""

FACT_COLUMNS: list[str] = [
    "inspection_id", "batch_id", "inspection_date", "product_id", "process_id",
    "sop_id", "operator_id", "measurement_mm", "cycle_time_sec", "defect_flag",
    "first_pass_yield_flag", "validated_at",
]


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else Path(config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def table_row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #
def read_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {table}", conn)


def load_dimensions(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    return {
        "products": read_table(conn, "dim_product_catalog"),
        "processes": read_table(conn, "dim_production_process"),
        "sops": read_table(conn, "dim_sop_documentation"),
    }


def load_production_keys(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT inspection_id, batch_id FROM fact_operations_quality", conn
    )


def load_audit_runs(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM audit_sla_runs ORDER BY run_id", conn)
    if not df.empty:
        df["run_timestamp"] = pd.to_datetime(df["run_timestamp"], utc=True, errors="coerce")
    return df


def latest_audit_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM audit_sla_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM audit_sla_runs LIMIT 0").description]
    return dict(zip(cols, row))


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def upsert_facts(conn: sqlite3.Connection, clean_df: pd.DataFrame) -> int:
    """
    Idempotent load: rows from the same batch_id are replaced first (a batch
    re-run supersedes its earlier load), then rows are INSERT OR REPLACE'd.
    """
    if clean_df.empty:
        return 0
    df = clean_df[FACT_COLUMNS].copy()
    batch_ids = df["batch_id"].dropna().unique().tolist()
    conn.executemany(
        "DELETE FROM fact_operations_quality WHERE batch_id = ?",
        [(b,) for b in batch_ids],
    )
    placeholders = ",".join("?" * len(FACT_COLUMNS))
    records = [
        tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in row)
        for row in df.itertuples(index=False, name=None)
    ]
    conn.executemany(
        f"INSERT OR REPLACE INTO fact_operations_quality ({','.join(FACT_COLUMNS)}) "
        f"VALUES ({placeholders})",
        records,
    )
    conn.commit()
    return len(records)


def insert_audit_run(conn: sqlite3.Connection, run: dict) -> int:
    payload = dict(run)
    if isinstance(payload.get("error_breakdown_json"), dict):
        payload["error_breakdown_json"] = json.dumps(payload["error_breakdown_json"])
    cols = [
        "run_timestamp", "batch_filename", "total_records", "valid_records",
        "invalid_records", "health_score_pct", "sla_status", "lkg_preserved_flag",
        "email_triggered_flag", "quarantine_file_path", "error_breakdown_json",
    ]
    cur = conn.execute(
        f"INSERT INTO audit_sla_runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [payload.get(c) for c in cols],
    )
    conn.commit()
    return int(cur.lastrowid)


# --------------------------------------------------------------------------- #
# Power BI export
# --------------------------------------------------------------------------- #
def export_star_schema(conn: sqlite3.Connection, out_dir: Path | None = None) -> dict[str, Path]:
    """Write the full Star Schema to CSVs consumed by Power BI."""
    target = Path(out_dir) if out_dir else Path(config.PRODUCTION_BI_DIR)
    target.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for table, filename in config.BI_EXPORT_FILES.items():
        df = read_table(conn, table)
        if table == "fact_operations_quality" and not df.empty:
            # Surrogate date_key for a clean Power BI relationship to Dim_Date
            df.insert(
                3, "date_key",
                pd.to_datetime(df["inspection_date"]).dt.strftime("%Y%m%d").astype(int),
            )
            df = df.sort_values(["inspection_date", "inspection_id"])
        path = target / filename
        df.to_csv(path, index=False)
        written[table] = path
    return written
