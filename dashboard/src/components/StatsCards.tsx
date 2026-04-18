"use client";

interface StatsCardsProps {
  classifications: any[];
  batDetections: any[];
}

export function StatsCards({ classifications, batDetections }: StatsCardsProps) {
  // Bat-focused stats
  const speciesGroups = new Set(
    batDetections
      .map((d: any) => d.predictedClass || d.species)
      .filter(Boolean)
  ).size;

  const avgConfidence =
    batDetections.length > 0
      ? batDetections.reduce(
          (sum: number, d: any) => sum + (d.predictionConfidence || d.detectionProb || 0),
          0
        ) / batDetections.length
      : 0;

  const avgSpl =
    classifications.length > 0
      ? classifications.reduce((sum: number, c: any) => sum + (c.spl || 0), 0) /
        classifications.length
      : 0;

  const stats = [
    {
      label: "Bat Detections",
      value: batDetections.length.toString(),
      icon: "🦇",
      color: "bg-purple-50 text-purple-700",
    },
    {
      label: "Species Groups",
      value: speciesGroups.toString(),
      icon: "🧬",
      color: "bg-indigo-50 text-indigo-700",
    },
    {
      label: "Avg Confidence",
      value: `${(avgConfidence * 100).toFixed(1)}%`,
      icon: "🎯",
      color: "bg-green-50 text-green-700",
    },
    {
      label: "Avg SPL",
      value: `${avgSpl.toFixed(1)} dB`,
      icon: "📊",
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
          <p
            className={`text-2xl font-bold ${stat.color} inline-block px-2 py-0.5 rounded`}
          >
            {stat.value}
          </p>
        </div>
      ))}
    </div>
  );
}
