import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { KTX2Loader } from 'three/addons/loaders/KTX2Loader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
import vision from 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.0';

const { FaceLandmarker, FilesetResolver } = vision;
const streamStatusEl = document.getElementById('streamStatus');
const commandStreamEl = document.getElementById('commandStream');
const viewport = document.getElementById('morphCanvasWrap');

const COMMAND_INTERVAL_MS = 90;
let lastCommandSentAt = 0;
let lastCommandSent = "";
let faceLandmarker = null;
let videoReady = false;
let smoothed = null;
let poseBaseline = null;
let lastTrackedPose = null;
let streamPlaybackEnabled = false;
let baselinePendingOnPlay = false;

const defaultTuneables = {
  yawGain: 4.8,
  pitchGain: -7,
  rollGain: -6,
  lateralGain: 1.8,
  frontBackGain: 1.6,
  heightGain: 2.5,
  smoothAlpha: 0.45,
  commandIntervalMs: COMMAND_INTERVAL_MS,
};
const tuneables = { ...defaultTuneables };

function setStreamStatus(message, error = false) {
  streamStatusEl.textContent = message;
  streamStatusEl.style.color = error ? '#ff4444' : 'var(--accent)';
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function normalizeAngleDelta(rad) {
  let wrapped = rad;
  while (wrapped > Math.PI) {
    wrapped -= Math.PI * 2;
  }
  while (wrapped < -Math.PI) {
    wrapped += Math.PI * 2;
  }
  return wrapped;
}

function setPoseBaselineFromPose(pose) {
  poseBaseline = { ...pose };
  smoothed = null;
  setStreamStatus('Tracking active (centered)');
  logToConsole('[CAL] Morphtarget centered at current pose');
}

function setPoseBaseline(transformObj, euler) {
  setPoseBaselineFromPose({
    x: transformObj.position.x,
    y: transformObj.position.y,
    z: transformObj.position.z,
    yaw: euler.y,
    pitch: euler.x,
    roll: euler.z,
  });
}

function updateStreamToggleUi() {
  const toggleBtn = document.getElementById('streamToggleBtn');
  if (!toggleBtn) {
    return;
  }
  toggleBtn.textContent = streamPlaybackEnabled ? 'Pause Stream' : 'Play Stream';
  toggleBtn.className = streamPlaybackEnabled ? '' : 'primary';
}

function setStreamPlaybackEnabled(enabled) {
  streamPlaybackEnabled = !!enabled;

  if (streamPlaybackEnabled) {
    baselinePendingOnPlay = true;
    poseBaseline = null;
    smoothed = null;
    lastCommandSent = "";
    lastCommandSentAt = 0;

    if (lastTrackedPose) {
      setPoseBaselineFromPose(lastTrackedPose);
      baselinePendingOnPlay = false;
      commandStreamEl.textContent = "Centered at Play position";
    } else {
      setStreamStatus('Play requested - waiting for face...');
      commandStreamEl.textContent = "Waiting for face to set Play baseline...";
    }
    logToConsole('[STREAM] Morphtarget play');
  } else {
    baselinePendingOnPlay = false;
    poseBaseline = null;
    smoothed = null;
    commandStreamEl.textContent = "Paused - press Play Stream";
    setStreamStatus('Tracking paused');
    logToConsole('[STREAM] Morphtarget paused');
  }

  updateStreamToggleUi();
}

function bindTuneablePair(numberId, rangeId, key, integerValue = false) {
  const numberEl = document.getElementById(numberId);
  const rangeEl = document.getElementById(rangeId);
  if (!numberEl || !rangeEl) {
    return;
  }

  const applyRawValue = (rawValue) => {
    const parsed = integerValue ? parseInt(rawValue, 10) : parseFloat(rawValue);
    if (!Number.isFinite(parsed)) {
      return;
    }
    tuneables[key] = parsed;
    numberEl.value = String(parsed);
    rangeEl.value = String(parsed);
  };

  numberEl.addEventListener('input', () => applyRawValue(numberEl.value));
  rangeEl.addEventListener('input', () => applyRawValue(rangeEl.value));
  applyRawValue(numberEl.value || rangeEl.value || String(defaultTuneables[key]));
}

function setTuneablesToDefaults() {
  Object.assign(tuneables, defaultTuneables);

  const tuneableInputs = [
    ['tuneLateralGain', 'lateralGain'],
    ['tuneLateralGainRange', 'lateralGain'],
    ['tuneFrontBackGain', 'frontBackGain'],
    ['tuneFrontBackGainRange', 'frontBackGain'],
    ['tuneHeightGain', 'heightGain'],
    ['tuneHeightGainRange', 'heightGain'],
    ['tuneYawGain', 'yawGain'],
    ['tuneYawGainRange', 'yawGain'],
    ['tunePitchGain', 'pitchGain'],
    ['tunePitchGainRange', 'pitchGain'],
    ['tuneRollGain', 'rollGain'],
    ['tuneRollGainRange', 'rollGain'],
    ['tuneSmoothAlpha', 'smoothAlpha'],
    ['tuneSmoothAlphaRange', 'smoothAlpha'],
    ['tuneIntervalMs', 'commandIntervalMs'],
    ['tuneIntervalMsRange', 'commandIntervalMs'],
  ];

  tuneableInputs.forEach(([id, key]) => {
    const inputEl = document.getElementById(id);
    if (inputEl) {
      inputEl.value = String(tuneables[key]);
    }
  });
}

function setupTuneablesUi() {
  bindTuneablePair('tuneLateralGain', 'tuneLateralGainRange', 'lateralGain');
  bindTuneablePair('tuneFrontBackGain', 'tuneFrontBackGainRange', 'frontBackGain');
  bindTuneablePair('tuneHeightGain', 'tuneHeightGainRange', 'heightGain');
  bindTuneablePair('tuneYawGain', 'tuneYawGainRange', 'yawGain');
  bindTuneablePair('tunePitchGain', 'tunePitchGainRange', 'pitchGain');
  bindTuneablePair('tuneRollGain', 'tuneRollGainRange', 'rollGain');
  bindTuneablePair('tuneSmoothAlpha', 'tuneSmoothAlphaRange', 'smoothAlpha');
  bindTuneablePair('tuneIntervalMs', 'tuneIntervalMsRange', 'commandIntervalMs', true);

  const streamToggleBtn = document.getElementById('streamToggleBtn');
  if (streamToggleBtn) {
    streamToggleBtn.addEventListener('click', () => {
      setStreamPlaybackEnabled(!streamPlaybackEnabled);
    });
  }

  const recenterBtn = document.getElementById('recenterPoseBtn');
  if (recenterBtn) {
    recenterBtn.addEventListener('click', () => {
      if (!lastTrackedPose) {
        setStreamStatus('Cannot recenter until face is detected', true);
        return;
      }
      setPoseBaselineFromPose(lastTrackedPose);
    });
  }

  const resetBtn = document.getElementById('resetTuneablesBtn');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      setTuneablesToDefaults();
      logToConsole('[CAL] Morphtarget tuneables reset to defaults');
    });
  }

  // Morphtarget starts disconnected from command streaming.
  setStreamPlaybackEnabled(false);
}

function sendCommandToNeck(commandStr) {
  if (typeof window.sendCommand === 'function') {
    window.sendCommand(commandStr);
    return;
  }
  console.warn('sendCommand not available, command not sent:', commandStr);
}

function buildPoseCommand(transformObj, euler) {
  if (!poseBaseline) {
    return null;
  }

  const deltaX = transformObj.position.x - poseBaseline.x;
  const deltaY = transformObj.position.y - poseBaseline.y;
  const deltaZ = transformObj.position.z - poseBaseline.z;

  const deltaYaw = normalizeAngleDelta(euler.y - poseBaseline.yaw);
  const deltaPitch = normalizeAngleDelta(euler.x - poseBaseline.pitch);
  const deltaRoll = normalizeAngleDelta(euler.z - poseBaseline.roll);

  const rawLateral = -deltaX;
  const rawHeight = deltaY;
  const rawFrontBack = -deltaZ;
  const rawYaw = THREE.MathUtils.radToDeg(deltaYaw);
  const rawPitch = THREE.MathUtils.radToDeg(deltaPitch);
  const rawRoll = THREE.MathUtils.radToDeg(deltaRoll);

  const yawMRaw = rawYaw * tuneables.yawGain;
  const lateralMRaw = rawLateral * tuneables.lateralGain;
  const frontBackMRaw = rawFrontBack * tuneables.frontBackGain;
  const rollMRaw = rawRoll * tuneables.rollGain;
  const pitchMRaw = rawPitch * tuneables.pitchGain;
  const heightRaw = rawHeight * tuneables.heightGain;

  if (!smoothed) {
    smoothed = {
      yaw: yawMRaw,
      lateral: lateralMRaw,
      frontBack: frontBackMRaw,
      roll: rollMRaw,
      pitch: pitchMRaw,
      height: heightRaw,
    };
  }

  const alpha = clamp(tuneables.smoothAlpha, 0.1, 0.95);
  smoothed.yaw = alpha * yawMRaw + (1 - alpha) * smoothed.yaw;
  smoothed.lateral = alpha * lateralMRaw + (1 - alpha) * smoothed.lateral;
  smoothed.frontBack = alpha * frontBackMRaw + (1 - alpha) * smoothed.frontBack;
  smoothed.roll = alpha * rollMRaw + (1 - alpha) * smoothed.roll;
  smoothed.pitch = alpha * pitchMRaw + (1 - alpha) * smoothed.pitch;
  smoothed.height = alpha * heightRaw + (1 - alpha) * smoothed.height;

  const magnitude = Math.max(
    Math.abs(smoothed.yaw),
    Math.abs(smoothed.pitch),
    Math.abs(smoothed.roll),
    Math.abs(smoothed.lateral),
    Math.abs(smoothed.frontBack)
  );

  const sDynamic = clamp(2 - (magnitude / 600), 1, 2);
  const aDynamic = clamp(1.2 - (0.8 * (magnitude / 600)), 0.5, 1.2);

  const xVal = Math.round(clamp(smoothed.yaw, -700, 700));
  const yVal = Math.round(clamp(smoothed.lateral, -700, 700));
  const zVal = Math.round(clamp(smoothed.frontBack, -700, 700));
  const hVal = Math.round(clamp(smoothed.height, 0, 70));
  const rVal = Math.round(clamp(smoothed.roll, -700, 700));
  const pVal = Math.round(clamp(smoothed.pitch, -700, 700));

  return `X${xVal},Y${yVal},Z${zVal},H${hVal},S${sDynamic.toFixed(1)},A${aDynamic.toFixed(1)},R${rVal},P${pVal}`;
}

function resizeViewport(renderer, camera) {
  const width = Math.max(320, viewport.clientWidth || 960);
  const height = Math.max(220, viewport.clientHeight || 420);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

async function main() {
  setStreamStatus('Starting camera...');
  setupTuneablesUi();

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  viewport.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x111111);

  const camera = new THREE.PerspectiveCamera(60, 1, 1, 100);
  camera.position.z = 3.8;

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableZoom = false;
  controls.enableRotate = false;
  controls.enablePan = false;

  resizeViewport(renderer, camera);
  window.addEventListener('resize', () => resizeViewport(renderer, camera));

  const grpTransform = new THREE.Group();
  grpTransform.name = 'grp_transform';
  scene.add(grpTransform);

  const video = document.createElement('video');
  video.autoplay = true;
  video.playsInline = true;

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'user' }
    });
    video.srcObject = stream;
    await video.play();
    videoReady = true;
    setStreamStatus('Camera ready');
  } catch (err) {
    console.error('Camera error:', err);
    setStreamStatus('Camera unavailable', true);
    return;
  }

  const gltfLoader = new GLTFLoader();
  const ktx2Loader = new KTX2Loader()
    .setTranscoderPath('https://unpkg.com/three@0.152.2/examples/jsm/libs/basis/')
    .detectSupport(renderer);
  gltfLoader.setKTX2Loader(ktx2Loader);
  gltfLoader.setMeshoptDecoder(MeshoptDecoder);
  gltfLoader.load(
    'https://threejs.org/examples/models/gltf/facecap.glb',
    (gltf) => {
      const mesh = gltf.scene.children[0];
      grpTransform.add(mesh);
      const headMesh = mesh.getObjectByName('mesh_2');
      if (headMesh) {
        headMesh.material = new THREE.MeshNormalMaterial();
      }
    },
    undefined,
    (error) => {
      console.error('Error loading facecap model:', error);
    }
  );

  try {
    const filesetResolver = await FilesetResolver.forVisionTasks(
      'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.0/wasm'
    );
    faceLandmarker = await FaceLandmarker.createFromOptions(filesetResolver, {
      baseOptions: {
        modelAssetPath: 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task',
        delegate: 'GPU'
      },
      runningMode: 'VIDEO',
      numFaces: 1,
      outputFaceBlendshapes: true,
      outputFacialTransformationMatrixes: true
    });
    setStreamStatus('Tracking active');
  } catch (err) {
    console.error('MediaPipe init error:', err);
    setStreamStatus('Tracking init failed', true);
    return;
  }

  const transformObj = new THREE.Object3D();

  function animate() {
    requestAnimationFrame(animate);

    if (videoReady && faceLandmarker && video.readyState >= video.HAVE_ENOUGH_DATA) {
      const now = Date.now();
      const results = faceLandmarker.detectForVideo(video, now);

      if (results.facialTransformationMatrixes.length > 0) {
        const matrixArray = results.facialTransformationMatrixes[0].data;
        transformObj.matrix.fromArray(matrixArray);
        transformObj.matrix.decompose(
          transformObj.position,
          transformObj.quaternion,
          transformObj.scale
        );

        const euler = new THREE.Euler().setFromQuaternion(transformObj.quaternion, 'YXZ');
        lastTrackedPose = {
          x: transformObj.position.x,
          y: transformObj.position.y,
          z: transformObj.position.z,
          yaw: euler.y,
          pitch: euler.x,
          roll: euler.z,
        };

        // Keep the rendered model in camera-space using absolute pose
        // while command generation remains baseline-relative.
        grpTransform.position.x = transformObj.position.x / 10;
        grpTransform.position.y = transformObj.position.y / 10;
        grpTransform.position.z = -transformObj.position.z / -10 + 4;
        grpTransform.rotation.x = euler.x;
        grpTransform.rotation.y = euler.y;
        grpTransform.rotation.z = euler.z;

        if (streamPlaybackEnabled && baselinePendingOnPlay) {
          setPoseBaselineFromPose(lastTrackedPose);
          baselinePendingOnPlay = false;
          commandStreamEl.textContent = "Centered at Play position";
        }

        if (streamPlaybackEnabled) {
          const commandStr = buildPoseCommand(transformObj, euler);
          if (commandStr) {
            commandStreamEl.textContent = commandStr;
            setStreamStatus('Tracking active');

            if (commandStr !== lastCommandSent && now - lastCommandSentAt >= tuneables.commandIntervalMs) {
              sendCommandToNeck(commandStr);
              lastCommandSent = commandStr;
              lastCommandSentAt = now;
            }
          }
        }
      } else {
        if (streamPlaybackEnabled) {
          setStreamStatus('Face not detected');
        }
      }
    }

    controls.update();
    renderer.render(scene, camera);
  }

  animate();
}

let headstreamStarted = false;

function initHeadstreamApp() {
  if (headstreamStarted) {
    return;
  }
  headstreamStarted = true;
  main();
}

window.initHeadstreamApp = initHeadstreamApp;

const initialRoute = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
if (initialRoute === "headstream") {
  initHeadstreamApp();
}
