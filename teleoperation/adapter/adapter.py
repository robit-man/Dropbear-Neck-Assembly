#!/usr/bin/env python
"""
All-in-one Python script that:
1. Auto-creates a virtual environment (if needed) and installs required packages (pyserial, Flask, Flask-SocketIO),
   then re-launches itself from the venv.
2. Loads configuration from config.json if available.
3. For the serial device and baudrate, attempts to use the saved values; if the connection fails,
   prompts for new values until a successful serial connection is established.
4. For the network port, route and host, attempts to use saved values; if any fail (e.g. port unavailable),
   prompts for new values.
5. Saves any new valid configuration automatically.
6. Sets up a Flask server + WebSocket endpoint on your chosen host:port, listening for:
     • HTTP POSTs at your chosen route (default `/send_command`)
     • raw WS messages at the same URL path
   Commands can be "home" or any subset of fields X,Y,Z,H,S,A,R,P.
7. Maintains a running current_state so partial updates merge into a full command string.
"""

import os
import sys
import subprocess
import json
import datetime
import re
import socket

# --- Virtual Environment Setup ---
def ensure_venv():
    if (hasattr(sys, 'real_prefix') or sys.base_prefix != sys.prefix) or os.environ.get("RUNNING_IN_VENV") == "1":
        return

    venv_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "venv")
    if not os.path.exists(venv_dir):
        print("Creating virtual environment in 'venv' directory...")
        import venv
        venv.create(venv_dir, with_pip=True)
    if os.name == 'nt':
        pip_path = os.path.join(venv_dir, "Scripts", "pip.exe")
        python_path = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        pip_path = os.path.join(venv_dir, "bin", "pip")
        python_path = os.path.join(venv_dir, "bin", "python")
    print("Installing required packages: pyserial, Flask, Flask-SocketIO...")
    subprocess.check_call([pip_path, "install", "pyserial", "Flask", "Flask-SocketIO"])
    print("Re-launching script from the virtual environment...")
    os.environ["RUNNING_IN_VENV"] = "1"
    os.execv(python_path, [python_path] + sys.argv)

ensure_venv()

try:
    import serial
except ImportError:
    print("pyserial is not installed. Exiting.")
    sys.exit(1)

try:
    from flask import Flask, request, jsonify
    from flask_socketio import SocketIO, send as ws_send
except ImportError:
    print("Flask or Flask-SocketIO is not installed. Exiting.")
    sys.exit(1)

# --- Allowed Ranges for Fields ---
allowed_ranges = {
    "X": (-700, 700, int),
    "Y": (-700, 700, int),
    "Z": (-700, 700, int),
    "H": (0, 70, int),
    "S": (0, 10, float),
    "A": (0, 10, float),
    "R": (-700, 700, int),
    "P": (-700, 700, int)
}

# --- Default Current State ---
current_state = {
    "X": 0,
    "Y": 0,
    "Z": 0,
    "H": 0,
    "S": 1.0,
    "A": 1.0,
    "R": 0,
    "P": 0
}

# --- Utility Logging Function ---
def log(message):
    print(f"[{datetime.datetime.now()}] {message}")

# --- Command Validation Function ---
def validate_command(cmd):
    if cmd.strip().lower() == "home":
        return True
    tokens = cmd.split(",")
    seen_keys = set()
    for token in tokens:
        token = token.strip()
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", token)
        if not m:
            return False
        key = m.group(1)
        if key in seen_keys:
            return False
        seen_keys.add(key)
        low, high, cast = allowed_ranges[key]
        try:
            value = cast(m.group(2))
        except Exception:
            return False
        if not (low <= value <= high):
            return False
    return True

# --- Merge Partial Commands into current_state ---
def merge_into_state(cmd):
    if cmd.strip().lower() == "home":
        for k in current_state:
            current_state[k] = 0 if k not in ("S","A") else 1.0
        return
    for token in cmd.split(","):
        m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", token.strip())
        if m:
            key, raw = m.group(1), m.group(2)
            low, high, cast = allowed_ranges[key]
            val = cast(raw)
            current_state[key] = val

def assemble_full_command():
    return ",".join(f"{k}{current_state[k]}" for k in ["X","Y","Z","H","S","A","R","P"])

# --- Check if Network Port is Available ---
def is_port_available(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False

# --- Configuration Load/Save Functions ---
def load_config():
    config_file = "config.json"
    if os.path.exists(config_file):
        try:
            return json.load(open(config_file, "r"))
        except Exception as e:
            print(f"Error reading config.json: {e}")
    return {}

def save_config(config):
    try:
        json.dump(config, open("config.json", "w"), indent=4)
        print("Configuration saved to config.json.")
    except Exception as e:
        print(f"Failed to save configuration: {e}")

# --- Process a single command string ---
def process_command(cmd, ser):
    log(f"Received command: {cmd}")
    if not validate_command(cmd):
        return {"status":"error","message":"Invalid command"}
    merge_into_state(cmd)
    full = assemble_full_command()
    try:
        ser.write((full + "\n").encode('utf-8'))
        log(f"Sent command: {full}")
        return {"status":"success","command":full}
    except Exception as e:
        log(f"Serial write failed: {e}")
        return {"status":"error","message":str(e)}

# --- Main Application Logic ---
def main():
    config = load_config()
    config_changed = False

    # --- Serial Connection Setup ---
    ser = None
    if "serial_device" in config and "baudrate" in config:
        try:
            ser = serial.Serial(config["serial_device"],
                                int(config["baudrate"]), timeout=1)
            print("Serial connection successful using saved configuration!")
        except Exception as e:
            print(f"Failed serial on saved config: {e}")
            ser = None

    while ser is None:
        device = input("Enter serial device (e.g., /dev/ttyUSB0 or COM3): ").strip()
        baud_str = input("Enter baudrate (e.g., 9600): ").strip()
        try:
            baud = int(baud_str)
            ser = serial.Serial(device, baud, timeout=1)
            print("Serial connection successful!")
            config.update(serial_device=device, baudrate=baud)
            config_changed = True
        except Exception as e:
            print(f"Serial connect error: {e}. Try again.")

    # --- Network Port Setup ---
    listen_host = config.get("listen_host", "0.0.0.0")
    listen_port = None
    if "listen_port" in config:
        try:
            p = int(config["listen_port"])
            if is_port_available(p, listen_host):
                listen_port = p
                print(f"Using saved port: {p}")
            else:
                print(f"Saved port {p} unavailable.")
        except:
            pass

    while listen_port is None:
        p_str = input("Enter port number for listening (e.g., 5000): ").strip()
        try:
            p = int(p_str)
            if is_port_available(p, listen_host):
                listen_port = p
                config["listen_port"] = p
                config_changed = True
            else:
                print("Port not available.")
        except:
            print("Invalid port.")

    # --- Route Setup ---
    if "listen_route" in config:
        listen_route = config["listen_route"]
    else:
        listen_route = input("Enter route path (e.g., /send_command): ").strip()
        if not listen_route.startswith("/"):
            listen_route = "/" + listen_route
        config["listen_route"] = listen_route
        config_changed = True

    # --- Host Save ---
    if "listen_host" not in config:
        config["listen_host"] = listen_host
        config_changed = True

    # --- Auto-save if changed ---
    if config_changed:
        save_config(config)

    # --- Flask + SocketIO Setup ---
    app = Flask(__name__)
    socketio = SocketIO(app, cors_allowed_origins="*")

    @app.route(listen_route, methods=["POST"])
    def http_receive():
        data = request.get_json() or {}
        cmd = data.get("command", "").strip()
        result = process_command(cmd, ser)
        return jsonify(result)

    @socketio.on('message')
    def ws_receive(msg):
        """Receive raw text over WS"""
        result = process_command(msg.strip(), ser)
        ws_send(json.dumps(result))

    # --- Startup Logging ---
    hint = f"http://{listen_host}:{listen_port}{listen_route}"
    if listen_host == "0.0.0.0":
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
            hint += f"  (LAN at http://{lan_ip}:{listen_port}{listen_route})"
        except:
            pass
    log(f"Starting server on {hint}")

    # --- Run Server ---
    socketio.run(app, host=listen_host, port=listen_port)

if __name__ == "__main__":
    main()
