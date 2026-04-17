"""Groups classifier head for North American bat species.

Runs on top of BatDetect2's frozen 32-dim feature vectors. Replaces the
raw BatDetect2 species labels (which are UK-trained and misclassify all
Ohio bats as European species) with Dr. Johnson's 5-class taxonomic
grouping: EPFU_LANO, LABO, LACI, MYSP, PESU.

See BATDETECT2_TRAINING.md and Docs/bat_deploy_bundle/inference.py for
the training-side reference implementation this is ported from.
"""

import numpy as np
import torch
import torch.nn as nn


class BatClassifierHead(nn.Module):
    """Tiny MLP head — ~13k params, runs fine on the Pi CPU."""

    def __init__(self, input_dim=32, hidden_dims=(128, 64), num_classes=5, dropout=0.0):
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


def load_groups_classifier(model_path):
    """Load a checkpoint produced by train_classifier.py.

    Returns (model, ckpt) where ckpt holds class_names, scaler_mean, and
    scaler_scale — all needed at inference time.
    """
    ckpt = torch.load(model_path, weights_only=False, map_location="cpu")
    model = BatClassifierHead(
        input_dim=ckpt["input_dim"],
        hidden_dims=tuple(ckpt["hidden_dims"]),
        num_classes=ckpt["num_classes"],
        dropout=0.0,
    )
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    return model, ckpt


def classify(features, model, ckpt):
    """Run the classifier on an (N, 32) feature matrix.

    Returns a list of {predicted_class, prediction_confidence} dicts,
    one per input row, in the same order as features.
    """
    if len(features) == 0:
        return []

    mean = np.asarray(ckpt["scaler_mean"])
    scale = np.asarray(ckpt["scaler_scale"])
    feats_norm = (np.asarray(features) - mean) / scale

    with torch.no_grad():
        x = torch.tensor(feats_norm, dtype=torch.float32)
        logits = model(x)
        probs = torch.softmax(logits, dim=1).numpy()
        preds = probs.argmax(axis=1)

    class_names = ckpt["class_names"]
    return [
        {
            "predicted_class": class_names[preds[i]],
            "prediction_confidence": float(probs[i][preds[i]]),
        }
        for i in range(len(preds))
    ]
