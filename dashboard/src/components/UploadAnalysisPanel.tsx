"use client";

import { useEffect, useMemo, useState } from "react";

const ENV_API_URL = process.env.NEXT_PUBLIC_ANALYSIS_API_URL ?? "";
const STORAGE_KEY = "analysisApiUrl";

interface AnalysisResponse {
  filename: string;
  sample_rate: number;
  duration_seconds: number;
  sync_id: string;
  summary?: {
    ast_segments_analysed?: number;
    ast_total_classifications?: number;
    bat_detections_count?: number;
    bat_species_found?: string[];
    stored_in_db?: boolean;
    will_sync_to_cloud?: boolean;
  };
}

interface HealthResponse {
  status: string;
  models_loaded?: {
    ast?: boolean;
    batdetect2?: boolean;
  };
}

function guessApiUrl(): string {
  if (typeof window === "undefined") return ENV_API_URL;
  if (ENV_API_URL) return ENV_API_URL;

  const host = window.location.hostname;
  const isLocalHost = host === "localhost" || host === "127.0.0.1" || host.endsWith(".local");
  const isPrivateIp = /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[0-1])\.)/.test(host);

  if (isLocalHost || isPrivateIp) {
    return `http://${host}:8080`;
  }

  return "";
}

export function UploadAnalysisPanel() {
  const [apiUrl, setApiUrl] = useState(ENV_API_URL);
  const [deviceLabel, setDeviceLabel] = useState("upload");
  const [topK, setTopK] = useState(5);
  const [runAstModel, setRunAstModel] = useState(true);
  const [runBatdetectModel, setRunBatdetectModel] = useState(true);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [testingConnection, setTestingConnection] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnalysisResponse | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<string | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const saved = window.localStorage.getItem(STORAGE_KEY);
    setApiUrl(saved || guessApiUrl());
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (apiUrl) {
      window.localStorage.setItem(STORAGE_KEY, apiUrl);
    }
  }, [apiUrl]);

  const endpoint = useMemo(() => {
    if (!apiUrl.trim()) return "";
    return `${apiUrl.replace(/\/$/, "")}/analyze`;
  }, [apiUrl]);

  async function handleConnectionTest() {
    setError(null);
    setConnectionStatus(null);

    if (!apiUrl.trim()) {
      setError("Enter a reachable Analysis API URL first.");
      return;
    }

    setTestingConnection(true);
    try {
      const healthUrl = `${apiUrl.replace(/\/$/, "")}/health`;
      const response = await fetch(healthUrl, {
        method: "GET",
      });

      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `Connection test failed with ${response.status}`);
      }

      const data = (await response.json()) as HealthResponse;
      const astLoaded = data.models_loaded?.ast ? "warm" : "cold";
      const batLoaded = data.models_loaded?.batdetect2 ? "warm" : "cold";
      setConnectionStatus(
        `Connected to Analysis API · status=${data.status} · AST ${astLoaded} · BatDetect2 ${batLoaded}`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connection test failed.");
    } finally {
      setTestingConnection(false);
    }
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setResult(null);

    if (!selectedFile) {
      setError("Choose a .wav file first.");
      return;
    }

    if (!selectedFile.name.toLowerCase().endsWith(".wav")) {
      setError("Only .wav files are supported.");
      return;
    }

    if (!endpoint) {
      setError("Enter a reachable Analysis API URL first.");
      return;
    }

    setSubmitting(true);
    try {
      const formData = new FormData();
      formData.append("file", selectedFile);

      const params = new URLSearchParams({
        run_ast_model: String(runAstModel),
        run_batdetect_model: String(runBatdetectModel),
        top_k: String(topK),
        device_label: deviceLabel || "upload",
      });

      const response = await fetch(`${endpoint}?${params.toString()}`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `Upload failed with ${response.status}`);
      }

      const data = (await response.json()) as AnalysisResponse;
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">📤 Offline WAV Analysis</h2>
          <p className="text-sm text-gray-500 mt-1">
            Upload an existing WAV file and send it through AST and BatDetect2.
          </p>
        </div>
        <span className="text-xs font-medium px-2 py-1 rounded-full bg-amber-50 text-amber-700 border border-amber-200">
          Source: upload
        </span>
      </div>

      <form className="space-y-4" onSubmit={handleSubmit}>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Analysis API URL
            </label>
            <input
              type="url"
              value={apiUrl}
              onChange={(e) => setApiUrl(e.target.value)}
              placeholder="http://raspberrypi.local:8080"
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="text-xs text-gray-500 mt-1.5 leading-relaxed">
              The analysis models run <strong>on your Pi</strong>, not on Vercel —
              your browser connects to it directly over your local network.
            </p>
            <ul className="text-xs text-gray-400 mt-1 space-y-0.5 list-disc list-inside">
              <li>Same Wi-Fi: <code className="font-mono text-gray-600">http://raspberrypi.local:8080</code></li>
              <li>By IP address: <code className="font-mono text-gray-600">http://192.168.x.x:8080</code></li>
              <li>Browsing on the Pi: <code className="font-mono text-gray-600">http://localhost:8080</code></li>
            </ul>
            <p className="text-xs text-gray-400 mt-1">
              Saved in this browser. To pre-fill permanently, set{" "}
              <code className="font-mono text-gray-600">NEXT_PUBLIC_ANALYSIS_API_URL</code> in Vercel.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Upload file
            </label>
            <input
              type="file"
              accept=".wav,audio/wav"
              onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-900 file:mr-3 file:rounded-md file:border-0 file:bg-blue-50 file:px-3 file:py-1.5 file:text-blue-700"
            />
            <p className="text-xs text-gray-400 mt-1">
              Large files may take a while because the models run on the Pi.
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Device label
            </label>
            <input
              type="text"
              value={deviceLabel}
              onChange={(e) => setDeviceLabel(e.target.value)}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Top K labels
            </label>
            <input
              type="number"
              min={1}
              max={10}
              value={topK}
              onChange={(e) => setTopK(Number(e.target.value) || 5)}
              className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <label className="flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={runAstModel}
              onChange={(e) => setRunAstModel(e.target.checked)}
              className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            Run AST
          </label>
          <label className="flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={runBatdetectModel}
              onChange={(e) => setRunBatdetectModel(e.target.checked)}
              className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
            />
            Run BatDetect2
          </label>
        </div>

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={handleConnectionTest}
            disabled={testingConnection}
            className="inline-flex items-center gap-2 rounded-lg border border-blue-200 bg-blue-50 px-4 py-2 text-sm font-medium text-blue-700 hover:bg-blue-100 disabled:cursor-not-allowed disabled:bg-blue-50 disabled:text-blue-300"
          >
            {testingConnection ? "Testing…" : "Test Connection"}
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-blue-300"
          >
            {submitting ? "Analyzing…" : "Analyze WAV"}
          </button>
          {selectedFile && (
            <span className="text-sm text-gray-500 truncate">{selectedFile.name}</span>
          )}
        </div>

        {connectionStatus && (
          <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-700 whitespace-pre-wrap">
            {connectionStatus}
          </div>
        )}

        {error && (
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 whitespace-pre-wrap">
            {error}
          </div>
        )}

        {result && (
          <div className="rounded-lg border border-green-200 bg-green-50 p-4">
            <div className="flex items-center justify-between gap-4 mb-3">
              <div>
                <p className="text-sm font-semibold text-green-800">Analysis completed</p>
                <p className="text-xs text-green-700">
                  {result.filename} · {result.duration_seconds}s · {result.sample_rate.toLocaleString()} Hz
                </p>
              </div>
              <span className="text-xs font-medium text-green-700 bg-white/70 px-2 py-1 rounded border border-green-200">
                Sync ID: {result.sync_id.slice(0, 8)}…
              </span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
              <div className="rounded-lg bg-white/70 p-3 border border-green-100">
                <p className="text-xs text-gray-500">AST Segments</p>
                <p className="font-semibold text-gray-900">{result.summary?.ast_segments_analysed ?? 0}</p>
              </div>
              <div className="rounded-lg bg-white/70 p-3 border border-green-100">
                <p className="text-xs text-gray-500">AST Rows</p>
                <p className="font-semibold text-gray-900">{result.summary?.ast_total_classifications ?? 0}</p>
              </div>
              <div className="rounded-lg bg-white/70 p-3 border border-green-100">
                <p className="text-xs text-gray-500">Bat Detections</p>
                <p className="font-semibold text-gray-900">{result.summary?.bat_detections_count ?? 0}</p>
              </div>
              <div className="rounded-lg bg-white/70 p-3 border border-green-100">
                <p className="text-xs text-gray-500">Cloud Sync</p>
                <p className="font-semibold text-gray-900">
                  {result.summary?.will_sync_to_cloud ? "Queued" : "Unknown"}
                </p>
              </div>
            </div>
            {!!result.summary?.bat_species_found?.length && (
              <p className="text-xs text-green-800 mt-3">
                Species found: {result.summary.bat_species_found.join(", ")}
              </p>
            )}
          </div>
        )}
      </form>
    </div>
  );
}
