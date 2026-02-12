-- General soundscape classifications (AST model)
CREATE TABLE IF NOT EXISTS classifications (
    id SERIAL PRIMARY KEY,
    label VARCHAR(255) NOT NULL,
    score FLOAT NOT NULL,
    spl FLOAT,
    device VARCHAR(100) NOT NULL,
    sync_id UUID NOT NULL,
    sync_time TIMESTAMP NOT NULL,
    synced BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_class_sync_time ON classifications(sync_time);
CREATE INDEX idx_class_device ON classifications(device);
CREATE INDEX idx_class_label ON classifications(label);
CREATE INDEX idx_class_synced ON classifications(synced);

-- Bat echolocation detections (BatDetect2 model)
CREATE TABLE IF NOT EXISTS bat_detections (
    id SERIAL PRIMARY KEY,
    species VARCHAR(255) NOT NULL,
    common_name VARCHAR(255),
    detection_prob FLOAT NOT NULL,
    start_time FLOAT,
    end_time FLOAT,
    low_freq FLOAT,
    high_freq FLOAT,
    duration_ms FLOAT,
    device VARCHAR(100) NOT NULL,
    sync_id UUID NOT NULL,
    detection_time TIMESTAMP NOT NULL,
    synced BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_bat_detection_time ON bat_detections(detection_time);
CREATE INDEX idx_bat_device ON bat_detections(device);
CREATE INDEX idx_bat_species ON bat_detections(species);
CREATE INDEX idx_bat_synced ON bat_detections(synced);

-- Device health status (collected by sync-service each cycle)
CREATE TABLE IF NOT EXISTS device_status (
    id SERIAL PRIMARY KEY,
    uptime_seconds FLOAT,
    cpu_temp FLOAT,
    cpu_load_1m FLOAT,
    cpu_load_5m FLOAT,
    cpu_load_15m FLOAT,
    mem_total_mb FLOAT,
    mem_available_mb FLOAT,
    disk_total_gb FLOAT,
    disk_used_gb FLOAT,
    internet_connected BOOLEAN DEFAULT FALSE,
    internet_latency_ms FLOAT,
    audiomoth_connected BOOLEAN DEFAULT FALSE,
    capture_errors_1h INTEGER DEFAULT 0,
    db_size_mb FLOAT,
    classifications_total INTEGER DEFAULT 0,
    bat_detections_total INTEGER DEFAULT 0,
    unsynced_count INTEGER DEFAULT 0,
    recorded_at TIMESTAMP NOT NULL DEFAULT NOW(),
    synced BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_device_status_recorded ON device_status(recorded_at);

-- Capture error log (written by ast-service and batdetect-service)
CREATE TABLE IF NOT EXISTS capture_errors (
    id SERIAL PRIMARY KEY,
    service VARCHAR(50) NOT NULL,
    error_type VARCHAR(100),
    message TEXT,
    recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_capture_errors_recorded ON capture_errors(recorded_at);
