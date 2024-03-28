## This script is built to move the pitch and yaw of the neck based on object recognition key points in a parallel script. 
## this can be used as an example, and you can implement your own control by modifying the move() function to accept as many inouts as you want.
## Refer to the readme for accepted serial commands: command = f"H30,X0,P0,S1,A1" ~ moves all actuators up 30mm
import serial
import time
import bluetooth

# Stream dimensions
streamWidth = 740
streamHeight = 1280

# Center of the stream
center_x = streamWidth // 2
center_y = streamHeight // 2

# Delta thresholds
delta_threshold_x = 5
delta_threshold_y = 5

# Maximum and minimum delta values
max_delta = 700
min_delta = -700

# Try to connect via Bluetooth
def bluetooth_connect(device_address, port):
    try:
        sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
        sock.connect((device_address, port))
        return sock
    except bluetooth.BluetoothError as e:
        print(f"Bluetooth connection failed: {e}")
        return None

# Open serial connection
def serial_connect(port, baudrate):
    return serial.Serial(port, baudrate)

device_address = "7C:9E:BD:F0:92:A4"
port = 1
ser = None

# Attempt Bluetooth connection
sock = bluetooth_connect(device_address, port)
if sock:
    print("Connected via Bluetooth")
    ser = sock
else:
    print("Falling back to serial connection")
    ser = serial_connect('/dev/ttyUSB0', 115200)

prev_x = center_x
prev_y = center_y
prev_time = time.time()

def move(x, y):
    global prev_x, prev_y, prev_time

    x_delta = 0
    p_delta = 0
    current_time = time.time()
    time_delta = current_time - prev_time

    # Adjust the deltas based on the position
    if y > center_y + delta_threshold_y:
        p_delta = y - (center_y + delta_threshold_y)
    elif y < center_y - delta_threshold_y:
        p_delta = -(center_y - delta_threshold_y - y)

    if x > center_x + delta_threshold_x:
        x_delta = x - (center_x + delta_threshold_x)
    elif x < center_x - delta_threshold_x:
        x_delta = -(center_x - delta_threshold_x - x)

    # Calculate the distance moved since the last command
    distance_x = abs(x - prev_x)
    distance_y = abs(y - prev_y)

    # Calculate speed and acceleration based on distance and time
    speed = max(distance_x, distance_y) / time_delta
    acceleration = speed / time_delta

    # Update the previous values
    prev_x = x
    prev_y = y
    prev_time = current_time

    # Constrain and then normalize the deltas
    p_delta_constrained = max(min(p_delta, max_delta), min_delta)
    x_delta_constrained = max(min(x_delta, max_delta), min_delta)
    p_delta_normalized = -1.2 * p_delta_constrained
    x_delta_normalized = -1.5 * x_delta_constrained

    # Send the command
    command = f"H30,X{x_delta_normalized},P{p_delta_normalized}, \n"
    print(command)
    ser.write(command.encode())
