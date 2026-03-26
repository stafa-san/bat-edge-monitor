FROM python:3.11-slim

# BlueZ and D-Bus dependencies for BLE access
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluetooth \
    bluez \
    libglib2.0-dev \
    dbus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/

CMD ["python", "-m", "src.main"]
