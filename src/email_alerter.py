"""
Automated SLA-breach alerting.

* Builds an executive-ready HTML email (+ .xlsx quarantine attachment).
* Always saves the exact HTML to data/quarantine/latest_email_alert.html and a
  JSON dispatch status to data/quarantine/latest_email_status.json.
* Sends live via Gmail SMTP-SSL (port 465) only when .env credentials exist;
  otherwise falls back to local mode. Never raises to the caller.
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from src import config

logger = logging.getLogger("ops_quality.email")

RULE_REMEDIATION = {
    "RULE_01_PK_DUPLICATE": "De-duplicate inspection_id at the MES export; confirm sequence generator reset.",
    "RULE_02_CRITICAL_NULL": "Re-extract rows with missing keys/measurements from the line historian.",
    "RULE_03_FK_ORPHAN": "Register new process/product codes in master data or correct the MES mapping.",
    "RULE_04_SCHEMA_TYPE": "Fix gauge export formatting (numeric decimals, ISO-8601 dates).",
    "RULE_05_RANGE_ANOMALY": "Inspect gauge calibration & unit-of-measure settings for the affected stations.",
    "RULE_06_SOP_COMPLIANCE": "Re-assign inspections to the current approved SOP; expedite document review.",
}


def build_subject(health_score_pct: float, sla_pct: float | None = None) -> str:
    sla = config.SLA_THRESHOLD_PERCENT if sla_pct is None else sla_pct
    return (f"[URGENT - Ops Quality Gatekeeper] Daily Data SLA Breach "
            f"({health_score_pct:.1f}% < {sla:.1f}% Target) — Power BI Locked to LKG")


def build_html_body(*, batch_name: str, run_timestamp: str, health_score_pct: float,
                    total_records: int, quarantined_records: int,
                    rule_failure_counts: dict[str, int], lkg_metadata: dict | None,
                    attachment_name: str | None, dispatch_note: str = "") -> str:
    sla = config.SLA_THRESHOLD_PERCENT
    lkg = lkg_metadata or {}
    gap = sla - health_score_pct
    rows = []
    for rule, count in sorted(rule_failure_counts.items(), key=lambda kv: -kv[1]):
        share = (count / quarantined_records * 100) if quarantined_records else 0
        rows.append(
            f"<tr><td style='padding:8px;border:1px solid #e2e8f0;font-family:monospace'>{html.escape(rule)}</td>"
            f"<td style='padding:8px;border:1px solid #e2e8f0'>{html.escape(config.RULE_CATALOG.get(rule, ''))}</td>"
            f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right;font-weight:600'>{count}</td>"
            f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right'>{share:.1f}%</td></tr>"
        )
    steps = "".join(
        f"<li style='margin-bottom:6px'><b>{html.escape(r)}</b> ({c} rows): {html.escape(RULE_REMEDIATION.get(r, ''))}</li>"
        for r, c in sorted(rule_failure_counts.items(), key=lambda kv: -kv[1]) if c > 0
    )

    def kv(label: str, value: str, color: str = "#0f172a") -> str:
        return (f"<tr><td style='padding:8px 12px;border:1px solid #e2e8f0;background:#f8fafc;"
                f"font-weight:600;width:42%'>{label}</td><td style='padding:8px 12px;border:1px solid "
                f"#e2e8f0;color:{color}'>{value}</td></tr>")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(build_subject(health_score_pct))}</title></head>
<body style="margin:0;padding:24px;background:#f1f5f9;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#0f172a">
<table role="presentation" width="100%" style="max-width:760px;margin:auto;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e2e8f0">
<tr><td style="background:#b91c1c;color:#fff;padding:20px 24px">
  <div style="font-size:12px;letter-spacing:1.5px;text-transform:uppercase;opacity:.85">Ops Quality Gatekeeper · Automated Alert</div>
  <div style="font-size:22px;font-weight:700;margin-top:6px">CRITICAL: Daily Data Quality SLA Breach</div>
  <div style="font-size:14px;margin-top:6px">Health score <b>{health_score_pct:.1f}%</b> is <b>{gap:.1f} pts</b> below the <b>{sla:.1f}%</b> SLA target.</div>
</td></tr>
<tr><td style="padding:20px 24px">
  <p style="margin-top:0">To: <b>{html.escape(config.ALERT_RECIPIENT_EMAIL)}</b> — Quality Engineering &amp; BI Operations</p>
  <h3 style="margin:18px 0 8px">1. Batch Validation Summary</h3>
  <table width="100%" style="border-collapse:collapse;font-size:14px">
    {kv("Batch Name", html.escape(batch_name))}
    {kv("Validation Timestamp", html.escape(run_timestamp))}
    {kv("Health Score vs SLA", f"<b>{health_score_pct:.1f}%</b> vs {sla:.1f}% target — <b>BREACH</b>", "#b91c1c")}
    {kv("Total Rows Received", f"{total_records:,}")}
    {kv("Valid Rows", f"{total_records - quarantined_records:,}")}
    {kv("Quarantined Rows", f"<b>{quarantined_records:,}</b>", "#b91c1c")}
    {kv("Quarantine Workbook", html.escape(attachment_name or "n/a") + " (attached)")}
  </table>
  <h3 style="margin:22px 0 8px">2. Rule-by-Rule Failure Breakdown</h3>
  <table width="100%" style="border-collapse:collapse;font-size:13px">
    <tr style="background:#0f172a;color:#fff"><th style="padding:8px;text-align:left">Rule</th>
    <th style="padding:8px;text-align:left">Description</th><th style="padding:8px;text-align:right">Rows</th>
    <th style="padding:8px;text-align:right">Share</th></tr>
    {''.join(rows)}
  </table>
  <div style="margin:22px 0;padding:14px 16px;background:#ecfdf5;border-left:5px solid #059669;border-radius:4px;font-size:14px">
    <b>🔒 Power BI Production Tables remain locked to the Last-Known-Good snapshot.</b><br>
    No data from this batch was promoted to <code>data/production_bi/</code>. Executive dashboards continue to serve
    certified batch <b>{html.escape(str(lkg.get("last_certified_batch", "n/a")))}</b>
    (certified {html.escape(str(lkg.get("last_certified_timestamp", "n/a")))},
    health score {lkg.get("health_score", "n/a")}%, {lkg.get("record_count", "n/a")} fact rows).
    Dashboards are flagged <code>stale_warning = true</code> until a passing batch is certified.
  </div>
  <h3 style="margin:22px 0 8px">3. Remediation Steps for Quality Engineering</h3>
  <ol style="font-size:14px;padding-left:20px">
    {steps}
    <li style="margin-bottom:6px">Correct the source extract, drop the fixed file into <code>data/landing_zone/</code> and re-run the Gatekeeper.</li>
    <li>On SLA PASS the Star-Schema CSVs are auto-promoted and the stale flag is cleared before the next Power BI refresh.</li>
  </ol>
  <p style="font-size:12px;color:#64748b;border-top:1px solid #e2e8f0;padding-top:12px;margin-bottom:0">
    Generated automatically by the Enterprise Operations Quality Pre-BI Gatekeeper at
    {datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}. {html.escape(dispatch_note)}
  </p>
</td></tr></table></body></html>"""


def _build_message(subject: str, html_body: str, attachment_path: Path | None,
                   sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender or "ops-quality-gatekeeper@localhost"
    msg["To"] = config.ALERT_RECIPIENT_EMAIL
    msg["X-Priority"] = "1"
    msg.set_content("This alert requires an HTML-capable email client.\n\n" + subject)
    msg.add_alternative(html_body, subtype="html")
    if attachment_path and Path(attachment_path).exists():
        ctype, _ = mimetypes.guess_type(str(attachment_path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(Path(attachment_path).read_bytes(), maintype=maintype,
                           subtype=subtype, filename=Path(attachment_path).name)
    return msg


def _ssl_context() -> ssl.SSLContext:
    """Return an SSLContext backed by certifi's CA bundle (fixes macOS Python SSL errors)."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _dispatch(msg: EmailMessage) -> tuple[bool, str]:
    """Send over SMTP-SSL if configured. Returns (sent, status_message)."""
    if not config.smtp_configured():
        return False, config.LOCAL_FALLBACK_MESSAGE
    try:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                              context=_ssl_context(), timeout=20) as smtp:
            smtp.login(config.SMTP_SENDER_EMAIL, config.SMTP_APP_PASSWORD)
            smtp.send_message(msg)
        return True, f"LIVE: Email dispatched via {config.SMTP_HOST}:{config.SMTP_PORT} to {config.ALERT_RECIPIENT_EMAIL}"
    except Exception as exc:  # network down, bad app password, etc.
        logger.warning("SMTP dispatch failed: %s", exc)
        return False, (f"SMTP dispatch failed ({type(exc).__name__}: {exc}). "
                       + config.LOCAL_FALLBACK_MESSAGE)


def _write_status(payload: dict) -> None:
    Path(config.QUARANTINE_DIR).mkdir(parents=True, exist_ok=True)
    Path(config.LATEST_EMAIL_STATUS_PATH).write_text(json.dumps(payload, indent=2))


def send_sla_breach_alert(*, batch_name: str, run_timestamp: str, health_score_pct: float,
                          total_records: int, quarantined_records: int,
                          rule_failure_counts: dict[str, int], attachment_path: Path | str | None,
                          lkg_metadata: dict | None = None) -> dict:
    """Generate, persist and (optionally) send the SLA breach alert. Never raises."""
    attachment = Path(attachment_path) if attachment_path else None
    subject = build_subject(health_score_pct)
    try:
        mode_note = ("Dispatched live via Gmail SMTP." if config.smtp_configured()
                     else "Local fallback mode — SMTP credentials not configured.")
        body = build_html_body(
            batch_name=batch_name, run_timestamp=run_timestamp,
            health_score_pct=health_score_pct, total_records=total_records,
            quarantined_records=quarantined_records, rule_failure_counts=rule_failure_counts,
            lkg_metadata=lkg_metadata, attachment_name=attachment.name if attachment else None,
            dispatch_note=mode_note,
        )
        Path(config.QUARANTINE_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.LATEST_EMAIL_HTML_PATH).write_text(body, encoding="utf-8")
        msg = _build_message(subject, body, attachment, config.SMTP_SENDER_EMAIL)
        sent, status = _dispatch(msg)
    except Exception as exc:  # absolute safety net for live demos
        logger.exception("Alert generation failed")
        sent, status = False, f"Alert generation error: {exc}"

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "type": "SLA_BREACH_ALERT",
        "recipient": config.ALERT_RECIPIENT_EMAIL,
        "subject": subject,
        "batch_name": batch_name,
        "health_score_pct": health_score_pct,
        "attachment_path": str(attachment) if attachment else None,
        "html_preview_path": str(config.LATEST_EMAIL_HTML_PATH),
        "smtp_configured": config.smtp_configured(),
        "sent_live": sent,
        "status_message": status,
    }
    _write_status(result)
    print(f"[EMAIL] {status}")
    return result


def send_test_email() -> dict:
    """Interactive connectivity test from the Streamlit Alert Center."""
    config.smtp_configured()
    subject = "[TEST - Ops Quality Gatekeeper] SMTP connectivity check"
    body = (f"<html><body style='font-family:Helvetica,Arial'><h2>✅ SMTP test successful</h2>"
            f"<p>The Ops Quality Gatekeeper can reach <b>{html.escape(config.ALERT_RECIPIENT_EMAIL)}</b>.</p>"
            f"<p>Sent {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}.</p></body></html>")
    try:
        sent, status = _dispatch(_build_message(subject, body, None, config.SMTP_SENDER_EMAIL))
    except Exception as exc:
        sent, status = False, f"Test email error: {exc}"
    return {"recipient": config.ALERT_RECIPIENT_EMAIL, "subject": subject,
            "sent_live": sent, "status_message": status,
            "smtp_configured": config.smtp_configured()}

