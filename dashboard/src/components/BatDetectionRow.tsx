"use client";

import { useState } from "react";

/**
 * Single detection row shared between:
 *   - The live BatDetectionFeed
 *   - The Offline WAV Analysis panel's per-upload result list
 *
 * Review actions (verify / reject / notes) are optional — pass the
 * ``onReview`` callback only where review makes sense (upload panel).
 * The live feed stays unchanged.
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

export const REVIEWABLE_CLASSES = Object.keys(GROUP_LABELS);

export type ReviewAction =
  | { kind: "verify" }
  | { kind: "correct"; correctedClass: string }
  | { kind: "notes"; notes: string };

interface BatDetectionRowProps {
  det: any;
  hideSourceBadge?: boolean;
  // When set, the row renders review controls (✓ / ✗ / 📝). Called
  // with a ReviewAction describing what the user clicked.
  onReview?: (action: ReviewAction) => void | Promise<void>;
}

export function BatDetectionRow({
  det,
  hideSourceBadge = false,
  onReview,
}: BatDetectionRowProps) {
  const group = det.predictedClass || det.species;
  const info = GROUP_LABELS[group];
  const confidence = det.predictionConfidence || det.detectionProb || 0;
  const verifiedClass: string | null = det.verifiedClass ?? null;
  const reviewedBy: string | null = det.reviewedBy ?? null;

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
            {verifiedClass === det.predictedClass && (
              <span className="text-[10px] font-medium bg-emerald-100 text-emerald-700 px-1.5 py-0.5 rounded shrink-0">
                ✓ verified
              </span>
            )}
            {verifiedClass &&
              verifiedClass !== det.predictedClass && (
                <span className="text-[10px] font-medium bg-rose-100 text-rose-700 px-1.5 py-0.5 rounded shrink-0">
                  corrected → {verifiedClass}
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
        <div className="flex items-center justify-between mt-1 gap-2">
          <p className="text-xs text-gray-400">
            {det.detectionTime?.toDate
              ? det.detectionTime.toDate().toLocaleString()
              : "—"}
            {reviewedBy && (
              <span className="ml-2 text-emerald-600">
                • reviewed by {reviewedBy}
              </span>
            )}
          </p>
          {onReview && (
            <ReviewControls det={det} onReview={onReview} />
          )}
        </div>
      </div>
    </div>
  );
}

function ReviewControls({
  det,
  onReview,
}: {
  det: any;
  onReview: (action: ReviewAction) => void | Promise<void>;
}) {
  // Lightweight local UI state. Dropdown / notes input inline rather
  // than modal to match the compact row style.
  const [mode, setMode] = useState<"idle" | "correct" | "notes">("idle");

  async function run(action: ReviewAction) {
    await onReview(action);
    setMode("idle");
  }

  if (mode === "correct") {
    return (
      <div className="flex items-center gap-1">
        <select
          className="text-xs rounded border border-gray-300 px-1 py-0.5"
          defaultValue=""
          onChange={(e) => {
            if (e.target.value) {
              void run({ kind: "correct", correctedClass: e.target.value });
            }
          }}
        >
          <option value="" disabled>
            correct to…
          </option>
          {REVIEWABLE_CLASSES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => setMode("idle")}
          className="text-[11px] text-gray-500 hover:text-gray-700"
        >
          cancel
        </button>
      </div>
    );
  }

  if (mode === "notes") {
    return (
      <div className="flex items-center gap-1">
        <input
          type="text"
          autoFocus
          defaultValue={det.reviewerNotes ?? ""}
          placeholder="note…"
          onBlur={(e) => {
            if (e.target.value !== (det.reviewerNotes ?? "")) {
              void run({ kind: "notes", notes: e.target.value });
            } else {
              setMode("idle");
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              (e.target as HTMLInputElement).blur();
            } else if (e.key === "Escape") {
              setMode("idle");
            }
          }}
          className="text-xs rounded border border-gray-300 px-1.5 py-0.5 w-48"
        />
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1 text-xs">
      <button
        type="button"
        title="verify — this prediction is correct"
        onClick={() => void run({ kind: "verify" })}
        className="rounded px-1.5 py-0.5 text-emerald-700 hover:bg-emerald-100"
      >
        ✓
      </button>
      <button
        type="button"
        title="correct — pick the right species"
        onClick={() => setMode("correct")}
        className="rounded px-1.5 py-0.5 text-rose-700 hover:bg-rose-100"
      >
        ✗
      </button>
      <button
        type="button"
        title="add a note"
        onClick={() => setMode("notes")}
        className="rounded px-1.5 py-0.5 text-gray-600 hover:bg-gray-100"
      >
        📝
      </button>
    </div>
  );
}
