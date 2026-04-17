# Bat classifier training environment
# Reproduces the Vast.ai training setup on any NVIDIA GPU machine.
#
# Build:  docker build -t bat-trainer .
# Run:    docker run --gpus all -v $(pwd)/bat_data:/workspace/bat_data \
#                              -v $(pwd)/output:/workspace/output \
#                              -it bat-trainer

FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

# System deps for audio I/O
RUN apt-get update && apt-get install -y \
    libsndfile1 \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
RUN pip install --no-cache-dir \
    batdetect2 \
    numpy \
    scikit-learn \
    tqdm

WORKDIR /workspace

# Copy training scripts
COPY scripts/extract_features.py /workspace/
COPY scripts/train_classifier.py /workspace/
COPY scripts/evaluate.py /workspace/

# Create expected directory structure
RUN mkdir -p /workspace/features /workspace/models /workspace/logs /workspace/predictions

# Volume mount points:
#   /workspace/bat_data  — read-only, one folder per species
#   /workspace/output    — where models/logs/predictions go (bind-mount this out)

ENTRYPOINT ["/bin/bash"]
