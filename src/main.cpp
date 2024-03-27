#include <FastAccelStepper.h>
#include <BluetoothSerial.h>

BluetoothSerial BTSerial;

// Function prototypes
void executeCommand(String command);
void moveToStepper(int stepperNum, int positionSteps);

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

// Leadscrew parameters
#define LEADSCREW_PITCH 2.0 // Pitch of the leadscrew in mm
#define STEPS_PER_REV 6400  // Steps per revolution for the stepper motor

int speedVar = 48000;
int accVar = 36000;

// Calculate the distance per step
#define DISTANCE_PER_STEP (LEADSCREW_PITCH / STEPS_PER_REV) / 8

// Create a FastAccelStepperEngine object
FastAccelStepperEngine engine = FastAccelStepperEngine();

// Create pointers for each stepper motor
FastAccelStepper *stepper1 = NULL;
FastAccelStepper *stepper2 = NULL;
FastAccelStepper *stepper3 = NULL;
FastAccelStepper *stepper4 = NULL;
FastAccelStepper *stepper5 = NULL;
FastAccelStepper *stepper6 = NULL;

void setup()
{
  Serial.begin(115200);
  BTSerial.begin("NECK_BT"); // Start Bluetooth with a name "ESP32_BT"

  // Initialize the stepper engine
  engine.init();

  // Create and configure the stepper motors
  stepper1 = engine.stepperConnectToPin(MOTOR1_STEP_PIN);
  if (stepper1)
  {
    stepper1->setDirectionPin(MOTOR1_DIR_PIN);
    stepper1->setEnablePin(25);
    stepper1->setAutoEnable(true);
    stepper1->setSpeedInHz(speedVar);
    stepper1->setAcceleration(accVar);
  }

  stepper2 = engine.stepperConnectToPin(MOTOR2_STEP_PIN);
  if (stepper2)
  {
    stepper2->setDirectionPin(MOTOR2_DIR_PIN);
    stepper2->setEnablePin(25);
    stepper2->setAutoEnable(true);
    stepper2->setSpeedInHz(speedVar);
    stepper2->setAcceleration(accVar);
  }

  stepper3 = engine.stepperConnectToPin(MOTOR3_STEP_PIN);
  if (stepper3)
  {
    stepper3->setDirectionPin(MOTOR3_DIR_PIN);
    stepper3->setEnablePin(25);
    stepper3->setAutoEnable(true);
    stepper3->setSpeedInHz(speedVar);
    stepper3->setAcceleration(accVar);
  }

  stepper4 = engine.stepperConnectToPin(MOTOR4_STEP_PIN);
  if (stepper4)
  {
    stepper4->setDirectionPin(MOTOR4_DIR_PIN);
    stepper4->setEnablePin(25);
    stepper4->setAutoEnable(true);
    stepper4->setSpeedInHz(speedVar);
    stepper4->setAcceleration(accVar);
  }

  stepper5 = engine.stepperConnectToPin(MOTOR5_STEP_PIN);
  if (stepper5)
  {
    stepper5->setDirectionPin(MOTOR5_DIR_PIN);
    stepper5->setEnablePin(25);
    stepper5->setAutoEnable(true);
    stepper5->setSpeedInHz(speedVar);
    stepper5->setAcceleration(accVar);
  }

  stepper6 = engine.stepperConnectToPin(MOTOR6_STEP_PIN);
  if (stepper6)
  {
    stepper6->setDirectionPin(MOTOR6_DIR_PIN);
    stepper6->setEnablePin(25);
    stepper6->setAutoEnable(true);
    stepper6->setSpeedInHz(speedVar);
    stepper6->setAcceleration(accVar);
  }

  // Execute the startup command
  executeCommand("H-40,S2,A2");
  delay(2000);
  // Reset the position of each stepper motor to 0
  if (stepper1) stepper1->setCurrentPosition(0);
  if (stepper2) stepper2->setCurrentPosition(0);
  if (stepper3) stepper3->setCurrentPosition(0);
  if (stepper4) stepper4->setCurrentPosition(0);
  if (stepper5) stepper5->setCurrentPosition(0);
  if (stepper6) stepper6->setCurrentPosition(0);
}
void moveHead(int angleX, int angleY, int angleZ, int heightOffset, float speedMultiplier, float accelMultiplier, int roll, int pitch)
{
  // Define scaling factors for converting angles to stepper steps
  const float pitchScale = 10.0;   // Adjust as needed
  const float rollScale = 10.0;    // Adjust as needed
  const float yawScale = 10.0;     // Adjust as needed for Z-axis movement
  const float heightScale = 400.0; // Adjust as needed, based on the mechanics of your platform
  const float rollMovementScale = 10.0; // Adjust as needed for roll movement
  const float pitchMovementScale = 10.0; // Adjust as needed for roll movement

  // Calculate the movement for each stepper based on the angles and roll
  int move1 = -angleX * pitchScale + angleY * rollScale + angleZ * yawScale + pitch * pitchMovementScale + roll * rollMovementScale;
  int move2 = angleX * pitchScale - angleY * rollScale - angleZ * yawScale + pitch * pitchMovementScale + roll * rollMovementScale;
  int move3 = -angleX * pitchScale - angleY * rollScale - angleZ * yawScale - pitch * pitchMovementScale + roll * rollMovementScale;
  int move4 = angleX * pitchScale + angleY * rollScale - angleZ * yawScale - pitch * pitchMovementScale - roll * rollMovementScale;
  int move5 = -angleX * pitchScale + angleY * rollScale - angleZ * yawScale + pitch * pitchMovementScale - roll * rollMovementScale;
  int move6 = angleX * pitchScale - angleY * rollScale + angleZ * yawScale  + pitch * pitchMovementScale - roll * rollMovementScale;

  // Apply the height offset to each stepper
  int heightMovement = heightOffset * heightScale;
  move1 += heightMovement;
  move2 += heightMovement;
  move3 += heightMovement;
  move4 += heightMovement;
  move5 += heightMovement;
  move6 += heightMovement;

  // Adjust the speed and acceleration based on the multipliers
  int newSpeed = speedVar * speedMultiplier;
  int newAccel = accVar * accelMultiplier;

  // Move the steppers with the adjusted speed and acceleration
  if (stepper1)
  {
    stepper1->setSpeedInHz(newSpeed);
    stepper1->setAcceleration(newAccel);
    stepper1->moveTo(move1);
  }
  if (stepper2)
  {
    stepper2->setSpeedInHz(newSpeed);
    stepper2->setAcceleration(newAccel);
    stepper2->moveTo(move2);
  }
  if (stepper3)
  {
    stepper3->setSpeedInHz(newSpeed);
    stepper3->setAcceleration(newAccel);
    stepper3->moveTo(move3);
  }
  if (stepper4)
  {
    stepper4->setSpeedInHz(newSpeed);
    stepper4->setAcceleration(newAccel);
    stepper4->moveTo(move4);
  }
  if (stepper5)
  {
    stepper5->setSpeedInHz(newSpeed);
    stepper5->setAcceleration(newAccel);
    stepper5->moveTo(move5);
  }
  if (stepper6)
  {
    stepper6->setSpeedInHz(newSpeed);
    stepper6->setAcceleration(newAccel);
    stepper6->moveTo(move6);
  }
}


void parseAndMove(String input)
{
  // Split the input into individual commands
  int startIdx = 0;
  int endIdx = input.indexOf('|');
  while (endIdx != -1)
  {
    String command = input.substring(startIdx, endIdx);
    executeCommand(command); // Execute each command

    startIdx = endIdx + 1;
    endIdx = input.indexOf('|', startIdx);
  }

  // Execute the last command (since it's not followed by a pipe symbol)
  String lastCommand = input.substring(startIdx);
  executeCommand(lastCommand);
}

void executeCommand(String command)
{
  // Check if the command is for direct control of axes
  if (command.indexOf(':') != -1)
  {
    // Parse and execute direct control commands
    int startIdx = 0;
    int endIdx = command.indexOf(',');
    while (endIdx != -1)
    {
      String axisCommand = command.substring(startIdx, endIdx);
      int colonIdx = axisCommand.indexOf(':');
      if (colonIdx != -1)
      {
        int stepperNum = axisCommand.substring(0, colonIdx).toInt();
        int positionMM = axisCommand.substring(colonIdx + 1).toFloat();
        // Convert mm to steps
        int positionSteps = positionMM / DISTANCE_PER_STEP;
        // Move the corresponding stepper
        moveToStepper(stepperNum, positionSteps);
      }
      startIdx = endIdx + 1;
      endIdx = command.indexOf(',', startIdx);
    }

    // Handle the last axis command (since it's not followed by a comma)
    String lastAxisCommand = command.substring(startIdx);
    int lastColonIdx = lastAxisCommand.indexOf(':');
    if (lastColonIdx != -1)
    {
      int stepperNum = lastAxisCommand.substring(0, lastColonIdx).toInt();
      int positionMM = lastAxisCommand.substring(lastColonIdx + 1).toFloat();
      // Convert mm to steps
      int positionSteps = positionMM / DISTANCE_PER_STEP;
      // Move the corresponding stepper
      moveToStepper(stepperNum, positionSteps);
    }
  }
  else
  {
    // Initialize angles, height offset, and multipliers with default values
    int angleX = 0;
    int angleY = 0;
    int angleZ = 0;
    int heightOffset = 0;
    int roll = 0; // Added roll
    int pitch = 0; // Added pitch
    float speedMultiplier = 1.0; // Default to no change
    float accelMultiplier = 1.0; // Default to no change

    // Parse the command for angles, height offset, and multipliers
    int startIdx = 0;
    int endIdx = command.indexOf(',');
    while (endIdx != -1)
    {
      String angleCommand = command.substring(startIdx, endIdx);
      char axis = angleCommand.charAt(0);
      float value = angleCommand.substring(1).toFloat();

      switch (axis)
      {
      case 'X':
        angleX = value; // Yaw or left to right rotation
        break;
      case 'Y':
        angleY = value; // Translation left to right
        break;
      case 'Z':
        angleZ = value; // Translation front to back
        break;
      case 'H':
        heightOffset = value; // Height of the neck in mm
        break;
      case 'S':
        speedMultiplier = value; // Acceleration Value in hz
        break;
      case 'A':
        accelMultiplier = value; // Acceleration Value in hz
        break;
      case 'R': // Added case for roll or tilt
        roll = value;
        break;
      case 'P': // Added case for pitch or chin up
        pitch = value;
        break;
      }

      startIdx = endIdx + 1;
      endIdx = command.indexOf(',', startIdx);
    }

    // Handle the last value (since it's not followed by a comma)
    String lastCommand = command.substring(startIdx);
    char lastAxis = lastCommand.charAt(0);
    float lastValue = lastCommand.substring(1).toFloat();
    switch (lastAxis)
    {
    case 'X':
      angleX = lastValue;
      break;
    case 'Y':
      angleY = lastValue;
      break;
    case 'Z':
      angleZ = lastValue;
      break;
    case 'H':
      heightOffset = lastValue;
      break;
    case 'S':
      speedMultiplier = lastValue;
      break;
    case 'A':
      accelMultiplier = lastValue;
      break;
    case 'R': // Added case for roll
      roll = lastValue;
      break;
    case 'P': // Added case for roll
      pitch = lastValue;
      break;
    }

    // Move the head based on the parsed angles, height offset, and multipliers
    moveHead(angleX, angleY, angleZ, heightOffset, speedMultiplier, accelMultiplier, roll, pitch);
  }
}

void moveToStepper(int stepperNum, int positionSteps)
{
  switch (stepperNum)
  {
  case 1:
    if (stepper1)
      stepper1->moveTo(positionSteps);
    break;
  case 2:
    if (stepper2)
      stepper2->moveTo(positionSteps);
    break;
  case 3:
    if (stepper3)
      stepper3->moveTo(positionSteps);
    break;
  case 4:
    if (stepper4)
      stepper4->moveTo(positionSteps);
    break;
  case 5:
    if (stepper5)
      stepper5->moveTo(positionSteps);
    break;
  case 6:
    if (stepper6)
      stepper6->moveTo(positionSteps);
    break;
  default:
    Serial.println("Invalid stepper number");
    break;
  }
}

void loop()
{
  if (BTSerial.available()) { // Check if there's data from Bluetooth
    String input = BTSerial.readStringUntil('\n');
    parseAndMove(input);
  }

  if (Serial.available()) { // Check if there's data from USB Serial
    String input = Serial.readStringUntil('\n');
    parseAndMove(input);
  }
}
