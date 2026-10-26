"""
Vectorized 6-rule enterprise data-quality engine.

Every incoming row is evaluated against all rules simultaneously using pandas
boolean masks (no Python row loops). A row is *valid* only if it passes all six.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src import config

RULE_01 = "RULE_01_PK_DUPLICATE"
RULE_02 = "RULE_02_CRITICAL_NULL"
RULE_03 = "RULE_03_FK_ORPHAN"
RULE_04 = "RULE_04_SCHEMA_TYPE"
RULE_05 = "RULE_05_RANGE_ANOMALY"
RULE_06 = "RULE_06_SOP_COMPLIANCE"
ALL_RULES = [RULE_01, RULE_02, RULE_03, RULE_04, RULE_05, RULE_06]

REQUIRED_COLUMNS = [
    "inspection_id", "batch_id", "inspection_date", "product_id", "process_id",
    "sop_id", "operator_id", "measurement_mm", "cycle_time_sec",
]


@dataclass
class ValidationResult:
    clean_df: pd.DataFrame
    quarantine_df: pd.DataFrame
    health_score_pct: float
    sla_passed: bool
    rule_failure_counts: dict[str, int]
    total_records: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    sla_threshold_pct: float = field(default=config.SLA_THRESHOLD_PERCENT)

    def __iter__(self):
        """Allow tuple-unpacking: clean, quarantine, score, passed, counts = result."""
        return iter((self.clean_df, self.quarantine_df, self.health_score_pct,
                     self.sla_passed, self.rule_failure_counts))


def read_batch_csv(source) -> pd.DataFrame:
    """
    Read a raw landing-zone batch as strings. Only truly empty cells become
    NaN, so tokens such as 'n/a' or '#VALUE!' are preserved and caught by
    RULE_04 instead of being silently coerced to null by pandas.
    """
    return pd.read_csv(source, dtype=str, keep_default_na=False, na_values=[""])


def _concat(parts, index) -> pd.Series:
    """Vectorized concatenation of per-row reason fragments."""
    out = pd.Series("", index=index, dtype=object)
    for part in parts:
        out = out + pd.Series(part, index=index).astype(str)
    return out.str.rstrip("; ")


def _blank(series: pd.Series) -> pd.Series:
    """True where value is NaN/None or an empty / whitespace-only string."""
    return series.isna() | series.astype(str).str.strip().isin(["", "nan", "None", "NaN", "null"])


class DataQualityValidator:
    """
    Parameters
    ----------
    products, processes, sops : master dimension DataFrames
    production_keys : DataFrame[inspection_id, batch_id] already in production
    sla_threshold_pct : SLA gate (default 80.0)
    """

    def __init__(self, products: pd.DataFrame, processes: pd.DataFrame, sops: pd.DataFrame,
                 production_keys: pd.DataFrame | None = None,
                 sla_threshold_pct: float | None = None):
        self.valid_products = set(products["product_id"].astype(str))
        self.valid_processes = set(processes["process_id"].astype(str))
        self.valid_sops = set(sops["sop_id"].astype(str))
        self.expired_sops = set(
            sops.loc[sops["review_status"].eq("Expired"), "sop_id"].astype(str)
        )
        self.production_keys = (
            production_keys if production_keys is not None
            else pd.DataFrame(columns=["inspection_id", "batch_id"])
        )
        self.sla_threshold_pct = (
            config.SLA_THRESHOLD_PERCENT if sla_threshold_pct is None else sla_threshold_pct
        )

    @classmethod
    def from_database(cls, conn) -> "DataQualityValidator":
        from src import database
        dims = database.load_dimensions(conn)
        return cls(dims["products"], dims["processes"], dims["sops"],
                   database.load_production_keys(conn))

    # ------------------------------------------------------------------ #
    def validate(self, raw_df: pd.DataFrame) -> ValidationResult:
        df = raw_df.copy()
        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                df[col] = np.nan
        for col in ("inspection_id", "batch_id", "product_id", "process_id", "sop_id"):
            df[col] = df[col].where(~_blank(df[col]), np.nan)
            df[col] = df[col].where(df[col].isna(), df[col].astype(str).str.strip())

        n = len(df)
        masks: dict[str, pd.Series] = {}
        reasons: dict[str, pd.Series] = {}
        empty = pd.Series("", index=df.index, dtype=object)

        # RULE 01 — PK duplicate (within batch, or in production from another batch)
        pk = df["inspection_id"]
        intra = pk.notna() & pk.duplicated(keep="first")
        prod = self.production_keys
        if not prod.empty:
            this_batches = set(df["batch_id"].dropna().astype(str))
            # Re-running the same batch is idempotent; only foreign batches conflict.
            foreign_ids = set(prod.loc[~prod["batch_id"].astype(str).isin(this_batches),
                                       "inspection_id"].astype(str))
            in_prod = pk.notna() & pk.isin(foreign_ids)
        else:
            in_prod = pd.Series(False, index=df.index)
        masks[RULE_01] = intra | in_prod
        reasons[RULE_01] = pd.Series(
            np.select([intra, in_prod],
                      ["Duplicate inspection_id '" + pk.astype(str) + "' repeated within batch",
                       "inspection_id '" + pk.astype(str) + "' already exists in production"],
                      ""), index=df.index)

        # RULE 02 — critical nulls
        null_frame = pd.DataFrame({c: _blank(df[c]) for c in config.CRITICAL_FIELDS})
        masks[RULE_02] = null_frame.any(axis=1)
        null_cols = _concat([np.where(null_frame[c], c + ", ", "") for c in null_frame], df.index)
        reasons[RULE_02] = np.where(masks[RULE_02],
                                    "Null critical field(s): " + null_cols.str.rstrip(", "), "")

        # RULE 03 — FK orphans
        fk_parts = []
        fk_mask = pd.Series(False, index=df.index)
        for col, domain, table in (
            ("product_id", self.valid_products, "dim_product_catalog"),
            ("process_id", self.valid_processes, "dim_production_process"),
            ("sop_id", self.valid_sops, "dim_sop_documentation"),
        ):
            orphan = df[col].notna() & ~df[col].isin(domain)
            fk_mask |= orphan
            fk_parts.append(np.where(orphan, f"{col}='" + df[col].astype(str) + f"' not in {table}; ", ""))
        masks[RULE_03] = fk_mask
        reasons[RULE_03] = _concat(fk_parts, df.index)

        # RULE 04 — schema / type
        meas_raw, cyc_raw = df["measurement_mm"], df["cycle_time_sec"]
        meas = pd.to_numeric(meas_raw, errors="coerce")
        cyc = pd.to_numeric(cyc_raw, errors="coerce")
        dates = pd.to_datetime(df["inspection_date"].astype(str).str.strip(),
                               format="%Y-%m-%d", errors="coerce")
        bad_meas = ~_blank(meas_raw) & meas.isna()
        bad_cyc = ~_blank(cyc_raw) & cyc.isna()
        bad_date = dates.isna()
        masks[RULE_04] = bad_meas | bad_cyc | bad_date
        reasons[RULE_04] = _concat([
            np.where(bad_meas, "measurement_mm='" + meas_raw.astype(str) + "' not numeric; ", ""),
            np.where(bad_cyc, "cycle_time_sec='" + cyc_raw.astype(str) + "' not numeric; ", ""),
            np.where(bad_date, "inspection_date='" + df["inspection_date"].astype(str) + "' unparseable; ", ""),
        ], df.index)

        # RULE 05 — physical range anomalies
        bad_m_range = meas.notna() & ((meas < config.MEASUREMENT_MIN_MM) | (meas > config.MEASUREMENT_MAX_MM))
        bad_c_range = cyc.notna() & (cyc <= 0)
        masks[RULE_05] = bad_m_range | bad_c_range
        reasons[RULE_05] = _concat([
            np.where(bad_m_range, "measurement_mm=" + meas.astype(str) + " outside 0-1000 mm; ", ""),
            np.where(bad_c_range, "cycle_time_sec=" + cyc.astype(str) + " <= 0; ", ""),
        ], df.index)

        # RULE 06 — SOP compliance
        expired = df["sop_id"].notna() & df["sop_id"].isin(self.expired_sops)
        masks[RULE_06] = expired
        reasons[RULE_06] = np.where(
            expired, "sop_id='" + df["sop_id"].astype(str) + "' is Expired (untraceable work instruction)", "")

        # ---- combine ---------------------------------------------------- #
        mask_frame = pd.DataFrame(masks).fillna(False).astype(bool)
        failed_any = mask_frame.any(axis=1)

        failed_rules = empty.copy()
        failure_reason = empty.copy()
        for rule in ALL_RULES:
            m = mask_frame[rule]
            failed_rules = failed_rules + np.where(m, rule + " | ", "")
            failure_reason = failure_reason + np.where(
                m, "[" + rule[:7] + "] " + pd.Series(reasons[rule], index=df.index).astype(str) + " | ", "")
        failed_rules = failed_rules.str.rstrip(" |")
        failure_reason = failure_reason.str.rstrip(" |")

        rule_counts = {r: int(mask_frame[r].sum()) for r in ALL_RULES}
        valid = int((~failed_any).sum())
        score = round(valid / n * 100, 1) if n else 0.0

        # Clean, typed output for loading into the fact table
        clean = df.loc[~failed_any].copy()
        clean["measurement_mm"] = meas[~failed_any].astype(float)
        clean["cycle_time_sec"] = cyc[~failed_any].astype(float)
        clean["inspection_date"] = dates[~failed_any].dt.strftime("%Y-%m-%d")
        for flag, default in (("defect_flag", 0), ("first_pass_yield_flag", 1)):
            src = clean[flag] if flag in clean else pd.Series(default, index=clean.index)
            clean[flag] = pd.to_numeric(src, errors="coerce").fillna(default).astype(int)

        quarantine = raw_df.loc[failed_any].copy()
        quarantine.insert(0, "source_row_number", quarantine.index + 2)  # header = row 1
        quarantine["failed_rules"] = failed_rules[failed_any]
        quarantine["failure_reason"] = failure_reason[failed_any]

        return ValidationResult(
            clean_df=clean.reset_index(drop=True),
            quarantine_df=quarantine.reset_index(drop=True),
            health_score_pct=score,
            sla_passed=bool(score >= self.sla_threshold_pct),
            rule_failure_counts=rule_counts,
            total_records=n,
            valid_records=valid,
            invalid_records=n - valid,
            sla_threshold_pct=self.sla_threshold_pct,
        )
