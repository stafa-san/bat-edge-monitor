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
