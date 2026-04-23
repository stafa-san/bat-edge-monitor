"""Nightly summary — aggregate yesterday's deployment activity and email it.

Fires once per day from the main sync loop. Pulls yesterday's
detections, audio levels, validator rejections, environmental readings,
and power/undervoltage events into a compact text report, then sends
it via Gmail SMTP (if credentials are configured) or just logs it.

Configuration env vars:

    ENABLE_DAILY_SUMMARY=true               # feature flag
    DAILY_SUMMARY_HOUR_UTC=11               # send at 11 UTC = 07 EDT
    DAILY_SUMMARY_RECIPIENTS=foo@bar,baz@bar
    GMAIL_USER=sender@gmail.com             # Gmail account to send from
    GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx  # Gmail app password (16 chars)

If GMAIL_USER / GMAIL_APP_PASSWORD are not set, the report is still
generated and printed to stdout so it ends up in container logs —
useful during setup and for people who want the summary without SMTP.

Set up an app password for a Gmail account:
    1. https://myaccount.google.com/security → turn 2FA on
    2. https://myaccount.google.com/apppasswords
    3. Create "Mail" app password, paste the 16-character string
       (keep the spaces — SMTP will take them either way)
"""

from __future__ import annotations

import datetime as _dt
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any


# ---------------------------------------------------------------------------
# Data collection — one query, lots of numbers
# ---------------------------------------------------------------------------

def _fetchone(conn, sql: str, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    return row


def _fetchall(conn, sql: str, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def collect_summary(conn, site_id: str, window_hours: int = 24) -> dict:
    """Return a dict of yesterday-ish aggregates for the summary email."""
    since_sql = f"NOW() - INTERVAL '{window_hours} hours'"

    out: dict[str, Any] = {
        "site_id": site_id,
        "generated_at": _dt.datetime.now(_dt.timezone.utc),
        "window_hours": window_hours,
    }

    # Bat detections breakdown
    total = _fetchone(conn, f"SELECT count(*) FROM bat_detections WHERE detection_time > {since_sql}")
    out["detections_total"] = total[0] if total else 0

    by_class = _fetchall(
        conn,
        f"""
        SELECT COALESCE(predicted_class, species, 'unknown') AS cls, count(*)
        FROM bat_detections
        WHERE detection_time > {since_sql}
        GROUP BY 1 ORDER BY 2 DESC
        """,
    )
    out["detections_by_class"] = [(c, int(n)) for c, n in by_class]

    # Audio levels (p50, p95, max)
    audio = _fetchone(
        conn,
        f"""
        SELECT round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY rms)::numeric, 5),
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY rms)::numeric, 5),
               round(max(peak)::numeric, 4),
               count(*)
        FROM audio_levels WHERE recorded_at > {since_sql}
        """,
    )
    if audio:
        out["audio_rms_p50"] = float(audio[0]) if audio[0] is not None else None
        out["audio_rms_p95"] = float(audio[1]) if audio[1] is not None else None
        out["audio_peak_max"] = float(audio[2]) if audio[2] is not None else None
        out["audio_segments"] = int(audio[3]) if audio[3] is not None else 0

    # BD detector stats
    bd = _fetchone(
        conn,
        f"""
        SELECT round(avg(bd_raw_count)::numeric, 2),
               round(max(bd_max_det_prob)::numeric, 3),
               count(*) FILTER (WHERE bd_user_pass > 0)
        FROM audio_levels WHERE recorded_at > {since_sql}
        """,
    )
    if bd:
        out["bd_raw_avg"] = float(bd[0]) if bd[0] is not None else None
        out["bd_max_det_prob"] = float(bd[1]) if bd[1] is not None else None
        out["bd_segments_passed_threshold"] = int(bd[2]) if bd[2] is not None else 0

    # Validator rejection counts
    rejections = _fetchall(
        conn,
        f"""
        SELECT split_part(rejection_reason, '(', 1) AS reason, count(*)
        FROM audio_levels
        WHERE rejection_reason IS NOT NULL
          AND recorded_at > {since_sql}
        GROUP BY 1 ORDER BY 2 DESC
        """,
    )
    out["rejections"] = [(r, int(n)) for r, n in rejections]

    # Environmental (HOBO) temperature
    env = _fetchone(
        conn,
        f"""
        SELECT round(min(temperature_c)::numeric, 1),
               round(avg(temperature_c)::numeric, 1),
               round(max(temperature_c)::numeric, 1),
               count(DISTINCT sensor_serial)
        FROM environmental_readings WHERE recorded_at > {since_sql}
        """,
    )
    if env:
        out["temp_min"] = float(env[0]) if env[0] is not None else None
        out["temp_avg"] = float(env[1]) if env[1] is not None else None
        out["temp_max"] = float(env[2]) if env[2] is not None else None
        out["temp_sensors"] = int(env[3]) if env[3] is not None else 0

    # Capture errors
    err = _fetchone(
        conn,
        f"SELECT count(*) FROM capture_errors WHERE recorded_at > {since_sql}",
    )
    out["capture_errors"] = int(err[0]) if err else 0

    # Model-health flag — derived. If BatDetect2 saw literally zero
    # raw detections across a full window of audio that wasn't silent,
    # the model is almost certainly in the degenerate state that
    # BATDETECT2_STABILITY_FIX.md describes. Conservative thresholds
    # so genuine quiet nights don't trigger:
    #   - need ≥100 segments in the window (enough data)
    #   - avg raw_count must be 0 (not just low — zero)
    #   - p50 audio RMS above the validator's noise floor (mic was
    #     picking up something louder than ambient)
    out["model_health_alert"] = (
        out.get("audio_segments", 0) >= 100
        and (out.get("bd_raw_avg") or 0) == 0
        and (out.get("audio_rms_p50") or 0) >= 0.005
    )

    return out


# ---------------------------------------------------------------------------
# Formatting — plain text + simple HTML
# ---------------------------------------------------------------------------

def _format_text(s: dict) -> str:
    lines = []
    lines.append(f"Bat Edge Monitor — daily summary ({s['site_id']})")
    lines.append(f"Window: last {s['window_hours']} h, generated "
                 f"{s['generated_at'].strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    lines.append(f"Bat detections: {s.get('detections_total', 0)}")
    for cls, n in s.get("detections_by_class", [])[:10]:
        lines.append(f"  {cls:<14s}  {n}")
    lines.append("")

    lines.append(f"Audio segments processed: {s.get('audio_segments', 0)}")
    if s.get("audio_rms_p50") is not None:
        lines.append(f"  RMS p50       : {s['audio_rms_p50']:.5f}")
        lines.append(f"  RMS p95       : {s['audio_rms_p95']:.5f}")
        lines.append(f"  Peak max      : {s.get('audio_peak_max', 0):.4f}")
    lines.append("")

    lines.append("BatDetect2 activity:")
    if s.get("bd_raw_avg") is not None:
        lines.append(f"  avg raw emissions / segment : {s['bd_raw_avg']:.2f}")
    if s.get("bd_max_det_prob") is not None:
        lines.append(f"  max det_prob observed       : {s['bd_max_det_prob']:.3f}")
    lines.append(f"  segments that passed user threshold : "
                 f"{s.get('bd_segments_passed_threshold', 0)}")
    lines.append("")

    if s.get("rejections"):
        lines.append("Validator rejections:")
        for reason, n in s["rejections"]:
            lines.append(f"  {reason:<28s} {n}")
        lines.append("")

    if s.get("temp_min") is not None:
        lines.append(f"Temperature ({s.get('temp_sensors', 0)} sensor(s)):")
        lines.append(f"  min / avg / max  : {s['temp_min']:.1f} / "
                     f"{s['temp_avg']:.1f} / {s['temp_max']:.1f} °C")
        lines.append("")

    if s.get("capture_errors"):
        lines.append(f"⚠ Capture errors in window: {s['capture_errors']}")
        lines.append("")

    if s.get("model_health_alert"):
        lines.append(
            "⚠ MODEL HEALTH ALERT: BatDetect2 returned 0 raw detections on "
            f"{s.get('audio_segments', 0)} segments despite audio RMS above "
            "the noise floor. Detector is likely in the degenerate state "
            "from BATDETECT2_STABILITY_FIX.md. Recommended: "
            "`docker compose restart batdetect-service` to force a warm-up "
            "reload and check the next morning's summary."
        )
        lines.append("")

    lines.append("—")
    lines.append("See dashboard and HARDWARE_TROUBLESHOOTING.md for context.")
    return "\n".join(lines)


def _format_html(s: dict) -> str:
    det = s.get("detections_total", 0)
    banner_color = "#16a34a" if det > 0 else "#6b7280"

    def _row(label: str, value: str) -> str:
        return (f'<tr><td style="padding:4px 10px 4px 0;color:#6b7280;">{label}</td>'
                f'<td style="padding:4px 0;font-weight:600;">{value}</td></tr>')

    rows = []
    rows.append(_row("Detections (total)", str(det)))
    cls_txt = ", ".join(f"{c} ({n})" for c, n in s.get("detections_by_class", [])[:5]) or "—"
    rows.append(_row("Top classes", cls_txt))
    if s.get("audio_rms_p50") is not None:
        rows.append(_row("Audio RMS p50 / p95",
                         f"{s['audio_rms_p50']:.4f} / {s['audio_rms_p95']:.4f}"))
    if s.get("bd_max_det_prob") is not None:
        rows.append(_row("Max BD det_prob", f"{s['bd_max_det_prob']:.3f}"))
    rows.append(_row("BD segments ≥ user threshold",
                     str(s.get("bd_segments_passed_threshold", 0))))
    if s.get("rejections"):
        rej_txt = ", ".join(f"{r} ({n})" for r, n in s["rejections"][:5])
        rows.append(_row("Validator rejections", rej_txt))
    if s.get("temp_min") is not None:
        rows.append(_row("Temperature min / max",
                         f"{s['temp_min']:.1f} / {s['temp_max']:.1f} °C"))
    if s.get("capture_errors"):
        rows.append(_row("Capture errors",
                         f'<span style="color:#dc2626;">{s["capture_errors"]}</span>'))
    if s.get("model_health_alert"):
        rows.append(_row(
            "Model health",
            '<span style="color:#dc2626;font-weight:700;">'
            "⚠ ALERT — 0 raw detections, non-silent audio. "
            "See BATDETECT2_STABILITY_FIX.md</span>",
        ))

    return (
        '<html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:#111;">'
        f'<div style="border-left:4px solid {banner_color};padding-left:12px;">'
        f'<h2 style="margin:0 0 4px 0;">🦇 Daily summary — {s["site_id"]}</h2>'
        f'<div style="color:#6b7280;font-size:13px;">'
        f'Window: last {s["window_hours"]} h · '
        f'{s["generated_at"].strftime("%Y-%m-%d %H:%M UTC")}</div>'
        '</div>'
        '<table style="margin-top:12px;font-size:14px;">' + "".join(rows) + '</table>'
        '<p style="color:#6b7280;font-size:12px;margin-top:16px;">'
        'Automated by bat-edge-monitor. Dashboard: '
        '<a href="https://bat-edge-monitor-dashboard.vercel.app/">open</a>'
        '</p></body></html>'
    )


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_summary(conn, site_id: str) -> dict:
    """Build + send (or log) the daily summary. Returns a status dict."""
    window_h = int(os.getenv("DAILY_SUMMARY_WINDOW_HOURS", "24"))
    data = collect_summary(conn, site_id, window_hours=window_h)
    text = _format_text(data)
    html = _format_html(data)

    gmail_user = os.getenv("GMAIL_USER")
    gmail_pw = os.getenv("GMAIL_APP_PASSWORD")
    recipients = [
        r.strip() for r in os.getenv("DAILY_SUMMARY_RECIPIENTS", "").split(",")
        if r.strip()
    ]

    # Always log the text version so operators see it in container logs.
    print("[SUMMARY] --- daily report ---")
    for line in text.splitlines():
        print(f"[SUMMARY] {line}")

    if not gmail_user or not gmail_pw:
        print("[SUMMARY] GMAIL_USER/GMAIL_APP_PASSWORD not set — skipping email send")
        return {"sent": False, "reason": "smtp_not_configured"}

    if not recipients:
        print("[SUMMARY] DAILY_SUMMARY_RECIPIENTS empty — skipping email send")
        return {"sent": False, "reason": "no_recipients"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🦇 {site_id} daily summary — {data.get('detections_total', 0)} detections"
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(gmail_user, gmail_pw.replace(" ", ""))
            s.sendmail(gmail_user, recipients, msg.as_string())
        print(f"[SUMMARY] sent to {len(recipients)} recipient(s)")
        return {"sent": True, "recipients": recipients}
    except Exception as e:
        print(f"[SUMMARY] send failed: {type(e).__name__}: {e}")
        return {"sent": False, "reason": f"{type(e).__name__}"}
