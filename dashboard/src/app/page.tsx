"use client";

import { useEffect, useState } from "react";
import { db } from "@/lib/firebase";
import {
  collection,
  query,
  orderBy,
  limit,
  onSnapshot,
  Timestamp,
  getDocs,
} from "firebase/firestore";
import { SoundscapeChart } from "@/components/SoundscapeChart";
import { BatDetectionFeed } from "@/components/BatDetectionFeed";
import { StatsCards } from "@/components/StatsCards";
import { SPLTimeline } from "@/components/SPLTimeline";

interface Classification {
  id: string;
  label: string;
  score: number;
  spl: number;
  device: string;
  syncId: string;
  syncTime: Timestamp;
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
}

export default function Dashboard() {
  const [classifications, setClassifications] = useState<Classification[]>([]);
  const [batDetections, setBatDetections] = useState<BatDetection[]>([]);
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    // Debug: log Firebase config and try a raw query
    console.log("[Firebase] Project ID:", db.app.options.projectId);
    console.log("[Firebase] App name:", db.app.name);
    
    // Debug: try the simplest possible query
    const debugRef = collection(db, "classifications");
    getDocs(query(debugRef, limit(5))).then((snap) => {
      console.log(`[Debug] Raw query returned ${snap.docs.length} docs`);
      snap.docs.forEach((doc) => {
        const d = doc.data();
        console.log(`[Debug] Doc ${doc.id}:`, {
          label: d.label,
          syncTime: d.syncTime,
          syncTimeType: typeof d.syncTime,
          hasSyncTime: "syncTime" in d,
        });
      });
    }).catch((err) => console.error("[Debug] Raw query error:", err));

    // Real-time listener for classifications
    const classQuery = query(
      collection(db, "classifications"),
      orderBy("syncTime", "desc"),
      limit(100)
    );

    const unsubClass = onSnapshot(
      classQuery,
      (snapshot) => {
        console.log(`[Firestore] Got ${snapshot.docs.length} classifications`);
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
          console.log(`[Firestore] Fallback got ${snapshot.docs.length} classifications`);
          const data = snapshot.docs.map((doc) => ({
            id: doc.id,
            ...doc.data(),
          })) as Classification[];
          setClassifications(data);
        });
      }
    );

    // Real-time listener for bat detections
    const batQuery = query(
      collection(db, "batDetections"),
      orderBy("detectionTime", "desc"),
      limit(50)
    );

    const unsubBat = onSnapshot(
      batQuery,
      (snapshot) => {
        console.log(`[Firestore] Got ${snapshot.docs.length} bat detections`);
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
          limit(50)
        );
        onSnapshot(fallbackQuery, (snapshot) => {
          console.log(`[Firestore] Fallback got ${snapshot.docs.length} bat detections`);
          const data = snapshot.docs.map((doc) => ({
            id: doc.id,
            ...doc.data(),
          })) as BatDetection[];
          setBatDetections(data);
        });
      }
    );

    return () => {
      unsubClass();
      unsubBat();
    };
  }, []);

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
        {/* Stats Cards */}
        <StatsCards
          classifications={classifications}
          batDetections={batDetections}
        />

        {/* Charts Row */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <SoundscapeChart classifications={classifications} />
          <BatDetectionFeed detections={batDetections} />
        </div>

        {/* SPL Timeline */}
        <SPLTimeline classifications={classifications} />

        {/* Recent Classifications Table */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">
            Recent Classifications
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200">
                  <th className="text-left py-3 px-4 font-medium text-gray-500">
                    Label
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500">
                    Score
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500">
                    SPL (dB)
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500">
                    Device
                  </th>
                  <th className="text-left py-3 px-4 font-medium text-gray-500">
                    Time
                  </th>
                </tr>
              </thead>
              <tbody>
                {classifications.slice(0, 20).map((c) => (
                  <tr
                    key={c.id}
                    className="border-b border-gray-100 hover:bg-gray-50"
                  >
                    <td className="py-3 px-4 font-medium text-gray-900">
                      {c.label}
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <div className="w-16 bg-gray-200 rounded-full h-2">
                          <div
                            className="bg-blue-500 h-2 rounded-full"
                            style={{ width: `${Math.min(c.score * 100, 100)}%` }}
                          />
                        </div>
                        <span className="text-gray-600">
                          {(c.score * 100).toFixed(1)}%
                        </span>
                      </div>
                    </td>
                    <td className="py-3 px-4 text-gray-600">
                      {c.spl?.toFixed(1) ?? "—"}
                    </td>
                    <td className="py-3 px-4 text-gray-600">{c.device}</td>
                    <td className="py-3 px-4 text-gray-500">
                      {c.syncTime?.toDate
                        ? c.syncTime.toDate().toLocaleTimeString()
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </main>
  );
}
