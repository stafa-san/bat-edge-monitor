"use client";

interface BatDetectionFeedProps {
  detections: any[];
}

export function BatDetectionFeed({ detections }: BatDetectionFeedProps) {
  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
      <h2 className="text-lg font-semibold text-gray-900 mb-4">
        🦇 Bat Detection Feed
      </h2>
      {detections.length > 0 ? (
        <div className="space-y-3 max-h-[300px] overflow-y-auto">
          {detections.map((det) => (
            <div
              key={det.id}
              className="flex items-start gap-3 p-3 bg-purple-50 rounded-lg border border-purple-100"
            >
              <div className="w-10 h-10 bg-purple-200 rounded-full flex items-center justify-center text-lg shrink-0">
                🦇
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between">
                  <p className="font-semibold text-purple-900 truncate">
                    {det.species}
                  </p>
                  <span className="text-xs text-purple-600 bg-purple-100 px-2 py-0.5 rounded-full shrink-0">
                    {(det.detectionProb * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="flex gap-4 mt-1 text-xs text-purple-700">
                  <span>
                    {(det.lowFreq / 1000).toFixed(1)}–
                    {(det.highFreq / 1000).toFixed(1)} kHz
                  </span>
                  <span>{det.durationMs?.toFixed(0)} ms</span>
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
          ))}
        </div>
      ) : (
        <div className="h-[300px] flex flex-col items-center justify-center text-gray-400">
          <span className="text-4xl mb-2">🦇</span>
          <p>No bat detections yet</p>
          <p className="text-xs mt-1">
            Detections will appear here in real-time
          </p>
        </div>
      )}
    </div>
  );
}
