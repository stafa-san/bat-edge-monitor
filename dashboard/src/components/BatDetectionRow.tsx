"use client";

/**
 * Single detection row shared between:
 *   - The live BatDetectionFeed
 *   - The Offline WAV Analysis panel's per-upload result list
 *
 * Kept as a dedicated component so both surfaces stay visually
 * identical without copy-paste drift.
 */

export const GROUP_LABELS: Record<string, { common: string; desc: string }> = {
  EPFU_LANO: {
    common: "Big Brown / Silver-haired",
    desc: "Eptesicus fuscus + Lasionycteris noctivagans",
  },
  LABO: { common: "Eastern Red Bat", desc: "Lasiurus borealis" },
  LACI: { common: "Hoary Bat", desc: "Lasiurus cinereus" },
  MYSP: {
    common: "Myotis spp.",
    desc: "Little Brown, Northern Long-eared, Indiana, Eastern Small-footed",
  },
  PESU: { common: "Tri-colored Bat", desc: "Perimyotis subflavus" },
};

interface BatDetectionRowProps {
  // Loose-typed for now — matches what onSnapshot hands back and the
  // shape writers in sync-service / worker produce.
  det: any;
  // The Offline WAV panel groups rows under an upload card, so the
  // UPLOAD source badge on every row would be noise. Passing true
  // suppresses it.
  hideSourceBadge?: boolean;
}

export function BatDetectionRow({ det, hideSourceBadge = false }: BatDetectionRowProps) {
  const group = det.predictedClass || det.species;
  const info = GROUP_LABELS[group];
  const confidence = det.predictionConfidence || det.detectionProb || 0;

  return (
    <div className="flex items-start gap-3 p-3 bg-purple-50 rounded-lg border border-purple-100">
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
            {!hideSourceBadge && det.source === "upload" && (
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
            <span className="text-purple-400">{det.modelVersion}</span>
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
}
