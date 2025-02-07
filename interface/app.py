#!/usr/bin/env python
"""
This is a self-contained Flask web application that provides a multi-modal interface for controlling
a Stewart platform (neck) via serial commands. The interface supports:

1. Direct Motor Control – individual motor commands (e.g., "1:30,2:45,...").
2. Euler Control – controlling yaw (X), pitch (Y) and roll (Z).
3. Full Head Control – controlling yaw (X), lateral translation (Y), front/back (Z), height (H),
   speed multiplier (S), acceleration multiplier (A), roll (R) and pitch (P).
4. Quaternion Control – controlling orientation via quaternion components (w, x, y, z) with optional
   speed (S) and acceleration (A) multipliers.

Each page provides:
 • Direct command entry via a text input.
 • Plus/Minus buttons to increment/decrement each parameter.
 • Sliders that stream updated commands as they are moved.

Before any pip‑imported modules are loaded, the script checks for a virtual environment (venv) and,
if needed, creates one and installs Flask and pyserial.
"""

import os
import sys
import subprocess

# ---------- VENV Setup ----------
# Check if we are running inside a virtual environment. If not, create one, install required packages,
# and re-execute the script using the venv’s Python interpreter.
if not os.environ.get("VIRTUAL_ENV"):
    venv_dir = os.path.join(os.path.dirname(__file__), "venv")
    if not os.path.exists(venv_dir):
        print("Creating virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        # Determine pip path based on OS.
        if os.name == 'nt':
            pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
        else:
            pip_exe = os.path.join(venv_dir, "bin", "pip")
        print("Installing required packages...")
        subprocess.check_call([pip_exe, "install", "Flask", "pyserial"])
    # Determine the Python executable inside the venv.
    if os.name == 'nt':
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        python_exe = os.path.join(venv_dir, "bin", "python")
    # Re-execute the script from the venv.
    os.execv(python_exe, [python_exe] + sys.argv)

# ---------- End VENV Setup ----------
# Now that we are inside the venv, import required pip packages.
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
import serial
import threading
import time
import json

# ---------- Serial Connection Setup ----------
# Change SERIAL_PORT to the port where your ESP32 is connected (e.g., "COM3" on Windows or "/dev/ttyUSB0" on Linux).
SERIAL_PORT = "/dev/ttyUSB0"
SERIAL_BAUD = 115200

# Global serial connection variable and a lock for thread-safe writes.
ser = None
ser_lock = threading.Lock()

def init_serial():
    """Initialize the serial connection to the neck (ESP32) device."""
    global ser
    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1)
        print(f"Serial connection established on {SERIAL_PORT} at {SERIAL_BAUD} baud.")
    except Exception as e:
        print(f"Failed to open serial port: {e}")
        ser = None

# Start the serial connection in a separate thread (so as not to block the main thread).
serial_thread = threading.Thread(target=init_serial)
serial_thread.start()

# ---------- Flask Application Setup ----------
app = Flask(__name__)

# Navigation template (included on every page)
nav_template = """
<nav>
  <ul>
    <li><a href="{{ url_for('direct') }}">Direct Motor Control</a></li>
    <li><a href="{{ url_for('euler') }}">Euler Control</a></li>
    <li><a href="{{ url_for('head') }}">Full Head Control</a></li>
    <li><a href="{{ url_for('quaternion') }}">Quaternion Control</a></li>
  </ul>
</nav>
<hr>
"""

# ---------- Routes ----------

@app.route("/")
def index():
    # Home page with navigation links.
    template = """
    <html>
      <head>
        <title>Stewart Platform Control Interface</title>
      </head>
      <body>
        """ + nav_template + """
        <h1>Welcome to the Stewart Platform Control Interface</h1>
        <p>Select a control mode from the navigation menu above.</p>
      </body>
    </html>
    """
    return render_template_string(template)

# ---------------- Direct Motor Control ----------------
@app.route("/direct")
def direct():
    # Direct motor control page.
    # The expected command format is: "1:<value>,2:<value>,...,6:<value>"
    template = """
    <html>
      <head>
        <title>Direct Motor Control</title>
        <script>
          // Send a command to the backend.
          function sendCommand(command) {
            fetch("/send_command", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ command: command })
            }).then(response => response.json())
              .then(data => console.log("Response:", data));
          }
          // Gather the six motor values and compose the command.
          function updateDirectCommand() {
            let values = [];
            for (let i = 1; i <= 6; i++) {
              let val = document.getElementById("motor" + i).value;
              values.push(i + ":" + val);
            }
            let command = values.join(",");
            document.getElementById("commandDisplay").innerText = command;
            sendCommand(command);
          }
          // Debounce slider events.
          let debounceTimer;
          function sliderChanged() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(updateDirectCommand, 200);
          }
          // Increment/decrement functions.
          function incrementMotor(motorId) {
            let input = document.getElementById("motor" + motorId);
            input.value = parseInt(input.value) + 1;
            updateDirectCommand();
          }
          function decrementMotor(motorId) {
            let input = document.getElementById("motor" + motorId);
            input.value = parseInt(input.value) - 1;
            updateDirectCommand();
          }
        </script>
      </head>
      <body>
        """ + nav_template + """
        <h2>Direct Motor Control</h2>
        <div>
          {% for i in range(1,7) %}
            <div>
              <label>Motor {{ i }}:</label>
              <button onclick="decrementMotor({{ i }})">-</button>
              <input type="number" id="motor{{ i }}" value="0" onchange="updateDirectCommand()">
              <button onclick="incrementMotor({{ i }})">+</button>
              <br>
              <input type="range" id="slider{{ i }}" min="-1000" max="1000" value="0"
                oninput="document.getElementById('motor{{ i }}').value=this.value; sliderChanged();">
            </div>
            <br>
          {% endfor %}
        </div>
        <div>
          <h3>Direct Command Entry</h3>
          <input type="text" id="directCommandInput" placeholder="e.g., 1:30,2:45">
          <button onclick="sendCommand(document.getElementById('directCommandInput').value)">Send Command</button>
        </div>
        <p>Current Command: <span id="commandDisplay"></span></p>
      </body>
    </html>
    """
    return render_template_string(template)

# ---------------- Euler Control ----------------
@app.route("/euler")
def euler():
    # Euler control page for yaw, pitch and roll.
    # The expected command format is: "X<yaw>,Y<pitch>,Z<roll>"
    template = """
    <html>
      <head>
        <title>Euler Control</title>
        <script>
          function sendCommand(command) {
            fetch("/send_command", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({command: command})
            }).then(response => response.json())
              .then(data => console.log("Response:", data));
          }
          function updateEulerCommand() {
            let yaw = document.getElementById("yaw").value;
            let pitch = document.getElementById("pitch").value;
            let roll = document.getElementById("roll").value;
            let command = "X" + yaw + ",Y" + pitch + ",Z" + roll;
            document.getElementById("commandDisplay").innerText = command;
            sendCommand(command);
          }
          let debounceTimer;
          function sliderChanged() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(updateEulerCommand, 200);
          }
          function increment(fieldId) {
            let input = document.getElementById(fieldId);
            input.value = parseInt(input.value) + 1;
            updateEulerCommand();
          }
          function decrement(fieldId) {
            let input = document.getElementById(fieldId);
            input.value = parseInt(input.value) - 1;
            updateEulerCommand();
          }
        </script>
      </head>
      <body>
        """ + nav_template + """
        <h2>Euler Control</h2>
        <div>
          <label>Yaw (X):</label>
          <button onclick="decrement('yaw')">-</button>
          <input type="number" id="yaw" value="0" onchange="updateEulerCommand()">
          <button onclick="increment('yaw')">+</button>
          <br>
          <input type="range" id="yawSlider" min="-180" max="180" value="0"
            oninput="document.getElementById('yaw').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Pitch (Y):</label>
          <button onclick="decrement('pitch')">-</button>
          <input type="number" id="pitch" value="0" onchange="updateEulerCommand()">
          <button onclick="increment('pitch')">+</button>
          <br>
          <input type="range" id="pitchSlider" min="-90" max="90" value="0"
            oninput="document.getElementById('pitch').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Roll (Z):</label>
          <button onclick="decrement('roll')">-</button>
          <input type="number" id="roll" value="0" onchange="updateEulerCommand()">
          <button onclick="increment('roll')">+</button>
          <br>
          <input type="range" id="rollSlider" min="-180" max="180" value="0"
            oninput="document.getElementById('roll').value=this.value; sliderChanged();">
        </div>
        <div>
          <h3>Direct Command Entry</h3>
          <input type="text" id="eulerCommandInput" placeholder="e.g., X30,Y15,Z-10">
          <button onclick="sendCommand(document.getElementById('eulerCommandInput').value)">Send Command</button>
        </div>
        <p>Current Command: <span id="commandDisplay"></span></p>
      </body>
    </html>
    """
    return render_template_string(template)

# ---------------- Full Head Control ----------------
@app.route("/head")
def head():
    # Full head control page – controls for eight parameters.
    # The expected command format is:
    # "X<value>,Y<value>,Z<value>,H<value>,S<value>,A<value>,R<value>,P<value>"
    template = """
    <html>
      <head>
        <title>Full Head Control</title>
        <script>
          function sendCommand(command) {
            fetch("/send_command", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({command: command})
            }).then(response => response.json())
              .then(data => console.log("Response:", data));
          }
          function updateHeadCommand() {
            let X = document.getElementById("X").value;
            let Y = document.getElementById("Y").value;
            let Z = document.getElementById("Z").value;
            let H = document.getElementById("H").value;
            let S = document.getElementById("S").value;
            let A = document.getElementById("A").value;
            let R = document.getElementById("R").value;
            let P = document.getElementById("P").value;
            let command = "X" + X + ",Y" + Y + ",Z" + Z + ",H" + H + ",S" + S + ",A" + A + ",R" + R + ",P" + P;
            document.getElementById("commandDisplay").innerText = command;
            sendCommand(command);
          }
          let debounceTimer;
          function sliderChanged() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(updateHeadCommand, 200);
          }
          function increment(fieldId) {
            let input = document.getElementById(fieldId);
            input.value = parseFloat(input.value) + 1;
            updateHeadCommand();
          }
          function decrement(fieldId) {
            let input = document.getElementById(fieldId);
            input.value = parseFloat(input.value) - 1;
            updateHeadCommand();
          }
        </script>
      </head>
      <body>
        """ + nav_template + """
        <h2>Full Head Control</h2>
        <div>
          <label>Yaw (X):</label>
          <button onclick="decrement('X')">-</button>
          <input type="number" id="X" value="0" onchange="updateHeadCommand()">
          <button onclick="increment('X')">+</button>
          <br>
          <input type="range" id="XSlider" min="-180" max="180" value="0"
            oninput="document.getElementById('X').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Lateral (Y):</label>
          <button onclick="decrement('Y')">-</button>
          <input type="number" id="Y" value="0" onchange="updateHeadCommand()">
          <button onclick="increment('Y')">+</button>
          <br>
          <input type="range" id="YSlider" min="-100" max="100" value="0"
            oninput="document.getElementById('Y').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Front-Back (Z):</label>
          <button onclick="decrement('Z')">-</button>
          <input type="number" id="Z" value="0" onchange="updateHeadCommand()">
          <button onclick="increment('Z')">+</button>
          <br>
          <input type="range" id="ZSlider" min="-100" max="100" value="0"
            oninput="document.getElementById('Z').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Height (H):</label>
          <button onclick="decrement('H')">-</button>
          <input type="number" id="H" value="0" onchange="updateHeadCommand()">
          <button onclick="increment('H')">+</button>
          <br>
          <input type="range" id="HSlider" min="-100" max="100" value="0"
            oninput="document.getElementById('H').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Speed Multiplier (S):</label>
          <button onclick="decrement('S')">-</button>
          <input type="number" id="S" value="1" step="0.1" onchange="updateHeadCommand()">
          <button onclick="increment('S')">+</button>
          <br>
          <input type="range" id="SSlider" min="0.1" max="5" step="0.1" value="1"
            oninput="document.getElementById('S').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Acceleration Multiplier (A):</label>
          <button onclick="decrement('A')">-</button>
          <input type="number" id="A" value="1" step="0.1" onchange="updateHeadCommand()">
          <button onclick="increment('A')">+</button>
          <br>
          <input type="range" id="ASlider" min="0.1" max="5" step="0.1" value="1"
            oninput="document.getElementById('A').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Roll (R):</label>
          <button onclick="decrement('R')">-</button>
          <input type="number" id="R" value="0" onchange="updateHeadCommand()">
          <button onclick="increment('R')">+</button>
          <br>
          <input type="range" id="RSlider" min="-180" max="180" value="0"
            oninput="document.getElementById('R').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Pitch (P):</label>
          <button onclick="decrement('P')">-</button>
          <input type="number" id="P" value="0" onchange="updateHeadCommand()">
          <button onclick="increment('P')">+</button>
          <br>
          <input type="range" id="PSlider" min="-180" max="180" value="0"
            oninput="document.getElementById('P').value=this.value; sliderChanged();">
        </div>
        <div>
          <h3>Direct Command Entry</h3>
          <input type="text" id="headCommandInput" placeholder="e.g., X30,Y0,Z10,H-40,S1,A1,R0,P0">
          <button onclick="sendCommand(document.getElementById('headCommandInput').value)">Send Command</button>
        </div>
        <p>Current Command: <span id="commandDisplay"></span></p>
      </body>
    </html>
    """
    return render_template_string(template)

# ---------------- Quaternion Control ----------------
@app.route("/quaternion")
def quaternion():
    # Quaternion control page.
    # The expected command format is: "Q:<w>,<x>,<y>,<z>[,S<speed>][,A<accel>]"
    template = """
    <html>
      <head>
        <title>Quaternion Control</title>
        <script>
          function sendCommand(command) {
            fetch("/send_command", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({command: command})
            }).then(response => response.json())
              .then(data => console.log("Response:", data));
          }
          function updateQuaternionCommand() {
            let w = document.getElementById("w").value;
            let x = document.getElementById("x").value;
            let y = document.getElementById("y").value;
            let z = document.getElementById("z").value;
            let speed = document.getElementById("qSpeed").value;
            let accel = document.getElementById("qAccel").value;
            let command = "Q:" + w + "," + x + "," + y + "," + z;
            if(speed !== "") {
              command += ",S" + speed;
            }
            if(accel !== "") {
              command += ",A" + accel;
            }
            document.getElementById("commandDisplay").innerText = command;
            sendCommand(command);
          }
          let debounceTimer;
          function sliderChanged() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(updateQuaternionCommand, 200);
          }
          function increment(fieldId) {
            let input = document.getElementById(fieldId);
            input.value = parseFloat(input.value) + 0.1;
            updateQuaternionCommand();
          }
          function decrement(fieldId) {
            let input = document.getElementById(fieldId);
            input.value = parseFloat(input.value) - 0.1;
            updateQuaternionCommand();
          }
        </script>
      </head>
      <body>
        """ + nav_template + """
        <h2>Quaternion Control</h2>
        <div>
          <label>W:</label>
          <button onclick="decrement('w')">-</button>
          <input type="number" id="w" value="1" step="0.1" onchange="updateQuaternionCommand()">
          <button onclick="increment('w')">+</button>
          <br>
          <input type="range" id="wSlider" min="0" max="1" step="0.01" value="1" oninput="document.getElementById('w').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>X:</label>
          <button onclick="decrement('x')">-</button>
          <input type="number" id="x" value="0" step="0.1" onchange="updateQuaternionCommand()">
          <button onclick="increment('x')">+</button>
          <br>
          <input type="range" id="xSlider" min="-1" max="1" step="0.01" value="0" oninput="document.getElementById('x').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Y:</label>
          <button onclick="decrement('y')">-</button>
          <input type="number" id="y" value="0" step="0.1" onchange="updateQuaternionCommand()">
          <button onclick="increment('y')">+</button>
          <br>
          <input type="range" id="ySlider" min="-1" max="1" step="0.01" value="0" oninput="document.getElementById('y').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Z:</label>
          <button onclick="decrement('z')">-</button>
          <input type="number" id="z" value="0" step="0.1" onchange="updateQuaternionCommand()">
          <button onclick="increment('z')">+</button>
          <br>
          <input type="range" id="zSlider" min="-1" max="1" step="0.01" value="0" oninput="document.getElementById('z').value=this.value; sliderChanged();">
        </div>
        <div>
          <label>Speed Multiplier (S):</label>
          <button onclick="decrement('qSpeed')">-</button>
          <input type="number" id="qSpeed" value="1" step="0.1" onchange="updateQuaternionCommand()">
          <button onclick="increment('qSpeed')">+</button>
        </div>
        <div>
          <label>Acceleration Multiplier (A):</label>
          <button onclick="decrement('qAccel')">-</button>
          <input type="number" id="qAccel" value="1" step="0.1" onchange="updateQuaternionCommand()">
          <button onclick="increment('qAccel')">+</button>
        </div>
        <div>
          <h3>Direct Command Entry</h3>
          <input type="text" id="quatCommandInput" placeholder="e.g., Q:1,0,0,0,S1,A1">
          <button onclick="sendCommand(document.getElementById('quatCommandInput').value)">Send Command</button>
        </div>
        <p>Current Command: <span id="commandDisplay"></span></p>
      </body>
    </html>
    """
    return render_template_string(template)

# ---------------- Command Sender Endpoint ----------------
@app.route("/send_command", methods=["POST"])
def send_command():
    """
    This endpoint accepts a JSON POST with a "command" string. It appends a newline
    (as expected by the neck’s serial code) and writes it to the serial port.
    """
    data = request.get_json()
    command = data.get("command", "")
    command_to_send = command.strip() + "\n"
    if ser:
        with ser_lock:
            try:
                ser.write(command_to_send.encode('utf-8'))
                print("Sent command:", command_to_send)
            except Exception as e:
                print("Error writing to serial:", e)
                return jsonify({"status": "error", "message": str(e)})
    else:
        print("Serial connection not available. Command not sent:", command_to_send)
        return jsonify({"status": "error", "message": "Serial connection not available."})
    return jsonify({"status": "success", "command": command_to_send})

# ---------- Run the Flask Application ----------
if __name__ == "__main__":
    # Wait briefly for the serial connection thread to complete.
    serial_thread.join(timeout=2)
    app.run(host="0.0.0.0", port=5000, debug=True)
