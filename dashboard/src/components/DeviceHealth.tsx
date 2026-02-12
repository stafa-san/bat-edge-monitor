"use client";

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
  recordedAt: Timestamp;
}

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
/*  Main component                                                     */
/* ------------------------------------------------------------------ */

export function DeviceHealth({ status }: { status: DeviceStatus | null }) {
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

  const lastSeen = status.recordedAt?.toDate
    ? status.recordedAt.toDate()
    : null;
  const staleMs = lastSeen ? Date.now() - lastSeen.getTime() : Infinity;
  const isStale = staleMs > 3 * 60 * 1000; // >3 min = stale

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <h2 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
          🖥️ Device Health
        </h2>
        <div className="flex items-center gap-2">
          <div
            className={`w-2.5 h-2.5 rounded-full ${
              isStale ? "bg-yellow-400 animate-pulse" : "bg-green-500"
            }`}
          />
          <span className="text-sm text-gray-500">
            {lastSeen ? lastSeen.toLocaleTimeString() : "—"}
          </span>
        </div>
      </div>

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
        </div>
      </div>

      {/* ── AudioMoth ── */}
      <div>
        <h3 className="text-sm font-medium text-gray-700 mb-3 flex items-center gap-1.5">
          🎙️ AudioMoth USB Microphone
        </h3>
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <MetricCard
            label="Status"
            value={status.audiomothConnected ? "Capturing" : "Inactive"}
            sub={status.audiomothConnected ? "Data received <5 min ago" : "No recent data"}
            color={
              status.audiomothConnected ? "text-green-600" : "text-red-600"
            }
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
    </div>
  );
}
