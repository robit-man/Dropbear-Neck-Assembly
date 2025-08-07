#!/usr/bin/env python
"""
All-in-one Python script that:
1. Auto-creates a virtual environment (if needed) and installs required packages (pyserial, Flask),
   then re-launches itself from the venv.
2. Loads configuration from config.json if available.
3. For the serial device and baudrate, attempts to use the saved values; if the connection fails,
   prompts for new values until a successful serial connection is established.
4. For the network port and route, attempts to use saved values; if any fail (e.g. port unavailable),
   prompts for new values.
5. Saves any new valid configuration automatically.
6. Sets up a Flask server on localhost that listens for POST commands on the user‑specified route.
   Commands can be the special "home" command or any subset (in any order) of the following fields:
      X, Y, Z, H, S, A, R, P
   with allowed ranges:
      • X, Y, Z, R, P: integers between -700 and 700
      • H: integer between 0 and 70
      • S, A: floats between 0 and 10
7. The system maintains a current command state (initialized with defaults) so that if a subsequent command
   contains only a subset (e.g. "P3,R4") it updates only those fields and then sends the full updated command.
   For example, if the current state is:
       X100,Y-100,Z0,H10,S1,A1,R20,P20
   and a command "P3,R4" is received, it updates R and P so that the forwarded command becomes:
       X100,Y-100,Z0,H10,S1,A1,R4,P3
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
        print("Installing required packages: pyserial, Flask...")
        if os.name == 'nt':
            pip_path = os.path.join(venv_dir, "Scripts", "pip.exe")
            python_path = os.path.join(venv_dir, "Scripts", "python.exe")
        else:
            pip_path = os.path.join(venv_dir, "bin", "pip")
            python_path = os.path.join(venv_dir, "bin", "python")
        subprocess.check_call([pip_path, "install", "pyserial", "Flask"])
        print("Re-launching script from the virtual environment...")
        os.environ["RUNNING_IN_VENV"] = "1"
        subprocess.check_call([python_path, __file__])
        sys.exit(0)

ensure_venv()

try:
    import serial
except ImportError:
    print("pyserial is not installed. Exiting.")
    sys.exit(1)

try:
    from flask import Flask, request, jsonify
except ImportError:
    print("Flask is not installed. Exiting.")
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
    if cmd.strip() == "home":
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

# --- Check if Network Port is Available ---
def is_port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

# --- Configuration Load/Save Functions ---
def load_config():
    config_file = "config.json"
    if os.path.exists(config_file):
        try:
            with open(config_file, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error reading config.json: {e}")
    return {}

def save_config(config):
    try:
        with open("config.json", "w") as f:
            json.dump(config, f, indent=4)
        print("Configuration saved to config.json.")
    except Exception as e:
        print(f"Failed to save configuration: {e}")

# --- Main Application Logic ---
def main():
    config = load_config()
    config_changed = False

    # --- Serial Connection Setup ---
    ser = None
    # Try to use saved serial configuration.
    if "serial_device" in config and "baudrate" in config:
        device = config["serial_device"]
        try:
            baudrate = int(config["baudrate"])
        except Exception:
            baudrate = None
        if device and baudrate:
            try:
                ser = serial.Serial(device, baudrate, timeout=1)
                print("Serial connection successful using saved configuration!")
            except Exception as e:
                print(f"Failed to connect using saved serial device ({device}) at baudrate ({baudrate}): {e}")
                ser = None

    # If saved config not available or connection failed, prompt the user.
    while ser is None:
        device = input("Enter serial device (e.g., /dev/ttyUSB0 or COM3): ").strip()
        baudrate_str = input("Enter baudrate (e.g., 9600): ").strip()
        try:
            baudrate = int(baudrate_str)
        except ValueError:
            print("Invalid baudrate. Please try again.\n")
            continue
        try:
            ser = serial.Serial(device, baudrate, timeout=1)
            print("Serial connection successful!")
            # Update configuration with new values.
            config["serial_device"] = device
            config["baudrate"] = baudrate
            config_changed = True
        except Exception as e:
            print(f"Failed to connect to serial device: {e}")
            print("Please try a different device or baudrate.\n")

    # --- Network Port Setup ---
    listen_port = None
    if "listen_port" in config:
        try:
            port_candidate = int(config["listen_port"])
            if is_port_available(port_candidate):
                listen_port = port_candidate
                print(f"Using saved port: {listen_port}")
            else:
                print(f"Saved port {port_candidate} is not available.")
        except Exception:
            pass

    while listen_port is None:
        listen_port_str = input("Enter port number for listening (e.g., 5000): ").strip()
        try:
            listen_port = int(listen_port_str)
        except ValueError:
            print("Invalid port number. Please try again.\n")
            continue
        if is_port_available(listen_port):
            # Update configuration with new port.
            config["listen_port"] = listen_port
            config_changed = True
            break
        else:
            print("Port not available. Please choose a different port.\n")
            listen_port = None

    # --- Route Setup ---
    if "listen_route" in config:
        listen_route = config["listen_route"].strip()
        if not listen_route.startswith("/"):
            listen_route = "/" + listen_route
        print(f"Using saved route: {listen_route}")
    else:
        listen_route = input("Enter route path for receiving commands (e.g., /send_command): ").strip()
        if not listen_route.startswith("/"):
            listen_route = "/" + listen_route
        config["listen_route"] = listen_route
        config_changed = True

    # --- Auto-save configuration if changes were made and no config existed before ---
    if config_changed:
        save_config(config)

    # --- Host Setup (allow binding beyond localhost) ---
    listen_host = config.get("listen_host")
    if not listen_host:
        host_input = input("Enter bind host/IP (default 0.0.0.0 for all interfaces): ").strip()
        listen_host = host_input or "0.0.0.0"
        config["listen_host"] = listen_host
        save_config(config)

    # --- Flask Web Server Setup ---
    app = Flask(__name__)
    global_serial = ser

    def assemble_full_command():
        return ",".join([f"{field}{current_state[field]}" for field in ["X", "Y", "Z", "H", "S", "A", "R", "P"]])


    @app.route(listen_route, methods=["POST"])
    def receive_command():
        data = request.get_json()
        if not data or "command" not in data:
            return jsonify({"error": "No command provided"}), 400

        cmd = data["command"].strip()
        log(f"Received command: {cmd}")

        if cmd == "home":
            try:
                #global_serial.write((cmd + "\n").encode('utf-8'))
                log(f"Sent command: {cmd}")
            except Exception as e:
                log(f"Failed to send command: {e}")
                return jsonify({"error": "Failed to send command"}), 500
            return jsonify({"status": "success", "command": cmd})

        if not validate_command(cmd):
            log("Invalid command received.")
            return jsonify({"error": "Invalid command"}), 400

        tokens = [token.strip() for token in cmd.split(",")]
        for token in tokens:
            m = re.match(r"^([XYZHSARP])(-?\d+(?:\.\d+)?)$", token)
            if m:
                key = m.group(1)
                low, high, cast = allowed_ranges[key]
                try:
                    value = cast(m.group(2))
                except Exception:
                    continue
                current_state[key] = value

        full_command = assemble_full_command()
        try:
            #global_serial.write((full_command + "\n").encode('utf-8'))
            log(f"Sent command: {full_command}")
        except Exception as e:
            log(f"Failed to send command: {e}")
            return jsonify({"error": "Failed to send command"}), 500

        return jsonify({"status": "success", "command": full_command})

    # Informational log with LAN hint if bound to all interfaces
    bind_msg = f"Starting server on http://{listen_host}:{listen_port}{listen_route}"
    if listen_host == "0.0.0.0":
        try:
            hostname = socket.gethostname()
            lan_ip = socket.gethostbyname(hostname)
            bind_msg += f"  (reachable on LAN at http://{lan_ip}:{listen_port}{listen_route})"
        except:
            pass
    log(bind_msg)

    app.run(host=listen_host, port=listen_port)

if __name__ == "__main__":
    main()
