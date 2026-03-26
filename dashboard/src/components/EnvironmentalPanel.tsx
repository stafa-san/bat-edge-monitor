"use client";

import { useMemo, useCallback, useState, useEffect } from "react";
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

interface SensorLocation {
  lat: string;
  lng: string;
  label: string;
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

const ALIAS_STORAGE_KEY = "env-sensor-aliases";
const LOCATION_STORAGE_KEY = "env-sensor-locations";

// ── Helpers ──────────────────────────────────────────────────────────────

function cToF(c: number): number {
  return c * 9 / 5 + 32;
}

function loadAliases(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    return JSON.parse(localStorage.getItem(ALIAS_STORAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveAliases(aliases: Record<string, string>) {
  localStorage.setItem(ALIAS_STORAGE_KEY, JSON.stringify(aliases));
}

function loadLocations(): Record<string, SensorLocation> {
  if (typeof window === "undefined") return {};
  try {
    return JSON.parse(localStorage.getItem(LOCATION_STORAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveLocations(locations: Record<string, SensorLocation>) {
  localStorage.setItem(LOCATION_STORAGE_KEY, JSON.stringify(locations));
}

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

/** Returns display name for a sensor: alias > serial > address suffix */
function sensorDisplayName(
  address: string,
  serial: string,
  aliases: Record<string, string>
): { primary: string; secondary: string | null } {
  const alias = aliases[address];
  if (alias) {
    return { primary: alias, secondary: serial !== address.slice(-8) ? serial : null };
  }
  if (serial && serial !== address.slice(-8)) {
    return { primary: serial, secondary: null };
  }
  return { primary: address.slice(-8), secondary: null };
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

// ── Inline edit components ───────────────────────────────────────────────

function AliasEditor({
  address,
  currentAlias,
  onSave,
  onClose,
}: {
  address: string;
  currentAlias: string;
  onSave: (address: string, alias: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState(currentAlias);
  return (
    <div className="absolute z-10 top-0 left-0 right-0 bg-white border border-gray-300 rounded-lg shadow-lg p-3 space-y-2">
      <label className="text-[10px] font-medium text-gray-500 uppercase tracking-wide">
        Sensor Alias
      </label>
      <input
        autoFocus
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="e.g. Cave Entrance"
        className="w-full px-2 py-1 text-sm border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-orange-400"
        onKeyDown={(e) => {
          if (e.key === "Enter") { onSave(address, value.trim()); onClose(); }
          if (e.key === "Escape") onClose();
        }}
      />
      <div className="flex justify-end gap-1">
        <button onClick={onClose} className="px-2 py-0.5 text-[10px] text-gray-500 hover:text-gray-700">
          Cancel
        </button>
        <button
          onClick={() => { onSave(address, value.trim()); onClose(); }}
          className="px-2 py-0.5 text-[10px] bg-orange-500 text-white rounded hover:bg-orange-600"
        >
          Save
        </button>
      </div>
    </div>
  );
}

function LocationEditor({
  address,
  current,
  onSave,
  onClose,
}: {
  address: string;
  current: SensorLocation | undefined;
  onSave: (address: string, loc: SensorLocation) => void;
  onClose: () => void;
}) {
  const [label, setLabel] = useState(current?.label || "");
  const [lat, setLat] = useState(current?.lat || "");
  const [lng, setLng] = useState(current?.lng || "");
  return (
    <div className="absolute z-10 top-0 left-0 right-0 bg-white border border-gray-300 rounded-lg shadow-lg p-3 space-y-2">
      <label className="text-[10px] font-medium text-gray-500 uppercase tracking-wide">
        Location
      </label>
      <input
        autoFocus
        type="text"
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        placeholder="Label (e.g. Cave Entrance)"
        className="w-full px-2 py-1 text-sm border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-orange-400"
      />
      <div className="flex gap-2">
        <input
          type="text"
          value={lat}
          onChange={(e) => setLat(e.target.value)}
          placeholder="Latitude"
          className="w-1/2 px-2 py-1 text-xs border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-orange-400"
        />
        <input
          type="text"
          value={lng}
          onChange={(e) => setLng(e.target.value)}
          placeholder="Longitude"
          className="w-1/2 px-2 py-1 text-xs border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-orange-400"
        />
      </div>
      <div className="flex justify-end gap-1">
        <button onClick={onClose} className="px-2 py-0.5 text-[10px] text-gray-500 hover:text-gray-700">
          Cancel
        </button>
        <button
          onClick={() => {
            onSave(address, { label: label.trim(), lat: lat.trim(), lng: lng.trim() });
            onClose();
          }}
          className="px-2 py-0.5 text-[10px] bg-orange-500 text-white rounded hover:bg-orange-600"
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
          }}
        >
          Save
        </button>
      </div>
    </div>
  );
}

// ── Component ────────────────────────────────────────────────────────────

export function EnvironmentalPanel({
  readings,
  timeRange,
  onTimeRangeChange,
}: EnvironmentalPanelProps) {
  // Alias & location state (localStorage-backed)
  const [aliases, setAliases] = useState<Record<string, string>>({});
  const [locations, setLocations] = useState<Record<string, SensorLocation>>({});
  const [editingAlias, setEditingAlias] = useState<string | null>(null);
  const [editingLocation, setEditingLocation] = useState<string | null>(null);

  useEffect(() => {
    setAliases(loadAliases());
    setLocations(loadLocations());
  }, []);

  const handleSaveAlias = useCallback((address: string, alias: string) => {
    setAliases((prev) => {
      const next = { ...prev };
      if (alias) {
        next[address] = alias;
      } else {
        delete next[address];
      }
      saveAliases(next);
      return next;
    });
  }, []);

  const handleSaveLocation = useCallback((address: string, loc: SensorLocation) => {
    setLocations((prev) => {
      const next = { ...prev, [address]: loc };
      saveLocations(next);
      return next;
    });
  }, []);

  // Build a display-label lookup for use in chart tooltip/legend
  const getChartLabel = useCallback(
    (address: string, sensors: SensorSummary[]) => {
      const sensor = sensors.find((s) => s.address === address);
      if (!sensor) return address.slice(-8);
      const { primary } = sensorDisplayName(address, sensor.serial, aliases);
      return primary;
    },
    [aliases]
  );

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

  // Build unified chart data (values stored as °F for chart)
  const chartData = useMemo(() => {
    if (sensors.length === 0) return [];

    const timeFormat = timeRange === "7d" ? "MM/dd HH:mm" : "HH:mm";
    const allPoints = new Map<string, Record<string, number | string>>();

    for (const sensor of sensors) {
      for (const { time, temp } of sensor.readings) {
        const label = format(time, timeFormat);
        if (!allPoints.has(label)) {
          allPoints.set(label, { time: label, _ts: time.getTime() as unknown as string });
        }
        allPoints.get(label)![`temp_${sensor.address}`] = parseFloat(cToF(temp).toFixed(1));
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
        "Temperature (°F)": parseFloat(cToF(r.temp).toFixed(1)),
      }));
      const ws = XLSX.utils.json_to_sheet(data);
      ws["!cols"] = [{ wch: 6 }, { wch: 22 }, { wch: 18 }, { wch: 18 }];
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
              const tempF = cToF(s.latestTemp);
              const { primary, secondary } = sensorDisplayName(s.address, s.serial, aliases);
              const location = locations[s.address];
              return (
                <div
                  key={s.address}
                  className="relative flex-shrink-0 min-w-[200px] rounded-xl border border-gray-200
                             bg-gradient-to-br from-gray-50 to-white p-4 space-y-3"
                >
                  {/* Alias editor overlay */}
                  {editingAlias === s.address && (
                    <AliasEditor
                      address={s.address}
                      currentAlias={aliases[s.address] || ""}
                      onSave={handleSaveAlias}
                      onClose={() => setEditingAlias(null)}
                    />
                  )}

                  {/* Location editor overlay */}
                  {editingLocation === s.address && (
                    <LocationEditor
                      address={s.address}
                      current={locations[s.address]}
                      onSave={handleSaveLocation}
                      onClose={() => setEditingLocation(null)}
                    />
                  )}

                  {/* Header */}
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-2">
                      <div
                        className="w-2.5 h-2.5 rounded-full mt-0.5"
                        style={{ backgroundColor: s.color }}
                      />
                      <div>
                        <button
                          onClick={() => setEditingAlias(s.address)}
                          title="Click to set alias"
                          className="text-sm font-semibold text-gray-900 hover:text-orange-600
                                     transition-colors cursor-pointer text-left leading-tight"
                        >
                          {primary}
                        </button>
                        {secondary && (
                          <p className="text-[10px] text-gray-400">{secondary}</p>
                        )}
                        <p className="text-[10px] text-gray-400">{s.model}</p>
                      </div>
                    </div>
                    <div className={`flex items-center gap-1 ${sig.color}`}>
                      <SignalBars bars={sig.bars} />
                      <span className="text-[10px]">{s.latestRssi ?? "?"} dBm</span>
                    </div>
                  </div>

                  {/* Location badge */}
                  <div className="flex items-center gap-1">
                    {location?.label ? (
                      <button
                        onClick={() => setEditingLocation(s.address)}
                        className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px]
                                   bg-blue-50 text-blue-600 rounded-full hover:bg-blue-100 transition-colors"
                        title={location.lat && location.lng ? `${location.lat}, ${location.lng}` : "Edit location"}
                      >
                        <svg className="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M15 10.5a3 3 0 11-6 0 3 3 0 016 0z" />
                          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25S4.5 17.642 4.5 10.5a7.5 7.5 0 1115 0z" />
                        </svg>
                        {location.label}
                      </button>
                    ) : (
                      <button
                        onClick={() => setEditingLocation(s.address)}
                        className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px]
                                   text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100 transition-colors"
                        title="Add location"
                      >
                        <svg className="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M15 10.5a3 3 0 11-6 0 3 3 0 016 0z" />
                          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25S4.5 17.642 4.5 10.5a7.5 7.5 0 1115 0z" />
                        </svg>
                        Add location
                      </button>
                    )}
                  </div>

                  {/* Temperature — °F primary, °C secondary */}
                  <div className="text-center py-1">
                    <p className="text-3xl font-bold" style={{ color: s.color }}>
                      {tempF.toFixed(1)}
                      <span className="text-lg font-normal text-gray-400">°F</span>
                    </p>
                    <p className="text-xs text-gray-400 mt-0.5">
                      {s.latestTemp.toFixed(1)}°C
                    </p>
                  </div>

                  {/* Stats (°F) */}
                  <div className="flex justify-between text-[10px] text-gray-500 border-t border-gray-100 pt-2">
                    <span>
                      Min <strong className="text-blue-600">{cToF(s.minTemp).toFixed(1)}°F</strong>
                    </span>
                    <span>
                      Avg <strong className="text-gray-700">{cToF(s.avgTemp).toFixed(1)}°F</strong>
                    </span>
                    <span>
                      Max <strong className="text-red-500">{cToF(s.maxTemp).toFixed(1)}°F</strong>
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
                  value: "°F",
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
                  const label = getChartLabel(addr, sensors);
                  const tempC = (value - 32) * 5 / 9;
                  return [`${value}°F / ${tempC.toFixed(1)}°C`, label];
                }}
                labelStyle={{ fontWeight: "bold" }}
              />
              <Legend
                formatter={(value: string) => {
                  const addr = value.replace("temp_", "");
                  return getChartLabel(addr, sensors);
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
