import * as THREE from "three";

const HUD_COLOR_HEX = 0xffae00;
const HUD_COLOR_CSS = "#ffae00";
const HUD_DEPTH = 2.35;
const GRID_BASE_OPACITY = 0.08;
const GRID_PULSE_OPACITY = 0.7;
const MAX_PIXEL_RATIO = 2;

const refs = {
  layer: null,
  touchPanel: null,
  touchOverlay: null,
  renderer: null,
  scene: null,
  camera: null,
  gridPivot: null,
  gridUniforms: null,
  hudGroup: null,
  ring: null,
  ringMat: null,
  ringBaseScale: 0.06,
  chevrons: {
    lateralLeft: null,
    lateralRight: null,
    rollLeft: null,
    rollRight: null,
  },
  resizeObserver: null,
};

const state = {
  width: 0,
  height: 0,
  frameSec: 0,
  visibility: 0,
  gridPulse: 0,
  imu: {
    targetPitch: 0,
    targetRoll: 0,
    pitch: 0,
    roll: 0,
    gyroPitch: 0,
    gyroRoll: 0,
    lastSampleMs: 0,
  },
  motion: {
    lateral: 0,
    roll: 0,
    swayX: 0,
    swayRoll: 0,
    rollBias: 0,
  },
  pointer: {
    active: false,
    ndcX: 0,
    ndcY: 0,
    pulse: 0,
    opacity: 0,
  },
};

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function num(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function isTouchModeActive() {
  if (refs.touchPanel && refs.touchPanel.dataset.hybridMode) {
    return refs.touchPanel.dataset.hybridMode === "touch";
  }
  return !!(refs.touchOverlay && refs.touchOverlay.classList.contains("active"));
}

function ndcToHud(ndcX, ndcY, depth = HUD_DEPTH) {
  const fovRad = THREE.MathUtils.degToRad(refs.camera.fov * 0.5);
  const halfH = Math.tan(fovRad) * depth;
  const halfW = halfH * refs.camera.aspect;
  return {
    x: clamp(ndcX, -1, 1) * halfW,
    y: clamp(ndcY, -1, 1) * halfH,
    z: -depth,
    halfW,
    halfH,
  };
}

function createGridMaterial() {
  return new THREE.ShaderMaterial({
    transparent: true,
    depthTest: false,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
    uniforms: {
      uColor: { value: new THREE.Color(HUD_COLOR_HEX) },
      uOpacity: { value: GRID_BASE_OPACITY },
      uScale: { value: 26.0 },
    },
    vertexShader: `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: `
      varying vec2 vUv;
      uniform vec3 uColor;
      uniform float uOpacity;
      uniform float uScale;

      float gridLine(vec2 uv, float scale) {
        vec2 coord = uv * scale;
        vec2 grid = abs(fract(coord - 0.5) - 0.5) / fwidth(coord);
        float line = min(grid.x, grid.y);
        return 1.0 - clamp(line, 0.0, 1.0);
      }

      void main() {
        float major = gridLine(vUv, uScale);
        float minor = gridLine(vUv + vec2(0.125, 0.085), uScale * 0.5) * 0.45;
        float grid = max(major, minor);
        vec2 centered = vUv * 2.0 - 1.0;
        float edgeFade = 1.0 - smoothstep(0.58, 1.06, length(centered));
        float alpha = grid * edgeFade * uOpacity;
        if (alpha <= 0.001) {
          discard;
        }
        gl_FragColor = vec4(uColor, alpha);
      }
    `,
  });
}

function createChevronSprite(direction = 1) {
  const canvas = document.createElement("canvas");
  canvas.width = 96;
  canvas.height = 96;
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.translate(canvas.width * 0.5, canvas.height * 0.5);
    if (direction < 0) {
      ctx.scale(-1, 1);
    }
    ctx.translate(-canvas.width * 0.5, -canvas.height * 0.5);
    ctx.strokeStyle = HUD_COLOR_CSS;
    ctx.lineWidth = 12;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.shadowColor = "rgba(255, 174, 0, 0.55)";
    ctx.shadowBlur = 10;
    ctx.beginPath();
    ctx.moveTo(24, 18);
    ctx.lineTo(70, 48);
    ctx.lineTo(24, 78);
    ctx.stroke();
    ctx.restore();
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.generateMipmaps = false;
  texture.minFilter = THREE.LinearFilter;
  texture.magFilter = THREE.LinearFilter;

  const material = new THREE.SpriteMaterial({
    map: texture,
    transparent: true,
    opacity: 0,
    depthTest: false,
    depthWrite: false,
  });
  const sprite = new THREE.Sprite(material);
  sprite.renderOrder = 9;
  sprite.userData.baseScale = 0.24;
  return { sprite, material, texture };
}

function applyChevronVisual(node, strength, primary, nowSec) {
  if (!node) {
    return;
  }
  const targetOpacity = strength > 0.02
    ? (primary ? 0.92 : 0.2) * strength * state.visibility
    : 0;
  node.material.opacity += (targetOpacity - node.material.opacity) * 0.24;
  const pulse = 1 + (primary ? 0.32 : 0.12) * strength * (0.5 + 0.5 * Math.sin(nowSec * 10.0));
  const scale = node.sprite.userData.baseScale * pulse;
  node.sprite.scale.set(scale, scale, 1);
  node.sprite.visible = node.material.opacity > 0.01;
}

function syncHudLayout() {
  if (!refs.camera || !refs.chevrons.lateralLeft || !refs.ring) {
    return;
  }
  const anchor = ndcToHud(0, 0);
  const lateralScale = Math.min(anchor.halfW, anchor.halfH) * 0.24;
  const rollScale = lateralScale * 0.9;

  refs.chevrons.lateralLeft.sprite.position.set(-anchor.halfW * 0.74, 0, -HUD_DEPTH);
  refs.chevrons.lateralRight.sprite.position.set(anchor.halfW * 0.74, 0, -HUD_DEPTH);
  refs.chevrons.rollLeft.sprite.position.set(-anchor.halfW * 0.34, anchor.halfH * 0.66, -HUD_DEPTH);
  refs.chevrons.rollRight.sprite.position.set(anchor.halfW * 0.34, anchor.halfH * 0.66, -HUD_DEPTH);

  refs.chevrons.lateralLeft.sprite.userData.baseScale = lateralScale;
  refs.chevrons.lateralRight.sprite.userData.baseScale = lateralScale;
  refs.chevrons.rollLeft.sprite.userData.baseScale = rollScale;
  refs.chevrons.rollRight.sprite.userData.baseScale = rollScale;

  refs.ringBaseScale = Math.max(0.04, Math.min(anchor.halfW, anchor.halfH) * 0.085);
}

function resizeRenderer(force = false) {
  if (!refs.layer || !refs.renderer || !refs.camera) {
    return;
  }
  const width = Math.max(1, refs.layer.clientWidth || refs.layer.offsetWidth || 1);
  const height = Math.max(1, refs.layer.clientHeight || refs.layer.offsetHeight || 1);
  if (!force && width === state.width && height === state.height) {
    return;
  }
  state.width = width;
  state.height = height;
  refs.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO));
  refs.renderer.setSize(width, height, false);
  refs.camera.aspect = width / height;
  refs.camera.updateProjectionMatrix();
  syncHudLayout();
}

function buildScene() {
  refs.scene = new THREE.Scene();
  refs.scene.background = null;

  refs.camera = new THREE.PerspectiveCamera(58, 1, 0.1, 30);
  refs.camera.position.set(0, 0, 3.5);
  refs.scene.add(refs.camera);

  const gridPivot = new THREE.Group();
  refs.scene.add(gridPivot);
  refs.gridPivot = gridPivot;

  const gridMaterial = createGridMaterial();
  refs.gridUniforms = gridMaterial.uniforms;

  const gridMesh = new THREE.Mesh(new THREE.PlaneGeometry(8.8, 6.4), gridMaterial);
  gridMesh.position.set(0, -0.56, -0.7);
  gridMesh.rotation.x = -Math.PI * 0.5;
  gridMesh.frustumCulled = false;
  gridMesh.renderOrder = 3;
  gridPivot.add(gridMesh);

  const hudGroup = new THREE.Group();
  refs.camera.add(hudGroup);
  refs.hudGroup = hudGroup;

  refs.chevrons.lateralLeft = createChevronSprite(-1);
  refs.chevrons.lateralRight = createChevronSprite(1);
  refs.chevrons.rollLeft = createChevronSprite(-1);
  refs.chevrons.rollRight = createChevronSprite(1);
  Object.values(refs.chevrons).forEach((entry) => {
    hudGroup.add(entry.sprite);
    entry.sprite.visible = false;
  });

  refs.ringMat = new THREE.MeshBasicMaterial({
    color: HUD_COLOR_HEX,
    transparent: true,
    opacity: 0,
    depthTest: false,
    depthWrite: false,
    side: THREE.DoubleSide,
  });
  refs.ring = new THREE.Mesh(new THREE.RingGeometry(0.045, 0.066, 48), refs.ringMat);
  refs.ring.position.set(0, 0, -HUD_DEPTH);
  refs.ring.visible = false;
  refs.ring.renderOrder = 11;
  hudGroup.add(refs.ring);
}

function onImuEvent(event) {
  const detail = event && event.detail ? event.detail : null;
  if (!detail) {
    return;
  }
  const accel = detail.accel && typeof detail.accel === "object" ? detail.accel : null;
  const gyro = detail.gyro && typeof detail.gyro === "object" ? detail.gyro : null;

  const sampleMs = [
    detail.timestampMs,
    detail.fetchedAtMs,
    accel ? accel.timestampMs : NaN,
    gyro ? gyro.timestampMs : NaN,
  ].map((value) => Number(value)).find((value) => Number.isFinite(value));
  const safeSampleMs = Number.isFinite(sampleMs) ? sampleMs : Date.now();

  let dt = 0;
  if (state.imu.lastSampleMs > 0) {
    dt = clamp((safeSampleMs - state.imu.lastSampleMs) / 1000, 0, 0.12);
  }
  state.imu.lastSampleMs = safeSampleMs;

  if (accel) {
    const ax = num(accel.x, 0);
    const ay = num(accel.y, 0);
    const az = num(accel.z, 9.81);
    const accelPitch = Math.atan2(-ax, Math.max(0.0001, Math.hypot(ay, az)));
    const accelRoll = Math.atan2(ay, Math.max(0.0001, az));
    state.imu.targetPitch = clamp(accelPitch, -1.0, 1.0);
    state.imu.targetRoll = clamp(accelRoll, -1.0, 1.0);
    state.gridPulse = Math.max(state.gridPulse, 0.34);
  }

  if (gyro && dt > 0) {
    const gx = num(gyro.x, 0);
    const gy = num(gyro.y, 0);
    const gz = num(gyro.z, 0);
    state.imu.gyroPitch = clamp((state.imu.gyroPitch * 0.985) + (-gx + gy * 0.2) * dt, -0.55, 0.55);
    state.imu.gyroRoll = clamp((state.imu.gyroRoll * 0.985) + (gz + gx * 0.1) * dt, -0.55, 0.55);
    state.gridPulse = Math.max(state.gridPulse, 0.26);
  }
}

function onDeltaEvent(event) {
  const detail = event && event.detail ? event.detail : null;
  const deltas = detail && detail.deltas && typeof detail.deltas === "object" ? detail.deltas : null;
  if (!deltas) {
    return;
  }

  const lateralDelta = num(deltas.Y, 0);
  const rollDelta = num(deltas.R, 0);

  if (Math.abs(lateralDelta) > 0.001) {
    state.motion.lateral = clamp(state.motion.lateral + (lateralDelta / 110), -1.8, 1.8);
    state.motion.swayX = clamp(state.motion.swayX + (lateralDelta / 2400), -0.7, 0.7);
    state.gridPulse = Math.max(state.gridPulse, clamp(Math.abs(lateralDelta) / 42, 0.2, 1));
  }
  if (Math.abs(rollDelta) > 0.001) {
    state.motion.roll = clamp(state.motion.roll + (rollDelta / 95), -1.8, 1.8);
    state.motion.swayRoll = clamp(state.motion.swayRoll + (rollDelta / 850), -0.6, 0.6);
    state.gridPulse = Math.max(state.gridPulse, clamp(Math.abs(rollDelta) / 50, 0.16, 0.95));
  }

  const poseRoll = detail && detail.pose ? num(detail.pose.R, 0) : 0;
  state.motion.rollBias = clamp(poseRoll / 300, -1, 1);
}

function onPointerEvent(event) {
  const detail = event && event.detail ? event.detail : null;
  if (!detail) {
    return;
  }

  const pointerType = String(detail.type || "").toLowerCase();
  if (pointerType === "start" || pointerType === "move") {
    let ndcX = Number(detail.ndcX);
    let ndcY = Number(detail.ndcY);
    if (!Number.isFinite(ndcX) || !Number.isFinite(ndcY)) {
      const nx = num(detail.normalizedX, 0.5);
      const ny = num(detail.normalizedY, 0.5);
      ndcX = (nx * 2) - 1;
      ndcY = 1 - (ny * 2);
    }
    state.pointer.ndcX = clamp(ndcX, -1, 1);
    state.pointer.ndcY = clamp(ndcY, -1, 1);
    state.pointer.active = true;
    state.pointer.pulse = 1;
    state.pointer.opacity = Math.max(state.pointer.opacity, 0.35);
    state.gridPulse = Math.max(state.gridPulse, 0.35);
    return;
  }

  state.pointer.active = false;
}

function animate(nowMs) {
  if (!refs.renderer || !refs.scene || !refs.camera) {
    return;
  }

  const nowSec = nowMs * 0.001;
  const dt = state.frameSec > 0 ? clamp(nowSec - state.frameSec, 0.001, 0.1) : (1 / 60);
  state.frameSec = nowSec;

  resizeRenderer(false);

  const visibilityTarget = isTouchModeActive() ? 1 : 0;
  state.visibility += (visibilityTarget - state.visibility) * Math.min(1, dt * 6.5);

  state.motion.lateral *= Math.exp(-dt * 4.8);
  state.motion.roll *= Math.exp(-dt * 4.7);
  state.motion.swayX *= Math.exp(-dt * 3.4);
  state.motion.swayRoll *= Math.exp(-dt * 3.1);
  state.motion.rollBias *= Math.exp(-dt * 1.8);

  state.imu.gyroPitch *= Math.exp(-dt * 2.5);
  state.imu.gyroRoll *= Math.exp(-dt * 2.5);
  state.imu.pitch += ((state.imu.targetPitch + state.imu.gyroPitch * 0.42) - state.imu.pitch) * Math.min(1, dt * 5.8);
  state.imu.roll += ((state.imu.targetRoll + state.imu.gyroRoll * 0.42) - state.imu.roll) * Math.min(1, dt * 5.8);

  state.gridPulse *= Math.exp(-dt * 2.25);
  const gridOpacity = clamp(
    (GRID_BASE_OPACITY + state.gridPulse * GRID_PULSE_OPACITY) * state.visibility,
    0,
    0.95
  );
  refs.gridUniforms.uOpacity.value = gridOpacity;
  refs.gridPivot.position.x = clamp(state.motion.swayX * 0.44, -0.42, 0.42);
  refs.gridPivot.rotation.x = state.imu.pitch * 0.9 + state.motion.swayRoll * 0.1;
  refs.gridPivot.rotation.z = -state.imu.roll * 0.9;

  const lateralStrength = clamp(Math.abs(state.motion.lateral), 0, 1);
  const lateralDir = Math.sign(state.motion.lateral);
  applyChevronVisual(refs.chevrons.lateralLeft, lateralStrength, lateralDir < 0, nowSec);
  applyChevronVisual(refs.chevrons.lateralRight, lateralStrength, lateralDir > 0, nowSec);

  const rollSignal = Math.abs(state.motion.roll) > 0.06 ? state.motion.roll : state.motion.rollBias;
  const rollStrength = clamp(Math.max(Math.abs(state.motion.roll), Math.abs(state.motion.rollBias) * 0.85), 0, 1);
  const rollDir = Math.sign(rollSignal);
  applyChevronVisual(refs.chevrons.rollLeft, rollStrength, rollDir < 0, nowSec);
  applyChevronVisual(refs.chevrons.rollRight, rollStrength, rollDir > 0, nowSec);

  state.pointer.opacity = state.pointer.active
    ? state.pointer.opacity + (1 - state.pointer.opacity) * Math.min(1, dt * 10.5)
    : state.pointer.opacity * Math.exp(-dt * 8.5);
  state.pointer.pulse *= Math.exp(-dt * 5.2);

  const pointerAnchor = ndcToHud(state.pointer.ndcX, state.pointer.ndcY);
  refs.ring.position.set(pointerAnchor.x, pointerAnchor.y, -HUD_DEPTH);
  const ringScale = refs.ringBaseScale * (1 + state.pointer.pulse * 0.45);
  refs.ring.scale.set(ringScale, ringScale, 1);
  refs.ringMat.opacity = clamp(state.pointer.opacity * 0.92 * state.visibility, 0, 0.95);
  refs.ring.visible = refs.ringMat.opacity > 0.01;

  refs.renderer.render(refs.scene, refs.camera);
  requestAnimationFrame(animate);
}

function initHud() {
  refs.layer = document.getElementById("hybridTouchHudLayer");
  refs.touchPanel = document.getElementById("hybridTabTouch");
  refs.touchOverlay = document.getElementById("hybridTouchOverlay");
  if (!refs.layer || !refs.touchPanel || !refs.touchOverlay) {
    return;
  }

  try {
    refs.renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
    });
  } catch (err) {
    return;
  }

  refs.renderer.setClearColor(0x000000, 0);
  refs.renderer.domElement.style.width = "100%";
  refs.renderer.domElement.style.height = "100%";
  refs.renderer.domElement.style.display = "block";
  refs.renderer.domElement.style.pointerEvents = "none";
  refs.layer.appendChild(refs.renderer.domElement);

  buildScene();
  resizeRenderer(true);

  window.addEventListener("resize", () => resizeRenderer(false));
  window.addEventListener("hybrid-preview-resize", () => resizeRenderer(true));
  window.addEventListener("hybrid-touch-imu", onImuEvent);
  window.addEventListener("hybrid-touch-deltas", onDeltaEvent);
  window.addEventListener("hybrid-touch-pointer", onPointerEvent);

  if (typeof ResizeObserver !== "undefined") {
    refs.resizeObserver = new ResizeObserver(() => resizeRenderer(false));
    refs.resizeObserver.observe(refs.layer);
  }

  requestAnimationFrame(animate);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initHud, { once: true });
} else {
  initHud();
}
