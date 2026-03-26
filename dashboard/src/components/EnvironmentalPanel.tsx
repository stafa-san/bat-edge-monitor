"use client";

import { useMemo, useCallback } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import { format, formatDistanceToNow } from "date-fns";
import { Timestamp } from "firebase/firestore";
import * as XLSX from "xlsx";

// ── Types ────────────────────────────────────────────────────────────────

export interface EnvironmentalReading {
  id: string;
  temperatureC: number;
  sensorAddress?: string;
  sensorSerial?: string;
  sensorModel?: string;
  rssi?: number;
  recordedAt: Timestamp;
}

export type EnvTimeRange = "1h" | "6h" | "24h" | "7d";

interface EnvironmentalPanelProps {
  readings: EnvironmentalReading[];
  timeRange: EnvTimeRange;
  onTimeRangeChange: (r: EnvTimeRange) => void;
}

// ── Constants ────────────────────────────────────────────────────────────

const SENSOR_COLORS = [
  "#f97316", // orange
  "#3b82f6", // blue
  "#10b981", // emerald
  "#8b5cf6", // violet
  "#ec4899", // pink
  "#eab308", // yellow
  "#06b6d4", // cyan
  "#ef4444", // red
];

const RANGE_LABELS: Record<EnvTimeRange, string> = {
  "1h": "1H",
  "6h": "6H",
  "24h": "24H",
  "7d": "7D",
};

// ── Helpers ──────────────────────────────────────────────────────────────

function signalStrength(rssi: number | undefined): { label: string; bars: number; color: string } {
  if (rssi == null) return { label: "N/A", bars: 0, color: "text-gray-300" };
  if (rssi >= -50) return { label: "Excellent", bars: 4, color: "text-green-500" };
  if (rssi >= -65) return { label: "Good", bars: 3, color: "text-green-400" };
  if (rssi >= -80) return { label: "Fair", bars: 2, color: "text-yellow-500" };
  return { label: "Weak", bars: 1, color: "text-red-500" };
}

function SignalBars({ bars }: { bars: number }) {
  return (
    <div className="flex items-end gap-[2px] h-3.5">
      {[1, 2, 3, 4].map((i) => (
        <div
          key={i}
          className={`w-[3px] rounded-sm ${
            i <= bars ? "bg-current" : "bg-gray-200"
          }`}
          style={{ height: `${i * 25}%` }}
        />
      ))}
    </div>
  );
}

interface SensorSummary {
  address: string;
  serial: string;
  model: string;
  latestTemp: number;
  latestRssi: number | undefined;
  latestTime: Date;
  readings: { time: Date; temp: number }[];
  color: string;
  avgTemp: number;
  minTemp: number;
  maxTemp: number;
}

// ── Component ────────────────────────────────────────────────────────────

export function EnvironmentalPanel({
  readings,
  timeRange,
  onTimeRangeChange,
}: EnvironmentalPanelProps) {
  // Group by sensor
  const sensors = useMemo<SensorSummary[]>(() => {
    const map = new Map<string, EnvironmentalReading[]>();
    for (const r of readings) {
      if (!r.recordedAt?.toDate) continue;
      const key = r.sensorAddress || r.sensorSerial || "unknown";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(r);
    }

    const result: SensorSummary[] = [];
    let colorIdx = 0;
    for (const [address, rds] of map) {
      const sorted = [...rds].sort(
        (a, b) => a.recordedAt.toDate().getTime() - b.recordedAt.toDate().getTime()
      );
      const latest = sorted[sorted.length - 1];
      const temps = sorted.map((r) => r.temperatureC);
      result.push({
        address,
        serial: latest.sensorSerial || address.slice(-8),
        model: latest.sensorModel || "MX2201",
        latestTemp: latest.temperatureC,
        latestRssi: latest.rssi,
        latestTime: latest.recordedAt.toDate(),
        readings: sorted.map((r) => ({
          time: r.recordedAt.toDate(),
          temp: parseFloat(r.temperatureC.toFixed(2)),
        })),
        color: SENSOR_COLORS[colorIdx % SENSOR_COLORS.length],
        avgTemp: temps.reduce((a, b) => a + b, 0) / temps.length,
        minTemp: Math.min(...temps),
        maxTemp: Math.max(...temps),
      });
      colorIdx++;
    }
    return result.sort((a, b) => a.serial.localeCompare(b.serial));
  }, [readings]);

  // Build unified chart data
  const chartData = useMemo(() => {
    if (sensors.length === 0) return [];

    // Collect all unique timestamps, bucket by time label
    const timeFormat = timeRange === "7d" ? "MM/dd HH:mm" : "HH:mm";
    const allPoints = new Map<string, Record<string, number | string>>();

    for (const sensor of sensors) {
      for (const { time, temp } of sensor.readings) {
        const label = format(time, timeFormat);
        if (!allPoints.has(label)) {
          allPoints.set(label, { time: label, _ts: time.getTime() as unknown as string });
        }
        allPoints.get(label)![`temp_${sensor.address}`] = temp;
      }
    }

    return Array.from(allPoints.values()).sort(
      (a, b) => (a._ts as unknown as number) - (b._ts as unknown as number)
    );
  }, [sensors, timeRange]);

  // XLSX export
  const handleExport = useCallback(() => {
    const wb = XLSX.utils.book_new();
    for (const sensor of sensors) {
      const data = sensor.readings.map((r, i) => ({
        "#": i + 1,
        "Date-Time": format(r.time, "MM/dd/yyyy HH:mm:ss"),
        "Temperature (°C)": r.temp,
      }));
      const ws = XLSX.utils.json_to_sheet(data);
      ws["!cols"] = [{ wch: 6 }, { wch: 22 }, { wch: 18 }];
      const sheetName = (sensor.serial || sensor.address).slice(0, 31);
      XLSX.utils.book_append_sheet(wb, ws, sheetName);
    }
    const now = format(new Date(), "yyyy-MM-dd_HHmmss");
    XLSX.writeFile(wb, `environmental_readings_${now}.xlsx`);
  }, [sensors]);

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
      {/* ── Header ──────────────────────────────────────────────── */}
      <div className="px-6 pt-5 pb-4 border-b border-gray-100">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">
              Environmental Monitoring
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              {sensors.length} sensor{sensors.length !== 1 ? "s" : ""} detected
              {" · "}
              {readings.length} readings
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* Time range toggle */}
            <div className="flex bg-gray-100 rounded-lg p-0.5">
              {(Object.keys(RANGE_LABELS) as EnvTimeRange[]).map((r) => (
                <button
                  key={r}
                  onClick={() => onTimeRangeChange(r)}
                  className={`px-3 py-1 text-xs font-medium rounded-md transition-all ${
                    timeRange === r
                      ? "bg-white text-gray-900 shadow-sm"
                      : "text-gray-500 hover:text-gray-700"
                  }`}
                >
                  {RANGE_LABELS[r]}
                </button>
              ))}
            </div>
            {/* Export button */}
            <button
              onClick={handleExport}
              disabled={sensors.length === 0}
              className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium
                         text-gray-600 bg-gray-100 rounded-lg hover:bg-gray-200
                         disabled:opacity-40 disabled:cursor-not-allowed transition-all"
            >
              <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              XLSX
            </button>
          </div>
        </div>
      </div>

      {/* ── Sensor Cards ────────────────────────────────────────── */}
      {sensors.length > 0 && (
        <div className="px-6 py-4 border-b border-gray-100">
          <div className="flex gap-3 overflow-x-auto pb-1">
            {sensors.map((s) => {
              const sig = signalStrength(s.latestRssi);
              const tempF = s.latestTemp * 9 / 5 + 32;
              return (
                <div
                  key={s.address}
                  className="flex-shrink-0 min-w-[200px] rounded-xl border border-gray-200
                             bg-gradient-to-br from-gray-50 to-white p-4 space-y-3"
                >
                  {/* Header */}
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-2">
                      <div
                        className="w-2.5 h-2.5 rounded-full mt-0.5"
                        style={{ backgroundColor: s.color }}
                      />
                      <div>
                        <p className="text-sm font-semibold text-gray-900">
                          SN {s.serial}
                        </p>
                        <p className="text-[10px] text-gray-400">{s.model}</p>
                      </div>
                    </div>
                    <div className={`flex items-center gap-1 ${sig.color}`}>
                      <SignalBars bars={sig.bars} />
                      <span className="text-[10px]">{s.latestRssi ?? "?"} dBm</span>
                    </div>
                  </div>

                  {/* Temperature */}
                  <div className="text-center py-1">
                    <p className="text-3xl font-bold" style={{ color: s.color }}>
                      {s.latestTemp.toFixed(1)}
                      <span className="text-lg font-normal text-gray-400">°C</span>
                    </p>
                    <p className="text-xs text-gray-400 mt-0.5">
                      {tempF.toFixed(1)}°F
                    </p>
                  </div>

                  {/* Stats */}
                  <div className="flex justify-between text-[10px] text-gray-500 border-t border-gray-100 pt-2">
                    <span>
                      Min <strong className="text-blue-600">{s.minTemp.toFixed(1)}°</strong>
                    </span>
                    <span>
                      Avg <strong className="text-gray-700">{s.avgTemp.toFixed(1)}°</strong>
                    </span>
                    <span>
                      Max <strong className="text-red-500">{s.maxTemp.toFixed(1)}°</strong>
                    </span>
                  </div>

                  {/* Last seen */}
                  <p className="text-[10px] text-gray-400 text-center">
                    Last seen{" "}
                    {formatDistanceToNow(s.latestTime, { addSuffix: true })}
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Chart ───────────────────────────────────────────────── */}
      <div className="px-6 py-4">
        {chartData.length > 1 ? (
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
              <XAxis
                dataKey="time"
                tick={{ fontSize: 11 }}
                interval="preserveStartEnd"
              />
              <YAxis
                tick={{ fontSize: 11 }}
                domain={["auto", "auto"]}
                label={{
                  value: "°C",
                  angle: -90,
                  position: "insideLeft",
                  style: { fontSize: 12, fill: "#9ca3af" },
                }}
              />
              <Tooltip
                contentStyle={{
                  borderRadius: "10px",
                  border: "1px solid #e5e7eb",
                  boxShadow: "0 4px 6px -1px rgb(0 0 0 / 0.05)",
                  fontSize: 12,
                }}
                formatter={(value: number, name: string) => {
                  const addr = name.replace("temp_", "");
                  const sensor = sensors.find((s) => s.address === addr);
                  const label = sensor ? `SN ${sensor.serial}` : addr.slice(-8);
                  return [`${value}°C / ${(value * 9/5 + 32).toFixed(1)}°F`, label];
                }}
                labelStyle={{ fontWeight: "bold" }}
              />
              <Legend
                formatter={(value: string) => {
                  const addr = value.replace("temp_", "");
                  const sensor = sensors.find((s) => s.address === addr);
                  return sensor ? `SN ${sensor.serial}` : addr.slice(-8);
                }}
                wrapperStyle={{ fontSize: 11 }}
              />
              {sensors.map((s) => (
                <Line
                  key={s.address}
                  type="monotone"
                  dataKey={`temp_${s.address}`}
                  stroke={s.color}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4, fill: s.color }}
                  connectNulls
                  name={`temp_${s.address}`}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-[280px] flex flex-col items-center justify-center text-gray-400 gap-2">
            <svg className="w-10 h-10" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v2.25m6.364.386l-1.591 1.591M21 12h-2.25m-.386 6.364l-1.591-1.591M12 18.75V21m-4.773-4.227l-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z" />
            </svg>
            <p className="text-sm">
              {readings.length === 0
                ? "No temperature readings yet"
                : "Collecting data..."}
            </p>
            <p className="text-xs">
              Ensure HOBO sensors are powered with Bluetooth Always On enabled
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
