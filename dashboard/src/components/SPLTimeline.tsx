"use client";

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";
import { format } from "date-fns";
import { Timestamp } from "firebase/firestore";

interface SPLTimelineProps {
  classifications: {
    id: string;
    label: string;
    score: number;
    spl: number;
    syncTime: Timestamp;
    syncId: string;
    device: string;
  }[];
}

export function SPLTimeline({ classifications }: SPLTimelineProps) {
  // Group by syncId (each syncId = one 1-second sample with 5 labels)
  // Pick one SPL value per sample (they share the same SPL within a syncId)
  const splBySyncId = new Map<string, { spl: number; time: Date }>();

  classifications.forEach((c) => {
    if (c.spl != null && c.syncTime?.toDate && !splBySyncId.has(c.syncId)) {
      splBySyncId.set(c.syncId, {
        spl: c.spl,
        time: c.syncTime.toDate(),
      });
    }
  });

  const chartData = Array.from(splBySyncId.values())
    .sort((a, b) => a.time.getTime() - b.time.getTime())
    .map((entry) => ({
      time: format(entry.time, "HH:mm:ss"),
      spl: parseFloat(entry.spl.toFixed(1)),
    }));

  // Compute stats
  const splValues = chartData.map((d) => d.spl);
  const avgSpl =
    splValues.length > 0
      ? splValues.reduce((a, b) => a + b, 0) / splValues.length
      : 0;
  const maxSpl = splValues.length > 0 ? Math.max(...splValues) : 0;
  const minSpl = splValues.length > 0 ? Math.min(...splValues) : 0;

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">
          📊 SPL Timeline
        </h2>
        {splValues.length > 0 && (
          <div className="flex gap-4 text-xs text-gray-500">
            <span>
              Avg: <strong className="text-gray-700">{avgSpl.toFixed(1)} dB</strong>
            </span>
            <span>
              Min: <strong className="text-green-600">{minSpl.toFixed(1)} dB</strong>
            </span>
            <span>
              Max: <strong className="text-red-600">{maxSpl.toFixed(1)} dB</strong>
            </span>
          </div>
        )}
      </div>

      {chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis
              dataKey="time"
              tick={{ fontSize: 11 }}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fontSize: 11 }}
              domain={["auto", "auto"]}
              label={{
                value: "dB SPL",
                angle: -90,
                position: "insideLeft",
                style: { fontSize: 12, fill: "#6b7280" },
              }}
            />
            <Tooltip
              formatter={(value: number) => [`${value} dB`, "SPL"]}
              labelStyle={{ fontWeight: "bold" }}
              contentStyle={{
                borderRadius: "8px",
                border: "1px solid #e5e7eb",
              }}
            />
            <ReferenceLine
              y={avgSpl}
              stroke="#9ca3af"
              strokeDasharray="5 5"
              label={{
                value: `Avg: ${avgSpl.toFixed(1)} dB`,
                position: "right",
                style: { fontSize: 10, fill: "#9ca3af" },
              }}
            />
            <Line
              type="monotone"
              dataKey="spl"
              stroke="#22c55e"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4, fill: "#16a34a" }}
            />
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-[300px] flex items-center justify-center text-gray-400">
          Waiting for SPL data...
        </div>
      )}
    </div>
  );
}
