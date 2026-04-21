"use client";

import { useState } from "react";
import { Timestamp } from "firebase/firestore";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

export interface DeviceStatus {
  uptimeSeconds: number;
  cpuTemp: number;
  cpuLoad1m: number;
  cpuLoad5m: number;
  cpuLoad15m: number;
  memTotalMb: number;
  memAvailableMb: number;
  diskTotalGb: number;
  diskUsedGb: number;
  internetConnected: boolean;
  internetLatencyMs: number | null;
  audiomothConnected: boolean;
  captureErrors1h: number;
  dbSizeMb: number;
  classificationsTotal: number;
  batDetectionsTotal: number;
  unsyncedCount: number;
  sampleRateHz?: number;
  audiomothHwSampleRate?: number | null;
  // Power / undervoltage diagnostics (Pi 5 via vcgencmd)
  throttledHex?: string | null;
  undervoltNow?: boolean | null;
  undervoltSinceBoot?: boolean | null;
  throttledNow?: boolean | null;
  throttledSinceBoot?: boolean | null;
  freqCappedNow?: boolean | null;
  freqCappedSinceBoot?: boolean | null;
  coreVoltage?: number | null;
  ext5vVoltage?: number | null;
  // Audio RMS telemetry from batdetect-service
  audioRmsLatest?: number | null;
  audioRmsAvg1m?: number | null;
  audioPeakLatest?: number | null;
  recordedAt: Timestamp;
  lastSeen?: Timestamp;
  lastOffline?: Timestamp;
  lastOfflineDuration?: number;
}

export interface HealthSnapshot {
  id: string;
  uptimeSeconds: number;
  cpuTemp: number;
  cpuLoad1m: number;
  memTotalMb: number;
  memAvailableMb: number;
  diskTotalGb: number;
  diskUsedGb: number;
  internetConnected: boolean;
  audiomothConnected: boolean;
  recordedAt: Timestamp;
}

export type HistoryRange = "1h" | "6h" | "24h" | "7d";

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function formatUptime(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatDuration(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${Math.round(seconds)}s`;
}

function timeAgo(date: Date | null): string {
  if (!date) return "—";
  const s = Math.floor((Date.now() - date.getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function pct(used: number, total: number): string {
  if (!total) return "—";
  return `${((used / total) * 100).toFixed(0)}%`;
}

function tempColor(t: number | null | undefined): string {
  if (t == null) return "text-gray-900";
  if (t >= 70) return "text-red-600";
  if (t >= 60) return "text-yellow-600";
  return "text-green-600";
}

function errorColor(n: number): string {
  if (n >= 5) return "text-red-600";
  if (n > 0) return "text-yellow-600";
  return "text-green-600";
}

function formatSampleRate(hz: number | null | undefined): string {
  if (!hz) return "—";
  return `${(hz / 1000).toFixed(0)} kHz`;
}

/* ------------------------------------------------------------------ */
/*  Power / undervoltage helpers                                       */
/* ------------------------------------------------------------------ */

type PowerFields = Pick<
  DeviceStatus,
  "undervoltNow" | "undervoltSinceBoot" | "throttledNow" | "throttledSinceBoot"
>;

function powerStateLabel(s: PowerFields): string {
  if (s.undervoltNow === true || s.throttledNow === true) return "Undervolting NOW";
  if (s.undervoltSinceBoot === true || s.throttledSinceBoot === true) return "Recovered";
  if (s.undervoltSinceBoot === false && s.throttledSinceBoot === false) return "Stable";
  return "—";
}

function powerStateSub(s: PowerFields): string | undefined {
  if (s.undervoltNow === true) return "Pi is below 4.6 V right now";
  if (s.throttledNow === true) return "CPU throttled right now";
  if (s.undervoltSinceBoot === true) return "Undervoltage occurred since boot";
  if (s.throttledSinceBoot === true) return "Throttling occurred since boot";
  if (s.undervoltSinceBoot === false && s.throttledSinceBoot === false) return "no undervoltage since boot";
  return undefined;
}

function powerStateColor(s: PowerFields): string {
  if (s.undervoltNow === true || s.throttledNow === true) return "text-red-600";
  if (s.undervoltSinceBoot === true || s.throttledSinceBoot === true) return "text-yellow-600";
  if (s.undervoltSinceBoot === false && s.throttledSinceBoot === false) return "text-green-600";
  return "text-gray-900";
}

function voltageColor(v: number | null | undefined): string {
  if (v == null) return "text-gray-900";
  if (v < 4.8) return "text-red-600";
  if (v < 5.0) return "text-yellow-600";
  return "text-green-600";
}

/* ------------------------------------------------------------------ */
/*  Audio RMS helpers                                                  */
/* ------------------------------------------------------------------ */

function formatRms(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toFixed(4);
}

function rmsColor(v: number | null | undefined): string {
  if (v == null) return "text-gray-900";
  if (v < 0.002) return "text-red-600";    // essentially silent
  if (v < 0.01) return "text-yellow-600";  // very quiet / possibly undervolted
  if (v > 0.3) return "text-yellow-600";   // clipping risk
  return "text-green-600";
}

function rmsSubLabel(
  v: number | null | undefined,
  power: Pick<DeviceStatus, "undervoltNow" | "undervoltSinceBoot"> | undefined,
): string | undefined {
  if (v == null) return undefined;
  // Power-related phrasing only when undervoltage is actually implicated.
  // When power is clean we don't want to mislead about the cause of
  // low levels — outdoor ambient with the 8 kHz hardware HPF legitimately
  // sits in the 0.005–0.01 range.
  const undervoltFlag =
    power?.undervoltNow === true || power?.undervoltSinceBoot === true;
  if (v < 0.002) {
    return undervoltFlag
      ? "mic silent — check PSU"
      : "mic silent — check hardware";
  }
  if (v < 0.005) {
    return undervoltFlag
      ? "very quiet — PSU may be dipping"
      : "very quiet — normal-low ambient";
  }
  if (v < 0.01) return "listening — low ambient";
  if (v > 0.3) return "loud / clipping risk";
  return "normal ambient";
}

function tsToDate(ts: Timestamp | undefined | null): Date | null {
  if (!ts) return null;
  if (typeof ts.toDate === "function") return ts.toDate();
  return null;
}

/* ------------------------------------------------------------------ */
/*  Metric card                                                        */
/* ------------------------------------------------------------------ */

function MetricCard({
  label,
  value,
  sub,
  color = "text-gray-900",
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
}) {
  return (
    <div className="bg-gray-50 rounded-lg p-3">
      <p className="text-xs font-medium text-gray-500 mb-1">{label}</p>
      <p className={`text-lg font-semibold ${color}`}>{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Progress bar (memory / disk)                                       */
/* ------------------------------------------------------------------ */

function Bar({ value, max, warn = 80 }: { value: number; max: number; warn?: number }) {
  const p = max > 0 ? (value / max) * 100 : 0;
  const barColor =
    p >= warn ? "bg-red-500" : p >= warn * 0.75 ? "bg-yellow-500" : "bg-blue-500";
  return (
    <div className="w-full bg-gray-200 rounded-full h-1.5 mt-1">
      <div
        className={`${barColor} h-1.5 rounded-full transition-all`}
        style={{ width: `${Math.min(p, 100)}%` }}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Mini sparkline (inline SVG for history charts)                     */
/* ------------------------------------------------------------------ */

function Sparkline({
  data,
  color = "#3b82f6",
  height = 40,
  width = "100%",
}: {
  data: number[];
  color?: string;
  height?: number;
  width?: string | number;
}) {
  if (data.length < 2) {
    return <p className="text-xs text-gray-400 italic">Not enough data yet</p>;
  }
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const svgW = 300;
  const step = svgW / (data.length - 1);
  const points = data
    .map((v, i) => `${i * step},${height - ((v - min) / range) * (height - 4) - 2}`)
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${svgW} ${height}`}
      preserveAspectRatio="none"
      style={{ width, height }}
      className="block"
    >
      <polyline
        fill="none"
        stroke={color}
        strokeWidth="2"
        strokeLinejoin="round"
        strokeLinecap="round"
        points={points}
      />
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/*  History panel                                                      */
/* ------------------------------------------------------------------ */

function HistoryPanel({
  history,
  range,
  onRangeChange,
}: {
  history: HealthSnapshot[];
  range: HistoryRange;
  onRangeChange: (r: HistoryRange) => void;
}) {
  const ranges: { key: HistoryRange; label: string }[] = [
    { key: "1h", label: "1 Hour" },
    { key: "6h", label: "6 Hours" },
    { key: "24h", label: "24 Hours" },
    { key: "7d", label: "7 Days" },
  ];

  // Sort by time ascending for charts
  const sorted = [...history].sort((a, b) => {
    const aT = tsToDate(a.recordedAt)?.getTime() ?? 0;
    const bT = tsToDate(b.recordedAt)?.getTime() ?? 0;
    return aT - bT;
  });

  const temps = sorted.map((s) => s.cpuTemp).filter((v) => v != null);
  const loads = sorted.map((s) => s.cpuLoad1m).filter((v) => v != null);
  const memPcts = sorted.map((s) => {
    if (!s.memTotalMb) return 0;
    return ((s.memTotalMb - (s.memAvailableMb ?? 0)) / s.memTotalMb) * 100;
  });
  const diskPcts = sorted.map((s) => {
    if (!s.diskTotalGb) return 0;
    return ((s.diskUsedGb ?? 0) / s.diskTotalGb) * 100;
  });

  // Find offline gaps (> 3 min between consecutive snapshots)
  const offlineGaps: { start: Date; end: Date; duration: number }[] = [];
  for (let i = 1; i < sorted.length; i++) {
    const prev = tsToDate(sorted[i - 1].recordedAt);
    const curr = tsToDate(sorted[i].recordedAt);
    if (prev && curr) {
      const gap = (curr.getTime() - prev.getTime()) / 1000;
      if (gap > 180) {
        offlineGaps.push({ start: prev, end: curr, duration: gap });
      }
    }
  }

  const firstSeen = sorted.length > 0 ? tsToDate(sorted[0].recordedAt) : null;
  const lastSeen = sorted.length > 0 ? tsToDate(sorted[sorted.length - 1].recordedAt) : null;

  return (
    <div className="mt-4 border-t border-gray-200 pt-4">
      {/* Range picker */}
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-medium text-gray-700">📈 Health History</h3>
        <div className="flex gap-1">
          {ranges.map((r) => (
            <button
              key={r.key}
              onClick={() => onRangeChange(r.key)}
              className={`px-2.5 py-1 text-xs rounded-md font-medium transition-colors ${
                range === r.key
                  ? "bg-blue-100 text-blue-700"
                  : "bg-gray-100 text-gray-500 hover:bg-gray-200"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {sorted.length === 0 ? (
        <p className="text-sm text-gray-400 italic">
          No history data for this range yet. Data is collected every 60 seconds.
        </p>
      ) : (
        <>
          {/* Summary */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
            <div className="bg-gray-50 rounded-lg p-2.5">
              <p className="text-xs text-gray-500">Snapshots</p>
              <p className="text-sm font-semibold text-gray-900">{sorted.length}</p>
            </div>
            <div className="bg-gray-50 rounded-lg p-2.5">
              <p className="text-xs text-gray-500">Period</p>
              <p className="text-sm font-semibold text-gray-900">
                {firstSeen ? firstSeen.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}
                {" → "}
                {lastSeen ? lastSeen.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}
              </p>
            </div>
            <div className="bg-gray-50 rounded-lg p-2.5">
              <p className="text-xs text-gray-500">Avg Temp</p>
              <p className="text-sm font-semibold text-gray-900">
                {temps.length > 0
                  ? `${(temps.reduce((a, b) => a + b, 0) / temps.length).toFixed(1)}°C`
                  : "—"}
              </p>
            </div>
            <div className="bg-gray-50 rounded-lg p-2.5">
              <p className="text-xs text-gray-500">Offline Gaps</p>
              <p className={`text-sm font-semibold ${offlineGaps.length > 0 ? "text-red-600" : "text-green-600"}`}>
                {offlineGaps.length}
              </p>
            </div>
          </div>

          {/* Sparkline charts */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
            <div className="bg-gray-50 rounded-lg p-3">
              <p className="text-xs font-medium text-gray-500 mb-2">🌡️ CPU Temperature</p>
              <Sparkline data={temps} color="#ef4444" />
              <div className="flex justify-between text-[10px] text-gray-400 mt-1">
                <span>{temps.length > 0 ? `${Math.min(...temps).toFixed(1)}°C` : ""}</span>
                <span>{temps.length > 0 ? `${Math.max(...temps).toFixed(1)}°C` : ""}</span>
              </div>
            </div>
            <div className="bg-gray-50 rounded-lg p-3">
              <p className="text-xs font-medium text-gray-500 mb-2">⚡ CPU Load</p>
              <Sparkline data={loads} color="#f59e0b" />
              <div className="flex justify-between text-[10px] text-gray-400 mt-1">
                <span>{loads.length > 0 ? Math.min(...loads).toFixed(2) : ""}</span>
                <span>{loads.length > 0 ? Math.max(...loads).toFixed(2) : ""}</span>
              </div>
            </div>
            <div className="bg-gray-50 rounded-lg p-3">
              <p className="text-xs font-medium text-gray-500 mb-2">🧠 Memory Usage</p>
              <Sparkline data={memPcts} color="#3b82f6" />
              <div className="flex justify-between text-[10px] text-gray-400 mt-1">
                <span>{memPcts.length > 0 ? `${Math.min(...memPcts).toFixed(0)}%` : ""}</span>
                <span>{memPcts.length > 0 ? `${Math.max(...memPcts).toFixed(0)}%` : ""}</span>
              </div>
            </div>
            <div className="bg-gray-50 rounded-lg p-3">
              <p className="text-xs font-medium text-gray-500 mb-2">💾 Disk Usage</p>
              <Sparkline data={diskPcts} color="#8b5cf6" />
              <div className="flex justify-between text-[10px] text-gray-400 mt-1">
                <span>{diskPcts.length > 0 ? `${Math.min(...diskPcts).toFixed(0)}%` : ""}</span>
                <span>{diskPcts.length > 0 ? `${Math.max(...diskPcts).toFixed(0)}%` : ""}</span>
              </div>
            </div>
          </div>

          {/* Offline gaps log */}
          {offlineGaps.length > 0 && (
            <div className="bg-red-50 rounded-lg p-3">
              <p className="text-xs font-medium text-red-700 mb-2">
                ⚠️ Offline Periods Detected
              </p>
              <div className="space-y-1.5">
                {offlineGaps.map((gap, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs">
                    <span className="w-2 h-2 rounded-full bg-red-400 flex-shrink-0" />
                    <span className="text-gray-600">
                      {gap.start.toLocaleString([], {
                        month: "short", day: "numeric",
                        hour: "2-digit", minute: "2-digit",
                      })}
                      {" → "}
                      {gap.end.toLocaleString([], {
                        month: "short", day: "numeric",
                        hour: "2-digit", minute: "2-digit",
                      })}
                    </span>
                    <span className="text-red-600 font-medium">
                      ({formatDuration(gap.duration)})
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main component                                                     */
/* ------------------------------------------------------------------ */

export function DeviceHealth({
  status,
  history = [],
  historyRange = "1h",
  onHistoryRangeChange,
}: {
  status: DeviceStatus | null;
  history?: HealthSnapshot[];
  historyRange?: HistoryRange;
  onHistoryRangeChange?: (r: HistoryRange) => void;
}) {
  const [showHistory, setShowHistory] = useState(false);

  if (!status) {
    return (
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
        <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
          🖥️ Device Health
        </h2>
        <p className="text-gray-400 text-sm mt-2">
          Waiting for first heartbeat…
        </p>
      </div>
    );
  }

  /* derived values */
  const memUsed = (status.memTotalMb ?? 0) - (status.memAvailableMb ?? 0);
  const memPct = pct(memUsed, status.memTotalMb);
  const diskPct = pct(status.diskUsedGb, status.diskTotalGb);

  const lastSeenDate = tsToDate(status.lastSeen) ?? tsToDate(status.recordedAt);
  const staleMs = lastSeenDate ? Date.now() - lastSeenDate.getTime() : Infinity;
  const isStale = staleMs > 3 * 60 * 1000; // >3 min = stale
  const isOffline = staleMs > 5 * 60 * 1000; // >5 min = offline

  const lastOfflineDate = tsToDate(status.lastOffline);

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
          🖥️ Device Health
        </h2>
        <div className="flex items-center gap-2">
          <div
            className={`w-2.5 h-2.5 rounded-full ${
              isOffline
                ? "bg-red-500"
                : isStale
                  ? "bg-yellow-400 animate-pulse"
                  : "bg-green-500"
            }`}
          />
          <span className="text-sm text-gray-500">
            {lastSeenDate ? lastSeenDate.toLocaleTimeString() : "—"}
          </span>
        </div>
      </div>

      {/* ── Connection status banner ── */}
      <div className={`rounded-lg p-3 mb-4 ${
        isOffline
          ? "bg-red-50 border border-red-200"
          : isStale
            ? "bg-yellow-50 border border-yellow-200"
            : "bg-green-50 border border-green-200"
      }`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-sm">
              {isOffline ? "🔴" : isStale ? "🟡" : "🟢"}
            </span>
            <div>
              <p className={`text-sm font-medium ${
                isOffline ? "text-red-700" : isStale ? "text-yellow-700" : "text-green-700"
              }`}>
                {isOffline ? "Device Offline" : isStale ? "Connection Stale" : "Device Online"}
              </p>
              <p className="text-xs text-gray-500">
                Last seen: {lastSeenDate ? `${timeAgo(lastSeenDate)} (${lastSeenDate.toLocaleString()})` : "Never"}
              </p>
            </div>
          </div>
          {lastOfflineDate && (
            <div className="text-right">
              <p className="text-xs text-gray-500">Last went offline</p>
              <p className="text-xs font-medium text-gray-700">
                {lastOfflineDate.toLocaleString([], {
                  month: "short", day: "numeric",
                  hour: "2-digit", minute: "2-digit",
                })}
              </p>
              {status.lastOfflineDuration != null && (
                <p className="text-xs text-red-600">
                  Down for {formatDuration(status.lastOfflineDuration)}
                </p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Undervoltage banner ── shows only if Pi is misbehaving right now */}
      {(status.undervoltNow === true || status.throttledNow === true) && (
        <div className="mb-4 px-4 py-3 rounded-lg border border-red-200 bg-red-50 flex items-start gap-3">
          <span className="text-xl">⚠️</span>
          <div className="flex-1">
            <p className="text-sm font-semibold text-red-800">
              {status.undervoltNow === true
                ? "Undervoltage right now"
                : "CPU throttled right now"}
            </p>
            <p className="text-xs text-red-700 mt-0.5">
              The power supply isn't delivering 5.1 V · 5 A. AudioMoth mic
              sensitivity will be reduced until the PSU is replaced.
              {status.ext5vVoltage != null
                ? ` 5V rail currently ${status.ext5vVoltage.toFixed(2)} V.`
                : ""}
            </p>
          </div>
        </div>
      )}
      {(status.undervoltNow !== true &&
        status.throttledNow !== true &&
        (status.undervoltSinceBoot === true || status.throttledSinceBoot === true)) && (
        <div className="mb-4 px-4 py-3 rounded-lg border border-yellow-200 bg-yellow-50 flex items-start gap-3">
          <span className="text-xl">⚡</span>
          <div className="flex-1">
            <p className="text-sm font-semibold text-yellow-800">
              Undervoltage has occurred since last boot
            </p>
            <p className="text-xs text-yellow-700 mt-0.5">
              The Pi is running normally right now, but the power supply
              dipped below 4.6 V at least once this session. Consider
              upgrading the PSU before the next deployment.
            </p>
          </div>
        </div>
      )}

      {/* ── Stale-data banner: device is offline, so every metric below is
          the last value we received before it went silent. Banner makes
          that explicit instead of letting stale numbers look live. ── */}
      {isOffline && lastSeenDate && (
        <div className="mb-4 px-4 py-3 rounded-lg border border-gray-300 bg-gray-100 flex items-start gap-3">
          <span className="text-xl">🕒</span>
          <div className="flex-1">
            <p className="text-sm font-semibold text-gray-700">
              Showing last-known values
            </p>
            <p className="text-xs text-gray-600 mt-0.5">
              The Pi hasn't reported in {timeAgo(lastSeenDate)}. CPU,
              memory, internet, audio level and everything below reflect
              the state at {lastSeenDate.toLocaleString([], {
                month: "short", day: "numeric",
                hour: "2-digit", minute: "2-digit",
              })}, not right now.
            </p>
          </div>
        </div>
      )}

      {/* Wrap Pi + AudioMoth metrics in a stale-styled block when offline so
          the numbers are visibly "not live" rather than indistinguishable
          from a healthy reading. */}
      <div className={isOffline ? "opacity-60 grayscale" : ""}>

      {/* ── Raspberry Pi ── */}
      <div className="mb-5">
        <h3 className="text-sm font-medium text-gray-700 mb-3 flex items-center gap-1.5">
          🍓 Raspberry Pi 5
        </h3>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <MetricCard
            label="Uptime"
            value={formatUptime(status.uptimeSeconds)}
          />
          <MetricCard
            label="CPU Temp"
            value={`${status.cpuTemp?.toFixed(1) ?? "—"}°C`}
            color={tempColor(status.cpuTemp)}
          />
          <MetricCard
            label="CPU Load"
            value={status.cpuLoad1m?.toFixed(2) ?? "—"}
            sub={`5m ${status.cpuLoad5m?.toFixed(2) ?? "—"} · 15m ${status.cpuLoad15m?.toFixed(2) ?? "—"}`}
          />
          <div className="bg-gray-50 rounded-lg p-3">
            <p className="text-xs font-medium text-gray-500 mb-1">Memory</p>
            <p className="text-lg font-semibold text-gray-900">{memPct}</p>
            <p className="text-xs text-gray-400">
              {(memUsed / 1024).toFixed(1)} / {(status.memTotalMb / 1024).toFixed(1)} GB
            </p>
            <Bar value={memUsed} max={status.memTotalMb} />
          </div>
          <div className="bg-gray-50 rounded-lg p-3">
            <p className="text-xs font-medium text-gray-500 mb-1">Disk</p>
            <p className="text-lg font-semibold text-gray-900">{diskPct}</p>
            <p className="text-xs text-gray-400">
              {status.diskUsedGb?.toFixed(0) ?? "—"} / {status.diskTotalGb?.toFixed(0) ?? "—"} GB
            </p>
            <Bar value={status.diskUsedGb} max={status.diskTotalGb} />
          </div>
          <MetricCard
            label="Internet"
            value={status.internetConnected ? "Connected" : "Offline"}
            sub={
              status.internetLatencyMs != null
                ? `${status.internetLatencyMs.toFixed(0)} ms latency`
                : undefined
            }
            color={
              status.internetConnected ? "text-green-600" : "text-red-600"
            }
          />
          <MetricCard
            label="Power"
            value={powerStateLabel(status)}
            sub={powerStateSub(status)}
            color={powerStateColor(status)}
          />
          <MetricCard
            label="5 V Rail"
            value={
              status.ext5vVoltage != null
                ? `${status.ext5vVoltage.toFixed(2)} V`
                : "—"
            }
            sub={
              status.coreVoltage != null
                ? `core ${status.coreVoltage.toFixed(2)} V`
                : undefined
            }
            color={voltageColor(status.ext5vVoltage)}
          />
        </div>
      </div>

      {/* ── AudioMoth ── */}
      <div>
        <h3 className="text-sm font-medium text-gray-700 mb-3 flex items-center gap-1.5">
          🎙️ AudioMoth USB Microphone
        </h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <MetricCard
            label="Status"
            value={status.audiomothConnected ? "Capturing" : "Inactive"}
            sub={status.audiomothConnected ? "Data received <5 min ago" : "No recent data"}
            color={
              status.audiomothConnected ? "text-green-600" : "text-red-600"
            }
          />
          <MetricCard
            label="Sample Rate"
            value={formatSampleRate(status.audiomothHwSampleRate ?? status.sampleRateHz)}
            sub={status.audiomothHwSampleRate
              ? `${status.audiomothHwSampleRate.toLocaleString()} Hz (hardware)`
              : status.sampleRateHz ? `${status.sampleRateHz.toLocaleString()} Hz` : undefined}
            color="text-blue-600"
          />
          <MetricCard
            label="Audio Level"
            value={formatRms(status.audioRmsLatest)}
            sub={
              status.audioRmsAvg1m != null
                ? `${rmsSubLabel(status.audioRmsLatest, status) ?? ""} · 1m avg ${formatRms(status.audioRmsAvg1m)}`.trim()
                : rmsSubLabel(status.audioRmsLatest, status)
            }
            color={rmsColor(status.audioRmsLatest)}
          />
          <MetricCard
            label="Database"
            value={`${status.dbSizeMb?.toFixed(1) ?? "—"} MB`}
            sub={`${status.classificationsTotal?.toLocaleString() ?? 0} rows · ${status.unsyncedCount ?? 0} unsynced`}
          />
          <MetricCard
            label="Errors (1 h)"
            value={`${status.captureErrors1h ?? 0}`}
            sub={`${status.batDetectionsTotal ?? 0} bat detections total`}
            color={errorColor(status.captureErrors1h ?? 0)}
          />
        </div>
      </div>

      </div>{/* end stale-styled Pi+AudioMoth wrapper */}

      {/* ── View History toggle ── */}
      <div className="mt-4 flex justify-center">
        <button
          onClick={() => setShowHistory(!showHistory)}
          className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-blue-600 bg-blue-50 hover:bg-blue-100 rounded-lg transition-colors"
        >
          <span>{showHistory ? "▲" : "▼"}</span>
          {showHistory ? "Hide History" : "View History"}
        </button>
      </div>

      {/* ── Expandable history panel ── */}
      {showHistory && (
        <HistoryPanel
          history={history}
          range={historyRange}
          onRangeChange={onHistoryRangeChange ?? (() => {})}
        />
      )}
    </div>
  );
}
