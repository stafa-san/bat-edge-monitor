"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  collection,
  doc,
  limit,
  onSnapshot,
  orderBy,
  query,
  serverTimestamp,
  setDoc,
  where,
  Timestamp,
} from "firebase/firestore";

import { db } from "@/lib/firebase";
import { uploadWavWithProgress, type UploadHandle } from "@/lib/firebaseStorage";
import { BatDetectionRow } from "./BatDetectionRow";

const MAX_BYTES = 100 * 1024 * 1024;
const RECENT_LIMIT = 25;
const DETECTIONS_LIMIT = 500;

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

export function UploadAnalysisPanel() {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [recentJobs, setRecentJobs] = useState<UploadJob[]>([]);
  const [detectionsBySyncId, setDetectionsBySyncId] = useState<
    Map<string, any[]>
  >(new Map());
  const [uploadingEntry, setUploadingEntry] = useState<UploadJob | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [submitError, setSubmitError] = useState<string | null>(null);
  const uploadHandleRef = useRef<UploadHandle | null>(null);

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

  // ── Upload-sourced detections subscription ──────────────────────
  // Single query keeps the read count modest (≤ 500 docs across the
  // last 25 uploads; typical is way less).
  useEffect(() => {
    const q = query(
      collection(db, "batDetections"),
      where("source", "==", "upload"),
      orderBy("detectionTime", "desc"),
      limit(DETECTIONS_LIMIT),
    );
    const unsub = onSnapshot(q, (snap) => {
      const grouped = new Map<string, any[]>();
      snap.docs.forEach((d) => {
        const det = { id: d.id, ...d.data() } as Record<string, any>;
        const sid = det.syncId as string | undefined;
        if (!sid) return;
        if (!grouped.has(sid)) grouped.set(sid, []);
        grouped.get(sid)!.push(det);
      });
      // Each group stays ordered desc (Firestore returned that way)
      setDetectionsBySyncId(grouped);
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
        {jobsToRender.length > 0 && (
          <span className="text-xs text-gray-500">
            last {jobsToRender.length}
          </span>
        )}
      </div>

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
                  <div className="px-4 pb-3 pt-1 border-t border-gray-100">
                    {job.status === "error" && (
                      <p className="text-xs text-red-600 mt-2">
                        {job.errorMessage ?? "Analysis failed."}
                      </p>
                    )}
                    {job.status === "done" && dets.length === 0 && (
                      <div className="mt-2 space-y-1">
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
                    {dets.length > 0 && (
                      <div className="space-y-2 mt-2">
                        {dets.map((det) => (
                          <BatDetectionRow
                            key={det.id}
                            det={det}
                            hideSourceBadge
                          />
                        ))}
                      </div>
                    )}
                    {(job.status === "pending" || job.status === "processing") && (
                      <p className="text-xs text-gray-400 italic mt-2">
                        {job.status === "pending"
                          ? "Waiting for the analyzer to pick this up…"
                          : "BatDetect2 is running — detections will appear here."}
                      </p>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
