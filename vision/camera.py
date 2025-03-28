#!/usr/bin/env python3
import os
import sys
import subprocess
import glob
import socket
import time
import threading

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
    subprocess.check_call([pip_exe, "install", "flask", "opencv-python", "numpy", "flask"])
    print("Relaunching inside virtual environment...")
    subprocess.check_call([python_exe] + sys.argv)
    sys.exit()

# ----------------- Attempt to Import Realsense -----------------
use_realsense = False
try:
    from realsensecv import RealsenseCapture
    import cv2  # required for realsense processing as well
    import numpy as np
    from flask import Flask, Response, render_template_string
    use_realsense = True
except ImportError:
    # Even if realsense is unavailable, we need cv2 and numpy for default cameras.
    import cv2
    import numpy as np
    from flask import Flask, Response, render_template_string

# ----------------- Default Camera Setup via GStreamer -----------------
# For default video devices (e.g., /dev/video*), we spawn GStreamer pipelines.
BASE_PORT = 9000  # starting TCP port for streams
default_camera_ports = {}  # mapping: device id (e.g. "default_0") -> TCP port
gst_processes = []         # list to hold gst-launch process objects

def get_camera_devices():
    devices = glob.glob("/dev/video*")
    devices.sort()
    return devices

devices = get_camera_devices()
if devices:
    for i, device in enumerate(devices):
        # Label these devices as "Default Camera X"
        dev_id = f"default_{i}"
        port = BASE_PORT + i
        default_camera_ports[dev_id] = port
        # GStreamer pipeline: encode as JPEG, mux into multipart stream over TCP.
        command = (
            f'gst-launch-1.0 nvv4l2camerasrc device={device} ! '
            f'"video/x-raw(memory:NVMM), format=(string)UYVY, width=(int)1920, height=(int)1080" ! '
            f'nvvidconv flip-method=3 ! '
            f'"video/x-raw(memory:NVMM), format=(string)I420, width=(int)1080, height=(int)1920" ! '
            f'nvvidconv ! videoconvert ! jpegenc ! multipartmux boundary=frame ! '
            f'tcpserversink host=0.0.0.0 port={port}'
        )
        print(f"Starting GStreamer pipeline for {device} on port {port}:\n{command}\n")
        proc = subprocess.Popen(command, shell=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gst_processes.append(proc)
else:
    print("No default video devices found at /dev/video*")

# ----------------- Realsense D455 Capture Setup -----------------
# If available, we start a thread to capture frames from the Realsense camera.
if use_realsense:
    current_rs_color = None
    current_rs_depth = None

    def capture_rs_frames():
        global current_rs_color, current_rs_depth
        cap = RealsenseCapture()  # use native resolution; add parameters if supported
        cap.start()
        while True:
            ret, frame = cap.read()
            if not ret:
                continue
            # Extract color and depth frames; rotate if needed
            color_frame = cv2.rotate(frame[0], cv2.ROTATE_90_CLOCKWISE)
            depth_frame = cv2.rotate(frame[1], cv2.ROTATE_90_CLOCKWISE)
            current_rs_color = color_frame
            current_rs_depth = depth_frame
            time.sleep(0.03)  # roughly 30fps

    threading.Thread(target=capture_rs_frames, daemon=True).start()

# ----------------- Flask Application -----------------
app = Flask(__name__)

# HTML template using darkmode, flexbox, and modular cards for each device.
HTML_TEMPLATE = """
<!doctype html>
<html>
  <head>
    <title>Camera Streams</title>
    <style>
      body {
        background-color: #121212;
        color: #fff;
        font-family: Arial, sans-serif;
        margin: 0;
        padding: 0;
      }
      h1 {
        text-align: center;
        margin: 20px 0;
      }
      .container {
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 20px;
        padding: 20px;
      }
      .card {
        background-color: #1e1e1e;
        border-radius: 8px;
        padding: 15px;
        width: 300px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      }
      .card h3 {
        margin-top: 0;
      }
      .card img {
        width: 100%;
        border-radius: 4px;
      }
      .card .buttons {
        text-align: center;
        margin-top: 10px;
      }
      .card button {
        margin: 5px;
        padding: 10px;
        border: none;
        border-radius: 4px;
        background-color: #6200ee;
        color: #fff;
        cursor: pointer;
      }
      .card button:hover {
        background-color: #3700b3;
      }
      a {
        text-decoration: none;
      }
    </style>
  </head>
  <body>
    <h1>Camera Streams</h1>
    <div class="container">
      {% for dev in default_devices %}
      <div class="card">
        <h3>{{ dev.label }}</h3>
        <!-- Display live video stream -->
        <img src="{{ dev.video_url }}" alt="{{ dev.label }}">
        <div class="buttons">
          <a href="{{ dev.snapshot_url }}"><button>CAMERA (Snapshot)</button></a>
          <a href="{{ dev.video_url }}"><button>VIDEO (Live)</button></a>
        </div>
      </div>
      {% endfor %}
      {% if realsense_available %}
      <div class="card">
        <h3>Realsense D455 - Color</h3>
        <img src="/video/realsense_color" alt="Realsense Color">
        <div class="buttons">
          <a href="/camera/realsense_color"><button>CAMERA (Snapshot)</button></a>
          <a href="/video/realsense_color"><button>VIDEO (Live)</button></a>
        </div>
      </div>
      <div class="card">
        <h3>Realsense D455 - Depth</h3>
        <img src="/video/realsense_depth" alt="Realsense Depth">
        <div class="buttons">
          <a href="/camera/realsense_depth"><button>CAMERA (Snapshot)</button></a>
          <a href="/video/realsense_depth"><button>VIDEO (Live)</button></a>
        </div>
      </div>
      {% endif %}
    </div>
  </body>
</html>
"""

@app.route("/")
def index():
    # Build list of default devices info for HTML.
    default_devices = []
    for dev_id, port in default_camera_ports.items():
        default_devices.append({
            "label": f"Default Camera ({dev_id})",
            "snapshot_url": f"/camera/{dev_id}",
            "video_url": f"/video/{dev_id}"
        })
    return render_template_string(HTML_TEMPLATE,
                                  default_devices=default_devices,
                                  realsense_available=use_realsense)

# ----------------- Default Camera Endpoints (GStreamer) -----------------
@app.route("/camera/<dev_id>")
def camera_snapshot(dev_id):
    # For default cameras, dev_id should be in default_camera_ports.
    if dev_id not in default_camera_ports:
        return Response(b"Camera not found", status=404, mimetype="text/plain")
    port = default_camera_ports[dev_id]
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", port))
    except Exception as e:
        error_msg = f"Error connecting to camera stream on port {port}: {e}"
        return Response(error_msg.encode("utf-8"), status=500, mimetype="text/plain")
    data = b""
    frame = None
    try:
        while True:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
            start = data.find(b'\xff\xd8')  # JPEG start
            end = data.find(b'\xff\xd9')    # JPEG end
            if start != -1 and end != -1 and end > start:
                frame = data[start:end+2]
                break
    except Exception as e:
        print(f"Error reading from socket: {e}")
    finally:
        s.close()
    if frame:
        return Response(frame, mimetype="image/jpeg")
    else:
        return Response(b"No frame received", status=500, mimetype="text/plain")

def default_video_stream_generator(port):
    # This generator proxies the multipart stream from gst-launch.
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("127.0.0.1", port))
            while True:
                data = s.recv(1024)
                if not data:
                    break
                yield data
        except Exception as e:
            print(f"Error in default video stream on port {port}: {e}")
        finally:
            s.close()
        time.sleep(0.03)

@app.route("/video/<dev_id>")
def video_stream(dev_id):
    if dev_id not in default_camera_ports:
        return Response(b"Camera not found", status=404, mimetype="text/plain")
    port = default_camera_ports[dev_id]
    return Response(default_video_stream_generator(port),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

# ----------------- Realsense Endpoints -----------------
def generate_frame(frame):
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        return None
    return jpeg.tobytes()

# Realsense Snapshot Endpoints
@app.route("/camera/realsense_color")
def camera_realsense_color():
    if not use_realsense or current_rs_color is None:
        return Response(b"No frame received", status=500, mimetype="text/plain")
    frame = generate_frame(current_rs_color)
    if frame is None:
        return Response(b"Failed to encode frame", status=500, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")

@app.route("/camera/realsense_depth")
def camera_realsense_depth():
    if not use_realsense or current_rs_depth is None:
        return Response(b"No frame received", status=500, mimetype="text/plain")
    frame = generate_frame(current_rs_depth)
    if frame is None:
        return Response(b"Failed to encode frame", status=500, mimetype="text/plain")
    return Response(frame, mimetype="image/jpeg")

# Realsense Live Video Streaming Generators
def rs_stream_generator(get_frame_func):
    while True:
        frame = get_frame_func()
        if frame is not None:
            ret, jpeg = cv2.imencode('.jpg', frame)
            if ret:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' +
                       jpeg.tobytes() +
                       b'\r\n')
        time.sleep(0.03)

@app.route("/video/realsense_color")
def video_realsense_color():
    def get_color():
        return current_rs_color
    return Response(rs_stream_generator(get_color),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/video/realsense_depth")
def video_realsense_depth():
    def get_depth():
        return current_rs_depth
    return Response(rs_stream_generator(get_depth),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

# ----------------- Run Flask Server with Auto-Reload -----------------
if __name__ == "__main__":
    print("Flask server starting on port 8080 with auto-reload enabled...")
    app.run(host="0.0.0.0", port=8080, debug=True, use_reloader=True)
