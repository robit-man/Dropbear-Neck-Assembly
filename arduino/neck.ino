/*
  Stewart Platform Controller for ESP32

  This code controls a Stewart platform using 6 stepper motors via lead screws.
  It accepts movement commands from both USB Serial and BluetoothSerial.
  
  The code supports two kinds of commands:
    1. Direct control commands for individual steppers (e.g., "1:30,2:45")
    2. General movement commands that specify platform head angles,
       height offsets, and speed/acceleration multipliers (e.g., "H-40,S2,A2")
    3. New quaternion-based commands for orientation control (e.g., 
       "Q:0.7071,0,0.7071,0,S1,A1")
  
  The quaternion command is interpreted as (w, x, y, z) and is converted to
  Euler angles using standard formulas. These Euler angles are then mapped into
  stepper positions (via the moveHead() function).

  Make sure you have the FastAccelStepper library installed and select your ESP32 board.
*/

#include <FastAccelStepper.h>    // Library for high-speed stepper control
#include <BluetoothSerial.h>     // Library for Bluetooth serial communication
#include <math.h>                // For math functions (e.g., sqrt, atan2, asin)

// Ensure PI is defined (Arduino usually defines PI already)
#ifndef PI
  #define PI 3.14159265358979323846
#endif

// ----------------------- Global Objects and Function Prototypes -----------------------

// Create an instance of BluetoothSerial for handling Bluetooth communication.
BluetoothSerial BTSerial;

// Function prototypes
void executeCommand(String command);
void moveToStepper(int stepperNum, int positionSteps);
void moveHead(int angleX, int angleY, int angleZ, int heightOffset,
              float speedMultiplier, float accelMultiplier, int roll, int pitch);
void parseAndMove(String input);
void handleQuaternionCommand(String command);
void setupStepper(FastAccelStepper* &stepper, int stepPin, int dirPin, int speed, int accel);

// ----------------------- Pin Definitions and Constants -----------------------

// Define the step and direction pins for each motor
#define MOTOR1_STEP_PIN 33
#define MOTOR1_DIR_PIN 32
#define MOTOR2_STEP_PIN 18
#define MOTOR2_DIR_PIN 26
#define MOTOR3_STEP_PIN 23
#define MOTOR3_DIR_PIN 14
#define MOTOR4_STEP_PIN 19
#define MOTOR4_DIR_PIN 27
#define MOTOR5_STEP_PIN 22
#define MOTOR5_DIR_PIN 12
#define MOTOR6_STEP_PIN 21
#define MOTOR6_DIR_PIN 13

// Define the enable pin used for all steppers (common to all motors)
#define MOTOR_ENABLE_PIN 25

// Leadscrew parameters: pitch in mm and steps per revolution for each motor.
#define LEADSCREW_PITCH 2.0   // mm pitch of the leadscrew
#define STEPS_PER_REV 6400    // Steps per revolution for the stepper motor

// Calculate the distance (in mm) that the leadscrew advances per step,
// factoring in microstepping (here assumed to be 1/8 microstepping).
#define DISTANCE_PER_STEP ((LEADSCREW_PITCH / STEPS_PER_REV) / 8)

// Global speed and acceleration variables (in Hz and steps/sec²)
int speedVar = 48000;
int accVar = 36000;

// Create a FastAccelStepperEngine instance to manage stepper motor actions.
FastAccelStepperEngine engine = FastAccelStepperEngine();

// Declare pointers for each of the 6 stepper motors.
FastAccelStepper *stepper1 = NULL;
FastAccelStepper *stepper2 = NULL;
FastAccelStepper *stepper3 = NULL;
FastAccelStepper *stepper4 = NULL;
FastAccelStepper *stepper5 = NULL;
FastAccelStepper *stepper6 = NULL;

// ----------------------- Setup Functions -----------------------

// Helper function to initialize and configure a stepper motor connected to a given step pin.
void setupStepper(FastAccelStepper* &stepper, int stepPin, int dirPin, int speed, int accel) {
  // Connect the stepper motor to the engine using the designated step pin.
  stepper = engine.stepperConnectToPin(stepPin);
  if (stepper) {
    stepper->setDirectionPin(dirPin);      // Set the direction pin
    stepper->setEnablePin(MOTOR_ENABLE_PIN); // Set the common enable pin
    stepper->setAutoEnable(true);            // Automatically enable on move command
    stepper->setSpeedInHz(speed);            // Set default speed (in Hz)
    stepper->setAcceleration(accel);         // Set default acceleration
  }
}

void setup() {
  // ----------------------- Initialize Serial Communication -----------------------
  Serial.begin(115200);          // Begin USB Serial communication for debugging
  BTSerial.begin("NECK_BT");      // Start Bluetooth with the name "NECK_BT"
  BTSerial.setTimeout(50);        // Set a short timeout for Bluetooth reads

  // ----------------------- Initialize the Stepper Engine -----------------------
  engine.init();

  // ----------------------- Configure Each Stepper Motor -----------------------
  setupStepper(stepper1, MOTOR1_STEP_PIN, MOTOR1_DIR_PIN, speedVar, accVar);
  setupStepper(stepper2, MOTOR2_STEP_PIN, MOTOR2_DIR_PIN, speedVar, accVar);
  setupStepper(stepper3, MOTOR3_STEP_PIN, MOTOR3_DIR_PIN, speedVar, accVar);
  setupStepper(stepper4, MOTOR4_STEP_PIN, MOTOR4_DIR_PIN, speedVar, accVar);
  setupStepper(stepper5, MOTOR5_STEP_PIN, MOTOR5_DIR_PIN, speedVar, accVar);
  setupStepper(stepper6, MOTOR6_STEP_PIN, MOTOR6_DIR_PIN, speedVar, accVar);

  // ----------------------- Execute a Startup Command -----------------------
  // This sample command positions the platform head. Adjust as needed.
  executeCommand("H-40,S2,A2");
  delay(2000);  // Allow time for the movement to complete

  // ----------------------- Reset Stepper Positions -----------------------
  if (stepper1) stepper1->setCurrentPosition(0);
  if (stepper2) stepper2->setCurrentPosition(0);
  if (stepper3) stepper3->setCurrentPosition(0);
  if (stepper4) stepper4->setCurrentPosition(0);
  if (stepper5) stepper5->setCurrentPosition(0);
  if (stepper6) stepper6->setCurrentPosition(0);
}

// ----------------------- Movement and Command Processing Functions -----------------------

/*
  moveHead()
  -----------
  Calculates and sets new target positions (in steps) for each of the 6 stepper motors
  based on input angles, height offset, and speed/acceleration multipliers.

  Parameters:
    - angleX, angleY, angleZ: Base angles (or translations) for head movement.
    - heightOffset: Additional vertical movement (in mm).
    - speedMultiplier: Scaling factor to adjust the speed.
    - accelMultiplier: Scaling factor to adjust the acceleration.
    - roll, pitch: Additional rotational adjustments.

  The movement for each stepper is computed as a linear combination of these inputs
  using predefined scaling factors (which should be tuned for your platform's mechanics).
*/
void moveHead(int angleX, int angleY, int angleZ, int heightOffset,
              float speedMultiplier, float accelMultiplier, int roll, int pitch) {
  // Define scale factors (tweak these values based on your mechanical design)
  const float pitchScale = 10.0;         // Scale for angleX (interpreted as yaw)
  const float rollScale = 10.0;          // Scale for angleY (interpreted as lateral translation)
  const float yawScale = 10.0;           // Scale for angleZ (interpreted as front/back translation)
  const float heightScale = 400.0;       // Scale to convert height offset (mm) to steps
  const float rollMovementScale = 10.0;  // Additional scale for roll adjustment
  const float pitchMovementScale = 10.0; // Additional scale for pitch adjustment

  // Compute target positions (in steps) for each stepper motor.
  int move1 = -angleX * pitchScale + angleY * rollScale + angleZ * yawScale + 
               pitch * pitchMovementScale + roll * rollMovementScale;
  int move2 =  angleX * pitchScale - angleY * rollScale - angleZ * yawScale + 
               pitch * pitchMovementScale + roll * rollMovementScale;
  int move3 = -angleX * pitchScale - angleY * rollScale - angleZ * yawScale - 
               pitch * pitchMovementScale + roll * rollMovementScale;
  int move4 =  angleX * pitchScale + angleY * rollScale - angleZ * yawScale - 
               pitch * pitchMovementScale - roll * rollMovementScale;
  int move5 = -angleX * pitchScale + angleY * rollScale - angleZ * yawScale + 
               pitch * pitchMovementScale - roll * rollMovementScale;
  int move6 =  angleX * pitchScale - angleY * rollScale + angleZ * yawScale + 
               pitch * pitchMovementScale - roll * rollMovementScale;

  // Calculate height adjustment (in steps) and add to each motor's target.
  int heightMovement = heightOffset * heightScale;
  move1 += heightMovement;
  move2 += heightMovement;
  move3 += heightMovement;
  move4 += heightMovement;
  move5 += heightMovement;
  move6 += heightMovement;

  // Adjust speed and acceleration according to multipliers.
  int newSpeed = speedVar * speedMultiplier;
  int newAccel = accVar * accelMultiplier;

  // Command each stepper motor to move to its new target position.
  if (stepper1) { stepper1->setSpeedInHz(newSpeed); stepper1->setAcceleration(newAccel); stepper1->moveTo(move1); }
  if (stepper2) { stepper2->setSpeedInHz(newSpeed); stepper2->setAcceleration(newAccel); stepper2->moveTo(move2); }
  if (stepper3) { stepper3->setSpeedInHz(newSpeed); stepper3->setAcceleration(newAccel); stepper3->moveTo(move3); }
  if (stepper4) { stepper4->setSpeedInHz(newSpeed); stepper4->setAcceleration(newAccel); stepper4->moveTo(move4); }
  if (stepper5) { stepper5->setSpeedInHz(newSpeed); stepper5->setAcceleration(newAccel); stepper5->moveTo(move5); }
  if (stepper6) { stepper6->setSpeedInHz(newSpeed); stepper6->setAcceleration(newAccel); stepper6->moveTo(move6); }
}

/*
  parseAndMove()
  ---------------
  Parses a compound command string that may contain several commands separated by the '|'
  character. Each individual command is then executed via executeCommand().
*/
void parseAndMove(String input) {
  int startIdx = 0;
  int endIdx = input.indexOf('|');

  // Process each command (delimited by '|')
  while (endIdx != -1) {
    String command = input.substring(startIdx, endIdx);
    executeCommand(command);
    startIdx = endIdx + 1;
    endIdx = input.indexOf('|', startIdx);
  }

  // Execute the last (or only) command if any.
  String lastCommand = input.substring(startIdx);
  if (lastCommand.length() > 0) {
    executeCommand(lastCommand);
  }
}

/*
  executeCommand()
  -----------------
  Interprets and executes a single command string.
  
  Command types:
    - If the command starts with 'Q', it is treated as a quaternion command.
    - If the command contains a colon (':'), it is processed as direct
      control of individual steppers (e.g., "1:30,2:45").
    - Otherwise, it is interpreted as a general movement command
      that sets head movement parameters (e.g., "H-40,S2,A2").
*/
void executeCommand(String command) {
  command.trim();  // Remove any extraneous whitespace

  // Ignore empty commands.
  if (command.length() == 0) return;

  // ---------- Handle Quaternion Commands ----------
  if (command.charAt(0) == 'Q') {
    handleQuaternionCommand(command);
    return;
  }

  // ---------- Handle Direct Stepper Control Commands ----------
  if (command.indexOf(':') != -1) {
    int startIdx = 0;
    int endIdx = command.indexOf(',');
    while (endIdx != -1) {
      String axisCommand = command.substring(startIdx, endIdx);
      int colonIdx = axisCommand.indexOf(':');
      if (colonIdx != -1) {
        // Extract the stepper number and target position (in mm)
        int stepperNum = axisCommand.substring(0, colonIdx).toInt();
        float positionMM = axisCommand.substring(colonIdx + 1).toFloat();
        // Convert mm to steps using the defined scale factor
        int positionSteps = positionMM / DISTANCE_PER_STEP;
        moveToStepper(stepperNum, positionSteps);
      }
      startIdx = endIdx + 1;
      endIdx = command.indexOf(',', startIdx);
    }

    // Process the last token in the command
    String lastAxisCommand = command.substring(startIdx);
    int lastColonIdx = lastAxisCommand.indexOf(':');
    if (lastColonIdx != -1) {
      int stepperNum = lastAxisCommand.substring(0, lastColonIdx).toInt();
      float positionMM = lastAxisCommand.substring(lastColonIdx + 1).toFloat();
      int positionSteps = positionMM / DISTANCE_PER_STEP;
      moveToStepper(stepperNum, positionSteps);
    }
  }
  // ---------- Handle General Movement Commands ----------
  else {
    // Default parameters for head movement
    int angleX = 0;         // e.g., yaw or horizontal rotation
    int angleY = 0;         // e.g., lateral translation
    int angleZ = 0;         // e.g., front-to-back translation
    int heightOffset = 0;   // Height adjustment (mm)
    int roll = 0;           // Additional roll adjustment
    int pitch = 0;          // Additional pitch adjustment
    float speedMultiplier = 1.0; // No speed scaling by default
    float accelMultiplier = 1.0; // No acceleration scaling by default

    // Parse the comma-separated tokens in the command.
    int startIdx = 0;
    int endIdx = command.indexOf(',');
    while (endIdx != -1) {
      String angleCommand = command.substring(startIdx, endIdx);
      if (angleCommand.length() > 0) {
        char axis = angleCommand.charAt(0);
        float value = angleCommand.substring(1).toFloat();
        switch (axis) {
          case 'X': angleX = value; break; // Typically yaw
          case 'Y': angleY = value; break; // Typically lateral translation
          case 'Z': angleZ = value; break; // Typically front-to-back translation
          case 'H': heightOffset = value; break; // Height offset in mm
          case 'S': speedMultiplier = value; break; // Speed multiplier
          case 'A': accelMultiplier = value; break; // Acceleration multiplier
          case 'R': roll = value; break; // Roll adjustment
          case 'P': pitch = value; break; // Pitch adjustment
        }
      }
      startIdx = endIdx + 1;
      endIdx = command.indexOf(',', startIdx);
    }

    // Process the final token in the command.
    String lastCommand = command.substring(startIdx);
    if (lastCommand.length() > 0) {
      char lastAxis = lastCommand.charAt(0);
      float lastValue = lastCommand.substring(1).toFloat();
      switch (lastAxis) {
        case 'X': angleX = lastValue; break;
        case 'Y': angleY = lastValue; break;
        case 'Z': angleZ = lastValue; break;
        case 'H': heightOffset = lastValue; break;
        case 'S': speedMultiplier = lastValue; break;
        case 'A': accelMultiplier = lastValue; break;
        case 'R': roll = lastValue; break;
        case 'P': pitch = lastValue; break;
      }
    }

    // Command the platform head to move with the specified parameters.
    moveHead(angleX, angleY, angleZ, heightOffset, speedMultiplier, accelMultiplier, roll, pitch);
  }
}

/*
  moveToStepper()
  ----------------
  Directs a specific stepper motor (identified by its number) to move to a target position.
  The target position is given in steps (calculated from mm using DISTANCE_PER_STEP).
*/
void moveToStepper(int stepperNum, int positionSteps) {
  switch (stepperNum) {
    case 1: if (stepper1) stepper1->moveTo(positionSteps); break;
    case 2: if (stepper2) stepper2->moveTo(positionSteps); break;
    case 3: if (stepper3) stepper3->moveTo(positionSteps); break;
    case 4: if (stepper4) stepper4->moveTo(positionSteps); break;
    case 5: if (stepper5) stepper5->moveTo(positionSteps); break;
    case 6: if (stepper6) stepper6->moveTo(positionSteps); break;
    default: Serial.println("Invalid stepper number"); break;
  }
}

/*
  handleQuaternionCommand()
  ---------------------------
  Processes a command string that starts with 'Q' (for quaternion control).
  
  Expected format:
    Q:<w>,<x>,<y>,<z>[,S<speedMultiplier>][,A<accelMultiplier>]

  The first four comma‐separated values are the quaternion components.
  Optional tokens starting with 'S' and 'A' set the speed and acceleration multipliers.
  The quaternion is normalized and converted to Euler angles (roll, pitch, yaw) using standard formulas.
  For this implementation:
    - The yaw angle is mapped to angleX.
    - The pitch angle is mapped to angleY.
    - The roll angle is mapped to angleZ.
  (Height offset and additional roll/pitch adjustments are set to 0 here.)
*/
void handleQuaternionCommand(String command) {
  // Remove the leading 'Q' and an optional colon.
  command = command.substring(1);
  command.trim();
  if (command.startsWith(":")) {
    command = command.substring(1);
    command.trim();
  }

  // Split the command by commas.
  // Expect at least 4 tokens: w, x, y, z.
  float q[4] = {0, 0, 0, 0};
  float speedMultiplier = 1.0;
  float accelMultiplier = 1.0;

  // Tokenize the command into an array of strings (maximum 6 tokens).
  String tokens[6];
  int tokenCount = 0;
  int startIdx = 0;
  int endIdx = command.indexOf(',');
  while (endIdx != -1 && tokenCount < 6) {
    tokens[tokenCount++] = command.substring(startIdx, endIdx);
    startIdx = endIdx + 1;
    endIdx = command.indexOf(',', startIdx);
  }
  // Add the last token, if any.
  if (startIdx < command.length() && tokenCount < 6) {
    tokens[tokenCount++] = command.substring(startIdx);
  }

  // Verify that there are at least 4 tokens for the quaternion components.
  if (tokenCount < 4) {
    Serial.println("Invalid quaternion command: not enough parameters.");
    return;
  }

  // Parse quaternion components (assumed order: w, x, y, z).
  for (int i = 0; i < 4; i++) {
    q[i] = tokens[i].toFloat();
  }

  // Parse optional speed (S) and acceleration (A) multipliers.
  for (int i = 4; i < tokenCount; i++) {
    String token = tokens[i];
    token.trim();
    if (token.startsWith("S")) {
      speedMultiplier = token.substring(1).toFloat();
    } else if (token.startsWith("A")) {
      accelMultiplier = token.substring(1).toFloat();
    }
  }

  // Normalize the quaternion to ensure it represents a valid rotation.
  float norm = sqrt(q[0]*q[0] + q[1]*q[1] + q[2]*q[2] + q[3]*q[3]);
  if (norm > 0) {
    for (int i = 0; i < 4; i++) {
      q[i] /= norm;
    }
  } else {
    Serial.println("Invalid quaternion: norm is zero.");
    return;
  }

  // Convert quaternion to Euler angles (in radians) using standard formulas.
  // Assumes q[0] = w, q[1] = x, q[2] = y, q[3] = z.
  float roll_rad  = atan2(2.0 * (q[0]*q[1] + q[2]*q[3]), 1.0 - 2.0 * (q[1]*q[1] + q[2]*q[2]));
  float pitch_rad = asin(2.0 * (q[0]*q[2] - q[3]*q[1]));
  float yaw_rad   = atan2(2.0 * (q[0]*q[3] + q[1]*q[2]), 1.0 - 2.0 * (q[2]*q[2] + q[3]*q[3]));

  // Convert the Euler angles from radians to degrees.
  int roll_deg  = round(roll_rad * (180.0 / PI));
  int pitch_deg = round(pitch_rad * (180.0 / PI));
  int yaw_deg   = round(yaw_rad * (180.0 / PI));

  // Debug output for the quaternion conversion.
  Serial.print("Quaternion command received. Euler angles (deg): Yaw=");
  Serial.print(yaw_deg);
  Serial.print(", Pitch=");
  Serial.print(pitch_deg);
  Serial.print(", Roll=");
  Serial.println(roll_deg);

  // Map the Euler angles to the movement command.
  // For this example, we use:
  //   angleX = yaw, angleY = pitch, angleZ = roll, with no height or extra roll/pitch adjustments.
  int heightOffset = 0;
  moveHead(yaw_deg, pitch_deg, roll_deg, heightOffset, speedMultiplier, accelMultiplier, 0, 0);
}

// ----------------------- Main Loop -----------------------

/*
  loop()
  -------
  Continuously checks for incoming data from both Bluetooth and USB Serial.
  If data is available, it reads a full command (terminated by a newline),
  trims it, and passes it to parseAndMove() for processing.
*/
void loop() {
  // Check for Bluetooth data.
  if (BTSerial.available()) {
    String input = BTSerial.readStringUntil('\n');
    input.trim();
    if (input.length() > 0) {
      parseAndMove(input);
    }
  }

  // Check for USB Serial data.
  if (Serial.available()) {
    String input = Serial.readStringUntil('\n');
    input.trim();
    if (input.length() > 0) {
      parseAndMove(input);
    }
  }
}
