/**
 * Thin wrapper around Firebase Storage's resumable upload so the
 * UploadAnalysisPanel can show progress + cancel without caring about
 * the SDK surface.
 *
 * Uploads land at gs://<bucket>/uploads/<jobId>.wav — matching the
 * Pi-side worker's download path and the storage.rules allowlist.
 */

import { getApp } from "firebase/app";
import {
  getStorage,
  ref as storageRef,
  uploadBytesResumable,
  type UploadTask,
} from "firebase/storage";

export interface UploadHandle {
  promise: Promise<void>;
  cancel: () => void;
}

/**
 * Kick off a resumable upload of a .wav file. The returned promise
 * resolves once the upload is fully committed in GCS — at that point
 * the Pi worker can download the object. Progress callbacks fire with
 * a 0–1 fraction so the caller can drive a progress bar directly.
 */
export function uploadWavWithProgress(
  file: File,
  jobId: string,
  onProgress?: (fraction: number) => void,
): UploadHandle {
  const storage = getStorage(getApp());
  const objectRef = storageRef(storage, `uploads/${jobId}.wav`);

  const task: UploadTask = uploadBytesResumable(objectRef, file, {
    contentType: "audio/wav",
  });

  const promise = new Promise<void>((resolve, reject) => {
    task.on(
      "state_changed",
      (snapshot) => {
        if (onProgress && snapshot.totalBytes > 0) {
          onProgress(snapshot.bytesTransferred / snapshot.totalBytes);
        }
      },
      (err) => reject(err),
      () => resolve(),
    );
  });

  return {
    promise,
    cancel: () => task.cancel(),
  };
}
