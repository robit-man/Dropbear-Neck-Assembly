import * as THREE from "three";

const PROJECTION_STORAGE_KEY = "orientationProjectionMode";

const COMMAND_LIMIT = 700;
const HEIGHT_MIN = 0;
const HEIGHT_MAX = 70;

const defaultTuneables = {
  orientationSensitivity: 1.0,
  headingGain: 0.3,
  yawGain: 4.4,
  pitchGain: -5.8,
  rollGain: -4.6,
  accelYGain: 16.0,
  accelZGain: 14.0,
  accelHGain: 11.0,
  smoothAlpha: 0.42,
  commandIntervalMs: 85,
  speed: 0.8,
  acceleration: 0.6,
  offsetX: 0,
  offsetY: 0,
  offsetZ: 0,
  offsetH: 30,
  offsetR: 0,
  offsetP: 0,
};

const tuneables = { ...defaultTuneables };

const state = {
  initialized: false,
  playbackEnabled: false,
  baselinePending: false,
  baseline: null,
  smoothed: null,
  lastCommand: "",
  lastCommandSentAt: 0,
  sensorsEnabled: false,
  localPayload: null,
  localHz: 0,
  lastSamplePerf: 0,
  sceneLoaded: false,
  lastScenePushAt: 0,
  projection: "plane",
  locationWatchId: null,
};

const ui = {
  projectionPlaneBtn: null,
  projectionSphereBtn: null,
  statusEl: null,
  streamToggleBtn: null,
  embedFrame: null,
};

const NORTH_POLE = new THREE.Vector3(0, 1, 0);
const FALLBACK_REF = new THREE.Vector3(0, 0, -1);

function logOrientation(message) {
  if (typeof logToConsole === "function") {
    logToConsole(message);
  }
}

function byId(id) {
  return document.getElementById(id);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function toFinite(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function setOrientationStatus(message, error = false) {
  if (!ui.statusEl) {
    return;
  }
  ui.statusEl.textContent = message;
  ui.statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function updateStreamToggleUi() {
  if (!ui.streamToggleBtn) {
    return;
  }
  ui.streamToggleBtn.textContent = state.playbackEnabled ? "Pause Stream" : "Play Stream";
  ui.streamToggleBtn.className = state.playbackEnabled ? "" : "primary";
}

function formatHeading(heading) {
  const value = toFinite(heading, NaN);
  if (!Number.isFinite(value)) {
    return "-";
  }
  const wrapped = ((value % 360) + 360) % 360;
  return `${wrapped.toFixed(1)}deg`;
}

function formatAccel(acc) {
  if (!acc || typeof acc !== "object") {
    return "-";
  }
  const x = toFinite(acc.x, NaN);
  const y = toFinite(acc.y, NaN);
  const z = toFinite(acc.z, NaN);
  if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) {
    return "-";
  }
  return `ax ${x.toFixed(2)}g, ay ${y.toFixed(2)}g, az ${z.toFixed(2)}g`;
}

function setPlaybackBaselinePending(reasonMessage) {
  state.baseline = null;
  state.baselinePending = true;
  state.smoothed = null;
  state.lastCommand = "";
  state.lastCommandSentAt = 0;
  if (reasonMessage) {
    setOrientationStatus(reasonMessage);
  }
}

function projectionMode() {
  return state.projection === "sphere" ? "sphere" : "plane";
}

function latLonToUp(latDeg, lonDeg) {
  const d = Math.PI / 180;
  const phi = (90 - latDeg) * d;
  const theta = lonDeg * d;
  const sinPhi = Math.sin(phi);

  const x = sinPhi * Math.sin(theta);
  const y = Math.cos(phi);
  const z = -sinPhi * Math.cos(theta);

  const v = new THREE.Vector3(x, y, z);
  if (v.lengthSq() < 1e-12) {
    return new THREE.Vector3(0, 1, 0);
  }
  return v.normalize();
}

function computeTangentFrameFromUp(upVec) {
  const up = upVec.clone().normalize();
  const east = new THREE.Vector3().crossVectors(NORTH_POLE, up);
  if (east.lengthSq() < 1e-10) {
    east.crossVectors(FALLBACK_REF, up);
  }
  east.normalize();

  const north = new THREE.Vector3().crossVectors(up, east).normalize();
  const zAxis = north.clone().multiplyScalar(-1);
  const m = new THREE.Matrix4().makeBasis(east, up, zAxis);
  const q = new THREE.Quaternion().setFromRotationMatrix(m);
  return { q };
}

function normalizeHeadingDelta(delta) {
  let wrapped = delta;
  while (wrapped > 180) {
    wrapped -= 360;
  }
  while (wrapped < -180) {
    wrapped += 360;
  }
  return wrapped;
}

function parseQuaternion(statePayload) {
  if (!statePayload || !statePayload.q || typeof statePayload.q !== "object") {
    return null;
  }
  const qObj = statePayload.q;
  const x = toFinite(qObj.x, NaN);
  const y = toFinite(qObj.y, NaN);
  const z = toFinite(qObj.z, NaN);
  const w = toFinite(qObj.w, NaN);
  if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z) || !Number.isFinite(w)) {
    return null;
  }
  const q = new THREE.Quaternion(x, y, z, w);
  if (q.lengthSq() < 1e-9) {
    return null;
  }
  q.normalize();
  return q;
}

function parseAcceleration(statePayload) {
  const source = statePayload && typeof statePayload === "object"
    ? (statePayload.acc || statePayload.accG || null)
    : null;
  if (!source) {
    return { x: 0, y: 0, z: 0 };
  }
  const x = toFinite(source.x, 0);
  const y = toFinite(source.y, 0);
  const z = toFinite(source.z, 0);
  return { x, y, z };
}

function snapshotFromState(statePayload) {
  const qRaw = parseQuaternion(statePayload);
  if (!qRaw) {
    return null;
  }

  let qDisplay = qRaw.clone();
  if (projectionMode() === "sphere") {
    const gps = statePayload && statePayload.gps ? statePayload.gps : null;
    const lat = toFinite(gps ? gps.lat : NaN, NaN);
    const lon = toFinite(gps ? gps.lon : NaN, NaN);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      const frame = computeTangentFrameFromUp(latLonToUp(lat, lon));
      qDisplay = frame.q.clone().multiply(qRaw);
    }
  }

  const heading = toFinite(statePayload ? statePayload.heading : NaN, NaN);
  return {
    qDisplay,
    heading: Number.isFinite(heading) ? ((heading % 360) + 360) % 360 : null,
    acc: parseAcceleration(statePayload),
  };
}

function captureBaselineFromSnapshot(snapshot) {
  if (!snapshot) {
    return false;
  }
  state.baseline = {
    qDisplay: snapshot.qDisplay.clone(),
    heading: snapshot.heading,
  };
  state.baselinePending = false;
  state.smoothed = null;
  state.lastCommand = "";
  state.lastCommandSentAt = 0;
  setOrientationStatus("Centered at local baseline");
  logOrientation("[ORIENTATION] Baseline captured (local)");
  return true;
}

function buildCommand(snapshot) {
  if (!snapshot || !state.baseline) {
    return null;
  }

  const baselineInverse = state.baseline.qDisplay.clone().invert();
  const deltaQuaternion = baselineInverse.multiply(snapshot.qDisplay);
  const deltaEuler = new THREE.Euler().setFromQuaternion(deltaQuaternion, "YXZ");

  const yawDeg = THREE.MathUtils.radToDeg(deltaEuler.y);
  const pitchDeg = THREE.MathUtils.radToDeg(deltaEuler.x);
  const rollDeg = THREE.MathUtils.radToDeg(deltaEuler.z);

  const headingDelta = (snapshot.heading !== null && state.baseline.heading !== null)
    ? normalizeHeadingDelta(snapshot.heading - state.baseline.heading)
    : 0;

  const orientationScale = tuneables.orientationSensitivity;
  const yawRaw = orientationScale * ((yawDeg * tuneables.yawGain) + (headingDelta * tuneables.headingGain));
  const pitchRaw = orientationScale * (pitchDeg * tuneables.pitchGain);
  const rollRaw = orientationScale * (rollDeg * tuneables.rollGain);
  const lateralRaw = snapshot.acc.x * tuneables.accelYGain;
  const frontBackRaw = -snapshot.acc.z * tuneables.accelZGain;
  const heightRaw = snapshot.acc.y * tuneables.accelHGain;

  if (!state.smoothed) {
    state.smoothed = {
      yaw: yawRaw,
      lateral: lateralRaw,
      frontBack: frontBackRaw,
      height: heightRaw,
      roll: rollRaw,
      pitch: pitchRaw,
    };
  }

  const alpha = clamp(tuneables.smoothAlpha, 0.1, 0.95);
  state.smoothed.yaw = alpha * yawRaw + (1 - alpha) * state.smoothed.yaw;
  state.smoothed.lateral = alpha * lateralRaw + (1 - alpha) * state.smoothed.lateral;
  state.smoothed.frontBack = alpha * frontBackRaw + (1 - alpha) * state.smoothed.frontBack;
  state.smoothed.height = alpha * heightRaw + (1 - alpha) * state.smoothed.height;
  state.smoothed.roll = alpha * rollRaw + (1 - alpha) * state.smoothed.roll;
  state.smoothed.pitch = alpha * pitchRaw + (1 - alpha) * state.smoothed.pitch;

  const xVal = Math.round(clamp(state.smoothed.yaw + tuneables.offsetX, -COMMAND_LIMIT, COMMAND_LIMIT));
  const yVal = Math.round(clamp(state.smoothed.lateral + tuneables.offsetY, -COMMAND_LIMIT, COMMAND_LIMIT));
  const zVal = Math.round(clamp(state.smoothed.frontBack + tuneables.offsetZ, -COMMAND_LIMIT, COMMAND_LIMIT));
  const hVal = Math.round(clamp(state.smoothed.height + tuneables.offsetH, HEIGHT_MIN, HEIGHT_MAX));
  const rVal = Math.round(clamp(state.smoothed.roll + tuneables.offsetR, -COMMAND_LIMIT, COMMAND_LIMIT));
  const pVal = Math.round(clamp(state.smoothed.pitch + tuneables.offsetP, -COMMAND_LIMIT, COMMAND_LIMIT));

  const speed = clamp(tuneables.speed, 0, 10);
  const accel = clamp(tuneables.acceleration, 0, 10);
  return `X${xVal},Y${yVal},Z${zVal},H${hVal},S${speed.toFixed(1)},A${accel.toFixed(1)},R${rVal},P${pVal}`;
}

function dispatchCommand(command) {
  if (!command) {
    return;
  }
  if (typeof window.sendCommandHttpOnly === "function") {
    window.sendCommandHttpOnly(command);
    return;
  }
  console.warn("sendCommandHttpOnly is unavailable, orientation command dropped:", command);
}

function normalizeHeading(value) {
  const normalized = toFinite(value, NaN);
  if (!Number.isFinite(normalized)) {
    return null;
  }
  return ((normalized % 360) + 360) % 360;
}

function quaternionFromDeviceAngles(alphaDeg, betaDeg, gammaDeg) {
  const alpha = THREE.MathUtils.degToRad(toFinite(alphaDeg, 0));
  const beta = THREE.MathUtils.degToRad(toFinite(betaDeg, 0));
  const gamma = THREE.MathUtils.degToRad(toFinite(gammaDeg, 0));

  const euler = new THREE.Euler(beta, alpha, -gamma, "YXZ");
  const q = new THREE.Quaternion().setFromEuler(euler);

  const screenAngle = window.screen && window.screen.orientation
    ? toFinite(window.screen.orientation.angle, 0)
    : toFinite(window.orientation, 0);
  const orient = THREE.MathUtils.degToRad(screenAngle);
  const zAxis = new THREE.Vector3(0, 0, 1);
  const qScreen = new THREE.Quaternion().setFromAxisAngle(zAxis, -orient);
  q.multiply(qScreen);

  if (q.lengthSq() < 1e-9) {
    return null;
  }
  q.normalize();
  return q;
}

function updateLocalPayload(nextPatch) {
  const current = state.localPayload || {
    q: { x: 0, y: 0, z: 0, w: 1 },
    heading: null,
    acc: { x: 0, y: 0, z: 0 },
  };

  state.localPayload = {
    ...current,
    ...nextPatch,
    acc: {
      ...(current.acc || { x: 0, y: 0, z: 0 }),
      ...((nextPatch && nextPatch.acc) || {}),
    },
  };
}

function pushEmbeddedSceneState(snapshot, command = "") {
  if (!ui.embedFrame || !state.sceneLoaded || !snapshot) {
    return;
  }

  const now = performance.now();
  if (now - state.lastScenePushAt < 33) {
    return;
  }
  state.lastScenePushAt = now;

  const euler = new THREE.Euler().setFromQuaternion(snapshot.qDisplay, "YXZ");
  const yaw = THREE.MathUtils.radToDeg(euler.y);
  const pitch = THREE.MathUtils.radToDeg(euler.x);
  const roll = THREE.MathUtils.radToDeg(euler.z);

  const targetWindow = ui.embedFrame.contentWindow;
  if (!targetWindow) {
    return;
  }

  targetWindow.postMessage(
    {
      type: "orientation_local_update",
      yaw,
      pitch,
      roll,
      heading: snapshot.heading,
      command,
      playbackEnabled: state.playbackEnabled,
    },
    "*"
  );
}

function processOrientationFrame() {
  if (!state.localPayload) {
    setOrientationStatus("Waiting for local sensor data...");
    return;
  }

  const snapshot = snapshotFromState(state.localPayload);
  if (!snapshot) {
    setOrientationStatus("Waiting for valid orientation payload...");
    return;
  }

  pushEmbeddedSceneState(snapshot, state.lastCommand);

  if (!state.playbackEnabled) {
    return;
  }

  if (state.baselinePending || !state.baseline) {
    captureBaselineFromSnapshot(snapshot);
  }

  const command = buildCommand(snapshot);
  if (!command) {
    return;
  }
  const hz = Number.isFinite(state.localHz) ? state.localHz.toFixed(1) : "0.0";
  setOrientationStatus(
    `Live ${hz}Hz | heading ${formatHeading(state.localPayload.heading)} | ${formatAccel(state.localPayload.acc)}`
  );

  const now = Date.now();
  if (command !== state.lastCommand && now - state.lastCommandSentAt >= tuneables.commandIntervalMs) {
    dispatchCommand(command);
    state.lastCommand = command;
    state.lastCommandSentAt = now;
  }
}

function onDeviceOrientation(event) {
  if (!state.sensorsEnabled) {
    return;
  }

  const alpha = toFinite(event.alpha, NaN);
  const beta = toFinite(event.beta, NaN);
  const gamma = toFinite(event.gamma, NaN);
  if (!Number.isFinite(alpha) || !Number.isFinite(beta) || !Number.isFinite(gamma)) {
    return;
  }

  const q = quaternionFromDeviceAngles(alpha, beta, gamma);
  if (!q) {
    return;
  }

  const nowPerf = performance.now();
  if (state.lastSamplePerf > 0) {
    const instHz = 1000 / Math.max(1, nowPerf - state.lastSamplePerf);
    state.localHz = state.localHz > 0 ? (state.localHz * 0.85 + instHz * 0.15) : instHz;
  }
  state.lastSamplePerf = nowPerf;

  const webkitHeading = toFinite(event.webkitCompassHeading, NaN);
  const heading = Number.isFinite(webkitHeading)
    ? normalizeHeading(webkitHeading)
    : normalizeHeading(alpha);

  updateLocalPayload({
    q: { x: q.x, y: q.y, z: q.z, w: q.w },
    heading,
  });

  processOrientationFrame();
}

function onDeviceMotion(event) {
  if (!state.sensorsEnabled) {
    return;
  }

  const source = event.accelerationIncludingGravity || event.acceleration;
  if (!source) {
    return;
  }

  const g = 9.80665;
  const ax = toFinite(source.x, 0) / g;
  const ay = toFinite(source.y, 0) / g;
  const az = toFinite(source.z, 0) / g;

  updateLocalPayload({
    acc: { x: ax, y: ay, z: az },
  });
}

async function requestSensorPermissionIfNeeded() {
  if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function") {
    const orientationResult = await DeviceOrientationEvent.requestPermission();
    if (orientationResult !== "granted") {
      throw new Error("Device orientation permission denied");
    }
  }

  if (typeof DeviceMotionEvent !== "undefined" && typeof DeviceMotionEvent.requestPermission === "function") {
    const motionResult = await DeviceMotionEvent.requestPermission();
    if (motionResult !== "granted") {
      throw new Error("Device motion permission denied");
    }
  }
}

function startLocationTracking() {
  if (!("geolocation" in navigator)) {
    setOrientationStatus("Geolocation unavailable; using planar orientation only", true);
    return false;
  }
  if (state.locationWatchId !== null) {
    return true;
  }

  const onPosition = (position) => {
    const coords = position && position.coords ? position.coords : null;
    if (!coords) {
      return;
    }
    updateLocalPayload({
      gps: {
        lat: toFinite(coords.latitude, NaN),
        lon: toFinite(coords.longitude, NaN),
        accuracy_m: toFinite(coords.accuracy, NaN),
      },
    });
    if (state.playbackEnabled) {
      processOrientationFrame();
    }
  };

  const onError = (error) => {
    const detail = error && error.message ? error.message : "geolocation permission denied";
    setOrientationStatus(`Location unavailable: ${detail}`, true);
    logOrientation(`[ORIENTATION] Location unavailable: ${detail}`);
  };

  try {
    state.locationWatchId = navigator.geolocation.watchPosition(onPosition, onError, {
      enableHighAccuracy: false,
      maximumAge: 15000,
      timeout: 10000,
    });
    // Trigger immediate permission prompt on first load where applicable.
    navigator.geolocation.getCurrentPosition(onPosition, onError, {
      enableHighAccuracy: false,
      maximumAge: 15000,
      timeout: 10000,
    });
    return true;
  } catch (err) {
    setOrientationStatus(`Location unavailable: ${err.message || err}`, true);
    return false;
  }
}

async function startLocalSensors() {
  if (state.sensorsEnabled) {
    setOrientationStatus("Local sensor input already enabled");
    return true;
  }

  if (typeof window.DeviceOrientationEvent === "undefined") {
    setOrientationStatus("DeviceOrientation is not available in this browser/device", true);
    return false;
  }

  try {
    await requestSensorPermissionIfNeeded();
  } catch (err) {
    setOrientationStatus(`Sensor permission failed: ${err.message || err}`, true);
    return false;
  }

  window.addEventListener("deviceorientation", onDeviceOrientation, true);
  window.addEventListener("devicemotion", onDeviceMotion, true);

  state.sensorsEnabled = true;
  setOrientationStatus("Local sensor input enabled");
  logOrientation("[ORIENTATION] Local sensors enabled");
  startLocationTracking();

  if (state.playbackEnabled) {
    setPlaybackBaselinePending("Sensors enabled. Re-centering...");
  }
  return true;
}

function stopLocalSensors() {
  window.removeEventListener("deviceorientation", onDeviceOrientation, true);
  window.removeEventListener("devicemotion", onDeviceMotion, true);

  state.sensorsEnabled = false;
  state.localHz = 0;
  state.lastSamplePerf = 0;
  setOrientationStatus("Local sensor input disabled");

  logOrientation("[ORIENTATION] Local sensors disabled");
}

function applyTuneableValue(key, rawValue, integerValue = false) {
  const parsed = integerValue ? parseInt(rawValue, 10) : parseFloat(rawValue);
  if (!Number.isFinite(parsed)) {
    return;
  }
  tuneables[key] = parsed;
}

function bindTuneablePair(numberId, rangeId, key, integerValue = false) {
  const numberEl = byId(numberId);
  const rangeEl = byId(rangeId);
  if (!numberEl || !rangeEl) {
    return;
  }

  const apply = (raw) => {
    applyTuneableValue(key, raw, integerValue);
    const valueText = integerValue ? String(Math.round(tuneables[key])) : String(tuneables[key]);
    numberEl.value = valueText;
    rangeEl.value = valueText;
    if (state.playbackEnabled) {
      processOrientationFrame();
    }
  };

  numberEl.addEventListener("input", () => apply(numberEl.value));
  rangeEl.addEventListener("input", () => apply(rangeEl.value));
  apply(numberEl.value || rangeEl.value || String(defaultTuneables[key]));
}

function setTuneablesToDefaults() {
  Object.assign(tuneables, defaultTuneables);
  const mappings = [
    ["orientationSensitivity", "orientationSensitivity"],
    ["orientationSensitivityRange", "orientationSensitivity"],
    ["orientationHeadingGain", "headingGain"],
    ["orientationHeadingGainRange", "headingGain"],
    ["orientationYawGain", "yawGain"],
    ["orientationYawGainRange", "yawGain"],
    ["orientationPitchGain", "pitchGain"],
    ["orientationPitchGainRange", "pitchGain"],
    ["orientationRollGain", "rollGain"],
    ["orientationRollGainRange", "rollGain"],
    ["orientationAccelYGain", "accelYGain"],
    ["orientationAccelYGainRange", "accelYGain"],
    ["orientationAccelZGain", "accelZGain"],
    ["orientationAccelZGainRange", "accelZGain"],
    ["orientationAccelHGain", "accelHGain"],
    ["orientationAccelHGainRange", "accelHGain"],
    ["orientationSmoothAlpha", "smoothAlpha"],
    ["orientationSmoothAlphaRange", "smoothAlpha"],
    ["orientationIntervalMs", "commandIntervalMs"],
    ["orientationIntervalMsRange", "commandIntervalMs"],
    ["orientationSpeed", "speed"],
    ["orientationSpeedRange", "speed"],
    ["orientationAccel", "acceleration"],
    ["orientationAccelRange", "acceleration"],
    ["orientationOffsetX", "offsetX"],
    ["orientationOffsetXRange", "offsetX"],
    ["orientationOffsetY", "offsetY"],
    ["orientationOffsetYRange", "offsetY"],
    ["orientationOffsetZ", "offsetZ"],
    ["orientationOffsetZRange", "offsetZ"],
    ["orientationOffsetH", "offsetH"],
    ["orientationOffsetHRange", "offsetH"],
    ["orientationOffsetR", "offsetR"],
    ["orientationOffsetRRange", "offsetR"],
    ["orientationOffsetP", "offsetP"],
    ["orientationOffsetPRange", "offsetP"],
  ];

  mappings.forEach(([id, key]) => {
    const input = byId(id);
    if (input) {
      input.value = String(tuneables[key]);
    }
  });
}

function setPlaybackEnabled(enabled) {
  state.playbackEnabled = !!enabled;
  if (state.playbackEnabled) {
    setPlaybackBaselinePending("Play requested - waiting for local sensor data...");
    logOrientation("[ORIENTATION] Stream play");
    processOrientationFrame();
  } else {
    state.baselinePending = false;
    state.baseline = null;
    state.smoothed = null;
    state.lastCommand = "";
    state.lastCommandSentAt = 0;
    setOrientationStatus("Paused");
    logOrientation("[ORIENTATION] Stream pause");
  }
  updateStreamToggleUi();
}

function recenterBaseline() {
  if (!state.localPayload) {
    setOrientationStatus("Cannot recenter: no local sensor payload", true);
    return;
  }
  const snapshot = snapshotFromState(state.localPayload);
  if (!snapshot) {
    setOrientationStatus("Cannot recenter: local payload is invalid", true);
    return;
  }
  captureBaselineFromSnapshot(snapshot);
  if (state.playbackEnabled) {
    processOrientationFrame();
  }
}

function embeddedSceneDocument() {
  return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      html, body {
        margin: 0;
        width: 100%;
        height: 100%;
        overflow: hidden;
        background: radial-gradient(circle at 30% 20%, #1f2d3a 0%, #0a1018 70%);
        color: #e5eef9;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace;
      }
      .root {
        width: 100%;
        height: 100%;
        display: grid;
        grid-template-rows: 1fr auto;
      }
      .stage {
        perspective: 900px;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .cube {
        position: relative;
        width: 110px;
        height: 110px;
        transform-style: preserve-3d;
        transition: transform 0.05s linear;
      }
      .face {
        position: absolute;
        width: 110px;
        height: 110px;
        border: 1px solid rgba(255,255,255,0.25);
        background: rgba(64, 145, 255, 0.14);
        box-shadow: inset 0 0 20px rgba(120, 180, 255, 0.2);
      }
      .f1 { transform: translateZ(55px); }
      .f2 { transform: rotateY(180deg) translateZ(55px); }
      .f3 { transform: rotateY(90deg) translateZ(55px); }
      .f4 { transform: rotateY(-90deg) translateZ(55px); }
      .f5 { transform: rotateX(90deg) translateZ(55px); }
      .f6 { transform: rotateX(-90deg) translateZ(55px); }
      .meta {
        padding: 8px 10px;
        font-size: 12px;
        line-height: 1.35;
        background: rgba(3, 8, 14, 0.72);
        border-top: 1px solid rgba(255,255,255,0.1);
      }
      .meta .line { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .accent { color: #8fd2ff; }
    </style>
  </head>
  <body>
    <div class="root">
      <div class="stage">
        <div id="cube" class="cube">
          <div class="face f1"></div>
          <div class="face f2"></div>
          <div class="face f3"></div>
          <div class="face f4"></div>
          <div class="face f5"></div>
          <div class="face f6"></div>
        </div>
      </div>
      <div class="meta">
        <div class="line">Mode: <span id="mode" class="accent">Paused</span></div>
        <div class="line" id="angles">Yaw 0.0 | Pitch 0.0 | Roll 0.0</div>
        <div class="line" id="heading">Heading -</div>
        <div class="line" id="command">Command: waiting...</div>
      </div>
    </div>
    <script>
      const cube = document.getElementById("cube");
      const modeEl = document.getElementById("mode");
      const anglesEl = document.getElementById("angles");
      const headingEl = document.getElementById("heading");
      const commandEl = document.getElementById("command");

      window.addEventListener("message", (event) => {
        const data = event.data || {};
        if (data.type !== "orientation_local_update") {
          return;
        }
        const yaw = Number(data.yaw) || 0;
        const pitch = Number(data.pitch) || 0;
        const roll = Number(data.roll) || 0;
        cube.style.transform = "rotateX(" + pitch.toFixed(2) + "deg) rotateY(" + yaw.toFixed(2) + "deg) rotateZ(" + roll.toFixed(2) + "deg)";
        modeEl.textContent = data.playbackEnabled ? "Streaming" : "Paused";
        anglesEl.textContent = "Yaw " + yaw.toFixed(1) + " | Pitch " + pitch.toFixed(1) + " | Roll " + roll.toFixed(1);
        headingEl.textContent = Number.isFinite(Number(data.heading))
          ? "Heading " + Number(data.heading).toFixed(1) + "deg"
          : "Heading -";
        commandEl.textContent = data.command ? "Command: " + data.command : "Command: waiting...";
      });
    </script>
  </body>
</html>`;
}

function loadEmbeddedScene() {
  if (!ui.embedFrame) {
    return;
  }

  state.sceneLoaded = false;
  ui.embedFrame.onload = () => {
    state.sceneLoaded = true;
    setOrientationStatus("Embedded local orientation scene loaded");
    if (state.localPayload) {
      const snapshot = snapshotFromState(state.localPayload);
      if (snapshot) {
        pushEmbeddedSceneState(snapshot, state.lastCommand);
      }
    }
  };
  ui.embedFrame.srcdoc = embeddedSceneDocument();
}

function updateProjectionButtonsUi() {
  if (ui.projectionPlaneBtn) {
    ui.projectionPlaneBtn.classList.toggle("primary", projectionMode() === "plane");
  }
  if (ui.projectionSphereBtn) {
    ui.projectionSphereBtn.classList.toggle("primary", projectionMode() === "sphere");
  }
}

function setProjectionMode(mode) {
  state.projection = mode === "sphere" ? "sphere" : "plane";
  localStorage.setItem(PROJECTION_STORAGE_KEY, state.projection);
  updateProjectionButtonsUi();
  if (state.playbackEnabled) {
    setPlaybackBaselinePending(`Projection changed to ${state.projection}. Re-centering...`);
  }
  processOrientationFrame();
}

function bindCoreUi() {
  if (ui.projectionPlaneBtn) {
    ui.projectionPlaneBtn.addEventListener("click", () => setProjectionMode("plane"));
  }
  if (ui.projectionSphereBtn) {
    ui.projectionSphereBtn.addEventListener("click", () => setProjectionMode("sphere"));
  }
  if (ui.streamToggleBtn) {
    ui.streamToggleBtn.addEventListener("click", async () => {
      if (!state.playbackEnabled) {
        const ok = await startLocalSensors();
        if (!ok) {
          return;
        }
      }
      setPlaybackEnabled(!state.playbackEnabled);
    });
  }
}

function bindTuneablesUi() {
  bindTuneablePair("orientationSensitivity", "orientationSensitivityRange", "orientationSensitivity");
  bindTuneablePair("orientationHeadingGain", "orientationHeadingGainRange", "headingGain");
  bindTuneablePair("orientationYawGain", "orientationYawGainRange", "yawGain");
  bindTuneablePair("orientationPitchGain", "orientationPitchGainRange", "pitchGain");
  bindTuneablePair("orientationRollGain", "orientationRollGainRange", "rollGain");
  bindTuneablePair("orientationAccelYGain", "orientationAccelYGainRange", "accelYGain");
  bindTuneablePair("orientationAccelZGain", "orientationAccelZGainRange", "accelZGain");
  bindTuneablePair("orientationAccelHGain", "orientationAccelHGainRange", "accelHGain");
  bindTuneablePair("orientationSmoothAlpha", "orientationSmoothAlphaRange", "smoothAlpha");
  bindTuneablePair("orientationIntervalMs", "orientationIntervalMsRange", "commandIntervalMs", true);
  bindTuneablePair("orientationSpeed", "orientationSpeedRange", "speed");
  bindTuneablePair("orientationAccel", "orientationAccelRange", "acceleration");
  bindTuneablePair("orientationOffsetX", "orientationOffsetXRange", "offsetX");
  bindTuneablePair("orientationOffsetY", "orientationOffsetYRange", "offsetY");
  bindTuneablePair("orientationOffsetZ", "orientationOffsetZRange", "offsetZ");
  bindTuneablePair("orientationOffsetH", "orientationOffsetHRange", "offsetH", true);
  bindTuneablePair("orientationOffsetR", "orientationOffsetRRange", "offsetR");
  bindTuneablePair("orientationOffsetP", "orientationOffsetPRange", "offsetP");
}

function hydrateDefaults() {
  const savedProjection = localStorage.getItem(PROJECTION_STORAGE_KEY);
  if (savedProjection === "sphere" || savedProjection === "plane") {
    state.projection = savedProjection;
  }
  updateProjectionButtonsUi();
}

function cacheElements() {
  ui.projectionPlaneBtn = byId("orientationProjectionPlaneBtn");
  ui.projectionSphereBtn = byId("orientationProjectionSphereBtn");
  ui.statusEl = byId("orientationStatus");
  ui.streamToggleBtn = byId("orientationStreamToggleBtn");
  ui.embedFrame = byId("orientationEmbedFrame");
}

function initOrientationApp() {
  if (state.initialized) {
    return;
  }
  cacheElements();

  if (!ui.streamToggleBtn) {
    return;
  }

  state.initialized = true;
  hydrateDefaults();
  bindCoreUi();
  bindTuneablesUi();
  updateStreamToggleUi();
  setOrientationStatus("Requesting orientation and location permissions...");
  loadEmbeddedScene();
  startLocationTracking();
  startLocalSensors().then((ok) => {
    if (!ok) {
      setOrientationStatus("Permission required. Press Play Stream to retry", true);
      return;
    }
    setOrientationStatus("Sensors ready. Press Play Stream to send commands");
  });
}

window.initOrientationApp = initOrientationApp;

const initialRoute = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
if (initialRoute === "orientation") {
  initOrientationApp();
}
