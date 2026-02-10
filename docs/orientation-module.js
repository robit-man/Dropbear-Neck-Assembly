import * as THREE from "three";

const HUB_URL_STORAGE_KEY = "orientationHubUrl";
const HUB_WS_STORAGE_KEY = "orientationHubWsUrl";
const HUB_SELECTED_PEER_STORAGE_KEY = "orientationSelectedPeer";
const HUB_PROJECTION_STORAGE_KEY = "orientationProjectionMode";

const DEFAULT_HUB_URL = "http://127.0.0.1:8080";
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
  ws: null,
  wsUrl: "",
  myClientId: "",
  peers: new Map(),
  playbackEnabled: false,
  baselinePending: false,
  baseline: null,
  smoothed: null,
  lastCommand: "",
  lastCommandSentAt: 0,
  lastSelectedPeerId: "",
  activeSourceId: "",
  disconnectRequested: false,
};

const ui = {
  hubUrlInput: null,
  hubWsInput: null,
  loadEmbedBtn: null,
  connectBtn: null,
  disconnectBtn: null,
  peerSelect: null,
  projectionSelect: null,
  statusEl: null,
  hubStateEl: null,
  sourceMetaEl: null,
  streamStateEl: null,
  commandStreamEl: null,
  streamToggleBtn: null,
  recenterBtn: null,
  resetTuneablesBtn: null,
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

function shortId(id) {
  if (!id) {
    return "-";
  }
  return id.length > 10 ? `${id.slice(0, 10)}...` : id;
}

function setOrientationStatus(message, error = false) {
  if (!ui.statusEl) {
    return;
  }
  ui.statusEl.textContent = message;
  ui.statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function setHubState(message, error = false) {
  if (!ui.hubStateEl) {
    return;
  }
  ui.hubStateEl.textContent = message;
  ui.hubStateEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function setStreamState(message, error = false) {
  if (!ui.streamStateEl) {
    return;
  }
  ui.streamStateEl.textContent = message;
  ui.streamStateEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function updateStreamToggleUi() {
  if (!ui.streamToggleBtn) {
    return;
  }
  ui.streamToggleBtn.textContent = state.playbackEnabled ? "Pause Stream" : "Play Stream";
  ui.streamToggleBtn.className = state.playbackEnabled ? "" : "primary";
}

function normalizeHubUrl(rawInput) {
  const trimmed = (rawInput || "").trim();
  if (!trimmed) {
    return "";
  }
  const candidate = trimmed.includes("://") ? trimmed : `http://${trimmed}`;
  const parsed = new URL(candidate);
  if (parsed.protocol === "ws:") {
    parsed.protocol = "http:";
  } else if (parsed.protocol === "wss:") {
    parsed.protocol = "https:";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("Hub URL must use http:// or https://");
  }
  return parsed.toString();
}

function normalizeWsUrl(rawInput) {
  const trimmed = (rawInput || "").trim();
  if (!trimmed) {
    return "";
  }

  const candidate = trimmed.includes("://") ? trimmed : `ws://${trimmed}`;
  const parsed = new URL(candidate);
  if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") {
    throw new Error("WebSocket URL must use ws:// or wss://");
  }
  const path = parsed.pathname === "/" ? "" : parsed.pathname;
  return `${parsed.protocol}//${parsed.host}${path}${parsed.search}`;
}

function deriveWsUrlFromHubUrl(rawInput) {
  const normalizedHub = normalizeHubUrl(rawInput);
  if (!normalizedHub) {
    return "";
  }
  const parsed = new URL(normalizedHub);
  const protocol = parsed.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${parsed.host}`;
}

function buildEmbedUrl() {
  const hubUrl = normalizeHubUrl(ui.hubUrlInput ? ui.hubUrlInput.value : "");
  if (!hubUrl) {
    return "";
  }
  const parsed = new URL(hubUrl);
  const wsUrl = normalizeWsUrl(ui.hubWsInput ? ui.hubWsInput.value : deriveWsUrlFromHubUrl(hubUrl));
  if (wsUrl) {
    parsed.searchParams.set("ws", wsUrl);
  }
  return parsed.toString();
}

function formatGps(gps) {
  if (!gps || typeof gps !== "object") {
    return "-";
  }
  const lat = toFinite(gps.lat, NaN);
  const lon = toFinite(gps.lon, NaN);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    return "-";
  }
  const acc = toFinite(gps.acc, 0);
  return `${lat.toFixed(4)},${lon.toFixed(4)} +/-${Math.round(acc)}m`;
}

function formatHeading(heading) {
  const value = toFinite(heading, NaN);
  if (!Number.isFinite(value)) {
    return "-";
  }
  const wrapped = ((value % 360) + 360) % 360;
  return `${wrapped.toFixed(1)}deg`;
}

function clearPeers() {
  state.peers.clear();
  updatePeerSelectOptions();
  updateSourceMeta();
}

function setPlaybackBaselinePending(reasonMessage) {
  state.baseline = null;
  state.baselinePending = true;
  state.smoothed = null;
  state.lastCommand = "";
  state.lastCommandSentAt = 0;
  if (reasonMessage) {
    setStreamState(reasonMessage);
  }
}

function updatePeerSelectOptions() {
  if (!ui.peerSelect) {
    return;
  }

  const previous = ui.peerSelect.value || localStorage.getItem(HUB_SELECTED_PEER_STORAGE_KEY) || "";
  ui.peerSelect.innerHTML = "";

  const autoOption = document.createElement("option");
  autoOption.value = "";
  autoOption.textContent = "Auto (latest active)";
  ui.peerSelect.appendChild(autoOption);

  Array.from(state.peers.entries())
    .filter(([peerId]) => peerId !== state.myClientId)
    .sort((left, right) => {
      const leftUpdated = left[1].updatedAt || 0;
      const rightUpdated = right[1].updatedAt || 0;
      return rightUpdated - leftUpdated;
    })
    .forEach(([peerId, peer]) => {
      const opt = document.createElement("option");
      opt.value = peerId;
      const hz = Number.isFinite(peer.hz) ? peer.hz.toFixed(1) : "0.0";
      opt.textContent = `${shortId(peerId)} | ${hz}Hz | ${formatHeading(peer.state ? peer.state.heading : null)}`;
      ui.peerSelect.appendChild(opt);
    });

  const optionExists = previous
    ? Array.from(ui.peerSelect.options).some((opt) => opt.value === previous)
    : false;
  if (optionExists) {
    ui.peerSelect.value = previous;
  } else {
    ui.peerSelect.value = "";
  }
}

function selectedPeerId() {
  if (!ui.peerSelect) {
    return "";
  }
  return (ui.peerSelect.value || "").trim();
}

function selectSourcePeer() {
  const explicit = selectedPeerId();
  if (explicit && state.peers.has(explicit)) {
    return { id: explicit, ...state.peers.get(explicit) };
  }

  let best = null;
  for (const [peerId, peer] of state.peers.entries()) {
    if (peerId === state.myClientId) {
      continue;
    }
    if (!best || (peer.updatedAt || 0) > (best.updatedAt || 0)) {
      best = { id: peerId, ...peer };
    }
  }
  return best;
}

function updateSourceMeta() {
  if (!ui.sourceMetaEl) {
    return;
  }

  const source = selectSourcePeer();
  if (!source) {
    ui.sourceMetaEl.textContent = "No peers publishing orientation yet";
    return;
  }

  const st = source.state || {};
  const hz = Number.isFinite(source.hz) ? source.hz.toFixed(1) : "0.0";
  ui.sourceMetaEl.textContent = `Peer ${shortId(source.id)} | ${hz}Hz | heading ${formatHeading(st.heading)} | gps ${formatGps(st.gps)}`;
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

function projectionMode() {
  if (!ui.projectionSelect) {
    return "plane";
  }
  return ui.projectionSelect.value === "sphere" ? "sphere" : "plane";
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

function captureBaselineFromSnapshot(snapshot, peerId = "") {
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
  setStreamState(`Centered at ${peerId ? shortId(peerId) : "source"} baseline`);
  logOrientation(`[ORIENTATION] Baseline captured${peerId ? ` (${shortId(peerId)})` : ""}`);
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
  if (typeof window.sendCommand === "function") {
    window.sendCommand(command);
  } else {
    console.warn("sendCommand is unavailable, orientation command dropped:", command);
  }
}

function processOrientationFrame() {
  updateSourceMeta();
  if (!state.playbackEnabled) {
    return;
  }

  const source = selectSourcePeer();
  if (!source) {
    state.activeSourceId = "";
    setStreamState("Waiting for device source...");
    if (ui.commandStreamEl) {
      ui.commandStreamEl.textContent = "Waiting for source data...";
    }
    return;
  }

  if (source.id !== state.activeSourceId) {
    state.activeSourceId = source.id;
    setPlaybackBaselinePending(`Source ${shortId(source.id)} active. Re-centering...`);
  }

  const snapshot = snapshotFromState(source.state);
  if (!snapshot) {
    setStreamState(`Waiting for quaternion from ${shortId(source.id)}...`);
    if (ui.commandStreamEl) {
      ui.commandStreamEl.textContent = "Source has no quaternion payload yet";
    }
    return;
  }

  if (state.baselinePending || !state.baseline) {
    captureBaselineFromSnapshot(snapshot, source.id);
  }

  const command = buildCommand(snapshot);
  if (!command) {
    return;
  }

  if (ui.commandStreamEl) {
    ui.commandStreamEl.textContent = command;
  }
  setStreamState(`Live from ${shortId(source.id)}`);

  const now = Date.now();
  if (command !== state.lastCommand && now - state.lastCommandSentAt >= tuneables.commandIntervalMs) {
    dispatchCommand(command);
    state.lastCommand = command;
    state.lastCommandSentAt = now;
  }
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
    setPlaybackBaselinePending("Play requested - waiting for source data...");
    if (ui.commandStreamEl) {
      ui.commandStreamEl.textContent = "Waiting for source to establish baseline...";
    }
    logOrientation("[ORIENTATION] Stream play");
    processOrientationFrame();
  } else {
    state.baselinePending = false;
    state.baseline = null;
    state.smoothed = null;
    state.lastCommand = "";
    state.lastCommandSentAt = 0;
    setStreamState("Paused");
    if (ui.commandStreamEl) {
      ui.commandStreamEl.textContent = "Paused - press Play Stream";
    }
    logOrientation("[ORIENTATION] Stream pause");
  }
  updateStreamToggleUi();
}

function recenterBaseline() {
  const source = selectSourcePeer();
  if (!source) {
    setStreamState("Cannot recenter: no source selected", true);
    return;
  }
  const snapshot = snapshotFromState(source.state);
  if (!snapshot) {
    setStreamState(`Cannot recenter: ${shortId(source.id)} has no quaternion`, true);
    return;
  }
  captureBaselineFromSnapshot(snapshot, source.id);
  if (state.playbackEnabled) {
    processOrientationFrame();
  }
}

function loadEmbeddedHub() {
  if (!ui.embedFrame || !ui.hubUrlInput || !ui.hubWsInput) {
    return;
  }

  try {
    const normalizedHub = normalizeHubUrl(ui.hubUrlInput.value);
    if (!normalizedHub) {
      setOrientationStatus("Enter Orientation Hub URL first", true);
      return;
    }

    if (!ui.hubWsInput.value.trim()) {
      ui.hubWsInput.value = deriveWsUrlFromHubUrl(normalizedHub);
    } else {
      ui.hubWsInput.value = normalizeWsUrl(ui.hubWsInput.value);
    }

    const embedUrl = buildEmbedUrl();
    ui.embedFrame.src = embedUrl;
    localStorage.setItem(HUB_URL_STORAGE_KEY, normalizedHub);
    localStorage.setItem(HUB_WS_STORAGE_KEY, ui.hubWsInput.value.trim());
    setOrientationStatus("Embedded hub loaded");
  } catch (err) {
    setOrientationStatus(`Embedded hub failed: ${err.message || err}`, true);
  }
}

function upsertPeerState(peerId, nextState) {
  if (!peerId) {
    return;
  }
  const now = performance.now();
  const existing = state.peers.get(peerId) || { state: {}, updatedAt: 0, hz: 0, lastRecvPerf: 0 };
  const mergedState = { ...(existing.state || {}), ...(nextState || {}) };
  let hz = existing.hz || 0;
  if (existing.lastRecvPerf) {
    const instHz = 1000 / Math.max(1, now - existing.lastRecvPerf);
    hz = hz ? (hz * 0.85 + instHz * 0.15) : instHz;
  }
  state.peers.set(peerId, {
    state: mergedState,
    updatedAt: Date.now(),
    hz,
    lastRecvPerf: now,
  });
}

function handleHubMessage(rawMessage) {
  if (!rawMessage || typeof rawMessage !== "object") {
    return;
  }
  if (rawMessage.type === "welcome") {
    state.myClientId = rawMessage.id || "";
    clearPeers();
    const peers = Array.isArray(rawMessage.peers) ? rawMessage.peers : [];
    peers.forEach((peerState) => {
      if (peerState && peerState.id) {
        upsertPeerState(peerState.id, peerState);
      }
    });
    updatePeerSelectOptions();
    updateSourceMeta();
    setHubState(`Connected (${state.peers.size} peers)`);
    processOrientationFrame();
    return;
  }

  if (rawMessage.type === "join" && rawMessage.peer && rawMessage.peer.id) {
    upsertPeerState(rawMessage.peer.id, rawMessage.peer);
    updatePeerSelectOptions();
    updateSourceMeta();
    processOrientationFrame();
    return;
  }

  if (rawMessage.type === "leave" && rawMessage.id) {
    state.peers.delete(rawMessage.id);
    if (selectedPeerId() === rawMessage.id && ui.peerSelect) {
      ui.peerSelect.value = "";
      localStorage.setItem(HUB_SELECTED_PEER_STORAGE_KEY, "");
      setPlaybackBaselinePending("Source left. Waiting for another device...");
    }
    updatePeerSelectOptions();
    updateSourceMeta();
    processOrientationFrame();
    return;
  }

  if (rawMessage.type === "peer_update" && rawMessage.peer && rawMessage.peer.id) {
    upsertPeerState(rawMessage.peer.id, rawMessage.peer);
    updatePeerSelectOptions();
    updateSourceMeta();
    processOrientationFrame();
  }
}

function disconnectHub() {
  state.disconnectRequested = true;
  if (state.ws) {
    try {
      state.ws.close();
    } catch (err) {}
  }
  state.ws = null;
  state.wsUrl = "";
  state.myClientId = "";
  clearPeers();
  setHubState("Disconnected");
  setOrientationStatus("Orientation hub disconnected");
}

function connectHub() {
  if (!ui.hubUrlInput || !ui.hubWsInput) {
    return;
  }

  try {
    const normalizedHub = normalizeHubUrl(ui.hubUrlInput.value || DEFAULT_HUB_URL);
    if (!ui.hubWsInput.value.trim()) {
      ui.hubWsInput.value = deriveWsUrlFromHubUrl(normalizedHub);
    }
    const wsUrl = normalizeWsUrl(ui.hubWsInput.value);
    if (!wsUrl) {
      setOrientationStatus("Enter Orientation Hub WS URL", true);
      return;
    }

    localStorage.setItem(HUB_URL_STORAGE_KEY, normalizedHub);
    localStorage.setItem(HUB_WS_STORAGE_KEY, wsUrl);

    if (state.ws) {
      disconnectHub();
    }

    state.disconnectRequested = false;
    state.wsUrl = wsUrl;
    setHubState(`Connecting ${wsUrl}...`);
    setOrientationStatus("Connecting to orientation hub...");

    const ws = new WebSocket(wsUrl);
    state.ws = ws;

    ws.addEventListener("open", () => {
      if (ws !== state.ws) {
        return;
      }
      setHubState(`Connected ${wsUrl}`);
      setOrientationStatus("Orientation hub connected");
      logOrientation(`[ORIENTATION] Hub connected: ${wsUrl}`);
    });

    ws.addEventListener("message", (event) => {
      if (ws !== state.ws) {
        return;
      }
      try {
        const data = JSON.parse(event.data);
        handleHubMessage(data);
      } catch (err) {}
    });

    ws.addEventListener("close", () => {
      if (ws !== state.ws) {
        return;
      }
      const wasRequested = state.disconnectRequested;
      state.ws = null;
      state.wsUrl = "";
      state.myClientId = "";
      clearPeers();
      if (wasRequested) {
        setHubState("Disconnected");
      } else {
        setHubState("Disconnected (hub closed)", true);
        setOrientationStatus("Hub connection closed", true);
      }
    });

    ws.addEventListener("error", () => {
      if (ws !== state.ws) {
        return;
      }
      setHubState("WebSocket error", true);
      setOrientationStatus("Orientation hub websocket error", true);
    });
  } catch (err) {
    setOrientationStatus(`Connect failed: ${err.message || err}`, true);
    setHubState("Disconnected", true);
  }
}

function bindCoreUi() {
  if (ui.loadEmbedBtn) {
    ui.loadEmbedBtn.addEventListener("click", loadEmbeddedHub);
  }
  if (ui.connectBtn) {
    ui.connectBtn.addEventListener("click", connectHub);
  }
  if (ui.disconnectBtn) {
    ui.disconnectBtn.addEventListener("click", disconnectHub);
  }
  if (ui.peerSelect) {
    ui.peerSelect.addEventListener("change", () => {
      const nextPeer = selectedPeerId();
      localStorage.setItem(HUB_SELECTED_PEER_STORAGE_KEY, nextPeer);
      if (nextPeer !== state.lastSelectedPeerId) {
        state.lastSelectedPeerId = nextPeer;
        if (state.playbackEnabled) {
          setPlaybackBaselinePending(`Source changed to ${nextPeer ? shortId(nextPeer) : "auto"}. Re-centering...`);
        }
      }
      updateSourceMeta();
      processOrientationFrame();
    });
  }
  if (ui.projectionSelect) {
    ui.projectionSelect.addEventListener("change", () => {
      localStorage.setItem(HUB_PROJECTION_STORAGE_KEY, projectionMode());
      if (state.playbackEnabled) {
        setPlaybackBaselinePending(`Projection changed to ${projectionMode()}. Re-centering...`);
      }
      processOrientationFrame();
    });
  }
  if (ui.streamToggleBtn) {
    ui.streamToggleBtn.addEventListener("click", () => {
      setPlaybackEnabled(!state.playbackEnabled);
    });
  }
  if (ui.recenterBtn) {
    ui.recenterBtn.addEventListener("click", recenterBaseline);
  }
  if (ui.resetTuneablesBtn) {
    ui.resetTuneablesBtn.addEventListener("click", () => {
      setTuneablesToDefaults();
      if (state.playbackEnabled) {
        setPlaybackBaselinePending("Tuneables reset. Re-centering...");
      }
      processOrientationFrame();
      logOrientation("[ORIENTATION] Tuneables reset to defaults");
    });
  }

  if (ui.hubUrlInput) {
    ui.hubUrlInput.addEventListener("change", () => {
      try {
        const normalizedHub = normalizeHubUrl(ui.hubUrlInput.value);
        ui.hubUrlInput.value = normalizedHub;
        if (ui.hubWsInput && !ui.hubWsInput.value.trim()) {
          ui.hubWsInput.value = deriveWsUrlFromHubUrl(normalizedHub);
        }
      } catch (err) {}
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
  if (ui.hubUrlInput) {
    const savedHub = localStorage.getItem(HUB_URL_STORAGE_KEY) || DEFAULT_HUB_URL;
    ui.hubUrlInput.value = savedHub;
  }
  if (ui.hubWsInput) {
    const savedWs = localStorage.getItem(HUB_WS_STORAGE_KEY) || deriveWsUrlFromHubUrl(ui.hubUrlInput ? ui.hubUrlInput.value : DEFAULT_HUB_URL);
    ui.hubWsInput.value = savedWs;
  }
  if (ui.peerSelect) {
    const savedPeer = localStorage.getItem(HUB_SELECTED_PEER_STORAGE_KEY) || "";
    ui.peerSelect.value = savedPeer;
    state.lastSelectedPeerId = savedPeer;
  }
  if (ui.projectionSelect) {
    const savedProjection = localStorage.getItem(HUB_PROJECTION_STORAGE_KEY);
    if (savedProjection === "sphere" || savedProjection === "plane") {
      ui.projectionSelect.value = savedProjection;
    }
  }
}

function cacheElements() {
  ui.hubUrlInput = byId("orientationHubUrlInput");
  ui.hubWsInput = byId("orientationHubWsInput");
  ui.loadEmbedBtn = byId("orientationLoadEmbedBtn");
  ui.connectBtn = byId("orientationConnectBtn");
  ui.disconnectBtn = byId("orientationDisconnectBtn");
  ui.peerSelect = byId("orientationPeerSelect");
  ui.projectionSelect = byId("orientationProjectionSelect");
  ui.statusEl = byId("orientationStatus");
  ui.hubStateEl = byId("orientationHubState");
  ui.sourceMetaEl = byId("orientationSourceMeta");
  ui.streamStateEl = byId("orientationStreamState");
  ui.commandStreamEl = byId("orientationCommandStream");
  ui.streamToggleBtn = byId("orientationStreamToggleBtn");
  ui.recenterBtn = byId("orientationRecenterBtn");
  ui.resetTuneablesBtn = byId("orientationResetTuneablesBtn");
  ui.embedFrame = byId("orientationEmbedFrame");
}

function initOrientationApp() {
  if (state.initialized) {
    return;
  }
  cacheElements();

  if (!ui.hubUrlInput || !ui.hubWsInput || !ui.streamToggleBtn) {
    return;
  }

  state.initialized = true;
  hydrateDefaults();
  bindCoreUi();
  bindTuneablesUi();
  updatePeerSelectOptions();
  updateSourceMeta();
  updateStreamToggleUi();
  setHubState("Disconnected");
  setStreamState("Paused");
  setOrientationStatus("Load or connect your orientation hub");
  loadEmbeddedHub();
}

window.initOrientationApp = initOrientationApp;

const initialRoute = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
if (initialRoute === "orientation") {
  initOrientationApp();
}
