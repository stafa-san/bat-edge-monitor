FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    libsndfile1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build context is repo root (see docker-compose.yml build.context).
COPY edge/analysis-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edge/analysis-api/src/ ./src/
COPY docker/models/groups_model.pt /app/models/groups_model.pt

# Firebase-driven upload worker. The FastAPI HTTP app at src.main still
# exists as dead code — flip this CMD back to `uvicorn src.main:app`
# to restore LAN HTTP uploads.
CMD ["python", "-m", "src.worker"]
