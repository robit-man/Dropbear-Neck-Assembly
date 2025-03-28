#!/usr/bin/env python3
import os
import sys
import subprocess

# ----------------- Auto‑venv Bootstrap -----------------
if sys.prefix == sys.base_prefix:
    venv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv")
    if not os.path.exists(venv_dir):
        print("Creating virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", "venv"])
    if os.name == "nt":
        pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_exe = os.path.join(venv_dir, "bin", "pip")
        python_exe = os.path.join(venv_dir, "bin", "python")
    print("Installing dependencies...")
    subprocess.check_call([pip_exe, "install", "--upgrade", "pip"])
    subprocess.check_call([pip_exe, "install", "flask", "opencv-python", "numpy"])
    print("Relaunching inside virtual environment...")
    subprocess.check_call([python_exe] + sys.argv)
    sys.exit()

# ----------------- Imports (inside venv) -----------------
from flask import Flask, Response, render_template_string
import cv2
import numpy as np
import time
import threading
from realsensecv import RealsenseCapture

# ----------------- Realsense D455 Capture -----------------
# Global variables to store the latest frames
current_color_frame = None
current_depth_frame = None

def capture_frames():
    global current_color_frame, current_depth_frame
    cap = RealsenseCapture()
    cap.start()
    while True:
        ret, frame = cap.read()
        if not ret:
            continue
        # Extract color and depth frames from the tuple
        color_frame = frame[0]
        depth_frame = frame[1]
        # Rotate frames if needed
        color_frame = cv2.rotate(color_frame, cv2.ROTATE_90_CLOCKWISE)
        depth_frame = cv2.rotate(depth_frame, cv2.ROTATE_90_CLOCKWISE)
        # Double the resolution (scale by factor of 2)
        color_frame = cv2.resize(color_frame, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)
        depth_frame = cv2.resize(depth_frame, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)
        # Update globals
        current_color_frame = color_frame
        current_depth_frame = depth_frame
        # Pause briefly (~30 fps)
        time.sleep(0.03)

# Start the capture thread in the background
capture_thread = threading.Thread(target=capture_frames, daemon=True)
capture_thread.start()

# ----------------- Flask Application -----------------
app = Flask(__name__)

@app.route("/")
def index():
    html = """
    <!doctype html>
    <html>
      <head>
        <title>Realsense D455 Streams</title>
      </head>
      <body>
        <h1>Realsense D455 Streams</h1>
        <div style="margin-bottom:20px;">
          <h3>Color Stream</h3>
          <img src="/camera/color" alt="Color Stream">
        </div>
        <hr>
        <div style="margin-bottom:20px;">
          <h3>Depth Stream</h3>
          <img src="/camera/depth" alt="Depth Stream">
        </div>
      </body>
    </html>
    """
    return render_template_string(html)

def generate_frame(frame):
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        return None
    return jpeg.tobytes()

@app.route("/camera/color")
def camera_color():
    if current_color_frame is None:
        return Response(b"No frame received", status=500, mimetype="text/plain")
    frame = generate_frame(current_color_frame)
    if frame is None:
        return Response(b"Failed to encode frame", status=500, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")

@app.route("/camera/depth")
def camera_depth():
    if current_depth_frame is None:
        return Response(b"No frame received", status=500, mimetype="text/plain")
    frame = generate_frame(current_depth_frame)
    if frame is None:
        return Response(b"Failed to encode frame", status=500, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")

# ----------------- Run Flask Server -----------------
if __name__ == "__main__":
    print("Flask server starting on port 8080...")
    app.run(host="0.0.0.0", port=8080)
