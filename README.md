
[![Group 2](https://github.com/robit-man/dropbear-neck-assembly/assets/36677806/bd13c6f5-7a3f-4262-9891-4259f17abbe0)](https://t.me/fractionalrobots)

![317462152-b25157ac-0184-4cff-bfda-d2495f46f825-ezgif com-optimize(1)](https://github.com/user-attachments/assets/cd753c06-48f7-4914-a3d4-d4f5289db4af)


# DROPBEAR NECK ASSEMBLY

This repository is focused on the development of a robot head and neck assembly, controlled by an ESP32 DevKit V1. The assembly is designed to allow for precise movements in multiple axes, enabling the robot head to simulate human-like motions. The system is controlled via Bluetooth, allowing for wireless operation.

## Features

- **Six Degrees of Freedom:** The robot head and neck assembly can move in six different axes, providing a wide range of motion.
- **Bluetooth Control:** Movement commands can be sent wirelessly via Bluetooth, enabling remote control of the robot head.
- **Precise Movement:** Utilizes the FastAccelStepper library for smooth and precise stepper motor control.
- **Adjustable Speed and Acceleration:** Movement speed and acceleration can be adjusted dynamically through commands.
- **Multiple Input Options:** Accepts commands via both Bluetooth and USB Serial, providing flexibility in control methods.

## Hardware Requirements

- ESP32 DevKit V1
- Six A4988 Stepper Motor Drivers and Associated Breakout Boards
- Six Standard NEMA 17 Stepper Motors
- Six 100mm length M8 Leadscrews and Associated T8 2mm pitch ACME Brass Nuts
- Six Universal Joints, 5mm to 5mm (neck to head) and an additional Six which are 5mm to 8mm (Motor to Leadscrews)
- Power Supply for the Motors and ESP32
- Linux or Other serial or bluetooth control source, we use an AGX Xavier, but a raspberry pi or laptop will suffice!

## Added Control Software in /interface

Just run via 
```bash
python3 app.py
```

and it will automatically generate a venv and start the application via flask.

![image](https://github.com/user-attachments/assets/01263283-5a82-4957-b464-39c6f7d5e6b1)


## Software Requirements

- [PlatformIO](https://platformio.org/) for ESP32 development
- [FastAccelStepper](https://github.com/gin66/FastAccelStepper) library for stepper motor control


![Group 1(2)](https://github.com/robit-man/dropbear-neck-assembly/assets/36677806/d8ad1fae-21bd-44cc-b0aa-567115c87615)


## Setup and Configuration

1. **Hardware Setup:**
   - Connect each stepper motor to an A4988 driver according to the pin map below.
   - Solid color is STEP pin on the driver breakout
   - Outline is the DIRECTION pin on the driver breakout
   - All Driver Breakouts have a daisy chained low power supply from the ESP
   - All Driver Breakouts also have a daisy chained HIGH power (isolated and for driving the nema's)
  

<img src="https://github.com/user-attachments/assets/a763decf-8748-48f2-a691-b59eb5c05b28" style="width:100%">


| Motor Number | Step Pin | Direction Pin |
|--------------|----------|---------------|
| Motor 1      | 33       | 32            |
| Motor 2      | 18       | 26            |
| Motor 3      | 23       | 14            |
| Motor 4      | 19       | 27            |
| Motor 5      | 22       | 12            |
| Motor 6      | 21       | 13            |

   - Pin 25 is optional for software based enable, however I tie all enable pins to ground to enable permanently.
   - Connect the step and direction pins of each A4988 driver to the corresponding pins on the ESP32.
   - Ensure the power supply is adequately rated for the stepper motors and the ESP32, 12-24v is safe.
   - If you want to modify your maxmimum current for the steppers, [please use this guide!](https://www.youtube.com/watch?v=OpaUwWouyE0)

2. **Software Setup:**
   - Open this folder in VSCode
   - Install PlatformIO and set up a new project for the ESP32 DevKit V1.
   - Add the FastAccelStepper library to the project dependencies.
   - Upload the provided script to the ESP32.

4. **Bluetooth Configuration:**
   - Pair the ESP32 with your Bluetooth control device.
   - Use the device name "NECK_BT" for connecting.
   - Send commands with the script in the py folder called neck_con.py
   - Import move(x,y) methods into parallel scripts, such as [Supervision](https://github.com/roboflow/supervision).
   - Modify the values on line 45 through 53 to reflect your imager resolution.
   - Add your own control schemes from and additional functions to neck_con.py and access them for your application.


![corner-head-transparent](https://github.com/robit-man/dropbear-neck-assembly/assets/36677806/99549253-a490-414b-ba1d-fceb49ccb87a)

## Usage

Send movement commands to the ESP32 via Bluetooth or USB Serial in the following format:

- **Direct Control:** `1:100,2:200,3:-150,...` where the number before the colon represents the motor number (1-6) and the number after the colon represents the target position in millimeters.
- **Angle and Height Control:** `X10,Y-5,Z15,H30,S1.5,A2,R10,P-5` where `X`, `Y`, and `Z` are the angles and planes for yaw or radial rotation, lateral, and ventral/dorsal translation respectively, `H` is the height offset, `S` is the speed multiplier, `A` is the acceleration multiplier, `R` is the roll angle, and `P` is the pitch angle.
- **Default Values:** The speed variable (speedVar) is set to 48000 hz, and acceleration value (accVar) is set to 36000. Refer to [FastAccelStepper library](https://github.com/gin66/FastAccelStepper/) for more details on where those variables are passed.

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

#### THANK YOU EVERYONE IN [FRACTIONAL ROBOTS!!!](https://t.me/fractionalrobots)
![photo_2024-03-24_21-52-06](https://github.com/robit-man/dropbear-neck-assembly/assets/36677806/75925903-4144-4487-bab8-65af1bf8f8df)
