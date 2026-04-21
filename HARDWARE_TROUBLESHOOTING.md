# Hardware Troubleshooting Playbook

For when the pipeline looks healthy (services green, AudioMoth "Capturing",
no errors) but **detections stay at zero**. This document captures the
full diagnostic arc from 2026-04-20 evening → 2026-04-21 late afternoon,
when the deployed Pi produced 0 bat detections for 18+ hours despite
every software dashboard being green.

Use this as a reference playbook when:

- Standing up a **new Pi / AudioMoth rig** (this covers the power and
  hardware-config choices you need to get right before software matters).
- Diagnosing a **running rig that stopped detecting**.
- Explaining to an advisor or collaborator **why software isn't always
  the culprit**.

Related docs: [`BATDETECT2_TRAINING.md`](BATDETECT2_TRAINING.md) for the
classifier, [`AUDIO_VALIDATOR.md`](AUDIO_VALIDATOR.md) for the third
detection gate, [`DETECTION_TUNING_PLAYBOOK.md`](DETECTION_TUNING_PLAYBOOK.md)
for classifier-threshold tuning, [`SESSION_NOTES_2026-04-20.md`](SESSION_NOTES_2026-04-20.md)
for the earlier day of changes that preceded this one.

---

## TL;DR

- A bat-monitoring pipeline with 0 detections has three possible failure
  modes: (1) no bats present, (2) something wrong with the detector/model,
  (3) something wrong with the **hardware upstream of the detector**.
- The dashboard is unreliable at distinguishing these. All three produce
  identical "service up, no rows, no errors."
- This session found two stacked hardware problems: **Pi 5 undervoltage**
  (wrong power bank) and **AudioMoth gain too low** (advisor's CUSTOM
  mode config hadn't fully applied).
- After both were fixed, the mic jumped from RMS ≈ 0.001 (silent) to
  RMS ≈ 0.006 (normal outdoor ambient) and the pipeline became usable.

---

## Symptom → root-cause reference table

| Symptom | Likely causes | First check |
| --- | --- | --- |
| All dashboard cards green, 0 detections, `audio_levels.rms < 0.002` | Mic silent: undervolt, gain, obstruction, dead mic | Run diagnostic capture (section 4.1) + verify 5 V rail |
| Dashboard shows **Undervoltage NOW** banner | Power supply can't deliver 5.1 V @ 5 A | Swap PSU (section 5.1) |
| Dashboard shows **Undervoltage since boot**, no banner now | Transient voltage sag — usually a marginal PSU under burst load | Swap PSU if it recurs |
| 5 V rail reads **< 4.8 V** | PSU or cable problem | Swap cable first, then PSU |
| Pi rebooted unexpectedly | PSU browned out hard enough to drop the rail below reset threshold | Swap PSU — don't ignore this |
| RMS jumps briefly (tap test) then settles back to 0.001 | Gain config not persisted on the AudioMoth | Unplug AudioMoth 10 s, replug, recheck |
| RMS flat at mic-noise-floor even with noise | Mic element dead, port obstructed, or wrong gain setting | Sequential checks in section 5.2 |
| Detections resume then stop again | Weather (< 10 °C), battery drain, intermittent cable | Cross-check HOBO temperature + power log |

---

## The incident, in order

A chronicle so future-you recognizes the pattern.

**2026-04-20 ~23:30** (previous session end): threshold tightening to
`DETECTION_THRESHOLD=0.5`, audio validator added, archive wiped.
Expected volume was stated as "1–5 detections/day." Dashboard looked
green.

**2026-04-21 ~11:00** (18 hrs later): user reports **0 detections ever
since the reset**. Dashboard still green. Services all up.

**~11:15** — first diagnostic sweep found:

- `docker ps`: batdetect-service up, no errors
- Logs: 530+ segments processed, every one "No bat calls detected"
- BatDetect2 emission count over 4 hrs: **0** at any threshold visible
  in logs
- AudioMoth: still registered at 384 kHz via `/proc/asound/card2/stream0`
- HOBO: site temp at 6 °C (below bat activity threshold ~10 °C)

**First hypothesis:** cold weather was the primary cause. User deferred.

**~12:40** (next day) — still 0. Dropped back in. Second sweep found:

- `dmesg`: **55 "Undervoltage detected!" events** this boot (~every
  1–2 min)
- `vcgencmd get_throttled`: `0x50000` (undervoltage + throttling
  occurred since boot)
- `vcgencmd pmic_read_adc EXT5V_V`: **4.76 V** (below Pi 5 spec of 5.1 V)
- Pi rebooted at 11:13 EDT unprompted — brownout reset
- A diagnostic 15 s capture (stop batdetect, `arecord` on host) showed
  RMS **0.000952** — the mic noise floor, no real signal

**First root cause:** Pi 5 undervoltage, caused by a power bank rated
5 V @ 3 A max. Pi 5 pulls up to 5 A under burst load. Power bank sagged,
Pi throttled USB, AudioMoth preamp starved.

**Remediation (power)**: PSU swap (advisor did this on-site). Result:
`throttled = 0x0`, `EXT5V_V = 5.01 V`, zero undervoltage events this
boot.

**~14:10** — verified post-PSU: power was clean **but audio RMS was
still 0.001**. Ambient mic output unchanged despite the PSU fix. Meant
undervoltage was a necessary-but-not-sufficient problem.

**Second hypothesis (ranked):** 50 % AudioMoth gain config not applied,
30 % physical obstruction, 15 % dead mic, 5 % cable.

**~21:45** — built a **live-stream watcher** (see section 4.2) to
observe `audio_levels` as the advisor interacted with the device.

**~21:54–22:31**: baseline still 0.00104, peak 0.006–0.018. Advisor
was reportedly "making noise near the mic" but the signal wasn't
moving. A few brief peak spikes to 0.012–0.018 suggested the mic was
*responding to impulses* but the baseline / sustained sensitivity was
stuck.

**~22:42** — breakthrough: RMS jumped 5–10× and peaks jumped 30–130×.

```
BEFORE (21:54 – 22:31):  rms ≈ 0.00104   peak ≈ 0.006–0.018
AFTER  (22:42 – 22:55):  rms ≈ 0.006     peak ≈ 0.15–0.82
```

The advisor had done **something** in that gap (they said gain change,
which was likely accompanied by a power-cycle of the AudioMoth). The
working theory: the AudioMoth USB Microphone firmware in CUSTOM mode
doesn't always re-apply its saved config until the device is fully
power-cycled. The **first** gain bump didn't take; the **second pass
with a disconnect** did.

**~22:56** — verified quiet-state baseline after advisor stopped
interacting: `rms ≈ 0.006`, `peak ≈ 0.16`, consistent with normal
outdoor ambient at a site with an 8 kHz hardware HPF.

Pipeline unblocked. Ready for real detections.

---

## Diagnostic methodology

Always do these in order. Each step narrows the failure mode; skipping
earlier steps will waste time on symptoms.

### Step 1 — Service-level health (30 seconds)

```bash
# Service state
docker ps --filter name=batdetect --format 'status={{.Status}} uptime={{.RunningFor}}'

# Error count in last 24 h
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT count(*) FROM capture_errors
  WHERE service='batdetect-service' AND recorded_at > NOW() - INTERVAL '24 hours';"

# Classifier + thresholds from startup log
docker logs edge-batdetect-service-1 2>&1 | grep -E \
  "Initializing|HPF enabled|Classifier ready|Storage tiering|Audio validator|Monitoring started" | tail -10
```

All green = move on. Errors/restarts = fix service issues first.

### Step 2 — Power / undervoltage (60 seconds)

```bash
vcgencmd get_throttled                    # want 0x0
vcgencmd pmic_read_adc EXT5V_V            # want ~5.00 V, at least 4.9 V
dmesg | grep -c "Undervoltage detected"   # want 0 this boot
uptime                                    # if Pi rebooted recently, suspect PSU
```

Throttled flags (hex):

| Bit | Hex | Meaning |
| --- | --- | --- |
| 0 | 0x1 | Under-voltage **right now** |
| 1 | 0x2 | ARM frequency capped right now |
| 2 | 0x4 | Currently throttled |
| 16 | 0x10000 | Under-voltage **has occurred since boot** |
| 17 | 0x20000 | ARM freq capping since boot |
| 18 | 0x40000 | Throttling since boot |

If any bit is set, **do not continue debugging audio** — fix the PSU
first (section 5.1). Undervoltage silently degrades the USB preamp
voltage to the AudioMoth, masking everything else.

### Step 3 — Audio level (30 seconds)

```bash
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT count(*) AS n,
         round(avg(rms)::numeric, 5)  AS avg_rms,
         round(avg(peak)::numeric, 4) AS avg_peak
  FROM audio_levels
  WHERE recorded_at > NOW() - INTERVAL '10 minutes';"
```

Reference ranges (outdoor with 8 kHz hardware HPF, AudioMoth Medium-High
gain):

| Condition | avg_rms | avg_peak |
| --- | --- | --- |
| Mic silent / broken | < 0.002 | < 0.01 |
| Undervolted or gain too low | 0.002 – 0.003 | 0.01 – 0.05 |
| **Normal outdoor ambient** | **0.005 – 0.02** | **0.05 – 0.2** |
| Loud near-field sound (voice, hands) | 0.02 – 0.05 | 0.2 – 0.8 |
| Clipping | saturating | ≥ 0.9 |

If `avg_rms < 0.003`, the mic is not getting useful signal. Proceed to
section 5.2.

### Step 4 — Weather reality check

Bats don't fly below ~10 °C. Check the HOBO sensor:

```bash
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT to_char(recorded_at AT TIME ZONE 'America/New_York', 'HH24:MI') AS et,
         round(temperature_c::numeric, 1) AS c, sensor_serial
  FROM environmental_readings
  WHERE recorded_at > NOW() - INTERVAL '2 hours'
  ORDER BY recorded_at DESC LIMIT 10;"
```

If it's cold and rainy, the pipeline may be fine — there are just no
bats in the air. Don't keep debugging software at that point.

---

## Remediation playbooks

### 5.1 Pi 5 power supply

**Symptom:** `vcgencmd get_throttled` returns non-zero, `EXT5V_V < 5.0 V`,
dmesg shows undervoltage events.

Root cause: Pi 5 officially requires **5.1 V @ 5 A (27 W)**. The
standard USB-C PD profiles top out at 5 V @ 3 A, so most consumer
power banks cap there. When the Pi needs more than 3 A (batdetect-service
inference + USB audio + SSD/SD I/O), the bank sags, the rail undervolts,
and the kernel logs it.

Options ranked by reliability:

1. **Official Raspberry Pi 5 27 W USB-C Power Supply** (5.1 V / 5 A)
   directly into mains. Cheapest and most reliable. If the deployment
   has mains, this is the answer.

2. **Mains + UPS HAT** (Waveshare UPS HAT (E), Argon ONE UPS, PiJuice)
   for brownout protection. Batteries ride through short mains outages.

3. **12 V LiFePO4 battery + 5 V / 5 A buck converter.** Rugged field
   solution; requires some DC wiring. Overkill for a single-site
   deployment unless mains is unavailable.

4. **Power bank explicitly rated 5 V / 5 A on USB-C**. Rare. Check the
   spec sheet — if it says "20 V @ 5 A" that's irrelevant; you need
   5 V @ 5 A specifically. The Pi's PD negotiator downshifts to 5 V.

What **does not work**:

- Pi 4 PSU (15 W / 3 A) — will trigger undervoltage on Pi 5
- Generic phone chargers — most are 5 V @ 2 A
- Most consumer USB-C power banks — they don't offer 5 V @ 5 A
- Battery banks without USB-C PD

**Verification after swap:**

```bash
vcgencmd get_throttled    # expect: throttled=0x0
vcgencmd pmic_read_adc EXT5V_V    # expect: ~5.0 V
uptime                    # uptime should be stable (not frequent reboots)
```

### 5.2 AudioMoth USB Microphone — gain not applied

**Symptom:** `audio_levels.rms` stuck near mic noise floor (~0.001)
despite clean power. Peak values might show small spikes during sound
(0.01–0.02) but the baseline doesn't change.

Root cause (likely): AudioMoth USB Microphone firmware in **CUSTOM**
mode occasionally doesn't re-apply the config until the device is
fully power-cycled. Saving in the config app writes to flash; the
device reads that flash only at power-on.

**Fix (on advisor's laptop with AudioMoth config app + USB cable):**

1. Open the AudioMoth USB Microphone configuration app.
2. Verify current settings:
   - Switch position: **CUSTOM** (not DEFAULT — DEFAULT ignores filters)
   - Sample rate: **384 kHz** (Pi asks for 256 kHz; ALSA resamples)
   - Gain: **Medium-High** or **High** (NOT Medium — too quiet outdoors)
   - Filter: **High-pass, 8 kHz cutoff** (advisor's site choice; 16 kHz
     cleaner for archive)
3. Click "Apply" / "Save" in the app.
4. **Fully unplug the AudioMoth's USB cable** from the host.
5. Wait **10 full seconds** (not just a tap).
6. Replug into the Pi.
7. The AudioMoth re-reads its flash config at power-on. Gain, filter,
   and rate now match the app.

**Verification: live-stream watcher**

While the advisor makes test noises near the mic, stream the RMS live
from the Pi:

```bash
last_seen=0
while true; do
  out=$(docker exec edge-db-1 psql -U postgres -d soundscape -t -A -F'|' -c "
    SELECT id,
           to_char(recorded_at AT TIME ZONE 'America/New_York','HH24:MI:SS'),
           round(rms::numeric,5),
           round(peak::numeric,4)
    FROM audio_levels
    WHERE id > $last_seen
    ORDER BY id ASC;")
  if [ -n "$out" ]; then
    echo "$out" | while IFS='|' read id t rms peak; do
      printf "%s  rms=%s  peak=%s\n" "$t" "$rms" "$peak"
    done
    last_seen=$(echo "$out" | tail -1 | cut -d'|' -f1)
  fi
  sleep 2
done
```

Ask advisor to:

1. **Tap the AudioMoth case** firmly — expect peak > 0.05
2. **Jingle keys near the mic** (broadband ultrasonic) — expect RMS to
   jump into the 0.01+ range during the 15 s segment
3. **Speak loudly** — expect peak 0.3+
4. **Stay quiet** — expect RMS to settle to ~0.005–0.01 (outdoor ambient)

If peaks move but RMS never recovers to 0.005+ in silence, gain is
still too low — bump to **High**, re-save, full power-cycle, retest.

If nothing moves at all (both RMS and peak flat), proceed to section
5.3.

### 5.3 AudioMoth — mic element dead

**Symptom:** Even with max gain and direct loud sounds at the mic port,
RMS and peak don't change. Device enumerates on USB, streams packets,
but audio content is flat.

Diagnostic ladder (cheapest first):

1. **Swap USB data cable.** A charge-only cable passes power but may
   drop audio packets silently.
2. **Remove AudioMoth from any enclosure.** Check for stickers, water,
   dust in the mic port (the pinhole on the side).
3. **Different USB port on the Pi.** Sometimes the Pi 5's USB 3.0
   ports have less margin than USB 2.0.
4. **Test on a different computer.** Plug the AudioMoth into a Mac
   or Windows laptop and open Audacity. If RMS is still flat, the
   AudioMoth itself is dead — replace it. If audio shows up correctly
   on the laptop, the problem is Pi-side (driver, USB bus, container
   lock, cable at Pi end).

The laptop test is definitive. It takes 5 minutes and splits the
diagnosis cleanly. Do this before buying another AudioMoth.

---

## Replicating the setup on a second Pi

For when you want an identical rig for indoor testing (offline-WAV
analysis + optional playback-through-ultrasonic-speaker) to iterate
without touching the field Pi.

### Hardware shopping list

| Item | Spec | Why |
| --- | --- | --- |
| Raspberry Pi 5 | **4 GB+ RAM** | BatDetect2 + classifier need ~1.5 GB resident |
| microSD card or SSD | 64 GB+, Samsung PRO Endurance or SanDisk High Endurance | Cheap cards cause postgres flush stalls |
| **Official Pi 5 27 W USB-C PSU** | 5.1 V @ 5 A | Non-negotiable — anything less undervolts |
| Pi 5 active cooling | Official active cooler or Argon case with fan | BatDetect2 hits 70 °C without it |
| Case, vented | Any Pi 5 case | Avoid sealed cases |
| AudioMoth USB Microphone | Firmware 1.9.1+ (USB Mic firmware, NOT standard AudioMoth firmware) | Streams via USB to the Pi |
| USB cable for AudioMoth | Short, data-capable USB-A → USB-micro (check AudioMoth version) | Charge-only cables silently drop audio |
| Optional: HOBO MX2201 BLE sensor | For cross-modal data integrity analysis | Only if replicating thesis experiment |

### Software bring-up

```bash
# 1. Flash Raspberry Pi OS 64-bit Lite (no desktop).
#    Set up SSH + WiFi in the imager.

# 2. Boot + update
ssh stafa@<pi-ip>
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone America/New_York  # or your zone

# 3. Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 4. Clone repo
git clone https://github.com/stafa-san/bat-edge-monitor.git
cd bat-edge-monitor

# 5. Firebase credentials — use a SEPARATE Firebase project for the
#    lab rig so dashboard data doesn't mix with the field Pi.
#    Drop the service-account JSON at:
#      edge/sync-service/serviceAccountKey.json

# 6. Create .env
cat > edge/.env <<EOF
FIREBASE_PROJECT_ID=<your-lab-project-id>
ENABLE_GROUPS_CLASSIFIER=true
ENABLE_STORAGE_TIERING=true
ENABLE_ONEDRIVE_SYNC=false
PI_SITE=lab01
EOF

# 7. AudioMoth config — use the AudioMoth USB Microphone App on a
#    Mac/Windows machine:
#      - Switch: CUSTOM (NOT DEFAULT)
#      - Sample rate: 384 kHz
#      - Gain: Medium-High (or High for quiet rooms)
#      - Filter: High-pass, 8 kHz (field config) or 16 kHz (archive-clean)
#    Save config. UNPLUG USB. Wait 10 s. Replug into Pi.

# 8. Build + start
cd edge
docker compose up -d --build
```

### Verify bring-up sequence

Run through the same Step 1–4 diagnostic ladder on the new rig. All
should come out green in this order:

```bash
# Step 1 — services
docker ps                           # all containers "Up"
docker logs edge-batdetect-service-1 --tail 20 | grep "Monitoring started"
# expect: Monitoring started — batdetect_threshold=0.5, min_pred_conf=0.6, segment=15s

# Step 2 — power
vcgencmd get_throttled              # expect: throttled=0x0
vcgencmd pmic_read_adc EXT5V_V      # expect: ~5.0 V

# Step 3 — audio (after 1 min of ambient)
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT round(avg(rms)::numeric, 5), round(avg(peak)::numeric, 4)
  FROM audio_levels WHERE recorded_at > NOW() - INTERVAL '1 minute';"
# expect (indoor ambient, 8 kHz HPF): avg_rms ~0.005–0.03

# Step 4 — dashboard (after deploying a new Vercel copy pointed at the
# lab Firebase project)
#   Power card: Stable (green)
#   5V Rail: green
#   Audio Level: green or yellow
#   No undervoltage banner
```

### Testing ideas for the lab rig

- **Offline WAV analysis**: POST a known-species WAV to
  `http://<pi-ip>:8080/analyze` — fastest iteration loop, no mic needed.
- **Playback testing**: requires an ultrasonic speaker (Avisoft,
  Pettersson, ~$800+). Not worth it unless the lab already has one;
  consumer speakers cap at 20 kHz and bats call above that.
- **Threshold experimentation**: change `DETECTION_THRESHOLD`,
  `MIN_PREDICTION_CONF`, `VALIDATOR_MIN_RMS` etc. on the lab rig
  without disturbing the field deployment; see
  [`DETECTION_TUNING_PLAYBOOK.md`](DETECTION_TUNING_PLAYBOOK.md).

---

## Verifying AudioMoth config from the Pi (probe script)

The AudioMoth USB Microphone firmware doesn't expose its current config
(gain, HPF cutoff, switch position) over USB — those are only readable
via the AudioMoth USB Microphone App running on a macOS / Windows
desktop. From the Pi we can **infer** the config by recording a short
sample and analyzing the spectrum. That's what
[`edge/scripts/probe_audiomoth.py`](edge/scripts/probe_audiomoth.py)
does.

### When to run it

- Right after the advisor saves a new config in the app — sanity-check
  that the change actually took effect.
- After any "unplug 10 s, replug" power-cycle.
- Any time the dashboard shows unexpected audio levels and you want to
  know if it's the mic, the gain, or the filter.

### How to run it

**Option A — inside a transient container** (preferred — doesn't need
host scipy/matplotlib):

```bash
# Stop batdetect so the USB audio device is free
docker stop edge-batdetect-service-1

# Run the probe — writes WAV + PNG + verdict
docker run --rm \
  --device /dev/snd --privileged \
  -v /home/stafa/bat-edge-monitor/edge/scripts/probe_audiomoth.py:/probe.py:ro \
  -v /tmp:/host_tmp \
  edge-batdetect-service:latest \
  python3 /probe.py --duration 10 --out /host_tmp

# Restart batdetect
docker start edge-batdetect-service-1
```

**Option B — on the host directly** (requires `python3-scipy python3-matplotlib`
installed system-wide):

```bash
docker stop edge-batdetect-service-1
python3 edge/scripts/probe_audiomoth.py --duration 10
docker start edge-batdetect-service-1
```

### What it tells you

```
============================================================
AudioMoth probe result
============================================================
Sample rate        : 256000 Hz
Duration           : 10.00 s
RMS amplitude      : 0.00843
Peak amplitude     : 0.1821
Inferred HPF cutoff: not detected (broadband content present)
Noise floor        : -96.4 dB

Band energy (dB, relative to in-band median):
          0–2 kHz : +5.3 dB
          2–8 kHz : +11.6 dB
         8–15 kHz : +11.0 dB
        15–25 kHz : +7.8 dB
        25–50 kHz : +0.8 dB
       50–100 kHz : -5.1 dB
      100–128 kHz : -9.3 dB

✓ Audio level is normal for outdoor ambient with a hardware high-pass filter (8–16 kHz cutoff).
⚠ No HPF cutoff detected — low-frequency (< 8 kHz) content is reaching the mic.
  Either no HPF is configured, or the switch is in DEFAULT.
  Expected setting: CUSTOM + 8 kHz HPF.
```

### Reading the verdict

- **"Inferred HPF cutoff: ~X kHz"** — the filter is working, cutoff is
  at ~X kHz. Bat calls need content above 18 kHz preserved, so a cutoff
  at 8–16 kHz is fine. A cutoff above 20 kHz will clip real calls.
- **"Inferred HPF cutoff: not detected"** — significant energy exists
  below 8 kHz. Either no HPF is configured or the switch is in
  DEFAULT. Tell the advisor to verify the switch position.
- **Band energy** — each row is the median power in that band,
  relative to the 20–80 kHz bat-band median. If 2–8 kHz is **higher**
  than 25–50 kHz, sub-bat content dominates (HPF not working). If
  2–8 kHz is **lower** than 25–50 kHz, HPF is working.
- **RMS verdict**:
  - `< 0.001` → mic silent
  - `0.001–0.003` → very low, check gain or obstruction
  - `0.003–0.02` → normal outdoor ambient with HPF
  - `0.02–0.1` → healthy / slightly loud
  - `> 0.1` → clipping risk

### Limitations

- This script **cannot** read the AudioMoth's switch position or gain
  setting directly. It can only show what the device is currently
  outputting. If gain is "Medium-High" vs "High" and the environment
  is quiet, RMS values can overlap — you may not be able to
  distinguish the two.
- The inferred HPF cutoff is a rough estimate (±2 kHz). A sharp 8 kHz
  filter may register as 6 kHz or 10 kHz depending on ambient spectral
  character.
- If the site is genuinely silent (e.g. middle of the night, no wind,
  no distant traffic), the probe won't have enough dynamic range to
  measure the HPF. Run it during daylight or when there's ambient
  broadband content to test against.

### Machine-readable output

The last line of the probe's stdout is `PROBE_JSON { ... }` with the
full result as JSON, so you can pipe to `jq` or save to a file for
trend tracking. Example:

```bash
python3 edge/scripts/probe_audiomoth.py --duration 10 --out /tmp \
  | grep -oP '(?<=^PROBE_JSON ).*' \
  | jq
```

---

## Sub-threshold BatDetect2 logging

A silent pipeline ("No bat calls detected" every segment) has two
very different root causes that used to look identical in the logs.
As of 2026-04-21 the heartbeat log line now includes diagnostic
stats from the detector itself:

```
# Detector saw literally nothing
[BAT] #40 | No bat calls detected (bd_raw=0)

# Detector saw 3 weak sub-threshold emissions, best was 0.22
[BAT] #50 | No bat calls detected (bd_raw=3 max=0.22 user_pass=0 top=Pipistrellus_pipi)

# Detector saw 8 emissions, 0 passed the DETECTION_THRESHOLD=0.5 gate
[BAT] #60 | No bat calls detected (bd_raw=8 max=0.48 user_pass=0 top=Nyctalus_noctu)
```

Interpretation:

| Pattern over 1 hour | Meaning | Action |
| --- | --- | --- |
| Every line `bd_raw=0` | Mic or detector sees nothing | Check audio level + mic (section 5.2) |
| `bd_raw` > 0, `max` < 0.2 | Detector sees noise, not bats | Probably noise. Wait for real bat activity. |
| `bd_raw` > 0, `max` 0.3–0.5 | Detector sees weak but possibly real signal | Consider lowering `DETECTION_THRESHOLD` (see DETECTION_TUNING_PLAYBOOK.md) |
| `bd_raw` > 0, `user_pass` > 0 | Detector passed threshold — downstream gate must be dropping it | Check rejection reason log lines |

### Command to summarize the last N hours

```bash
docker logs edge-batdetect-service-1 --since 6h 2>&1 \
  | grep -oP 'bd_raw=\d+ max=[\d.]+' \
  | sort | uniq -c | sort -rn | head -20
```

---

## Daily summary email

Once per UTC day, the sync-service can generate a plain-text + HTML
rollup of the last 24 hours and email it. It always logs the summary
to container stdout regardless of email config, so you can see the
format immediately.

### Enabling the email send

1. **Turn on 2-factor auth** for the sender Gmail account:
   <https://myaccount.google.com/security>.

2. **Create an app password** (Mail):
   <https://myaccount.google.com/apppasswords>. Copy the 16-character
   string — the spaces are optional, SMTP strips them.

3. Add to `edge/.env`:
   ```
   ENABLE_DAILY_SUMMARY=true
   DAILY_SUMMARY_HOUR_UTC=11
   DAILY_SUMMARY_RECIPIENTS=you@example.com,advisor@example.com
   GMAIL_USER=sender@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   ```

4. `docker compose up -d sync-service`. The next tick at 11:00 UTC
   (07:00 EDT) will send.

### Forcing a test send now (without waiting for 11 UTC)

```bash
docker exec edge-sync-service-1 python3 -c "
from src.daily_summary import send_summary
from src.main import get_db_connection
import os
send_summary(get_db_connection(), os.getenv('PI_SITE', 'pi01'))
"
```

Output shows the full report in stdout, then attempts to send. If
credentials are missing it logs `GMAIL_USER/GMAIL_APP_PASSWORD not set
— skipping email send` and exits cleanly.

### What the email contains

- Bat detections count + top classes
- Audio RMS p50 / p95 / max peak (window of segments processed)
- BatDetect2 stats: average raw emissions per segment, max det_prob
  observed, count of segments that passed the user threshold
- Validator rejection counts, broken down by reason
- HOBO temperature min / avg / max
- Capture error count (flagged in red if non-zero)
- Dashboard URL

Example:

```
Bat Edge Monitor — daily summary (pi01)
Window: last 24 h, generated 2026-04-21 11:00 UTC

Bat detections: 3
  LACI              2
  EPFU_LANO         1

Audio segments processed: 2,744
  RMS p50       : 0.00620
  RMS p95       : 0.01410
  Peak max      : 0.8291

BatDetect2 activity:
  avg raw emissions / segment : 8.4
  max det_prob observed       : 0.58
  segments that passed user threshold : 3

Validator rejections:
  validator:no_burst             5
  all_below_user_threshold     1412

Temperature (2 sensor(s)):
  min / avg / max  : 14.2 / 19.7 / 24.1 °C
```

### Failure modes

- **`GMAIL_USER/GMAIL_APP_PASSWORD not set`** — credentials missing.
  The summary still runs, just isn't emailed.
- **`DAILY_SUMMARY_RECIPIENTS empty`** — configure recipients in `.env`.
- **`smtplib.SMTPAuthenticationError`** — the app password is wrong or
  2FA isn't enabled on the Gmail account. Regenerate the app password.

---

## Command reference — quick diagnostic one-liners

```bash
# Power
vcgencmd get_throttled
vcgencmd pmic_read_adc EXT5V_V
dmesg | grep -i undervolt | tail

# Audio level
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT round(avg(rms)::numeric, 5) FROM audio_levels
  WHERE recorded_at > NOW() - INTERVAL '10 minutes';"

# Live stream (Ctrl+C to stop)
watch -n 2 "docker exec edge-db-1 psql -U postgres -d soundscape -c \"
  SELECT to_char(recorded_at, 'HH24:MI:SS'), round(rms::numeric, 5), round(peak::numeric, 4)
  FROM audio_levels ORDER BY recorded_at DESC LIMIT 5;\""

# AudioMoth USB enumeration
lsusb | grep -i audiomoth
cat /proc/asound/card2/stream0 2>/dev/null | head -14

# Recent detections (last 6 h)
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT to_char(detection_time, 'MM-DD HH24:MI'),
         predicted_class,
         round(prediction_confidence::numeric, 2) AS pc,
         round(detection_prob::numeric, 2) AS det
  FROM bat_detections
  WHERE detection_time > NOW() - INTERVAL '6 hours'
  ORDER BY detection_time DESC;"

# Capture errors
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT to_char(recorded_at, 'MM-DD HH24:MI'), error_type, count(*)
  FROM capture_errors
  WHERE service='batdetect-service' AND recorded_at > NOW() - INTERVAL '6 hours'
  GROUP BY 1, 2 ORDER BY 1 DESC;"

# Weather (HOBO sensor)
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT to_char(recorded_at, 'MM-DD HH24:MI'),
         round(temperature_c::numeric, 1), sensor_serial
  FROM environmental_readings
  WHERE recorded_at > NOW() - INTERVAL '1 hour'
  ORDER BY recorded_at DESC LIMIT 6;"
```

---

## Baseline reference — what "healthy" looks like

Numbers to compare your own rig against. These are from this Pi on
2026-04-21 after both power and gain fixes were applied.

| Metric | Value | Pass threshold |
| --- | --- | --- |
| `throttled_hex` | `0x0` | **0x0** only |
| `EXT5V_V` | 4.98–5.01 V | ≥ 4.9 V |
| `coreVoltage` | 0.88 V | 0.75–0.95 V |
| undervolt events / hour | 0 | **0** |
| `audio_levels.rms` avg (10 min ambient) | 0.006 | 0.003–0.03 |
| `audio_levels.peak` avg (10 min ambient) | 0.16 | 0.05–0.5 |
| `audio_levels.peak` on tap test | 0.8+ | ≥ 0.3 |
| AudioMoth in `/proc/asound/cards` | `384kHz AudioMoth USB Microphone` | must list |
| `arecord -l` shows card 2 | USB Audio | yes |
| Pi CPU temp (idle) | 46–55 °C | ≤ 70 °C |
| Pi load avg 5m | 2–4 | < 5 sustained |

A rig that meets all of these and has weather above 10 °C at night
will produce real bat detections. The converse: if any of these fails,
no amount of threshold tuning in software will save you.

---

## Lessons — pin these on the wall

1. **Services up ≠ pipeline working.** Our dashboard showed "everything
   green" for 18 hours while producing zero useful data. Add telemetry
   *before* you need it — the voltage + audio-RMS panels built during
   this session would have pinpointed both root causes in 30 seconds.
2. **Fix upstream first.** Undervoltage masks mic problems. Mic problems
   mask classifier problems. Debug the stack top-down.
3. **AudioMoth batteries do not help in USB mode.** USB Microphone
   firmware runs entirely on USB power regardless of whether AAs are
   inserted. Don't waste time checking batteries.
4. **CUSTOM mode on the AudioMoth requires a full power-cycle** after
   saving config. "Click save" alone isn't enough.
5. **"Normal outdoor ambient" with an 8 kHz HPF is RMS ≈ 0.005–0.02**,
   not indoor-quiet 0.00001. Don't over-calibrate validator thresholds
   against a lab environment.
6. **Weather is a real variable.** A 6 °C night genuinely produces zero
   bat activity. Always check the HOBO temp before assuming the
   pipeline is broken.
7. **Peak and RMS tell different stories.** A responsive mic with low
   gain shows big peaks during sound events but low overall RMS. A dead
   mic shows flat everything. Look at both before drawing conclusions.
