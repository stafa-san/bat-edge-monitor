"use client";

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

interface SoundscapeChartProps {
  classifications: any[];
}

export function SoundscapeChart({ classifications }: SoundscapeChartProps) {
  // Aggregate labels by frequency
  const labelCounts: Record<string, { count: number; avgScore: number }> = {};

  classifications.forEach((c) => {
    if (!labelCounts[c.label]) {
      labelCounts[c.label] = { count: 0, avgScore: 0 };
    }
    labelCounts[c.label].count += 1;
    labelCounts[c.label].avgScore += c.score;
  });

  const chartData = Object.entries(labelCounts)
    .map(([label, data]) => ({
      label,
      count: data.count,
      avgScore: data.avgScore / data.count,
    }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 10);

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
      <h2 className="text-lg font-semibold text-gray-900 mb-4">
        Sound Class Distribution
      </h2>
      {chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height={300}>
          <BarChart data={chartData} layout="vertical">
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis type="number" />
            <YAxis
              type="category"
              dataKey="label"
              width={120}
              tick={{ fontSize: 12 }}
            />
            <Tooltip
              formatter={(value: number, name: string) => [
                name === "count" ? value : `${(value * 100).toFixed(1)}%`,
                name === "count" ? "Occurrences" : "Avg Score",
              ]}
            />
            <Bar dataKey="count" fill="#3b82f6" radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="h-[300px] flex items-center justify-center text-gray-400">
          Waiting for data...
        </div>
      )}
    </div>
  );
}
