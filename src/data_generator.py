"""
Synthetic — but realistic — industrial operations-quality datasets.

Run ``python -m src.data_generator`` (or ``--force`` to rebuild from scratch).

Seeds:
1. Master dimensions (products, processes, SOP / work instructions, calendar).
2. 12 days of historical SLA audit runs (88-98 %, plus one 74 % breach).
3. A certified Last-Known-Good (LKG) Star-Schema snapshot in data/production_bi/.
4. Two 500-row landing-zone batches:
   * batch_2026_10_30_healthy.csv   -> 482 / 500 valid  (96.4 %, SLA PASS)
   * batch_2026_10_31_corrupted.csv -> 356 / 500 valid  (71.2 %, SLA BREACH)
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src import config, database

RNG_SEED = 20261031

# --------------------------------------------------------------------------- #
# Master data
# --------------------------------------------------------------------------- #
PRODUCTS = [
    # product_id, sku_code, product_category, spec_min_mm, spec_max_mm
    ("P001", "SSM-T100", "Smart Sensor Modules", 24.80, 25.20),
    ("P002", "SSM-P220", "Smart Sensor Modules", 18.40, 18.70),
    ("P003", "ICU-X500", "Industrial Control Units", 120.00, 121.00),
    ("P004", "ICU-M350", "Industrial Control Units", 95.50, 96.50),
    ("P005", "HPA-L80", "High-Precision Actuators", 80.00, 80.10),
    ("P006", "HPA-R45", "High-Precision Actuators", 45.20, 45.35),
    ("P007", "PSA-600W", "Power Supply Assemblies", 210.00, 212.00),
    ("P008", "PSA-1200W", "Power Supply Assemblies", 305.00, 308.00),
]

PROCESSES = [
    # process_id, process_name, production_line, target_cycle_sec
    ("PR01", "SMT Pick & Place", "Line A - Electronics", 42.0),
    ("PR02", "Reflow Soldering", "Line A - Electronics", 300.0),
    ("PR03", "Automated Optical Inspection (AOI)", "Line A - Electronics", 35.0),
    ("PR04", "CNC Precision Machining", "Line B - Mechatronics", 180.0),
    ("PR05", "Actuator Sub-Assembly", "Line B - Mechatronics", 95.0),
    ("PR06", "Conformal Coating", "Line C - Final Assembly", 120.0),
    ("PR07", "End-of-Line Functional Test", "Line C - Final Assembly", 150.0),
    ("PR08", "Automated Calibration & Burn-In", "Line C - Final Assembly", 210.0),
]

SOPS = [
    # sop_id, title, version, status, owner_role, last_reviewed, sla_days, actual_days
    ("SOP-001", "WI: Smart Sensor Module Calibration Procedure", "v4.2", "Approved",
     "Quality Engineer", "2026-08-14", 30, 22),
    ("SOP-002", "WI: SMT Placement Accuracy Verification", "v3.0", "Approved",
     "Process Engineer", "2026-07-02", 30, 27),
    ("SOP-003", "WI: Reflow Thermal Profile Validation", "v2.5", "Approved",
     "Process Engineer", "2026-09-10", 21, 18),
    ("SOP-004", "WI: Industrial Control Unit End-of-Line Test", "v5.1", "Approved",
     "Test Engineer", "2026-06-21", 30, 34),
    ("SOP-005", "WI: High-Precision Actuator Torque & Stroke Check", "v2.0", "Approved",
     "Quality Engineer", "2026-05-30", 21, 16),
    ("SOP-006", "WI: Power Supply Hi-Pot & Insulation Test", "v3.3", "Approved",
     "Test Engineer", "2026-09-28", 14, 12),
    ("SOP-007", "WI: Automated Test Station Calibration (ATE)", "v6.0", "Approved",
     "Metrology Lead", "2026-10-05", 30, 25),
    ("SOP-008", "WI: Conformal Coating Thickness Inspection", "v1.4", "In Review",
     "Quality Engineer", "2026-10-12", 30, 41),
    ("SOP-009", "WI: Legacy Sensor Calibration Procedure (superseded)", "v3.9", "Expired",
     "Quality Engineer", "2024-11-03", 30, 96),
    ("SOP-010", "WI: ICU Functional Test - Gen 1 Fixture", "v4.0", "Expired",
     "Test Engineer", "2025-01-17", 30, 74),
    ("SOP-011", "WI: CNC Machining First-Article Inspection", "v2.2", "In Review",
     "Manufacturing Engineer", "2026-10-20", 21, 26),
    ("SOP-012", "WI: Manual Gauge R&R Calibration Check", "v1.1", "Expired",
     "Metrology Lead", "2025-03-09", 21, 58),
]

ACTIVE_SOPS = [s[0] for s in SOPS if s[3] != "Expired"]
EXPIRED_SOPS = [s[0] for s in SOPS if s[3] == "Expired"]
OPERATORS = [f"OP-{i}" for i in range(1001, 1041)]

# 12 days of historical runs (business dates Oct 18-29); Oct 22 is a breach.
HISTORY_DATES = [date(2026, 10, d) for d in range(18, 30)]
HISTORY_SCORES = [93.8, 95.2, 91.6, 97.4, 74.0, 89.2, 94.6, 96.8, 92.4, 97.8, 88.4, 95.6]
HISTORY_TOTAL = 500

BATCH_COLUMNS = [
    "inspection_id", "batch_id", "inspection_date", "product_id", "process_id",
    "sop_id", "operator_id", "measurement_mm", "cycle_time_sec", "defect_flag",
    "first_pass_yield_flag",
]


def _tz_for(d: date) -> str:
    """Central-European offset (DST ends Sunday 25 Oct 2026)."""
    return "+02:00" if d < date(2026, 10, 25) else "+01:00"


# --------------------------------------------------------------------------- #
# Dimension seeding
# --------------------------------------------------------------------------- #
def build_dim_date(start: date = date(2026, 1, 1), end: date = date(2026, 12, 31)) -> pd.DataFrame:
    days = pd.date_range(start, end, freq="D")
    return pd.DataFrame({
        "date_key": days.strftime("%Y%m%d").astype(int),
        "full_date": days.strftime("%Y-%m-%d"),
        "year": days.year,
        "quarter": days.quarter,
        "month": days.month,
        "week_of_year": days.isocalendar().week.astype(int).values,
        "day_name": days.day_name(),
    })


def seed_dimensions(conn) -> None:
    if database.table_row_count(conn, "dim_product_catalog") == 0:
        conn.executemany("INSERT INTO dim_product_catalog VALUES (?,?,?,?,?)", PRODUCTS)
    if database.table_row_count(conn, "dim_production_process") == 0:
        conn.executemany("INSERT INTO dim_production_process VALUES (?,?,?,?)", PROCESSES)
    if database.table_row_count(conn, "dim_sop_documentation") == 0:
        conn.executemany("INSERT INTO dim_sop_documentation VALUES (?,?,?,?,?,?,?,?)", SOPS)
    if database.table_row_count(conn, "dim_date") == 0:
        dim_date = build_dim_date()
        conn.executemany(
            "INSERT INTO dim_date VALUES (?,?,?,?,?,?,?)",
            [tuple(int(v) if isinstance(v, (np.integer,)) else v for v in r)
             for r in dim_date.itertuples(index=False, name=None)],
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Clean record synthesis
# --------------------------------------------------------------------------- #
def generate_clean_records(n: int, batch_date: date, rng: np.random.Generator,
                           start_seq: int = 1) -> pd.DataFrame:
    """n fully valid inspection rows for a given business date."""
    prod = pd.DataFrame(PRODUCTS, columns=["product_id", "sku", "cat", "lo", "hi"])
    proc = pd.DataFrame(PROCESSES, columns=["process_id", "name", "line", "target"])

    p_idx = rng.integers(0, len(prod), n)
    r_idx = rng.integers(0, len(proc), n)
    lo, hi = prod.loc[p_idx, "lo"].values, prod.loc[p_idx, "hi"].values
    nominal, tol = (lo + hi) / 2, (hi - lo)
    measurement = rng.normal(nominal, tol / 5.0)         # ~4-5 % out of spec
    target = proc.loc[r_idx, "target"].values
    cycle = np.clip(rng.normal(target, target * 0.08), target * 0.6, None)

    defect = ((measurement < lo) | (measurement > hi)).astype(int)
    fpy = ((defect == 0) & (rng.random(n) > 0.025)).astype(int)
    stamp = batch_date.strftime("%Y%m%d")

    return pd.DataFrame({
        "inspection_id": [f"INS-{stamp}-{i:05d}" for i in range(start_seq, start_seq + n)],
        "batch_id": f"BATCH-{batch_date.isoformat()}",
        "inspection_date": batch_date.isoformat(),
        "product_id": prod.loc[p_idx, "product_id"].values,
        "process_id": proc.loc[r_idx, "process_id"].values,
        "sop_id": rng.choice(ACTIVE_SOPS, n),
        "operator_id": rng.choice(OPERATORS, n),
        "measurement_mm": np.round(measurement, 3),
        "cycle_time_sec": np.round(cycle, 1),
        "defect_flag": defect,
        "first_pass_yield_flag": fpy,
    })


# --------------------------------------------------------------------------- #
# Landing-zone batches with controlled corruption
# --------------------------------------------------------------------------- #
def _inject(df: pd.DataFrame, plan: list[tuple[str, int]], rng: np.random.Generator,
            prod_collision_ids: list[str]) -> pd.DataFrame:
    """
    Apply each corruption type to a disjoint set of rows (positions >= 50) so
    every corrupted row violates exactly one rule -> deterministic pass rate.
    Rows 0-49 are kept pristine and serve as originals for duplicate IDs.
    """
    df = df.astype(object).copy()
    total_bad = sum(c for _, c in plan)
    positions = rng.permutation(np.arange(50, len(df)))[:total_bad]
    cursor = 0
    null_fields = config.CRITICAL_FIELDS
    orphan_process = ["PR09", "PR13", "PR99", "PR-404"]
    for kind, count in plan:
        rows = positions[cursor:cursor + count]
        cursor += count
        for k, pos in enumerate(rows):
            if kind == "dup_intra":
                df.at[pos, "inspection_id"] = df.at[int(rng.integers(0, 50)), "inspection_id"]
            elif kind == "dup_prod":
                df.at[pos, "inspection_id"] = prod_collision_ids[k % len(prod_collision_ids)]
            elif kind == "null":
                df.at[pos, null_fields[k % len(null_fields)]] = None
            elif kind == "null_soft":  # healthy batch: avoid nulling the PK
                df.at[pos, null_fields[1 + k % (len(null_fields) - 1)]] = None
            elif kind == "fk_process":
                df.at[pos, "process_id"] = orphan_process[k % len(orphan_process)]
            elif kind == "fk_mixed":
                col, val = [("product_id", "P099"), ("process_id", "PR99"),
                            ("sop_id", "SOP-999")][k % 3]
                df.at[pos, col] = val
            elif kind == "schema":
                col, val = [("measurement_mm", "ERR"), ("cycle_time_sec", "n/a"),
                            ("inspection_date", "2026-13-45"), ("measurement_mm", "#VALUE!"),
                            ("inspection_date", "31/31/2026")][k % 5]
                df.at[pos, col] = val
            elif kind == "range_measure":
                df.at[pos, "measurement_mm"] = (
                    round(-float(rng.uniform(0.5, 50)), 3) if k % 2 == 0
                    else round(float(rng.uniform(1200, 99999)), 3)
                )
            elif kind == "range_cycle":
                df.at[pos, "cycle_time_sec"] = [0, -12.5, -1.0][k % 3]
            elif kind == "sop_expired":
                df.at[pos, "sop_id"] = EXPIRED_SOPS[k % len(EXPIRED_SOPS)]
    return df


HEALTHY_PLAN = [  # 18 bad rows -> 96.4 %
    ("dup_intra", 2), ("null_soft", 4), ("fk_mixed", 3),
    ("schema", 3), ("range_measure", 2), ("range_cycle", 1), ("sop_expired", 3),
]
CORRUPTED_PLAN = [  # 144 bad rows -> 71.2 %
    ("dup_intra", 15), ("dup_prod", 15), ("null", 28), ("fk_process", 26),
    ("schema", 10), ("range_measure", 24), ("range_cycle", 6), ("sop_expired", 20),
]


def generate_landing_batches(force: bool = False) -> dict[str, Path]:
    config.LANDING_ZONE_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    specs = [
        (config.HEALTHY_BATCH_NAME, date(2026, 10, 30), HEALTHY_PLAN, 101),
        (config.CORRUPTED_BATCH_NAME, date(2026, 10, 31), CORRUPTED_PLAN, 202),
    ]
    collision_ids = [f"INS-20261029-{i:05d}" for i in range(1, 16)]
    for name, d, plan, salt in specs:
        path = Path(config.LANDING_ZONE_DIR) / name
        out[name] = path
        if path.exists() and not force:
            continue
        rng = np.random.default_rng(RNG_SEED + salt)
        clean = generate_clean_records(500, d, rng)
        dirty = _inject(clean, plan, rng, collision_ids)
        dirty[BATCH_COLUMNS].to_csv(path, index=False)
    return out


# --------------------------------------------------------------------------- #
# History + LKG snapshot
# --------------------------------------------------------------------------- #
def _split_errors(invalid: int, rng: np.random.Generator, breach: bool) -> dict[str, int]:
    rules = list(config.RULE_CATALOG)
    weights = (np.array([0.22, 0.20, 0.20, 0.08, 0.18, 0.12]) if breach
               else np.array([0.12, 0.25, 0.15, 0.18, 0.18, 0.12]))
    counts = rng.multinomial(invalid, weights)
    return {r: int(c) for r, c in zip(rules, counts)}


def seed_history_and_lkg(conn) -> None:
    rng = np.random.default_rng(RNG_SEED)
    if database.table_row_count(conn, "audit_sla_runs") == 0:
        frames = []
        for d, score in zip(HISTORY_DATES, HISTORY_SCORES):
            valid = int(round(HISTORY_TOTAL * score / 100))
            invalid = HISTORY_TOTAL - valid
            breach = score < config.SLA_THRESHOLD_PERCENT
            run_day = d + timedelta(days=1)
            ts = (f"{run_day.isoformat()}T06:{int(rng.integers(5, 25)):02d}:"
                  f"{int(rng.integers(0, 60)):02d}{_tz_for(run_day)}")
            database.insert_audit_run(conn, {
                "run_timestamp": ts,
                "batch_filename": f"batch_{d.strftime('%Y_%m_%d')}_daily.csv",
                "total_records": HISTORY_TOTAL,
                "valid_records": valid,
                "invalid_records": invalid,
                "health_score_pct": round(valid / HISTORY_TOTAL * 100, 1),
                "sla_status": "BREACH" if breach else "PASS",
                "lkg_preserved_flag": int(breach),
                "email_triggered_flag": int(breach),
                "quarantine_file_path": (
                    f"data/quarantine/archive/quarantine_batch_{d.strftime('%Y_%m_%d')}_daily.xlsx"
                    if breach else None),
                "error_breakdown_json": _split_errors(invalid, rng, breach),
            })
            if not breach:  # only certified days are in production
                facts = generate_clean_records(valid, d, rng)
                facts["validated_at"] = ts
                frames.append(facts)
        if database.table_row_count(conn, "fact_operations_quality") == 0 and frames:
            database.upsert_facts(conn, pd.concat(frames, ignore_index=True))

    # Certified Last-Known-Good snapshot for Power BI
    meta_path = Path(config.LKG_METADATA_PATH)
    if not meta_path.exists():
        database.export_star_schema(conn)
        last_day = HISTORY_DATES[-1]
        last_run = database.latest_audit_run(conn)
        meta = {
            "last_certified_batch": f"batch_{last_day.strftime('%Y_%m_%d')}_daily.csv",
            "last_certified_batch_id": f"BATCH-{last_day.isoformat()}",
            "last_certified_timestamp": last_run["run_timestamp"] if last_run else None,
            "record_count": database.table_row_count(conn, "fact_operations_quality"),
            "health_score": HISTORY_SCORES[-1],
            "sla_threshold_pct": config.SLA_THRESHOLD_PERCENT,
            "stale_warning": False,
            "last_failed_batch": None,
        }
        meta_path.write_text(json.dumps(meta, indent=2))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def reset_environment() -> None:
    """Remove all generated state (DB, BI snapshot, landing batches, quarantine)."""
    Path(config.DB_PATH).unlink(missing_ok=True)
    for folder in (config.PRODUCTION_BI_DIR, config.QUARANTINE_DIR):
        if Path(folder).exists():
            shutil.rmtree(folder)
    for name in (config.HEALTHY_BATCH_NAME, config.CORRUPTED_BATCH_NAME):
        (Path(config.LANDING_ZONE_DIR) / name).unlink(missing_ok=True)


def seed_all(force: bool = False) -> dict:
    if force:
        reset_environment()
    config.ensure_directories()
    (Path(config.QUARANTINE_DIR) / ".gitkeep").touch(exist_ok=True)
    with database.connect() as conn:
        database.init_schema(conn)
        seed_dimensions(conn)
        seed_history_and_lkg(conn)
        summary = {
            t: database.table_row_count(conn, t)
            for t in ("dim_product_catalog", "dim_production_process", "dim_sop_documentation",
                      "dim_date", "fact_operations_quality", "audit_sla_runs")
        }
    batches = generate_landing_batches(force=force)
    summary["landing_batches"] = [p.name for p in batches.values()]
    return summary


def is_seeded() -> bool:
    return (Path(config.DB_PATH).exists() and Path(config.LKG_METADATA_PATH).exists()
            and (Path(config.LANDING_ZONE_DIR) / config.CORRUPTED_BATCH_NAME).exists())


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Ops Quality Gatekeeper datasets.")
    parser.add_argument("--force", action="store_true", help="Wipe and regenerate everything.")
    args = parser.parse_args()
    summary = seed_all(force=args.force)
    print(f"[{datetime.now():%H:%M:%S}] Seed complete:")
    for k, v in summary.items():
        print(f"  - {k}: {v}")


if __name__ == "__main__":
    main()
