import serial
import time
import bluetooth

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

device_address = "SET:THIS:TO:YOUR:ESP32:MAC:ADDRESS"
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

prev_x = 640
prev_y = 360
prev_time = time.time()

def move(x, y):
    global prev_x, prev_y, prev_time

    x_delta = 0
    p_delta = 0
    current_time = time.time()
    time_delta = current_time - prev_time

    # Adjust the deltas based on the position
    if y < 355:
        p_delta = 355 - y
    elif y > 365:
        p_delta = -(y - 365)

    if x > 645:
        x_delta = x - 645
    elif x < 635:
        x_delta = -(635 - x)

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

    # Send the command
    command = f"H30,X{x_delta},P{p_delta},S{speed:.2f},A{acceleration:.2f}"
    print(command)
    ser.write(command.encode())
