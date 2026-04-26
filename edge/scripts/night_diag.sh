#!/usr/bin/env bash
# One-shot diagnostic snapshot — answers "what happened tonight?" even
# when bat_detections row count is zero. Designed to be run by a human
# OR a scheduled remote agent.
#
# Usage:
#   ssh stafa@<pi> bash ~/bat-edge-monitor/edge/scripts/night_diag.sh
#
# Output sections, in order of "skim from top":
#   1. Pi liveness + uptime
#   2. sync-service watchdog state (did it restart? last tick age?)
#   3. bat_detections totals (by species + raw count)
#   4. /bat_audio/tier1_permanent/ — accepted segments per class
#   5. /bat_audio/_diagnostic/ — rejected segments by reason histogram
#   6. Last 30 [BAT] log lines (raw events + per-segment verdict)
#   7. audio_levels — last 10 per-band RMS rows (mic alive check)
#   8. Active gates from .env

set -u  # fail on uninitialized vars; do NOT set -e (we want every section to run even if one fails)

cd "$(dirname "$0")/.."
COMPOSE=(docker compose)

ROW () {
  printf '\n=== %s ===\n' "$1"
}


ROW "PI LIVENESS"
date -u +"now (UTC):    %F %T"
date    +"now (local):  %F %T %Z"
uptime  | sed 's/^ //'


ROW "SYNC-SERVICE WATCHDOG STATE"
# Check if the watchdog has fired at least once (would be in older logs)
# and find the most recent "Cycle N" line — its timestamp tells us
# whether the worker is actively ticking.
echo "container status:"
"${COMPOSE[@]}" ps sync-service --format 'table {{.Name}}\t{{.Status}}'  | tail -1
echo
echo "watchdog log markers (any restarts? last 5):"
"${COMPOSE[@]}" logs --tail 1000 sync-service 2>&1 | grep -E 'Watchdog (armed|.*stalled)|exited|restart' | tail -5 \
  || echo '  (no watchdog markers in last 1000 lines — fine if no recent restart)'
echo
echo "most recent sync cycle log line:"
"${COMPOSE[@]}" logs --tail 200 sync-service 2>&1 | grep -E '\[SYNC\] Cycle' | tail -1 \
  || echo '  (no Cycle line in last 200 — sync-service may be stuck or just started)'


ROW "BAT_DETECTIONS TOTALS"
"${COMPOSE[@]}" exec -T db psql -U postgres -d soundscape -c "
SELECT predicted_class,
       COUNT(*) AS rows,
       ROUND(AVG(prediction_confidence)::numeric, 2) AS avg_conf,
       MIN(detection_time) AS earliest,
       MAX(detection_time) AS latest
FROM bat_detections
GROUP BY predicted_class
ORDER BY rows DESC;" 2>&1 | sed 's/^/  /'

"${COMPOSE[@]}" exec -T db psql -U postgres -d soundscape -t -c "
SELECT 'TOTAL: ' || COUNT(*) FROM bat_detections;" 2>&1 | sed 's/^[[:space:]]*//' | grep -v '^$'


ROW "TIER1_PERMANENT (accepted segments)"
"${COMPOSE[@]}" exec -T batdetect-service bash -c '
  for sp in EPFU_LANO LABO LACI MYSP PESU; do
    n=$(ls /bat_audio/tier1_permanent/$sp/ 2>/dev/null | wc -l)
    printf "  %-12s %4d files\n" "$sp" "$n"
  done
  total=$(find /bat_audio/tier1_permanent -name "*.wav" 2>/dev/null | wc -l)
  printf "  %-12s %4d files\n" "TOTAL" "$total"
'


ROW "_DIAGNOSTIC (rejected segments — what FM_SWEEP / etc. threw away)"
"${COMPOSE[@]}" exec -T batdetect-service bash -c '
  total=$(ls /bat_audio/_diagnostic/ 2>/dev/null | wc -l)
  echo "  total rejected segments: $total"
  if [ "$total" -gt 0 ]; then
    echo
    echo "  by rejection reason (top 15):"
    ls /bat_audio/_diagnostic/ 2>/dev/null \
      | sed -E "s/.*__//; s/\.wav$//; s/_r2.*//; s/-r2.*//; s/_[0-9]p[0-9]+x.*//" \
      | sort | uniq -c | sort -rn | head -15 \
      | sed "s/^/    /"
  fi
'


ROW "LAST 30 [BAT] LOG LINES (raw events + verdict)"
"${COMPOSE[@]}" logs --tail 200 batdetect-service 2>&1 \
  | grep -E '^\[BAT\]|^batdetect-service-1.*\[BAT\]' \
  | tail -30 \
  | sed 's/^/  /'


ROW "AUDIO_LEVELS (mic alive + BD activity per segment, last 12 rows)"
"${COMPOSE[@]}" exec -T db psql -U postgres -d soundscape -c "
SELECT to_char(recorded_at, 'HH24:MI:SS') AS t,
       ROUND(rms::numeric, 4) AS rms,
       ROUND(bat_band_mid_rms::numeric, 4) AS bat_mid,
       bd_raw_count AS raw,
       ROUND(bd_max_det_prob::numeric, 2) AS max_p,
       bd_user_pass AS pass,
       rejection_reason AS rejected,
       LEFT(bd_top_class, 22) AS top
FROM audio_levels
ORDER BY recorded_at DESC
LIMIT 12;" 2>&1 | sed 's/^/  /'

ROW "REJECTION COUNTS (all-time, audio_levels.rejection_reason)"
"${COMPOSE[@]}" exec -T db psql -U postgres -d soundscape -c "
SELECT COALESCE(rejection_reason, '<accepted>') AS reason,
       COUNT(*)
FROM audio_levels
WHERE recorded_at > NOW() - INTERVAL '1 hour'
GROUP BY 1
ORDER BY 2 DESC;" 2>&1 | sed 's/^/  /'

ROW "BD ACTIVITY HEALTH (last 60 min)"
"${COMPOSE[@]}" exec -T db psql -U postgres -d soundscape -c "
SELECT
  COUNT(*)                                      AS segments,
  ROUND(AVG(rms)::numeric, 4)                   AS avg_rms,
  ROUND(AVG(bat_band_mid_rms)::numeric, 4)      AS avg_bat_rms,
  SUM(bd_raw_count)                             AS total_raw_bd_events,
  ROUND(AVG(bd_max_det_prob)::numeric, 3)       AS avg_max_p,
  SUM(bd_user_pass)                             AS total_passing_user_thr,
  COUNT(*) FILTER (WHERE rejection_reason IS NULL) AS accepted_segments
FROM audio_levels
WHERE recorded_at > NOW() - INTERVAL '1 hour';" 2>&1 | sed 's/^/  /'


ROW "ACTIVE GATES (.env values)"
grep -E '^(DETECTION_THRESHOLD|VALIDATOR|FM_SWEEP|MIN_PREDICTION_CONF|UPLOAD_BAT_AUDIO|ENABLE_)' .env \
  | sed 's/^/  /'


ROW "INTERPRETATION HINTS"
cat <<'HINT' | sed 's/^/  /'
* tier1 == 0 + diagnostic >> 0  → gates too tight; check rejection histogram.
* tier1 == 0 + diagnostic == 0  → BD never fired; mic dead, raise gain, or no bats.
* bat_band_rms consistently <0.001 → mic dead or wrong device.
* bat_band_rms healthy + tier1 == 0 → real signal, gates rejecting it.
* sync-service container restart count > 0 → watchdog fired; investigate logs.
* "[BAT] #N | 0 bat call(s)" repeating → BD threshold too high for current activity.
HINT
