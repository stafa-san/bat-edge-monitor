FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    libsndfile1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build context is repo root (see docker-compose.yml build.context).
COPY edge/analysis-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# analysis-api's own code (worker + legacy HTTP path).
COPY edge/analysis-api/src/ ./src/

# Shared pipeline + supporting modules live in batdetect-service/src/
# so there's a single source of truth. Copied in after analysis-api's
# own src so a missing module here can't silently shadow the canonical
# version. Any change to these files automatically propagates to the
# cloud worker on next ``docker compose build analysis-api``.
COPY edge/batdetect-service/src/bat_pipeline.py ./src/bat_pipeline.py
COPY edge/batdetect-service/src/audio_validator.py ./src/audio_validator.py
COPY edge/batdetect-service/src/classifier.py ./src/classifier.py

COPY docker/models/groups_model.pt /app/models/groups_model.pt

# Firebase-driven upload worker. The legacy FastAPI HTTP app at src.main
# still exists as dead code — flip this CMD back to ``uvicorn src.main:app``
# to restore LAN HTTP uploads if ever needed.
CMD ["python", "-m", "src.worker"]
