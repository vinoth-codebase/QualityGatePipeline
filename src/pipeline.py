"""
Last-Known-Good (LKG) orchestrator.

    landing_zone/*.csv ──► DataQualityValidator ──┬─ PASS  (>= 80 %) ──► upsert SQLite fact
                                                  │                    ──► export Star Schema CSVs
                                                  │                    ──► lkg_metadata.json (certified)
                                                  └─ BREACH (< 80 %) ──► production_bi/ UNTOUCHED (LKG)
                                                                       ──► lkg_metadata.json (stale_warning)
                                                                       ──► quarantine_<batch>_<ts>.xlsx
                                                                       ──► HTML email alert
    every run ──► audit_sla_runs

CLI:
    python -m src.pipeline                       # healthy then corrupted batch
    python -m src.pipeline data/landing_zone/x.csv
    python -m src.pipeline --auto-fix data/landing_zone/batch_2026_10_31_corrupted.csv
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from src import config, database, email_alerter
from src.validator import DataQualityValidator, ValidationResult, read_batch_csv, _blank


# --------------------------------------------------------------------------- #
# LKG metadata helpers
# --------------------------------------------------------------------------- #
def read_lkg_metadata() -> dict:
    path = Path(config.LKG_METADATA_PATH)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_lkg_metadata(meta: dict) -> None:
    Path(config.PRODUCTION_BI_DIR).mkdir(parents=True, exist_ok=True)
    Path(config.LKG_METADATA_PATH).write_text(json.dumps(meta, indent=2, default=str))


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(config.PROJECT_ROOT).resolve()))
    except ValueError:
        return str(path)


def resolve_project_path(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    p = Path(path_str)
    return p if p.is_absolute() else Path(config.PROJECT_ROOT) / p


# --------------------------------------------------------------------------- #
# Excel quarantine export (openpyxl)
# --------------------------------------------------------------------------- #
HEADER_FILL = PatternFill("solid", fgColor="0F172A")
HEADER_FONT = Font(bold=True, color="FFFFFF")
BREACH_FILL = PatternFill("solid", fgColor="FEE2E2")
PASS_FILL = PatternFill("solid", fgColor="DCFCE7")
THIN = Side(style="thin", color="CBD5E1")


def _style_sheet(ws, widths: dict[int, int] | None = None) -> None:
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for idx, column in enumerate(ws.columns, start=1):
        if widths and idx in widths:
            width = widths[idx]
        else:
            longest = max((len(str(c.value)) for c in column if c.value is not None), default=8)
            width = min(max(longest + 2, 10), 70)
        ws.column_dimensions[get_column_letter(idx)].width = width


def export_quarantine_workbook(result: ValidationResult, batch_name: str,
                               run_timestamp: str, sla_status: str) -> Path:
    Path(config.QUARANTINE_DIR).mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(run_timestamp).strftime("%Y%m%d_%H%M%S")
    path = Path(config.QUARANTINE_DIR) / f"quarantine_{Path(batch_name).stem}_{stamp}.xlsx"

    summary = pd.DataFrame([
        ("Batch File", batch_name),
        ("Validation Timestamp", run_timestamp),
        ("SLA Threshold (%)", config.SLA_THRESHOLD_PERCENT),
        ("Health Score (%)", result.health_score_pct),
        ("SLA Status", sla_status),
        ("Total Records", result.total_records),
        ("Valid Records", result.valid_records),
        ("Quarantined Records", result.invalid_records),
        ("Power BI Action", "LKG preserved — production_bi untouched" if sla_status == "BREACH"
         else "Clean rows promoted to production_bi"),
        ("Alert Recipient", config.ALERT_RECIPIENT_EMAIL),
        ("", ""),
        ("Rule", "Failed Rows"),
        *[(f"{r} — {config.RULE_CATALOG[r]}", c) for r, c in result.rule_failure_counts.items()],
    ], columns=["Metric", "Value"])

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary_Metrics", index=False)
        result.quarantine_df.to_excel(xw, sheet_name="Quarantined_Records", index=False)
        ws = xw.sheets["Summary_Metrics"]
        _style_sheet(ws, {1: 70, 2: 48})
        ws["B6"].fill = BREACH_FILL if sla_status == "BREACH" else PASS_FILL
        ws["B6"].font = Font(bold=True, color="B91C1C" if sla_status == "BREACH" else "15803D")
        rule_header_row = 13
        for cell in ws[rule_header_row]:
            cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        wq = xw.sheets["Quarantined_Records"]
        _style_sheet(wq)
        for row in wq.iter_rows(min_row=2):
            for cell in row[-2:]:
                cell.fill = BREACH_FILL
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    return path


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def run_pipeline(file_path: str | Path, send_alert: bool = True) -> dict:
    """Validate one landing-zone batch and apply the Last-Known-Good policy."""
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(file_path)
    config.ensure_directories()
    batch_name = file_path.name
    run_ts = datetime.now().astimezone().isoformat(timespec="seconds")
    raw = read_batch_csv(file_path)

    with database.connect() as conn:
        database.init_schema(conn)
        validator = DataQualityValidator.from_database(conn)
        result = validator.validate(raw)
        sla_status = "PASS" if result.sla_passed else "BREACH"
        lkg = read_lkg_metadata()

        quarantine_path = None
        if not result.quarantine_df.empty:
            quarantine_path = export_quarantine_workbook(result, batch_name, run_ts, sla_status)

        email_info = None
        if result.sla_passed:
            clean = result.clean_df.copy()
            clean["validated_at"] = run_ts
            loaded = database.upsert_facts(conn, clean)
            database.export_star_schema(conn)
            lkg = {
                "last_certified_batch": batch_name,
                "last_certified_batch_id": ", ".join(sorted(clean["batch_id"].astype(str).unique())),
                "last_certified_timestamp": run_ts,
                "record_count": database.table_row_count(conn, "fact_operations_quality"),
                "certified_batch_rows": loaded,
                "health_score": result.health_score_pct,
                "sla_threshold_pct": config.SLA_THRESHOLD_PERCENT,
                "stale_warning": False,
                "last_failed_batch": None,
            }
            write_lkg_metadata(lkg)
            action = f"SLA PASS — {loaded} clean rows promoted; Power BI Star Schema refreshed."
        else:
            # LKG policy: production_bi CSVs are deliberately NOT touched.
            lkg.update({
                "stale_warning": True,
                "last_failed_batch": {
                    "batch_filename": batch_name,
                    "run_timestamp": run_ts,
                    "health_score": result.health_score_pct,
                    "quarantined_records": result.invalid_records,
                    "quarantine_file": _rel(quarantine_path) if quarantine_path else None,
                },
            })
            write_lkg_metadata(lkg)
            if send_alert:
                email_info = email_alerter.send_sla_breach_alert(
                    batch_name=batch_name, run_timestamp=run_ts,
                    health_score_pct=result.health_score_pct,
                    total_records=result.total_records,
                    quarantined_records=result.invalid_records,
                    rule_failure_counts=result.rule_failure_counts,
                    attachment_path=quarantine_path, lkg_metadata=lkg,
                )
            action = ("SLA BREACH — production_bi preserved at Last-Known-Good "
                      f"({lkg.get('last_certified_batch')}); alert raised.")

        run_id = database.insert_audit_run(conn, {
            "run_timestamp": run_ts,
            "batch_filename": batch_name,
            "total_records": result.total_records,
            "valid_records": result.valid_records,
            "invalid_records": result.invalid_records,
            "health_score_pct": result.health_score_pct,
            "sla_status": sla_status,
            "lkg_preserved_flag": int(not result.sla_passed),
            "email_triggered_flag": int(email_info is not None),
            "quarantine_file_path": _rel(quarantine_path) if quarantine_path else None,
            "error_breakdown_json": result.rule_failure_counts,
        })

    return {
        "run_id": run_id,
        "run_timestamp": run_ts,
        "batch_filename": batch_name,
        "result": result,
        "health_score_pct": result.health_score_pct,
        "sla_status": sla_status,
        "sla_passed": result.sla_passed,
        "quarantine_path": quarantine_path,
        "email": email_info,
        "lkg_metadata": lkg,
        "action": action,
    }


# --------------------------------------------------------------------------- #
# Simulated data-engineering remediation
# --------------------------------------------------------------------------- #
def auto_fix_batch(file_path: str | Path) -> Path:
    """
    Simulate remediation of a quarantined batch and write
    ``<stem>_remediated.csv`` to the landing zone:
      * re-key duplicate / colliding / null inspection_ids
      * impute null batch/date/FK fields with the batch mode
      * remap orphan FKs to valid masters, Expired SOPs to their superseding SOP
      * impute non-numeric / out-of-range measures with product nominal / process target
    """
    file_path = Path(file_path)
    df = read_batch_csv(file_path)
    with database.connect() as conn:
        dims = database.load_dimensions(conn)
        prod_keys = database.load_production_keys(conn)
    products = dims["products"].set_index("product_id")
    processes = dims["processes"].set_index("process_id")
    sops = dims["sops"]
    active_sops = set(sops.loc[sops.review_status.ne("Expired"), "sop_id"])

    for col in ("inspection_id", "batch_id", "product_id", "process_id", "sop_id", "inspection_date"):
        df[col] = df[col].where(~_blank(df[col]), np.nan).astype(object)

    batch_id = df["batch_id"].mode().iat[0]
    df["batch_id"] = batch_id
    dates = pd.to_datetime(df["inspection_date"], format="%Y-%m-%d", errors="coerce")
    batch_date = dates.mode().iat[0]
    df["inspection_date"] = dates.fillna(batch_date).dt.strftime("%Y-%m-%d")

    foreign_ids = set(prod_keys.loc[prod_keys.batch_id.ne(batch_id), "inspection_id"])
    bad_pk = (df["inspection_id"].isna() | df["inspection_id"].duplicated(keep="first")
              | df["inspection_id"].isin(foreign_ids))
    df.loc[bad_pk, "inspection_id"] = [
        f"INS-{batch_date:%Y%m%d}-R{i:04d}" for i in range(1, int(bad_pk.sum()) + 1)]

    for col, valid in (("product_id", set(products.index)), ("process_id", set(processes.index))):
        ok = df[col].isin(valid)
        df.loc[~ok, col] = df.loc[ok, col].mode().iat[0]
    df["sop_id"] = df["sop_id"].replace(config.SOP_SUPERSESSION_MAP)
    ok_sop = df["sop_id"].isin(active_sops)
    df.loc[~ok_sop, "sop_id"] = df.loc[ok_sop, "sop_id"].mode().iat[0]

    meas = pd.to_numeric(df["measurement_mm"], errors="coerce")
    nominal = df["product_id"].map((products.spec_min_mm + products.spec_max_mm) / 2)
    bad_m = meas.isna() | (meas < config.MEASUREMENT_MIN_MM) | (meas > config.MEASUREMENT_MAX_MM)
    df["measurement_mm"] = meas.where(~bad_m, nominal).round(3)

    cyc = pd.to_numeric(df["cycle_time_sec"], errors="coerce")
    target = df["process_id"].map(processes.target_cycle_sec)
    df["cycle_time_sec"] = cyc.where(cyc.notna() & (cyc > 0), target).round(1)

    lo, hi = df["product_id"].map(products.spec_min_mm), df["product_id"].map(products.spec_max_mm)
    df["defect_flag"] = ((df["measurement_mm"] < lo) | (df["measurement_mm"] > hi)).astype(int)
    df["first_pass_yield_flag"] = pd.to_numeric(df.get("first_pass_yield_flag"), errors="coerce") \
        .fillna(1).astype(int).where(df["defect_flag"].eq(0), 0)

    out = Path(config.LANDING_ZONE_DIR) / f"{file_path.stem.replace('_remediated', '')}_remediated.csv"
    df.to_csv(out, index=False)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_run(r: dict) -> None:
    res: ValidationResult = r["result"]
    icon = "✅" if r["sla_passed"] else "🚨"
    print(f"\n{icon} {r['batch_filename']}  →  {r['health_score_pct']:.1f}%  [{r['sla_status']}]"
          f"  ({res.valid_records}/{res.total_records} valid, {res.invalid_records} quarantined)")
    for rule, c in res.rule_failure_counts.items():
        print(f"     {rule:<24} {c:>4}")
    print(f"   Action     : {r['action']}")
    if r["quarantine_path"]:
        print(f"   Quarantine : {_rel(r['quarantine_path'])}")
    if r["email"]:
        print(f"   Email To   : {r['email']['recipient']}")
        print(f"   Email HTML : {_rel(Path(r['email']['html_preview_path']))}")


def main() -> None:
    from src import data_generator

    parser = argparse.ArgumentParser(description="Run the Pre-BI Data Validation Gatekeeper.")
    parser.add_argument("files", nargs="*", help="Landing-zone CSV(s). Default: healthy then corrupted.")
    parser.add_argument("--auto-fix", metavar="CSV", help="Remediate a quarantined batch and re-run it.")
    parser.add_argument("--no-alert", action="store_true", help="Skip email alert generation.")
    args = parser.parse_args()

    if not data_generator.is_seeded():
        data_generator.seed_all()

    if args.auto_fix:
        fixed = auto_fix_batch(args.auto_fix)
        print(f"Remediated file written: {_rel(fixed)}")
        _print_run(run_pipeline(fixed, send_alert=not args.no_alert))
        return

    files = args.files or [config.LANDING_ZONE_DIR / config.HEALTHY_BATCH_NAME,
                           config.LANDING_ZONE_DIR / config.CORRUPTED_BATCH_NAME]
    for f in files:
        _print_run(run_pipeline(f, send_alert=not args.no_alert))
    meta = read_lkg_metadata()
    print(f"\nPower BI LKG snapshot → {meta.get('last_certified_batch')} "
          f"@ {meta.get('last_certified_timestamp')} | stale_warning={meta.get('stale_warning')}")


if __name__ == "__main__":
    main()

