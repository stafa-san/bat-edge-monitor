import numpy as np
import pandas as pd
import torch
from transformers import AutoFeatureExtractor, ASTForAudioClassification


class AudioClassifier:
    def __init__(self, pretrained_ast: str = "MIT/ast-finetuned-audioset-10-10-0.4593"):
        self.model = ASTForAudioClassification.from_pretrained(pretrained_ast)
        self.feature_extractor = AutoFeatureExtractor.from_pretrained(pretrained_ast)
        self.sampling_rate = self.feature_extractor.sampling_rate

    async def predict(self, audio: np.ndarray, top_k: int = 5) -> pd.DataFrame:
        with torch.no_grad():
            inputs = self.feature_extractor(
                audio, sampling_rate=self.sampling_rate, return_tensors='pt'
            )
            logits = self.model(**inputs).logits[0]
            proba = torch.sigmoid(logits)
            top_k_indices = torch.argsort(proba)[-top_k:].flip(dims=(0,)).tolist()

            return pd.DataFrame({
                'label': [self.model.config.id2label[i] for i in top_k_indices],
                'score': [proba[i].item() for i in top_k_indices],
            })
