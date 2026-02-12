"""
Device health metrics collector for Raspberry Pi and AudioMoth.

Reads host system metrics via mounted /proc and /sys paths,
checks internet connectivity, and queries PostgreSQL for
AudioMoth activity and database statistics.
"""

import os
import socket
import time
import urllib.request


# ---------------------------------------------------------------------------
# Raspberry Pi metrics (read from host-mounted paths)
# ---------------------------------------------------------------------------

def get_uptime() -> float | None:
    """Read Pi uptime in seconds from mounted /host/uptime."""
    try:
        with open("/host/uptime", "r") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def get_cpu_temp() -> float | None:
    """Read CPU temperature in °C from mounted /host/cpu_temp."""
    try:
        with open("/host/cpu_temp", "r") as f:
            return float(f.read().strip()) / 1000.0
    except Exception:
        return None


def get_memory_info() -> tuple[float | None, float | None]:
    """Read total and available memory (MB) from mounted /host/meminfo."""
    try:
        with open("/host/meminfo", "r") as f:
            info = {}
            for line in f:
                parts = line.split()
                key = parts[0].rstrip(":")
                if key in ("MemTotal", "MemAvailable"):
                    info[key] = int(parts[1]) / 1024  # KB → MB
            return info.get("MemTotal"), info.get("MemAvailable")
    except Exception:
        return None, None


def get_cpu_load() -> tuple[float | None, float | None, float | None]:
    """Read 1/5/15-minute load averages from mounted /host/loadavg."""
    try:
        with open("/host/loadavg", "r") as f:
            parts = f.read().split()
            return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        return None, None, None


def get_disk_usage() -> tuple[float | None, float | None]:
    """Get SD-card disk usage in GB via statvfs on the container root.

    Because Docker's overlay2 filesystem shares the host partition,
    ``os.statvfs('/')`` reflects the host SD-card stats.
    """
    try:
        stat = os.statvfs("/")
        total = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
        free = (stat.f_bfree * stat.f_frsize) / (1024 ** 3)
        return total, total - free
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Connectivity checks
# ---------------------------------------------------------------------------

def check_internet(timeout: float = 5) -> tuple[bool, float | None]:
    """Check internet connectivity via HTTP HEAD to Google.

    Uses HTTP rather than raw TCP sockets because Docker container
    networking may block direct socket connections to DNS ports.

    Returns (connected, latency_ms).
    """
    try:
        start = time.time()
        req = urllib.request.Request(
            "https://www.google.com",
            method="HEAD",
        )
        urllib.request.urlopen(req, timeout=timeout)
        latency = (time.time() - start) * 1000  # ms
        return True, round(latency, 2)
    except Exception:
        return False, None


# ---------------------------------------------------------------------------
# AudioMoth & database statistics (require a psycopg2 connection)
# ---------------------------------------------------------------------------

def check_audiomoth(conn) -> bool:
    """Return True if classifications arrived in the last 5 minutes.

    Uses a 5-minute window (rather than 2) to account for the
    buffer flush delay and AudioMoth device-contention retries.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM classifications "
                "WHERE sync_time > NOW() - INTERVAL '5 minutes'"
            )
            return cur.fetchone()[0] > 0
    except Exception:
        return False


def get_db_stats(conn) -> dict:
    """Return database size, row counts, and unsynced count."""
    out = {
        "db_size_mb": None,
        "classifications_total": None,
        "bat_detections_total": None,
        "unsynced_count": None,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_database_size('soundscape') / (1024.0 * 1024.0)"
            )
            out["db_size_mb"] = round(float(cur.fetchone()[0]), 2)

            cur.execute("SELECT COUNT(*) FROM classifications")
            out["classifications_total"] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM bat_detections")
            out["bat_detections_total"] = cur.fetchone()[0]

            cur.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM classifications WHERE synced = FALSE) + "
                "(SELECT COUNT(*) FROM bat_detections WHERE synced = FALSE)"
            )
            out["unsynced_count"] = cur.fetchone()[0]
    except Exception:
        pass
    return out


def get_error_count(conn, hours: int = 1) -> int:
    """Count capture errors logged in the last *hours* hours."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM capture_errors "
                "WHERE recorded_at > NOW() - INTERVAL '%s hours'",
                (hours,),
            )
            return cur.fetchone()[0]
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Aggregate collector
# ---------------------------------------------------------------------------

def collect_all_metrics(conn) -> dict:
    """Collect every metric into a flat dict suitable for DB insert."""
    uptime = get_uptime()
    cpu_temp = get_cpu_temp()
    mem_total, mem_available = get_memory_info()
    load_1, load_5, load_15 = get_cpu_load()
    disk_total, disk_used = get_disk_usage()
    internet_ok, internet_latency = check_internet()
    audiomoth_ok = check_audiomoth(conn)
    db = get_db_stats(conn)
    errors = get_error_count(conn)

    return {
        "uptime_seconds": uptime,
        "cpu_temp": cpu_temp,
        "cpu_load_1m": load_1,
        "cpu_load_5m": load_5,
        "cpu_load_15m": load_15,
        "mem_total_mb": mem_total,
        "mem_available_mb": mem_available,
        "disk_total_gb": disk_total,
        "disk_used_gb": disk_used,
        "internet_connected": internet_ok,
        "internet_latency_ms": internet_latency,
        "audiomoth_connected": audiomoth_ok,
        "capture_errors_1h": errors,
        **db,
    }
