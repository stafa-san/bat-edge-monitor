"use client";

import { useEffect, useState } from "react";
import { db } from "@/lib/firebase";
import {
  collection,
  doc,
  query,
  orderBy,
  limit,
  where,
  onSnapshot,
  Timestamp,
} from "firebase/firestore";
import { SoundscapeChart } from "@/components/SoundscapeChart";
import { BatDetectionFeed } from "@/components/BatDetectionFeed";
import { StatsCards } from "@/components/StatsCards";
import { SPLTimeline } from "@/components/SPLTimeline";
import { DeviceHealth, type DeviceStatus, type HealthSnapshot, type HistoryRange } from "@/components/DeviceHealth";
import { UploadAnalysisPanel } from "@/components/UploadAnalysisPanel";
import { EnvironmentalPanel, type EnvironmentalReading, type EnvTimeRange } from "@/components/EnvironmentalPanel";

interface Classification {
  id: string;
  label: string;
  score: number;
  spl: number;
  device: string;
  syncId: string;
  syncTime: Timestamp;
  source?: string;
}

interface BatDetection {
  id: string;
  species: string;
  commonName: string;
  detectionProb: number;
  lowFreq: number;
  highFreq: number;
  durationMs: number;
  device: string;
  detectionTime: Timestamp;
  audioUrl?: string;
  source?: string;
  predictedClass?: string;
  predictionConfidence?: number;
  modelVersion?: string;
}

export default function Dashboard() {
  const [classifications, setClassifications] = useState<Classification[]>([]);
  const [batDetections, setBatDetections] = useState<BatDetection[]>([]);
  const [deviceStatus, setDeviceStatus] = useState<DeviceStatus | null>(null);
  const [healthHistory, setHealthHistory] = useState<HealthSnapshot[]>([]);
  const [historyRange, setHistoryRange] = useState<HistoryRange>("1h");
  const [environmentalReadings, setEnvironmentalReadings] = useState<EnvironmentalReading[]>([]);
  const [envTimeRange, setEnvTimeRange] = useState<EnvTimeRange>("6h");
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    // Real-time listener for classifications
    const classQuery = query(
      collection(db, "classifications"),
      orderBy("syncTime", "desc"),
      limit(100)
    );

    const unsubClass = onSnapshot(
      classQuery,
      (snapshot) => {
        const data = snapshot.docs.map((doc) => ({
          id: doc.id,
          ...doc.data(),
        })) as Classification[];
        setClassifications(data);
        setIsConnected(true);
      },
      (error) => {
        console.error("[Firestore] Classifications query error:", error);
        setIsConnected(true);
        // Fallback: try without orderBy in case index is missing
        const fallbackQuery = query(
          collection(db, "classifications"),
          limit(100)
        );
        onSnapshot(fallbackQuery, (snapshot) => {
          const data = snapshot.docs.map((doc) => ({
            id: doc.id,
            ...doc.data(),
          })) as Classification[];
          setClassifications(data);
        });
      }
    );

    // Real-time listener for bat detections. Limit raised to 500 so the
    // Offline WAV Analysis panel can group upload-sourced rows by
    // syncId (last 25 uploads × ~10 detections each = comfortably
    // under 500). Both the Live feed and the Upload panel filter this
    // one array client-side by ``source`` — keeps us on a single-field
    // index and avoids the composite-index silent-fail that left the
    // upload card showing "no bat calls" when the backing rows did
    // exist.
    const batQuery = query(
      collection(db, "batDetections"),
      orderBy("detectionTime", "desc"),
      limit(500)
    );

    const unsubBat = onSnapshot(
      batQuery,
      (snapshot) => {
        const data = snapshot.docs.map((doc) => ({
          id: doc.id,
          ...doc.data(),
        })) as BatDetection[];
        setBatDetections(data);
      },
      (error) => {
        console.error("[Firestore] Bat detections query error:", error);
        // Fallback: try without orderBy
        const fallbackQuery = query(
          collection(db, "batDetections"),
          limit(500)
        );
        onSnapshot(fallbackQuery, (snapshot) => {
          const data = snapshot.docs.map((doc) => ({
            id: doc.id,
            ...doc.data(),
          })) as BatDetection[];
          setBatDetections(data);
        });
      }
    );

    // Real-time listener for device health status
    const statusRef = doc(db, "deviceStatus", "edge-device");
    const unsubStatus = onSnapshot(
      statusRef,
      (snapshot) => {
        if (snapshot.exists()) {
          setDeviceStatus(snapshot.data() as DeviceStatus);
        }
      },
      (error) => {
        console.error("[Firestore] Device status error:", error);
      }
    );

    return () => {
      unsubClass();
      unsubBat();
      unsubStatus();
    };
  }, []);

  // Environmental readings listener — depends on envTimeRange so it re-subscribes
  useEffect(() => {
    const rangeMs: Record<EnvTimeRange, number> = {
      "1h": 60 * 60 * 1000,
      "6h": 6 * 60 * 60 * 1000,
      "24h": 24 * 60 * 60 * 1000,
      "7d": 7 * 24 * 60 * 60 * 1000,
    };
    const since = Timestamp.fromDate(new Date(Date.now() - rangeMs[envTimeRange]));

    const envQuery = query(
      collection(db, "environmentalReadings"),
      where("recordedAt", ">=", since),
      orderBy("recordedAt", "desc"),
      limit(2000)
    );

    const unsubEnv = onSnapshot(
      envQuery,
      (snapshot) => {
        const data = snapshot.docs.map((doc) => ({
          id: doc.id,
          ...doc.data(),
        })) as EnvironmentalReading[];
        setEnvironmentalReadings(data);
      },
      (error) => {
        console.error("[Firestore] Environmental readings error:", error);
        const fallbackQuery = query(
          collection(db, "environmentalReadings"),
          where("recordedAt", ">=", since),
          limit(2000)
        );
        onSnapshot(fallbackQuery, (snapshot) => {
          const data = snapshot.docs.map((doc) => ({
            id: doc.id,
            ...doc.data(),
          })) as EnvironmentalReading[];
          setEnvironmentalReadings(data);
        });
      }
    );

    return () => {
      unsubEnv();
    };
  }, [envTimeRange]);

  // Health history listener — depends on historyRange so it re-subscribes
  useEffect(() => {
    const rangeMs: Record<HistoryRange, number> = {
      "1h": 60 * 60 * 1000,
      "6h": 6 * 60 * 60 * 1000,
      "24h": 24 * 60 * 60 * 1000,
      "7d": 7 * 24 * 60 * 60 * 1000,
    };
    const since = Timestamp.fromDate(new Date(Date.now() - rangeMs[historyRange]));

    const historyQuery = query(
      collection(db, "healthHistory"),
      where("recordedAt", ">=", since),
      orderBy("recordedAt", "asc"),
      limit(500)
    );

    const unsubHistory = onSnapshot(
      historyQuery,
      (snapshot) => {
        const data = snapshot.docs.map((d) => ({
          id: d.id,
          ...d.data(),
        })) as HealthSnapshot[];
        setHealthHistory(data);
      },
      (error) => {
        console.error("[Firestore] Health history error:", error);
        // Fallback without ordering
        const fallbackQuery = query(
          collection(db, "healthHistory"),
          where("recordedAt", ">=", since),
          limit(500)
        );
        onSnapshot(fallbackQuery, (snapshot) => {
          const data = snapshot.docs.map((d) => ({
            id: d.id,
            ...d.data(),
          })) as HealthSnapshot[];
          setHealthHistory(data);
        });
      }
    );

    return () => {
      unsubHistory();
    };
  }, [historyRange]);

  return (
    <main className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-2xl">🦇</span>
            <div>
              <h1 className="text-xl font-bold text-gray-900">
                Soundscape Monitor
              </h1>
              <p className="text-sm text-gray-500">
                Real-time acoustic monitoring
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div
              className={`w-2.5 h-2.5 rounded-full ${
                isConnected ? "bg-green-500" : "bg-red-500"
              }`}
            />
            <span className="text-sm text-gray-600">
              {isConnected ? "Live" : "Connecting..."}
            </span>
          </div>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-6 py-6 space-y-6">
        {/* Stats Cards — live captures only; uploads are counted
            separately inside the Offline WAV Analysis panel. Mixing
            them in the header would overstate field activity. */}
        <StatsCards
          classifications={classifications}
          batDetections={batDetections.filter((d) => d.source !== "upload")}
          batDetectionsTotal={deviceStatus?.batDetectionsTotal}
        />

        {/* Live Bat Detection Feed — only live-source rows. Upload-
            sourced rows render inside the Offline WAV Analysis panel
            instead, so the two pipelines never bleed into each other. */}
        <BatDetectionFeed
          detections={batDetections.filter((d) => d.source !== "upload")}
          totalDetections={deviceStatus?.batDetectionsTotal}
        />

        {/* Device Health */}
        <DeviceHealth
          status={deviceStatus}
          history={healthHistory}
          historyRange={historyRange}
          onHistoryRangeChange={setHistoryRange}
        />

        {/* Environmental Panel */}
        <EnvironmentalPanel
          readings={environmentalReadings}
          timeRange={envTimeRange}
          onTimeRangeChange={setEnvTimeRange}
        />

        <UploadAnalysisPanel
          batDetections={batDetections.filter((d) => d.source === "upload")}
        />

        {/* Acoustic Environment — secondary, collapsible */}
        <details className="group">
          <summary className="bg-white rounded-xl shadow-sm border border-gray-200 px-6 py-4 cursor-pointer list-none flex items-center justify-between hover:bg-gray-50 transition-colors">
            <div className="flex items-center gap-2">
              <span className="text-lg">🔊</span>
              <h2 className="text-lg font-semibold text-gray-900">
                Acoustic Environment
              </h2>
              <span className="text-sm text-gray-400 ml-2">
                SPL, sound classes, background noise
              </span>
            </div>
            <svg
              className="w-5 h-5 text-gray-400 transition-transform group-open:rotate-180"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </summary>
          <div className="mt-4 space-y-6">
            {/* SPL Timeline */}
            <SPLTimeline classifications={classifications} />

            {/* Charts Row */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <SoundscapeChart classifications={classifications} />

              {/* Compact recent classifications */}
              <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
                <h2 className="text-lg font-semibold text-gray-900 mb-4">
                  Recent Classifications
                </h2>
                <div className="overflow-x-auto max-h-[300px] overflow-y-auto">
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-white">
                      <tr className="border-b border-gray-200">
                        <th className="text-left py-2 px-3 font-medium text-gray-500">Label</th>
                        <th className="text-left py-2 px-3 font-medium text-gray-500">Score</th>
                        <th className="text-left py-2 px-3 font-medium text-gray-500">SPL</th>
                        <th className="text-left py-2 px-3 font-medium text-gray-500">Time</th>
                      </tr>
                    </thead>
                    <tbody>
                      {classifications.slice(0, 20).map((c) => (
                        <tr key={c.id} className="border-b border-gray-100 hover:bg-gray-50">
                          <td className="py-2 px-3 font-medium text-gray-900 text-xs">
                            <div className="flex items-center gap-1">
                              <span>{c.label}</span>
                              {c.source === "upload" && (
                                <span className="text-[10px] font-medium bg-amber-100 text-amber-700 px-1 py-0.5 rounded">
                                  UPLOAD
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="py-2 px-3 text-xs text-gray-600">
                            {(c.score * 100).toFixed(1)}%
                          </td>
                          <td className="py-2 px-3 text-xs text-gray-600">
                            {c.spl?.toFixed(1) ?? "—"}
                          </td>
                          <td className="py-2 px-3 text-xs text-gray-500">
                            {c.syncTime?.toDate ? c.syncTime.toDate().toLocaleTimeString() : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </div>
        </details>
      </div>
    </main>
  );
}
