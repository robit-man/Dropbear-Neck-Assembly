#!/usr/bin/env python3
"""
qr_scan.py — ultra-low-latency QR scanner from a video URL.

Usage:
  python3 qr_scan.py --video-url http://127.0.0.1:8080/video/rs_color

Notes:
  - Uses a background grabber thread and keeps only the latest frame to avoid lag.
  - Prints ALL decoded QR payloads on EVERY frame (can be spammy by design).
  - Shows a preview window with green polygons around detected QR codes.
  - Press 'q' or ESC to quit.

Requires:
  pip install opencv-python numpy
"""

import argparse
import time
import threading
from typing import Optional, Tuple, List

import cv2
import numpy as np


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--video-url",
        default="http://127.0.0.1:8080/video/rs_color",
        help="Video stream URL (HTTP/MJPEG/RTSP/etc.)",
    )
    ap.add_argument(
        "--window",
        default="QR Scan",
        help="OpenCV window title",
    )
    ap.add_argument(
        "--max-width",
        type=int,
        default=0,
        help="Optionally downscale frames to this width for faster decode (0 = disable).",
    )
    return ap.parse_args()


class LatestFrameGrabber:
    """
    Background frame grabber that always keeps only the latest frame.
    This prevents UI/decoder from falling behind buffered frames.
    """
    def __init__(self, url: str):
        # Try FFMPEG first (low-latency-friendly for IP sources)
        try:
            self.cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        except Exception:
            self.cap = cv2.VideoCapture(url)

        # Best-effort low-latency hints (not all backends honor these)
        for prop, val in [
            (cv2.CAP_PROP_BUFFERSIZE, 1),
            (cv2.CAP_PROP_FPS, 120),
            (cv2.CAP_PROP_CONVERT_RGB, 1),
        ]:
            try:
                self.cap.set(prop, val)
            except Exception:
                pass

        if not self.cap or not self.cap.isOpened():
            raise RuntimeError(f"Could not open video source: {url}")

        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._stopped = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        # Tight loop: read as fast as possible; always keep only the newest frame.
        while not self._stopped.is_set():
            ok, frame = self.cap.read()
            if not ok:
                # Small nap to avoid busy-wait if stream hiccups
                time.sleep(0.002)
                continue
            with self._lock:
                self._latest = frame

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._latest is None:
                return None
            # Return a view (no deep copy) to minimize overhead; caller must not hold long
            return self._latest

    def stop(self):
        self._stopped.set()
        try:
            self._t.join(timeout=0.5)
        except Exception:
            pass
        try:
            if self.cap:
                self.cap.release()
        except Exception:
            pass


def resize_keep_aspect(img: np.ndarray, max_w: int) -> Tuple[np.ndarray, float]:
    if max_w <= 0:
        return img, 1.0
    h, w = img.shape[:2]
    if w <= max_w:
        return img, 1.0
    new_w = max_w
    new_h = int(round(h * (new_w / w)))
    # Use INTER_AREA for downscale to keep QR modules crisp
    out = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    scale = w / float(new_w)
    return out, scale


def draw_polys(img: np.ndarray, polys: List[np.ndarray], color=(0, 255, 0)):
    if not polys:
        return
    for p in polys:
        pts = np.asarray(p, dtype=np.int32).reshape(-1, 2)
        cv2.polylines(img, [pts], True, color, 2, cv2.LINE_AA)


def main():
    args = parse_args()

    # Start grabber
    grabber = LatestFrameGrabber(args.video_url)
    print(f"[qr] opened: {args.video_url}")

    detector = cv2.QRCodeDetector()
    cv2.namedWindow(args.window, cv2.WINDOW_NORMAL)

    last_ts = time.perf_counter()
    frames = 0
    fps = 0.0

    try:
        while True:
            frame = grabber.read()
            if frame is None:
                # No frame yet; very short yield
                time.sleep(0.001)
                continue

            # Optional working resize for speed (decode still uses full-res polys via scale)
            work, scale = resize_keep_aspect(frame, args.max_width) if args.max_width > 0 else (frame, 1.0)

            # Convert to grayscale only (no contrast/binary)
            gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)

            # First try multi-detector (can return many codes)
            decoded_strings = []
            polys_full = []

            try:
                retval, decoded_info, pts, _ = detector.detectAndDecodeMulti(gray)
                if pts is not None and len(pts):
                    for poly in pts:
                        p = (np.asarray(poly).reshape(-1, 2) * scale).astype(np.float32)
                        polys_full.append(p)
                if retval and decoded_info:
                    decoded_strings.extend([s for s in decoded_info if s])
            except Exception:
                pass

            # Fallback to single detector if none decoded
            if not decoded_strings:
                try:
                    txt, pts, _ = detector.detectAndDecode(gray)
                    if pts is not None and len(pts):
                        p = (np.asarray(pts).reshape(-1, 2) * scale).astype(np.float32)
                        polys_full.append(p)
                    if txt:
                        decoded_strings.append(txt)
                except Exception:
                    pass

            # Print ALL decoded strings (every frame)
            for s in decoded_strings:
                print(s)

            # Draw green polygons around detections on the *original* frame for accurate overlay
            vis = frame.copy()
            draw_polys(vis, polys_full, color=(0, 255, 0))

            # Simple FPS indicator (updated ~once/sec)
            frames += 1
            now = time.perf_counter()
            if now - last_ts >= 1.0:
                fps = frames / (now - last_ts)
                frames = 0
                last_ts = now
            cv2.putText(vis, f"{fps:.1f} fps", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2, cv2.LINE_AA)

            # Ultra-low-latency display
            cv2.imshow(args.window, vis)
            key = cv2.waitKey(1) & 0xFF
            if key == 27 or key == ord('q'):
                break

    finally:
        grabber.stop()
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass


if __name__ == "__main__":
    main()
