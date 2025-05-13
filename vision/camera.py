#!/usr/bin/env python3
import os
import sys
import subprocess
import glob
import socket
import time
import threading
from flask import Flask, Response, render_template_string, jsonify

# ----------------- Auto-venv Bootstrap -----------------
if sys.prefix == sys.base_prefix:
    venv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv")
    if not os.path.exists(venv_dir):
        print("Creating virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", "venv"])
    pip_exe = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin", "pip")
    python_exe = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin", "python")
    print("Installing dependencies...")
    subprocess.check_call([pip_exe, "install", "--upgrade", "pip"])
    subprocess.check_call([pip_exe, "install", "flask", "opencv-python", "numpy"])
    print("Relaunching inside virtual environment...")
    subprocess.check_call([python_exe] + sys.argv)
    sys.exit()

# ----------------- Attempt to Import Realsense -----------------
use_realsense = False
try:
    from realsensecv import RealsenseCapture
    import cv2
    import numpy as np
    use_realsense = True
except ImportError:
    import cv2
    import numpy as np

# ----------------- Default Camera Setup via GStreamer -----------------
BASE_PORT = 9000
default_camera_ports = {}
gst_processes = []


def get_camera_devices():
    devices = glob.glob("/dev/video*")
    devices.sort()
    return devices


for i, dev in enumerate(get_camera_devices()):
    dev_id = f"default_{i}"
    port = BASE_PORT + i
    default_camera_ports[dev_id] = port
    cmd = (
        f'gst-launch-1.0 nvv4l2camerasrc device={dev} ! '
        f'"video/x-raw(memory:NVMM), format=(string)UYVY, width=(int)1920, height=(int)1080" ! '
        f'nvvidconv flip-method=3 ! '
        f'"video/x-raw(memory:NVMM), format=(string)I420, width=(int)1080, height=(int)1920" ! '
        f'nvvidconv ! videoconvert ! jpegenc ! multipartmux boundary=frame ! '
        f'tcpserversink host=0.0.0.0 port={port}'
    )
    print(f"Starting GStreamer pipeline for {dev} on port {port}\n")
    gst_processes.append(subprocess.Popen(cmd, shell=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE))

# ----------------- RealSense Capture Thread -----------------
if use_realsense:
    current_rs_color = current_rs_depth = None
    current_rs_ir_left = current_rs_ir_right = None
    current_rs_imu = {}

    def capture_rs():
        global current_rs_color, current_rs_depth, current_rs_ir_left, current_rs_ir_right, current_rs_imu
        cap = RealsenseCapture()
        cap.start()
        retry_delay = 0.1
        max_retries = 5
        while True:
            retries = 0
            ok = False
            fr = None
            # retry loop for transient wait_for_frames errors
            while retries < max_retries:
                try:
                    ok, fr = cap.read(include_ir=True)
                    break
                except RuntimeError as e:
                    print(f"[Realsense] read error: {e}. Retrying {retries+1}/{max_retries}")
                    retries += 1
                    time.sleep(retry_delay)
            # if still failed, reinitialize sensor
            if not ok or fr is None:
                print("[Realsense] frame capture failed after retries, reinitializing sensor")
                try:
                    cap.stop()
                except Exception as e:
                    print(f"[Realsense] error stopping capture: {e}")
                cap = RealsenseCapture()
                cap.start()
                continue

            # successful frame
            # fr == (color, depth_vis, ir_left, ir_right, imu_data)
            current_rs_color    = cv2.rotate(fr[0], cv2.ROTATE_90_CLOCKWISE)
            current_rs_depth    = cv2.rotate(fr[1], cv2.ROTATE_90_CLOCKWISE)
            current_rs_ir_left  = cv2.rotate(fr[2], cv2.ROTATE_90_CLOCKWISE)
            current_rs_ir_right = cv2.rotate(fr[3], cv2.ROTATE_90_CLOCKWISE)
            current_rs_imu      = fr[4]
            time.sleep(0.03)

    threading.Thread(target=capture_rs, daemon=True).start()


# ----------------- Flask Application -----------------
app = Flask(__name__)

HTML_TEMPLATE = """
<!doctype html>
<html>
  <head>
    <title>Camera Streams</title>
    <style>
      * { transition: all 0.2s ease; font-family: 'Courier New', monospace; }
      body {
        background-color: #101010;
        color: #fff;
        margin: 0; padding: 0;
      }
      .container {
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 20px;
        padding: 20px;
      }
      .card {
        background-color: #212121;
        border-radius: 16px;
        padding: 15px;
        width: 300px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        position: relative;
      }
      .card h3 {
        margin: 0;
        padding: 10px;
        border: 2px solid #fff;
        border-radius: 4px;
        opacity: 0.3;
        font-size: 0.75rem;
        text-transform: uppercase;
        mix-blend-mode: exclusion;
      }
      .card img {
        width: 100%;
        border-radius: 4px;
      }
      .buttons {
        display: flex;
        gap: 10px;
        opacity: 0.3;
        mix-blend-mode: difference;
      }
      .buttons button {
        flex: 1;
        padding: 10px;
        border-radius: 4px;
        border: 1px solid #fff;
        cursor: pointer;
      }
      .buttons button:first-child {
        background-color: #fff; color: #000;
      }
      .buttons button:last-child {
        background-color: #000; color: #fff;
      }
      .buttons button:hover {
        background-color: currentColor;
        color: currentBackground;
      }
      .imu {
        font-size: 0.8rem;
        color: #0f0;
        opacity: 0.8;
        white-space: pre;
      }
    </style>
  </head>
  <body>
    <div class="container">
      {% for cam in cams %}
      <div class="card">
        <h3>{{ cam.label }}</h3>
        <img src="{{ cam.video_url }}" alt="{{ cam.label }}">
        <div class="buttons">
          <a href="{{ cam.snapshot_url }}"><button>CAMERA</button></a>
          <a href="{{ cam.video_url }}"><button>VIDEO</button></a>
        </div>
        {% if cam.imu %}
        <div class="imu">Accel: -- | Gyro: --</div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
    <script>
      async function updateIMU() {
        try {
          const res = await fetch('/imu');
          const data = await res.json();
          document.querySelectorAll('.card').forEach((card, i) => {
            if (!card.querySelector('.imu')) return;
            const a = data.accel || [0,0,0];
            const g = data.gyro  || [0,0,0];
            card.querySelector('.imu').innerHTML =
              `Accel: ${a[0].toFixed(3)},${a[1].toFixed(3)},${a[2].toFixed(3)} ` +
              `<br>Gyro: ${g[0].toFixed(3)},${g[1].toFixed(3)},${g[2].toFixed(3)}`;
          });
        } catch (e) {
          // silent
        }
      }
      setInterval(updateIMU, 200);
    </script>
  </body>
</html>
"""

# ----------------- Helper Functions -----------------
def jpeg_buf(frame):
    ok, buf = cv2.imencode('.jpg', frame)
    return buf.tobytes() if ok else None


def socket_stream(port):
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
            print(f"Socket error port {port}: {e}")
        finally:
            s.close()
        time.sleep(0.03)


def rs_stream(frame_fn):
    while True:
        f = frame_fn()
        if f is not None:
            jpeg = jpeg_buf(f)
            if jpeg:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n')
        time.sleep(0.03)


# ----------------- Index -----------------
@app.route("/")
def index():
    cams = [
        {
            "label": f"Default Camera ({dev_id})",
            "snapshot_url": f"/camera/{dev_id}",
            "video_url": f"/video/{dev_id}",
            "imu": False
        }
        for dev_id in default_camera_ports
    ]

    if use_realsense:
        cams += [
            {"label": "Realsense D455 - Color",   "snapshot_url": "/camera/rs_color",   "video_url": "/video/rs_color",   "imu": True},
            {"label": "Realsense D455 - Depth",   "snapshot_url": "/camera/rs_depth",   "video_url": "/video/rs_depth",   "imu": True},
            {"label": "Realsense D455 - IR Left", "snapshot_url": "/camera/rs_ir_left", "video_url": "/video/rs_ir_left", "imu": True},
            {"label": "Realsense D455 - IR Right","snapshot_url": "/camera/rs_ir_right","video_url": "/video/rs_ir_right","imu": True},
        ]

    return render_template_string(HTML_TEMPLATE, cams=cams)


# ----------------- Snapshot Endpoints -----------------
@app.route("/camera/<dev_id>")
def snap_default(dev_id):
    if dev_id not in default_camera_ports:
        return Response(b"Camera not found", 404)
    port = default_camera_ports[dev_id]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", port))
        data = b""
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            data += chunk
            st = data.find(b'\xff\xd8')
            en = data.find(b'\xff\xd9')
            if st != -1 and en != -1 and en > st:
                return Response(data[st:en+2], mimetype="image/jpeg")
    finally:
        sock.close()
    return Response(b"No frame", 500)


@app.route("/camera/rs_color")
def snap_rs_color():
    if not use_realsense or current_rs_color is None:
        return Response(b"No frame", 500)
    buf = jpeg_buf(current_rs_color)
    return Response(buf, mimetype="image/jpeg") if buf else Response(b"Err", 500)


@app.route("/camera/rs_depth")
def snap_rs_depth():
    if not use_realsense or current_rs_depth is None:
        return Response(b"No frame", 500)
    buf = jpeg_buf(current_rs_depth)
    return Response(buf, mimetype="image/jpeg") if buf else Response(b"Err", 500)


@app.route("/camera/rs_ir_left")
def snap_rs_ir_left():
    if not use_realsense or current_rs_ir_left is None:
        return Response(b"No frame", 500)
    buf = jpeg_buf(current_rs_ir_left)
    return Response(buf, mimetype="image/jpeg") if buf else Response(b"Err", 500)


@app.route("/camera/rs_ir_right")
def snap_rs_ir_right():
    if not use_realsense or current_rs_ir_right is None:
        return Response(b"No frame", 500)
    buf = jpeg_buf(current_rs_ir_right)
    return Response(buf, mimetype="image/jpeg") if buf else Response(b"Err", 500)


# ----------------- IMU Endpoint -----------------
@app.route("/imu")
def imu_endpoint():
    return jsonify(current_rs_imu)


# ----------------- Video Endpoints -----------------
@app.route("/video/<dev_id>")
def video_default(dev_id):
    if dev_id not in default_camera_ports:
        return Response(b"Camera not found", 404)
    return Response(socket_stream(default_camera_ports[dev_id]),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/video/rs_color")
def video_rs_color():
    return Response(rs_stream(lambda: current_rs_color),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/video/rs_depth")
def video_rs_depth():
    return Response(rs_stream(lambda: current_rs_depth),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/video/rs_ir_left")
def video_rs_ir_left():
    return Response(rs_stream(lambda: current_rs_ir_left),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/video/rs_ir_right")
def video_rs_ir_right():
    return Response(rs_stream(lambda: current_rs_ir_right),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


# ----------------- Run -----------------
if __name__ == "__main__":
    print("Flask server starting on port 8080...")
    app.run(host="0.0.0.0", port=8080)
