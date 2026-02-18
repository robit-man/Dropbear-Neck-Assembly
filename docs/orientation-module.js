import * as THREE from "three";

const PROJECTION_STORAGE_KEY = "orientationProjectionMode";

const COMMAND_LIMIT = 700;
const ROLL_PITCH_LIMIT = 300;
const HEIGHT_MIN = 0;
const HEIGHT_MAX = 70;
const SPHERE_RADIUS = 6.0;

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
  speed: 0.6,
  acceleration: 0.4,
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
  projection: "plane",

  playbackEnabled: false,
  baselinePending: false,
  baselineQuaternion: null,
  baselineHeading: null,
  smoothed: null,
  lastCommand: "",
  lastCommandSentAt: 0,

  sensorsEnabled: false,
  localHz: 0,
  lastSamplePerf: 0,

  qRaw: new THREE.Quaternion(),
  qOffset: new THREE.Quaternion(),
  qDisp: new THREE.Quaternion(),
  heading: null,
  headingAcc: null,
  acc: { x: 0, y: 0, z: 0 },
  accG: null,
  gps: null,
  locationWatchId: null,

  // 3D scene
  sceneReady: false,
  sceneHost: null,
  scene: null,
  renderer: null,
  mainCamera: null,
  animationHandle: 0,
  lights: null,
  ground: null,
  grid: null,
  globe: null,
  anchorMarker: null,
  anchorUp: new THREE.Vector3(0, 1, 0),
  anchorFrame: null,
  localTwin: null,
};

const ui = {
  projectionPlaneBtn: null,
  projectionSphereBtn: null,
  streamToggleBtn: null,
  statusEl: null,
  sceneHost: null,
};
const hybridCommandReadoutEl = document.getElementById("hybridControlReadout");

const NORTH_POLE = new THREE.Vector3(0, 1, 0);
const FALLBACK_REF = new THREE.Vector3(0, 0, -1);
const ORIGIN = new THREE.Vector3(0, 0, 0);
const WORLD_G = new THREE.Vector3(0, -9.81, 0);

// DeviceOrientationControls-compatible quaternion conversion.
const zee = new THREE.Vector3(0, 0, 1);
const doEuler = new THREE.Euler();
const doQ0 = new THREE.Quaternion();
const doQ1 = new THREE.Quaternion(-Math.sqrt(0.5), 0, 0, Math.sqrt(0.5));

function byId(id) {
  return document.getElementById(id);
}

function isControlTransportReady() {
  if (typeof window.isControlTransportReady === "function") {
    return !!window.isControlTransportReady();
  }
  const sessionKey = String(localStorage.getItem("sessionKey") || "").trim();
  const httpUrl = String(localStorage.getItem("httpUrl") || "").trim();
  return !!(sessionKey && httpUrl);
}

function logOrientation(message) {
  if (typeof logToConsole === "function") {
    logToConsole(message);
  }
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function toFinite(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function setOrientationStatus(message, error = false) {
  if (ui.statusEl) {
    ui.statusEl.textContent = message;
    ui.statusEl.style.color = error ? "#ff4444" : "var(--accent)";
  }
  const hybridPanel = document.getElementById("hybridTabTouch");
  const hybridMode = hybridPanel ? String(hybridPanel.dataset.hybridMode || "") : "";
  if (hybridMode !== "orientation") {
    return;
  }
  const hybridStatusEl = document.getElementById("hybridModeStatus");
  if (hybridStatusEl) {
    hybridStatusEl.textContent = String(message || "");
    hybridStatusEl.style.color = error ? "#ff4444" : "var(--accent)";
  }
}

function updateHybridCommandReadout(command) {
  const hybridPanel = document.getElementById("hybridTabTouch");
  const hybridMode = hybridPanel ? String(hybridPanel.dataset.hybridMode || "") : "";
  if (hybridMode !== "orientation") {
    return;
  }
  if (hybridCommandReadoutEl && command) {
    hybridCommandReadoutEl.textContent = String(command);
  }
}

function updateStreamToggleUi() {
  if (!ui.streamToggleBtn) {
    return;
  }
  ui.streamToggleBtn.textContent = state.playbackEnabled ? "Pause Stream" : "Play Stream";
  ui.streamToggleBtn.classList.toggle("primary", !state.playbackEnabled);
  const ready = isControlTransportReady();
  ui.streamToggleBtn.disabled = !ready;
  ui.streamToggleBtn.title = ready
    ? "Play or pause orientation stream"
    : "Control transport disconnected. Configure Auth first.";
}

function projectionMode() {
  return state.projection === "sphere" ? "sphere" : "plane";
}

function updateProjectionButtonsUi() {
  if (ui.projectionPlaneBtn) {
    ui.projectionPlaneBtn.classList.toggle("primary", projectionMode() === "plane");
  }
  if (ui.projectionSphereBtn) {
    ui.projectionSphereBtn.classList.toggle("primary", projectionMode() === "sphere");
  }
}

function formatHeading(heading) {
  const value = toFinite(heading, NaN);
  if (!Number.isFinite(value)) {
    return "-";
  }
  return (((value % 360) + 360) % 360).toFixed(1);
}

function formatAccel(acc) {
  if (!acc) {
    return "ax -, ay -, az -";
  }
  return `ax ${toFinite(acc.x, 0).toFixed(2)}, ay ${toFinite(acc.y, 0).toFixed(2)}, az ${toFinite(acc.z, 0).toFixed(2)}`;
}

function normalizeHeadingDelta(delta) {
  let wrapped = delta;
  while (wrapped > 180) wrapped -= 360;
  while (wrapped < -180) wrapped += 360;
  return wrapped;
}

function normalizeHeading(value) {
  const normalized = toFinite(value, NaN);
  if (!Number.isFinite(normalized)) {
    return null;
  }
  return ((normalized % 360) + 360) % 360;
}

function screenOrientationRad() {
  const angle =
    window.screen && window.screen.orientation && typeof window.screen.orientation.angle === "number"
      ? window.screen.orientation.angle
      : typeof window.orientation === "number"
        ? window.orientation
        : 0;
  return angle * Math.PI / 180;
}

function compassHeadingFromEuler(alpha, beta, gamma) {
  const degtorad = Math.PI / 180;
  const a = alpha * degtorad;
  const b = beta * degtorad;
  const g = gamma * degtorad;
  const cA = Math.cos(a);
  const sA = Math.sin(a);
  const sB = Math.sin(b);
  const cG = Math.cos(g);
  const sG = Math.sin(g);
  const rA = -cA * sG - sA * sB * cG;
  const rB = -sA * sG + cA * sB * cG;
  let heading = Math.atan2(rA, rB);
  if (heading < 0) heading += 2 * Math.PI;
  return heading * 180 / Math.PI;
}

function deviceEulerToQuaternion(alpha, beta, gamma, orientRad) {
  const degtorad = Math.PI / 180;
  doEuler.set(beta * degtorad, alpha * degtorad, -gamma * degtorad, "YXZ");
  const q = new THREE.Quaternion().setFromEuler(doEuler);
  q.multiply(doQ1);
  q.multiply(doQ0.setFromAxisAngle(zee, -orientRad));
  return q;
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
  if (v.lengthSq() < 1e-12) return new THREE.Vector3(0, 1, 0);
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

  return { up, east, north, q };
}

function setAnchorFromUp(upVec) {
  state.anchorUp.copy(upVec).normalize();
  state.anchorFrame = computeTangentFrameFromUp(state.anchorUp);
  if (state.anchorMarker) {
    state.anchorMarker.position.copy(state.anchorFrame.up).multiplyScalar(SPHERE_RADIUS);
  }
}

function makePhoneTwin(accent) {
  const group = new THREE.Group();
  const phoneGeom = new THREE.BoxGeometry(0.6, 1.2, 0.08);
  const mat = new THREE.MeshStandardMaterial({
    color: accent ? 0xffae00 : 0xffffff,
    roughness: accent ? 0.25 : 0.65,
    metalness: 0.05,
  });
  const mesh = new THREE.Mesh(phoneGeom, mat);
  group.add(mesh);

  const fwd = new THREE.ArrowHelper(new THREE.Vector3(0, 0, -1), ORIGIN, 1.0, accent ? 0xffae00 : 0xffffff);
  group.add(fwd);

  const ax = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), ORIGIN, 0.001, accent ? 0xffae00 : 0xffffff);
  const ay = new THREE.ArrowHelper(new THREE.Vector3(0, 1, 0), ORIGIN, 0.001, accent ? 0xffae00 : 0xffffff);
  const az = new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), ORIGIN, 0.001, accent ? 0xffae00 : 0xffffff);
  group.add(ax, ay, az);

  return { group, mesh, fwd, accelArrows: { ax, ay, az } };
}

function updateAccelArrows(accelArrows, acc) {
  const scale = 0.08;
  const maxLen = 1.1;
  const minVis = 0.12;
  const comps = [
    ["ax", acc && acc.x, new THREE.Vector3(1, 0, 0)],
    ["ay", acc && acc.y, new THREE.Vector3(0, 1, 0)],
    ["az", acc && acc.z, new THREE.Vector3(0, 0, 1)],
  ];
  for (let i = 0; i < comps.length; i += 1) {
    const [key, value, axis] = comps[i];
    const arrow = accelArrows[key];
    if (!arrow) continue;
    if (typeof value !== "number" || !Number.isFinite(value) || Math.abs(value) < minVis) {
      arrow.setLength(0.001);
      arrow.visible = false;
      continue;
    }
    arrow.visible = true;
    const dir = axis.clone().multiplyScalar(value >= 0 ? 1 : -1).normalize();
    const length = Math.min(maxLen, Math.abs(value) * scale);
    arrow.setDirection(dir);
    arrow.setLength(Math.max(0.02, length));
  }
}

function applyLocalVisual() {
  if (!state.localTwin) {
    return;
  }
  state.qDisp.copy(state.qOffset).multiply(state.qRaw);
  updateAccelArrows(state.localTwin.accelArrows, state.acc);

  const mode = projectionMode();
  if (state.globe) {
    state.globe.visible = mode === "sphere";
  }
  if (state.grid) {
    state.grid.visible = mode === "plane";
  }
  if (state.anchorMarker) {
    state.anchorMarker.visible = mode === "sphere";
  }

  if (mode === "sphere") {
    if (state.anchorFrame) {
      state.localTwin.group.position.copy(state.anchorFrame.up).multiplyScalar(SPHERE_RADIUS);
      state.localTwin.group.quaternion.copy(state.anchorFrame.q).multiply(state.qDisp);
    }
    return;
  }

  state.localTwin.group.position.set(0, 1.0, 0);
  state.localTwin.group.quaternion.copy(state.qDisp);
}

function buildOrientationSnapshot() {
  const qFlat = state.qDisp.clone().normalize();
  let qDisplay = qFlat.clone();
  if (projectionMode() === "sphere" && state.anchorFrame) {
    qDisplay = state.anchorFrame.q.clone().multiply(qFlat);
  }
  return {
    qDisplay,
    heading: state.heading,
    acc: state.acc || { x: 0, y: 0, z: 0 },
  };
}

function setPlaybackBaselinePending(reasonMessage) {
  state.baselineQuaternion = null;
  state.baselineHeading = null;
  state.baselinePending = true;
  state.smoothed = null;
  state.lastCommand = "";
  state.lastCommandSentAt = 0;
  if (reasonMessage) {
    setOrientationStatus(reasonMessage);
  }
}

function captureBaseline(snapshot) {
  state.baselineQuaternion = snapshot.qDisplay.clone();
  state.baselineHeading = typeof snapshot.heading === "number" ? snapshot.heading : null;
  state.baselinePending = false;
  state.smoothed = null;
  state.lastCommand = "";
  state.lastCommandSentAt = 0;
  setOrientationStatus("Centered at local baseline");
}

function buildCommand(snapshot) {
  if (!snapshot || !state.baselineQuaternion) {
    return null;
  }

  const baselineInverse = state.baselineQuaternion.clone().invert();
  const deltaQuaternion = baselineInverse.multiply(snapshot.qDisplay.clone());
  const deltaEuler = new THREE.Euler().setFromQuaternion(deltaQuaternion, "YXZ");

  const yawDeg = THREE.MathUtils.radToDeg(deltaEuler.y);
  const pitchDeg = THREE.MathUtils.radToDeg(deltaEuler.x);
  const rollDeg = THREE.MathUtils.radToDeg(deltaEuler.z);

  const headingDelta =
    snapshot.heading !== null && state.baselineHeading !== null
      ? normalizeHeadingDelta(snapshot.heading - state.baselineHeading)
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
  const rVal = Math.round(clamp((-state.smoothed.roll) + tuneables.offsetR, -ROLL_PITCH_LIMIT, ROLL_PITCH_LIMIT));
  const pVal = Math.round(clamp((-state.smoothed.pitch) + tuneables.offsetP, -ROLL_PITCH_LIMIT, ROLL_PITCH_LIMIT));

  const speed = clamp(tuneables.speed, 0, 10);
  const accel = clamp(tuneables.acceleration, 0, 10);
  return `X${xVal},Y${yVal},Z${zVal},H${hVal},S${speed.toFixed(1)},A${accel.toFixed(1)},R${rVal},P${pVal}`;
}

function dispatchCommand(command) {
  if (!command) return;
  if (!isControlTransportReady()) {
    if (state.playbackEnabled) {
      setPlaybackEnabled(false);
    } else {
      updateStreamToggleUi();
    }
    setOrientationStatus("Control transport disconnected. Configure Auth first.", true);
    return;
  }
  if (typeof window.sendCommandHttpOnly === "function") {
    window.sendCommandHttpOnly(command);
    return;
  }
  console.warn("sendCommandHttpOnly is unavailable, orientation command dropped:", command);
}

function processOrientationFrame() {
  if (!state.sensorsEnabled) {
    setOrientationStatus("Waiting for sensor permissions...");
    return;
  }

  const snapshot = buildOrientationSnapshot();

  if (!state.playbackEnabled) {
    const hz = Number.isFinite(state.localHz) ? state.localHz.toFixed(1) : "0.0";
    setOrientationStatus(
      `Sensors ready ${hz}Hz | heading ${formatHeading(state.heading)}deg | ${formatAccel(state.acc)}`
    );
    return;
  }

  if (state.baselinePending || !state.baselineQuaternion) {
    captureBaseline(snapshot);
  }

  const command = buildCommand(snapshot);
  if (!command) return;
  updateHybridCommandReadout(command);

  const now = Date.now();
  if (command !== state.lastCommand && now - state.lastCommandSentAt >= tuneables.commandIntervalMs) {
    dispatchCommand(command);
    state.lastCommand = command;
    state.lastCommandSentAt = now;
  }

  const hz = Number.isFinite(state.localHz) ? state.localHz.toFixed(1) : "0.0";
  setOrientationStatus(
    `Streaming ${hz}Hz | heading ${formatHeading(state.heading)}deg | ${formatAccel(state.acc)}`
  );
}

function computeLinearAccelFallbackFromAccG(accG, qRaw) {
  if (!accG) return null;
  if (typeof accG.x !== "number" || typeof accG.y !== "number" || typeof accG.z !== "number") return null;
  const inv = qRaw.clone().invert();
  const gDevice = WORLD_G.clone().applyQuaternion(inv);
  return {
    x: accG.x + gDevice.x,
    y: accG.y + gDevice.y,
    z: accG.z + gDevice.z,
  };
}

function onDeviceOrientation(event) {
  if (!state.sensorsEnabled) return;
  if (event.alpha == null || event.beta == null || event.gamma == null) return;

  state.qRaw.copy(deviceEulerToQuaternion(event.alpha, event.beta, event.gamma, screenOrientationRad()));

  let heading = null;
  let headingAcc = null;
  if (typeof event.webkitCompassHeading === "number" && event.webkitCompassHeading >= 0) {
    heading = event.webkitCompassHeading;
    if (typeof event.webkitCompassAccuracy === "number") {
      headingAcc = event.webkitCompassAccuracy;
    }
  } else if (event.absolute === true) {
    heading = compassHeadingFromEuler(event.alpha, event.beta, event.gamma);
  }
  state.heading = normalizeHeading(heading);
  state.headingAcc = Number.isFinite(headingAcc) ? headingAcc : null;

  const nowPerf = performance.now();
  if (state.lastSamplePerf > 0) {
    const instHz = 1000 / Math.max(1, nowPerf - state.lastSamplePerf);
    state.localHz = state.localHz > 0 ? (state.localHz * 0.85 + instHz * 0.15) : instHz;
  }
  state.lastSamplePerf = nowPerf;

  applyLocalVisual();
  processOrientationFrame();
}

function onDeviceMotion(event) {
  if (!state.sensorsEnabled) return;

  const a = event.acceleration;
  const ag = event.accelerationIncludingGravity;
  if (ag && Number.isFinite(ag.x) && Number.isFinite(ag.y) && Number.isFinite(ag.z)) {
    state.accG = { x: ag.x, y: ag.y, z: ag.z };
  }
  if (a && Number.isFinite(a.x) && Number.isFinite(a.y) && Number.isFinite(a.z)) {
    state.acc = { x: a.x, y: a.y, z: a.z };
  } else if (state.accG) {
    const fallback = computeLinearAccelFallbackFromAccG(state.accG, state.qRaw);
    if (fallback) state.acc = fallback;
  }

  updateAccelArrows(state.localTwin.accelArrows, state.acc);
  processOrientationFrame();
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
    return false;
  }
  if (state.locationWatchId !== null) {
    return true;
  }

  const onPosition = (position) => {
    const coords = position && position.coords ? position.coords : null;
    if (!coords) return;
    state.gps = {
      lat: toFinite(coords.latitude, NaN),
      lon: toFinite(coords.longitude, NaN),
      acc: toFinite(coords.accuracy, NaN),
    };
    if (projectionMode() === "sphere" && Number.isFinite(state.gps.lat) && Number.isFinite(state.gps.lon)) {
      setAnchorFromUp(latLonToUp(state.gps.lat, state.gps.lon));
      applyLocalVisual();
    }
    processOrientationFrame();
  };

  const onError = (error) => {
    const detail = error && error.message ? error.message : "location unavailable";
    logOrientation(`[ORIENTATION] Location unavailable: ${detail}`);
  };

  try {
    state.locationWatchId = navigator.geolocation.watchPosition(onPosition, onError, {
      enableHighAccuracy: true,
      maximumAge: 250,
      timeout: 15000,
    });
    navigator.geolocation.getCurrentPosition(onPosition, onError, {
      enableHighAccuracy: true,
      maximumAge: 250,
      timeout: 15000,
    });
    return true;
  } catch (err) {
    logOrientation(`[ORIENTATION] Location tracking failed: ${err.message || err}`);
    return false;
  }
}

async function startLocalSensors() {
  if (state.sensorsEnabled) {
    return true;
  }
  if (typeof window.DeviceOrientationEvent === "undefined") {
    setOrientationStatus("DeviceOrientation is not available on this browser/device", true);
    return false;
  }

  try {
    await requestSensorPermissionIfNeeded();
  } catch (err) {
    setOrientationStatus(`Sensor permission failed: ${err.message || err}`, true);
    return false;
  }

  window.addEventListener("deviceorientation", onDeviceOrientation, true);
  window.addEventListener("deviceorientationabsolute", onDeviceOrientation, true);
  window.addEventListener("devicemotion", onDeviceMotion, true);

  state.sensorsEnabled = true;
  startLocationTracking();
  setOrientationStatus("Sensors enabled");
  logOrientation("[ORIENTATION] Local sensors enabled");
  return true;
}

function stopLocalSensors() {
  window.removeEventListener("deviceorientation", onDeviceOrientation, true);
  window.removeEventListener("deviceorientationabsolute", onDeviceOrientation, true);
  window.removeEventListener("devicemotion", onDeviceMotion, true);
  if (state.locationWatchId !== null && "geolocation" in navigator) {
    try {
      navigator.geolocation.clearWatch(state.locationWatchId);
    } catch (err) {
      // no-op
    }
    state.locationWatchId = null;
  }
  state.sensorsEnabled = false;
  state.localHz = 0;
  state.lastSamplePerf = 0;
  setOrientationStatus("Sensors disabled");
  logOrientation("[ORIENTATION] Local sensors disabled");
}

function setPlaybackEnabled(enabled) {
  if (enabled && !isControlTransportReady()) {
    state.playbackEnabled = false;
    setOrientationStatus("Control transport disconnected. Configure Auth first.", true);
    updateStreamToggleUi();
    return;
  }
  state.playbackEnabled = !!enabled;
  if (state.playbackEnabled) {
    setPlaybackBaselinePending("Play requested - waiting for baseline...");
    processOrientationFrame();
  } else {
    state.baselinePending = false;
    state.baselineQuaternion = null;
    state.baselineHeading = null;
    state.smoothed = null;
    state.lastCommand = "";
    state.lastCommandSentAt = 0;
    processOrientationFrame();
  }
  updateStreamToggleUi();
}

function handleControlTransportChanged() {
  const ready = isControlTransportReady();
  if (!ready && state.playbackEnabled) {
    setPlaybackEnabled(false);
    setOrientationStatus("Control transport disconnected. Configure Auth first.", true);
    return;
  }
  updateStreamToggleUi();
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
    const textValue = integerValue ? String(Math.round(tuneables[key])) : String(tuneables[key]);
    numberEl.value = textValue;
    rangeEl.value = textValue;
    if (state.playbackEnabled) {
      processOrientationFrame();
    }
  };
  numberEl.addEventListener("input", () => apply(numberEl.value));
  rangeEl.addEventListener("input", () => apply(rangeEl.value));
  apply(numberEl.value || rangeEl.value || String(defaultTuneables[key]));
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

function resizeScene() {
  if (!state.sceneReady || !state.renderer || !state.mainCamera || !ui.sceneHost) {
    return;
  }
  const rect = ui.sceneHost.getBoundingClientRect();
  const width = Math.max(1, Math.floor(rect.width));
  const height = Math.max(1, Math.floor(rect.height));
  state.mainCamera.aspect = width / height;
  state.mainCamera.updateProjectionMatrix();
  state.renderer.setSize(width, height, false);
}

function animateScene() {
  if (!state.sceneReady) {
    return;
  }
  state.animationHandle = window.requestAnimationFrame(animateScene);
  if (projectionMode() === "plane" && state.localTwin) {
    state.localTwin.group.position.y = 1.0 + Math.sin(Date.now() * 0.0012) * 0.02;
  } else {
    applyLocalVisual();
  }
  state.renderer.render(state.scene, state.mainCamera);
}

function initScene() {
  if (state.sceneReady || !ui.sceneHost) {
    return;
  }

  state.scene = new THREE.Scene();
  state.scene.fog = null;
  state.scene.background = null;

  state.mainCamera = new THREE.PerspectiveCamera(65, 16 / 9, 0.05, 100);
  state.mainCamera.position.set(0, 15, 15);
  state.mainCamera.lookAt(0, 1.2, 0);

  state.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  state.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  state.renderer.setClearColor(0x000000, 0);
  ui.sceneHost.innerHTML = "";
  ui.sceneHost.appendChild(state.renderer.domElement);

  state.scene.add(new THREE.AmbientLight(0xffffff, 0.45));
  const dir = new THREE.DirectionalLight(0xffffff, 0.8);
  dir.position.set(5, 8, 5);
  state.scene.add(dir);

  state.grid = new THREE.GridHelper(20, 20, 0x222222, 0x111111);
  state.grid.position.y = 0.01;
  state.scene.add(state.grid);

  state.globe = new THREE.Mesh(
    new THREE.SphereGeometry(SPHERE_RADIUS, 64, 32),
    new THREE.MeshStandardMaterial({
      color: 0xaaaaaa,
      roughness: 1,
      metalness: 0,
      wireframe: true,
      transparent: true,
      opacity: 0.35,
    })
  );
  state.globe.visible = false;
  state.scene.add(state.globe);

  state.anchorMarker = new THREE.Mesh(
    new THREE.SphereGeometry(0.12, 16, 12),
    new THREE.MeshStandardMaterial({ color: 0xffae00, roughness: 0.3, metalness: 0.1 })
  );
  state.anchorMarker.visible = false;
  state.scene.add(state.anchorMarker);

  state.localTwin = makePhoneTwin(true);
  state.localTwin.group.position.set(0, 1.0, 0);
  state.scene.add(state.localTwin.group);

  setAnchorFromUp(state.anchorUp);
  applyLocalVisual();

  state.sceneReady = true;
  resizeScene();
  animateScene();
}

function setProjectionMode(mode) {
  state.projection = mode === "sphere" ? "sphere" : "plane";
  localStorage.setItem(PROJECTION_STORAGE_KEY, state.projection);
  updateProjectionButtonsUi();

  if (state.projection === "sphere" && state.gps && Number.isFinite(state.gps.lat) && Number.isFinite(state.gps.lon)) {
    setAnchorFromUp(latLonToUp(state.gps.lat, state.gps.lon));
  }
  applyLocalVisual();

  if (state.playbackEnabled) {
    setPlaybackBaselinePending(`Projection changed to ${state.projection}. Re-centering...`);
  } else {
    processOrientationFrame();
  }
}

function bindCoreUi() {
  if (ui.streamToggleBtn) {
    ui.streamToggleBtn.addEventListener("click", async () => {
      if (!isControlTransportReady()) {
        setOrientationStatus("Control transport disconnected. Configure Auth first.", true);
        updateStreamToggleUi();
        return;
      }
      if (!state.playbackEnabled && !state.sensorsEnabled) {
        const ok = await startLocalSensors();
        if (!ok) {
          return;
        }
      }
      setPlaybackEnabled(!state.playbackEnabled);
    });
  }
  if (ui.projectionPlaneBtn) {
    ui.projectionPlaneBtn.addEventListener("click", () => setProjectionMode("plane"));
  }
  if (ui.projectionSphereBtn) {
    ui.projectionSphereBtn.addEventListener("click", () => setProjectionMode("sphere"));
  }
}

function hydrateDefaults() {
  const saved = localStorage.getItem(PROJECTION_STORAGE_KEY);
  if (saved === "sphere" || saved === "plane") {
    state.projection = saved;
  }
  updateProjectionButtonsUi();
}

function cacheElements() {
  ui.projectionPlaneBtn = byId("orientationProjectionPlaneBtn");
  ui.projectionSphereBtn = byId("orientationProjectionSphereBtn");
  ui.streamToggleBtn = byId("orientationStreamToggleBtn");
  ui.statusEl = byId("orientationStatus");
  ui.sceneHost = byId("orientationSceneHost");
}

function initOrientationApp() {
  if (state.initialized) {
    return;
  }

  cacheElements();
  if (!ui.sceneHost || !ui.streamToggleBtn) {
    return;
  }

  state.initialized = true;
  hydrateDefaults();
  bindCoreUi();
  bindTuneablesUi();
  updateStreamToggleUi();
  window.addEventListener("control-transport-changed", handleControlTransportChanged);
  initScene();

  window.addEventListener("resize", resizeScene);
  window.addEventListener("hybrid-preview-resize", (event) => {
    resizeScene();
    const mode = event && event.detail ? String(event.detail.mode || "") : "";
    if (mode && mode !== "orientation" && state.playbackEnabled) {
      setPlaybackEnabled(false);
    }
  });
  setOrientationStatus("Requesting orientation and location permissions...");

  startLocationTracking();
  startLocalSensors().then((ok) => {
    if (!ok) {
      setOrientationStatus("Permission required. Press Play Stream to retry", true);
      return;
    }
    processOrientationFrame();
    setOrientationStatus("Sensors ready. Press Play Stream to send commands");
  });
}

window.initOrientationApp = initOrientationApp;

const initialRoute = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
if (initialRoute === "orientation") {
  initOrientationApp();
}
