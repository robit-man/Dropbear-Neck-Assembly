#!/usr/bin/env python
"""
This is a self-contained Flask web application for controlling a Stewart platform (“neck”)
via serial commands. It supports multiple control modes:

  1. Direct Motor Control: Individual motor commands (e.g., "1:30,2:45,...").
  2. Euler Control: Control yaw (X), pitch (Y), roll (Z) and height (H) (e.g., "X30,Y15,Z-10,H50").
  3. Full Head Control: Control yaw (X), lateral translation (Y), front/back (Z),
     height (H), speed multiplier (S), acceleration multiplier (A), roll (R) and pitch (P)
     (e.g., "X30,Y0,Z10,H-40,S1,A1,R0,P0").
  4. Quaternion Control: Control orientation via quaternion (w, x, y, z) plus optional
     speed (S) and acceleration (A) multipliers, and a height value (e.g., "Q:1,0,0,0,H50,S1,A1").

Additionally, a "HOME" command is supported to re-home the platform.

Before any control pages are available the user is presented with a “Connect to Neck”
page where a serial port is selected.

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

from flask import Flask, render_template_string, request, jsonify, redirect, url_for
import serial
from serial.tools import list_ports
import threading
import time
import json
import math

# ---------- Serial Connection Setup ----------
# (User selects the port manually.)
SERIAL_BAUD = 115200
ser = None
ser_lock = threading.Lock()

# ---------- Global State Tracking ----------
# current_state stores the latest known values for each actuator (in mm) and head parameters.
# Actuator positions are stored as a list of 6 values.
current_state = {
    "actuators": [0, 0, 0, 0, 0, 0],  # in mm
    "X": 0,   # yaw
    "Y": 0,   # lateral translation
    "Z": 0,   # front/back translation
    "H": 0,   # height (mm)
    "S": 1,   # speed multiplier
    "A": 1,   # acceleration multiplier
    "R": 0,   # roll adjustment
    "P": 0    # pitch adjustment
}

# ---------- Flask Application Setup ----------
app = Flask(__name__)

# ---------- Base CSS and JavaScript (Dark Mode, Flexbox Layout) ----------
base_css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Exo:ital,wght@0,100..900;1,100..900&family=Monomaniac+One&family=Oxanium:wght@200..800&family=Roboto+Mono:ital,wght@0,100..70;1,100..70&display=swap');
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
/* Navigation styling using divs and flexbox */
nav {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0;
    margin: 0;
}
.nav-container {
    display: flex;
    flex-direction: row;
    gap: 0.5rem;
    align-items: center;
    width: 100%;
}
.nav-link {
    color: #FFFAFA;
    text-decoration: none;
    border: 1px solid #FFFAFA;
    padding: 0.5rem;
    border-radius: 0.25rem;
}
.nav-link:hover {
    background: #222;
}
.nav-button {
    background: none;
    border: 1px solid #FFFAFA;
    color: #FFFAFA;
    padding: 0.5rem;
    border-radius: 0.25rem;
    cursor: pointer;
    width:fit-content;
    margin-left: unset;
}
.nav-button:hover {
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
// Define PI for use in any quaternion math if needed.
const PI = Math.PI;

function logToConsole(msg) {
    var consoleEl = document.getElementById('console');
    if (consoleEl) {
        var newLine = document.createElement('div');
        newLine.textContent = msg;
        consoleEl.appendChild(newLine);
        consoleEl.scrollTop = consoleEl.scrollHeight;
    }
}

function resetSliders() {
    // Default values for various controls.
    let defaults = {
        'motor': 0,
        'yaw': 0,
        'pitch': 0,
        'roll': 0,
        'height': 0,
        'X': 0,
        'Y': 0,
        'Z': 0,
        'H': 0,
        'S': 1,
        'A': 1,
        'R': 0,
        'P': 0,
        'w': 1,
        'x': 0,
        'y': 0,
        'z': 0,
        'qH': 0,
        'qS': 1,
        'qA': 1
    };
    document.querySelectorAll("input[type='number'], input[type='range']").forEach(function(input) {
        for (let key in defaults) {
            if (input.id.startsWith(key)) {
                input.value = defaults[key];
                input.dispatchEvent(new Event('change'));
                break;
            }
        }
    });
    let currentCmdEl = document.getElementById('currentCmd');
    if(currentCmdEl) {
        currentCmdEl.textContent = "";
    }
}

function sendHomeCommand() {
    sendCommand("HOME");
    resetSliders();
    logToConsole("Sent HOME command");
}
</script>
"""

# Navigation block using only divs and flexbox.
nav_html = """
<nav>
  <div class="nav-container">
    <a href="/connect" class="nav-link">Connect</a>
    <a href="/home" class="nav-link">Home</a>
    <a href="/direct" class="nav-link">Direct Motor</a>
    <a href="/euler" class="nav-link">Euler</a>
    <a href="/head" class="nav-link">Full Head</a>
    <a href="/quaternion" class="nav-link">Quaternion</a>
    <a href="/headstream" class="nav-link">Morphtarget</a>
  </div>
      <button onclick="sendHomeCommand()" class="nav-button">HOME Command</button>

</nav>
"""

# ---------- Page Templates ----------
# 1. Connect Page
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
      <a id="proceedBtn" href="/home" style="display:none;"><button style="background:white;color:black;">Proceed to Control Interface</button></a>
    </div>
  </div>
  <footer id="console"></footer>
  <script>
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
connect_page = connect_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js);

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
home_page = home_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);

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
        <input type="range" id="slider{{ i }}" min="-800" max="800" value="0"
               oninput="document.getElementById('motor{{ i }}').value=this.value; updateDirect();">
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
direct_page = direct_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);

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
      // Retrieve yaw from its input
      let yaw = document.getElementById('yaw').value;
      // Now, note that the second row now provides the "roll" input—but we want its value to be used as the pitch (Y)
      let pitch = document.getElementById('roll').value;
      // And the third row now provides the "pitch" input—but we want its value to be used as the roll (Z)
      let roll = document.getElementById('pitch').value;
      let height = document.getElementById('height').value;
      // Construct the command as expected by the firmware: X (yaw), Y (pitch), Z (roll), H (height)
      let cmd = "X" + yaw + ",Y" + pitch + ",Z" + roll + ",H" + height;
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
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
      <input type="range" id="yawSlider" min="-800" max="800" value="0"
             oninput="document.getElementById('yaw').value=this.value; updateEuler();">
    </div>
    <!-- Swap pitch and roll: the second row now is for Roll (Y) (i.e. the pitch value) -->
    <div class="row">
      <label>Roll (Y):</label>
      <button onclick="decField('roll')">-</button>
      <input type="number" id="roll" value="0" onchange="updateEuler()">
      <button onclick="incField('roll')">+</button>
      <input type="range" id="rollSlider" min="-800" max="800" value="0"
             oninput="document.getElementById('roll').value=this.value; updateEuler();">
    </div>
    <!-- The third row now is for Pitch (Z) -->
    <div class="row">
      <label>Pitch (Z):</label>
      <button onclick="decField('pitch')">-</button>
      <input type="number" id="pitch" value="0" onchange="updateEuler()">
      <button onclick="incField('pitch')">+</button>
      <input type="range" id="pitchSlider" min="-800" max="800" value="0"
             oninput="document.getElementById('pitch').value=this.value; updateEuler();">
    </div>
    <div class="row">
      <label>Height (H):</label>
      <button onclick="decField('height')">-</button>
      <input type="number" id="height" value="0" onchange="updateEuler()">
      <button onclick="incField('height')">+</button>
      <input type="range" id="heightSlider" min="0" max="70" value="0"
             oninput="document.getElementById('height').value=this.value; updateEuler();">
    </div>
    <div class="row">
      <input type="text" id="eulerCmdInput" placeholder="e.g., X30,Y15,Z-10,H50">
      <button onclick="sendCommand(document.getElementById('eulerCmdInput').value)">Send Command</button>
    </div>
    <p>Current Command: <span id="currentCmd"></span></p>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
euler_page = euler_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);


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
    function incField(field, step) {
      let el = document.getElementById(field);
      if(field === 'S' || field === 'A'){
        el.value = parseFloat(el.value) + step;
      } else {
        el.value = parseInt(el.value) + step;
      }
      updateHead();
    }
    function decField(field, step) {
      let el = document.getElementById(field);
      if(field === 'S' || field === 'A'){
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
      <input type="range" id="XSlider" min="-800" max="800" value="0"
             oninput="document.getElementById('X').value=this.value; updateHead();">
    </div>
    <div class="row">
      <label>Lateral (Y):</label>
      <button onclick="decField('Y', 1)">-</button>
      <input type="number" id="Y" value="0" onchange="updateHead()">
      <button onclick="incField('Y', 1)">+</button>
      <input type="range" id="YSlider" min="-800" max="800" value="0"
             oninput="document.getElementById('Y').value=this.value; updateHead();">
    </div>
    <div class="row">
      <label>Front/Back (Z):</label>
      <button onclick="decField('Z', 1)">-</button>
      <input type="number" id="Z" value="0" onchange="updateHead()">
      <button onclick="incField('Z', 1)">+</button>
      <input type="range" id="ZSlider" min="-800" max="800" value="0"
             oninput="document.getElementById('Z').value=this.value; updateHead();">
    </div>
    <div class="row">
      <label>Height (H):</label>
      <button onclick="decField('H', 1)">-</button>
      <input type="number" id="H" value="0" onchange="updateHead()">
      <button onclick="incField('H', 1)">+</button>
      <input type="range" id="HSlider" min="0" max="70" value="0"
             oninput="document.getElementById('H').value=this.value; updateHead();">
    </div>
    <div class="row">
      <label>Speed Multiplier (S):</label>
      <button onclick="decField('S', 0.1)">-</button>
      <input type="number" id="S" value="1" step="0.1" onchange="updateHead()">
      <button onclick="incField('S', 0.1)">+</button>
      <input type="range" id="SSlider" min="0" max="10" value="1"
             oninput="document.getElementById('S').value=this.value; updateHead();">
    </div>
    <div class="row">
      <label>Acceleration Multiplier (A):</label>
      <button onclick="decField('A', 0.1)">-</button>
      <input type="number" id="A" value="1" step="0.1" onchange="updateHead()">
      <button onclick="incField('A', 0.1)">+</button>
      <input type="range" id="ASlider" min="0" max="10" value="1"
             oninput="document.getElementById('A').value=this.value; updateHead();">
    </div>
    <div class="row">
      <label>Roll (R):</label>
      <button onclick="decField('R', 1)">-</button>
      <input type="number" id="R" value="0" onchange="updateHead()">
      <button onclick="incField('R', 1)">+</button>
      <input type="range" id="RSlider" min="-800" max="800" value="0"
             oninput="document.getElementById('R').value=this.value; updateHead();">
    </div>
    <div class="row">
      <label>Pitch (P):</label>
      <button onclick="decField('P', 1)">-</button>
      <input type="number" id="P" value="0" onchange="updateHead()">
      <button onclick="incField('P', 1)">+</button>
      <input type="range" id="PSlider" min="-800" max="800" value="0"
             oninput="document.getElementById('P').value=this.value; updateHead();">
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
head_page = head_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);

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
      let h = document.getElementById('qH').value;
      let S = document.getElementById('qS').value;
      let A = document.getElementById('qA').value;
      // Compose command with height included.
      let cmd = "Q:" + w + "," + x + "," + y + "," + z + ",H" + h;
      if (S !== "") { cmd += ",S" + S; }
      if (A !== "") { cmd += ",A" + A; }
      document.getElementById('currentCmd').textContent = cmd;
      sendCommand(cmd);
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
             oninput="document.getElementById('w').value=this.value; updateQuat();">
    </div>
    <div class="row">
      <label>X:</label>
      <button onclick="decField('x', 0.1)">-</button>
      <input type="number" id="x" value="0" step="0.1" onchange="updateQuat()">
      <button onclick="incField('x', 0.1)">+</button>
      <input type="range" id="xSlider" min="-800" max="800" step="0.01" value="0"
             oninput="document.getElementById('x').value=this.value; updateQuat();">
    </div>
    <div class="row">
      <label>Y:</label>
      <button onclick="decField('y', 0.1)">-</button>
      <input type="number" id="y" value="0" step="0.1" onchange="updateQuat()">
      <button onclick="incField('y', 0.1)">+</button>
      <input type="range" id="ySlider" min="-800" max="800" step="0.01" value="0"
             oninput="document.getElementById('y').value=this.value; updateQuat();">
    </div>
    <div class="row">
      <label>Z:</label>
      <button onclick="decField('z', 0.1)">-</button>
      <input type="number" id="z" value="0" step="0.1" onchange="updateQuat()">
      <button onclick="incField('z', 0.1)">+</button>
      <input type="range" id="zSlider" min="-800" max="800" step="0.01" value="0"
             oninput="document.getElementById('z').value=this.value; updateQuat();">
    </div>
    <div class="row">
      <label>Height (H):</label>
      <button onclick="decField('qH', 1)">-</button>
      <input type="number" id="qH" value="0" onchange="updateQuat()">
      <button onclick="incField('qH', 1)">+</button>
      <input type="range" id="qHSlider" min="0" max="70" value="0"
             oninput="document.getElementById('qH').value=this.value; updateQuat();">
    </div>
    <div class="row">
      <label>Speed Multiplier (S):</label>
      <button onclick="decField('qS', 0.1)">-</button>
      <input type="number" id="qS" value="1" step="0.1" onchange="updateQuat()">
      <button onclick="incField('qS', 0.1)">+</button>
      <input type="range" id="qSSlider" min="-800" max="800" value="1"
             oninput="document.getElementById('qS').value=this.value; updateQuat();">
    </div>
    <div class="row">
      <label>Acceleration Multiplier (A):</label>
      <button onclick="decField('qA', 0.1)">-</button>
      <input type="number" id="qA" value="1" step="0.1" onchange="updateQuat()">
      <button onclick="incField('qA', 0.1)">+</button>
      <input type="range" id="qASlider" min="-800" max="800" value="1"
             oninput="document.getElementById('qA').value=this.value; updateQuat();">
    </div>
    <div class="row">
      <input type="text" id="quatCmdInput" placeholder="e.g., Q:1,0,0,0,H50,S1,A1">
      <button onclick="sendCommand(document.getElementById('quatCmdInput').value)">Send Command</button>
    </div>
    <p>Current Command: <span id="currentCmd"></span></p>
  </div>
  <footer id="console"></footer>
</body>
</html>
"""
quat_page = quat_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);

headstream_page = r"""
<!DOCTYPE html>
<html lang="en">
  <head>
    <title>Head Pose Command Stream</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <!-- Import fonts and basic styles -->
    %%CSS%%
    %%JS%%
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Roboto+Mono&display=swap');
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
        padding: 1rem;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
      }
      #commandStream {
        border: 1px solid #FFFAFA;
        padding: 0.5rem;
      }
      #canvasContainer {
        position: fixed;
        left: 0;
        top: 0;
        width: 100%;
        height: 480px;
        z-index: -2;
      }
      canvas {
        width: 100%;
        height: 100%;
        display: block;
        filter:saturate(0)brightness(0.8)contrast(1)invert(0);
      }
    </style>
    <!-- Importmap for three.js and its addons from CDN -->
    <script type="importmap">
    {
      "imports": {
        "three": "https://unpkg.com/three@0.152.2/build/three.module.js",
        "three/addons/": "https://unpkg.com/three@0.152.2/examples/jsm/"
      }
    }
    </script>
  </head>
  <body>
    <div class="container">
      %%NAV%%
      <h1>Head Pose Command Stream</h1>
      <div id="commandStream">Waiting for head pose...</div>
      <div id="canvasContainer"></div>
    </div>
    <script type="module">
      import * as THREE from 'three';
      import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
      import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
      import { KTX2Loader } from 'three/addons/loaders/KTX2Loader.js';
      import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
      
      // Import MediaPipe tasks-vision from CDN
      import vision from 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.0';
      const { FaceLandmarker, FilesetResolver } = vision;
      
      // Setup renderer, scene, and camera (matching the example)
      const renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setPixelRatio(window.devicePixelRatio);
      renderer.setSize(window.innerWidth, window.innerHeight);
      document.getElementById('canvasContainer').appendChild(renderer.domElement);
      
      // Do not flip the entire scene—only mirror the video texture.
      const scene = new THREE.Scene();
      
      // Camera: 60° fov, near=1, far=100, positioned at z=5 (per example)
      const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 1, 100);
      camera.position.z = 3.8;
      
      const controls = new OrbitControls(camera, renderer.domElement);
      // to disable zoom
      controls.enableZoom = false;

      // to disable rotation
      controls.enableRotate = false;

      // to disable pan
      controls.enablePan = false;

      // Create a group to hold the face model and receive transformation updates from MediaPipe
      const grpTransform = new THREE.Group();
      grpTransform.name = 'grp_transform';
      scene.add(grpTransform);
      
      // Create a video element for the webcam stream
      const video = document.createElement('video');
      video.autoplay = true;
      video.playsInline = true;
      navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user' } })
        .then(stream => {
          video.srcObject = stream;
          video.play();
        })
        .catch(err => console.error('Camera error:', err));
      
      // Create a video texture from the webcam and add it as a plane.
      // The plane is defined as 1×1 and will be scaled each frame to preserve the aspect ratio.
      const videoTexture = new THREE.VideoTexture(video);
      const videoMaterial = new THREE.MeshBasicMaterial({ map: videoTexture, depthWrite: false });
      const videoGeometry = new THREE.PlaneGeometry(1, 1);
      const videoMesh = new THREE.Mesh(videoGeometry, videoMaterial);
      // Mirror the video texture by flipping its X scale
      videoMesh.scale.x = -1;
      //scene.add(videoMesh);
      
      // Load the Face Cap model (facecap.glb) from the official three.js examples CDN.
      // In the original example, the face mesh is the first child,
      // and the head (named "mesh_2") along with the eyes ("eyeLeft" and "eyeRight") are extracted.
      let face = null, eyeL = null, eyeR = null;
      const gltfLoader = new GLTFLoader();
      const ktx2Loader = new KTX2Loader()
          .setTranscoderPath('https://unpkg.com/three@0.152.2/examples/jsm/libs/basis/')
          .detectSupport(renderer);
      gltfLoader.setKTX2Loader(ktx2Loader);
      gltfLoader.setMeshoptDecoder(MeshoptDecoder);
      gltfLoader.load(
        'https://threejs.org/examples/models/gltf/facecap.glb',
        (gltf) => {
          const mesh = gltf.scene.children[0];
          // Attach the loaded mesh to our transformation group.
          grpTransform.add(mesh);
          const headMesh = mesh.getObjectByName('mesh_2');
          headMesh.material = new THREE.MeshNormalMaterial();
          face = headMesh;
          eyeL = mesh.getObjectByName('eyeLeft');
          eyeR = mesh.getObjectByName('eyeRight');
        },
        undefined,
        (error) => { console.error('Error loading facecap model:', error); }
      );
      
      // Setup MediaPipe FaceLandmarker
      const filesetResolver = await FilesetResolver.forVisionTasks(
        'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.0/wasm'
      );
      const faceLandmarker = await FaceLandmarker.createFromOptions(filesetResolver, {
        baseOptions: {
          modelAssetPath: 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task',
          delegate: 'GPU'
        },
        runningMode: 'VIDEO',
        numFaces: 1,
        outputFaceBlendshapes: true,
        outputFacialTransformationMatrixes: true
      });
      
      // Function to send head-pose commands (if needed)
      function sendCommandToNeck(commandStr) {
        fetch('/send_command', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ command: commandStr })
        })
        .then(response => response.json())
        .then(data => { console.log("Neck response:", data); })
        .catch(err => console.error("Error sending command:", err));
      }
      
      // Create an Object3D to hold the MediaPipe transform
      const transformObj = new THREE.Object3D();
      
      function animate() {
        requestAnimationFrame(animate);
        
        if (video.readyState >= video.HAVE_ENOUGH_DATA) {
          const now = Date.now();
          const results = faceLandmarker.detectForVideo(video, now);
          if (results.facialTransformationMatrixes.length > 0) {
            const matrixArray = results.facialTransformationMatrixes[0].data;
            transformObj.matrix.fromArray(matrixArray);
            transformObj.matrix.decompose(transformObj.position, transformObj.quaternion, transformObj.scale);
            // Convert the quaternion to Euler angles (using YXZ order)
            const euler = new THREE.Euler().setFromQuaternion(transformObj.quaternion, 'YXZ');
            const grp = scene.getObjectByName('grp_transform');
            // Updated GLB transformation: swap the front/back and up/down.
            // Previously, it was:
            //   grp.position.y = transformObj.position.z / 10 + 4;
            //   grp.position.z = - transformObj.position.y / 10;
            // Now, we swap these:
            grp.position.x = transformObj.position.x / 10;
            grp.position.y = transformObj.position.y / 10;  // Up/down now comes from position.y
            grp.position.z = - transformObj.position.z / -10 + 4;     // Front/back now comes from position.z
            grp.rotation.x = euler.x;
            grp.rotation.y = euler.y;
            grp.rotation.z = euler.z;
            
            // Generate and send the head-pose command string (unchanged multipliers)
            const lateral = -transformObj.position.x;
            const height = transformObj.position.y + 35;
            const frontBack = (-transformObj.position.z - 50) * 1.5;
            const yaw = THREE.MathUtils.radToDeg(euler.y);
            const pitch = THREE.MathUtils.radToDeg(euler.x);
            const roll = THREE.MathUtils.radToDeg(euler.z);
            const yawM = yaw * 15;
            const lateralM = lateral * 20;
            const frontBackM = frontBack * 10;
            const rollM = roll * -20;
            const pitchM = pitch * -20;
            const commandStr = "X" + yawM.toFixed(1) +
                               ",Y" + lateralM.toFixed(1) +
                               ",Z" + frontBackM.toFixed(1) +
                               ",H" + height.toFixed(1) +
                               ",S2,A5" +
                               ",R" + rollM.toFixed(1) +
                               ",P" + pitchM.toFixed(1);
            document.getElementById('commandStream').textContent = commandStr;
            sendCommandToNeck(commandStr);
          }
          
          // Update the video mesh scale based on the actual video dimensions (to preserve aspect ratio)
          if (video.videoWidth && video.videoHeight) {
            // Ensure the X scale remains negative (to mirror the video)
            videoMesh.scale.x = (video.videoWidth / 100);
            videoMesh.scale.y = video.videoHeight / 100;
          }
        }
        
        controls.update();
        renderer.render(scene, camera);
      }
      
      animate();
      
      window.addEventListener('resize', () => {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
      });
    </script>
  </body>
</html>
"""
headstream_page = headstream_page.replace("%%CSS%%", base_css).replace("%%JS%%", base_js).replace("%%NAV%%", nav_html);




# ---------- Additional Flask Endpoints ----------

@app.route("/available_ports")
def available_ports():
    ports = [port.device for port in list_ports.comports()]
    return jsonify({"ports": ports})

@app.route("/do_connect_manual", methods=["POST"])
def do_connect_manual():
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
    connected = (ser is not None) and ser.isOpen()
    return jsonify({"connected": connected})

# New endpoint to retrieve the current state.
@app.route("/get_state")
def get_state():
    return jsonify(current_state)

@app.route("/send_command", methods=["POST"])
def send_command():
    data = request.get_json()
    command = data.get("command", "").strip() + "\n"
    # Update our global state based on the command before sending.
    update_state(command.strip())
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
    return jsonify({"status": "success", "command": command, "state": current_state})

# ---------- Global State Update Function ----------
def update_state(cmd):
    """
    Updates the global current_state dictionary based on the command string.
    This function mimics the parsing performed by the firmware.
    """
    global current_state
    cmd = cmd.strip()
    if not cmd:
        return
    # Homing command: if cmd equals "HOME", reset state.
    if cmd.lower() == "home":
        current_state["X"] = 0
        current_state["Y"] = 0
        current_state["Z"] = 0
        current_state["H"] = 0
        current_state["S"] = 1
        current_state["A"] = 1
        current_state["R"] = 0
        current_state["P"] = 0
        current_state["actuators"] = [0, 0, 0, 0, 0, 0]
        return
    # If command starts with "Q", process as quaternion.
    if cmd.startswith("Q"):
        body = cmd[1:].strip()
        if body.startswith(":"):
            body = body[1:].strip()
        tokens = body.split(',')
        if len(tokens) < 4:
            print("Invalid quaternion command: not enough parameters.")
            return
        try:
            q = [float(tokens[i]) for i in range(4)]
        except:
            return
        speedMult = 1.0
        accelMult = 1.0
        heightVal = 0
        for token in tokens[4:]:
            token = token.strip()
            if token.startswith("S"):
                speedMult = float(token[1:])
            elif token.startswith("A"):
                accelMult = float(token[1:])
            elif token.startswith("H"):
                heightVal = int(token[1:])
        norm = math.sqrt(q[0]*q[0] + q[1]*q[1] + q[2]*q[2] + q[3]*q[3])
        if norm > 0:
            q = [x/norm for x in q]
        else:
            print("Invalid quaternion: norm is zero.")
            return
        yaw_rad = math.atan2(2.0*(q[0]*q[3] + q[1]*q[2]), 1.0 - 2.0*(q[2]*q[2] + q[3]*q[3]))
        pitch_rad = math.asin(2.0*(q[0]*q[2] - q[3]*q[1]))
        roll_rad = math.atan2(2.0*(q[0]*q[1] + q[2]*q[3]), 1.0 - 2.0*(q[1]*q[1] + q[2]*q[2]))
        current_state["X"] = round(yaw_rad * (180.0 / math.pi))
        current_state["Y"] = round(pitch_rad * (180.0 / math.pi))
        current_state["Z"] = round(roll_rad * (180.0 / math.pi))
        current_state["H"] = heightVal
        current_state["S"] = speedMult
        current_state["A"] = accelMult
        return
    # If command contains a colon, assume direct motor control.
    if ":" in cmd:
        tokens = cmd.split(',')
        for token in tokens:
            token = token.strip()
            if ":" in token:
                parts = token.split(":")
                if len(parts) >= 2:
                    try:
                        idx = int(parts[0])
                        pos_mm = float(parts[1])
                        if pos_mm < 0: pos_mm = 0
                        if pos_mm > 70: pos_mm = 70
                        current_state["actuators"][idx-1] = pos_mm
                    except:
                        pass
        return
    # Otherwise, assume a general head movement command.
    tokens = cmd.split(',')
    for token in tokens:
        token = token.strip()
        if len(token) < 2:
            continue
        axis = token[0]
        try:
            val = float(token[1:])
        except:
            continue
        if axis == 'X':
            current_state["X"] = val
        elif axis == 'Y':
            current_state["Y"] = val
        elif axis == 'Z':
            current_state["Z"] = val
        elif axis == 'H':
            if val < 0: val = 0
            if val > 70: val = 70
            current_state["H"] = val
        elif axis == 'S':
            current_state["S"] = val
        elif axis == 'A':
            current_state["A"] = val
        elif axis == 'R':
            current_state["R"] = val
        elif axis == 'P':
            current_state["P"] = val

# ---------- Main Page Routes ----------

@app.route("/")
def index():
    return redirect(url_for("connect"))

@app.route("/connect")
def connect():
    return render_template_string(connect_page)

@app.route("/headstream")
def headstream():
    return render_template_string(headstream_page)

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
