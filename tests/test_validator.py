"""
Pytest suite for the 80 % SLA gatekeeper, the 6 validation rules and the
Last-Known-Good pipeline policy.

    pytest -q
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow plain `pytest`

from src import config
from src.validator import (ALL_RULES, RULE_01, RULE_02, RULE_03, RULE_04, RULE_05, RULE_06,
                           DataQualityValidator, read_batch_csv)

PRODUCTS = pd.DataFrame({"product_id": ["P001", "P002"], "sku_code": ["A", "B"],
                         "product_category": ["Smart Sensor Modules"] * 2,
                         "spec_min_mm": [24.8, 18.4], "spec_max_mm": [25.2, 18.7]})
PROCESSES = pd.DataFrame({"process_id": ["PR01", "PR02"], "process_name": ["SMT", "Reflow"],
                          "production_line": ["A", "A"], "target_cycle_sec": [42.0, 300.0]})
SOPS = pd.DataFrame({"sop_id": ["SOP-001", "SOP-008", "SOP-009"],
                     "review_status": ["Approved", "In Review", "Expired"]})


def make_row(i: int, **overrides) -> dict:
    row = {"inspection_id": f"INS-20261031-{i:05d}", "batch_id": "BATCH-2026-10-31",
           "inspection_date": "2026-10-31", "product_id": "P001", "process_id": "PR01",
           "sop_id": "SOP-001", "operator_id": "OP-1001", "measurement_mm": "25.01",
           "cycle_time_sec": "41.5", "defect_flag": "0", "first_pass_yield_flag": "1"}
    row.update(overrides)
    return row


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).astype(object)


@pytest.fixture
def validator() -> DataQualityValidator:
    prod_keys = pd.DataFrame({"inspection_id": ["INS-20261029-00001"],
                              "batch_id": ["BATCH-2026-10-29"]})
    return DataQualityValidator(PRODUCTS, PROCESSES, SOPS, prod_keys)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_sla_threshold_and_recipient_configured():
    assert config.SLA_THRESHOLD_PERCENT == 80.0
    assert config.ALERT_RECIPIENT_EMAIL == "na.vinothkumar2026@gmail.com"


def test_clean_batch_is_100_percent(validator):
    res = validator.validate(frame([make_row(i) for i in range(1, 11)]))
    assert res.health_score_pct == 100.0 and res.sla_passed
    assert res.quarantine_df.empty and len(res.clean_df) == 10
    assert all(v == 0 for v in res.rule_failure_counts.values())
    assert set(res.rule_failure_counts) == set(ALL_RULES)


# --------------------------------------------------------------------------- #
# Individual rules
# --------------------------------------------------------------------------- #
def test_rule01_duplicate_within_batch(validator):
    rows = [make_row(1), make_row(2), make_row(1)]
    res = validator.validate(frame(rows))
    assert res.rule_failure_counts[RULE_01] == 1  # first occurrence kept
    assert res.quarantine_df.iloc[0]["failed_rules"] == RULE_01


def test_rule01_duplicate_against_production(validator):
    res = validator.validate(frame([make_row(1, inspection_id="INS-20261029-00001")]))
    assert res.rule_failure_counts[RULE_01] == 1
    assert "already exists in production" in res.quarantine_df.iloc[0]["failure_reason"]


def test_rule01_same_batch_rerun_is_idempotent():
    prod = pd.DataFrame({"inspection_id": ["INS-20261031-00001"], "batch_id": ["BATCH-2026-10-31"]})
    v = DataQualityValidator(PRODUCTS, PROCESSES, SOPS, prod)
    assert v.validate(frame([make_row(1)])).rule_failure_counts[RULE_01] == 0


@pytest.mark.parametrize("field", config.CRITICAL_FIELDS)
def test_rule02_critical_nulls(validator, field):
    res = validator.validate(frame([make_row(1, **{field: None}), make_row(2, **{field: "  "})]))
    assert res.rule_failure_counts[RULE_02] == 2
    assert field in res.quarantine_df.iloc[0]["failure_reason"]


@pytest.mark.parametrize("col,val", [("product_id", "P099"), ("process_id", "PR99"),
                                     ("sop_id", "SOP-999")])
def test_rule03_fk_orphans(validator, col, val):
    res = validator.validate(frame([make_row(1, **{col: val})]))
    assert res.rule_failure_counts[RULE_03] == 1
    assert val in res.quarantine_df.iloc[0]["failure_reason"]


@pytest.mark.parametrize("col,val", [("measurement_mm", "ERR"), ("cycle_time_sec", "n/a"),
                                     ("inspection_date", "2026-13-45"),
                                     ("inspection_date", "31/31/2026")])
def test_rule04_schema_types(validator, col, val):
    res = validator.validate(frame([make_row(1, **{col: val})]))
    assert res.rule_failure_counts[RULE_04] == 1
    assert res.rule_failure_counts[RULE_02] == 0


@pytest.mark.parametrize("col,val", [("measurement_mm", "-0.5"), ("measurement_mm", "1000.01"),
                                     ("cycle_time_sec", "0"), ("cycle_time_sec", "-3")])
def test_rule05_range_anomalies(validator, col, val):
    res = validator.validate(frame([make_row(1, **{col: val})]))
    assert res.rule_failure_counts[RULE_05] == 1


def test_rule05_boundaries_are_valid(validator):
    res = validator.validate(frame([make_row(1, measurement_mm="0"), make_row(2, measurement_mm="1000")]))
    assert res.rule_failure_counts[RULE_05] == 0


def test_rule06_expired_sop_blocked_but_in_review_allowed(validator):
    res = validator.validate(frame([make_row(1, sop_id="SOP-009"), make_row(2, sop_id="SOP-008")]))
    assert res.rule_failure_counts[RULE_06] == 1
    assert res.quarantine_df.iloc[0]["sop_id"] == "SOP-009"


def test_multiple_rules_on_one_row_counted_once_in_score(validator):
    row = make_row(1, process_id="PR99", sop_id="SOP-009", measurement_mm="-5")
    res = validator.validate(frame([row, make_row(2)]))
    assert res.health_score_pct == 50.0
    failed = res.quarantine_df.iloc[0]["failed_rules"]
    assert RULE_03 in failed and RULE_05 in failed and RULE_06 in failed


# --------------------------------------------------------------------------- #
# SLA gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad,expected_pass", [(0, True), (20, True), (21, False), (40, False)])
def test_sla_gate_boundary(validator, bad, expected_pass):
    rows = [make_row(i) for i in range(1, 101)]
    for i in range(bad):
        rows[i]["measurement_mm"] = "-1"
    res = validator.validate(frame(rows))
    assert res.health_score_pct == 100 - bad
    assert res.sla_passed is expected_pass  # exactly 80.0 % passes


def test_result_supports_tuple_unpacking(validator):
    clean, quarantine, score, passed, counts = validator.validate(frame([make_row(1)]))
    assert score == 100.0 and passed and isinstance(counts, dict)


def test_read_batch_csv_preserves_text_tokens(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text("inspection_id,cycle_time_sec\nA,n/a\nB,\n")
    df = read_batch_csv(p)
    assert df.loc[0, "cycle_time_sec"] == "n/a" and pd.isna(df.loc[1, "cycle_time_sec"])


# --------------------------------------------------------------------------- #
# End-to-end: generated batches + Last-Known-Good policy (isolated sandbox)
# --------------------------------------------------------------------------- #
@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for name, value in {
        "DATA_DIR": data, "LANDING_ZONE_DIR": data / "landing_zone",
        "PRODUCTION_BI_DIR": data / "production_bi", "QUARANTINE_DIR": data / "quarantine",
        "DB_DIR": tmp_path / "db", "DB_PATH": tmp_path / "db" / "test.db",
        "LKG_METADATA_PATH": data / "production_bi" / "lkg_metadata.json",
        "LATEST_EMAIL_HTML_PATH": data / "quarantine" / "latest_email_alert.html",
        "LATEST_EMAIL_STATUS_PATH": data / "quarantine" / "latest_email_status.json",
        "PROJECT_ROOT": tmp_path, "SMTP_SENDER_EMAIL": "", "SMTP_APP_PASSWORD": "",
    }.items():
        monkeypatch.setattr(config, name, value)
    from src import data_generator
    data_generator.seed_all()
    return tmp_path


def _hash_dir(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(folder.glob("*.csv"))}


def test_generated_batches_hit_target_scores(sandbox):
    from src import database
    with database.connect() as conn:
        v = DataQualityValidator.from_database(conn)
    healthy = v.validate(read_batch_csv(config.LANDING_ZONE_DIR / config.HEALTHY_BATCH_NAME))
    corrupted = v.validate(read_batch_csv(config.LANDING_ZONE_DIR / config.CORRUPTED_BATCH_NAME))
    assert healthy.total_records == corrupted.total_records == 500
    assert healthy.health_score_pct == pytest.approx(96.4) and healthy.sla_passed
    assert corrupted.health_score_pct == pytest.approx(71.2) and not corrupted.sla_passed
    assert all(corrupted.rule_failure_counts[r] > 0 for r in ALL_RULES)


def test_pipeline_pass_promotes_and_breach_preserves_lkg(sandbox):
    from src import pipeline
    passed = pipeline.run_pipeline(config.LANDING_ZONE_DIR / config.HEALTHY_BATCH_NAME)
    assert passed["sla_status"] == "PASS"
    meta = json.loads(config.LKG_METADATA_PATH.read_text())
    assert meta["last_certified_batch"] == config.HEALTHY_BATCH_NAME and not meta["stale_warning"]
    for f in config.BI_EXPORT_FILES.values():
        assert (config.PRODUCTION_BI_DIR / f).exists()
    snapshot = _hash_dir(config.PRODUCTION_BI_DIR)

    breach = pipeline.run_pipeline(config.LANDING_ZONE_DIR / config.CORRUPTED_BATCH_NAME)
    assert breach["sla_status"] == "BREACH" and breach["health_score_pct"] == pytest.approx(71.2)
    assert _hash_dir(config.PRODUCTION_BI_DIR) == snapshot, "LKG CSVs must not change on breach"

    meta = json.loads(config.LKG_METADATA_PATH.read_text())
    assert meta["stale_warning"] is True
    assert meta["last_certified_batch"] == config.HEALTHY_BATCH_NAME
    assert meta["last_failed_batch"]["batch_filename"] == config.CORRUPTED_BATCH_NAME

    xlsx = breach["quarantine_path"]
    assert xlsx.exists() and xlsx.suffix == ".xlsx"
    sheets = pd.read_excel(xlsx, sheet_name=None)
    assert {"Summary_Metrics", "Quarantined_Records"} <= set(sheets)
    assert len(sheets["Quarantined_Records"]) == 144

    html = config.LATEST_EMAIL_HTML_PATH.read_text()
    assert "na.vinothkumar2026@gmail.com" in html and "71.2%" in html
    assert "Last-Known-Good" in html
    assert breach["email"]["sent_live"] is False
    assert breach["email"]["status_message"] == config.LOCAL_FALLBACK_MESSAGE
    assert breach["email"]["subject"] == (
        "[URGENT - Ops Quality Gatekeeper] Daily Data SLA Breach (71.2% < 80.0% Target) "
        "— Power BI Locked to LKG")


def test_auto_fix_remediates_and_promotes(sandbox):
    from src import pipeline
    fixed = pipeline.auto_fix_batch(config.LANDING_ZONE_DIR / config.CORRUPTED_BATCH_NAME)
    run = pipeline.run_pipeline(fixed)
    assert run["sla_passed"] and run["health_score_pct"] == 100.0
    meta = json.loads(config.LKG_METADATA_PATH.read_text())
    assert meta["last_certified_batch"] == fixed.name and meta["stale_warning"] is False


def test_audit_log_records_every_run(sandbox):
    from src import database, pipeline
    with database.connect() as conn:
        before = database.table_row_count(conn, "audit_sla_runs")
    pipeline.run_pipeline(config.LANDING_ZONE_DIR / config.CORRUPTED_BATCH_NAME)
    with database.connect() as conn:
        last = database.latest_audit_run(conn)
        assert database.table_row_count(conn, "audit_sla_runs") == before + 1
    assert last["sla_status"] == "BREACH" and last["lkg_preserved_flag"] == 1
    assert last["email_triggered_flag"] == 1
    assert json.loads(last["error_breakdown_json"])[RULE_06] == 20
