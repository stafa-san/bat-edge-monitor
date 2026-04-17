FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    alsa-utils \
    libsndfile1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build context is repo root (see docker-compose.yml build.context).
COPY edge/batdetect-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edge/batdetect-service/src/ ./src/
COPY edge/scripts/ ./edge/scripts/
COPY docker/models/groups_model.pt /app/models/groups_model.pt

CMD ["python", "-m", "src.main"]
