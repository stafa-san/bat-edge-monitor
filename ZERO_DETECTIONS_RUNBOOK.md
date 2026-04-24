# Runbook — "Zero Detections Overnight"

Use this when you wake up, check the dashboard, and see an empty
`bat_detections` table / "0 bats found" for last night. Follow the decision
tree in order — each step narrows the failure mode further.

**Keep this doc in sync with:**
- [`AUDIO_CAPTURE_AUDIT.md`](AUDIO_CAPTURE_AUDIT.md) — what each pipeline stage does
- [`PIPELINE_AUDIT_AND_FIXES.md`](PIPELINE_AUDIT_AND_FIXES.md) — gate tuning history
- [`BATDETECT2_STABILITY_FIX.md`](BATDETECT2_STABILITY_FIX.md) — cold-start failure mode
- [`AUDIO_VALIDATOR.md`](AUDIO_VALIDATOR.md) — validator-gate math

---

## Step 0 — Is the Pi even alive?

```bash
ssh stafa@100.112.237.70 'uptime; docker compose -f ~/bat-edge-monitor/edge/docker-compose.yml ps'
```

- If SSH times out → Tailscale / Pi power issue. Physical check required.
- If containers are not `running` → skip to **Step 4 (containers)**.
- If everything running → continue.

---

## Step 1 — Did audio_levels rows actually get written overnight?

```sql
-- On the Pi:
docker compose exec -T db psql -U postgres -d soundscape -c "
SELECT
  count(*)                                     AS total_segments,
  min(recorded_at)                             AS first_row,
  max(recorded_at)                             AS last_row,
  round(extract(epoch from max(recorded_at) - min(recorded_at))/60.0)::int AS minutes_span
FROM audio_levels
WHERE recorded_at > NOW() - INTERVAL '14 hours';
"
```

### Decision

| Observation | Diagnosis | Next |
|---|---|---|
| `total_segments = 0` | Capture never fired. | **Step 4** (container / capture pipeline) |
| `total_segments < 100` | Brief/partial run; service crashing or flapping. | **Step 4** (check for crash loop in logs) |
| `total_segments ≈ 2000–3000`, healthy span | Capture ran all night. | **Step 2** |
| `last_row` is hours before now, older than expected | Capture stopped partway. | **Step 4** (USB disconnect? SD card full?) |

Expected `total_segments` for a ~10-hour night at 15 s cadence ≈ 2400.

---

## Step 2 — Did ambient audio reach the bat band?

```sql
docker compose exec -T db psql -U postgres -d soundscape -c "
SELECT
  round(percentile_cont(0.50) WITHIN GROUP (ORDER BY rms)::numeric, 5)                AS full_p50,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY rms)::numeric, 5)                AS full_p95,
  round(percentile_cont(0.50) WITHIN GROUP (ORDER BY bat_band_low_rms)::numeric, 6)   AS low_p50,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY bat_band_low_rms)::numeric, 6)   AS low_p95,
  round(percentile_cont(0.50) WITHIN GROUP (ORDER BY bat_band_mid_rms)::numeric, 6)   AS mid_p50,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY bat_band_mid_rms)::numeric, 6)   AS mid_p95,
  round(percentile_cont(0.50) WITHIN GROUP (ORDER BY bat_band_high_rms)::numeric, 6)  AS high_p50,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY bat_band_high_rms)::numeric, 6)  AS high_p95
FROM audio_levels WHERE recorded_at > NOW() - INTERVAL '14 hours';
"
```

### Decision

| Observation | Diagnosis | Next |
|---|---|---|
| All `*_p95` columns are near-zero (< 1e-5) | Mic captured silence all night. **Either**: no bats flew (cold/rainy night — confirm with `environmental_readings`), **or** mic failed. | **Step 3** |
| `low_p95 > 1e-4` but `bd_raw_avg = 0` (from **Step 5**) | Bat-band energy present but detector missed it. | **Step 5** (BatDetect2 side) |
| `high_p95` is 5–10× above `low_p95` with no detections | Could be insect chorus (cicadas 20–25 kHz) or bats too high-freq for detector. | **Step 5** |
| `full_p95 > 0.01` but bat-band all near zero | Audible noise but no ultrasound (motor, voice). Mic may be in wrong mode / bad connection. | **Step 3** |

**Reference floors from 2026-04-23 indoor-quiet measurement**: `full_p50 ≈ 0.0025`, each band `p50 < 1e-5` (silence). Real bat-active nights should push `low_p95` and `mid_p95` into the 1e-4 to 1e-3 range.

---

## Step 3 — Is the AudioMoth still enumerated?

```bash
ssh stafa@100.112.237.70 'lsusb | grep -i audiomoth; arecord -l | grep -i audiomoth; dmesg --since="14 hours ago" | grep -iE "AudioMoth|disconnect|reset|overrun|xrun" | tail -20'
```

### Decision

| Observation | Diagnosis | Fix |
|---|---|---|
| Neither lsusb nor arecord shows the AudioMoth | USB disconnected or device failure. | Physical check — reseat USB cable. If still absent, try a different USB port. |
| AudioMoth present but `dmesg` shows repeated `disconnect`/`reset` | USB cable is flaky or power-limited. | Replace USB cable (use a shielded short one). |
| AudioMoth present, no errors in dmesg | Enumeration is fine — capture chain is healthy; problem is upstream or in SW. | **Step 4** |

---

## Step 4 — Is batdetect-service capturing cleanly?

```bash
ssh stafa@100.112.237.70 'cd ~/bat-edge-monitor/edge && \
  docker compose ps batdetect-service; echo "---"; \
  docker compose logs --since 14h batdetect-service | grep -cE "MODEL-HEALTH|warm-up|Error|rejected|detected"; echo "---"; \
  docker compose logs --tail 50 batdetect-service | tail -40'
```

### Decision

| Observation | Diagnosis | Fix |
|---|---|---|
| Container in `restarting` loop | Warm-up failing, or arecord failing. See logs. | Check warm-up log for "0 detections on synthetic chirp" — if so, torch state is broken. `docker compose down && docker compose up -d batdetect-service`. |
| Logs show `MODEL-HEALTH WARNING` | Detector went degenerate mid-night. | `docker compose restart batdetect-service` to force warm-up reload. |
| Logs show `No devices found matching AudioMoth` | USB enumeration racy at boot. | `docker compose restart batdetect-service` — this was the original fix. |
| Logs show normal `#10, #20 | No bat calls detected` heartbeat | Capture was fine. | **Step 5** (model side) |
| Logs show many `rejected by validator:*` or `shape:*` | Gates might be killing real calls. | **Step 6** |

---

## Step 5 — Is BatDetect2 returning anything at all?

```sql
docker compose exec -T db psql -U postgres -d soundscape -c "
SELECT
  count(*)                                                      AS segments,
  count(*) FILTER (WHERE bd_raw_count > 0)                      AS with_any_raw,
  count(*) FILTER (WHERE bd_user_pass > 0)                      AS passed_threshold,
  round(avg(bd_raw_count)::numeric, 2)                          AS avg_raw,
  round(max(bd_max_det_prob)::numeric, 3)                       AS max_prob
FROM audio_levels WHERE recorded_at > NOW() - INTERVAL '14 hours';
"
echo ""
docker compose exec -T db psql -U postgres -d soundscape -c "
SELECT bd_top_class, count(*)
FROM audio_levels
WHERE recorded_at > NOW() - INTERVAL '14 hours'
  AND bd_top_class IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
"
```

### Decision

| Observation | Diagnosis | Fix |
|---|---|---|
| `with_any_raw = 0` everywhere | Model is OUT (degenerate state, or truly no bat audio). Cross-check with Step 2 — if band energy present, model's forward pass is failing. | `docker compose restart batdetect-service`. If still zero after restart + 30 min, run the golden-file test (§ bottom). |
| `with_any_raw > 0` but `passed_threshold = 0` + `max_prob < 0.3` | Detector is seeing weak sub-threshold signal. Could be distant bats + noise. | Consider lowering `DETECTION_THRESHOLD` from 0.3 to 0.2 for a night. Will increase false-positives — compensated by gates. |
| `passed_threshold > 0` but zero detections surviving to `bat_detections` | Downstream gates killed every call. | **Step 6** |
| Top classes are all European species (Pipistrellus, Nyctalus, Eptesicus) | UK-backbone OOD problem; see `RETRAINED_NA_DETECTOR_PLAN.md` for the long-term fix. Doesn't block detection — classifier head re-maps to NA — but low raw confidence means `user_pass` often 0. | Use the spot-check workflow — feed a known bat WAV through `test_pipeline_golden.py` and confirm detector is working. |

---

## Step 6 — Which gate is rejecting?

```sql
docker compose exec -T db psql -U postgres -d soundscape -c "
SELECT split_part(rejection_reason, '(', 1) AS reason, count(*)
FROM audio_levels
WHERE rejection_reason IS NOT NULL
  AND recorded_at > NOW() - INTERVAL '14 hours'
GROUP BY 1 ORDER BY 2 DESC;
"
```

| Rejection reason | Gate | Relax by |
|---|---|---|
| `batdetect2_no_detections` | BatDetect2 returned zero. | Step 5. |
| `all_below_user_threshold` | Detector saw emissions but none ≥ 0.3. | Lower `DETECTION_THRESHOLD` if you trust the shape filter to catch clicks. |
| `all_below_min_pred_conf` | Classifier never got ≥ 0.3 confident. | Inspect `/bat_audio/_diagnostic/` — if WAVs look like real bats, lower further to 0.25 or retrain classifier head on more Ohio data. |
| `shape:chaotic_peaks(r2=X)` | FM-sweep filter rejected — the call didn't look like a linear downward chirp. | If diagnostic WAVs clearly ARE bat calls: lower `FM_SWEEP_MIN_R2` from 0.2 to 0.1, or `FM_SWEEP_MAX_LOW_BAND_RATIO` from 0.5 to 0.7. |
| `shape:*_not_downward_sweep` | Call was upward or flat. | Legit rejection unless the "call" is actually from a species using CF (constant frequency) calls — not present in NA bat species we target. |
| `validator:rms_too_low` | Whole-segment RMS below 0.002. | Already at the floor. Consider running validator on detection window instead of whole segment (future work, see PIPELINE_AUDIT_AND_FIXES.md §5). |
| `validator:snr_too_low` | Bat-band SNR < 10 dB. | If diagnostic WAVs clearly contain bats at low SNR, drop `VALIDATOR_MIN_SNR_DB` to 8. |
| `validator:no_burst` | Audio is too steady-state. | If diagnostic WAVs clearly contain bats, drop `VALIDATOR_MIN_BURST_RATIO` to 2.0. |

---

## Step 7 — Look at what was saved

Diagnostic-save is on, so every near-miss rejection is a WAV on disk:

```bash
ssh stafa@100.112.237.70 'ls -lhrt ~/bat-edge-monitor/edge/bat_audio/_diagnostic/ | tail -20'
# Or inside the container:
ssh stafa@100.112.237.70 'docker compose -f ~/bat-edge-monitor/edge/docker-compose.yml \
  exec -T batdetect-service ls -lhrt /bat_audio/_diagnostic/ | tail -20'
```

Files are named with the rejection reason: `pi01_<timestamp>__BDpass_<reason>.wav`.
Upload a couple into the dashboard's Offline WAV Analysis panel — if they
detect bats on reanalysis, that confirms real-bat false-negatives at the
live edge gates.

---

## Step 8 — Environmental sanity check

Bats hunt insects. Cold / windy / rainy nights = no bats, not a pipeline
failure. HOBO sensor temperatures are in the same DB:

```sql
docker compose exec -T db psql -U postgres -d soundscape -c "
SELECT
  round(min(temperature_c)::numeric, 1) AS min_c,
  round(avg(temperature_c)::numeric, 1) AS avg_c,
  round(max(temperature_c)::numeric, 1) AS max_c,
  count(DISTINCT sensor_serial)         AS sensors
FROM environmental_readings
WHERE recorded_at > NOW() - INTERVAL '14 hours';
"
```

Rule of thumb for Ohio:
- Avg temp < 10 °C → bats mostly don't fly. Genuine zero is expected.
- Avg temp 10–15 °C → some species active (LACI tolerates cold).
- Avg temp > 15 °C → full assemblage active; zero detections is a pipeline issue.

Also worth checking: was it raining? (No humidity logger currently, but a
quick look at the weather for the deploy site at dusk/evening tells you.)

---

## Step 9 — The ultimate sanity check: run the golden-file test

If you got this far and nothing's conclusive, prove the pipeline still
works on known-good audio:

```bash
# copy a known-bat WAV from Dr. Johnson's corpus to the Pi, then:
ssh stafa@100.112.237.70 '
  docker compose -f ~/bat-edge-monitor/edge/docker-compose.yml \
    cp /path/to/known_bat.wav batdetect-service:/tmp/gold.wav
  docker compose -f ~/bat-edge-monitor/edge/docker-compose.yml \
    exec -T batdetect-service python /app/edge/scripts/test_pipeline_golden.py \
      --wav /tmp/gold.wav --min-raw-detections 20
'
```

- `PASS` → pipeline is healthy; last night was genuinely quiet.
- `FAIL: raw_count below minimum` → model is in a degenerate state. Do
  `docker compose restart batdetect-service`, re-test. If still FAIL,
  see `BATDETECT2_STABILITY_FIX.md`.

---

## Quick-ref: one-liner summary query

All the diagnostics above in one shot:

```sql
docker compose exec -T db psql -U postgres -d soundscape <<'SQL'
SELECT
  count(*)                                                                    AS total_segments,
  count(*) FILTER (WHERE bd_raw_count > 0)                                    AS segs_with_raw,
  count(*) FILTER (WHERE bd_user_pass > 0)                                    AS segs_passed,
  round(avg(bd_raw_count)::numeric, 2)                                        AS raw_avg,
  round(max(bd_max_det_prob)::numeric, 3)                                     AS max_prob,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY rms)::numeric, 4)        AS full_rms_p95,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY bat_band_mid_rms)::numeric, 6) AS mid_band_p95,
  (SELECT count(*) FROM bat_detections WHERE detection_time > NOW() - INTERVAL '14 hours') AS kept_detections
FROM audio_levels WHERE recorded_at > NOW() - INTERVAL '14 hours';
SQL
```

If you see `total_segments ≈ 2400`, `segs_with_raw > 100`, `mid_band_p95 > 1e-4`, and `kept_detections = 0` — that's the interesting case, go to Steps 5/6. Everything else has a clearer diagnosis from that one row.
