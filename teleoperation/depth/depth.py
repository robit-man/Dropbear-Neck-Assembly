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
    # Determine pip and python paths inside the venv.
    if os.name == "nt":
        pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_exe = os.path.join(venv_dir, "bin", "pip")
        python_exe = os.path.join(venv_dir, "bin", "python")
    print("Installing dependencies...")
    subprocess.check_call([pip_exe, "install", "--upgrade", "pip"])
    subprocess.check_call([pip_exe, "install", "flask"])
    print("Relaunching inside virtual environment...")
    subprocess.check_call([python_exe] + sys.argv)
    sys.exit()

# ----------------- Imports (inside venv) -----------------
import glob
import socket
import json
import atexit
from dataclasses import dataclass, asdict
from flask import Flask, Response, render_template_string, jsonify, request
import subprocess

# ----------------- Configuration -----------------
BASE_VIDEO_PORT = 9000  # Starting TCP port for video streams
AUDIO_PORT = 9100       # TCP port for audio stream
DA3_API_BASE = os.environ.get("DA3_API_BASE", "http://127.0.0.1:5000")
# Depth Anything API base (separate app you uploaded)
DEPTH_API_BASE = os.environ.get("DEPTH_API_BASE", "http://127.0.0.1:5000")

# ----------------- Helper: List Camera Devices -----------------
def get_camera_devices():
    devices = glob.glob("/dev/video*")
    devices.sort()
    return devices

# ----------------- Camera Toolkit -----------------
@dataclass
class CameraSettings:
    """Config for a single physical camera."""
    capture_width: int = 1920
    capture_height: int = 1080
    output_width: int = 1080
    output_height: int = 1920
    framerate: int = 60
    flip_method: int = 3  # Jetson nvvidconv flip-method (3 ≈ rotate 90°)

    @classmethod
    def from_env(cls):
        """Allow overriding defaults via environment variables."""
        return cls(
            capture_width=int(os.getenv("CAM_CAPTURE_WIDTH", "1920")),
            capture_height=int(os.getenv("CAM_CAPTURE_HEIGHT", "1080")),
            output_width=int(os.getenv("CAM_OUTPUT_WIDTH", "1080")),
            output_height=int(os.getenv("CAM_OUTPUT_HEIGHT", "1920")),
            framerate=int(os.getenv("CAM_FRAMERATE", "60")),
            flip_method=int(os.getenv("CAM_FLIP_METHOD", "3")),
        )


class CameraProcess:
    """Wraps a single GStreamer pipeline for one /dev/videoX."""

    def __init__(self, index, device, port, settings: CameraSettings):
        self.index = index
        self.device = device
        self.port = port
        self.settings = settings
        self.proc = None

    def build_pipeline(self) -> str:
        """Build an MJPEG over TCP pipeline using current settings."""
        s = self.settings
        pipeline = (
            f'gst-launch-1.0 nvv4l2camerasrc device={self.device} ! '
            f'"video/x-raw(memory:NVMM), format=(string)UYVY, '
            f'width=(int){s.capture_width}, height=(int){s.capture_height}, '
            f'framerate=(fraction){s.framerate}/1" ! '
            f'nvvidconv flip-method={s.flip_method} ! '
            f'"video/x-raw(memory:NVMM), format=(string)I420, '
            f'width=(int){s.output_width}, height=(int){s.output_height}, '
            f'framerate=(fraction){s.framerate}/1" ! '
            'nvvidconv ! videoconvert ! jpegenc ! multipartmux boundary=frame ! '
            f'tcpserversink host=0.0.0.0 port={self.port}'
        )
        return pipeline

    def start(self):
        """(Re)start the GStreamer process."""
        self.stop()
        command = self.build_pipeline()
        print(f"Starting GStreamer pipeline for {self.device} on port {self.port}:\n{command}\n")
        self.proc = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def stop(self):
        """Stop the GStreamer process if still running."""
        if self.proc is not None and self.proc.poll() is None:
            print(f"Stopping GStreamer pipeline for {self.device} on port {self.port}")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def to_dict(self):
        return {
            "id": self.index,
            "device": self.device,
            "port": self.port,
            "settings": asdict(self.settings),
        }


class CameraManager:
    """High-level toolkit for handling multiple physical cameras."""

    def __init__(self, base_port: int):
        self.base_port = base_port
        self.cameras = {}
        self.default_settings = CameraSettings.from_env()

    def init_from_devices(self):
        devices = get_camera_devices()
        if not devices:
            print("No camera devices found at /dev/video*")
            sys.exit(1)
        for i, device in enumerate(devices):
            port = self.base_port + i
            # Copy default settings so each camera can diverge later.
            settings = CameraSettings(**asdict(self.default_settings))
            cam = CameraProcess(i, device, port, settings)
            self.cameras[i] = cam
            cam.start()

    @property
    def camera_ports(self):
        return {idx: cam.port for idx, cam in self.cameras.items()}

    def get(self, cam_id: int) -> CameraProcess:
        if cam_id not in self.cameras:
            raise KeyError(f"Unknown camera id {cam_id}")
        return self.cameras[cam_id]

    def update_resolution(self, cam_id: int, width: int, height: int, framerate=None):
        """Update requested camera resolution and restart its pipeline."""
        cam = self.get(cam_id)
        cam.settings.capture_width = width
        cam.settings.capture_height = height
        # Keep 90° rotation assumption: swap for output to keep portrait vs landscape consistent.
        cam.settings.output_width = height
        cam.settings.output_height = width
        if framerate is not None:
            cam.settings.framerate = framerate
        cam.start()

    def to_list(self):
        return [cam.to_dict() for cam in self.cameras.values()]

    def shutdown(self):
        for cam in self.cameras.values():
            cam.stop()


# Instantiate and start camera pipelines.
camera_manager = CameraManager(BASE_VIDEO_PORT)
camera_manager.init_from_devices()
camera_ports = camera_manager.camera_ports

# ----------------- Audio GStreamer Pipeline -----------------
audio_proc = None
audio_command = (
    'gst-launch-1.0 alsasrc device=default ! '
    'audioconvert ! audioresample ! vorbisenc ! oggmux ! '
    f'tcpserversink host=0.0.0.0 port={AUDIO_PORT}'
)
print(f"Starting Audio GStreamer pipeline on port {AUDIO_PORT}:\n{audio_command}\n")
audio_proc = subprocess.Popen(
    audio_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
)


def shutdown_pipelines():
    print("Shutting down pipelines...")
    camera_manager.shutdown()
    global audio_proc
    if audio_proc is not None and audio_proc.poll() is None:
        audio_proc.terminate()
        try:
            audio_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            audio_proc.kill()


atexit.register(shutdown_pipelines)

# ----------------- Prepare Video Stream URL List -----------------
streams = []
for i in sorted(camera_ports.keys()):
    streams.append(f"/camera/{i}")
#streams.reverse()  # Reverse order (highest index first)

# Fill up to 6 positions with a blank (transparent 1x1 PNG) if needed.
blank_data_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAE0lEQVR42mP8z/C/HwAE/wH+W+KrAAAAAElFTkSuQmCC"
while len(streams) < 6:
    streams.append(blank_data_url)
streams_json = json.dumps(streams)

# ----------------- Flask Application -----------------
app = Flask(__name__)
app.secret_key = "replace_with_a_random_secret_key"


# ---- REST API for camera control ----
@app.route("/api/cameras")
def api_cameras():
    """Return basic info about all cameras."""
    return jsonify(camera_manager.to_list())

@app.route("/")
def index():
    html = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Multi-Camera + Depth Anything 3</title>

    <!-- Three.js import map -->
    <script type="importmap">
    {
      "imports": {
        "three": "https://unpkg.com/three@0.161.0/build/three.module.js",
        "three/addons/": "https://unpkg.com/three@0.161.0/examples/jsm/"
      }
    }
    </script>

    <!-- Font Awesome for icons -->
    <link rel="stylesheet"
          href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"/>

    <style>
      * {
        margin: 0;
        padding: 0;
        box-sizing: border-box;
      }

      html, body {
        width: 100%;
        height: 100%;
        overflow: hidden;
        background: #050505;
        color: #f9fafb;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      }

      canvas {
        display: block;
      }

      /* Root control panel container (top-left) */
      #control-panel {
        position: fixed;
        top: 16px;
        left: 16px;
        z-index: 20;
        max-width: 320px;
      }

      /* Monochrome side panel */
      .side-panel {
        background: rgba(10, 10, 10, 0.92);
        border-radius: 12px;
        border: 1px solid rgba(156, 163, 175, 0.4);
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.8);
        backdrop-filter: blur(16px);
        overflow: hidden;
      }

      .side-panel-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 10px;
        cursor: pointer;
        background: radial-gradient(circle at top left,
                                    rgba(156, 163, 175, 0.35),
                                    rgba(15, 23, 42, 0.98));
        border-bottom: 1px solid rgba(148, 163, 184, 0.5);
        column-gap: 8px;
      }

      .side-panel-title {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: #e5e7eb;
      }

      .side-panel-title i {
        font-size: 14px;
        color: #d1d5db;
      }

      .side-panel-toggle {
        width: 28px;
        height: 26px;
        border-radius: 8px;
        border: 1px solid rgba(156, 163, 175, 0.9);
        background: rgba(17, 24, 39, 0.98);
        color: #e5e7eb;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: background 0.15s ease, transform 0.15s ease;
        margin-left: 4px; /* add gap between title and arrow */
      }

      .side-panel-toggle i {
        transition: transform 0.15s ease;
      }

      .side-panel-toggle:hover {
        background: rgba(31, 41, 55, 1);
      }

      .side-panel-body {
        padding: 10px 10px 8px;
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .side-panel.collapsed .side-panel-body {
        display: none;
      }

      .side-panel.collapsed .side-panel-toggle i {
        transform: rotate(-90deg);
      }

      /* Sections & rows inside the panel */
      .panel-section {
        padding: 8px;
        border-radius: 10px;
        background: rgba(17, 24, 39, 0.96);
        border: 1px solid rgba(75, 85, 99, 0.8);
      }

      .panel-section + .panel-section {
        margin-top: 6px;
      }

      .panel-section-title {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #9ca3af;
        margin-bottom: 6px;
      }

      .panel-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        margin: 4px 0;
        font-size: 12px;
      }

      .panel-label {
        flex: 0 0 90px;
        color: #e5e7eb;
        font-weight: 500;
      }

      .panel-control {
        flex: 1 1 auto;
        display: flex;
        align-items: center;
        gap: 6px;
      }

      .panel-control.aspect {
        gap: 4px;
      }

      .panel-control.aspect span {
        font-size: 11px;
        color: #9ca3af;
      }

      .panel-input-text,
      .panel-select {
        width: 100%;
        padding: 6px 8px;
        border-radius: 8px;
        border: 1px solid rgba(75, 85, 99, 0.9);
        background: rgba(15, 23, 42, 0.98);
        color: #e5e7eb;
        font-size: 12px;
        outline: none;
      }

      .panel-input-text:focus,
      .panel-select:focus {
        border-color: rgba(209, 213, 219, 0.95);
        box-shadow: 0 0 0 1px rgba(209, 213, 219, 0.7);
      }

      .panel-input-number {
        width: 60px;
        padding: 4px 6px;
        border-radius: 7px;
        border: 1px solid rgba(75, 85, 99, 0.9);
        background: rgba(15, 23, 42, 0.98);
        color: #e5e7eb;
        font-size: 11px;
        outline: none;
      }

      .panel-input-number:focus {
        border-color: rgba(209, 213, 219, 0.95);
        box-shadow: 0 0 0 1px rgba(209, 213, 219, 0.7);
      }

      .panel-range {
        flex: 1 1 auto;
        accent-color: #9ca3af; /* grey, not blue */
      }

      .panel-value {
        min-width: 40px;
        text-align: right;
        font-size: 11px;
        color: #9ca3af;
      }

      .panel-buttons-row {
        display: flex;
        gap: 6px;
        margin-top: 4px;
      }

      .panel-button {
        flex: 1 1 auto;
        padding: 6px 10px;
        border-radius: 8px;
        border: 1px solid rgba(107, 114, 128, 0.9);
        background: radial-gradient(circle at top left,
                                    rgba(75, 85, 99, 0.8),
                                    rgba(15, 23, 42, 1));
        color: #f9fafb;
        font-size: 12px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        cursor: pointer;
        transition: background 0.15s ease, transform 0.1s ease;
      }

      .panel-button.small {
        flex: 0 0 auto;
        padding-inline: 8px;
        font-size: 11px;
      }

      .panel-button.wide {
        width: 100%;
      }

      .panel-button:hover {
        background: radial-gradient(circle at top left,
                                    rgba(156, 163, 175, 0.9),
                                    rgba(17, 24, 39, 1));
        transform: translateY(-1px);
      }

      .panel-button:disabled {
        opacity: 0.5;
        cursor: default;
        transform: none;
      }

      .panel-checkbox {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: #e5e7eb;
      }

      .panel-checkbox input[type="checkbox"] {
        width: 14px;
        height: 14px;
        accent-color: #9ca3af; /* grey */
      }

      .panel-footer {
        margin-top: 4px;
        padding-top: 6px;
        border-top: 1px solid rgba(31, 41, 55, 0.9);
        font-size: 11px;
        color: #9ca3af;
      }

      #status-line {
        white-space: normal;
      }

      @media (max-width: 720px) {
        #control-panel {
          max-width: 260px;
        }
      }
    </style>
  </head>
  <body>
    <!-- Collapsible control panel -->
    <div id="control-panel" class="side-panel collapsed">
      <div class="side-panel-header">
        <div class="side-panel-title">
          <i class="fas fa-cubes"></i>
          <span>Multi-Cam Depth</span>
        </div>
        <button class="side-panel-toggle" type="button" aria-expanded="false">
          <i class="fas fa-chevron-down"></i>
        </button>
      </div>
      <div class="side-panel-body">
        <div class="panel-section">
          <div class="panel-section-title">DA3 Server</div>
          <div class="panel-row">
            <div class="panel-label">API Base</div>
            <div class="panel-control">
              <input id="api-base" type="text" class="panel-input-text" />
            </div>
          </div>

          <div class="panel-row">
            <div class="panel-label">Model</div>
            <div class="panel-control">
              <select id="model-select" class="panel-select">
                <option value="">(loading…)</option>
              </select>
            </div>
          </div>

          <div class="panel-buttons-row">
            <button id="model-refresh" type="button" class="panel-button small">
              <i class="fas fa-sync-alt"></i><span>Refresh</span>
            </button>
            <button id="model-load" type="button" class="panel-button small">
              <i class="fas fa-download"></i><span>Load</span>
            </button>
          </div>
        </div>

        <div class="panel-section">
          <div class="panel-section-title">Ring Layout</div>
          <div class="panel-row">
            <div class="panel-label">Panel FOV</div>
            <div class="panel-control">
              <input id="panel-fov" type="range" min="20" max="120" step="1"
                     value="60" class="panel-range" />
              <span id="panel-fov-value" class="panel-value">60°</span>
            </div>
          </div>

          <div class="panel-row">
            <div class="panel-label">Aspect</div>
            <div class="panel-control aspect">
              <span>W</span>
              <input id="aspect-w" type="number" min="1" step="1"
                     value="9" class="panel-input-number" />
              <span>H</span>
              <input id="aspect-h" type="number" min="1" step="1"
                     value="16" class="panel-input-number" />
            </div>
          </div>

          <div class="panel-row">
            <div class="panel-label">Angle step</div>
            <div class="panel-control">
              <input id="angle-step" type="number" min="0" max="360" step="1"
                     value="0" class="panel-input-number" />
              <span class="panel-value">0° = auto</span>
            </div>
          </div>

          <div class="panel-row">
            <div class="panel-label">Order</div>
            <div class="panel-control">
              <input id="camera-order" type="text" class="panel-input-text"
                     placeholder="e.g. 0,1,2,0,1,2" />
              <button id="camera-order-apply" type="button"
                      class="panel-button small">
                Apply
              </button>
            </div>
          </div>
        </div>

        <div class="panel-section">
          <div class="panel-section-title">Point Cloud</div>

          <div class="panel-row">
            <div class="panel-label">Point Size</div>
            <div class="panel-control">
              <input id="point-size" type="range" min="0.002" max="0.08" step="0.001"
                     value="0.020" class="panel-range" />
              <span id="point-size-value" class="panel-value">0.020</span>
            </div>
          </div>

          <div class="panel-row">
            <div class="panel-label">PC FOV X</div>
            <div class="panel-control">
              <input id="pc-fov-x" type="range" min="60" max="120" step="1"
                     value="90" class="panel-range" />
              <span id="pc-fov-x-value" class="panel-value">90°</span>
            </div>
          </div>

          <div class="panel-row">
            <div class="panel-label">PC FOV Y</div>
            <div class="panel-control">
              <input id="pc-fov-y" type="range" min="60" max="120" step="1"
                     value="90" class="panel-range" />
              <span id="pc-fov-y-value" class="panel-value">90°</span>
            </div>
          </div>

          <div class="panel-row">
            <div class="panel-label">Depth Scale Z</div>
            <div class="panel-control">
              <input id="pc-depth-scale" type="range" min="0.25" max="4.0" step="0.01"
                     value="1.00" class="panel-range" />
              <span id="pc-depth-scale-value" class="panel-value">1.00×</span>
            </div>
          </div>

          <div class="panel-row">
            <div class="panel-label">Depth Offset</div>
            <div class="panel-control">
              <input id="pc-depth-offset" type="range" min="-5" max="5" step="0.1"
                     value="0.0" class="panel-range" />
              <span id="pc-depth-offset-value" class="panel-value">0.0</span>
            </div>
          </div>

          <div class="panel-row">
            <button id="pc-auto-align" type="button" class="panel-button wide">
              <i class="fas fa-magic"></i>
              <span>Auto stitch / Z-align</span>
            </button>
          </div>
        </div>

        <div class="panel-section">
          <div class="panel-section-title">Depth Capture</div>
          <div class="panel-row">
            <button id="depth-once" type="button" class="panel-button wide">
              <i class="fas fa-bolt"></i>
              <span>Depth snapshot (all cams)</span>
            </button>
          </div>
          <div class="panel-row">
            <label class="panel-checkbox">
              <input id="depth-auto" type="checkbox" />
              <span>Auto depth (per camera)</span>
            </label>
          </div>
        </div>

        <div class="panel-footer">
          <div id="status-line">DA3: idle</div>
        </div>
      </div>
    </div>

    <script type="module">
      import * as THREE from 'three';
      import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

      // Raw camera stream URLs from Flask; blanks are replaced by tiny PNGs
      const rawStreams = {{ streams_json|safe }};
      const videoStreams = rawStreams.filter(u =>
        typeof u === 'string' && !u.startsWith('data:image')
      );

      // ---------- Settings persistence ----------
      const LS_KEY = 'multiCamDa3Settings';

      function loadSettings() {
        try {
          const raw = localStorage.getItem(LS_KEY);
          const base = raw ? JSON.parse(raw) : {};
          // Back-compat with DA3 API base key
          if (!base.apiBase) {
            const legacy = localStorage.getItem('da3_api_base');
            if (legacy) base.apiBase = legacy;
          }
          return base;
        } catch (err) {
          console.warn('Failed to load settings', err);
          return {};
        }
      }

      function saveSettings(patch) {
        try {
          settings = { ...settings, ...patch };
          localStorage.setItem(LS_KEY, JSON.stringify(settings));
          if (patch.apiBase) {
            try { localStorage.setItem('da3_api_base', patch.apiBase); } catch (_) {}
          }
        } catch (err) {
          console.warn('Failed to save settings', err);
        }
      }

      let settings = loadSettings();

      // ---------- UI elements ----------
      const controlPanel       = document.getElementById('control-panel');
      const panelToggle        = controlPanel.querySelector('.side-panel-toggle');
      const panelHeader        = controlPanel.querySelector('.side-panel-header');

      const apiBaseInput       = document.getElementById('api-base');
      const modelSelect        = document.getElementById('model-select');
      const modelRefreshBtn    = document.getElementById('model-refresh');
      const modelLoadBtn       = document.getElementById('model-load');

      const fovSlider          = document.getElementById('panel-fov');
      const fovLabel           = document.getElementById('panel-fov-value');
      const aspectWInput       = document.getElementById('aspect-w');
      const aspectHInput       = document.getElementById('aspect-h');
      const angleStepInput     = document.getElementById('angle-step');

      const pointSizeSlider    = document.getElementById('point-size');
      const pointSizeLabel     = document.getElementById('point-size-value');

      const pcFovXSlider       = document.getElementById('pc-fov-x');
      const pcFovXLabel        = document.getElementById('pc-fov-x-value');
      const pcFovYSlider       = document.getElementById('pc-fov-y');
      const pcFovYLabel        = document.getElementById('pc-fov-y-value');

      const pcDepthScaleSlider  = document.getElementById('pc-depth-scale');
      const pcDepthScaleLabel   = document.getElementById('pc-depth-scale-value');
      const pcDepthOffsetSlider = document.getElementById('pc-depth-offset');
      const pcDepthOffsetLabel  = document.getElementById('pc-depth-offset-value');

      const pcAutoAlignButton   = document.getElementById('pc-auto-align');

      const cameraOrderInput   = document.getElementById('camera-order');
      const cameraOrderApply   = document.getElementById('camera-order-apply');

      const depthOnceButton    = document.getElementById('depth-once');
      const depthAutoCheckbox  = document.getElementById('depth-auto');
      const statusLine         = document.getElementById('status-line');

      // ---------- Three.js globals ----------
      let scene, camera, renderer, controls;
      let cameraPanels = [];
      let depthStates  = [];
      let cameraOrder  = [];
      let currentPointSize = 0.02;
      const depthMinIntervalMs = 1500;

      let da3Ensuring = false;
      let modelList   = [];
      let selectedModelId = settings.modelId || null;

      // polling timer for model list retry
      let modelRefreshRetryTimer = null;

      // depthStates[i] = { busy, jobId, auto, lastRequestTime, group, zScale, stats }
      const DA3_BASE_PC_FOV = 90; // nominal "zero correction" FOV for X/Y

      // Optional explicit yaw (degrees) per original camera index in videoStreams.
      const cameraYawDegByIndex = [
        // 0,   // cam 0 at   0°
        // 60,  // cam 1 at  60°
        // 120, // cam 2 at 120°
      ];

      init();
      animate();

      // ============================================================
      // INIT
      // ============================================================
      function init() {
        applyInitialSettings();
        setupPanelCollapsible();

        // Three.js scene
        scene = new THREE.Scene();
        scene.background = new THREE.Color(0x000000);

        camera = new THREE.PerspectiveCamera(
          75,
          window.innerWidth / window.innerHeight,
          0.01,
          1000
        );
        camera.position.set(0, 0, 0.01);

        renderer = new THREE.WebGLRenderer({ antialias: true });
        renderer.setPixelRatio(window.devicePixelRatio);
        renderer.setSize(window.innerWidth, window.innerHeight);
        document.body.appendChild(renderer.domElement);

        controls = new OrbitControls(camera, renderer.domElement);
        controls.enableDamping = true;
        controls.dampingFactor = 0.08;

        buildCameraPanels();
        rebuildLayout();

        // UI wiring
        fovSlider.addEventListener('input', () => {
          const v = parseFloat(fovSlider.value) || 60;
          fovLabel.textContent = `${v.toFixed(0)}°`;
          saveSettings({ panelFov: v });
          rebuildLayout();
        });

        aspectWInput.addEventListener('change', () => {
          saveSettings({
            aspectW: parseFloat(aspectWInput.value) || 9,
            aspectH: parseFloat(aspectHInput.value) || 16
          });
          rebuildLayout();
        });
        aspectHInput.addEventListener('change', () => {
          saveSettings({
            aspectW: parseFloat(aspectWInput.value) || 9,
            aspectH: parseFloat(aspectHInput.value) || 16
          });
          rebuildLayout();
        });

        angleStepInput.addEventListener('change', () => {
          let v = parseFloat(angleStepInput.value);
          if (!isFinite(v) || v < 0) v = 0;
          angleStepInput.value = String(v);
          saveSettings({ camAngleIncrementDeg: v });
          rebuildLayout();
        });

        pointSizeSlider.addEventListener('input', () => {
          const v = parseFloat(pointSizeSlider.value);
          if (!isFinite(v)) return;
          currentPointSize = v;
          pointSizeLabel.textContent = v.toFixed(3);
          saveSettings({ pointSize: v });
          // Update existing depth materials
          for (const state of depthStates) {
            if (!state.group) continue;
            state.group.traverse(obj => {
              if (obj.isPoints && obj.material) {
                obj.material.size = currentPointSize;
                obj.material.needsUpdate = true;
              }
            });
          }
        });

        pcFovXSlider.addEventListener('input', () => {
          updatePointCloudFovFromSliders();
        });
        pcFovYSlider.addEventListener('input', () => {
          updatePointCloudFovFromSliders();
        });

        pcDepthScaleSlider.addEventListener('input', () => {
          let v = parseFloat(pcDepthScaleSlider.value);
          if (!isFinite(v) || v <= 0) v = 1;
          pcDepthScaleLabel.textContent = v.toFixed(2) + '×';
          saveSettings({ pcScaleZ: v });
          applyPointCloudScaleToAll();
        });

        pcDepthOffsetSlider.addEventListener('input', () => {
          let v = parseFloat(pcDepthOffsetSlider.value);
          if (!isFinite(v)) v = 0;
          pcDepthOffsetLabel.textContent = v.toFixed(1);
          saveSettings({ pcDepthOffset: v });
          // Reposition all existing clouds using new offset
          for (let i = 0; i < depthStates.length; i++) {
            if (depthStates[i].group) {
              orientDepthGroupForCamera(i);
            }
          }
        });

        pcAutoAlignButton.addEventListener('click', () => {
          autoAlignDepthClouds();
        });

        apiBaseInput.addEventListener('change', () => {
          const base = apiBaseInput.value.trim();
          saveSettings({ apiBase: base });
          clearModelRefreshRetry();
          refreshModelList(true);
        });

        cameraOrderApply.addEventListener('click', () => {
          applyCameraOrderFromInput();
        });

        modelSelect.addEventListener('change', () => {
          selectedModelId = modelSelect.value || null;
          saveSettings({ modelId: selectedModelId });
        });

        modelRefreshBtn.addEventListener('click', () => {
          clearModelRefreshRetry();
          refreshModelList(true);
        });

        modelLoadBtn.addEventListener('click', () => {
          loadSelectedModel();
        });

        depthOnceButton.addEventListener('click', () => {
          for (let i = 0; i < depthStates.length; i++) {
            requestDepthForCamera(i);
          }
        });

        depthAutoCheckbox.addEventListener('change', () => {
          const enabled = depthAutoCheckbox.checked;
          saveSettings({ depthAuto: enabled });
          for (let i = 0; i < depthStates.length; i++) {
            depthStates[i].auto = enabled;
            if (enabled) requestDepthForCamera(i);
          }
        });

        window.addEventListener('resize', onWindowResize);

        // Try to hydrate model list once at startup
        refreshModelList(false);
      }

      function setupPanelCollapsible() {
        const setCollapsed = (collapsed) => {
          controlPanel.classList.toggle('collapsed', collapsed);
          panelToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
          saveSettings({ panelCollapsed: collapsed });
        };

        panelHeader.addEventListener('click', (e) => {
          // If click was on the toggle button, let its handler run
          if (e.target.closest('.side-panel-toggle')) return;
          const nowCollapsed = !controlPanel.classList.contains('collapsed');
          setCollapsed(nowCollapsed);
        });

        panelToggle.addEventListener('click', (e) => {
          e.stopPropagation();
          const nowCollapsed = !controlPanel.classList.contains('collapsed');
          setCollapsed(nowCollapsed);
        });
      }

      function applyInitialSettings() {
        // Panel FOV
        if (typeof settings.panelFov === 'number') {
          fovSlider.value = String(settings.panelFov);
        }
        fovLabel.textContent = `${parseFloat(fovSlider.value || '60').toFixed(0)}°`;

        // Aspect (default 9:16 portrait)
        if (typeof settings.aspectW === 'number') aspectWInput.value = String(settings.aspectW);
        if (typeof settings.aspectH === 'number') aspectHInput.value = String(settings.aspectH);

        // Camera angle step (deg; 0 = auto/uniform)
        const angleStep = (typeof settings.camAngleIncrementDeg === 'number')
          ? settings.camAngleIncrementDeg
          : 0;
        angleStepInput.value = String(angleStep);

        // API base
        const defaultApiBase = "{{ da3_api_base }}";
        const apiBase = settings.apiBase || defaultApiBase;
        apiBaseInput.value = apiBase;
        saveSettings({ apiBase });

        // Point size
        currentPointSize = typeof settings.pointSize === 'number' ? settings.pointSize : 0.02;
        currentPointSize = Math.min(Math.max(currentPointSize, 0.002), 0.08);
        pointSizeSlider.value = currentPointSize.toFixed(3);
        pointSizeLabel.textContent = currentPointSize.toFixed(3);

        // Depth auto
        const auto = !!settings.depthAuto;
        depthAutoCheckbox.checked = auto;

        // Camera order: arbitrary length & repeats allowed, but indices must be valid
        const cameraCount = videoStreams.length;
        const defaultOrder = Array.from({ length: cameraCount }, (_, i) => i);
        if (Array.isArray(settings.cameraOrder)) {
          const filtered = settings.cameraOrder
            .map(x => Number(x))
            .filter(n => Number.isInteger(n) && n >= 0 && n < cameraCount);
          cameraOrder = filtered.length ? filtered : defaultOrder;
        } else {
          cameraOrder = defaultOrder;
        }
        updateCameraOrderInput();

        // Panel collapsed
        const collapsed = !!settings.panelCollapsed;
        if (collapsed) {
          controlPanel.classList.add('collapsed');
          panelToggle.setAttribute('aria-expanded', 'false');
        } else {
          controlPanel.classList.remove('collapsed');
          panelToggle.setAttribute('aria-expanded', 'true');
        }

        // Point cloud FOV adjust (per-axis X/Y)
        const baseFov = DA3_BASE_PC_FOV;
        const fovX = typeof settings.pcFovX === 'number' ? settings.pcFovX : baseFov;
        const fovY = typeof settings.pcFovY === 'number' ? settings.pcFovY : baseFov;
        pcFovXSlider.value = String(fovX);
        pcFovYSlider.value = String(fovY);
        pcFovXLabel.textContent = `${fovX.toFixed(0)}°`;
        pcFovYLabel.textContent = `${fovY.toFixed(0)}°`;

        const baseRad = THREE.MathUtils.degToRad(baseFov);
        const sx = Math.tan(THREE.MathUtils.degToRad(fovX) / 2) / Math.tan(baseRad / 2);
        const sy = Math.tan(THREE.MathUtils.degToRad(fovY) / 2) / Math.tan(baseRad / 2);

        if (typeof settings.pcScaleX !== 'number') settings.pcScaleX = sx;
        if (typeof settings.pcScaleY !== 'number') settings.pcScaleY = sy;
        if (typeof settings.pcScaleZ !== 'number') settings.pcScaleZ = 1;
        if (typeof settings.pcDepthOffset !== 'number') settings.pcDepthOffset = 0;

        // Depth Z scale & offset UI
        pcDepthScaleSlider.value = settings.pcScaleZ.toFixed(2);
        pcDepthScaleLabel.textContent = settings.pcScaleZ.toFixed(2) + '×';
        pcDepthOffsetSlider.value = settings.pcDepthOffset.toFixed(1);
        pcDepthOffsetLabel.textContent = settings.pcDepthOffset.toFixed(1);

        saveSettings({
          pcFovX: fovX,
          pcFovY: fovY,
          pcScaleX: settings.pcScaleX,
          pcScaleY: settings.pcScaleY,
          pcScaleZ: settings.pcScaleZ,
          pcDepthOffset: settings.pcDepthOffset
        });
      }

      // ---------- Layout helpers ----------
      function onWindowResize() {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
      }

      function getAspect() {
        const w = parseFloat(aspectWInput.value) || 9;
        const h = parseFloat(aspectHInput.value) || 16;
        return w / h;
      }

      function clearCameraPanelsAndDepth() {
        for (const panel of cameraPanels) {
          scene.remove(panel.group);
          if (panel.mesh.geometry) panel.mesh.geometry.dispose();
          if (panel.mesh.material) panel.mesh.material.dispose();
          if (panel.texture) panel.texture.dispose();
        }
        cameraPanels = [];

        for (const state of depthStates) {
          if (state.group) {
            scene.remove(state.group);
            state.group.traverse(obj => {
              if (obj.isPoints && obj.geometry) obj.geometry.dispose();
              if (obj.material) obj.material.dispose();
            });
          }
        }
        depthStates = [];
      }

      function buildCameraPanels() {
        clearCameraPanelsAndDepth();

        const cameraCount = videoStreams.length;
        const order = (Array.isArray(cameraOrder) && cameraOrder.length > 0)
          ? cameraOrder
          : Array.from({ length: cameraCount }, (_, i) => i);

        const panelCount = order.length;

        for (let slot = 0; slot < panelCount; slot++) {
          const srcIndex = order[slot];
          if (srcIndex == null || srcIndex < 0 || srcIndex >= cameraCount) continue;
          const url = videoStreams[srcIndex];
          const panel = createCameraPanel(slot, url);
          // remember which physical camera this panel is mirroring
          panel.cameraIndex = srcIndex;
          cameraPanels.push(panel);
          depthStates.push({
            busy: false,
            jobId: null,
            auto: depthAutoCheckbox.checked,
            lastRequestTime: 0,
            group: null,
            zScale: 1,
            stats: null
          });
        }
      }

      function updateCameraOrderInput() {
        if (!cameraOrderInput) return;
        cameraOrderInput.value = cameraOrder.join(',');
      }

      function applyCameraOrderFromInput() {
        const cameraCount = videoStreams.length;
        const text = cameraOrderInput.value.trim();

        if (!text) {
          cameraOrder = Array.from({ length: cameraCount }, (_, i) => i);
        } else {
          const parts = text.split(',').map(s => s.trim()).filter(Boolean);
          const nums = parts
            .map(p => parseInt(p, 10))
            .filter(n => Number.isInteger(n) && n >= 0 && n < cameraCount);

          cameraOrder = nums.length ? nums : Array.from({ length: cameraCount }, (_, i) => i);
        }

        saveSettings({ cameraOrder });
        updateCameraOrderInput();
        buildCameraPanels();
        rebuildLayout();
        applyPointCloudScaleToAll();
      }

      function rebuildLayout() {
        const aspect = getAspect();
        const fovDeg  = parseFloat(fovSlider.value) || 60;
        const fovRad  = THREE.MathUtils.degToRad(fovDeg);
        const radius  = 4.0;

        const count = cameraPanels.length;
        if (!count) return;

        const panelWidth  = 2 * radius * Math.tan(fovRad / 2);
        const panelHeight = panelWidth / aspect;

        const angleStepSetting =
          (typeof settings.camAngleIncrementDeg === 'number' && settings.camAngleIncrementDeg > 0)
            ? settings.camAngleIncrementDeg
            : null;

        for (let i = 0; i < count; i++) {
          const panel = cameraPanels[i];

          const idx = (typeof panel.cameraIndex === 'number') ? panel.cameraIndex : i;

          let yawDeg;
          if (angleStepSetting !== null) {
            // Hard-set increment per slot in the current ring
            yawDeg = angleStepSetting * i;
          } else if (typeof cameraYawDegByIndex[idx] === 'number') {
            // Optional per-physical-camera yaw if configured
            yawDeg = cameraYawDegByIndex[idx];
          } else {
            // Fallback: evenly distribute around full circle
            yawDeg = i * 360 / Math.max(1, count);
          }

          const yaw = THREE.MathUtils.degToRad(yawDeg);

          const x = radius * Math.sin(yaw);
          const z = radius * Math.cos(yaw);

          panel.group.position.set(x, 0, z);
          panel.group.lookAt(0, 0, 0);

          if (panel.mesh.geometry) panel.mesh.geometry.dispose();
          panel.mesh.geometry = new THREE.PlaneGeometry(panelWidth, panelHeight);

          panel.forward.set(
            panel.group.position.x,
            panel.group.position.y,
            panel.group.position.z
          ).normalize();
        }

        // Re-orient & rescale existing point clouds after layout changes
        for (let i = 0; i < depthStates.length; i++) {
          if (depthStates[i].group) {
            orientDepthGroupForCamera(i);
          }
        }
      }

      // ---------- Camera panel creation ----------
      function createCameraPanel(index, url) {
        const group = new THREE.Group();
        scene.add(group);

        const canvas = document.createElement('canvas');
        const ctx    = canvas.getContext('2d', { willReadFrequently: true });
        canvas.width  = 0;
        canvas.height = 0;

        const texture = new THREE.CanvasTexture(canvas);
        texture.minFilter = THREE.LinearFilter;
        texture.magFilter = THREE.LinearFilter;
        texture.encoding  = THREE.sRGBEncoding;

        const material = new THREE.MeshBasicMaterial({
          map: texture,
          side: THREE.BackSide,
          transparent: true,
          opacity: 1.0
        });

        const mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), material);
        group.add(mesh);

        const img = new Image();
        img.crossOrigin = "anonymous";
        img.decoding = "async";
        img.src = url;

        const forward = new THREE.Vector3(0, 0, 1);

        return { index, group, mesh, img, canvas, ctx, texture, forward };
      }

      function copyImageToCanvas(img, canvas, ctx) {
        if (!img.naturalWidth || !img.naturalHeight) return;

        if (canvas.width === 0 || canvas.height === 0) {
          const aspect = getAspect();
          const baseWidth  = 640;
          const baseHeight = Math.round(baseWidth / aspect);
          canvas.width  = baseWidth;
          canvas.height = baseHeight;
        }

        const sWidth  = img.naturalWidth;
        const sHeight = img.naturalHeight;
        const destAspect = canvas.width / canvas.height;
        const srcAspect  = sWidth / sHeight;

        let sx, sy, sw, sh;

        if (srcAspect > destAspect) {
          sh = sHeight;
          sw = sh * destAspect;
          sx = (sWidth - sw) * 0.5;
          sy = 0;
        } else {
          sw = sWidth;
          sh = sw / destAspect;
          sx = 0;
          sy = (sHeight - sh) * 0.5;
        }

        ctx.drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
      }

      // ---------- Animation loop ----------
      function animate() {
        requestAnimationFrame(animate);

        for (const panel of cameraPanels) {
          const img = panel.img;
          if (!img.complete || !img.naturalWidth || !img.naturalHeight) continue;
          copyImageToCanvas(img, panel.canvas, panel.ctx);
          panel.texture.needsUpdate = true;
        }

        controls.update();
        renderer.render(scene, camera);
      }

      // ============================================================
      // DA3 API helpers
      // ============================================================
      function getApiBase() {
        const v = apiBaseInput.value.trim();
        return v.replace(/\\/+$/, '');
      }

      const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

      async function fetchModelStatus() {
        const base = getApiBase();
        if (!base) return null;
        try {
          const res = await fetch(base + "/api/model_status");
          if (!res.ok) return null;
          return await res.json();
        } catch (err) {
          console.warn("Model status check failed", err);
          return null;
        }
      }

      async function waitForModelReady(maxAttempts = 60, delayMs = 1500) {
        for (let i = 0; i < maxAttempts; i++) {
          const status = await fetchModelStatus();
          if (status) {
            if (status.status === "ready") {
              statusLine.textContent = "DA3: model ready";
              return true;
            }
            if (status.status === "error") {
              statusLine.textContent = "DA3: model error";
              return false;
            }
            statusLine.textContent = `DA3: loading (${status.progress || 0}%)`;
          }
          await sleep(delayMs);
        }
        statusLine.textContent = "DA3: model load timeout";
        return false;
      }

      async function ensureDa3ModelReady() {
        const base = getApiBase();
        if (!base) {
          statusLine.textContent = "DA3: API base URL not set";
          return false;
        }

        if (da3Ensuring) return waitForModelReady();
        da3Ensuring = true;
        try {
          const status = await fetchModelStatus();
          if (status && status.status === "ready") {
            statusLine.textContent = "DA3: model ready";
            return true;
          }
          if (status && status.status === "loading") {
            return await waitForModelReady();
          }
          statusLine.textContent = "DA3: starting model load...";
          const res = await fetch(base + "/api/load_model", { method: "POST" });
          if (!res.ok) {
            statusLine.textContent = "DA3: failed to start model load";
            return false;
          }
          return await waitForModelReady();
        } finally {
          da3Ensuring = false;
        }
      }

      // ---------- Port-parsing + model list helpers ----------
      function parseBasePort(base) {
        try {
          let urlStr = base;
          if (!/^https?:\\/\\//i.test(urlStr)) {
            urlStr = "http://" + urlStr;
          }
          const url = new URL(urlStr);
          const protocol = url.protocol;
          const hostname = url.hostname;
          const portNum = url.port ? parseInt(url.port, 10) : 5000;
          return {
            protocol,
            hostname,
            port: Number.isFinite(portNum) ? portNum : 5000
          };
        } catch (err) {
          console.warn("Could not parse API base for port hopping", err);
          return null;
        }
      }

      function buildBaseWithPort(info, port) {
        const p = port || 5000;
        return info.protocol + "//" + info.hostname + ":" + p;
      }

      async function fetchModelsRaw(base) {
        try {
          const res = await fetch(base + "/api/models/list");
          if (!res.ok) {
            return { ok: false, models: [], base, status: res.status };
          }
          const data = await res.json();
          const models = data.models || data.data || [];
          return { ok: true, models, base, status: res.status };
        } catch (err) {
          console.error("Model list error for", base, err);
          return { ok: false, models: [], base, status: 0 };
        }
      }

      function applyModelsToUi(models, showStatus) {
        modelList = models;
        modelSelect.innerHTML = "";

        if (!models.length) {
          const opt = document.createElement("option");
          opt.value = "";
          opt.textContent = "(no models from API)";
          modelSelect.appendChild(opt);
          if (showStatus) statusLine.textContent = "DA3: no models from API";
          return false;
        }

        let current = models.find(m => m.current) ||
                      models.find(m => m.id === selectedModelId) ||
                      models[0];

        models.forEach(m => {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = m.name || m.id;
          if (current && m.id === current.id) opt.selected = true;
          modelSelect.appendChild(opt);
        });

        selectedModelId = current.id;
        saveSettings({ modelId: selectedModelId });

        if (showStatus) {
          const name = current.name || current.id;
          statusLine.textContent = `DA3: models loaded (${models.length}), current: ${name}`;
        }
        return true;
      }

      function scheduleModelRefreshRetry() {
        if (modelRefreshRetryTimer !== null) return;
        modelRefreshRetryTimer = setTimeout(() => {
          modelRefreshRetryTimer = null;
          refreshModelList(false);
        }, 5000);
      }

      function clearModelRefreshRetry() {
        if (modelRefreshRetryTimer !== null) {
          clearTimeout(modelRefreshRetryTimer);
          modelRefreshRetryTimer = null;
        }
      }

      // ---------- Model list + selection (dropdown) ----------
      async function refreshModelList(showStatus = true) {
        const base = getApiBase();
        if (!base) {
          if (showStatus) statusLine.textContent = "DA3: API base URL not set";
          return;
        }

        const results = [];

        // 1) Try current base
        const primary = await fetchModelsRaw(base);
        results.push(primary);

        const portInfo = parseBasePort(base);

        if (portInfo) {
          let currentPort = portInfo.port || 5000;
          let nextPort = currentPort + 1;
          if (nextPort > 5001) nextPort = 5000;  // increment up to 5001, then wrap to 5000

          const altBase = buildBaseWithPort(portInfo, nextPort);
          if (altBase !== base) {
            const alt = await fetchModelsRaw(altBase);
            results.push(alt);

            // If neither attempt has hit 5000 yet, explicitly try 5000 as "restart"
            if (currentPort !== 5000 && nextPort !== 5000) {
              const base5000 = buildBaseWithPort(portInfo, 5000);
              if (base5000 !== base && base5000 !== altBase) {
                const r5000 = await fetchModelsRaw(base5000);
                results.push(r5000);
              }
            }
          }
        }

        // Prefer any base that returned models successfully
        const bestSuccess = results.find(r => r.ok && r.models.length);
        if (bestSuccess) {
          if (bestSuccess.base !== base) {
            apiBaseInput.value = bestSuccess.base;
            saveSettings({ apiBase: bestSuccess.base });
          }
          applyModelsToUi(bestSuccess.models, showStatus);
          clearModelRefreshRetry();
          return;
        }

        // Next-best: any OK response but empty models
        const okNoModels = results.find(r => r.ok && !r.models.length);
        if (okNoModels) {
          if (okNoModels.base !== base) {
            apiBaseInput.value = okNoModels.base;
            saveSettings({ apiBase: okNoModels.base });
          }
          applyModelsToUi(okNoModels.models, showStatus);
          // Poll until we actually see models on some port
          scheduleModelRefreshRetry();
          return;
        }

        // Everything failed
        if (showStatus) {
          statusLine.textContent = "DA3: model list error";
        }
        scheduleModelRefreshRetry();
      }

      async function loadSelectedModel() {
        const base = getApiBase();
        if (!base) {
          statusLine.textContent = "DA3: API base URL not set";
          return;
        }
        const modelId = selectedModelId || (modelSelect && modelSelect.value) || null;
        if (!modelId) {
          statusLine.textContent = "DA3: select a model first";
          return;
        }

        try {
          statusLine.textContent = `DA3: selecting model ${modelId}...`;
          const selRes = await fetch(base + "/api/models/select", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model_id: modelId })
          });
          const selJson = await selRes.json().catch(() => ({}));
          if (!selRes.ok) {
            statusLine.textContent = `DA3: select failed: ${selJson.error || selJson.message || selRes.status}`;
            return;
          }

          statusLine.textContent = `DA3: loading model ${modelId}...`;
          const loadRes = await fetch(base + "/api/load_model", { method: "POST" });
          const loadJson = await loadRes.json().catch(() => ({}));
          if (!loadRes.ok) {
            statusLine.textContent = `DA3: load failed: ${loadJson.error || loadJson.message || loadRes.status}`;
            return;
          }

          await waitForModelReady();
          // Refresh list so "current" flags update
          refreshModelList(false);
        } catch (err) {
          console.error("Model load error", err);
          statusLine.textContent = "DA3: model load error (see console)";
        }
      }

      // ---------- Depth per camera ----------
      async function requestDepthForCamera(index) {
        const state = depthStates[index];
        const panel = cameraPanels[index];
        const base  = getApiBase();

        if (!panel || !base) {
          statusLine.textContent = "DA3: API base URL not set";
          return;
        }
        if (state.busy) return;

        const now = performance.now();
        if (now - state.lastRequestTime < depthMinIntervalMs) {
          if (state.auto) {
            const remaining = depthMinIntervalMs - (now - state.lastRequestTime);
            setTimeout(() => requestDepthForCamera(index), remaining + 10);
          }
          return;
        }

        if (!panel.img.naturalWidth || !panel.img.naturalHeight) {
          console.warn("Camera", index, "frame not ready yet");
          return;
        }

        const modelOk = await ensureDa3ModelReady();
        if (!modelOk) return;

        state.busy = true;
        state.lastRequestTime = now;
        statusLine.textContent = `DA3: capturing depth for camera ${index}...`;

        copyImageToCanvas(panel.img, panel.canvas, panel.ctx);

        panel.canvas.toBlob(async (blob) => {
          if (!blob) {
            state.busy = false;
            return;
          }

          const file = new File([blob], `camera-${index}-${Date.now()}.jpg`, { type: "image/jpeg" });

          const formData = new FormData();
          formData.append("file", file);
          formData.append("resolution", "504");
          formData.append("max_points", "250000");
          formData.append("process_res_method", "upper_bound_resize");
          formData.append("align_to_input_ext_scale", "true");
          formData.append("infer_gs", "false");
          formData.append("export_feat_layers", "");
          formData.append("conf_thresh_percentile", "40");
          formData.append("apply_confidence_filter", "false");
          formData.append("include_confidence", "false");
          formData.append("show_cameras", "false");
          formData.append("feat_vis_fps", "15");

          try {
            const response = await fetch(base + "/api/v1/infer", {
              method: "POST",
              body: formData
            });
            const data = await response.json().catch(() => null);

            if (!response.ok) {
              console.error("DA3 infer error", response.status, data);
              statusLine.textContent = `DA3: error ${response.status}`;
              state.busy = false;
              if (state.auto) {
                setTimeout(() => requestDepthForCamera(index), depthMinIntervalMs);
              }
              return;
            }

            if (data.pointcloud || data.point_cloud) {
              const pc = data.pointcloud || data.point_cloud;
              statusLine.textContent = `DA3: depth ready (camera ${index})`;
              handlePointCloudForCamera(index, pc);
              state.busy = false;
              if (state.auto) {
                requestDepthForCamera(index);
              }
            } else if (data.job_id) {
              state.jobId = data.job_id;
              statusLine.textContent = `DA3: job ${data.job_id} (camera ${index})`;
              pollDepthJob(index);
            } else {
              console.warn("Unexpected DA3 response", data);
              statusLine.textContent = "DA3: unexpected response";
              state.busy = false;
              if (state.auto) {
                setTimeout(() => requestDepthForCamera(index), depthMinIntervalMs);
              }
            }
          } catch (err) {
            console.error("DA3 infer exception", err);
            statusLine.textContent = "DA3: request failed";
            state.busy = false;
            if (state.auto) {
              setTimeout(() => requestDepthForCamera(index), depthMinIntervalMs);
            }
          }
        }, "image/jpeg", 0.9);
      }

      function pollDepthJob(index) {
        const state = depthStates[index];
        const base  = getApiBase();
        if (!state.jobId || !base) return;

        const jobId = state.jobId;

        const intervalId = setInterval(async () => {
          try {
            const res = await fetch(base + "/api/v1/jobs/" + jobId);
            const data = await res.json().catch(() => null);

            if (!res.ok) {
              console.error("DA3 job polling error", res.status, data);
              clearInterval(intervalId);
              state.jobId = null;
              state.busy = false;
              if (state.auto) {
                setTimeout(() => requestDepthForCamera(index), depthMinIntervalMs);
              }
              return;
            }

            if (data.status === "completed") {
              clearInterval(intervalId);
              state.jobId = null;
              statusLine.textContent = `DA3: depth ready (camera ${index})`;
              if (data.pointcloud || data.point_cloud) {
                const pc = data.pointcloud || data.point_cloud;
                handlePointCloudForCamera(index, pc);
              }
              state.busy = false;
              if (state.auto) {
                requestDepthForCamera(index);
              }
            } else if (data.status === "error") {
              console.error("DA3 job failed", data.error);
              statusLine.textContent = "DA3: job error";
              clearInterval(intervalId);
              state.jobId = null;
              state.busy = false;
              if (state.auto) {
                setTimeout(() => requestDepthForCamera(index), depthMinIntervalMs);
              }
            }
          } catch (err) {
            console.error("DA3 job poll exception", err);
          }
        }, 1000);
      }

      // ---------- Depth stats + auto stitch ----------
      function computeDepthStats(geometry) {
        const pos = geometry.getAttribute("position");
        if (!pos) return null;
        const arr = pos.array;
        let sum = 0;
        let count = 0;
        let min = Infinity;
        let max = -Infinity;
        for (let i = 2; i < arr.length; i += 3) {
          const z = arr[i];        // camera-space z
          const d = Math.abs(z);   // treat depth as |z|
          if (!isFinite(d)) continue;
          sum += d;
          count++;
          if (d < min) min = d;
          if (d > max) max = d;
        }
        if (!count) return null;
        return { mean: sum / count, min, max, count };
      }

      function autoAlignDepthClouds() {
        const statsList = [];

        for (let i = 0; i < depthStates.length; i++) {
          const state = depthStates[i];
          const group = state.group;
          if (!group || !group.children.length) continue;
          const points = group.children[0];
          if (!points.geometry) continue;
          const stats = computeDepthStats(points.geometry);
          if (!stats) continue;
          state.stats = stats;
          statsList.push({ index: i, stats });
        }

        if (!statsList.length) {
          statusLine.textContent = "DA3: no depth clouds to auto-align";
          return;
        }

        // Use median mean-depth as target to be robust to outliers
        const means = statsList.map(e => e.stats.mean).sort((a, b) => a - b);
        const median = means[Math.floor(means.length / 2)];

        for (const entry of statsList) {
          const idx = entry.index;
          const s = entry.stats.mean > 0 ? (median / entry.stats.mean) : 1;
          depthStates[idx].zScale = s;
        }

        // Re-apply orientation/scales with per-camera Z scale
        for (let i = 0; i < depthStates.length; i++) {
          if (depthStates[i].group) {
            orientDepthGroupForCamera(i);
          }
        }

        statusLine.textContent = "DA3: auto stitch / Z-align applied";
      }

      // ---------- Point cloud construction & alignment ----------
      function handlePointCloudForCamera(index, pointcloud) {
        const vertices = pointcloud.vertices || [];
        if (!vertices.length) {
          console.warn("Empty point cloud for camera", index);
          return;
        }

        const colors = pointcloud.colors || [];
        const hasColors = colors.length === vertices.length;

        const positions  = new Float32Array(vertices.length * 3);
        const colorArray = hasColors ? new Float32Array(colors.length * 3) : null;

        for (let i = 0; i < vertices.length; i++) {
          const v = vertices[i];
          positions[3 * i + 0] = v[0];
          positions[3 * i + 1] = v[1];
          positions[3 * i + 2] = v[2];

          if (hasColors) {
            const c = colors[i] || [255, 255, 255];
            colorArray[3 * i + 0] = (c[0] || 0) / 255.0;
            colorArray[3 * i + 1] = (c[1] || 0) / 255.0;
            colorArray[3 * i + 2] = (c[2] || 0) / 255.0;
          }
        }

        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
        if (hasColors) {
          geometry.setAttribute("color", new THREE.BufferAttribute(colorArray, 3));
        }
        geometry.computeBoundingSphere();

        const material = new THREE.PointsMaterial({
          size: currentPointSize,
          sizeAttenuation: true,
          vertexColors: hasColors,
          depthWrite: false,
          transparent: true
        });

        let group = depthStates[index].group;
        if (!group) {
          group = new THREE.Group();
          depthStates[index].group = group;
          depthStates[index].zScale = depthStates[index].zScale ?? 1;
          scene.add(group);
        } else {
          while (group.children.length) {
            const child = group.children.pop();
            if (child.geometry) child.geometry.dispose();
            if (child.material) child.material.dispose();
          }
        }

        const points = new THREE.Points(geometry, material);
        group.add(points);

        // Update stats for auto-stitcher
        const stats = computeDepthStats(geometry);
        if (stats) {
          depthStates[index].stats = stats;
        }

        orientDepthGroupForCamera(index);
      }

      function orientDepthGroupForCamera(index) {
        const state = depthStates[index];
        const group = state.group;
        const panel = cameraPanels[index];
        if (!group || !panel) return;

        const forwardWorld = panel.forward.clone().normalize();
        const da3Forward   = new THREE.Vector3(0, 0, -1); // DA3 camera-forward in OpenGL space
        const quat         = new THREE.Quaternion().setFromUnitVectors(da3Forward, forwardWorld);

        group.quaternion.copy(quat);

        const sx = settings.pcScaleX ?? 1;
        const sy = settings.pcScaleY ?? 1;
        const baseSz = settings.pcScaleZ ?? 1;
        const perZ   = state.zScale ?? 1;
        const sz     = baseSz * perZ;
        group.scale.set(sx, sy, sz);

        const offset = settings.pcDepthOffset ?? 0;
        group.position.copy(forwardWorld.multiplyScalar(offset));
      }

      // ---------- Point cloud FOV-based scaling ----------
      function updatePointCloudFovFromSliders() {
        const base = DA3_BASE_PC_FOV;
        const fovX = parseFloat(pcFovXSlider.value) || base;
        const fovY = parseFloat(pcFovYSlider.value) || base;

        pcFovXLabel.textContent = `${fovX.toFixed(0)}°`;
        pcFovYLabel.textContent = `${fovY.toFixed(0)}°`;

        const baseRad = THREE.MathUtils.degToRad(base);
        const sx = Math.tan(THREE.MathUtils.degToRad(fovX) / 2) / Math.tan(baseRad / 2);
        const sy = Math.tan(THREE.MathUtils.degToRad(fovY) / 2) / Math.tan(baseRad / 2);

        saveSettings({ pcFovX: fovX, pcFovY: fovY, pcScaleX: sx, pcScaleY: sy });
        applyPointCloudScaleToAll();
      }

      function applyPointCloudScaleToAll() {
        const sx = settings.pcScaleX ?? 1;
        const sy = settings.pcScaleY ?? 1;
        const baseSz = settings.pcScaleZ ?? 1;
        for (let i = 0; i < depthStates.length; i++) {
          const state = depthStates[i];
          if (state.group) {
            const perZ = state.zScale ?? 1;
            state.group.scale.set(sx, sy, baseSz * perZ);
          }
        }
      }
    </script>
  </body>
</html>
    """
    return render_template_string(
        html,
        streams_json=streams_json,
        da3_api_base=DA3_API_BASE
    )







# The /camera/<id> route proxies the MJPEG video stream.
@app.route("/camera/<int:cam_id>")
def camera_stream(cam_id):
    if cam_id not in camera_ports:
        return "Camera not found", 404
    port = camera_ports[cam_id]
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", port))
    except Exception as e:
        return f"Error connecting to camera stream on port {port}: {e}", 500

    def generate():
        try:
            while True:
                data = s.recv(1024)
                if not data:
                    break
                yield data
        except Exception as e:
            print(f"Error reading from video socket: {e}")
        finally:
            s.close()
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# The /audio route proxies the audio stream.
@app.route("/audio")
def audio_stream():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", AUDIO_PORT))
    except Exception as e:
        return f"Error connecting to audio stream on port {AUDIO_PORT}: {e}", 500

    def generate():
        try:
            while True:
                data = s.recv(1024)
                if not data:
                    break
                yield data
        except Exception as e:
            print(f"Error reading from audio socket: {e}")
        finally:
            s.close()
    return Response(generate(), mimetype="audio/ogg")


# ----------------- Run Flask Server -----------------
if __name__ == "__main__":
    print("Flask server starting on port 8080...")
    app.run(host="0.0.0.0", port=8080)
