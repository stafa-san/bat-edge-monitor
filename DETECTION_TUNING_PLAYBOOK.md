# Detection Tuning Playbook — First-Week Monitoring

Companion doc to `SESSION_NOTES_2026-04-20.md` and `BATDETECT2_TRAINING.md`.
Use this as the reference when deciding whether and how to change the
detection thresholds after the first 24–48 hours of real deployment data.

Written: 2026-04-21 (late evening, post single-gate-model commit —
`MIN_PREDICTION_CONF=0.6`, `DETECTION_THRESHOLD=0.3`).

Status: the pipeline is live, capturing confident detections to Postgres
→ Firestore → `/bat_audio/tier1_permanent/<CLASS>/` → Google Drive.

**Update (late 2026-04-20):** after a manual WAV review surfaced a
silent-file false positive, we raised `DETECTION_THRESHOLD` to 0.5 to
match training, added the audio-level validator described in
[`AUDIO_VALIDATOR.md`](AUDIO_VALIDATOR.md), and wiped the archive.
Several sections of this playbook now describe *completed* work — see
the "✅ implemented" markers below.

---

## TL;DR

1. The classifier head was **trained on features from calls where
   `det_prob > 0.5`**. Our Pi is running inference at
   `DETECTION_THRESHOLD=0.3`. Features from the 0.3–0.5 range are
   out-of-distribution for the classifier.
2. This doesn't break the pipeline — frequencies and durations match
   biology (LACI 22–32 kHz, EPFU_LANO 57–62 kHz, etc.). Real weak bat
   calls are still correctly classified.
3. But the classifier cannot say "this isn't a bat" — it must pick one
   of 5 classes. Broadband noise that happens to sit in a low-frequency
   band gets assigned to the nearest class, most often **LACI**, which
   explains the current 88 % LACI dominance.
4. Be patient 24–48 h. Collect data, then tune with evidence instead
   of guessing.

---

## 1. The training–inference threshold mismatch

From `BATDETECT2_TRAINING.md`:

> "BatDetect2 is frozen. Each call produces a 32-dim feature vector per
> detection. **We filter to `det_prob > 0.5`.** ... Below 0.5 is mostly
> noise that BatDetect2 is unsure about anyway."

Current Pi state:

| | Training | Pi inference |
| --- | --- | --- |
| BatDetect2 `det_prob` filter | **0.5** | **0.3** |
| Classifier input distribution | Only high-confidence features | Mix of high + medium |
| Fallback when classifier is uncertain | Not exercised — all inputs are clean | Must pick 1 of 5 classes anyway |

Post-cleanup `det_prob` distribution on this Pi (last 7 days, 49 rows):

| Bucket | n | Comment |
| --- | --- | --- |
| 0.3–0.4 | 28 | Majority; out-of-training-distribution |
| 0.4–0.5 | 19 | Still out-of-distribution |
| 0.5+ | 2 | Only this bucket matches training |

47 of 49 detections are technically out of distribution for the
classifier. The fact that they come out with sensible species/frequency
alignment suggests most ARE real bat calls at lower SNR — but the
pipeline cannot distinguish "real weak call" from "noise that looks
like a weak call" by itself.

### How big a problem is this for the thesis?

- Not fatal. The architecture is sound.
- Quantifiable — the distribution of frequencies per class acts as an
  internal consistency check. All current predictions check out
  biologically.
- The cleanest thesis-defensible move is to raise
  `DETECTION_THRESHOLD` back to 0.5 once we have a week of data to
  compare before/after volumes.
- The messiest version is to retrain the classifier on features coming
  from this deployment site, which dissolves the mismatch entirely.

---

## 2. What to watch over the next 24–48 hours

Six diagnostic checks. If the pipeline is behaving, most of these
should look plausible. When any of them look wrong, that's the signal
pointing at the next change.

### 2.1 Temporal pattern of detections

Ohio bat activity in April:
- Peak just after sunset (~20:00–22:00 ET)
- Second smaller peak near dawn (~05:00–06:00 ET)
- Near-zero 03:00–05:00 ET
- Minimal midday

**If detections are uniformly distributed across all 24 h, most are
noise.** If sunset/dawn peaks are visible, the pipeline is catching
real bats.

Query:

```bash
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT extract(hour from detection_time AT TIME ZONE 'America/New_York')::int AS hour_et,
         count(*) AS n
  FROM bat_detections
  WHERE detection_time > NOW() - INTERVAL '48 hours'
  GROUP BY 1 ORDER BY 1;"
```

### 2.2 Species distribution vs Ohio April expectations

April in Ohio:
- **EPFU_LANO** (Big Brown + Silver-haired): resident, should be
  common as temperatures warm
- **LACI** (Hoary): migrating through
- **LABO** (Eastern Red): migrating through
- **MYSP** (Myotis spp.): most affected by white-nose syndrome —
  rare
- **PESU** (Tricolored): rare, white-nose affected

88 %+ LACI for weeks straight is implausible. If it persists even with
clean-weather nights, that's the low-frequency-noise bias talking, and
raising `DETECTION_THRESHOLD` becomes clearly worth it.

Query (lives in the dashboard feed chip summary too):

```bash
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT predicted_class,
         count(*) AS n,
         round(avg(prediction_confidence)::numeric, 2) AS avg_pc
  FROM bat_detections
  WHERE detection_time > NOW() - INTERVAL '48 hours'
  GROUP BY 1 ORDER BY n DESC;"
```

### 2.3 Score distribution per class

For each predicted class, look at the histogram of
`prediction_confidence`. A healthy class shows a **tight cluster at
0.7–0.95** (real calls). A class that's collecting noise-coerced
assignments shows a **long smooth tail toward 0.6** (every call just
barely clearing the gate).

Query:

```bash
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT predicted_class,
         round(min(prediction_confidence)::numeric, 2) AS min_pc,
         round(avg(prediction_confidence)::numeric, 2) AS avg_pc,
         round(max(prediction_confidence)::numeric, 2) AS max_pc,
         count(*) AS n
  FROM bat_detections
  WHERE detection_time > NOW() - INTERVAL '48 hours'
  GROUP BY 1 ORDER BY n DESC;"
```

### 2.4 Frequency-class consistency

Biological expectation:

| Class | Typical call band (kHz) |
| --- | --- |
| EPFU_LANO | 25–55 |
| LABO | 35–55 |
| LACI | 20–35 |
| MYSP | 35–80 |
| PESU | 40–55 |

Any detection where the centre frequency sits outside the expected
band for its predicted class is suspicious — likely noise or
misclassification.

Query (flags inconsistent rows):

```bash
docker exec edge-db-1 psql -U postgres -d soundscape -c "
  SELECT predicted_class,
         round(low_freq::numeric/1000, 1) AS low_kHz,
         round(high_freq::numeric/1000, 1) AS high_kHz,
         round(prediction_confidence::numeric, 2) AS pc,
         to_char(detection_time, 'MM-DD HH24:MI') AS ts
  FROM bat_detections
  WHERE detection_time > NOW() - INTERVAL '48 hours'
    AND (
         (predicted_class = 'LACI'      AND (high_freq/1000 > 40 OR low_freq/1000 < 15))
      OR (predicted_class = 'EPFU_LANO' AND (high_freq/1000 > 70 OR low_freq/1000 < 20))
      OR (predicted_class = 'LABO'      AND (high_freq/1000 > 65 OR low_freq/1000 < 30))
      OR (predicted_class = 'MYSP'      AND (high_freq/1000 > 95 OR low_freq/1000 < 30))
      OR (predicted_class = 'PESU'      AND (high_freq/1000 > 65 OR low_freq/1000 < 35))
    )
  ORDER BY detection_time DESC;"
```

### 2.5 Manual Audacity spot-check

Pick 3–5 WAVs per species from
`gdrive:Bat Recordings from pi01/tier1_permanent/<CLASS>/`. In Audacity
(track → Spectrogram view):

- **Clear downward FM sweep in the expected band** → real bat call ✅
- **Broadband vertical streaks or speckle** → noise ❌
- **Nothing visible at all** → silent segment, false positive ❌

Dr. Johnson can verify a handful quickly. Ten WAVs of spot-checking is
worth more than any threshold calibration we could invent.

### 2.6 Noise correlation

Does detection count spike with:
- High CPU load (fan running harder) → mechanical noise bleed
- Wind / storm timestamps → wind buffeting
- HOBO temperature swings → atmospheric disturbance

`device_status` table has CPU load and fan-driving metrics; HOBO env
data has temperature. Join on timestamp and look for correlation.

---

## 3. Observability improvements to implement

Two additions worth making before the next tuning decision. Both are
small, both make tuning *from data* possible instead of *by
intuition*.

### 3.1 Bring back tier-3 metadata-only logging — **superseded**

After the 2026-04-20 follow-up, we chose a different direction: an
audio-level validator (see [`AUDIO_VALIDATOR.md`](AUDIO_VALIDATOR.md))
that actually rejects noise rather than logging it. Tier-3 metadata
logging remains a reasonable future enhancement if we later want a
diagnostic record of what the validator rejected, but the user
problem ("my gdrive is full of noise") needed a filter, not a
counter.

**If reintroduced later**, the shape would be the same as described
in this section — add storage_tier = 3 rows for every classified
detection that didn't make it past all three gates, with the
rejection reason stored as a new column. Tier-3 rows already
auto-expire after 7 days via `sync-service/main.py:cleanup_old_data()`.

### 3.2 Nightly diagnostic summary

A small script that prints / emails the six checks from section 2.
Runs from cron each morning. Gives us 15 seconds of "is the pipeline
healthy and what is it seeing" instead of needing to SSH in and write
queries.

Not urgent — the queries are short enough to run manually during the
first few mornings.

---

## 4. Longer-term path to proper retrain

The training data was folder-per-species from Dr. Johnson's library.
That's ground-truth quality labels, which is why the classifier
generalizes at all. To actually improve it for **this site**, we need
site-specific labelled data:

### 4.1 Deployment-site labels

Once we have ~50 tier-1 WAVs per species from Google Drive, ship them
to Dr. Johnson for review. He marks each as:

- correct species
- misclassified — actual species is X
- not a bat (noise)

Even 100 labels total is enough to detect systematic misclassification
patterns (e.g. "everything below 25 kHz gets called LACI regardless").

### 4.2 Save features, not just metadata

The classifier operates on 32-dim feature vectors extracted by
BatDetect2. If we save the feature vector alongside each row in
Postgres, we can re-run classification experiments offline with new
thresholds or retrained models **without re-processing audio**.

Cost: 32 floats × 4 bytes = 128 bytes per row. Nothing.

Payoff: we can A/B test new classifier heads against historical
deployment data without touching anything on the Pi.

### 4.3 Parallel validation with a commercial NA tool

Kaleidoscope Pro and SonoBat both do NA species ID. Running one of them
on a sample of tier-1 WAVs gives an independent species call for
comparison — useful for the thesis's data-integrity chapter.

### 4.4 Retrain with site features

Once labels and features are both captured:

- Extract features from 100-ish labelled site WAVs
- Add them to the original training data
- Retrain classifier head (~1 hr Vast.ai GPU, ~$0.50)
- Swap `groups_model.pt` on the Pi, bump `MODEL_VERSION` env var

This resolves the training-vs-inference distribution mismatch entirely
and gives us a classifier that knows what THIS site sounds like.

---

## 5. Action items, prioritized

Original plan (2026-04-20 afternoon):

1. ~~Wait 24–48 h to collect a baseline~~ **Superseded** — a single
   manual WAV review that evening was evidence enough that
   `DETECTION_THRESHOLD=0.3` was too permissive.
2. **Ask Dr. Johnson to raise AudioMoth hardware HPF from 8 kHz →
   16 kHz.** Still open. Now quality-of-life rather than
   correctness — the validator protects the archive regardless of
   hardware HPF.
3. Run the six diagnostic queries each morning (section 2) and log
   what each one says. Still the right thing to do.
4. Spot-check 3–5 gdrive WAVs per species in Audacity (section 2.5).
   Still the right thing to do.
5. ~~Decide after 48 h whether to raise `DETECTION_THRESHOLD` to 0.5~~
   **✅ Done 2026-04-20 evening.** See
   [`AUDIO_VALIDATOR.md`](AUDIO_VALIDATOR.md) and
   `SESSION_NOTES_2026-04-20.md` Follow-up 2.
6. ~~Implement tier-3 metadata logging~~ **Superseded** by the
   validator — see section 3.1 above.

Next up:

7. Watch the validator's rejection reasons over the first week.
   Tune `VALIDATOR_MIN_RMS` etc. if we're losing real calls.
8. Start section 4.1 (deployment-site label collection) as soon as
   tier-1 WAVs accumulate. Target: 50+ labeled per species before
   attempting a retrain.

## 6. One-line reminder

> The pipeline is running honestly — one gate decides everything and
> downstream consumers all see the same set. The remaining work is
> **validating which detections are real** using the 48 h of deployment
> data we're about to collect, not re-engineering the pipeline.
