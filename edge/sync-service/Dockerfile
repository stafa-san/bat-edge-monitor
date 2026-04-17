FROM python:3.11-slim

# rclone is invoked via subprocess for Tier 1 OneDrive archival.
# The binary is always present even when the feature flag is off
# (~40 MB extra in the image); OAuth config is mounted at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

CMD ["python", "-m", "src.main"]
