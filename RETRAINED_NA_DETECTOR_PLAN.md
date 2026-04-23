# Retrained North American Bat Detector — Research Plan

A scoping document for replacing UK-trained BatDetect2 with a detector trained on North American (specifically Ohio) recordings. Framed as both a production improvement and a multi-year research track that naturally extends the thesis work into PhD-adjacent territory.

- **Author:** Mustapha Zakari Nasomah
- **Advisor:** Dr. Joseph S. Johnson (UC, both MS and planned PhD)
- **Status:** Proposal — not started
- **Last updated:** 2026-04-23

---

## TL;DR

The current system uses UK-trained BatDetect2 as a frozen feature extractor, with a 5-class NA-trained classifier head layered on top. This works, but the *detector* itself — the part that decides "is there a bat call here at all?" — is systematically under-confident on NA species. On a real test file (`2MU01134_20240527_005517.wav`), the detector emitted **zero candidates** at its original threshold despite the spectrogram showing ~60 obvious FM pulses. Lowering the threshold recovered the calls, but that's a workaround.

The durable fix is to retrain BatDetect2 (or its architecture) from NA data. This is a **2-4 week project** once labeled data is in hand, with expected gains in detection recall and a modest gain in classification accuracy. Doable with a Vast.ai rental (~$10-30 in GPU). The bottleneck is **annotated data**, not compute.

Dr. Johnson has **terabytes** of unlabeled Ohio bat recordings. This is a massive asset — **yes, this is absolutely worth doing** — but it needs to be turned into bounding-box labels before it's useful for training a detector. That annotation effort is where the bulk of the work lives.

This scales naturally from "production improvement for the thesis system" into "PhD thesis-adjacent work on transfer-learning acoustic detectors for regional fauna."

---

## Why Retrain the Detector?

### Current architecture

```
[WAV] → BatDetect2 (UK-trained) → { detections[], features[32], spec }
                                                    │
                                                    ▼
                                       Groups classifier head
                                        (NA-trained, April 2026)
                                                    │
                                                    ▼
                                       One of {EPFU_LANO, LABO, LACI,
                                               MYSP, PESU}
```

### The gap

The **classifier head** works well — we retrained it on Dr. Johnson's NA recordings and it hits 80% validation accuracy overall, 83.4% on PESU (up from 0% with UK labels).

The **detector** is still UK-trained. It was trained on a corpus where bat calls had different:
- Amplitude profiles (different microphone conditions, closer bats)
- Frequency ranges (UK species vs NA species)
- Call shapes (UK horseshoe bats have constant-frequency components; NA bats are primarily FM)
- Background noise profiles

As a result, BatDetect2's `det_prob` output on Ohio recordings is systematically low — typical real bat calls produce `det_prob` values of 0.1–0.4, not the 0.5+ its training distribution expected. Our workaround is to lower the threshold to 0.3 and lean on downstream gates (classifier confidence, FM-sweep shape filter, audio-level validator) to reject false positives.

That's working. But it's fragile — if a fourth NA species with unusual call shape appeared, we'd have no way to know whether the detector is silently dropping it or whether it genuinely isn't in the audio.

### What retraining would fix

- **Detection recall** on NA species (the 005517-style false negatives).
- **Classifier input distribution stability** — features from a NA-trained detector would be closer to the NA-labeled data the classifier was trained on.
- **Per-species calibration** — the detector could be made sensitive to low-frequency LACI calls (22-28 kHz) separately from high-frequency MYSP calls (60-100 kHz), instead of treating all bat calls as a single homogeneous target.
- **Non-bat rejection** — if we include NA-specific hard negatives (katydid trills, rain, electrical clicks, bird chirps recorded in Ohio habitats) in training, the detector itself becomes a first-pass noise filter, instead of relying on the audio-level validator downstream.

---

## Is It Worth It Given Dr. Johnson's Data?

**Yes. Unambiguously.** Here's the calculus:

| Resource | What we have | What we need |
|---|---|---|
| Labeled audio (species label per file) | 9 species × hundreds of files = ✅ already used for classifier head | n/a — already leveraged |
| **Bounding-box annotations** (call locations in time/freq) | ❌ we have zero | ~500-1000 bat calls annotated across all species |
| Raw audio | ✅ terabytes — "lots and lots" per Dr. Johnson | n/a — plenty |
| GPU compute | ❌ not owned; can rent | ~$10-30 via Vast.ai for one training run |
| Expertise | Mustapha (CS/ML) + Dr. Johnson (bioacoustics) | covered by the pairing |

The bottleneck is annotation, not data or compute. That bottleneck is **linear in effort, not exponential**: you look at a spectrogram, drag a box around each call, save. A trained annotator can label ~50-100 bat calls per hour. 500 calls ≈ 5-10 hours of Dr. Johnson's or a research assistant's time.

Dr. Johnson's terabytes matter because:
1. **Diversity**: his corpus covers multiple seasons, sites, weather conditions, and species behaviors. Training on a narrow slice = narrow detector. Training across his full range = robust detector.
2. **Negatives**: the same corpus contains silent segments, noise passes, insect activity, and other non-bat acoustic events. These are *free hard negatives* — we don't have to synthesize them, they're already there in the "no-bat" windows of his recordings.
3. **Species coverage**: any NA species that's seen the field will eventually pass an AudioMoth in his corpus. A detector trained on his data generalizes across the Eastern US bat community, not just our 5 grouped classes.

---

## How This Fits a PhD Thesis

The MS thesis narrative is already set: "data integrity in multi-modal edge systems for ecological monitoring." The retraining work is tangential to *that* story (it's about algorithm improvement, not sensor-fusion data integrity), but it's a natural standalone research program that:

- **Keeps the same advisor** (Dr. Johnson is committed to the work and owns the data).
- **Uses the infrastructure the MS built**: AudioMoth + Pi + Firebase + dashboard + retraining pipeline are all in place. The next researcher doesn't start from zero.
- **Has clear publishable output**:
  1. Technical contribution: methodology for transfer-learning bat detectors across ecoregions with small-N bounding-box data.
  2. Applied contribution: an open-source NA-trained BatDetect2 variant that the whole NA bat research community can use.
  3. Ecological contribution: the continuous-learning framework from the MS thesis now has a real model-evolution loop closing.
- **Matches PhD scope**: 3-4 years allows iterative rounds of labeling → training → field validation → re-labeling. That's roughly the lifecycle of a PhD dissertation chapter.

Proposed PhD dissertation chapters (draft):
1. Regional calibration of passive bioacoustic detectors: transfer learning from UK to NA bats.
2. Field-deployed continuous-learning framework for acoustic monitoring (expanding the MS flywheel).
3. Non-bat negative mining: can a detector learn what "not a bat" sounds like from unlabeled field audio?
4. Cross-taxa generalization — does the NA bat detector architecture extend to North American amphibians, cicadas, or birds sharing the same acoustic niche?

---

## Annotation Tools (Ranked)

### Recommended starting point: **Raven Pro**

- Maker: Cornell Lab of Ornithology
- Cost: $800 academic license, free Raven Lite for 1-channel files
- Strengths:
  - Industry standard in bioacoustics — Dr. Johnson likely already knows it
  - Handles large files well (our 15s AudioMoth clips are trivial)
  - Selection tables are CSV-exportable and match BatDetect2's label format almost directly
  - Batch annotation workflow (queue many files, annotate serially)
- Output: CSV with `Begin Time (s)`, `End Time (s)`, `Low Freq (Hz)`, `High Freq (Hz)`, `Annotation` (species)

### Open-source alternatives

| Tool | Strengths | Weaknesses |
|---|---|---|
| **Audacity + Label Tracks** | Free, familiar, cross-platform | No native bounding-box UI (only time-axis labels); would need a post-process script to add frequency bounds |
| **Praat** | Free, spectrogram view is excellent, widely used in linguistics | Primarily time-domain; bounding-box support via "TextGrids" is awkward |
| **Sonic Visualiser** | Free, clean UI, layers work like Raven | Less bioacoustics-specific tooling |
| **Labelbee** | Web-based, good for team review | Young project, smaller community |
| **CleverAudio** | Good for team-based annotation | Commercial, unclear NA support |

### Commercial bioacoustics platforms

| Tool | Role |
|---|---|
| **Kaleidoscope Pro** (Wildlife Acoustics) | Auto-ID + manual review. Closed-source models; can use for **validation** but not ideal for generating training labels since their labeling conventions may not match ours. |
| **SonoBat** | NA-specific auto-ID, highly regarded among US bat biologists. Useful as a **cross-check** — if SonoBat and our classifier agree, confidence goes up. |
| **BatExplorer** (Elekon) | European-focused; less useful for our work. |

### Custom annotation pipeline (consider later)

If annotation volume grows beyond what Raven Pro supports efficiently, build a simple web UI on top of the existing dashboard:
- Upload a WAV → show spectrogram
- Click-drag to draw bounding boxes
- Pick species from a dropdown
- Save to a `trainingLabels` Firestore collection

This leverages the Firebase + Vercel infrastructure we already have. ~1 week of dashboard work. Turns the committee into distributed annotators.

---

## Training Approach

### Option A — Fine-tune BatDetect2 from UK weights

- **Pros**: fewer labels needed (~200 bounding boxes could be enough), fast convergence, preserves UK feature-extraction insights that might still generalize.
- **Cons**: can inherit UK bias; may need architecture changes for NA-specific call shapes.
- **Blocker to check first**: the BatDetect2 repo's `finetune/` module was flagged as broken ("This code is currently broken. Will fix soon, stay tuned."). Need to verify current status or implement fine-tuning manually using their detector architecture. The `train_detector.py` script in the repo works for from-scratch training.
- **Compute**: 1-4 hours on an RTX 5090 at Vast.ai (~$0.50-$2).

### Option B — Train from scratch on NA data

- **Pros**: fully NA-calibrated, no inherited bias, full architecture freedom.
- **Cons**: needs ~500-2000 bounding-box labels for a robust model.
- **Compute**: 8-24 hours on an RTX 5090 (~$4-12).
- **Risk**: small-data from-scratch training can overfit. Need careful validation.

### Option C — Transfer learning with architecture adjustment

- Start from BatDetect2 UK weights, add a NA-specific output head, and fine-tune end-to-end with a lower learning rate.
- This is the **research-forward** path. It extends beyond a simple re-training into a methodological contribution (which is what a PhD thesis needs).
- Compute: similar to B; effort in experimentation with architecture variants.

**Recommended sequence:** A first (quick win, validate the data pipeline), then C (research contribution).

---

## Impact on the Existing Classifier Head

The groups classifier head was trained on **UK-BatDetect2 features** (the 32-dim feature vectors produced by the frozen UK detector). If we replace the detector, the features change. The classifier head trained on old features **won't work directly on new features**.

This means retraining the detector implies retraining the classifier head as well. That's fine — both use the same labeled corpus (files with species labels, plus bounding-box annotations). Actual cost:

- Extract new features from NA detector on all labeled files (~1 hour GPU).
- Train classifier head (~10 minutes GPU, we did this in April).
- Validate on held-out set.

Total: <2 hours of additional compute on top of the detector retrain. Not a blocker.

---

## Expected Accuracy Gains

These are **educated guesses** pending actual experiments:

| Metric | Current (UK detector + NA classifier) | After NA-trained detector + retrained classifier |
|---|---|---|
| **Detection recall** on Ohio AudioMoth data | ~40-60% (hard to measure without ground-truth annotations — the 005517 false-negative case suggests major gaps) | 80-90%+ |
| **Groups classifier val accuracy** | 80.0% | 85-92% |
| **PESU specific accuracy** | 83.4% | 87-93% |
| **False positive rate on noise** | Low (4 gates catch it) | Low or lower (detector pre-filters) |

The **detection recall gain is the major win.** Classification accuracy improvements are nice-to-have; detection recall is a step-change because right now we're silently losing bat passes.

---

## Non-Bat Rejection Improvements

A NA-retrained detector with good hard negatives should:

- Pre-reject most silent segments without the audio-level validator ever running (saves compute).
- Pre-reject broadband clicks without the FM-sweep filter (cleaner pipeline).
- Surface "weird acoustic events" for manual review (species not in the 5-class set, novel insect sounds, mechanical noise) because they'd produce low-confidence detector output instead of being mis-fit into a bat class.

The four-gate pipeline we've built still runs after the new detector — but the work it has to do reduces, and the failure modes it catches will be rarer and weirder.

---

## Cost and Timeline

### Realistic timeline (sequential)

| Phase | Duration | Effort |
|---|---|---|
| Labeling tool setup | 1 week | Mustapha |
| Protocol + training docs for annotators | 1 week | Mustapha + Dr. Johnson |
| Pilot annotation (50 files) | 2 weeks | Dr. Johnson or RA |
| Full annotation (500-1000 calls) | 4-8 weeks | Dr. Johnson + RA + possibly Mustapha |
| Model training + iteration | 2-3 weeks | Mustapha |
| Validation on held-out set | 1-2 weeks | Mustapha + Dr. Johnson |
| Deployment to Pi + cloud | 1 week | Mustapha |
| **Total** | **3-5 months elapsed**, **~6-8 weeks active work** | |

### GPU cost

| Activity | GPU-hours | Cost |
|---|---|---|
| Fine-tune BatDetect2 | ~4 | ~$2 |
| From-scratch retraining runs (3-5 iterations) | ~40 | ~$20 |
| Classifier head retraining (multiple models) | ~4 | ~$2 |
| Validation / feature extraction | ~4 | ~$2 |
| **Total** | **~50 GPU-hours** | **~$25-30** |

At Vast.ai rates (~$0.48/hour for RTX 5090). Trivial cost.

---

## Long-Term Hard Rules for a Robust NA Detection Tool

These are durable principles that apply regardless of which detector we're running.

1. **Detector optimizes recall. Classifier + filters optimize precision.** Feed any plausible candidate into the classifier; trust the downstream gates to reject noise. The April 2026 threshold tuning (0.5 → 0.3) is the canonical example — the detector became more permissive, the downstream gates kept the output clean.

2. **Version everything.** Every detection row must carry `model_version` and `pipeline_version`. Changing *any* threshold, weight, or gate constant = bump the version. Never silently mutate pipeline behavior.

3. **Hold out a validation set you never touch.** Pick ~100 diverse Ohio recordings across species, seasons, noise conditions, and time of day. Every pipeline change must be benchmarked against it before it ships. Don't tune on a single file.

4. **Deploy identical pipelines on Pi and cloud.** `bat_pipeline.py` is the single source of truth. Any drift between Pi and cloud `pipeline_version` values = data integrity violation. Check mismatches in the database monthly.

5. **Log every rejection reason.** Each dropped detection records WHY (`validator:rms_too_low`, `shape:broadband_noise`, etc.). Systematic failure modes become visible in aggregate stats.

6. **Actually use the data flywheel.** The ✓/✗/📝 review UI is a training-data generator. Monthly: export `verifiedClass IS NOT NULL` rows. Quarterly: retrain classifier on accumulated reviewed data. Yearly or when labeled data doubles: retrain detector.

7. **Human-in-the-loop for novelty.** When the classifier's max confidence is below 0.5 across all classes, flag for review instead of forcing a label. Unknown species or unusual variants shouldn't get shoehorned into the 5-class output.

8. **Audit false negatives as routinely as false positives.** Review a random sample of "no bat detections" segments weekly. If the spectrogram shows obvious calls, the detector is miscalibrated — tune it.

9. **Every tuning change needs empirical validation.** What we did with the 0.3 threshold (real-file + bad-file + training-corpus tests) is the right pattern. Document every tuning decision with the evidence that motivated it.

10. **Prefer reproducibility over speed.** If a change works in a hotfix, commit the hotfix. Don't leave "I'll clean this up later" state. If a parameter gets tuned live on Pi, it must also land in the repo — future-you (or future-researcher) needs to rebuild from source.

---

## Concrete Next Steps

When ready to kick this off (post-MS defense or whenever Dr. Johnson agrees to carve out time):

1. **Meeting with Dr. Johnson** — scope, data access, annotation commitment. Discuss whether annotation is Dr. Johnson personally, a research assistant, or crowdsourced to committee members through the dashboard.
2. **Pilot** — 50 files, Mustapha labels half, Dr. Johnson labels half, compare consistency. Calibrate the annotation protocol.
3. **Tool decision** — default to Raven Pro unless volume argues for a web-based custom tool.
4. **Training protocol document** — write down exactly which time/frequency bounds count as a bat call, how to handle call sequences vs individual pulses, whether to annotate social calls, etc.
5. **GitHub repo for the training pipeline** — fork `bat-edge-monitor` or create a new repo `bat-edge-monitor-training`. Keep the production pipeline clean.
6. **Vast.ai account setup** — one-time. ~10 minutes.
7. **First training run** — once 50-100 annotations exist, try a fine-tune to validate the plumbing works end-to-end. Don't wait for the full corpus before testing.

---

## Open Questions

- Does Raven Pro export format map cleanly to BatDetect2's training labels? (Needs verification.)
- Is Dr. Johnson's data organized by recording session and site? Training needs to avoid data leakage (same site or session shouldn't span train/val sets).
- Does UC IT have compute we can use before we lean on Vast.ai rentals? (Would be cheaper and might be available for grad-student projects.)
- Any existing Ohio bat acoustic corpora that are already annotated? (e.g., through the Ohio Division of Wildlife or consortia like NABat.) If so, standing on their annotations would jump-start the work.
- Can the annotation effort double as a labeled data contribution to the broader NA bat research community? Publishing a labeled corpus is itself a research output.

---

## References

- Mac Aodha, O. et al. (2022). *Towards a General Approach for Bat Echolocation Detection and Classification.*
- Current BatDetect2 repository + training code: https://github.com/macaodha/batdetect2
- BATDETECT2_TRAINING.md in this repo (training run details, 2026-04-17)
- DATA_FLYWHEEL.md in this repo (continuous-learning architecture)
- DETECTION_TUNING_PLAYBOOK.md in this repo (active pipeline tuning notes — see threshold 0.3 experiment)
