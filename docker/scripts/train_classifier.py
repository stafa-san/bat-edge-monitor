"""
Train a classifier head on BatDetect2-extracted features.

Usage:
    python train_classifier.py --scheme species    # 9-class species-level
    python train_classifier.py --scheme groups     # 5-class Dr. Johnson grouping
    python train_classifier.py --scheme frequency  # 2-class low/high freq
    python train_classifier.py --all               # train all 3
"""
import os
import sys
import argparse
import json
import time
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import classification_report, confusion_matrix

FEATURES_DIR = '/workspace/features'
MODELS_DIR = '/workspace/models'
LOGS_DIR = '/workspace/logs'
os.makedirs(MODELS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

LABEL_SCHEMES = {
    'species': {
        'Epfu': 'Epfu', 'Labo': 'Labo', 'Laci': 'Laci', 'Lano': 'Lano',
        'Myle': 'Myle', 'Mylu': 'Mylu', 'Myse': 'Myse', 'Myso': 'Myso',
        'Pesu': 'Pesu',
    },
    'groups': {
        'Epfu': 'EPFU_LANO', 'Lano': 'EPFU_LANO',
        'Laci': 'LACI',
        'Myle': 'MYSP', 'Mylu': 'MYSP', 'Myse': 'MYSP', 'Myso': 'MYSP',
        'Labo': 'LABO',
        'Pesu': 'PESU',
    },
    'frequency': {
        'Epfu': 'LowFreq', 'Lano': 'LowFreq', 'Laci': 'LowFreq',
        'Myle': 'HighFreq', 'Mylu': 'HighFreq', 'Myse': 'HighFreq',
        'Myso': 'HighFreq', 'Labo': 'HighFreq', 'Pesu': 'HighFreq',
    },
}

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

def load_all_features():
    data = {}
    for f in sorted(os.listdir(FEATURES_DIR)):
        if not f.endswith('.npz'):
            continue
        species = f[:-4]
        loaded = np.load(os.path.join(FEATURES_DIR, f))
        data[species] = {
            'features': loaded['features'],
            'source_files': loaded['source_files'] if 'source_files' in loaded.files else None,
            'det_probs': loaded['det_probs'] if 'det_probs' in loaded.files else None,
        }
        print(f"  Loaded {species}: {len(data[species]['features'])} detections")
    return data

def build_dataset(species_data, label_scheme):
    X_parts, y_parts, source_species_parts, source_files_parts = [], [], [], []
    for species, entry in species_data.items():
        if species not in label_scheme:
            print(f"  [WARN] {species} not in label scheme, skipping")
            continue
        target_label = label_scheme[species]
        X_parts.append(entry['features'])
        y_parts.extend([target_label] * len(entry['features']))
        source_species_parts.extend([species] * len(entry['features']))
        if entry['source_files'] is not None:
            source_files_parts.extend(entry['source_files'].tolist())
        else:
            source_files_parts.extend([''] * len(entry['features']))
    X = np.vstack(X_parts)
    y_labels = np.array(y_parts)
    source_species = np.array(source_species_parts)
    source_files = np.array(source_files_parts)
    class_names = sorted(set(y_labels))
    class_to_idx = {c: i for i, c in enumerate(class_names)}
    y = np.array([class_to_idx[lbl] for lbl in y_labels])
    return X, y, class_names, source_species, source_files

def train_one(scheme_name, label_scheme, species_data, epochs=50, batch_size=256, lr=1e-3, patience=8):
    print(f"\n{'='*70}\nTRAINING SCHEME: {scheme_name.upper()}\n{'='*70}")
    X, y, class_names, source_species, source_files = build_dataset(species_data, label_scheme)
    print(f"\nTotal examples: {len(X)}")
    print(f"Classes ({len(class_names)}): {class_names}")
    print(f"\nClass distribution:")
    for i, c in enumerate(class_names):
        count = (y == i).sum()
        pct = 100 * count / len(y)
        contributing = sorted(set(source_species[y == i]))
        print(f"  {c}: {count} ({pct:.1f}%) from species {contributing}")
    
    unique_files = sorted(set(source_files))
    file_to_class = {}
    for fname in unique_files:
        mask = source_files == fname
        cls = np.bincount(y[mask]).argmax()
        file_to_class[fname] = cls
    file_array = np.array(unique_files)
    file_classes = np.array([file_to_class[f] for f in unique_files])
    
    from collections import Counter
    class_file_counts = Counter(file_classes)
    min_files = min(class_file_counts.values())
    if min_files < 2:
        print(f"\n[WARN] Some classes have <2 files. Using detection-level split.")
        X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    else:
        train_files, val_files = train_test_split(file_array, test_size=0.2, random_state=42, stratify=file_classes)
        train_mask = np.isin(source_files, train_files)
        val_mask = np.isin(source_files, val_files)
        X_train, X_val = X[train_mask], X[val_mask]
        y_train, y_val = y[train_mask], y[val_mask]
        print(f"\nFile-level split: {len(train_files)} train files, {len(val_files)} val files")
    print(f"Train: {len(X_train)}, Val: {len(X_val)}")
    
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_val = scaler.transform(X_val)
    
    class_counts = np.bincount(y_train, minlength=len(class_names))
    class_weights = len(y_train) / (len(class_names) * class_counts.clip(min=1))
    class_weights = torch.tensor(class_weights, dtype=torch.float32)
    print(f"\nClass weights: {dict(zip(class_names, class_weights.numpy().round(3)))}")
    
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    class_weights = class_weights.to(device)
    
    train_ds = TensorDataset(torch.tensor(X_train, dtype=torch.float32), torch.tensor(y_train, dtype=torch.long))
    val_ds = TensorDataset(torch.tensor(X_val, dtype=torch.float32), torch.tensor(y_val, dtype=torch.long))
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, num_workers=2)
    val_loader = DataLoader(val_ds, batch_size=batch_size, num_workers=2)
    
    model = BatClassifierHead(input_dim=X_train.shape[1], hidden_dims=(128, 64), num_classes=len(class_names), dropout=0.3).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"\nModel: {n_params:,} parameters")
    
    criterion = nn.CrossEntropyLoss(weight=class_weights)
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=3)
    
    best_val_loss = float('inf')
    best_state = None
    best_preds, best_targets = None, None
    patience_counter = 0
    history = []
    t_start = time.time()
    
    for epoch in range(1, epochs + 1):
        model.train()
        train_loss, train_correct, train_total = 0.0, 0, 0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            logits = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * xb.size(0)
            train_correct += (logits.argmax(1) == yb).sum().item()
            train_total += xb.size(0)
        train_loss /= train_total
        train_acc = train_correct / train_total
        
        model.eval()
        val_loss, val_correct, val_total = 0.0, 0, 0
        all_preds, all_targets = [], []
        with torch.no_grad():
            for xb, yb in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                logits = model(xb)
                loss = criterion(logits, yb)
                val_loss += loss.item() * xb.size(0)
                preds = logits.argmax(1)
                val_correct += (preds == yb).sum().item()
                val_total += xb.size(0)
                all_preds.extend(preds.cpu().numpy())
                all_targets.extend(yb.cpu().numpy())
        val_loss /= val_total
        val_acc = val_correct / val_total
        
        scheduler.step(val_loss)
        current_lr = optimizer.param_groups[0]['lr']
        history.append({'epoch': epoch, 'train_loss': train_loss, 'train_acc': train_acc, 'val_loss': val_loss, 'val_acc': val_acc, 'lr': current_lr})
        
        improved = val_loss < best_val_loss
        marker = ' *' if improved else ''
        print(f"  Epoch {epoch:3d}: train_loss={train_loss:.4f} train_acc={train_acc:.3f}  val_loss={val_loss:.4f} val_acc={val_acc:.3f}  lr={current_lr:.1e}{marker}")
        
        if improved:
            best_val_loss = val_loss
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            best_preds = all_preds
            best_targets = all_targets
            patience_counter = 0
        else:
            patience_counter += 1
            if patience_counter >= patience:
                print(f"  Early stopping at epoch {epoch}")
                break
    
    elapsed = time.time() - t_start
    print(f"\nTraining complete in {elapsed:.1f}s. Best val_loss: {best_val_loss:.4f}")
    
    print(f"\n=== Final Classification Report ({scheme_name}) ===")
    print(classification_report(best_targets, best_preds, target_names=class_names, zero_division=0))
    
    print(f"\n=== Confusion Matrix ({scheme_name}) ===")
    cm = confusion_matrix(best_targets, best_preds)
    header = "TRUE\\PRED  " + "  ".join(f"{c[:8]:>8}" for c in class_names)
    print(header)
    for i, c in enumerate(class_names):
        row = "  ".join(f"{cm[i,j]:>8d}" for j in range(len(class_names)))
        print(f"{c[:8]:<10} {row}")
    
    output = {
        'state_dict': best_state,
        'scaler_mean': scaler.mean_.tolist(),
        'scaler_scale': scaler.scale_.tolist(),
        'class_names': class_names,
        'label_scheme': label_scheme,
        'scheme_name': scheme_name,
        'input_dim': X_train.shape[1],
        'hidden_dims': (128, 64),
        'num_classes': len(class_names),
    }
    model_path = os.path.join(MODELS_DIR, f'{scheme_name}_model.pt')
    torch.save(output, model_path)
    
    log_path = os.path.join(LOGS_DIR, f'{scheme_name}_training.json')
    with open(log_path, 'w') as fp:
        json.dump({
            'scheme': scheme_name,
            'class_names': class_names,
            'best_val_loss': best_val_loss,
            'epochs_trained': len(history),
            'history': history,
            'confusion_matrix': cm.tolist(),
            'classification_report': classification_report(best_targets, best_preds, target_names=class_names, zero_division=0, output_dict=True),
        }, fp, indent=2)
    print(f"\nSaved model to {model_path}")
    print(f"Saved training log to {log_path}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scheme', choices=list(LABEL_SCHEMES.keys()), default=None)
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--batch_size', type=int, default=256)
    parser.add_argument('--lr', type=float, default=1e-3)
    args = parser.parse_args()
    
    print("=== Loading features ===")
    species_data = load_all_features()
    print(f"Total species loaded: {len(species_data)}")
    
    if args.all:
        schemes = LABEL_SCHEMES.keys()
    elif args.scheme:
        schemes = [args.scheme]
    else:
        print("ERROR: specify --scheme {species|groups|frequency} or --all")
        sys.exit(1)
    
    for scheme_name in schemes:
        train_one(scheme_name, LABEL_SCHEMES[scheme_name], species_data, epochs=args.epochs, batch_size=args.batch_size, lr=args.lr)
    print("\n=== ALL TRAINING COMPLETE ===")

if __name__ == '__main__':
    main()
