#!/usr/bin/env python
"""
This is a self-contained Flask web application for controlling a Stewart platform (“neck”)
via serial commands. It supports multiple control modes:

  1. Direct Motor Control: Individual motor commands (e.g., "1:30,2:45,...").
  2. Euler Control: Control yaw (X), pitch (Y) and roll (Z) (e.g., "X30,Y15,Z-10").
  3. Full Head Control: Control yaw (X), lateral translation (Y), front/back (Z),
     height (H), speed multiplier (S), acceleration multiplier (A), roll (R) and pitch (P)
     (e.g., "X30,Y0,Z10,H-40,S1,A1,R0,P0").
  4. Quaternion Control: Control orientation via quaternion (w, x, y, z) plus optional
     speed (S) and acceleration (A) multipliers (e.g., "Q:1,0,0,0,S1,A1").

Before any control pages are available the user is presented with a “Connect to Neck”
page. There the user selects a serial port from a dropdown and clicks “Connect.”

All pages use a dark‑mode interface with background #111 and text #FFFAFA. All content is
centered in a 1024px‑wide container, using flexbox with a 0.5rem gap. Buttons are outlined
with #FFFAFA, have 0.5rem padding and 0.25rem border‑radius. A footer “console” displays every
serial command sent.

The fonts are imported from Google Fonts.
 
Before any pip‑imported modules are loaded, the script checks for (and if needed creates) a virtual
environment so that Flask and pyserial are installed automatically.
"""

import os
import sys
import subprocess

# ---------- VENV SETUP ----------
def in_virtualenv():
    return sys.prefix != sys.base_prefix

if not in_virtualenv():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(script_dir, "venv")
    if not os.path.exists(venv_dir):
        print("Creating virtual environment...")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
    if os.name == "nt":
        pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_exe = os.path.join(venv_dir, "bin", "pip")
        python_exe = os.path.join(venv_dir, "bin", "python")
    print("Installing required packages (Flask, pyserial)...")
    subprocess.check_call([pip_exe, "install", "Flask", "pyserial"])
    print("Restarting script inside virtual environment...")
    os.execv(python_exe, [python_exe] + sys.argv)
# ---------- End VENV SETUP ----------

# Now that we are inside the venv, import required modules.
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
import serial
from serial.tools import list_ports
import threading
import time
import json

# ---------- Serial Connection Setup ----------
# (We do NOT auto‑connect at startup. The user will select a port manually.)
SERIAL_BAUD = 115200
ser = None  # Global serial connection variable.
ser_lock = threading.Lock()  # Lock for thread‑safe serial writes.

# ---------- Flask Application Setup ----------
app = Flask(__name__)

# ---------- Base CSS and JavaScript (Dark Mode, Flexbox Layout) ----------
base_css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Exo:ital,wght@0,100..900;1,100..900&family=Monomaniac+One&family=Oxanium:wght@200..800&family=Roboto+Mono:ital,wght@0,100..700;1,100..700&display=swap');
body {
    background: #111;
    color: #FFFAFA;
    font-family: 'Roboto Mono', monospace;
    margin: 0;
    padding: 0;
}
.container {
    width: 1024px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 1rem;
}
nav ul {
    list-style: none;
    display: flex;
    gap: 0.5rem;
    padding: 0;
    margin: 0;
}
nav ul li {
    margin: 0;
}
nav ul li a {
    color: #FFFAFA;
    text-decoration: none;
    border: 1px solid #FFFAFA;
    padding: 0.5rem;
    border-radius: 0.25rem;
}
nav ul li a:hover {
    background: #222;
}
.row {
    display: flex;
    flex-direction: row;
    gap: 0.5rem;
    align-items: center;
}
.column {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}
button {
    background: none;
    border: 1px solid #FFFAFA;
    color: #FFFAFA;
    padding: 0.5rem;
    border-radius: 0.25rem;
    cursor: pointer;
}
button:hover {
    background: #222;
}
input[type="number"],
input[type="text"],
input[type="range"],
select {
    background: #222;
    border: 1px solid #FFFAFA;
    color: #FFFAFA;
    padding: 0.5rem;
    border-radius: 0.25rem;
}
footer {
    background: #222;
    padding: 0.5rem;
    font-size: 0.8rem;
    overflow-y: auto;
    max-height: 150px;
    border-top: 1px solid #FFFAFA;
}
</style>
"""

base_js = """
<script>
function logToConsole(msg) {
    var consoleEl = document.getElementById('console');
    if (consoleEl) {
        var newLine = document.createElement('div');
        newLine.textContent = msg;
        consoleEl.appendChild(newLine);
        consoleEl.scrollTop = consoleEl.scrollHeight;
    }
}
</script>
"""

# ---------- Navigation Bar HTML ----------
nav_html = """
<nav>
  <ul>
    <li><a href="/home">Home</a></li>
    <li><a href="/direct">Direct Motor Control</a></li>
    <li><a href="/euler">Euler Control</a></li>
    <li><a href="/head">Full Head Control</a></li>
    <li><a href="/quaternion">Quaternion Control</a></li>
  </ul>
</nav>
<hr>
"""

# ---------- Page Templates Using Placeholder Markers ----------
# We use placeholders %%CSS%%, %%JS%%, and %%NAV%% for simple string replacement.

# 1. Connect Page – with a dropdown of available ports.
connect_page = """
<html>
<head>
  <title>Connect to Neck</title>
  %%CSS%%
  %%JS%%
  <script>
    function checkStatus() {
      fetch('/status')
      .then(response => response.json())
      .then(data => {
          if (data.connected) {
              document.getElementById('status').textContent = "Connected!";
              document.getElementById('proceedBtn').style.display = "block";
          } else {
              document.getElementById('status').textContent = "Not Connected";
              document.getElementById('proceedBtn').style.display = "none";
          }
      });
    }
    setInterval(checkStatus, 2000);
  </script>
</head>
<body>
  <div class="container">
    <h1>Connect to Neck</h1>
    <div class="row">
      <label for="portSelect">Select Port:</label>
      <select id="portSelect"></select>
      <button onclick="doConnectManual()">Connect</button>
      <span id="status">Checking...</span>
    </div>
    <div class="row">
      <a id="proceedBtn" href="/home" style="display:none;">Proceed to Control Interface</a>
    </div>
  </div>
  <footer id="console"></footer>
  <script>
  // Populate the dropdown with available ports from the server.
  fetch('/available_ports')
    .then(response => response.json())
    .then(data => {
      let select = document.getElementById('portSelect');
      data.ports.forEach(function(port) {
          let opt = document.createElement('option');
          opt.value = port;
          opt.textContent = port;
          select.appendChild(opt);
      });
    });
  function doConnectManual() {
      let port = document.getElementById('portSelect').value;
      fetch('/do_connect_manual', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({port: port})
      })
      .then(response => response.json())
      .then(data => {
          if (data.connected) {
              document.getElementById('status').textContent = "Connected!";
              document.getElementById('proceedBtn').style.display = "block";
              logToConsole("Connected to " + port);
          } else {
              document.getElementById('status').textContent = "Connection failed.";
              logToConsole("Connection failed to " + port);
          }
      });
  }
  </script>
</body>
</html>
"""
connect_page = connect_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js)

# 2. Home Page
home_page = """
<html>
<head>
  <title>Stewart Platform Control Interface</title>
  %%CSS%%
  %%JS%%
</head>
<body>
  <div class="container">
    %%NAV%%
    <h1>Stewart Platform Control Interface</h1>
    <p>Select a control mode from the navigation menu above.</p>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
home_page = home_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html)

# 3. Direct Motor Control Page
direct_page = """
<html>
<head>
  <title>Direct Motor Control</title>
  %%CSS%%
  %%JS%%
  <script>
    function sendCommand(command) {
      fetch('/send_command', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({command: command})
      }).then(response => response.json())
        .then(data => {
            console.log("Response:", data);
            logToConsole("Sent: " + command);
        });
    }
    function updateDirect() {
      let cmdParts = [];
      for (let i = 1; i <= 6; i++) {
        let val = document.getElementById('motor' + i).value;
        cmdParts.push(i + ":" + val);
      }
      let cmd = cmdParts.join(",");
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
    }
    let debounceTimer;
    function sliderUpdate() {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(updateDirect, 200);
    }
    function incMotor(id) {
      let el = document.getElementById('motor' + id);
      el.value = parseInt(el.value) + 1;
      updateDirect();
    }
    function decMotor(id) {
      let el = document.getElementById('motor' + id);
      el.value = parseInt(el.value) - 1;
      updateDirect();
    }
  </script>
</head>
<body>
  <div class="container">
    %%NAV%%
    <h2>Direct Motor Control</h2>
    {% for i in range(1,7) %}
      <div class="row">
        <label>Motor {{ i }}:</label>
        <button onclick="decMotor({{ i }})">-</button>
        <input type="number" id="motor{{ i }}" value="0" onchange="updateDirect()">
        <button onclick="incMotor({{ i }})">+</button>
        <input type="range" id="slider{{ i }}" min="-1000" max="1000" value="0"
               oninput="document.getElementById('motor{{ i }}').value=this.value; sliderUpdate();">
      </div>
    {% endfor %}
    <div class="row">
      <input type="text" id="directCmdInput" placeholder="e.g., 1:30,2:45">
      <button onclick="sendCommand(document.getElementById('directCmdInput').value)">Send Command</button>
    </div>
    <p>Current Command: <span id="currentCmd"></span></p>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
direct_page = direct_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html)

# 4. Euler Control Page
euler_page = """
<html>
<head>
  <title>Euler Control</title>
  %%CSS%%
  %%JS%%
  <script>
    function sendCommand(command) {
      fetch('/send_command', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({command: command})
      }).then(response => response.json())
        .then(data => {
            console.log("Response:", data);
            logToConsole("Sent: " + command);
        });
    }
    function updateEuler() {
      let yaw = document.getElementById('yaw').value;
      let pitch = document.getElementById('pitch').value;
      let roll = document.getElementById('roll').value;
      let cmd = "X" + yaw + ",Y" + pitch + ",Z" + roll;
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
    }
    let debounceTimer;
    function sliderUpdate() {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(updateEuler, 200);
    }
    function incField(field) {
      let el = document.getElementById(field);
      el.value = parseInt(el.value) + 1;
      updateEuler();
    }
    function decField(field) {
      let el = document.getElementById(field);
      el.value = parseInt(el.value) - 1;
      updateEuler();
    }
  </script>
</head>
<body>
  <div class="container">
    %%NAV%%
    <h2>Euler Control</h2>
    <div class="row">
      <label>Yaw (X):</label>
      <button onclick="decField('yaw')">-</button>
      <input type="number" id="yaw" value="0" onchange="updateEuler()">
      <button onclick="incField('yaw')">+</button>
      <input type="range" id="yawSlider" min="-180" max="180" value="0"
             oninput="document.getElementById('yaw').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Pitch (Y):</label>
      <button onclick="decField('pitch')">-</button>
      <input type="number" id="pitch" value="0" onchange="updateEuler()">
      <button onclick="incField('pitch')">+</button>
      <input type="range" id="pitchSlider" min="-90" max="90" value="0"
             oninput="document.getElementById('pitch').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Roll (Z):</label>
      <button onclick="decField('roll')">-</button>
      <input type="number" id="roll" value="0" onchange="updateEuler()">
      <button onclick="incField('roll')">+</button>
      <input type="range" id="rollSlider" min="-180" max="180" value="0"
             oninput="document.getElementById('roll').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <input type="text" id="eulerCmdInput" placeholder="e.g., X30,Y15,Z-10">
      <button onclick="sendCommand(document.getElementById('eulerCmdInput').value)">Send Command</button>
    </div>
    <p>Current Command: <span id="currentCmd"></span></p>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
euler_page = euler_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html)

# 5. Full Head Control Page
head_page = """
<html>
<head>
  <title>Full Head Control</title>
  %%CSS%%
  %%JS%%
  <script>
    function sendCommand(command) {
      fetch('/send_command', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({command: command})
      }).then(response => response.json())
        .then(data => {
            console.log("Response:", data);
            logToConsole("Sent: " + command);
        });
    }
    function updateHead() {
      let X = document.getElementById('X').value;
      let Y = document.getElementById('Y').value;
      let Z = document.getElementById('Z').value;
      let H = document.getElementById('H').value;
      let S = document.getElementById('S').value;
      let A = document.getElementById('A').value;
      let R = document.getElementById('R').value;
      let P = document.getElementById('P').value;
      let cmd = "X" + X + ",Y" + Y + ",Z" + Z + ",H" + H + ",S" + S + ",A" + A + ",R" + R + ",P" + P;
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
    }
    let debounceTimer;
    function sliderUpdate() {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(updateHead, 200);
    }
    function incField(field, step) {
      let el = document.getElementById(field);
      if(field === 'S' || field === 'A') {
        el.value = parseFloat(el.value) + step;
      } else {
        el.value = parseInt(el.value) + step;
      }
      updateHead();
    }
    function decField(field, step) {
      let el = document.getElementById(field);
      if(field === 'S' || field === 'A') {
        el.value = parseFloat(el.value) - step;
      } else {
        el.value = parseInt(el.value) - step;
      }
      updateHead();
    }
  </script>
</head>
<body>
  <div class="container">
    %%NAV%%
    <h2>Full Head Control</h2>
    <div class="row">
      <label>Yaw (X):</label>
      <button onclick="decField('X', 1)">-</button>
      <input type="number" id="X" value="0" onchange="updateHead()">
      <button onclick="incField('X', 1)">+</button>
      <input type="range" id="XSlider" min="-180" max="180" value="0"
             oninput="document.getElementById('X').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Lateral (Y):</label>
      <button onclick="decField('Y', 1)">-</button>
      <input type="number" id="Y" value="0" onchange="updateHead()">
      <button onclick="incField('Y', 1)">+</button>
      <input type="range" id="YSlider" min="-100" max="100" value="0"
             oninput="document.getElementById('Y').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Front/Back (Z):</label>
      <button onclick="decField('Z', 1)">-</button>
      <input type="number" id="Z" value="0" onchange="updateHead()">
      <button onclick="incField('Z', 1)">+</button>
      <input type="range" id="ZSlider" min="-100" max="100" value="0"
             oninput="document.getElementById('Z').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Height (H):</label>
      <button onclick="decField('H', 1)">-</button>
      <input type="number" id="H" value="0" onchange="updateHead()">
      <button onclick="incField('H', 1)">+</button>
      <input type="range" id="HSlider" min="-100" max="100" value="0"
             oninput="document.getElementById('H').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Speed Multiplier (S):</label>
      <button onclick="decField('S', 0.1)">-</button>
      <input type="number" id="S" value="1" step="0.1" onchange="updateHead()">
      <button onclick="incField('S', 0.1)">+</button>
      <input type="range" id="SSlider" min="0.1" max="5" step="0.1" value="1"
             oninput="document.getElementById('S').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Acceleration Multiplier (A):</label>
      <button onclick="decField('A', 0.1)">-</button>
      <input type="number" id="A" value="1" step="0.1" onchange="updateHead()">
      <button onclick="incField('A', 0.1)">+</button>
      <input type="range" id="ASlider" min="0.1" max="5" step="0.1" value="1"
             oninput="document.getElementById('A').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Roll (R):</label>
      <button onclick="decField('R', 1)">-</button>
      <input type="number" id="R" value="0" onchange="updateHead()">
      <button onclick="incField('R', 1)">+</button>
      <input type="range" id="RSlider" min="-180" max="180" value="0"
             oninput="document.getElementById('R').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Pitch (P):</label>
      <button onclick="decField('P', 1)">-</button>
      <input type="number" id="P" value="0" onchange="updateHead()">
      <button onclick="incField('P', 1)">+</button>
      <input type="range" id="PSlider" min="-180" max="180" value="0"
             oninput="document.getElementById('P').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <input type="text" id="headCmdInput" placeholder="e.g., X30,Y0,Z10,H-40,S1,A1,R0,P0">
      <button onclick="sendCommand(document.getElementById('headCmdInput').value)">Send Command</button>
    </div>
    <p>Current Command: <span id="currentCmd"></span></p>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
head_page = head_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html)

# 6. Quaternion Control Page
quat_page = """
<html>
<head>
  <title>Quaternion Control</title>
  %%CSS%%
  %%JS%%
  <script>
    function sendCommand(command) {
      fetch('/send_command', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({command: command})
      }).then(response => response.json())
        .then(data => {
            console.log("Response:", data);
            logToConsole("Sent: " + command);
        });
    }
    function updateQuat() {
      let w = document.getElementById('w').value;
      let x = document.getElementById('x').value;
      let y = document.getElementById('y').value;
      let z = document.getElementById('z').value;
      let S = document.getElementById('qS').value;
      let A = document.getElementById('qA').value;
      let cmd = "Q:" + w + "," + x + "," + y + "," + z;
      if (S !== "") { cmd += ",S" + S; }
      if (A !== "") { cmd += ",A" + A; }
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
    }
    let debounceTimer;
    function sliderUpdate() {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(updateQuat, 200);
    }
    function incField(field, step) {
      let el = document.getElementById(field);
      el.value = parseFloat(el.value) + step;
      updateQuat();
    }
    function decField(field, step) {
      let el = document.getElementById(field);
      el.value = parseFloat(el.value) - step;
      updateQuat();
    }
  </script>
</head>
<body>
  <div class="container">
    %%NAV%%
    <h2>Quaternion Control</h2>
    <div class="row">
      <label>W:</label>
      <button onclick="decField('w', 0.1)">-</button>
      <input type="number" id="w" value="1" step="0.1" onchange="updateQuat()">
      <button onclick="incField('w', 0.1)">+</button>
      <input type="range" id="wSlider" min="0" max="1" step="0.01" value="1"
             oninput="document.getElementById('w').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>X:</label>
      <button onclick="decField('x', 0.1)">-</button>
      <input type="number" id="x" value="0" step="0.1" onchange="updateQuat()">
      <button onclick="incField('x', 0.1)">+</button>
      <input type="range" id="xSlider" min="-1" max="1" step="0.01" value="0"
             oninput="document.getElementById('x').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Y:</label>
      <button onclick="decField('y', 0.1)">-</button>
      <input type="number" id="y" value="0" step="0.1" onchange="updateQuat()">
      <button onclick="incField('y', 0.1)">+</button>
      <input type="range" id="ySlider" min="-1" max="1" step="0.01" value="0"
             oninput="document.getElementById('y').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Z:</label>
      <button onclick="decField('z', 0.1)">-</button>
      <input type="number" id="z" value="0" step="0.1" onchange="updateQuat()">
      <button onclick="incField('z', 0.1)">+</button>
      <input type="range" id="zSlider" min="-1" max="1" step="0.01" value="0"
             oninput="document.getElementById('z').value=this.value; sliderUpdate();">
    </div>
    <div class="row">
      <label>Speed Multiplier (S):</label>
      <button onclick="decField('qS', 0.1)">-</button>
      <input type="number" id="qS" value="1" step="0.1" onchange="updateQuat()">
      <button onclick="incField('qS', 0.1)">+</button>
    </div>
    <div class="row">
      <label>Acceleration Multiplier (A):</label>
      <button onclick="decField('qA', 0.1)">-</button>
      <input type="number" id="qA" value="1" step="0.1" onchange="updateQuat()">
      <button onclick="incField('qA', 0.1)">+</button>
    </div>
    <div class="row">
      <input type="text" id="quatCmdInput" placeholder="e.g., Q:1,0,0,0,S1,A1">
      <button onclick="sendCommand(document.getElementById('quatCmdInput').value)">Send Command</button>
    </div>
    <p>Current Command: <span id="currentCmd"></span></p>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
quat_page = quat_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html)

# ---------- Additional Flask Endpoints ----------

@app.route("/available_ports")
def available_ports():
    """Return a JSON list of available serial port device names."""
    ports = [port.device for port in list_ports.comports()]
    return jsonify({"ports": ports})

@app.route("/do_connect_manual", methods=["POST"])
def do_connect_manual():
    """Attempt to open the selected serial port and return JSON indicating success."""
    data = request.get_json()
    port = data.get("port")
    global ser
    try:
        ser = serial.Serial(port, SERIAL_BAUD, timeout=1)
        connected = ser.isOpen()
        print(f"Connected to {port}")
    except Exception as e:
        print(f"Error connecting to port {port}: {e}")
        connected = False
    return jsonify({"connected": connected})

@app.route("/status")
def status():
    """Return JSON indicating whether the serial connection is open."""
    connected = (ser is not None) and ser.isOpen()
    return jsonify({"connected": connected})

@app.route("/send_command", methods=["POST"])
def send_command():
    """
    This endpoint accepts a JSON POST with a "command" string,
    appends a newline, writes it to the serial port, and returns the result.
    """
    data = request.get_json()
    command = data.get("command", "").strip() + "\n"
    if ser:
        with ser_lock:
            try:
                ser.write(command.encode("utf-8"))
                print("Sent:", command)
            except Exception as e:
                print("Serial write error:", e)
                return jsonify({"status": "error", "message": str(e)})
    else:
        print("Serial connection not available. Command:", command)
        return jsonify({"status": "error", "message": "Serial connection not available."})
    return jsonify({"status": "success", "command": command})

# ---------- Main Page Routes ----------
@app.route("/")
def index():
    return redirect(url_for("connect"))

@app.route("/connect")
def connect():
    return render_template_string(connect_page)

@app.route("/home")
def home():
    return render_template_string(home_page)

@app.route("/direct")
def direct():
    return render_template_string(direct_page)

@app.route("/euler")
def euler():
    return render_template_string(euler_page)

@app.route("/head")
def head():
    return render_template_string(head_page)

@app.route("/quaternion")
def quaternion():
    return render_template_string(quat_page)

# ---------- Run Flask App ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
