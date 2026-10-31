"""
Enterprise Operations Quality — Pre-BI Data Validation Gatekeeper console.

    streamlit run app.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from src import config, data_generator, database, email_alerter, pipeline
from src.validator import ALL_RULES

st.set_page_config(
    page_title="Operations Quality Gatekeeper",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --------------------------------------------------------------------------- #
# Minimal, clean styling
# --------------------------------------------------------------------------- #
st.markdown("""
<style>
  .block-container {padding-top: 1.5rem; padding-bottom: 2rem; max-width: 1400px;}
  .app-title {font-size: 1.4rem; font-weight: 700; color: #0f172a; letter-spacing: -0.01em; margin-bottom: 0.15rem;}
  .app-sub {color: #64748b; font-size: 0.875rem; margin-bottom: 1.25rem;}
  .status-bar {padding: 0.85rem 1.1rem; border-radius: 6px; font-size: 0.92rem;
               margin: 0.25rem 0 1.25rem 0; border: 1px solid; border-left-width: 4px;}
  .status-bar strong {font-weight: 600;}
  .status-bar span {display: block; font-size: 0.82rem; margin-top: 0.2rem; opacity: 0.85;}
  .status-pass {background: #f8fafc; color: #0f172a; border-color: #cbd5e1; border-left-color: #16a34a;}
  .status-breach {background: #fef2f2; color: #7f1d1d; border-color: #fecaca; border-left-color: #dc2626;}
  div[data-testid="stMetric"] {background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;
           padding: 0.75rem 1rem;}
  .lkg-box {border-radius: 6px; padding: 0.75rem; font-size: 0.8rem; line-height: 1.5;
            background: #f8fafc; border: 1px solid #e2e8f0; color: #334155;}
  .lkg-label {font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em;
              margin-bottom: 0.25rem; display: block;}
  .lkg-ok .lkg-label {color: #15803d;}
  .lkg-stale .lkg-label {color: #b45309;}
  .section-title {font-size: 0.95rem; font-weight: 600; color: #0f172a; margin: 1rem 0 0.5rem 0;}
</style>
""", unsafe_allow_html=True)

STATUS_COLORS = {"Approved": "#16a34a", "In Review": "#d97706", "Expired": "#dc2626"}


# --------------------------------------------------------------------------- #
# Bootstrap & data access
# --------------------------------------------------------------------------- #
if not data_generator.is_seeded():
    with st.spinner("Initializing database and sample batches..."):
        data_generator.seed_all()


def load_audit() -> pd.DataFrame:
    with database.connect() as conn:
        return database.load_audit_runs(conn)


def load_sops() -> pd.DataFrame:
    with database.connect() as conn:
        return database.read_table(conn, "dim_sop_documentation")


def read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def business_date(row) -> pd.Timestamp:
    m = re.search(r"(\d{4})[_-](\d{2})[_-](\d{2})", str(row["batch_filename"]))
    if m:
        return pd.Timestamp(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return pd.Timestamp(row["run_timestamp"]).tz_convert(None).normalize()


def load_quarantine_from_file(path: Path | None) -> pd.DataFrame:
    if path and path.exists():
        try:
            return pd.read_excel(path, sheet_name="Quarantined_Records", dtype=str)
        except Exception:
            pass
    return pd.DataFrame()


def current_run_view() -> dict | None:
    if "last_run" in st.session_state:
        r = st.session_state["last_run"]
        res = r["result"]
        return {
            "batch": r["batch_filename"],
            "timestamp": r["run_timestamp"],
            "score": r["health_score_pct"],
            "passed": r["sla_passed"],
            "valid": res.valid_records,
            "invalid": res.invalid_records,
            "total": res.total_records,
            "rule_counts": res.rule_failure_counts,
            "quarantine_df": res.quarantine_df,
            "quarantine_path": r["quarantine_path"],
            "action": r["action"],
        }
    with database.connect() as conn:
        last = database.latest_audit_run(conn)
    if not last:
        return None
    qpath = pipeline.resolve_project_path(last["quarantine_file_path"])
    return {
        "batch": last["batch_filename"],
        "timestamp": last["run_timestamp"],
        "score": last["health_score_pct"],
        "passed": last["sla_status"] == "PASS",
        "valid": last["valid_records"],
        "invalid": last["invalid_records"],
        "total": last["total_records"],
        "rule_counts": json.loads(last["error_breakdown_json"] or "{}"),
        "quarantine_df": load_quarantine_from_file(qpath),
        "quarantine_path": qpath,
        "action": "Loaded from latest recorded audit run.",
    }


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "n/a"
    try:
        return datetime.fromisoformat(str(ts)).strftime("%b %d, %Y %H:%M")
    except ValueError:
        return str(ts)


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("#### Pipeline Controls")
    landing = sorted(p.name for p in Path(config.LANDING_ZONE_DIR).glob("*.csv"))
    default_idx = landing.index(config.CORRUPTED_BATCH_NAME) if config.CORRUPTED_BATCH_NAME in landing else 0
    selected = st.selectbox("Batch File", landing, index=default_idx)
    target_path = Path(config.LANDING_ZONE_DIR) / selected if selected else None

    run_clicked = st.button("Run Validation", type="primary", width="stretch")
    fix_clicked = st.button("Auto-Fix and Re-Run", width="stretch")

    if run_clicked and target_path:
        with st.spinner(f"Validating {target_path.name}..."):
            st.session_state["last_run"] = pipeline.run_pipeline(target_path)

    if fix_clicked:
        source = target_path
        if source is None or source.name == config.HEALTHY_BATCH_NAME:
            source = Path(config.LANDING_ZONE_DIR) / config.CORRUPTED_BATCH_NAME
        with st.spinner(f"Remediating {source.name}..."):
            fixed = pipeline.auto_fix_batch(source)
            st.session_state["last_run"] = pipeline.run_pipeline(fixed)

    st.divider()
    st.markdown("#### Power BI Snapshot")
    lkg = pipeline.read_lkg_metadata()
    stale = bool(lkg.get("stale_warning"))
    failed = lkg.get("last_failed_batch") or {}
    state_label = "Last-Known-Good (Stale)" if stale else "Certified (Current)"
    st.markdown(
        f"<div class='lkg-box {'lkg-stale' if stale else 'lkg-ok'}'>"
        f"<span class='lkg-label'>{state_label}</span>"
        f"Batch: {lkg.get('last_certified_batch', 'n/a')}<br>"
        f"Certified: {fmt_ts(lkg.get('last_certified_timestamp'))}<br>"
        f"Score: {lkg.get('health_score', 'n/a')}% &middot; Rows: {lkg.get('record_count', 'n/a')}"
        + (f"<br>Blocked: {failed.get('batch_filename')} ({failed.get('health_score')}%)" if stale else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    st.divider()
    if st.button("Reset Demo Data", width="stretch"):
        data_generator.seed_all(force=True)
        st.session_state.pop("last_run", None)
        st.rerun()


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.markdown("<div class='app-title'>Operations Quality Data Gatekeeper</div>", unsafe_allow_html=True)
st.markdown(
    "<div class='app-sub'>Pre-BI data validation, 80.0% SLA enforcement, and SOP compliance tracking</div>",
    unsafe_allow_html=True,
)

view = current_run_view()
audit = load_audit()
lkg = pipeline.read_lkg_metadata()

tab1, tab2, tab3, tab4 = st.tabs([
    "Overview",
    "Quarantine",
    "SOP Governance",
    "Email Alerts",
])

# --------------------------------------------------------------------------- #
# TAB 1 — Overview
# --------------------------------------------------------------------------- #
with tab1:
    if view is None:
        st.info("No validation runs recorded yet. Select a batch in the sidebar and click Run Validation.")
    else:
        if view["passed"]:
            st.markdown(
                f"<div class='status-bar status-pass'>"
                f"<strong>SLA Passed ({view['score']:.1f}%) — Power BI Refreshed</strong>"
                f"<span>Batch {view['batch']} met the {config.SLA_THRESHOLD_PERCENT:.0f}% threshold. {view['action']}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div class='status-bar status-breach'>"
                f"<strong>SLA Breach ({view['score']:.1f}%) — Power BI Locked to Last-Known-Good Snapshot</strong>"
                f"<span>Batch {view['batch']} fell below the {config.SLA_THRESHOLD_PERCENT:.0f}% threshold. "
                f"Serving {lkg.get('last_certified_batch')} while alert sent to {config.ALERT_RECIPIENT_EMAIL}.</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(
            "Health Score",
            f"{view['score']:.1f}%",
            delta=f"{view['score'] - config.SLA_THRESHOLD_PERCENT:+.1f}% vs SLA",
        )
        c2.metric(
            "Valid Records",
            f"{view['valid']:,}",
            delta=f"of {view['total']:,} total",
            delta_color="off",
        )
        c3.metric(
            "Quarantined Records",
            f"{view['invalid']:,}",
            delta=f"{(view['invalid'] / view['total'] * 100 if view['total'] else 0):.1f}% of batch",
            delta_color="inverse",
        )
        c4.metric(
            "Power BI Snapshot",
            "Stale (LKG)" if lkg.get("stale_warning") else "Certified",
            delta=fmt_ts(lkg.get("last_certified_timestamp")),
            delta_color="off",
        )

        left, right = st.columns([3, 2])
        with left:
            if not audit.empty:
                trend = audit.copy()
                trend["business_date"] = trend.apply(business_date, axis=1)
                trend = (
                    trend.sort_values("run_id")
                    .groupby("business_date")
                    .tail(1)
                    .sort_values("business_date")
                    .tail(14)
                )
                colors = ["#dc2626" if s == "BREACH" else "#0f172a" for s in trend["sla_status"]]
                fig = go.Figure()
                fig.add_trace(
                    go.Scatter(
                        x=trend["business_date"],
                        y=trend["health_score_pct"],
                        mode="lines+markers",
                        line=dict(color="#0f172a", width=2),
                        marker=dict(size=8, color=colors),
                        customdata=trend[["batch_filename", "sla_status"]],
                        hovertemplate="<b>%{x|%b %d}</b><br>Score: %{y:.1f}%<br>%{customdata[0]} (%{customdata[1]})<extra></extra>",
                    )
                )
                fig.add_hline(
                    y=config.SLA_THRESHOLD_PERCENT,
                    line_dash="dash",
                    line_color="#dc2626",
                    line_width=1.5,
                    annotation_text="80% SLA",
                    annotation_position="top left",
                    annotation_font_color="#dc2626",
                )
                ymin = min(65, float(trend["health_score_pct"].min()) - 5)
                fig.update_layout(
                    title=dict(text="14-Day SLA Health Trend", font=dict(size=14, color="#0f172a")),
                    height=340,
                    yaxis=dict(title="Health Score (%)", range=[ymin, 102]),
                    xaxis=dict(title="", tickformat="%b %d"),
                    margin=dict(l=10, r=10, t=40, b=10),
                    showlegend=False,
                    plot_bgcolor="white",
                    paper_bgcolor="white",
                )
                fig.update_xaxes(showgrid=False, linecolor="#e2e8f0")
                fig.update_yaxes(showgrid=True, gridcolor="#f1f5f9", linecolor="#e2e8f0")
                st.plotly_chart(fig, width="stretch")

        with right:
            counts = pd.Series({r: int(view["rule_counts"].get(r, 0)) for r in ALL_RULES}).sort_values(ascending=True)
            short_labels = [r.replace("RULE_", "R").replace("_", " ") for r in counts.index]
            fig = go.Figure(
                go.Bar(
                    x=counts.values,
                    y=short_labels,
                    orientation="h",
                    marker_color="#334155",
                    text=counts.values,
                    textposition="outside",
                )
            )
            max_x = max(int(counts.max()) * 1.2, 5)
            fig.update_layout(
                title=dict(text="Rule Violations (Current Batch)", font=dict(size=14, color="#0f172a")),
                height=340,
                xaxis=dict(title="Failed Rows", range=[0, max_x]),
                yaxis=dict(title=""),
                margin=dict(l=10, r=20, t=40, b=10),
                showlegend=False,
                plot_bgcolor="white",
                paper_bgcolor="white",
            )
            fig.update_xaxes(showgrid=True, gridcolor="#f1f5f9", linecolor="#e2e8f0")
            fig.update_yaxes(showgrid=False, linecolor="#e2e8f0")
            st.plotly_chart(fig, width="stretch")

        if not audit.empty:
            st.markdown("<div class='section-title'>Recent Validation Runs</div>", unsafe_allow_html=True)
            history = audit.sort_values("run_id", ascending=False).head(10).copy()
            history["run_timestamp"] = history["run_timestamp"].dt.strftime("%Y-%m-%d %H:%M")
            history = history[[
                "run_id",
                "run_timestamp",
                "batch_filename",
                "total_records",
                "valid_records",
                "invalid_records",
                "health_score_pct",
                "sla_status",
            ]].rename(columns={
                "run_id": "Run ID",
                "run_timestamp": "Timestamp",
                "batch_filename": "Batch",
                "total_records": "Total",
                "valid_records": "Valid",
                "invalid_records": "Quarantined",
                "health_score_pct": "Score (%)",
                "sla_status": "Status",
            })
            st.dataframe(history, width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
# TAB 2 — Quarantine
# --------------------------------------------------------------------------- #
with tab2:
    if view is None or view["quarantine_df"].empty:
        st.info("No quarantined records for the current batch.")
    else:
        qdf = view["quarantine_df"].copy()
        f1, f2, f3 = st.columns([2, 2, 1])
        rule_filter = f1.multiselect("Filter by Rule", ALL_RULES, default=[])
        search = f2.text_input("Search ID or Reason")

        qpath = view["quarantine_path"]
        with f3:
            st.write("")
            if qpath and Path(qpath).exists():
                st.download_button(
                    "Download Excel",
                    Path(qpath).read_bytes(),
                    file_name=Path(qpath).name,
                    type="primary",
                    width="stretch",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

        mask = pd.Series(True, index=qdf.index)
        if rule_filter:
            mask &= qdf["failed_rules"].fillna("").apply(lambda s: any(r in s for r in rule_filter))
        if search:
            mask &= (
                qdf["inspection_id"].fillna("").str.contains(search, case=False, regex=False)
                | qdf["failure_reason"].fillna("").str.contains(search, case=False, regex=False)
            )
        filtered = qdf[mask]
        st.caption(f"Showing {len(filtered):,} of {len(qdf):,} quarantined records from {view['batch']}")

        lead = ["source_row_number", "inspection_id", "failed_rules", "failure_reason"]
        cols = [c for c in lead if c in filtered] + [c for c in filtered.columns if c not in lead]
        st.dataframe(
            filtered[cols],
            width="stretch",
            hide_index=True,
            height=460,
            column_config={"failure_reason": st.column_config.TextColumn(width="large")},
        )

# --------------------------------------------------------------------------- #
# TAB 3 — SOP Governance
# --------------------------------------------------------------------------- #
with tab3:
    sops = load_sops()
    sops["sla_met"] = sops["actual_review_days"] <= sops["approval_sla_days"]

    rule6_hits = pd.Series(dtype=int)
    if view is not None and not view["quarantine_df"].empty and "sop_id" in view["quarantine_df"]:
        q = view["quarantine_df"]
        rule6_hits = q.loc[q["failed_rules"].fillna("").str.contains("RULE_06"), "sop_id"].value_counts()
    sops["rule06_failures"] = sops["sop_id"].map(rule6_hits).fillna(0).astype(int)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Controlled Documents", len(sops))
    k2.metric("Review SLA Compliance", f"{sops['sla_met'].mean() * 100:.0f}%")
    k3.metric("Expired Documents", int((sops["review_status"] == "Expired").sum()))
    k4.metric("Rule 06 Failures (Current Batch)", int(sops["rule06_failures"].sum()))

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=sops["sop_id"],
            y=sops["actual_review_days"],
            name="Actual Review Days",
            marker_color=[STATUS_COLORS.get(s, "#475569") for s in sops["review_status"]],
        )
    )
    fig.add_trace(
        go.Scatter(
            x=sops["sop_id"],
            y=sops["approval_sla_days"],
            mode="markers",
            marker=dict(symbol="line-ew-open", size=28, color="#0f172a", line=dict(width=2.5)),
            name="SLA Target (Days)",
        )
    )
    fig.update_layout(
        title=dict(text="SOP Review Cycle Time vs SLA Target (Days)", font=dict(size=14, color="#0f172a")),
        height=340,
        plot_bgcolor="white",
        paper_bgcolor="white",
        legend=dict(orientation="h", y=1.12, x=1, xanchor="right"),
        margin=dict(l=10, r=10, t=45, b=10),
    )
    fig.update_xaxes(showgrid=False, linecolor="#e2e8f0")
    fig.update_yaxes(title="Days", showgrid=True, gridcolor="#f1f5f9", linecolor="#e2e8f0")
    st.plotly_chart(fig, width="stretch")

    st.markdown("<div class='section-title'>Controlled Document Register</div>", unsafe_allow_html=True)
    display_sops = sops[[
        "sop_id",
        "sop_title",
        "version",
        "owner_role",
        "last_reviewed_date",
        "review_status",
        "approval_sla_days",
        "actual_review_days",
        "rule06_failures",
    ]].rename(columns={
        "sop_id": "SOP ID",
        "sop_title": "Title",
        "version": "Version",
        "owner_role": "Owner",
        "last_reviewed_date": "Last Reviewed",
        "review_status": "Status",
        "approval_sla_days": "SLA Target (Days)",
        "actual_review_days": "Actual Days",
        "rule06_failures": "Rule 06 Failures",
    })
    st.dataframe(display_sops, width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
# TAB 4 — Email Alerts
# --------------------------------------------------------------------------- #
with tab4:
    status = read_json(config.LATEST_EMAIL_STATUS_PATH)
    e1, e2, e3, e4 = st.columns([2, 1, 1, 1])
    e1.metric("Recipient", config.ALERT_RECIPIENT_EMAIL)
    e2.metric("Mode", "Live SMTP" if config.smtp_configured() else "Local Fallback")
    e3.metric("Last Alert", fmt_ts(status.get("generated_at")) if status else "None")
    with e4:
        st.write("")
        send_test = st.button("Send Test Email", width="stretch")

    if send_test:
        with st.spinner("Sending test email..."):
            res = email_alerter.send_test_email()
        if res["sent_live"]:
            st.success(res["status_message"])
        else:
            st.info(res["status_message"])

    if status:
        st.caption(f"Subject: {status.get('subject')} | Status: {status.get('status_message')}")

    html_path = Path(config.LATEST_EMAIL_HTML_PATH)
    if html_path.exists():
        if hasattr(st, "iframe"):
            st.iframe(html_path, height=960)
        else:
            components.html(html_path.read_text(encoding="utf-8"), height=960, scrolling=True)
    else:
        st.info("No alert preview available yet. Run validation on a breached batch to generate an alert.")
