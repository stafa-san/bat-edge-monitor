# 🦇 BatDetect2 — Species Classification for Ohio

**Date:** 12 March 2026  
**Location:** Ohio, USA  
**Current model:** BatDetect2 (pre-trained on UK/European species)

---

## Current Status

### ✅ What works now
- **Bat call detection** — the model reliably detects ultrasonic echolocation pulses regardless of species or geography. It correctly identifies *that a bat is present*.
- **Acoustic features** — every detection records `low_freq`, `high_freq`, `duration_ms`, and `detection_prob`, which are accurate and species-independent.
- **Activity monitoring** — the system tracks *when* and *how often* bats are active at the site, which is immediately useful without species IDs.

### ❌ What doesn't work
- **Species identification** — the model was trained exclusively on 17 UK/European bat species. It maps Ohio bat calls to the closest European match. For example:
  - Ohio's Big Brown Bat (*Eptesicus fuscus*) → labelled as *Eptesicus serotinus* (European Serotine)
  - Other Ohio species get similarly misidentified
- The species label is **wrong**, but the detection itself (presence, frequency, timing) is **real and accurate**.

---

## Ohio Bat Species to Target

| Species | Scientific Name | Status | Call Frequency | Notes |
|---------|----------------|--------|---------------|-------|
| Big brown bat | *Eptesicus fuscus* | Common | 25–30 kHz | Year-round resident, most likely detection |
| Little brown bat | *Myotis lucifugus* | Declining (WNS) | 40–80 kHz | Severely impacted by White-nose Syndrome |
| Indiana bat | *Myotis sodalis* | **Federally endangered** | 35–55 kHz | High conservation value |
| Northern long-eared bat | *Myotis septentrionalis* | **Federally threatened** | 30–60 kHz | High conservation value |
| Eastern red bat | *Lasiurus borealis* | Common | 35–50 kHz | Migratory, tree-roosting |
| Hoary bat | *Lasiurus cinereus* | Common | 20–30 kHz | Largest Ohio bat, migratory |
| Silver-haired bat | *Lasionycteris noctivagans* | Common | 25–35 kHz | Migratory, spring/fall |
| Tri-colored bat | *Perimyotis subflavus* | Declining (WNS) | 40–50 kHz | Proposed for endangered listing |
| Evening bat | *Nycticeius humeralis* | Uncommon in OH | 25–30 kHz | Southern Ohio mostly |

**WNS** = White-nose Syndrome  
**Ohio bat season:** April–September (peak activity), some year-round residents (Big brown bat)

---

## Options for Improving Species Classification

### Option 1: Use as-is for detection only (no effort)
Keep the current setup. Ignore the species label and treat every detection as "bat present." The acoustic features (`low_freq`, `high_freq`, `duration_ms`) are still collected and can be analysed later for manual species ID or model training.

**Pros:** No work needed, already deployed and running  
**Cons:** No automated species identification

---

### Option 2: Fine-tune BatDetect2 on North American data (moderate effort)

BatDetect2 supports fine-tuning. The approach keeps the detection backbone (which already works) and retrains only the classification head for Ohio species.

#### What you need
1. **Labelled recordings** — 50–200 annotated calls per target species
   - **NABat** (North American Bat Monitoring Program) — USGS national dataset
   - **Macaulay Library / Bat Library** (Cornell Lab of Ornithology) — free recordings
   - **Your own recordings** — accumulate over spring/summer 2026 at your site
   - **Anabat Insight / Kaleidoscope** — commercial tools that have reference call libraries

2. **Annotation format** — BatDetect2 uses JSON annotations with:
   - Start/end time of each call
   - Low/high frequency bounds
   - Species label

3. **Training hardware** — a machine with a GPU (not the Raspberry Pi)
   - Google Colab (free tier has GPU)
   - Local laptop/desktop with NVIDIA GPU
   - Training takes minutes to hours depending on dataset size

#### Fine-tuning steps
```
1. Collect labelled .wav files for each Ohio species
2. Format annotations in BatDetect2's JSON schema
3. Clone BatDetect2 repo: https://github.com/macaodha/batdetect2
4. Follow their fine-tuning guide to retrain the classifier head
5. Export the new model weights (.pth file)
6. Copy the weights into the batdetect-service Docker image
7. Rebuild: docker compose up -d --build batdetect-service
```

**Pros:** Best accuracy, uses your existing pipeline, model is already optimized for edge deployment  
**Cons:** Requires collecting and annotating training data

---

### Option 3: Use a North American classifier instead of or alongside BatDetect2

| Tool | Source | Notes |
|------|--------|-------|
| **BattyBirdNET** | Adaptation of BirdNET for NA bats | Built on BirdNET's architecture, pre-trained for North American species |
| **OpenSoundscape** | Pittsburgh University | Supports custom classifiers, has NA bat examples, Python-based |
| **NABat ML tools** | USGS | Classifiers being developed for the NABat national monitoring program |
| **Kaleidoscope Pro** | Wildlife Acoustics | Commercial, auto-ID for NA bats, not open-source |
| **SonoBat** | SonoBat Inc. | Commercial, widely used for NA bat surveys |

Any of these could be integrated as a second classification step in the pipeline — run BatDetect2 for detection, then pass detected segments to an NA classifier for species ID.

**Pros:** Pre-trained for North American species, no training needed  
**Cons:** Integration work, may require different audio formats or dependencies

---

## Recommended Approach

### Phase 1 — Now (already done)
Deploy as-is. Accumulate detections with raw acoustic features through the 2026 bat season (April–September). The system is collecting all the data needed for future training:
- Audio segments (when `UPLOAD_BAT_AUDIO=true`)
- Frequency ranges, durations, detection probabilities
- Timestamps and activity patterns

### Phase 2 — Summer 2026
- Review accumulated detections and audio clips
- Cross-reference with NABat reference calls to manually label species
- Investigate BattyBirdNET or OpenSoundscape for a quick NA species classifier

### Phase 3 — Fall 2026
- Fine-tune BatDetect2 (or train a custom classifier) on your labelled Ohio data
- Deploy the updated model to the Pi
- Validate against known species at your site

---

## Useful Resources

- **BatDetect2 repo:** https://github.com/macaodha/batdetect2
- **NABat program:** https://sciencebase.usgs.gov/nabat/
- **Macaulay Library (bat recordings):** https://www.macaulaylibrary.org
- **Ohio bat species guide:** https://ohiodnr.gov/discover-and-learn/animals/mammals/bats
- **White-nose Syndrome tracker:** https://www.whitenosesyndrome.org
- **BirdNET (parent of BattyBirdNET):** https://birdnet.cornell.edu
- **OpenSoundscape docs:** https://opensoundscape.org
