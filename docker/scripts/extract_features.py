"""
Extract BatDetect2 features from labeled bat audio folders.

Usage:
    python extract_features.py                 # extract all species in bat_data/
    python extract_features.py --species Myso  # just Myso
"""
import os
import sys
import argparse
import time
import numpy as np
from batdetect2 import api

DATA_ROOT = '/workspace/bat_data'
OUTPUT_DIR = '/workspace/features'
DET_THRESHOLD = 0.5

os.makedirs(OUTPUT_DIR, exist_ok=True)

def extract_features_from_file(wav_path, threshold=DET_THRESHOLD):
    """Return (features, det_probs) for high-confidence detections, or (None, None)."""
    try:
        audio = api.load_audio(wav_path)
        detections, features, _ = api.process_audio(audio)
    except Exception as e:
        return None, None, str(e)
    
    if len(detections) == 0:
        return np.empty((0, 32)), np.empty(0), None
    
    mask = np.array([d['det_prob'] > threshold for d in detections])
    
    if not mask.any():
        return np.empty((0, 32)), np.empty(0), None
    
    filtered_features = features[mask]
    filtered_probs = np.array([d['det_prob'] for d, m in zip(detections, mask) if m])
    return filtered_features, filtered_probs, None

def process_species(species_name):
    species_dir = os.path.join(DATA_ROOT, species_name)
    if not os.path.isdir(species_dir):
        print(f"[SKIP] {species_name}: directory not found")
        return
    
    wav_files = sorted([f for f in os.listdir(species_dir) if f.endswith('.wav')])
    if not wav_files:
        print(f"[SKIP] {species_name}: no WAV files")
        return
    
    print(f"\n=== Processing {species_name} ({len(wav_files)} files) ===")
    
    all_features = []
    all_probs = []
    all_source_files = []
    errors = []
    t_start = time.time()
    
    for i, wav in enumerate(wav_files, 1):
        wav_path = os.path.join(species_dir, wav)
        feats, probs, err = extract_features_from_file(wav_path)
        
        if err:
            errors.append((wav, err))
            continue
        
        if len(feats) > 0:
            all_features.append(feats)
            all_probs.append(probs)
            all_source_files.extend([wav] * len(feats))
        
        if i % 25 == 0 or i == len(wav_files):
            elapsed = time.time() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            n_dets = sum(len(f) for f in all_features)
            print(f"  [{i}/{len(wav_files)}] {rate:.1f} files/sec, {n_dets} detections so far")
    
    if not all_features:
        print(f"[WARN] {species_name}: no high-confidence detections found")
        return
    
    X = np.vstack(all_features)
    probs = np.concatenate(all_probs)
    sources = np.array(all_source_files)
    labels = np.array([species_name] * len(X))
    
    output_path = os.path.join(OUTPUT_DIR, f'{species_name}.npz')
    np.savez_compressed(
        output_path,
        features=X,
        labels=labels,
        det_probs=probs,
        source_files=sources
    )
    
    elapsed = time.time() - t_start
    print(f"[DONE] {species_name}: {len(X)} detections from {len(wav_files)} files in {elapsed:.1f}s")
    print(f"       Saved to {output_path} ({os.path.getsize(output_path)/1024/1024:.1f} MB)")
    
    if errors:
        print(f"[WARN] {len(errors)} files had errors (first 3):")
        for w, e in errors[:3]:
            print(f"       {w}: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--species', help='Process only this species (default: all)')
    args = parser.parse_args()
    
    if args.species:
        process_species(args.species)
    else:
        # Discover all species folders
        species_list = sorted([
            d for d in os.listdir(DATA_ROOT)
            if os.path.isdir(os.path.join(DATA_ROOT, d))
        ])
        print(f"Found {len(species_list)} species folders: {species_list}")
        
        for sp in species_list:
            process_species(sp)
    
    # Summary of output files
    print("\n=== Extraction complete. Summary: ===")
    for f in sorted(os.listdir(OUTPUT_DIR)):
        path = os.path.join(OUTPUT_DIR, f)
        data = np.load(path)
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"  {f}: {len(data['features'])} detections, {size_mb:.1f} MB")

if __name__ == '__main__':
    main()
