"use client";

import { BatDetectionRow, GROUP_LABELS } from "./BatDetectionRow";

interface BatDetectionFeedProps {
  detections: any[];
  // Authoritative total from the edge device (all rows in Postgres).
  // The ``detections`` array is capped at whatever the Firestore query
  // limit is (currently 50) so we show both numbers to avoid
  // undercounting in the summary.
  totalDetections?: number;
}

export function BatDetectionFeed({ detections, totalDetections }: BatDetectionFeedProps) {
  // Group detections by predicted class for the summary bar
  const groupCounts: Record<string, number> = {};
  detections.forEach((d) => {
    const group = d.predictedClass || d.species || "Unknown";
    groupCounts[group] = (groupCounts[group] || 0) + 1;
  });

  const sortedGroups = Object.entries(groupCounts).sort((a, b) => b[1] - a[1]);

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">
          🦇 Bat Detection Feed
        </h2>
        {detections.length > 0 && (
          <span className="text-sm text-purple-600 font-medium">
            {typeof totalDetections === "number" && totalDetections > detections.length
              ? `last ${detections.length} of ${totalDetections.toLocaleString()}`
              : `${detections.length} detection${detections.length !== 1 ? "s" : ""}`}
          </span>
        )}
      </div>

      {/* Species group summary chips */}
      {sortedGroups.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-4">
          {sortedGroups.map(([group, count]) => {
            const info = GROUP_LABELS[group];
            return (
              <span
                key={group}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-purple-50 text-purple-800 rounded-full text-xs font-medium border border-purple-200"
                title={info?.desc || group}
              >
                <span>{info?.common || group}</span>
                <span className="bg-purple-200 text-purple-900 px-1.5 py-0.5 rounded-full text-[10px]">
                  {count}
                </span>
              </span>
            );
          })}
        </div>
      )}

      {detections.length > 0 ? (
        <div className="space-y-3 max-h-[500px] overflow-y-auto">
          {detections.map((det) => (
            <BatDetectionRow key={det.id} det={det} />
          ))}
        </div>
      ) : (
        <div className="h-[200px] flex flex-col items-center justify-center text-gray-400">
          <span className="text-4xl mb-2">🦇</span>
          <p className="font-medium">Listening for bat calls...</p>
          <p className="text-xs mt-1">
            Detections will appear here in real-time when echolocation calls are captured
          </p>
        </div>
      )}
    </div>
  );
}
