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
from plotly.subplots import make_subplots

from src import config, data_generator, database, email_alerter, pipeline
from src.validator import ALL_RULES

st.set_page_config(page_title="Ops Quality Data Gatekeeper", page_icon="🛡️", layout="wide",
                   initial_sidebar_state="expanded")

# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #
st.markdown("""
<style>
  .block-container {padding-top: 1.6rem; padding-bottom: 2rem;}
  .app-title {font-size: 1.55rem; font-weight: 800; color: #0f172a; margin-bottom: .1rem;}
  .app-sub {color: #475569; font-size: .92rem; margin-bottom: 1rem;}
  .banner {padding: 1rem 1.25rem; border-radius: 10px; font-weight: 700; font-size: 1.05rem;
           margin: .3rem 0 1rem 0; border-left: 8px solid;}
  .banner small {display:block; font-weight: 500; font-size: .85rem; margin-top: .3rem; opacity: .9;}
  .banner-pass {background: #ecfdf5; color: #065f46; border-color: #059669;}
  .banner-breach {background: #fef2f2; color: #991b1b; border-color: #dc2626;}
  div[data-testid="stMetric"] {background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
           padding: .8rem 1rem; box-shadow: 0 1px 2px rgba(15,23,42,.05);}
  .lkg-card {border-radius: 8px; padding: .7rem .8rem; font-size: .82rem; line-height: 1.45;}
  .lkg-ok {background:#ecfdf5; border:1px solid #6ee7b7; color:#065f46;}
  .lkg-stale {background:#fff7ed; border:1px solid #fdba74; color:#9a3412;}
  .pill {display:inline-block; padding:.1rem .55rem; border-radius:999px; font-size:.75rem; font-weight:700;}
</style>
""", unsafe_allow_html=True)

RULE_COLORS = {
    "RULE_01_PK_DUPLICATE": "#7c3aed", "RULE_02_CRITICAL_NULL": "#dc2626",
    "RULE_03_FK_ORPHAN": "#ea580c", "RULE_04_SCHEMA_TYPE": "#ca8a04",
    "RULE_05_RANGE_ANOMALY": "#0891b2", "RULE_06_SOP_COMPLIANCE": "#db2777",
}
STATUS_COLORS = {"Approved": "#059669", "In Review": "#d97706", "Expired": "#dc2626"}


# --------------------------------------------------------------------------- #
# Bootstrap & data access
# --------------------------------------------------------------------------- #
if not data_generator.is_seeded():
    with st.spinner("First run detected — seeding Star Schema, history & landing-zone batches..."):
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
    """Latest run: from this session if available, else from the audit log."""
    if "last_run" in st.session_state:
        r = st.session_state["last_run"]
        res = r["result"]
        return {
            "batch": r["batch_filename"], "timestamp": r["run_timestamp"],
            "score": r["health_score_pct"], "passed": r["sla_passed"],
            "valid": res.valid_records, "invalid": res.invalid_records,
            "total": res.total_records, "rule_counts": res.rule_failure_counts,
            "quarantine_df": res.quarantine_df, "quarantine_path": r["quarantine_path"],
            "action": r["action"],
        }
    with database.connect() as conn:
        last = database.latest_audit_run(conn)
    if not last:
        return None
    qpath = pipeline.resolve_project_path(last["quarantine_file_path"])
    return {
        "batch": last["batch_filename"], "timestamp": last["run_timestamp"],
        "score": last["health_score_pct"], "passed": last["sla_status"] == "PASS",
        "valid": last["valid_records"], "invalid": last["invalid_records"],
        "total": last["total_records"],
        "rule_counts": json.loads(last["error_breakdown_json"] or "{}"),
        "quarantine_df": load_quarantine_from_file(qpath), "quarantine_path": qpath,
        "action": "Loaded from audit_sla_runs (latest recorded run).",
    }


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "n/a"
    try:
        return datetime.fromisoformat(str(ts)).strftime("%b %d, %Y %H:%M")
    except ValueError:
        return str(ts)


# --------------------------------------------------------------------------- #
# Sidebar control panel
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("### 🛡️ Gatekeeper Control Panel")
    landing = sorted(p.name for p in Path(config.LANDING_ZONE_DIR).glob("*.csv"))
    default_idx = landing.index(config.CORRUPTED_BATCH_NAME) if config.CORRUPTED_BATCH_NAME in landing else 0
    selected = st.selectbox("Daily batch in `data/landing_zone/`", landing, index=default_idx)

    uploaded = st.file_uploader("…or upload a custom CSV batch", type=["csv"])
    target_path = Path(config.LANDING_ZONE_DIR) / selected if selected else None
    if uploaded is not None:
        target_path = Path(config.LANDING_ZONE_DIR) / f"upload_{Path(uploaded.name).name}"
        target_path.write_bytes(uploaded.getvalue())
        st.caption(f"Using uploaded file → `{target_path.name}`")

    run_clicked = st.button("▶ Run Daily Validation Gatekeeper", type="primary", width="stretch")
    fix_clicked = st.button("🛠 Auto-Fix Quarantined Batch & Re-Run", width="stretch",
                            help="Simulates data-engineering remediation (re-key duplicates, "
                                 "impute nulls, remap orphan FKs & expired SOPs) then promotes to Power BI.")

    if run_clicked and target_path:
        with st.spinner(f"Validating {target_path.name} against 6 enterprise rules..."):
            st.session_state["last_run"] = pipeline.run_pipeline(target_path)
        st.toast(st.session_state["last_run"]["action"], icon="✅" if st.session_state["last_run"]["sla_passed"] else "🚨")

    if fix_clicked:
        source = target_path
        if source is None or source.name == config.HEALTHY_BATCH_NAME:
            source = Path(config.LANDING_ZONE_DIR) / config.CORRUPTED_BATCH_NAME
        with st.spinner(f"Remediating {source.name} and re-running the gate..."):
            fixed = pipeline.auto_fix_batch(source)
            st.session_state["last_run"] = pipeline.run_pipeline(fixed)
        st.toast(f"Remediated → {fixed.name}: {st.session_state['last_run']['action']}", icon="🛠")

    st.divider()
    st.markdown("#### 📡 Live Power BI LKG Status")
    lkg = pipeline.read_lkg_metadata()
    stale = bool(lkg.get("stale_warning"))
    failed = lkg.get("last_failed_batch") or {}
    st.markdown(
        f"<div class='lkg-card {'lkg-stale' if stale else 'lkg-ok'}'>"
        f"<b>{'🟠 SERVING LAST-KNOWN-GOOD (STALE)' if stale else '🟢 SERVING CURRENT CERTIFIED DATA'}</b><br>"
        f"Snapshot: <code>{lkg.get('last_certified_batch', 'n/a')}</code><br>"
        f"Certified: {fmt_ts(lkg.get('last_certified_timestamp'))}<br>"
        f"Health: {lkg.get('health_score', 'n/a')}% · Fact rows: {lkg.get('record_count', 'n/a')}"
        + (f"<br>Blocked batch: <code>{failed.get('batch_filename')}</code> ({failed.get('health_score')}%)"
           if stale else "")
        + "</div>", unsafe_allow_html=True)

    with st.expander("⚙️ Demo utilities"):
        if st.button("Reset & re-seed demo environment", width="stretch"):
            data_generator.seed_all(force=True)
            st.session_state.pop("last_run", None)
            st.rerun()
    st.caption(f"SLA gate: **{config.SLA_THRESHOLD_PERCENT:.1f}%** · Alerts → {config.ALERT_RECIPIENT_EMAIL}")


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.markdown(f"<div class='app-title'>🏭 {config.APP_TITLE}</div>", unsafe_allow_html=True)
st.markdown("<div class='app-sub'>Last-Known-Good staging · 6-rule vectorized validation · "
            "80.0% Data Quality SLA · Excel quarantine · SOP traceability · SMTP alerting</div>",
            unsafe_allow_html=True)

view = current_run_view()
audit = load_audit()
lkg = pipeline.read_lkg_metadata()

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Morning BI Readiness & SLA Overview",
    "🧪 Quarantine Inspector & Triage",
    "📑 SOP & Work-Instruction Governance",
    f"✉️ Email Alert Center ({config.ALERT_RECIPIENT_EMAIL})",
])

# --------------------------------------------------------------------------- #
# TAB 1 — Executive overview
# --------------------------------------------------------------------------- #
with tab1:
    if view is None:
        st.info("No validation runs yet. Use **Run Daily Validation Gatekeeper** in the sidebar.")
    else:
        if view["passed"]:
            st.markdown(
                f"<div class='banner banner-pass'>✅ SLA PASSED ≥ 80% — Power BI Refreshed with Today's Data"
                f"<small>Batch <code>{view['batch']}</code> scored {view['score']:.1f}% · {view['action']}</small></div>",
                unsafe_allow_html=True)
        else:
            st.markdown(
                f"<div class='banner banner-breach'>🚨 CRITICAL SLA BREACH &lt; 80% — Power BI Protected with "
                f"Yesterday's Last-Known-Good Data | Alert Sent to {config.ALERT_RECIPIENT_EMAIL}"
                f"<small>Batch <code>{view['batch']}</code> scored {view['score']:.1f}% · "
                f"Serving LKG <code>{lkg.get('last_certified_batch')}</code></small></div>",
                unsafe_allow_html=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Daily Health Score", f"{view['score']:.1f}%",
                  delta=f"{view['score'] - config.SLA_THRESHOLD_PERCENT:+.1f} pts vs 80% SLA")
        c2.metric("Validated Records", f"{view['valid']:,}", delta=f"of {view['total']:,} received",
                  delta_color="off")
        c3.metric("Quarantined Records", f"{view['invalid']:,}",
                  delta=f"{(view['invalid'] / view['total'] * 100 if view['total'] else 0):.1f}% of batch",
                  delta_color="inverse")
        c4.metric("Current Power BI Snapshot", fmt_ts(lkg.get("last_certified_timestamp")),
                  delta="STALE — LKG locked" if lkg.get("stale_warning") else "Fresh — certified",
                  delta_color="inverse" if lkg.get("stale_warning") else "normal")

        left, right = st.columns([3, 2])
        with left:
            if not audit.empty:
                trend = audit.copy()
                trend["business_date"] = trend.apply(business_date, axis=1)
                trend = (trend.sort_values("run_id").groupby("business_date").tail(1)
                         .sort_values("business_date").tail(14))
                colors = ["#dc2626" if s == "BREACH" else "#059669" for s in trend["sla_status"]]
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=trend["business_date"], y=trend["health_score_pct"], mode="lines+markers",
                    fill="tozeroy", fillcolor="rgba(37,99,235,0.12)",
                    line=dict(color="#2563eb", width=3, shape="spline"),
                    marker=dict(size=11, color=colors, line=dict(color="white", width=2)),
                    customdata=trend[["batch_filename", "sla_status", "valid_records", "total_records"]],
                    hovertemplate="<b>%{x|%a %b %d}</b><br>Health: %{y:.1f}%<br>%{customdata[0]}"
                                  "<br>Status: %{customdata[1]} (%{customdata[2]}/%{customdata[3]})<extra></extra>",
                    name="Health score"))
                fig.add_hline(y=config.SLA_THRESHOLD_PERCENT, line_dash="dash", line_color="#dc2626",
                              line_width=3, annotation_text="80.0% SLA Threshold",
                              annotation_position="top left", annotation_font_color="#dc2626")
                ymin = min(60, float(trend["health_score_pct"].min()) - 5)
                fig.update_layout(title="14-Day Data Quality SLA Trend", height=400,
                                  yaxis=dict(title="Health score (%)", range=[ymin, 101]),
                                  xaxis=dict(title="Business date", tickformat="%b %d"),
                                  margin=dict(l=10, r=10, t=50, b=10), showlegend=False,
                                  plot_bgcolor="white")
                fig.update_xaxes(showgrid=True, gridcolor="#f1f5f9")
                fig.update_yaxes(showgrid=True, gridcolor="#f1f5f9")
                st.plotly_chart(fig, width="stretch")
        with right:
            counts = pd.Series({r: int(view["rule_counts"].get(r, 0)) for r in ALL_RULES})
            counts = counts.sort_values(ascending=False)
            total_fail = counts.sum()
            fig = make_subplots(specs=[[{"secondary_y": True}]])
            fig.add_trace(go.Bar(x=[r[:7] for r in counts.index], y=counts.values,
                                 marker_color=[RULE_COLORS[r] for r in counts.index],
                                 text=counts.values, textposition="outside",
                                 hovertext=counts.index, name="Failed rows"), secondary_y=False)
            if total_fail:
                fig.add_trace(go.Scatter(x=[r[:7] for r in counts.index],
                                         y=(counts.cumsum() / total_fail * 100).round(1),
                                         mode="lines+markers", line=dict(color="#0f172a", width=2),
                                         name="Cumulative %"), secondary_y=True)
            fig.update_layout(title=f"Defect Pareto by Validation Rule — {view['batch']}", height=400,
                              margin=dict(l=10, r=10, t=50, b=10), plot_bgcolor="white",
                              legend=dict(orientation="h", y=-0.15))
            fig.update_yaxes(title_text="Failed rows", secondary_y=False)
            fig.update_yaxes(title_text="Cumulative %", range=[0, 105], secondary_y=True)
            st.plotly_chart(fig, width="stretch")

        with st.expander("📁 Power BI Star-Schema files currently served from `data/production_bi/`"):
            rows = []
            for f in sorted(Path(config.PRODUCTION_BI_DIR).glob("*")):
                rows.append({"file": f.name, "size_kb": round(f.stat().st_size / 1024, 1),
                             "last_modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            st.json(lkg)

        with st.expander("🗂 Audit log — `audit_sla_runs`"):
            if not audit.empty:
                show = audit.sort_values("run_id", ascending=False).copy()
                show["run_timestamp"] = show["run_timestamp"].dt.strftime("%Y-%m-%d %H:%M UTC")
                st.dataframe(show, width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
# TAB 2 — Quarantine inspector
# --------------------------------------------------------------------------- #
with tab2:
    if view is None or view["quarantine_df"].empty:
        st.success("No quarantined records for the latest run (or quarantine workbook archived).")
    else:
        qdf = view["quarantine_df"].copy()
        st.subheader(f"Quarantined records — `{view['batch']}` ({len(qdf):,} rows)")
        f1, f2, f3 = st.columns([2, 1, 1])
        rule_filter = f1.multiselect("Filter by failed rule", ALL_RULES, default=[])
        product_filter = f2.multiselect("Product", sorted(qdf["product_id"].dropna().unique()))
        search = f3.text_input("Search (ID / reason)")
        mask = pd.Series(True, index=qdf.index)
        if rule_filter:
            mask &= qdf["failed_rules"].fillna("").apply(lambda s: any(r in s for r in rule_filter))
        if product_filter:
            mask &= qdf["product_id"].isin(product_filter)
        if search:
            mask &= (qdf["inspection_id"].fillna("").str.contains(search, case=False, regex=False)
                     | qdf["failure_reason"].fillna("").str.contains(search, case=False, regex=False))
        filtered = qdf[mask]
        st.caption(f"Showing {len(filtered):,} of {len(qdf):,} quarantined rows")
        lead = ["source_row_number", "inspection_id", "failed_rules", "failure_reason"]
        cols = [c for c in lead if c in filtered] + [c for c in filtered.columns if c not in lead]
        st.dataframe(filtered[cols], width="stretch", hide_index=True, height=460,
                     column_config={"failure_reason": st.column_config.TextColumn(width="large")})

        qpath = view["quarantine_path"]
        d1, d2 = st.columns(2)
        if qpath and Path(qpath).exists():
            d1.download_button("⬇️ Download Quarantine Workbook (.xlsx)", Path(qpath).read_bytes(),
                               file_name=Path(qpath).name, type="primary", width="stretch",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            d1.caption(f"`{pipeline._rel(Path(qpath))}` · sheets: Summary_Metrics, Quarantined_Records")
        d2.download_button("⬇️ Download filtered view (.csv)", filtered[cols].to_csv(index=False).encode(),
                           file_name=f"filtered_{Path(view['batch']).stem}.csv", width="stretch")

# --------------------------------------------------------------------------- #
# TAB 3 — SOP governance
# --------------------------------------------------------------------------- #
with tab3:
    sops = load_sops()
    sops["sla_met"] = sops["actual_review_days"] <= sops["approval_sla_days"]
    sops["days_over_sla"] = (sops["actual_review_days"] - sops["approval_sla_days"]).clip(lower=0)

    rule6_hits = pd.Series(dtype=int)
    if view is not None and not view["quarantine_df"].empty and "sop_id" in view["quarantine_df"]:
        q = view["quarantine_df"]
        rule6_hits = q.loc[q["failed_rules"].fillna("").str.contains("RULE_06"), "sop_id"].value_counts()
    sops["rule06_failures_latest_run"] = sops["sop_id"].map(rule6_hits).fillna(0).astype(int)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Controlled documents", len(sops))
    k2.metric("Review SLA compliance", f"{sops['sla_met'].mean() * 100:.0f}%")
    k3.metric("Expired work instructions", int((sops["review_status"] == "Expired").sum()),
              delta="blocks production data (RULE_06)", delta_color="off")
    k4.metric("RULE_06 failures (latest run)", int(sops["rule06_failures_latest_run"].sum()))

    g1, g2 = st.columns([3, 2])
    with g1:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=sops["sop_id"], y=sops["actual_review_days"], name="Actual review days",
                             marker_color=[STATUS_COLORS[s] for s in sops["review_status"]],
                             customdata=sops[["sop_title", "review_status", "owner_role"]],
                             hovertemplate="<b>%{x}</b> %{customdata[0]}<br>%{customdata[1]} · "
                                           "%{customdata[2]}<br>Actual: %{y} days<extra></extra>"))
        fig.add_trace(go.Scatter(x=sops["sop_id"], y=sops["approval_sla_days"], mode="markers",
                                 marker=dict(symbol="line-ew-open", size=34, color="#0f172a",
                                             line=dict(width=3)), name="Approval SLA target"))
        fig.update_layout(title="Review & Approval Cycle Time vs SLA Target (days)", height=420,
                          plot_bgcolor="white", legend=dict(orientation="h", y=-0.18),
                          margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, width="stretch")
    with g2:
        status = sops["review_status"].value_counts()
        fig = go.Figure(go.Pie(labels=status.index, values=status.values, hole=0.55,
                               marker_colors=[STATUS_COLORS[s] for s in status.index],
                               textinfo="label+value"))
        fig.update_layout(title="Document Status Breakdown", height=420, showlegend=False,
                          margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig, width="stretch")

    st.markdown("##### 🚫 Expired work instructions causing `RULE_06_SOP_COMPLIANCE` failures")
    expired = sops[sops["review_status"] == "Expired"].copy()
    expired["superseded_by"] = expired["sop_id"].map(config.SOP_SUPERSESSION_MAP)
    if not expired.empty:
        fig = go.Figure(go.Bar(x=expired["rule06_failures_latest_run"], y=expired["sop_id"] + " · " + expired["sop_title"],
                               orientation="h", marker_color="#dc2626",
                               text=expired["rule06_failures_latest_run"], textposition="outside"))
        fig.update_layout(height=220, margin=dict(l=10, r=30, t=10, b=10), plot_bgcolor="white",
                          xaxis_title="Quarantined inspections in latest run")
        st.plotly_chart(fig, width="stretch")
    st.dataframe(
        expired[["sop_id", "sop_title", "version", "owner_role", "last_reviewed_date",
                 "approval_sla_days", "actual_review_days", "rule06_failures_latest_run", "superseded_by"]],
        width="stretch", hide_index=True)
    with st.expander("Full `dim_sop_documentation` register"):
        st.dataframe(sops, width="stretch", hide_index=True)

# --------------------------------------------------------------------------- #
# TAB 4 — Email alert centre
# --------------------------------------------------------------------------- #
with tab4:
    status = read_json(config.LATEST_EMAIL_STATUS_PATH)
    e1, e2, e3 = st.columns(3)
    e1.metric("Alert recipient", config.ALERT_RECIPIENT_EMAIL)
    e2.metric("SMTP mode", "LIVE (Gmail SSL 465)" if config.smtp_configured() else "Local fallback")
    e3.metric("Last alert", fmt_ts(status.get("generated_at")) if status else "none yet")

    if status:
        (st.success if status.get("sent_live") else st.warning)(f"**Dispatch status:** {status.get('status_message')}")
        st.markdown(f"**Subject:** {status.get('subject')}")
        attach = status.get("attachment_path")
        st.markdown(f"**Attached quarantine file:** `{pipeline._rel(Path(attach)) if attach else 'n/a'}`")
    else:
        st.info("No SLA breach alert generated yet. Run the corrupted batch to trigger one.")

    if not config.smtp_configured():
        st.caption("Configure `SMTP_SENDER_EMAIL` and `SMTP_APP_PASSWORD` (Gmail App Password) in `.env`, "
                   "then restart the app to enable live dispatch.")
    if st.button(f"📨 Send live test email to {config.ALERT_RECIPIENT_EMAIL}"):
        with st.spinner("Contacting smtp.gmail.com:465 ..."):
            res = email_alerter.send_test_email()
        (st.success if res["sent_live"] else st.warning)(res["status_message"])

    st.markdown("##### 🖥️ Embedded preview — `data/quarantine/latest_email_alert.html`")
    html_path = Path(config.LATEST_EMAIL_HTML_PATH)
    if html_path.exists():
        if hasattr(st, "iframe"):  # Streamlit >= 1.5x
            st.iframe(html_path, height=1150)
        else:  # older Streamlit fallback
            components.html(html_path.read_text(encoding="utf-8"), height=1150, scrolling=True)
    else:
        st.caption("Preview will appear after the first SLA breach.")
