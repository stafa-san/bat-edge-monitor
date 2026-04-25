#!/usr/bin/env python3
"""Post-hoc triage of a Permissive Night Mode (PNM) capture window.

Run the morning after a PNM night to review every detection that landed in
``bat_detections`` plus relevant context from ``audio_levels``. Emits a CSV
sorted by likelihood-of-real-bat (best signals first), pre-flagging obvious
false positives so you can focus on the borderline rows.

Usage (inside the batdetect-service container):

    docker compose exec -T batdetect-service \
        python /app/edge/scripts/triage_pnm_night.py \
        --window-hours 14 > pnm_review.csv

The CSV is emitted on stdout; redirect or copy out as needed.

What you'll see:

    likely_real_score  predicted_class  pred_conf  det_prob  segment_top_class  ...

`likely_real_score` is a 0..1 heuristic combining:
  * prediction_confidence (classifier head's call)
  * detection_prob (BatDetect2's call)
  * segment-level mid-band RMS (was there actually energy?)
  * sensible FM-sweep frequency span
  * top BD class is FM (not constant-frequency Rhinolophus)

Higher = more likely a real bat. Use it to sort and review.

Companion to FIELD_DIAGNOSTIC_PROTOCOL.md.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Optional

# Postgres connection — same env as batdetect-service
import psycopg2
from psycopg2.extras import RealDictCursor


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "soundscape"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def fetch_detections(conn, window_hours: int):
    """Pull every bat_detection in the window, joined with the parent
    audio_levels row (same sync_id segment) for context."""
    sql = f"""
        SELECT
          d.id                          AS detection_id,
          d.detection_time              AS detection_time,
          d.species                     AS bd_label,
          d.predicted_class             AS predicted_class,
          d.prediction_confidence       AS pred_conf,
          d.detection_prob              AS det_prob,
          d.start_time                  AS start_time,
          d.end_time                    AS end_time,
          d.duration_ms                 AS duration_ms,
          d.low_freq                    AS low_freq,
          d.high_freq                   AS high_freq,
          d.audio_path                  AS audio_path,
          d.storage_tier                AS storage_tier,
          d.sync_id                     AS sync_id,
          al.rms                        AS seg_rms,
          al.peak                       AS seg_peak,
          al.bat_band_low_rms           AS seg_low_rms,
          al.bat_band_mid_rms           AS seg_mid_rms,
          al.bat_band_high_rms          AS seg_high_rms,
          al.bd_raw_count               AS seg_bd_raw,
          al.bd_max_det_prob            AS seg_bd_max,
          al.bd_top_class               AS seg_top_class
        FROM bat_detections d
        LEFT JOIN audio_levels al
          ON al.recorded_at BETWEEN d.detection_time - INTERVAL '20 seconds'
                                AND d.detection_time + INTERVAL '5 seconds'
        WHERE d.detection_time > NOW() - INTERVAL '{window_hours} hours'
        ORDER BY d.detection_time DESC
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def likely_real_score(row) -> float:
    """0..1 heuristic. Higher = more likely a real bat call.

    Ingredients:
      pred_conf  (0.5 weight) — classifier head agreement
      det_prob   (0.2 weight) — BD raw confidence
      seg_mid_rms (0.15 weight) — was there bat-band energy in the parent segment?
      sweep_quality (0.1 weight) — sensible high_freq > low_freq with decent span
      top_class_penalty (-0.2 if Rhinolophus*) — UK CF mislabel signature
      duration_penalty (-0.3 if duration_ms < 2) — too short to be a real call
    """
    score = 0.0

    pred_conf = row.get("pred_conf") or 0.0
    score += 0.5 * float(pred_conf)

    det_prob = row.get("det_prob") or 0.0
    score += 0.2 * float(det_prob)

    seg_mid = row.get("seg_mid_rms") or 0.0
    # mid-band RMS of 0.005 is "loud bat passing"; 0.001 is silence. Map to 0..1.
    score += 0.15 * min(1.0, float(seg_mid) / 0.005)

    lo = row.get("low_freq") or 0
    hi = row.get("high_freq") or 0
    if hi > lo and (hi - lo) > 5000:
        # Sensible FM sweep span > 5 kHz
        score += 0.1

    top = (row.get("seg_top_class") or "")
    if top.startswith("Rhinolophus"):
        score -= 0.2  # CF call mislabel — likely not a real Ohio bat detection

    dur = row.get("duration_ms") or 0
    if 0 < float(dur) < 2:
        score -= 0.3  # too short for a real bat call

    return max(0.0, min(1.0, score))


def quick_verdict(row, score: float) -> str:
    pred_conf = float(row.get("pred_conf") or 0)
    seg_mid = float(row.get("seg_mid_rms") or 0)
    top = (row.get("seg_top_class") or "")
    if score >= 0.7:
        return "LIKELY REAL"
    if score >= 0.45:
        return "borderline"
    if top.startswith("Rhinolophus"):
        return "FALSE POSITIVE (Rhinolophus relabel)"
    if seg_mid < 0.001:
        return "FALSE POSITIVE (silent segment)"
    if pred_conf < 0.25:
        return "FALSE POSITIVE (low classifier conf)"
    return "FALSE POSITIVE"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-hours", type=int, default=14)
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="Only emit rows with likely_real_score >= this")
    args = ap.parse_args()

    conn = connect()
    rows = fetch_detections(conn, args.window_hours)

    if not rows:
        print(f"No detections in last {args.window_hours} hours.", file=sys.stderr)
        sys.exit(0)

    # Augment + sort
    augmented = []
    for r in rows:
        s = likely_real_score(r)
        if s < args.min_score:
            continue
        r["likely_real_score"] = round(s, 3)
        r["verdict"] = quick_verdict(r, s)
        augmented.append(r)
    augmented.sort(key=lambda r: -r["likely_real_score"])

    # Top-line summary to stderr (so piping to file keeps CSV clean)
    n_total = len(augmented)
    n_likely = sum(1 for r in augmented if r["verdict"] == "LIKELY REAL")
    n_border = sum(1 for r in augmented if r["verdict"] == "borderline")
    n_false = n_total - n_likely - n_border
    print(f"\n=== PNM TRIAGE — {n_total} detections in last {args.window_hours} h ===",
          file=sys.stderr)
    print(f"  LIKELY REAL : {n_likely}", file=sys.stderr)
    print(f"  borderline  : {n_border}", file=sys.stderr)
    print(f"  false-pos   : {n_false}", file=sys.stderr)
    print(f"  (review borderline rows manually; LIKELY REAL ones are your "
          f"strongest field-data candidates)\n", file=sys.stderr)

    # Emit sorted CSV on stdout — friendly column order
    cols = [
        "likely_real_score", "verdict", "detection_time",
        "predicted_class", "pred_conf", "det_prob",
        "low_freq", "high_freq", "duration_ms",
        "seg_mid_rms", "seg_top_class",
        "seg_bd_raw", "seg_bd_max",
        "storage_tier", "audio_path", "sync_id", "detection_id",
    ]
    w = csv.writer(sys.stdout)
    w.writerow(cols)
    for r in augmented:
        row = []
        for c in cols:
            v = r.get(c)
            if hasattr(v, "isoformat"):
                v = v.isoformat()
            row.append(v if v is not None else "")
        w.writerow(row)


if __name__ == "__main__":
    main()
