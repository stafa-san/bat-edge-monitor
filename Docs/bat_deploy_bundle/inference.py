"""
Run a trained classifier on new audio files.
"""
import os
import argparse
import json
import numpy as np
import torch
import torch.nn as nn
from batdetect2 import api

DET_THRESHOLD = 0.5

class BatClassifierHead(nn.Module):
    def __init__(self, input_dim=32, hidden_dims=(128, 64), num_classes=9, dropout=0.3):
        super().__init__()
        layers = []
        prev = input_dim
        for h in hidden_dims:
            layers += [nn.Linear(prev, h), nn.ReLU(), nn.BatchNorm1d(h), nn.Dropout(dropout)]
            prev = h
        layers.append(nn.Linear(prev, num_classes))
        self.net = nn.Sequential(*layers)
    def forward(self, x):
        return self.net(x)

def load_model(model_path):
    ckpt = torch.load(model_path, weights_only=False, map_location='cpu')
    model = BatClassifierHead(
        input_dim=ckpt['input_dim'],
        hidden_dims=tuple(ckpt['hidden_dims']),
        num_classes=ckpt['num_classes'],
        dropout=0.0,
    )
    model.load_state_dict(ckpt['state_dict'])
    model.eval()
    return model, ckpt

def predict_file(wav_path, model, ckpt, threshold=DET_THRESHOLD):
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = model.to(device)
    audio = api.load_audio(wav_path)
    detections, features, _ = api.process_audio(audio)
    if len(detections) == 0:
        return []
    mask = np.array([d['det_prob'] > threshold for d in detections])
    if not mask.any():
        return []
    high_conf_feats = features[mask]
    high_conf_dets = [d for d, m in zip(detections, mask) if m]
    mean = np.array(ckpt['scaler_mean'])
    scale = np.array(ckpt['scaler_scale'])
    feats_norm = (high_conf_feats - mean) / scale
    with torch.no_grad():
        x = torch.tensor(feats_norm, dtype=torch.float32).to(device)
        logits = model(x)
        probs = torch.softmax(logits, dim=1).cpu().numpy()
        preds = probs.argmax(axis=1)
    class_names = ckpt['class_names']
    results = []
    for i, d in enumerate(high_conf_dets):
        results.append({
            'start_time': d['start_time'],
            'end_time': d['end_time'],
            'predicted_class': class_names[preds[i]],
            'confidence': float(probs[i][preds[i]]),
        })
    return results

def summarize(results, file_name):
    from collections import Counter
    if not results:
        print(f"{file_name}: 0 detections")
        return
    classes = [r['predicted_class'] for r in results]
    counts = Counter(classes)
    mean_conf = np.mean([r['confidence'] for r in results])
    print(f"{file_name}: {len(results)} dets, mean_conf={mean_conf:.3f}")
    for c, n in counts.most_common():
        print(f"  {c}: {n} ({100*n/len(results):.1f}%)")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--audio')
    parser.add_argument('--audio_dir')
    parser.add_argument('--output_json')
    args = parser.parse_args()
    
    print(f"Loading model from {args.model}")
    model, ckpt = load_model(args.model)
    print(f"Model: {ckpt['scheme_name']} ({ckpt['num_classes']} classes)")
    print(f"Classes: {ckpt['class_names']}\n")
    
    all_results = {}
    if args.audio:
        results = predict_file(args.audio, model, ckpt)
        summarize(results, os.path.basename(args.audio))
        all_results[os.path.basename(args.audio)] = results
    elif args.audio_dir:
        wav_files = sorted([f for f in os.listdir(args.audio_dir) if f.endswith('.wav')])
        print(f"Processing {len(wav_files)} files from {args.audio_dir}\n")
        for f in wav_files:
            results = predict_file(os.path.join(args.audio_dir, f), model, ckpt)
            summarize(results, f)
            all_results[f] = results
        
        # Aggregate totals
        print(f"\n=== AGGREGATE ACROSS {len(wav_files)} FILES ===")
        from collections import Counter
        all_preds = [r['predicted_class'] for fs in all_results.values() for r in fs]
        if all_preds:
            counts = Counter(all_preds)
            total = len(all_preds)
            print(f"Total detections: {total}")
            for c, n in counts.most_common():
                print(f"  {c}: {n} ({100*n/total:.1f}%)")
    
    if args.output_json:
        with open(args.output_json, 'w') as fp:
            json.dump(all_results, fp, indent=2, default=str)
        print(f"\nSaved to {args.output_json}")

if __name__ == '__main__':
    main()
