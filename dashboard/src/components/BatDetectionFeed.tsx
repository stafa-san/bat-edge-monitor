"use client";

const GROUP_LABELS: Record<string, { common: string; desc: string }> = {
  EPFU_LANO: { common: "Big Brown / Silver-haired", desc: "Eptesicus fuscus + Lasionycteris noctivagans" },
  LABO: { common: "Eastern Red Bat", desc: "Lasiurus borealis" },
  LACI: { common: "Hoary Bat", desc: "Lasiurus cinereus" },
  MYSP: { common: "Myotis spp.", desc: "Little Brown, Northern Long-eared, Indiana, Eastern Small-footed" },
  PESU: { common: "Tri-colored Bat", desc: "Perimyotis subflavus" },
};

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
          {detections.map((det) => {
            const group = det.predictedClass || det.species;
            const info = GROUP_LABELS[group];
            const confidence = det.predictionConfidence || det.detectionProb || 0;

            return (
              <div
                key={det.id}
                className="flex items-start gap-3 p-3 bg-purple-50 rounded-lg border border-purple-100"
              >
                <div className="w-10 h-10 bg-purple-200 rounded-full flex items-center justify-center text-lg shrink-0">
                  🦇
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 min-w-0">
                      <p className="font-semibold text-purple-900 truncate">
                        {info?.common || group || det.species}
                      </p>
                      {det.predictedClass && (
                        <span className="text-[10px] font-mono bg-indigo-100 text-indigo-700 px-1.5 py-0.5 rounded shrink-0">
                          {det.predictedClass}
                        </span>
                      )}
                      {det.source === "upload" && (
                        <span className="text-[10px] font-medium bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded shrink-0">
                          UPLOAD
                        </span>
                      )}
                    </div>
                    <span className="text-xs text-purple-600 bg-purple-100 px-2 py-0.5 rounded-full shrink-0">
                      {(confidence * 100).toFixed(0)}%
                    </span>
                  </div>
                  {info && (
                    <p className="text-[11px] text-purple-500 italic mt-0.5">
                      {info.desc}
                    </p>
                  )}
                  <div className="flex gap-4 mt-1 text-xs text-purple-700">
                    <span>
                      {(det.lowFreq / 1000).toFixed(1)}–
                      {(det.highFreq / 1000).toFixed(1)} kHz
                    </span>
                    <span>{det.durationMs?.toFixed(0)} ms</span>
                    {det.modelVersion && (
                      <span className="text-purple-400">
                        {det.modelVersion}
                      </span>
                    )}
                  </div>
                  {det.audioUrl && (
                    <audio
                      controls
                      preload="none"
                      className="mt-2 w-full h-8"
                      src={det.audioUrl}
                    >
                      Your browser does not support audio playback.
                    </audio>
                  )}
                  <p className="text-xs text-gray-400 mt-1">
                    {det.detectionTime?.toDate
                      ? det.detectionTime.toDate().toLocaleString()
                      : "—"}
                  </p>
                </div>
              </div>
            );
          })}
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
