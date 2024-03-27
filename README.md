# Robot Head and Neck Assembly

https://github.com/robit-man/dropbear-neck-assembly/assets/36677806/b25157ac-0184-4cff-bfda-d2495f46f825

## Overview

This project is focused on the development of a robot head and neck assembly, controlled by an ESP32 DevKit V1. The assembly is designed to allow for precise movements in multiple axes, enabling the robot head to simulate human-like motions. The system is controlled via Bluetooth, allowing for wireless operation.

## Features

- **Six Degrees of Freedom:** The robot head and neck assembly can move in six different axes, providing a wide range of motion.
- **Bluetooth Control:** Movement commands can be sent wirelessly via Bluetooth, enabling remote control of the robot head.
- **Precise Movement:** Utilizes the FastAccelStepper library for smooth and precise stepper motor control.
- **Adjustable Speed and Acceleration:** Movement speed and acceleration can be adjusted dynamically through commands.
- **Multiple Input Options:** Accepts commands via both Bluetooth and USB Serial, providing flexibility in control methods.

## Hardware Requirements

- ESP32 DevKit V1
- Six A4988 Stepper Motor Drivers
- Six NEMA 17 Stepper Motors
- Leadscrews and Mechanical Assembly for the Robot Head and Neck
- Power Supply for the Motors and ESP32
- Bluetooth Module (if not integrated into the ESP32 DevKit V1)

## Software Requirements

- [PlatformIO](https://platformio.org/) for ESP32 development
- [FastAccelStepper](https://github.com/gin66/FastAccelStepper) library for stepper motor control

## Setup and Configuration

1. **Hardware Setup:**
   - Connect each stepper motor to an A4988 driver.
   - Connect the step and direction pins of each A4988 driver to the corresponding pins on the ESP32.
   - Ensure the power supply is adequately rated for the stepper motors and the ESP32.

2. **Software Setup:**
   - Open this folder in VSCode
   - Install PlatformIO and set up a new project for the ESP32 DevKit V1.
   - Add the FastAccelStepper library to the project dependencies.
   - Upload the provided script to the ESP32.

4. **Bluetooth Configuration:**
   - Pair the ESP32 with your Bluetooth control device.
   - Use the device name "NECK_BT" for connecting.
   - send commands with the script in the py folder called neck_con.py
   - Import move(x,y) methods into parallel scripts, such as [Supervision](https://github.com/roboflow/supervision).
   - add your own control schemes from and additional functions to neck_con.py and access them for your applicaiton.

## Usage

Send movement commands to the ESP32 via Bluetooth or USB Serial in the following format:

- **Direct Control:** `1:100,2:200,3:-150,...` where the number before the colon represents the motor number (1-6) and the number after the colon represents the target position in millimeters.
- **Angle and Height Control:** `X10,Y-5,Z15,H30,S1.5,A2,R10,P-5` where `X`, `Y`, and `Z` are the angles for yaw, roll, and pitch respectively, `H` is the height offset, `S` is the speed multiplier, `A` is the acceleration multiplier, `R` is the roll angle, and `P` is the pitch angle.

## Limitations

- Ensure that the mechanical limits of the robot head and neck assembly are not exceeded to avoid damage.
- The speed and acceleration values should be set carefully to prevent excessive wear on the mechanical components.

## Future Enhancements

- Implement feedback mechanisms such as encoders or limit switches for more precise control.
- Develop a user-friendly interface for controlling the robot head and neck assembly.
- Integrate sensors for autonomous movement and interaction.

## License

This project is open-source and available under the [MIT License](LICENSE).

## Acknowledgments

- Thanks to the developers of the FastAccelStepper library for providing an efficient way to control stepper motors with the ESP32.
- Gratitude to the ESP32 community for their valuable resources and support.
