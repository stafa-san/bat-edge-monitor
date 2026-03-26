"""
HOBO MX2201 BLE Temperature Service — Multi-Sensor Advertisement Scanner

Passively listens for BLE advertisements from ALL Onset HOBO MX temperature
loggers in range and writes temperature readings to PostgreSQL.

Each sensor is identified by its BLE MAC address (stable on Linux/Pi).
No configuration needed — the service auto-discovers any device advertising
with Onset's BLE company ID 0x00C5.

Temperature formula (reverse-engineered, verified ±0.1°C against logged data):
    temp_c = byte[13] * 0.04185405 + 19.21346115

Environment variables:
  HOBO_POLL_INTERVAL Seconds between DB writes (default: 30)
  DB_HOST / DB_NAME / DB_USER / DB_PASSWORD — PostgreSQL connection
"""

import asyncio
import os
import signal
import sys
import psycopg2
from datetime import datetime, timezone
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

# ── Configuration ──────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("HOBO_POLL_INTERVAL", "30"))
DEFAULT_MODEL = os.getenv("HOBO_MODEL", "MX2201")

# Onset HOBO MX BLE company ID (registered with Bluetooth SIG)
HOBO_COMPANY_ID = 0x00C5

# Temperature decoding — reverse-engineered from MX2201 advertisement data
# Verified against HOBOconnect logged data: max error ±0.1°C
TEMP_SCALE = 0.04185405
TEMP_OFFSET = 19.21346115
TEMP_BYTE_INDEX = 13

# ── State: one entry per BLE address ──────────────────────────────────────
latest_readings: dict[str, dict] = {}
discovered: set[str] = set()


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "db"),
        dbname=os.getenv("DB_NAME", "soundscape"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def write_readings(readings: dict[str, dict]):
    """Batch-write all sensor readings to PostgreSQL."""
    if not readings:
        return
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for addr, r in readings.items():
                cur.execute(
                    """
                    INSERT INTO environmental_readings
                        (temperature_c, sensor_address, sensor_serial, sensor_model, rssi, recorded_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        r["temp_c"],
                        addr,
                        r.get("serial", ""),
                        r.get("model", DEFAULT_MODEL),
                        r.get("rssi", 0),
                        datetime.now(timezone.utc),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def decode_temperature(data: bytes) -> float | None:
    """Decode temperature from Onset MX manufacturer data.

    Formula: temp_c = data[13] * 0.04185405 + 19.21346115
    Byte 13 is a single unsigned byte (0–255).
    Verified ±0.1°C against HOBOconnect logged data.
    """
    if data is None or len(data) < TEMP_BYTE_INDEX + 1:
        return None
    raw_byte = data[TEMP_BYTE_INDEX]
    return round(raw_byte * TEMP_SCALE + TEMP_OFFSET, 3)


def extract_serial(name: str) -> str:
    """Try to extract serial number from device name.

    HOBO devices advertise as e.g. "MX Temp 20515200" or "HOBO MX2201".
    """
    if not name:
        return ""
    parts = name.split()
    for part in reversed(parts):
        if part.isdigit() and len(part) >= 6:
            return part
    return ""


def on_advertisement(device: BLEDevice, adv: AdvertisementData):
    """Callback for every BLE advertisement received."""
    hobo_data = adv.manufacturer_data.get(HOBO_COMPANY_ID)
    if hobo_data is None or len(hobo_data) < TEMP_BYTE_INDEX + 1:
        return

    addr = device.address
    name = device.name or adv.local_name or ""

    # Log discovery info on first detection per device
    if addr not in discovered:
        discovered.add(addr)
        temp = decode_temperature(hobo_data)
        temp_str = f"{temp:.2f}°C / {temp * 9/5 + 32:.1f}°F" if temp else "N/A"
        serial = extract_serial(name)
        print(f"[HOBO] Found sensor: {name or addr}")
        print(f"[HOBO]   Address: {addr}")
        print(f"[HOBO]   Serial: {serial or '(unknown)'}")
        print(f"[HOBO]   RSSI: {adv.rssi} dBm")
        print(f"[HOBO]   Raw ({len(hobo_data)} bytes): {hobo_data.hex()}")
        print(f"[HOBO]   Temp byte[{TEMP_BYTE_INDEX}] = {hobo_data[TEMP_BYTE_INDEX]} → {temp_str}")
        print(f"[HOBO]   Total sensors discovered: {len(discovered)}")

    # Store latest reading
    temp = decode_temperature(hobo_data)
    if temp is not None:
        latest_readings[addr] = {
            "temp_c": temp,
            "rssi": adv.rssi,
            "serial": extract_serial(name),
            "model": DEFAULT_MODEL,
            "name": name,
            "raw_hex": hobo_data.hex(),
            "updated_at": datetime.now(timezone.utc),
        }


async def poll_loop():
    print(f"[HOBO] Starting multi-sensor BLE advertisement scanner")
    print(f"[HOBO] Company ID: 0x{HOBO_COMPANY_ID:04X}")
    print(f"[HOBO] Poll interval: {POLL_INTERVAL}s")
    print(f"[HOBO] Temp formula: byte[{TEMP_BYTE_INDEX}] × {TEMP_SCALE} + {TEMP_OFFSET}")
    print(f"[HOBO] Listening for all Onset HOBO sensors in range...")

    scanner = BleakScanner(detection_callback=on_advertisement)
    consecutive_empty = 0

    while True:
        # Clear readings from previous cycle
        latest_readings.clear()

        try:
            await scanner.start()
            await asyncio.sleep(POLL_INTERVAL)
            await scanner.stop()
        except Exception as e:
            print(f"[HOBO] Scanner error: {e}")
            await asyncio.sleep(10)
            continue

        if latest_readings:
            try:
                write_readings(latest_readings)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                parts = []
                for addr, r in latest_readings.items():
                    label = r["serial"] or addr[-8:]
                    temp_f = r["temp_c"] * 9 / 5 + 32
                    parts.append(f"{label}={r['temp_c']:.1f}°C/{temp_f:.0f}°F")
                print(f"[HOBO] {ts}  {len(latest_readings)} sensor(s): {', '.join(parts)}")
                consecutive_empty = 0
            except Exception as e:
                print(f"[HOBO] DB write error: {e}")
        else:
            consecutive_empty += 1
            if consecutive_empty <= 3 or consecutive_empty % 10 == 0:
                print(
                    f"[HOBO] No sensors detected (cycle {consecutive_empty}). "
                    f"Ensure Bluetooth Always On is enabled in HOBOconnect."
                )


def handle_signal(signum, frame):
    print(f"[HOBO] Received signal {signum}, shutting down...")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    asyncio.run(poll_loop())
