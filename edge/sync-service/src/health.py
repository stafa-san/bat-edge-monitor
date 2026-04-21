"""
Device health metrics collector for Raspberry Pi and AudioMoth.

Reads host system metrics via mounted /proc and /sys paths,
checks internet connectivity, and queries PostgreSQL for
AudioMoth activity and database statistics.
"""

import os
import re
import socket
import subprocess
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
    """Return True if an AudioMoth USB microphone is physically connected.

    Checks /proc/asound/cards (mounted at /host/asound/cards) for an
    AudioMoth device.  This gives instant detection when the device is
    plugged or unplugged, unlike the previous approach of querying for
    recent classifications (which had up to a 5-minute lag).
    """
    try:
        with open("/host/asound/cards", "r") as f:
            return "AudioMoth" in f.read()
    except FileNotFoundError:
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Power / undervoltage — Raspberry Pi vcgencmd wrapper
# ---------------------------------------------------------------------------
#
# On Pi 5, we run ``vcgencmd`` from the sync-service container. The binary
# is bind-mounted in from the host at /usr/bin/vcgencmd and talks to the
# firmware via /dev/vcio (mounted as a device in docker-compose.yml).
#
# Throttled flag bits (from the Raspberry Pi docs):
#   bit 0  (0x1)     = Under-voltage right now
#   bit 1  (0x2)     = ARM frequency capped right now
#   bit 2  (0x4)     = Currently throttled
#   bit 3  (0x8)     = Soft temperature limit active
#   bit 16 (0x10000) = Under-voltage has occurred since last reboot
#   bit 17 (0x20000) = ARM frequency capping has occurred since last reboot
#   bit 18 (0x40000) = Throttling has occurred since last reboot
#   bit 19 (0x80000) = Soft temperature limit has occurred since last reboot

_VCGENCMD_PATH = "/usr/bin/vcgencmd"
_VCGENCMD_TIMEOUT_SEC = 2


def _vcgencmd(*args) -> str | None:
    """Run vcgencmd with *args* and return stdout, or None on failure."""
    if not os.path.exists(_VCGENCMD_PATH):
        return None
    try:
        res = subprocess.run(
            [_VCGENCMD_PATH, *args],
            capture_output=True, text=True,
            timeout=_VCGENCMD_TIMEOUT_SEC,
        )
        if res.returncode != 0:
            return None
        return res.stdout.strip()
    except Exception:
        return None


def get_power_status() -> dict:
    """Return undervoltage / throttling / voltage info.

    Safe to call every health tick; returns a dict of None values when
    vcgencmd isn't available (e.g. running on a dev laptop).
    """
    out = {
        "throttled_hex": None,
        "undervolt_now": None,
        "undervolt_since_boot": None,
        "throttled_now": None,
        "throttled_since_boot": None,
        "freq_capped_now": None,
        "freq_capped_since_boot": None,
        "core_voltage": None,
        "ext5v_voltage": None,
    }

    throttled = _vcgencmd("get_throttled")
    if throttled:
        # Format: "throttled=0x50000"
        m = re.search(r"throttled=(0x[0-9a-fA-F]+)", throttled)
        if m:
            val = int(m.group(1), 16)
            out["throttled_hex"] = m.group(1)
            out["undervolt_now"] = bool(val & 0x1)
            out["freq_capped_now"] = bool(val & 0x2)
            out["throttled_now"] = bool(val & 0x4)
            out["undervolt_since_boot"] = bool(val & 0x10000)
            out["freq_capped_since_boot"] = bool(val & 0x20000)
            out["throttled_since_boot"] = bool(val & 0x40000)

    core = _vcgencmd("measure_volts", "core")
    if core:
        m = re.search(r"volt=([0-9.]+)V", core)
        if m:
            out["core_voltage"] = round(float(m.group(1)), 3)

    ext = _vcgencmd("pmic_read_adc", "EXT5V_V")
    if ext:
        # Format: "     EXT5V_V volt(24)=4.77978000V"
        m = re.search(r"volt\(\d+\)=([0-9.]+)V", ext)
        if m:
            out["ext5v_voltage"] = round(float(m.group(1)), 3)

    return out


# ---------------------------------------------------------------------------
# Audio RMS — surfaced from the batdetect-service via the audio_levels table
# ---------------------------------------------------------------------------
#
# batdetect-service writes one row per captured segment with the RMS and
# peak amplitude of the loaded (post-HPF) audio. We surface the most
# recent sample plus a 1-minute moving average for the dashboard so a
# silent / undervolted microphone is visible at a glance.

def get_audio_levels(conn) -> dict:
    """Latest + recent-average audio levels and BD stats + rejection counts.

    Pulls a compact snapshot of the last 15 s segment plus rolled-up
    counters over the last hour so the dashboard has enough to show
    "detector is seeing weak signal" vs "detector is seeing nothing"
    and "validator is rejecting N segments for reason R" without
    needing log grep.
    """
    out = {
        "audio_rms_latest": None,
        "audio_rms_avg_1m": None,
        "audio_peak_latest": None,
        "audio_levels_last_at": None,
        "bd_raw_count_latest": None,
        "bd_max_det_prob_latest": None,
        "bd_user_pass_latest": None,
        "bd_max_det_prob_1h": None,
        "bd_raw_avg_1h": None,
        "rejection_reasons_1h": {},
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rms, peak, recorded_at, "
                "       bd_raw_count, bd_max_det_prob, bd_user_pass "
                "FROM audio_levels "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                out["audio_rms_latest"] = round(float(row[0]), 6) if row[0] is not None else None
                out["audio_peak_latest"] = round(float(row[1]), 6) if row[1] is not None else None
                out["audio_levels_last_at"] = row[2]
                if row[3] is not None:
                    out["bd_raw_count_latest"] = int(row[3])
                if row[4] is not None:
                    out["bd_max_det_prob_latest"] = round(float(row[4]), 3)
                if row[5] is not None:
                    out["bd_user_pass_latest"] = int(row[5])

            cur.execute(
                "SELECT AVG(rms), AVG(bd_raw_count), MAX(bd_max_det_prob) "
                "FROM audio_levels "
                "WHERE recorded_at > NOW() - INTERVAL '1 hour'"
            )
            row = cur.fetchone()
            if row:
                if row[0] is not None:
                    out["audio_rms_avg_1m"] = round(float(row[0]), 6)
                if row[1] is not None:
                    out["bd_raw_avg_1h"] = round(float(row[1]), 2)
                if row[2] is not None:
                    out["bd_max_det_prob_1h"] = round(float(row[2]), 3)

            cur.execute(
                "SELECT rejection_reason, COUNT(*) "
                "FROM audio_levels "
                "WHERE rejection_reason IS NOT NULL "
                "  AND recorded_at > NOW() - INTERVAL '1 hour' "
                "GROUP BY rejection_reason"
            )
            reasons = {}
            for reason, cnt in cur.fetchall():
                # Truncate tail like "validator:rms_too_low(0.002)" → "validator:rms_too_low"
                clean = reason.split("(")[0] if reason else "unknown"
                reasons[clean] = reasons.get(clean, 0) + int(cnt)
            out["rejection_reasons_1h"] = reasons
    except Exception:
        pass
    return out


def get_audiomoth_hw_sample_rate() -> int | None:
    """Read the AudioMoth's native hardware sample rate from /proc/asound.

    Parses the stream0 file for the AudioMoth card to find the supported
    rate.  Returns the rate in Hz (e.g. 384000) or None if unavailable.
    """
    try:
        with open("/host/asound/cards", "r") as f:
            cards_text = f.read()
        # Find the card number for AudioMoth
        for line in cards_text.splitlines():
            if "AudioMoth" in line:
                card_num = line.strip().split()[0]
                stream_path = f"/host/asound/card{card_num}/stream0"
                with open(stream_path, "r") as sf:
                    for sline in sf:
                        if "Rates:" in sline:
                            rate_str = sline.split("Rates:")[1].strip()
                            return int(rate_str)
        return None
    except Exception:
        return None


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
    audiomoth_hw_rate = get_audiomoth_hw_sample_rate() if audiomoth_ok else None
    power = get_power_status()
    audio_levels = get_audio_levels(conn)
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
        "audiomoth_hw_sample_rate": audiomoth_hw_rate,
        "capture_errors_1h": errors,
        **power,
        **audio_levels,
        **db,
    }
