"""
Central configuration for the Ops Quality Data Gatekeeper.

All filesystem locations are expressed with ``pathlib.Path`` relative to the
project root so the application runs identically from any macOS directory.

Other modules should reference these values as ``config.<NAME>`` (attribute
access at call time) rather than ``from src.config import NAME`` so the test
suite can safely monkeypatch paths into a temporary sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Project paths
# --------------------------------------------------------------------------- #
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

DATA_DIR: Path = PROJECT_ROOT / "data"
LANDING_ZONE_DIR: Path = DATA_DIR / "landing_zone"
PRODUCTION_BI_DIR: Path = DATA_DIR / "production_bi"
QUARANTINE_DIR: Path = DATA_DIR / "quarantine"

DB_DIR: Path = PROJECT_ROOT / "db"
DB_PATH: Path = DB_DIR / "ops_quality_gatekeeper.db"

LKG_METADATA_PATH: Path = PRODUCTION_BI_DIR / "lkg_metadata.json"
LATEST_EMAIL_HTML_PATH: Path = QUARANTINE_DIR / "latest_email_alert.html"
LATEST_EMAIL_STATUS_PATH: Path = QUARANTINE_DIR / "latest_email_status.json"

HEALTHY_BATCH_NAME: str = "batch_2026_10_30_healthy.csv"
CORRUPTED_BATCH_NAME: str = "batch_2026_10_31_corrupted.csv"

# Power BI Star-Schema export file names (table name -> csv file)
BI_EXPORT_FILES: dict[str, str] = {
    "fact_operations_quality": "Fact_Operations_Quality.csv",
    "dim_production_process": "Dim_Production_Process.csv",
    "dim_product_catalog": "Dim_Product_Catalog.csv",
    "dim_sop_documentation": "Dim_SOP_Documentation.csv",
    "dim_date": "Dim_Date.csv",
}

# --------------------------------------------------------------------------- #
# Data-quality SLA
# --------------------------------------------------------------------------- #
SLA_THRESHOLD_PERCENT: float = 80.0

MEASUREMENT_MIN_MM: float = 0.0
MEASUREMENT_MAX_MM: float = 1000.0

CRITICAL_FIELDS: list[str] = [
    "inspection_id",
    "batch_id",
    "product_id",
    "process_id",
    "sop_id",
    "measurement_mm",
]

RULE_CATALOG: dict[str, str] = {
    "RULE_01_PK_DUPLICATE": "Duplicate inspection_id (within batch or already in production)",
    "RULE_02_CRITICAL_NULL": "Missing value in a critical field",
    "RULE_03_FK_ORPHAN": "Foreign key not found in master dimension tables",
    "RULE_04_SCHEMA_TYPE": "Non-numeric measure or unparseable inspection_date",
    "RULE_05_RANGE_ANOMALY": "cycle_time_sec <= 0 or measurement_mm outside 0-1000 mm",
    "RULE_06_SOP_COMPLIANCE": "Inspection references an Expired SOP / work instruction",
}

# Expired SOP -> currently approved superseding SOP (used by auto-remediation)
SOP_SUPERSESSION_MAP: dict[str, str] = {
    "SOP-009": "SOP-001",
    "SOP-010": "SOP-004",
    "SOP-012": "SOP-007",
}

# --------------------------------------------------------------------------- #
# Alerting / SMTP
# --------------------------------------------------------------------------- #
load_dotenv(PROJECT_ROOT / ".env")

ALERT_RECIPIENT_EMAIL: str = "na.vinothkumar2026@gmail.com"
ALERT_RECIPIENT = ALERT_RECIPIENT_EMAIL  # alias used in docs

SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "465"))
SMTP_SENDER_EMAIL: str = os.getenv("SMTP_SENDER_EMAIL", "").strip()
SMTP_APP_PASSWORD: str = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()

_PLACEHOLDER_VALUES = {"", "your_sender@gmail.com", "your_16_char_app_password", "changeme"}


def smtp_configured() -> bool:
    """True only when real (non-placeholder) Gmail SMTP credentials exist."""
    return (
        SMTP_SENDER_EMAIL not in _PLACEHOLDER_VALUES
        and SMTP_APP_PASSWORD not in _PLACEHOLDER_VALUES
    )


LOCAL_FALLBACK_MESSAGE: str = (
    "HTML Alert Generated & Saved Locally (Configure .env SMTP_APP_PASSWORD for "
    f"live Gmail dispatch to {ALERT_RECIPIENT_EMAIL})"
)

APP_TITLE: str = (
    "Enterprise Operations Quality — Pre-BI Data Validation Gatekeeper, "
    "SLA Monitor & Document Traceability Pipeline"
)


def ensure_directories() -> None:
    """Create every runtime directory if missing (idempotent)."""
    for folder in (LANDING_ZONE_DIR, PRODUCTION_BI_DIR, QUARANTINE_DIR, DB_DIR):
        Path(folder).mkdir(parents=True, exist_ok=True)
