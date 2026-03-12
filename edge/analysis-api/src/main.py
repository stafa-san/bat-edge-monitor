"""
Analysis API — Channel 2: Offline .wav file analysis.

Accepts .wav uploads via POST /analyze and runs both:
  • AST (Audio Spectrogram Transformer) — general soundscape classification
  • BatDetect2 — bat echolocation detection

Results are written to the same PostgreSQL tables (with source='upload')
so they flow through the existing sync → Firestore → dashboard pipeline.
"""

import os
import uuid
import warnings
from datetime import datetime
from tempfile import NamedTemporaryFile
from typing import Optional

import librosa
import numpy as np
import psycopg2
import soundfile as sf
from fastapi import FastAPI, File, HTTPException, UploadFile
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
#  App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Bat Edge Monitor — Analysis API",
    description="Upload .wav files for AST + BatDetect2 analysis",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
#  Lazy-loaded models (saves RAM until first request)
# ---------------------------------------------------------------------------

_ast_classifier = None
_bat_config = None


def get_ast_classifier():
    """Load AST model on first use."""
    global _ast_classifier
    if _ast_classifier is None:
        import torch
        from transformers import ASTForAudioClassification, AutoFeatureExtractor

        print("[ANALYSIS] Loading AST model...")
        model_name = "MIT/ast-finetuned-audioset-10-10-0.4593"
        _ast_classifier = {
            "model": ASTForAudioClassification.from_pretrained(model_name),
            "extractor": AutoFeatureExtractor.from_pretrained(model_name),
        }
        print("[ANALYSIS] AST model ready")
    return _ast_classifier


def get_bat_config():
    """Load BatDetect2 config on first use."""
    global _bat_config
    if _bat_config is None:
        from batdetect2 import api as bat_api

        print("[ANALYSIS] Loading BatDetect2 model...")
        _bat_config = bat_api.get_config()
        threshold = float(os.getenv("DETECTION_THRESHOLD", "0.3"))
        _bat_config["detection_threshold"] = threshold
        print("[ANALYSIS] BatDetect2 model ready")
    return _bat_config


# ---------------------------------------------------------------------------
#  Database
# ---------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "soundscape"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


# ---------------------------------------------------------------------------
#  AST classification (1-second windows)
# ---------------------------------------------------------------------------

def run_ast(audio: np.ndarray, orig_sr: int, top_k: int = 5):
    """Run AST on 1-second windows, return list of classification dicts."""
    import torch
    from maad.spl import wav2dBSPL
    from maad.util import mean_dB

    ast = get_ast_classifier()
    model = ast["model"]
    extractor = ast["extractor"]
    target_sr = extractor.sampling_rate  # 16 000

    # Resample full audio to 16 kHz once
    audio_16k = librosa.resample(audio, orig_sr=orig_sr, target_sr=target_sr)

    window = target_sr  # 1 second
    n_windows = max(1, len(audio_16k) // window)

    results = []
    for i in range(n_windows):
        chunk = audio_16k[i * window : (i + 1) * window]
        if len(chunk) < window // 2:
            continue

        # Classify
        with torch.no_grad():
            inputs = extractor(chunk, sampling_rate=target_sr, return_tensors="pt")
            logits = model(**inputs).logits[0]
            proba = torch.sigmoid(logits)
            top_indices = torch.argsort(proba)[-top_k:].flip(dims=(0,)).tolist()

        # SPL from the original-rate chunk
        orig_window = orig_sr  # 1 second in original samples
        orig_chunk = audio[i * orig_window : (i + 1) * orig_window]
        spl = _safe_spl(orig_chunk)

        for idx in top_indices:
            results.append({
                "time_offset_s": i,
                "label": model.config.id2label[idx],
                "score": round(proba[idx].item(), 4),
                "spl": round(spl, 1),
            })

    return results


def _safe_spl(audio: np.ndarray, gain: float = 25, sensitivity: float = -18) -> float:
    from maad.spl import wav2dBSPL
    from maad.util import mean_dB

    audio_safe = np.where(audio == 0, 1e-10, audio)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        x = wav2dBSPL(audio_safe, gain=gain, sensitivity=sensitivity, Vadc=1.25)
        return float(mean_dB(x, axis=0))


# ---------------------------------------------------------------------------
#  BatDetect2 analysis (full file)
# ---------------------------------------------------------------------------

def run_batdetect(wav_path: str):
    """Run BatDetect2 on the wav file, return list of detection dicts."""
    from batdetect2 import api as bat_api

    config = get_bat_config()
    results = bat_api.process_file(wav_path, config=config)

    pred_dict = results.get("pred_dict", {})
    detections = pred_dict.get("annotation", [])

    out = []
    for det in detections:
        species = det.get("class", "Unknown")
        start = det.get("start_time", 0.0)
        end = det.get("end_time", 0.0)
        out.append({
            "species": species,
            "common_name": species,
            "detection_prob": round(det.get("det_prob", 0.0), 4),
            "start_time": round(start, 4),
            "end_time": round(end, 4),
            "low_freq": round(det.get("low_freq", 0.0), 1),
            "high_freq": round(det.get("high_freq", 0.0), 1),
            "duration_ms": round((end - start) * 1000, 1),
        })
    return out


# ---------------------------------------------------------------------------
#  API endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "models_loaded": {
        "ast": _ast_classifier is not None,
        "batdetect2": _bat_config is not None,
    }}


@app.post("/analyze")
async def analyze(
    file: UploadFile = File(...),
    run_ast_model: bool = True,
    run_batdetect_model: bool = True,
    top_k: int = 5,
    device_label: Optional[str] = "upload",
):
    """Upload a .wav file for AST + BatDetect2 analysis.

    Results are stored in PostgreSQL and will sync to Firestore
    on the next sync-service cycle (≤ 60 s).
    """
    if not file.filename or not file.filename.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="Only .wav files are accepted")

    # Save upload to temp file
    with NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Read audio
        audio, sr = sf.read(tmp_path, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)  # stereo → mono

        duration_s = len(audio) / sr
        sync_id = str(uuid.uuid4())
        now = datetime.utcnow()

        response = {
            "filename": file.filename,
            "sample_rate": sr,
            "duration_seconds": round(duration_s, 2),
            "channels": "mono",
            "sync_id": sync_id,
            "ast_classifications": [],
            "bat_detections": [],
            "summary": {},
        }

        conn = get_db_connection()

        # ── AST ──
        if run_ast_model:
            print(f"[ANALYSIS] Running AST on {file.filename} ({duration_s:.1f}s, {sr} Hz)")
            ast_results = run_ast(audio, orig_sr=sr, top_k=top_k)
            response["ast_classifications"] = ast_results

            # Store in DB
            rows = [
                (r["label"], r["score"], r["spl"], device_label, sync_id, now, "upload")
                for r in ast_results
            ]
            if rows:
                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO classifications
                            (label, score, spl, device, sync_id, sync_time, source)
                        VALUES %s
                    """, rows)
                conn.commit()
                print(f"[ANALYSIS] AST: {len(rows)} classifications stored")

        # ── BatDetect2 ──
        if run_batdetect_model:
            print(f"[ANALYSIS] Running BatDetect2 on {file.filename}")
            bat_results = run_batdetect(tmp_path)
            response["bat_detections"] = bat_results

            if bat_results:
                rows = [
                    (
                        d["species"], d["common_name"], d["detection_prob"],
                        d["start_time"], d["end_time"], d["low_freq"],
                        d["high_freq"], d["duration_ms"],
                        device_label, sync_id, now, "upload",
                    )
                    for d in bat_results
                ]
                with conn.cursor() as cur:
                    execute_values(cur, """
                        INSERT INTO bat_detections
                            (species, common_name, detection_prob, start_time,
                             end_time, low_freq, high_freq, duration_ms,
                             device, sync_id, detection_time, source)
                        VALUES %s
                    """, rows)
                conn.commit()
                print(f"[ANALYSIS] BatDetect2: {len(bat_results)} detections stored")

        conn.close()

        # ── Summary ──
        n_segments = len(set(r["time_offset_s"] for r in response["ast_classifications"])) if response["ast_classifications"] else 0
        species_found = list(set(d["species"] for d in response["bat_detections"]))
        response["summary"] = {
            "ast_segments_analysed": n_segments,
            "ast_total_classifications": len(response["ast_classifications"]),
            "bat_detections_count": len(response["bat_detections"]),
            "bat_species_found": species_found,
            "stored_in_db": True,
            "will_sync_to_cloud": True,
        }

        print(f"[ANALYSIS] Done: {n_segments} AST segments, {len(response['bat_detections'])} bat detections")
        return response

    except Exception as e:
        print(f"[ANALYSIS] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)


@app.get("/")
def root():
    return {
        "service": "Bat Edge Monitor — Analysis API",
        "version": "1.0.0",
        "endpoints": {
            "POST /analyze": "Upload a .wav file for analysis",
            "GET /health": "Service health check",
        },
    }
