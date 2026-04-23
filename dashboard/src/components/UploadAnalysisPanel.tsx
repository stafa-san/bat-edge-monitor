"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  collection,
  deleteDoc,
  doc,
  getDocs,
  limit,
  onSnapshot,
  orderBy,
  query,
  serverTimestamp,
  setDoc,
  updateDoc,
  where,
  writeBatch,
  Timestamp,
} from "firebase/firestore";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { db } from "@/lib/firebase";
import { uploadWavWithProgress, type UploadHandle } from "@/lib/firebaseStorage";
import { BatDetectionRow, type ReviewAction } from "./BatDetectionRow";

const MAX_BYTES = 100 * 1024 * 1024;
const RECENT_LIMIT = 25;

type JobStatus = "uploading" | "pending" | "processing" | "done" | "error";

interface UploadJob {
  id: string;
  status: JobStatus;
  filename?: string;
  sizeBytes?: number;
  createdAt?: Timestamp;
  processingStartedAt?: Timestamp;
  completedAt?: Timestamp;
  detectionCount?: number;
  speciesFound?: string[];
  durationSeconds?: number;
  errorMessage?: string;
  // Populated by the Cloud Function when a gate rejected the whole
  // segment (detectionCount=0). ``rejectionMessage`` is the UI-ready
  // sentence ("Audio appears to be silence…"); ``rejectionReason`` is
  // the machine code (e.g. "validator:rms_too_low(0.0012)") useful
  // for tuning and logs.
  rejectionReason?: string;
  rejectionMessage?: string;
  pipelineVersion?: string;
  // Populated by the Cloud Function after every analysis — a labelled
  // PNG spectrogram of the uploaded audio with red detection boxes
  // overlaid, hosted in Firebase Storage under spectrograms/.
  spectrogramUrl?: string;
  // 10× slowdown of the uploaded WAV so ultrasonic bat calls become
  // audible (40 kHz → 4 kHz). Hosted in Firebase Storage under audio/.
  timeExpandedAudioUrl?: string;
  // Client-only — present on the synthetic "uploading" row before the
  // Firestore doc exists.
  progress?: number;
}

function formatSize(bytes?: number): string {
  if (!bytes) return "?";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatRelative(ts?: Timestamp): string {
  if (!ts?.toDate) return "";
  const diffMs = Date.now() - ts.toDate().getTime();
  const s = Math.round(diffMs / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return ts.toDate().toLocaleString();
}

interface JobMetrics {
  count: number;
  meanLowKhz: number;
  meanHighKhz: number;
  meanDurationMs: number;
  meanConfidencePct: number;
  meanInterCallMs: number | null;
  uniqueSpeciesCount: number;
}

function computeMetrics(dets: any[]): JobMetrics | null {
  if (dets.length === 0) return null;
  const n = dets.length;
  const lows = dets.map((d) => (d.lowFreq ?? 0) / 1000);
  const highs = dets.map((d) => (d.highFreq ?? 0) / 1000);
  const durs = dets.map((d) => d.durationMs ?? 0);
  const confs = dets.map(
    (d) => d.predictionConfidence ?? d.detectionProb ?? 0,
  );
  const species = new Set(
    dets.map((d) => d.predictedClass ?? d.species ?? "Unknown"),
  );

  // Inter-call interval — sort by startTime, diff consecutive.
  const starts = dets
    .map((d) => d.startTime ?? 0)
    .sort((a, b) => a - b);
  let meanInterCallMs: number | null = null;
  if (starts.length >= 2) {
    let sum = 0;
    for (let i = 1; i < starts.length; i += 1) {
      sum += (starts[i] - starts[i - 1]) * 1000;
    }
    meanInterCallMs = sum / (starts.length - 1);
  }

  const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;
  return {
    count: n,
    meanLowKhz: avg(lows),
    meanHighKhz: avg(highs),
    meanDurationMs: avg(durs),
    meanConfidencePct: avg(confs) * 100,
    meanInterCallMs,
    uniqueSpeciesCount: species.size,
  };
}

function buildConfidenceHistogram(
  dets: any[],
): { bin: string; count: number }[] {
  const bins = Array.from({ length: 10 }, (_, i) => ({
    bin: `${i * 10}–${(i + 1) * 10}%`,
    count: 0,
  }));
  for (const d of dets) {
    const c = d.predictionConfidence ?? d.detectionProb ?? 0;
    const idx = Math.min(Math.floor(c * 10), 9);
    bins[idx].count += 1;
  }
  return bins;
}

function exportJobAsCsv(job: UploadJob, dets: any[]): void {
  const header = [
    "job_id",
    "filename",
    "detection_time",
    "start_s",
    "end_s",
    "duration_ms",
    "low_freq_khz",
    "high_freq_khz",
    "predicted_class",
    "prediction_confidence",
    "raw_species",
    "detection_prob",
    "verified_class",
    "reviewed_by",
    "reviewer_notes",
    "model_version",
    "pipeline_version",
  ];
  const rows = dets.map((d) => [
    job.id,
    job.filename ?? "",
    d.detectionTime?.toDate?.().toISOString() ?? "",
    d.startTime ?? "",
    d.endTime ?? "",
    d.durationMs ?? "",
    ((d.lowFreq ?? 0) / 1000).toFixed(1),
    ((d.highFreq ?? 0) / 1000).toFixed(1),
    d.predictedClass ?? "",
    d.predictionConfidence ?? "",
    d.species ?? "",
    d.detectionProb ?? "",
    d.verifiedClass ?? "",
    d.reviewedBy ?? "",
    d.reviewerNotes ?? "",
    d.modelVersion ?? "",
    d.pipelineVersion ?? "",
  ]);
  const csv = [header, ...rows]
    .map((r) =>
      r
        .map((v) => {
          const s = String(v);
          return s.includes(",") || s.includes('"') || s.includes("\n")
            ? `"${s.replace(/"/g, '""')}"`
            : s;
        })
        .join(","),
    )
    .join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${job.filename?.replace(/\.wav$/i, "") ?? job.id}_detections.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function statusBadge(job: UploadJob): { label: string; className: string } {
  switch (job.status) {
    case "uploading":
      return {
        label: `uploading ${Math.round((job.progress ?? 0) * 100)}%`,
        className: "bg-blue-100 text-blue-700 border-blue-200",
      };
    case "pending":
      return {
        label: "queued",
        className: "bg-gray-100 text-gray-600 border-gray-200",
      };
    case "processing":
      return {
        label: "analyzing…",
        className: "bg-yellow-100 text-yellow-700 border-yellow-200",
      };
    case "done": {
      const count = job.detectionCount ?? 0;
      if (count > 0) {
        return {
          label: `${count} bat call${count === 1 ? "" : "s"} found`,
          className: "bg-green-100 text-green-700 border-green-200",
        };
      }
      // Zero-detection but successful analysis — distinguish visually
      // so it doesn't look like a pending job or an error.
      return {
        label: "no bat calls",
        className: "bg-slate-100 text-slate-700 border-slate-200",
      };
    }
    case "error":
      return {
        label: "error",
        className: "bg-red-100 text-red-700 border-red-200",
      };
  }
}

interface UploadAnalysisPanelProps {
  // Upload-sourced detection rows, sourced by the parent's single
  // ``batDetections`` subscription. The panel filters by ``syncId``
  // to group per-job. Passed in rather than subscribed here so we
  // stay on a single Firestore index and the parent/children never
  // disagree about which rows exist.
  batDetections: any[];
}

export function UploadAnalysisPanel({ batDetections }: UploadAnalysisPanelProps) {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [recentJobs, setRecentJobs] = useState<UploadJob[]>([]);
  const [uploadingEntry, setUploadingEntry] = useState<UploadJob | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  const [reviewerName, setReviewerName] = useState<string>("");
  const uploadHandleRef = useRef<UploadHandle | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    setReviewerName(window.localStorage.getItem("reviewerName") ?? "");
  }, []);

  function saveReviewerName(name: string) {
    setReviewerName(name);
    if (typeof window !== "undefined") {
      window.localStorage.setItem("reviewerName", name);
    }
  }

  async function handleReview(detId: string, action: ReviewAction) {
    if (!reviewerName) {
      const name = typeof window !== "undefined"
        ? window.prompt(
            "Who's reviewing? (stored locally so we can record who verified each call)",
          )
        : null;
      if (!name) return;
      saveReviewerName(name);
    }
    const name = reviewerName || window.localStorage.getItem("reviewerName") || "anonymous";
    const ref = doc(db, "batDetections", detId);
    try {
      if (action.kind === "verify") {
        // Find the current row's predictedClass from the passed-in
        // detections — needed because Firestore update can't read then
        // write atomically without transactions, and we want to tag
        // verifiedClass with the class the reviewer endorsed.
        const det = batDetections.find((d) => d.id === detId);
        const verifiedClass = det?.predictedClass ?? det?.species ?? null;
        await updateDoc(ref, {
          reviewedBy: name,
          reviewedAt: serverTimestamp(),
          verifiedClass,
        });
      } else if (action.kind === "correct") {
        await updateDoc(ref, {
          reviewedBy: name,
          reviewedAt: serverTimestamp(),
          verifiedClass: action.correctedClass,
        });
      } else if (action.kind === "notes") {
        await updateDoc(ref, {
          reviewedBy: name,
          reviewedAt: serverTimestamp(),
          reviewerNotes: action.notes,
        });
      }
    } catch (err) {
      console.error("[UploadAnalysis] Review update failed:", err);
    }
  }

  // Group the injected detections by the upload job they came from.
  // Recomputed on every snapshot update — cheap.
  const detectionsBySyncId = useMemo(() => {
    const grouped = new Map<string, any[]>();
    for (const det of batDetections) {
      const sid = (det as any).syncId as string | undefined;
      if (!sid) continue;
      if (!grouped.has(sid)) grouped.set(sid, []);
      grouped.get(sid)!.push(det);
    }
    return grouped;
  }, [batDetections]);

  // ── Recent uploads subscription ─────────────────────────────────
  useEffect(() => {
    const q = query(
      collection(db, "uploadJobs"),
      orderBy("createdAt", "desc"),
      limit(RECENT_LIMIT),
    );
    const unsub = onSnapshot(q, (snap) => {
      const jobs = snap.docs.map((d) => ({
        id: d.id,
        ...(d.data() as Omit<UploadJob, "id">),
      })) as UploadJob[];
      setRecentJobs(jobs);
      // Once the active upload has landed in Firestore, drop the synthetic row.
      setUploadingEntry((cur) =>
        cur && jobs.some((j) => j.id === cur.id) ? null : cur,
      );
    });
    return unsub;
  }, []);

  const jobsToRender = useMemo(() => {
    if (!uploadingEntry) return recentJobs;
    return [uploadingEntry, ...recentJobs].slice(0, RECENT_LIMIT);
  }, [uploadingEntry, recentJobs]);

  function toggleExpanded(id: string) {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handleClearAll() {
    setClearing(true);
    try {
      // Grab every uploadJobs doc and every upload-sourced detection
      // row, batch-delete them. Rules enforce that only upload rows can
      // be deleted (live rows stay safe).
      const jobsSnap = await getDocs(collection(db, "uploadJobs"));
      const detsSnap = await getDocs(
        query(collection(db, "batDetections"), where("source", "==", "upload")),
      );
      const allDocs = [...jobsSnap.docs, ...detsSnap.docs];
      // Firestore batches cap at 500 writes. Chunk if we're over.
      const CHUNK = 400;
      for (let i = 0; i < allDocs.length; i += CHUNK) {
        const batch = writeBatch(db);
        for (const d of allDocs.slice(i, i + CHUNK)) {
          batch.delete(d.ref);
        }
        await batch.commit();
      }
      // Synthetic "uploading" entry should vanish too if the user
      // started an upload and wants a hard reset.
      setUploadingEntry(null);
      setExpandedIds(new Set());
    } catch (err) {
      console.error("[UploadAnalysis] Clear failed:", err);
      setSubmitError(
        err instanceof Error ? `Clear failed: ${err.message}` : "Clear failed.",
      );
    } finally {
      setClearing(false);
      setShowClearConfirm(false);
    }
  }

  async function handleAnalyze(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitError(null);

    if (!selectedFile) {
      setSubmitError("Pick a .wav file first.");
      return;
    }
    if (!selectedFile.name.toLowerCase().endsWith(".wav")) {
      setSubmitError("Only .wav files are supported.");
      return;
    }
    if (selectedFile.size > MAX_BYTES) {
      setSubmitError(
        `File is ${formatSize(selectedFile.size)} — max is 100 MB.`,
      );
      return;
    }

    const jobId = crypto.randomUUID();
    const filename = selectedFile.name;
    const sizeBytes = selectedFile.size;

    setUploadingEntry({
      id: jobId,
      status: "uploading",
      filename,
      sizeBytes,
      progress: 0,
    });
    // Expand the active card by default so the status is visible.
    setExpandedIds((prev) => new Set(prev).add(jobId));

    try {
      const handle = uploadWavWithProgress(selectedFile, jobId, (p) => {
        setUploadingEntry((prev) =>
          prev && prev.id === jobId ? { ...prev, progress: p } : prev,
        );
      });
      uploadHandleRef.current = handle;
      await handle.promise;

      // Upload succeeded — hand off to the Pi worker by creating the
      // Firestore job doc. The worker polls every ~5s.
      await setDoc(doc(db, "uploadJobs", jobId), {
        status: "pending",
        filename,
        sizeBytes,
        createdAt: serverTimestamp(),
      });

      // onSnapshot will now drive the card's state until done/error.
      // Clear the file picker so user can queue another upload.
      setSelectedFile(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Upload failed.";
      setUploadingEntry((prev) =>
        prev && prev.id === jobId
          ? { ...prev, status: "error", errorMessage: message }
          : prev,
      );
      setSubmitError(message);
    } finally {
      uploadHandleRef.current = null;
    }
  }

  const activeUploadInFlight = uploadingEntry?.status === "uploading";

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">
            📤 Offline WAV Analysis
          </h2>
          <p className="text-sm text-gray-500 mt-1">
            Upload a .wav, get BatDetect2 + species classification results
            back — works from any network.
          </p>
        </div>
      </div>

      <form className="flex items-center gap-3 mb-6" onSubmit={handleAnalyze}>
        <input
          type="file"
          accept=".wav,audio/wav"
          onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
          disabled={activeUploadInFlight}
          className="flex-1 rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-900 file:mr-3 file:rounded-md file:border-0 file:bg-blue-50 file:px-3 file:py-1.5 file:text-blue-700 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={!selectedFile || activeUploadInFlight}
          className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-blue-300"
        >
          {activeUploadInFlight ? "Uploading…" : "Analyze WAV"}
        </button>
      </form>

      {submitError && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 mb-4">
          {submitError}
        </div>
      )}

      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-700">
          Recent uploads
        </h3>
        <div className="flex items-center gap-3">
          {jobsToRender.length > 0 && (
            <span className="text-xs text-gray-500">
              last {jobsToRender.length}
            </span>
          )}
          {jobsToRender.length > 0 && (
            <button
              type="button"
              onClick={() => setShowClearConfirm(true)}
              disabled={clearing}
              className="text-xs text-red-600 hover:text-red-700 hover:underline disabled:text-red-300 disabled:cursor-not-allowed"
            >
              {clearing ? "clearing…" : "clear all"}
            </button>
          )}
        </div>
      </div>

      {showClearConfirm && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm">
          <p className="font-medium text-red-800">
            Clear all upload history?
          </p>
          <p className="text-red-700 mt-1 text-xs">
            Deletes every upload job and every upload-sourced detection
            from Firestore. Live Pi captures are unaffected. This cannot
            be undone — the WAVs in Firebase Storage age out on their
            own 7-day lifecycle, but DB metadata is gone immediately.
          </p>
          <div className="flex items-center gap-2 mt-3">
            <button
              type="button"
              onClick={handleClearAll}
              disabled={clearing}
              className="rounded-md bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:bg-red-300"
            >
              {clearing ? "clearing…" : "yes, clear everything"}
            </button>
            <button
              type="button"
              onClick={() => setShowClearConfirm(false)}
              disabled={clearing}
              className="rounded-md border border-gray-300 bg-white px-3 py-1 text-xs text-gray-700 hover:bg-gray-50"
            >
              cancel
            </button>
          </div>
        </div>
      )}

      {jobsToRender.length === 0 ? (
        <div className="rounded-lg border border-dashed border-gray-200 p-6 text-center text-sm text-gray-400">
          No uploads yet — pick a .wav above to get started.
        </div>
      ) : (
        <ul className="space-y-2">
          {jobsToRender.map((job) => {
            const badge = statusBadge(job);
            const dets = detectionsBySyncId.get(job.id) ?? [];
            const expanded = expandedIds.has(job.id);
            return (
              <li
                key={job.id}
                className="rounded-lg border border-gray-200 bg-white"
              >
                <button
                  type="button"
                  onClick={() => toggleExpanded(job.id)}
                  className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left hover:bg-gray-50 rounded-lg"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm text-gray-400 font-mono">
                        {expanded ? "▼" : "▶"}
                      </span>
                      <p className="text-sm font-medium text-gray-900 truncate">
                        {job.filename ?? job.id}
                      </p>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-gray-500 mt-1 ml-5">
                      <span>{formatSize(job.sizeBytes)}</span>
                      <span>{formatRelative(job.createdAt)}</span>
                      {job.durationSeconds != null && (
                        <span>{job.durationSeconds.toFixed(1)}s audio</span>
                      )}
                      {job.speciesFound && job.speciesFound.length > 0 && (
                        <span className="truncate">
                          {job.speciesFound.join(", ")}
                        </span>
                      )}
                    </div>
                  </div>
                  <span
                    className={`text-[11px] font-medium px-2 py-1 rounded-full border shrink-0 ${badge.className}`}
                  >
                    {badge.label}
                  </span>
                </button>

                {job.status === "uploading" && job.progress != null && (
                  <div className="px-4 pb-3">
                    <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-blue-500 transition-all"
                        style={{ width: `${Math.round(job.progress * 100)}%` }}
                      />
                    </div>
                  </div>
                )}

                {expanded && (
                  <ExpandedJobView
                    job={job}
                    dets={dets}
                    reviewerName={reviewerName}
                    onChangeReviewerName={saveReviewerName}
                    onReview={handleReview}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────
// Rich per-upload analysis view. Rendered only when a card is
// expanded. Presentation-only; all data + callbacks come from props.
// ─────────────────────────────────────────────────────────────────

interface ExpandedJobViewProps {
  job: UploadJob;
  dets: any[];
  reviewerName: string;
  onChangeReviewerName: (name: string) => void;
  onReview: (detId: string, action: ReviewAction) => void | Promise<void>;
}

function ExpandedJobView({
  job,
  dets,
  reviewerName,
  onChangeReviewerName,
  onReview,
}: ExpandedJobViewProps) {
  const metrics = useMemo(() => computeMetrics(dets), [dets]);
  const histogram = useMemo(() => buildConfidenceHistogram(dets), [dets]);
  const duration = job.durationSeconds ?? 15;

  return (
    <div className="px-4 pb-4 pt-1 border-t border-gray-100 space-y-4">
      {/* ── Spectrogram ── */}
      {job.spectrogramUrl && (
        <div>
          <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1.5">
            Spectrogram
          </p>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={job.spectrogramUrl}
            alt={`Spectrogram of ${job.filename ?? "upload"}`}
            className="w-full rounded-lg border border-gray-200 bg-gray-50"
          />
          <p className="text-[10px] text-gray-400 mt-1">
            Red boxes mark detected bat calls labelled with the
            classifier&apos;s predicted species and confidence.
          </p>
        </div>
      )}

      {/* ── Error / zero-detection messaging ── */}
      {job.status === "error" && (
        <p className="text-xs text-red-600">
          {job.errorMessage ?? "Analysis failed."}
        </p>
      )}
      {job.status === "done" && dets.length === 0 && (
        <div className="space-y-1">
          <p className="text-xs text-slate-700">
            {job.rejectionMessage ??
              "No bat calls detected in this recording."}
          </p>
          {job.rejectionReason && (
            <p className="text-[10px] font-mono text-slate-400">
              reason: {job.rejectionReason}
            </p>
          )}
        </div>
      )}
      {(job.status === "pending" || job.status === "processing") && (
        <p className="text-xs text-gray-400 italic">
          {job.status === "pending"
            ? "Waiting for the analyzer to pick this up…"
            : "BatDetect2 is running — detections will appear here."}
        </p>
      )}

      {/* ── Metrics cards ── */}
      {metrics && <MetricsGrid metrics={metrics} />}

      {/* ── Time-expanded audio ── */}
      {job.timeExpandedAudioUrl && (
        <div>
          <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1.5">
            Time-expanded audio (10× slowdown · ultrasonic → audible)
          </p>
          <audio
            controls
            preload="none"
            src={job.timeExpandedAudioUrl}
            className="w-full h-10"
          >
            Your browser does not support audio playback.
          </audio>
          <p className="text-[10px] text-gray-400 mt-1">
            The original file is at 256 kHz; playback at 25.6 kHz
            pitch-shifts every call into the human hearing range.
          </p>
        </div>
      )}

      {/* ── Histogram + call-density timeline side-by-side ── */}
      {metrics && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <ConfidencePanel data={histogram} />
          <CallDensityPanel dets={dets} duration={duration} />
        </div>
      )}

      {/* ── Detections list ── */}
      {dets.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <p className="text-sm font-semibold text-gray-700">
              Detections ({dets.length})
            </p>
            <div className="flex items-center gap-3">
              <ReviewerNameControl
                value={reviewerName}
                onChange={onChangeReviewerName}
              />
              <button
                type="button"
                onClick={() => exportJobAsCsv(job, dets)}
                className="text-xs rounded-md border border-gray-300 bg-white px-2.5 py-1 text-gray-700 hover:bg-gray-50"
                title="Download all detections for this upload as CSV"
              >
                ⬇ CSV
              </button>
            </div>
          </div>
          <div className="space-y-2">
            {dets.map((det) => (
              <BatDetectionRow
                key={det.id}
                det={det}
                hideSourceBadge
                onReview={(action) => onReview(det.id, action)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function MetricsGrid({ metrics }: { metrics: JobMetrics }) {
  const cards: { label: string; value: string; hint?: string }[] = [
    { label: "Calls", value: String(metrics.count) },
    {
      label: "Species",
      value: String(metrics.uniqueSpeciesCount),
      hint: "unique groups",
    },
    {
      label: "Mean low freq",
      value: `${metrics.meanLowKhz.toFixed(1)} kHz`,
    },
    {
      label: "Mean high freq",
      value: `${metrics.meanHighKhz.toFixed(1)} kHz`,
    },
    {
      label: "Mean duration",
      value: `${metrics.meanDurationMs.toFixed(1)} ms`,
    },
    {
      label: "Mean confidence",
      value: `${metrics.meanConfidencePct.toFixed(0)}%`,
    },
    {
      label: "Mean ICI",
      value:
        metrics.meanInterCallMs == null
          ? "—"
          : `${metrics.meanInterCallMs.toFixed(0)} ms`,
      hint: "inter-call interval",
    },
  ];
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1.5">
        Call metrics
      </p>
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2">
        {cards.map((c) => (
          <div
            key={c.label}
            className="rounded-lg border border-gray-200 bg-gradient-to-br from-white to-gray-50 p-2.5"
          >
            <p className="text-[10px] uppercase tracking-wide text-gray-400">
              {c.label}
            </p>
            <p className="text-lg font-semibold text-gray-900 leading-tight mt-0.5">
              {c.value}
            </p>
            {c.hint && (
              <p className="text-[9px] text-gray-400 mt-0.5">{c.hint}</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function ConfidencePanel({
  data,
}: {
  data: { bin: string; count: number }[];
}) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-3">
      <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-2">
        Classifier confidence distribution
      </p>
      <ResponsiveContainer width="100%" height={140}>
        <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis
            dataKey="bin"
            tick={{ fontSize: 9, fill: "#6b7280" }}
            interval={1}
          />
          <YAxis tick={{ fontSize: 9, fill: "#6b7280" }} allowDecimals={false} />
          <Tooltip
            contentStyle={{ fontSize: 11, borderRadius: 8 }}
            labelStyle={{ color: "#111827" }}
          />
          <Bar dataKey="count" fill="#8b5cf6" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
      <p className="text-[9px] text-gray-400 mt-1">
        How often the classifier was at each confidence level across all
        calls in this file.
      </p>
    </div>
  );
}

function CallDensityPanel({
  dets,
  duration,
}: {
  dets: any[];
  duration: number;
}) {
  const dom = duration > 0 ? duration : 15;
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-3">
      <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-2">
        Call density over time
      </p>
      <div className="relative h-14 rounded bg-gradient-to-r from-purple-50 via-indigo-50 to-purple-50 border border-purple-100 overflow-hidden">
        {dets.map((d) => {
          const start = d.startTime ?? 0;
          const end = d.endTime ?? start + 0.01;
          const left = Math.min(Math.max(start / dom, 0), 1) * 100;
          const width = Math.max(0.4, ((end - start) / dom) * 100);
          const conf = d.predictionConfidence ?? d.detectionProb ?? 0.5;
          return (
            <div
              key={d.id}
              className="absolute top-0 bottom-0 bg-purple-500"
              style={{
                left: `${left}%`,
                width: `${width}%`,
                opacity: 0.35 + 0.55 * conf,
              }}
              title={`${(d.predictedClass ?? d.species)} @ ${start.toFixed(2)}s — ${(conf * 100).toFixed(0)}%`}
            />
          );
        })}
      </div>
      <div className="flex justify-between text-[9px] text-gray-400 mt-1">
        <span>0s</span>
        <span>{(dom / 2).toFixed(1)}s</span>
        <span>{dom.toFixed(1)}s</span>
      </div>
      <p className="text-[9px] text-gray-400 mt-1">
        Each bar is a detected call; taller opacity = higher classifier
        confidence. Hover for details.
      </p>
    </div>
  );
}

function ReviewerNameControl({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  if (editing) {
    return (
      <input
        type="text"
        autoFocus
        defaultValue={value}
        placeholder="your name"
        onBlur={(e) => {
          const v = e.target.value.trim();
          if (v) onChange(v);
          setEditing(false);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          if (e.key === "Escape") setEditing(false);
        }}
        className="text-xs rounded-md border border-gray-300 px-2 py-1 w-32"
      />
    );
  }
  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      className="text-xs text-gray-500 hover:text-gray-700 hover:underline"
      title="Your name is attached to every ✓ / ✗ / 📝 action so the flywheel records who verified what."
    >
      👤 {value || "sign in"}
    </button>
  );
}
