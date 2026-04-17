# Bat Classifier Deploy Bundle

Trained: 2026-04-17 on Vast.ai RTX 5090
Training data: 279,019 detections from 9 species (8 full + Epfu partial 1621/3957)

## Models

| File | Classes | Val Acc | Use case |
|------|---------|---------|----------|
| groups_model.pt | 5 (EPFU_LANO, LABO, LACI, MYSP, PESU) | 80.0% | **PRODUCTION** |
| frequency_model.pt | 2 (HighFreq, LowFreq) | 91.1% | Sanity baseline |
| species_model.pt | 9 (Epfu..Pesu) | 65.0% | Thesis comparison |

All models share the BatClassifierHead architecture:
- Input: 32-dim features from BatDetect2 api.process_audio()
- Hidden: Linear(128) -> ReLU -> BatchNorm -> Dropout(0.3) -> Linear(64) -> same
- Output: N classes (softmax)
- ~13k params total, fits easily on Pi CPU

## Integration

See `BATDETECT2_TRAINING.md` in repo root for full Pi integration guide.
See `inference.py` for reference Python implementation.

Short version:
1. Load groups_model.pt
2. Run audio through batdetect2.api.process_audio()
3. Filter detections by det_prob > 0.5
4. Standardize features with scaler_mean/scaler_scale from checkpoint
5. Run through model, apply softmax, take argmax
6. Write class + confidence to PostgreSQL/Firestore

## Thesis money shot

Same 16 Pesu files through:
- UK-trained BatDetect2 alone: 0% PESU (100% European species)
- This groups model: 83.4% PESU (mean confidence 0.856)

See `predictions/pesu_final.json` for per-file breakdown.

## Known caveats

- Trained at 192 kHz; Pi currently records at 250 kHz — consider reverting Pi
- Epfu at 41% of available data (1621/3957) due to OneDrive zip corruption
- Pesu underrepresented (16 files) — more recordings would help
- BatDetect2 det_prob on Ohio bats often <0.1; we filter at 0.5 as a compromise
