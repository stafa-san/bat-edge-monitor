"use client";

interface StatsCardsProps {
  classifications: any[];
  batDetections: any[];
}

export function StatsCards({ classifications, batDetections }: StatsCardsProps) {
  const avgSpl = classifications.length > 0
    ? classifications.reduce((sum, c) => sum + (c.spl || 0), 0) / classifications.length
    : 0;

  const uniqueLabels = new Set(classifications.map((c) => c.label)).size;

  const stats = [
    {
      label: "Classifications",
      value: classifications.length.toString(),
      icon: "🎵",
      color: "bg-blue-50 text-blue-700",
    },
    {
      label: "Bat Detections",
      value: batDetections.length.toString(),
      icon: "🦇",
      color: "bg-purple-50 text-purple-700",
    },
    {
      label: "Avg SPL",
      value: `${avgSpl.toFixed(1)} dB`,
      icon: "📊",
      color: "bg-green-50 text-green-700",
    },
    {
      label: "Sound Classes",
      value: uniqueLabels.toString(),
      icon: "🏷️",
      color: "bg-amber-50 text-amber-700",
    },
  ];

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      {stats.map((stat) => (
        <div
          key={stat.label}
          className="bg-white rounded-xl shadow-sm border border-gray-200 p-5"
        >
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-gray-500">
              {stat.label}
            </span>
            <span className="text-xl">{stat.icon}</span>
          </div>
          <p className={`text-2xl font-bold ${stat.color} inline-block px-2 py-0.5 rounded`}>
            {stat.value}
          </p>
        </div>
      ))}
    </div>
  );
}
