# Cost, Storage & Data Flow Analysis

## Data Flow Summary

```
AudioMoth USB Mic → temp .wav → ML Models → metadata only → PostgreSQL → Firestore → Dashboard
                   (deleted)   (AST/BatDetect2)  (text/numbers)
```

**Key point**: No audio files are stored permanently by default. Only small metadata records (label, score, SPL, species, frequencies) are persisted. Bat audio upload to Firebase Storage is available but **disabled by default** via the `UPLOAD_BAT_AUDIO` environment variable.

---

## What's Stored Where

| Destination | What's Stored | Format | Typical Size |
|-------------|--------------|--------|-------------|
| **Firebase Firestore** | Classification & detection metadata | JSON docs (~0.5 KB each) | ~200 MB/month |
| **Vercel** | Static Next.js dashboard build | JS/CSS/HTML | ~1 MB (fixed) |
| **PostgreSQL on Pi** | Same metadata + synced flag | Relational rows | ~4 GB/month |
| **Temp files (auto-deleted)** | Raw .wav audio samples | PCM WAV | 0 (deleted after processing) |

### Temporary Audio Files (NOT stored)

| Service | Duration | Sample Rate | File Size | Lifetime |
|---------|----------|------------|-----------|----------|
| AST Service | 1 second | 192 kHz | ~375 KB | Deleted every ~1s |
| BatDetect2 Service | 5 seconds | 192 kHz | ~1.9 MB | Deleted every ~6s |

These files are created in Python's `TemporaryDirectory()` and are automatically cleaned up after each classification cycle.

---

## Firebase Costs (Firestore)

### Free Tier (Spark Plan)

- 20,000 writes/day
- 50,000 reads/day
- 1 GB storage

### Your Usage Rate

- ~1 classification/second × 5 labels = **432,000 writes/day** (exceeds free tier)
- Dashboard reads: ~100 docs on load + real-time deltas = **minimal reads**
- Storage: ~0.5 KB/doc × 432K docs/day = ~210 MB/day growing

### Blaze Plan (Pay-as-you-go) Estimated Cost

| Resource | Daily Usage | Cost |
|----------|------------|------|
| Writes | ~432K | ~$0.30/day |
| Reads | ~1K | negligible |
| Storage | ~210 MB/day (cumulative) | ~$0.18/GB/month |
| **Total** | | **~$9–12/month** |

### Cost Reduction Strategies

1. **Reduce labels per sample**: Sync only the #1 label per sample instead of top 5 (5× reduction → ~$2/month)
2. **Increase flush threshold**: Raise from 25 to 50+ per batch (fewer write operations)
3. **Aggregate locally**: Sync hourly summaries instead of raw data
4. **Firestore TTL rules**: Auto-delete documents older than N days to cap storage

---

## Raspberry Pi Storage (256 GB SD Card)

### Current Usage

```
Filesystem      Size  Used  Avail  Use%
/dev/mmcblk0p2  229G   20G   200G   9%
```

### What Uses Disk Space

| Component | Current Size | Growth Rate | Notes |
|-----------|-------------|-------------|-------|
| Docker images | 8.4 GB | Static | Rebuilt only on code changes |
| Docker build cache | 4.2 GB | Static | Can be pruned: `docker builder prune` |
| PostgreSQL data | 8 MB (1,650 rows) | ~134 MB/day | **Main growth concern** |
| OS + packages | ~7 GB | Static | Raspberry Pi OS |
| **Total** | ~20 GB | ~134 MB/day | |

### Growth Projections

| Timeframe | PostgreSQL Size | Total Disk Used | Remaining |
|-----------|----------------|-----------------|-----------|
| Now | 8 MB | 20 GB | 209 GB |
| 1 week | ~1 GB | 21 GB | 208 GB |
| 1 month | ~4 GB | 24 GB | 205 GB |
| 6 months | ~24 GB | 44 GB | 185 GB |
| 1 year | ~48 GB | 68 GB | 161 GB |

### Recommended: Data Retention Policy

The sync-service automatically prunes synced records older than 30 days to keep the database lean. You can also run this manually:

```sql
DELETE FROM classifications WHERE synced = TRUE AND sync_time < NOW() - INTERVAL '30 days';
DELETE FROM bat_detections WHERE synced = TRUE AND detection_time < NOW() - INTERVAL '30 days';
DELETE FROM device_status WHERE recorded_at < NOW() - INTERVAL '7 days';
DELETE FROM capture_errors WHERE recorded_at < NOW() - INTERVAL '7 days';
VACUUM;
```

This keeps the PostgreSQL database under ~4 GB permanently.

---

## Bat Audio Upload (Optional)

Bat audio upload is controlled by the `UPLOAD_BAT_AUDIO` environment variable in `docker-compose.yml`. It is **disabled by default**.

| Setting | Behaviour |
|---------|-----------|
| `UPLOAD_BAT_AUDIO=false` (default) | Audio files are deleted after classification. No upload. No storage cost. |
| `UPLOAD_BAT_AUDIO=true` | When a bat call is detected, the 5-second .wav file is saved, uploaded to Firebase Storage, and a playback link is added to the dashboard. |

### Cost When Enabled

| Item | Size | Destination | Cost Impact |
|------|------|-------------|-------------|
| Bat call .wav (5s, 192 kHz) | ~1.9 MB | Firebase Storage | $0.026/GB/month storage + $0.12/GB download |
| Typical night (20 detections) | ~38 MB | Firebase Storage | ~$0.001/night |
| Typical month | ~1.1 GB | Firebase Storage | ~$0.03/month storage |

To enable, edit `edge/docker-compose.yml` and set:
```yaml
UPLOAD_BAT_AUDIO=true
```

Then restart the stack:
```bash
cd ~/bat-edge-monitor/edge
docker compose up -d
```

---

## Vercel Costs

The dashboard is a static Next.js site deployed on Vercel's free Hobby plan:

- **Bandwidth**: 100 GB/month (free tier) — dashboard uses <1 MB per visit
- **Builds**: 6,000 minutes/month — each build takes <2 minutes
- **Cost**: **$0/month** for typical thesis usage

---

## Device Health Monitoring

The sync-service collects Raspberry Pi and AudioMoth health metrics every sync cycle (default: 60 seconds) and pushes them to Firestore. The dashboard displays these in real time.

### Metrics Collected

| Metric | Source | Description |
|--------|--------|-------------|
| Uptime | `/proc/uptime` | Time since last Pi reboot |
| CPU Temperature | `/sys/class/thermal/thermal_zone0/temp` | Pi SoC temperature in °C |
| CPU Load | `/proc/loadavg` | 1-min, 5-min, 15-min load averages |
| Memory | `/proc/meminfo` | Total and available RAM |
| Disk | `os.statvfs('/')` | SD card total and used space |
| Internet | TCP connect to 8.8.8.8:53 | Connectivity status and latency |
| AudioMoth | Recent classification count | Active if data received in last 2 minutes |
| Capture Errors | `capture_errors` table | Error count in last hour |
| DB Size | `pg_database_size()` | PostgreSQL database size |

### Firestore Cost for Health Data

One document overwritten per sync cycle: **1,440 writes/day** (negligible — ~$0.001/day).

---

## Summary

| Service | Monthly Cost | Notes |
|---------|-------------|-------|
| Vercel (dashboard) | $0 | Free Hobby plan |
| Firebase Firestore | $9–12 | Blaze plan; reducible with optimisation |
| Firebase Storage | $0 (disabled) | ~$0.03/month if bat audio enabled |
| Raspberry Pi | $0 | Local hardware; ~134 MB/day PostgreSQL growth |
| **Total** | **$9–12/month** | Primarily Firestore writes |
