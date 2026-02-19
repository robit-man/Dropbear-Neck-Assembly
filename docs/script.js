// Define PI if you need quaternion math.
const PI = Math.PI;

// Defaults injected from backend config
const SERVER_DEFAULT_WS_URL = "ws://127.0.0.1:5160/ws";
const SERVER_DEFAULT_HTTP_URL = "http://127.0.0.1:5160/send_command";
const LEGACY_DEFAULT_WS_URLS = ["ws://127.0.0.1:5060/ws", "ws://127.0.0.1:5001/ws"];
const LEGACY_DEFAULT_HTTP_URLS = [
    "http://127.0.0.1:5060/send_command",
    "http://127.0.0.1:5001/send_command",
];

// Connection state - no auto-fill, only use saved or query params
let WS_URL = "";
let HTTP_URL = "";
try {
    localStorage.removeItem("wsUrl");
    localStorage.removeItem("httpUrl");
} catch (err) {}
if (WS_URL === SERVER_DEFAULT_WS_URL || LEGACY_DEFAULT_WS_URLS.includes(WS_URL)) {
    WS_URL = "";
}
if (HTTP_URL === SERVER_DEFAULT_HTTP_URL || LEGACY_DEFAULT_HTTP_URLS.includes(HTTP_URL)) {
    HTTP_URL = "";
}
let SESSION_KEY = localStorage.getItem('sessionKey') || "";
let PASSWORD = localStorage.getItem('password') || "";
let socket = null;
let useWS = false;
let authenticated = false;
let suppressCommandDispatch = false;
const ROUTE_ALIASES = Object.freeze({
    home: "auth",
    connect: "auth",
    neck: "auth",
    streams: "auth",
    audio: "auth",
    direct: "debug",
    euler: "debug",
    head: "debug",
    quaternion: "debug",
    headstream: "hybrid",
    orientation: "hybrid",
});
const ROUTES = new Set(["auth", "hybrid", "debug"]);
const CONTROL_ROUTE_LABELS = Object.freeze({
    direct: "Direct Motor",
    euler: "Euler",
    head: "Full Head",
    quaternion: "Quaternion",
});
const CONTROL_ROUTE_STORAGE_KEY = "selectedControlRoute";
let selectedControlRoute = localStorage.getItem(CONTROL_ROUTE_STORAGE_KEY) || "";
let controlsNavInitialized = false;
let controlsMenuOpen = false;
let headstreamInitTriggered = false;
let orientationInitTriggered = false;
let hybridUiInitialized = false;
let unifiedLayoutInitialized = false;
let hybridTabsInitialized = false;
let debugUiInitialized = false;
let activeHybridTab = "touch";
let activeDebugControl = "direct";
const SIDEBAR_COLLAPSED_STORAGE_KEY = "uiSidebarCollapsed";
let sidebarUiInitialized = false;
let buttonIconsObserver = null;
let buttonIconApplyInProgress = false;
let buttonIconApplyScheduled = false;

const ROUTE_ICON_KEYS = Object.freeze({
    auth: "lock",
    hybrid: "hybrid",
    debug: "sliders",
});

const CONTROL_ROUTE_ICON_KEYS = Object.freeze({
    direct: "sliders",
    euler: "axis",
    head: "head",
    quaternion: "cube",
    headstream: "spark",
    orientation: "gyro",
});

const BUTTON_ICON_BY_ID = Object.freeze({
    connectionModalCloseBtn: "close",
    routerResolveBtn: "send",
    routerRefreshInfoBtn: "refresh",
    routerScanQrBtn: "target",
    routerQrScannerStopBtn: "stop",
    controlsMenuToggleBtn: "sliders",
    pinnedStreamCloseBtn: "close",
});

const BUTTON_ICON_SVGS = Object.freeze({
    settings:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19 12a7 7 0 0 0-.07-.95l2.02-1.57-2-3.46-2.45 1a7.2 7.2 0 0 0-1.64-.95L14.5 3h-5l-.36 3.07a7.2 7.2 0 0 0-1.64.95l-2.45-1-2 3.46 2.02 1.57A7 7 0 0 0 5 12c0 .32.02.63.07.95L3.05 14.5l2 3.46 2.45-1c.5.4 1.05.72 1.64.95L9.5 21h5l.36-3.07c.59-.23 1.14-.55 1.64-.95l2.45 1 2-3.46-2.02-1.57c.05-.32.07-.63.07-.95z"></path></svg>',
    home:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 11.5 12 4l9 7.5"></path><path d="M5 10.5V20h5v-5h4v5h5v-9.5"></path></svg>',
    refresh:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"></path><path d="M21 3v6h-6"></path></svg>',
    plug:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 7v5a4 4 0 0 0 8 0V7"></path><path d="M9 3v4"></path><path d="M15 3v4"></path><path d="M12 16v5"></path></svg>',
    link:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.5 13.5 13.5 10.5"></path><path d="M7 17a4 4 0 0 1 0-5.66l2.34-2.34A4 4 0 0 1 15 14.66l-.84.84"></path><path d="M17 7a4 4 0 0 1 0 5.66l-2.34 2.34A4 4 0 0 1 9 9.34l.84-.84"></path></svg>',
    send:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 3 10 14"></path><path d="m21 3-7 18-4-7-7-4 18-7z"></path></svg>',
    play:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"></path></svg>',
    stop:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10"></rect></svg>',
    lock:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V8a4 4 0 0 1 8 0v3"></path></svg>',
    rotate:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4v6h6"></path><path d="M20 20v-6h-6"></path><path d="M20 9a8 8 0 0 0-13.66-3L4 8"></path><path d="M4 15a8 8 0 0 0 13.66 3L20 16"></path></svg>',
    check:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 13 4 4 10-10"></path></svg>',
    share:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="18" cy="5" r="3"></circle><circle cx="6" cy="12" r="3"></circle><circle cx="18" cy="19" r="3"></circle><path d="m8.6 13.5 6.8 3.9"></path><path d="m15.4 7.6-6.8 3.9"></path></svg>',
    target:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="7"></circle><circle cx="12" cy="12" r="2.5"></circle><path d="M12 2v3"></path><path d="M12 19v3"></path><path d="M2 12h3"></path><path d="M19 12h3"></path></svg>',
    grid:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M3 12h18"></path><path d="M12 4v16"></path></svg>',
    globe:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M3 12h18"></path><path d="M12 3a14 14 0 0 1 0 18"></path><path d="M12 3a14 14 0 0 0 0 18"></path></svg>',
    video:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="6" width="14" height="12" rx="2"></rect><path d="m17 10 4-2v8l-4-2"></path></svg>',
    mic:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5 11a7 7 0 0 0 14 0"></path><path d="M12 18v3"></path><path d="M8 21h8"></path></svg>',
    sliders:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16"></path><circle cx="9" cy="6" r="2"></circle><path d="M4 12h16"></path><circle cx="15" cy="12" r="2"></circle><path d="M4 18h16"></path><circle cx="11" cy="18" r="2"></circle></svg>',
    axis:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 19 19 5"></path><path d="M5 5v14h14"></path><path d="M14 5h5v5"></path></svg>',
    cube:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"></path><path d="m12 3 8 4.5-8 4.5-8-4.5z"></path><path d="M12 12v9"></path></svg>',
    spark:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v6"></path><path d="M12 15v6"></path><path d="M3 12h6"></path><path d="M15 12h6"></path><path d="m6 6 4 4"></path><path d="m14 14 4 4"></path><path d="m18 6-4 4"></path><path d="m10 14-4 4"></path></svg>',
    gyro:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="2.5"></circle><path d="M12 2a10 10 0 0 1 10 10"></path><path d="M2 12A10 10 0 0 1 12 2"></path><path d="M12 22A10 10 0 0 1 2 12"></path><path d="M22 12A10 10 0 0 1 12 22"></path></svg>',
    head:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 11a4 4 0 1 1 8 0v4a4 4 0 0 1-8 0z"></path><path d="M10 19h4"></path><path d="M7 12H5"></path><path d="M19 12h-2"></path></svg>',
    hybrid:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="8" width="14" height="9" rx="3"></rect><path d="M8 12h4"></path><path d="M10 10v4"></path><circle cx="16" cy="11" r="1"></circle><circle cx="17.5" cy="13.5" r="1"></circle></svg>',
    arrow_right:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"></path><path d="m13 6 6 6-6 6"></path></svg>',
    close:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12"></path><path d="M18 6 6 18"></path></svg>',
    pin:
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4h10l-2 6v3l3 3v2h-5v3l-2-1-2 1v-3H4v-2l3-3V10z"></path></svg>',
});

// Prevent browser zoom gestures (double-tap, pinch, ctrl+wheel) for touch control stability.
(function installViewportZoomGuards() {
    let lastTouchEndMs = 0;
    document.addEventListener("touchend", (event) => {
        const now = Date.now();
        if (now - lastTouchEndMs < 320) {
            event.preventDefault();
        }
        lastTouchEndMs = now;
    }, { passive: false });

    document.addEventListener("touchmove", (event) => {
        if (event.touches && event.touches.length > 1) {
            event.preventDefault();
        }
    }, { passive: false });

    const preventGesture = (event) => event.preventDefault();
    document.addEventListener("gesturestart", preventGesture, { passive: false });
    document.addEventListener("gesturechange", preventGesture, { passive: false });
    document.addEventListener("gestureend", preventGesture, { passive: false });

    window.addEventListener("wheel", (event) => {
        if (event.ctrlKey) {
            event.preventDefault();
        }
    }, { passive: false });

    window.addEventListener("keydown", (event) => {
        if (!(event.ctrlKey || event.metaKey)) {
            return;
        }
        const key = String(event.key || "").toLowerCase();
        if (key === "+" || key === "-" || key === "=" || key === "_" || key === "0") {
            event.preventDefault();
        }
    }, { passive: false });
})();

function routeAllowsWebSocket(route) {
    return route !== "orientation";
}

function disableWebSocketMode() {
    if (socket) {
        try {
            socket.disconnect();
        } catch (err) {
            console.warn("Socket disconnect failed:", err);
        }
        socket = null;
    }
    useWS = false;
    metrics.connected = !!authenticated;
    updateMetrics();
}

// Metrics tracking
let metrics = {
    connected: false,
    lastPing: 0,
    latency: 0,
    commandsSent: 0,
    dataRate: 0,
    lastCommandTime: 0,
    video: {
        state: "Idle",
        stats: "-- fps | -- kbps | -- clients",
        quality: "neutral"
    }
};

let controlTransportReadyState = null;
const SERVICE_AUTH_LABELS = Object.freeze({
    adapter: "Adapter",
    camera: "Camera",
    audio: "Audio",
});
const SERVICE_AUTH_SKIP_COOLDOWN_MS = 60000;
const serviceAuthQueue = [];
let serviceAuthQueueRunning = false;
let serviceAuthCurrentRequest = null;
let serviceAuthModalDom = null;
let serviceAuthModalResolver = null;
const serviceAuthSkippedAt = new Map();
const resolvedServiceAuthEndpoints = {
    adapter: "",
    camera: "",
    audio: "",
};
const SERVICE_TRANSPORT_HTTP = "http";
const SERVICE_TRANSPORT_NKN = "nkn";
const serviceTransportModes = {
    adapter: SERVICE_TRANSPORT_HTTP,
    camera: SERVICE_TRANSPORT_HTTP,
    audio: SERVICE_TRANSPORT_HTTP,
};
const serviceNknAddresses = {
    adapter: "",
    camera: "",
    audio: "",
};

function isControlTransportReady() {
    const hasSession = !!String(SESSION_KEY || "").trim();
    const hasHttp = !!String(HTTP_URL || "").trim();
    const hasNkn = !!resolveServiceNknTarget("adapter");
    const useNkn = getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN;
    return !!(authenticated && hasSession && (useNkn ? hasNkn : hasHttp));
}

function publishControlTransportState() {
    const ready = isControlTransportReady();
    if (controlTransportReadyState === ready) {
        return;
    }
    controlTransportReadyState = ready;
    window.__controlTransportReady = ready;
    document.body.classList.toggle("control-transport-ready", ready);
    window.dispatchEvent(new CustomEvent("control-transport-changed", { detail: { ready } }));
}

function getSelectedFeedStatus() {
    const feedSelect = document.getElementById("cameraFeedSelect");
    const preferredId =
      cameraPreview.targetCameraId ||
      cameraPreview.activeCameraId ||
      (feedSelect ? feedSelect.value : "");
    if (!preferredId) {
        return null;
    }
    return cameraRouterFeeds.find((feed) => feed.id === preferredId) || null;
}

function updateVideoMetrics() {
  const feed = getSelectedFeedStatus();
  if (!cameraPreview.desired && !feed) {
    metrics.video.state = "Idle";
    metrics.video.stats = "-- fps | -- kbps | -- clients";
    metrics.video.quality = "neutral";
    return;
  }

  if (cameraPreview.activeMode === STREAM_MODE_NKN) {
    const activeId = String(cameraPreview.activeCameraId || cameraPreview.targetCameraId || "").trim();
    if (cameraPreview.restartTimer) {
      metrics.video.state = activeId ? `Reconnecting ${activeId}` : "Reconnecting";
      metrics.video.stats = "NKN relay reconnecting";
      metrics.video.quality = "warning";
      return;
    }
    if (!resolveServiceNknTarget("camera")) {
      metrics.video.state = "NKN target missing";
      metrics.video.stats = "Set camera/router NKN address";
      metrics.video.quality = "error";
      return;
    }
    metrics.video.state = activeId ? `Live ${activeId}` : "Live (NKN)";
    metrics.video.stats = `${ROUTER_NKN_FRAME_MAX_KBPS} kbps cap | pull ${ROUTER_NKN_FRAME_POLL_INTERVAL_MS}ms`;
    metrics.video.quality = "good";
    return;
  }

  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    metrics.video.state = "Auth required";
    metrics.video.stats = "-- fps | -- kbps | -- clients";
    metrics.video.quality = "error";
    return;
  }

  const feedId =
    cameraPreview.targetCameraId ||
    cameraPreview.activeCameraId ||
    (feed ? feed.id : "");
    if (cameraPreview.restartTimer) {
        metrics.video.state = feedId ? `Reconnecting ${feedId}` : "Reconnecting";
        metrics.video.stats = "-- fps | -- kbps | -- clients";
        metrics.video.quality = "warning";
        return;
    }

    if (!feed) {
        metrics.video.state = feedId ? `Unavailable ${feedId}` : "Awaiting feed";
        metrics.video.stats = "-- fps | -- kbps | -- clients";
        metrics.video.quality = "warning";
        return;
    }

    const fps = Number(feed.fps) || 0;
    const kbps = Number(feed.kbps) || 0;
    const clients = Number(feed.clients) || 0;
    metrics.video.stats = `${fps.toFixed(1)} fps | ${Math.round(kbps)} kbps | ${clients} clients`;

    const requiresPersistentClient =
      cameraPreview.activeMode !== STREAM_MODE_JPEG &&
      cameraPreview.activeMode !== STREAM_MODE_NKN;
    if (!feed.online) {
        metrics.video.state = `Offline ${feed.id}`;
        metrics.video.quality = "error";
    } else if (fps > 1 && (!requiresPersistentClient || clients > 0) && cameraPreview.desired) {
        metrics.video.state = `Live ${feed.id}`;
        metrics.video.quality = "good";
    } else if (!cameraPreview.desired) {
        metrics.video.state = `Selected ${feed.id}`;
        metrics.video.quality = "neutral";
    } else {
        metrics.video.state = `Degraded ${feed.id}`;
        metrics.video.quality = "warning";
    }
}

// Common logger for the footer console.
function logToConsole(msg) {
    const consoleEl = document.getElementById('console');
    if (consoleEl) {
        const line = document.createElement('div');
        line.textContent = msg;
        consoleEl.appendChild(line);
        consoleEl.scrollTop = consoleEl.scrollHeight;
    }
}

// Update metrics display
function updateMetrics() {
    const statusEl = document.getElementById('metricStatus');
    const latencyEl = document.getElementById('metricLatency');
    const rateEl = document.getElementById('metricRate');
    const commandsEl = document.getElementById('metricCommands');
    const videoStateEl = document.getElementById('metricVideoState');
    const videoStatsEl = document.getElementById('metricVideoStats');

    if (statusEl) {
        if (metrics.connected) {
            if (getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN) {
                statusEl.textContent = 'NKN';
            } else {
                statusEl.textContent = useWS ? 'WebSocket' : 'HTTP';
            }
            statusEl.className = 'metric-value good';
        } else {
            statusEl.textContent = 'Disconnected';
            statusEl.className = 'metric-value error';
        }
    }

    if (latencyEl) {
        latencyEl.textContent = 'Latency ' + metrics.latency + 'ms';
        latencyEl.className = 'metric-meta';
    }

    if (rateEl) {
        rateEl.textContent = metrics.dataRate.toFixed(1) + ' cmd/s';
        rateEl.className = 'metric-value';
    }

    if (commandsEl) {
        commandsEl.textContent = metrics.commandsSent + ' commands';
        commandsEl.className = 'metric-meta';
    }

    updateVideoMetrics();
    if (videoStateEl) {
        videoStateEl.textContent = metrics.video.state;
        videoStateEl.className = 'metric-value';
        if (metrics.video.quality === "good") {
            videoStateEl.classList.add("good");
        } else if (metrics.video.quality === "warning") {
            videoStateEl.classList.add("warning");
        } else if (metrics.video.quality === "error") {
            videoStateEl.classList.add("error");
        }
    }
    if (videoStatsEl) {
        videoStatsEl.textContent = metrics.video.stats;
        videoStatsEl.className = 'metric-meta';
    }
    publishControlTransportState();
    updateServiceHeaderChips();
}

function paintServiceChip(chipId, label, connected, value) {
    const chipEl = document.getElementById(chipId);
    if (!chipEl) {
        return;
    }
    const isConnected = !!connected;
    const stateText = String(value || (isConnected ? "Connected" : "Offline"));
    chipEl.classList.toggle("connected", isConnected);
    chipEl.classList.toggle("disconnected", !isConnected);
    chipEl.setAttribute("aria-label", `${label} endpoint ${stateText}`);

    const labelEl = chipEl.querySelector(".service-status-label");
    const valueEl = chipEl.querySelector(".service-status-value");
    if (labelEl) {
        labelEl.textContent = label;
    }
    if (valueEl) {
        valueEl.textContent = stateText;
    } else {
        chipEl.textContent = `${label}: ${stateText}`;
    }
}

function updateServiceHeaderChips() {
    const routerConnected = !!browserNknClientReady;
    const routerValue = routerConnected
        ? "Connected"
        : "Offline";
    paintServiceChip("svcChipRouter", "Router", routerConnected, routerValue);

    const adapterUseNkn = getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN;
    const adapterReady = !!(
      authenticated &&
      SESSION_KEY &&
      (adapterUseNkn ? resolveServiceNknTarget("adapter") : HTTP_URL)
    );
    const adapterValue = adapterReady
        ? (adapterUseNkn ? "NKN" : "Connected")
        : "Offline";
    paintServiceChip("svcChipAdapter", "Adapter", adapterReady, adapterValue);

    const cameraReady = !!(cameraRouterBaseUrl && cameraRouterSessionKey);
    const cameraLive = !!(cameraReady && cameraPreview.desired && cameraPreview.activeCameraId);
    const cameraValue = cameraLive
        ? "Live"
        : (cameraReady ? "Connected" : "Offline");
    paintServiceChip("svcChipCamera", "Camera", cameraReady, cameraValue);

    const audioReady = !!(audioRouterBaseUrl && audioRouterSessionKey);
    const audioLive = !!audioBridge.active;
    const audioValue = audioLive
        ? "Live"
        : (audioReady ? "Connected" : "Offline");
    paintServiceChip("svcChipAudio", "Audio", audioReady, audioValue);

    const hybridReady = !!(adapterReady && cameraReady);
    const hybridLive = !!(hybridReady && hybridSelectedFeedId && cameraPreview.desired);
    const hybridValue = hybridLive
        ? "Live"
        : (hybridReady ? "Connected" : "Offline");
    paintServiceChip("svcChipHybrid", "Hybrid", hybridReady, hybridValue);
}

// Calculate data rate
setInterval(() => {
    const now = Date.now();
    const elapsed = (now - metrics.lastCommandTime) / 1000;
    if (elapsed > 2) {
        metrics.dataRate = 0;
    }
    updateMetrics();
}, 1000);

// All your original defaults:
const DEFAULTS = {
    'motor': 0,'yaw': 0,'pitch': 0,'roll': 0,'height': 0,
    'X': 0,'Y': 0,'Z': 0,'H': 0,'S': 0.8,'A': 0.8,'R': 0,'P': 0,
    'w': 1,'x': 0,'y': 0,'z': 0,'qH': 0,'qS': 1,'qA': 1
};

// Reset sliders/inputs back to defaults and clear the command display.
function resetSliders(options = {}) {
    const silent = !!options.silent;
    const previousSuppress = suppressCommandDispatch;
    if (silent) {
        suppressCommandDispatch = true;
    }
    try {
        document.querySelectorAll("input[type='number'], input[type='range']").forEach(input => {
            for (let k in DEFAULTS) {
                if (input.id.startsWith(k)) {
                    input.value = DEFAULTS[k];
                    input.dispatchEvent(new Event('change'));
                    break;
                }
            }
        });
        document.querySelectorAll('.current-command').forEach((el) => {
            el.textContent = "";
        });
        if (typeof resetHybridPoseState === "function") {
            resetHybridPoseState();
        }
    } finally {
        suppressCommandDispatch = previousSuppress;
    }
}

function normalizeRoute(route) {
    const normalized = String(route || "").trim().toLowerCase();
    return ROUTE_ALIASES[normalized] || normalized;
}

function isControlRoute(route) {
    const normalized = String(route || "").trim().toLowerCase();
    return Object.prototype.hasOwnProperty.call(CONTROL_ROUTE_LABELS, normalized);
}

if (!isControlRoute(selectedControlRoute)) {
    selectedControlRoute = "";
}

function updateControlGhostLabel() {
    const ghostBtn = document.getElementById("controlsCurrentRouteGhost");
    if (!ghostBtn) {
        return;
    }
    const hasSelection = isControlRoute(selectedControlRoute);
    ghostBtn.textContent = hasSelection
        ? CONTROL_ROUTE_LABELS[selectedControlRoute]
        : "No Control Selected";
    ghostBtn.classList.toggle("active-control", hasSelection);
}

function setControlsMenuOpen(open) {
    const panel = document.getElementById("controlsMenuPanel");
    const toggleBtn = document.getElementById("controlsMenuToggleBtn");
    if (!panel || !toggleBtn) {
        controlsMenuOpen = false;
        return;
    }
    controlsMenuOpen = !!open;
    panel.hidden = !controlsMenuOpen;
    toggleBtn.setAttribute("aria-expanded", controlsMenuOpen ? "true" : "false");
    toggleBtn.classList.toggle("open", controlsMenuOpen);
}

function syncControlsNavState(route) {
    const rawRoute = String(route || "").trim().toLowerCase();
    const normalized = normalizeRoute(rawRoute);
    if (isControlRoute(rawRoute)) {
        selectedControlRoute = rawRoute;
        localStorage.setItem(CONTROL_ROUTE_STORAGE_KEY, selectedControlRoute);
    }

    updateControlGhostLabel();

    const activeControl = isControlRoute(rawRoute)
        ? rawRoute
        : (isControlRoute(selectedControlRoute) ? selectedControlRoute : "");

    document.querySelectorAll(".controls-menu-option[data-control-route]").forEach((option) => {
        option.classList.toggle("active", option.dataset.controlRoute === activeControl);
    });

    const toggleBtn = document.getElementById("controlsMenuToggleBtn");
    if (toggleBtn) {
        toggleBtn.classList.toggle("active", isControlRoute(rawRoute));
    }
}

function activateControlRoute(route) {
    const normalized = String(route || "").trim().toLowerCase();
    if (!isControlRoute(normalized)) {
        return;
    }
    setControlsMenuOpen(false);
    setRoute(normalized);
}

function initializeControlsNav() {
    if (controlsNavInitialized) {
        return;
    }
    controlsNavInitialized = true;

    const navShell = document.getElementById("controlsNav");
    const toggleBtn = document.getElementById("controlsMenuToggleBtn");
    if (toggleBtn) {
        toggleBtn.addEventListener("click", (event) => {
            event.preventDefault();
            setControlsMenuOpen(!controlsMenuOpen);
        });
    }

    document.querySelectorAll(".controls-menu-option[data-control-route]").forEach((option) => {
        option.addEventListener("click", () => {
            activateControlRoute(option.dataset.controlRoute || "");
        });
        option.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                activateControlRoute(option.dataset.controlRoute || "");
            }
        });
    });

    document.addEventListener("click", (event) => {
        if (!controlsMenuOpen || !navShell) {
            return;
        }
        if (!navShell.contains(event.target)) {
            setControlsMenuOpen(false);
        }
    });

    window.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && controlsMenuOpen) {
            setControlsMenuOpen(false);
        }
    });

    setControlsMenuOpen(false);
    syncControlsNavState(getRouteFromLocation());
}

function isMobileSidebarViewport() {
    return window.matchMedia("(max-width: 900px)").matches;
}

function syncSidebarScrim() {
    const scrim = document.getElementById("sidebarScrim");
    if (!scrim) {
        document.body.classList.remove("sidebar-mobile-open");
        return;
    }
    const isOpen = !document.body.classList.contains("sidebar-collapsed");
    const show = isMobileSidebarViewport() && isOpen;
    document.body.classList.toggle("sidebar-mobile-open", show);
    scrim.hidden = !show;
}

function sidebarToggleIconMarkup(collapsed) {
    return '<span class="sidebar-toggle-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m6 6 12 12"></path><path d="m18 6-12 12"></path></svg></span>';
}

function setSidebarCollapsed(collapsed, options = {}) {
    const shouldPersist = options.persist !== false;
    const nextCollapsed = !!collapsed;
    document.body.classList.toggle("sidebar-collapsed", nextCollapsed);

    const toggleBtn = document.getElementById("sidebarToggleBtn");
    if (toggleBtn) {
        toggleBtn.innerHTML = sidebarToggleIconMarkup(false);
        toggleBtn.setAttribute("aria-expanded", nextCollapsed ? "false" : "true");
        toggleBtn.setAttribute("aria-label", "Close sidebar");
        toggleBtn.setAttribute("title", "Close sidebar");
    }

    syncSidebarScrim();

    if (shouldPersist) {
        try {
            localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, nextCollapsed ? "1" : "0");
        } catch (err) {}
    }
}

const sidebarSwipeState = {
    active: false,
    pointerId: null,
    startX: 0,
    startY: 0,
};

function installSidebarSwipeGesture() {
    const zone = document.getElementById("sidebarSwipeZone");
    if (!zone) {
        return;
    }

    const reset = () => {
        sidebarSwipeState.active = false;
        sidebarSwipeState.pointerId = null;
        sidebarSwipeState.startX = 0;
        sidebarSwipeState.startY = 0;
    };

    const onPointerDown = (event) => {
        if (!document.body.classList.contains("sidebar-collapsed")) {
            return;
        }
        if (event.pointerType === "mouse" && event.button !== 0) {
            return;
        }
        sidebarSwipeState.active = true;
        sidebarSwipeState.pointerId = event.pointerId;
        sidebarSwipeState.startX = event.clientX;
        sidebarSwipeState.startY = event.clientY;
        try {
            zone.setPointerCapture(event.pointerId);
        } catch (err) {}
    };

    const onPointerMove = (event) => {
        if (!sidebarSwipeState.active || sidebarSwipeState.pointerId !== event.pointerId) {
            return;
        }
        const dx = Number(event.clientX) - sidebarSwipeState.startX;
        const dy = Number(event.clientY) - sidebarSwipeState.startY;
        if (dx > 54 && Math.abs(dy) < Math.max(44, dx * 0.8)) {
            setSidebarCollapsed(false);
            reset();
        }
    };

    const onPointerStop = (event) => {
        if (!sidebarSwipeState.active || sidebarSwipeState.pointerId !== event.pointerId) {
            return;
        }
        reset();
    };

    zone.addEventListener("pointerdown", onPointerDown);
    zone.addEventListener("pointermove", onPointerMove);
    zone.addEventListener("pointerup", onPointerStop);
    zone.addEventListener("pointercancel", onPointerStop);
    zone.addEventListener("pointerleave", onPointerStop);
}

function initializeSidebarUi() {
    if (sidebarUiInitialized) {
        return;
    }
    sidebarUiInitialized = true;

    const toggleBtn = document.getElementById("sidebarToggleBtn");
    const scrim = document.getElementById("sidebarScrim");
    if (!toggleBtn) {
        return;
    }

    let storedCollapsed = null;
    try {
        const raw = localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY);
        if (raw === "1") {
            storedCollapsed = true;
        } else if (raw === "0") {
            storedCollapsed = false;
        }
    } catch (err) {}

    const defaultCollapsed = window.innerWidth <= 900;
    setSidebarCollapsed(storedCollapsed === null ? defaultCollapsed : storedCollapsed, { persist: false });

    toggleBtn.addEventListener("click", (event) => {
        event.preventDefault();
        setSidebarCollapsed(true);
    });

    if (scrim) {
        scrim.addEventListener("click", () => {
            setSidebarCollapsed(true, { persist: false });
        });
    }

    installSidebarSwipeGesture();

    const collapseOnSelect = () => {
        if (isMobileSidebarViewport()) {
            setSidebarCollapsed(true);
        }
    };
    document.querySelectorAll(".nav-link[data-route]").forEach((node) => {
        node.addEventListener("click", collapseOnSelect);
    });
    document.querySelectorAll(".controls-menu-option[data-control-route]").forEach((node) => {
        node.addEventListener("click", collapseOnSelect);
    });

    window.addEventListener("resize", () => {
        setSidebarCollapsed(document.body.classList.contains("sidebar-collapsed"), { persist: false });
    });

    window.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && isMobileSidebarViewport() && !document.body.classList.contains("sidebar-collapsed")) {
            setSidebarCollapsed(true, { persist: false });
        }
    });
}

function normalizeButtonText(value) {
    return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function inferButtonIconKey(element) {
    if (!element) {
        return "";
    }

    const elementId = String(element.id || "").trim();
    if (elementId && BUTTON_ICON_BY_ID[elementId]) {
        return BUTTON_ICON_BY_ID[elementId];
    }

    const route = String(element.dataset.route || "").trim().toLowerCase();
    if (route && ROUTE_ICON_KEYS[route]) {
        return ROUTE_ICON_KEYS[route];
    }

    const controlRoute = String(element.dataset.controlRoute || "").trim().toLowerCase();
    if (controlRoute && CONTROL_ROUTE_ICON_KEYS[controlRoute]) {
        return CONTROL_ROUTE_ICON_KEYS[controlRoute];
    }

    const text = normalizeButtonText(element.textContent);
    if (!text || text === "+" || text === "-" || text === "<" || text === ">" || text === "x" || text === "×") {
        return "";
    }
    if (text.includes("settings")) return "settings";
    if (text.includes("home")) return "home";
    if (text.includes("normalize")) return "link";
    if (text.includes("reconnect")) return "refresh";
    if (text.includes("connect")) return "plug";
    if (text.includes("resolve")) return "send";
    if (text === "send" || text.startsWith("send ")) return "send";
    if (text.includes("authenticate") || text.includes("auth")) return "lock";
    if (text.includes("refresh")) return "refresh";
    if (text.includes("rotate")) return "rotate";
    if (text.includes("apply")) return "check";
    if (text.includes("copied")) return "check";
    if (text.includes("share")) return "share";
    if (text.includes("stop")) return "stop";
    if (text.includes("start") || text.includes("play")) return "play";
    if (text.includes("reset")) return "refresh";
    if (text.includes("recenter")) return "target";
    if (text.includes("planar")) return "grid";
    if (text.includes("globe")) return "globe";
    if (text.includes("proceed")) return "arrow_right";
    if (text.includes("close")) return "close";
    return "";
}

function applyIconToElement(element, iconKey) {
    if (!element || !iconKey || !BUTTON_ICON_SVGS[iconKey]) {
        return;
    }
    if (element.id === "sidebarToggleBtn") {
        return;
    }
    if (element.classList.contains("hybrid-arrow")) {
        return;
    }
    if (element.classList.contains("stream-pin-btn")) {
        return;
    }
    if (element.querySelector("svg") && !element.querySelector(".ui-btn-icon")) {
        return;
    }

    const labelText = String(element.textContent || "").trim();
    if (!labelText) {
        return;
    }

    element.textContent = "";
    const iconNode = document.createElement("span");
    iconNode.className = "ui-btn-icon";
    iconNode.innerHTML = BUTTON_ICON_SVGS[iconKey];

    element.appendChild(iconNode);
    const compactCloseLabel = iconKey === "close" && (labelText === "x" || labelText === "X" || labelText === "×");
    if (!compactCloseLabel) {
        const labelNode = document.createElement("span");
        labelNode.className = "ui-btn-label";
        labelNode.textContent = labelText;
        element.appendChild(labelNode);
    }
    element.classList.add("has-ui-icon");
}

function applyActionIcons() {
    if (buttonIconApplyInProgress) {
        return;
    }
    buttonIconApplyInProgress = true;
    try {
        const targets = document.querySelectorAll("button, .nav-link[data-route], .controls-menu-option[data-control-route]");
        targets.forEach((element) => {
            if (element.classList.contains("has-ui-icon") && element.querySelector(".ui-btn-icon")) {
                return;
            }
            const iconKey = inferButtonIconKey(element);
            applyIconToElement(element, iconKey);
        });
    } finally {
        buttonIconApplyInProgress = false;
    }
}

function scheduleActionIconRefresh() {
    if (buttonIconApplyScheduled) {
        return;
    }
    buttonIconApplyScheduled = true;
    setTimeout(() => {
        buttonIconApplyScheduled = false;
        applyActionIcons();
    }, 0);
}

function initializeActionIcons() {
    applyActionIcons();
    if (buttonIconsObserver || !document.body) {
        return;
    }
    buttonIconsObserver = new MutationObserver(() => {
        if (buttonIconApplyInProgress) {
            return;
        }
        scheduleActionIconRefresh();
    });
    buttonIconsObserver.observe(document.body, {
        subtree: true,
        childList: true,
        characterData: true,
    });
}

function getRouteFromLocation() {
    const rawHashRoute = String(window.location.hash.replace(/^#\/?/, "") || "").trim().toLowerCase();
    if (isControlRoute(rawHashRoute)) {
        activeDebugControl = rawHashRoute;
        return "debug";
    }
    if (rawHashRoute === "headstream") {
        activeHybridTab = "morph";
        return "hybrid";
    }
    if (rawHashRoute === "orientation") {
        activeHybridTab = "orientation";
        return "hybrid";
    }
    const hashRoute = normalizeRoute(rawHashRoute);
    if (ROUTES.has(hashRoute)) {
        return hashRoute;
    }
    const pathRoute = String(window.location.pathname.split("/").filter(Boolean).pop() || "").trim().toLowerCase();
    if (isControlRoute(pathRoute)) {
        activeDebugControl = pathRoute;
        return "debug";
    }
    if (pathRoute === "headstream") {
        activeHybridTab = "morph";
        return "hybrid";
    }
    if (pathRoute === "orientation") {
        activeHybridTab = "orientation";
        return "hybrid";
    }
    const normalizedPathRoute = normalizeRoute(pathRoute || "");
    if (normalizedPathRoute && ROUTES.has(normalizedPathRoute)) {
        return normalizedPathRoute;
    }
    return "auth";
}

function setStreamsPanelFocus(targetPanel = "camera") {
    const cameraDetails = document.getElementById("streamsCameraDetails");
    const audioDetails = document.getElementById("streamsAudioDetails");
    if (!cameraDetails || !audioDetails) {
        return;
    }
    if (targetPanel === "audio") {
        audioDetails.open = true;
        cameraDetails.open = false;
        return;
    }
    cameraDetails.open = true;
}

function setHybridTab(tab, options = {}) {
    const requested = String(tab || "").trim().toLowerCase();
    const nextTab = requested === "morph" || requested === "orientation" ? requested : "touch";
    activeHybridTab = nextTab;

    document.querySelectorAll(".hybrid-tab[data-hybrid-tab]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.hybridTab === nextTab);
    });
    const touchPanel = document.getElementById("hybridTabTouch");
    if (touchPanel) {
        touchPanel.classList.add("active");
        touchPanel.dataset.hybridMode = nextTab;
    }
    document.querySelectorAll(".hybrid-mode-overlay[data-overlay-mode]").forEach((overlay) => {
        const isActive = overlay.dataset.overlayMode === nextTab;
        overlay.classList.toggle("active", isActive);
        overlay.setAttribute("aria-hidden", isActive ? "false" : "true");
    });
    document.querySelectorAll(".hybrid-mode-toggle").forEach((btn) => {
        const mode = String(btn.dataset.hybridModeToggle || "").trim();
        const isActive = mode === nextTab;
        btn.classList.toggle("active", isActive);
        btn.disabled = !isActive;
        btn.hidden = !isActive;
        btn.setAttribute("aria-hidden", isActive ? "false" : "true");
    });
    document.querySelectorAll(".hybrid-tuneables-panel[data-hybrid-tuneables-mode]").forEach((panel) => {
        const isActive = panel.dataset.hybridTuneablesMode === nextTab;
        panel.classList.toggle("active", isActive);
        panel.hidden = !isActive;
    });

    if (nextTab === "morph" && !headstreamInitTriggered && typeof window.initHeadstreamApp === "function") {
        window.initHeadstreamApp();
        headstreamInitTriggered = true;
    }
    if (nextTab === "orientation" && !orientationInitTriggered && typeof window.initOrientationApp === "function") {
        window.initOrientationApp();
        orientationInitTriggered = true;
    }

    const modeStatusEl = document.getElementById("hybridModeStatus");
    if (modeStatusEl) {
        if (nextTab === "touch") {
            modeStatusEl.textContent = "Touch control overlay active";
            modeStatusEl.style.color = "var(--accent)";
        } else if (nextTab === "morph") {
            modeStatusEl.textContent = "Morphtarget overlay ready";
            modeStatusEl.style.color = "var(--accent)";
        } else if (nextTab === "orientation") {
            modeStatusEl.textContent = "Orientation overlay ready";
            modeStatusEl.style.color = "var(--accent)";
        }
    }

    window.dispatchEvent(new CustomEvent("hybrid-preview-resize", {
        detail: { mode: nextTab, source: "hybrid-tab" },
    }));

    if (!options.skipMetrics) {
        updateMetrics();
    }
}

function initializeHybridTabs() {
    if (hybridTabsInitialized) {
        return;
    }
    hybridTabsInitialized = true;
    document.querySelectorAll(".hybrid-tab[data-hybrid-tab]").forEach((btn) => {
        btn.addEventListener("click", () => {
            setHybridTab(btn.dataset.hybridTab || "touch");
        });
    });
    setHybridTab(activeHybridTab, { skipMetrics: true });
}

function setDebugControlTab(controlRoute) {
    const requested = String(controlRoute || "").trim().toLowerCase();
    const next = isControlRoute(requested) ? requested : "direct";
    activeDebugControl = next;
    selectedControlRoute = next;
    localStorage.setItem(CONTROL_ROUTE_STORAGE_KEY, selectedControlRoute);

    document.querySelectorAll(".debug-tab[data-debug-control]").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.debugControl === next);
    });
    document.querySelectorAll(".debug-control-panel[data-debug-control]").forEach((panel) => {
        panel.classList.toggle("active", panel.dataset.debugControl === next);
    });
    updateMetrics();
}

function initializeDebugTabs() {
    if (debugUiInitialized) {
        return;
    }
    debugUiInitialized = true;
    document.querySelectorAll(".debug-tab[data-debug-control]").forEach((btn) => {
        btn.addEventListener("click", () => {
            setDebugControlTab(btn.dataset.debugControl || "direct");
        });
    });
    setDebugControlTab(activeDebugControl);
}

function applyRoute(route) {
    setControlsMenuOpen(false);
    const displayRoute = ROUTES.has(route) ? route : "auth";
    document.querySelectorAll("[data-view]").forEach((view) => {
        view.classList.toggle("active", view.dataset.view === displayRoute);
    });
    document.querySelectorAll(".nav-link[data-route]").forEach((link) => {
        link.classList.toggle("active", link.dataset.route === displayRoute);
    });
    syncControlsNavState(route);

    if (displayRoute === "auth") {
        setupStreamConfigUi();
        initializeDebugAudioActions();
        initializeDebugCameraActions();
    }
    if (displayRoute === "hybrid") {
        setupStreamConfigUi();
        setupHybridUi();
        initializeHybridTabs();
        hybridSyncPoseFromHeadControls();
        renderHybridReadout();
        if (cameraRouterBaseUrl && cameraRouterSessionKey && cameraRouterFeeds.length === 0) {
            refreshCameraFeeds({ silent: true, suppressErrors: true }).catch(() => {});
        } else {
            renderHybridFeedOptions();
        }
    }
    if (displayRoute === "debug") {
        setupStreamConfigUi();
        initializeDebugTabs();
        initializeDebugAudioActions();
        initializeDebugCameraActions();
    }

    if (
        routeAllowsWebSocket(displayRoute) &&
        WS_URL &&
        SESSION_KEY &&
        authenticated &&
        (!socket || socket.disconnected)
    ) {
        initWebSocket();
    }

    if (isMobileSidebarViewport()) {
        setSidebarCollapsed(true);
    }
    updateMetrics();
}

function setRoute(route, updateHash = true) {
    const rawRoute = String(route || "").trim().toLowerCase();
    if (isControlRoute(rawRoute)) {
        activeDebugControl = rawRoute;
    }
    if (rawRoute === "headstream") {
        activeHybridTab = "morph";
    } else if (rawRoute === "orientation") {
        activeHybridTab = "orientation";
    }
    const normalizedRoute = normalizeRoute(rawRoute);
    const normalized = ROUTES.has(normalizedRoute) ? normalizedRoute : "auth";
    applyRoute(normalized);
    if (updateHash && window.location.hash !== `#${normalized}`) {
        window.location.hash = `#${normalized}`;
    }
}

function initializeRouting() {
    const initialRoute = getRouteFromLocation();
    applyRoute(initialRoute);

    if (!window.location.hash) {
        window.location.hash = `#${initialRoute}`;
    }

    window.addEventListener("hashchange", () => {
        setRoute(getRouteFromLocation(), false);
    });
}

function collectDirectChildSections(parentNode, className = "control-section") {
    if (!parentNode) {
        return [];
    }
    return Array.from(parentNode.children || []).filter((node) => {
        return !!(node && node.classList && node.classList.contains(className));
    });
}

function moveNodeToMount(node, mount) {
    if (!node || !mount) {
        return;
    }
    mount.appendChild(node);
}

function reorganizeUnifiedViews() {
    if (unifiedLayoutInitialized) {
        return;
    }
    unifiedLayoutInitialized = true;

    const authRouterMount = document.getElementById("authRouterMount");
    const authAdapterMount = document.getElementById("authAdapterMount");
    const authCameraMount = document.getElementById("authCameraMount");
    const authAudioMount = document.getElementById("authAudioMount");
    const hybridAudioMount = document.getElementById("hybridAudioMount");
    const hybridModeActionMount = document.getElementById("hybridModeActionMount");
    const hybridMorphSceneMount = document.getElementById("hybridMorphSceneMount");
    const hybridOrientationSceneMount = document.getElementById("hybridOrientationSceneMount");
    const hybridMorphTuneablesMount = document.getElementById("hybridMorphTuneablesMount");
    const hybridOrientationTuneablesMount = document.getElementById("hybridOrientationTuneablesMount");
    const debugControlMount = document.getElementById("debugControlMount");
    const debugCameraMount = document.getElementById("debugCameraMount");
    const debugAudioMount = document.getElementById("debugAudioMount");

    const routerDiscoverySection = document.querySelector("#connectionModal .modal-section");
    moveNodeToMount(routerDiscoverySection, authRouterMount);

    const neckView = document.querySelector('[data-view="neck"]');
    const neckSections = collectDirectChildSections(neckView);
    neckSections.forEach((section) => moveNodeToMount(section, authAdapterMount));

    const cameraBody = document.querySelector("#streamsCameraDetails .streams-panel-body");
    const cameraSections = collectDirectChildSections(cameraBody);
    cameraSections.forEach((section, index) => {
        if (index === 0) {
            moveNodeToMount(section, authCameraMount);
            return;
        }
        moveNodeToMount(section, debugCameraMount);
    });
    // Enforce Mode and Feed placement in Debug > Camera Stream Test, above Preview.
    const modeAndFeedSection = Array.from(document.querySelectorAll("#streamsCameraDetails .control-section")).find((section) => {
        const heading = section.querySelector("h3");
        return heading && heading.textContent.trim() === "Mode and Feed";
    });
    if (modeAndFeedSection && debugCameraMount) {
        const previewSection = Array.from(debugCameraMount.children || []).find((child) => {
            const heading = child && child.querySelector ? child.querySelector("h3") : null;
            return !!(heading && heading.textContent.trim() === "Preview");
        });
        if (previewSection) {
            debugCameraMount.insertBefore(modeAndFeedSection, previewSection);
        } else {
            debugCameraMount.prepend(modeAndFeedSection);
        }
    }

    const audioBody = document.querySelector("#streamsAudioDetails .streams-panel-body");
    const audioSections = collectDirectChildSections(audioBody);
    audioSections.forEach((section, index) => {
        if (index === 0) {
            moveNodeToMount(section, authAudioMount);
            return;
        }
        if (index === 1) {
            moveNodeToMount(section, debugAudioMount);
            return;
        }
        moveNodeToMount(section, hybridAudioMount);
    });

    const headstreamView = document.querySelector('[data-view="headstream"]');
    if (headstreamView) {
        const headstreamSections = collectDirectChildSections(headstreamView);
        const morphCanvas = headstreamView.querySelector("#morphCanvasWrap");
        const morphToggleBtn = headstreamView.querySelector("#streamToggleBtn");
        if (morphCanvas) {
            moveNodeToMount(morphCanvas, hybridMorphSceneMount);
        }
        if (morphToggleBtn) {
            morphToggleBtn.classList.add("hybrid-mode-toggle");
            morphToggleBtn.dataset.hybridModeToggle = "morph";
            morphToggleBtn.hidden = true;
            moveNodeToMount(morphToggleBtn, hybridModeActionMount);
        }
        if (headstreamSections.length > 1) {
            moveNodeToMount(headstreamSections[1], hybridMorphTuneablesMount);
        }
    }

    const orientationView = document.querySelector('[data-view="orientation"]');
    if (orientationView) {
        const orientationSections = collectDirectChildSections(orientationView);
        const orientationSceneHost = orientationView.querySelector("#orientationSceneHost");
        const orientationToggleBtn = orientationView.querySelector("#orientationStreamToggleBtn");
        const projectionPlaneBtn = orientationView.querySelector("#orientationProjectionPlaneBtn");
        const projectionSphereBtn = orientationView.querySelector("#orientationProjectionSphereBtn");
        const orientationStatus = orientationView.querySelector("#orientationStatus");
        if (orientationSceneHost) {
            moveNodeToMount(orientationSceneHost, hybridOrientationSceneMount);
        }
        if (orientationToggleBtn) {
            orientationToggleBtn.classList.add("hybrid-mode-toggle");
            orientationToggleBtn.dataset.hybridModeToggle = "orientation";
            orientationToggleBtn.hidden = true;
            moveNodeToMount(orientationToggleBtn, hybridModeActionMount);
        }
        if (projectionPlaneBtn && projectionSphereBtn && hybridOrientationTuneablesMount) {
            const projectionRow = document.createElement("div");
            projectionRow.className = "row";
            projectionRow.appendChild(projectionPlaneBtn);
            projectionRow.appendChild(projectionSphereBtn);
            hybridOrientationTuneablesMount.appendChild(projectionRow);
        }
        if (orientationStatus && hybridOrientationTuneablesMount) {
            orientationStatus.classList.add("hybrid-mode-status");
            hybridOrientationTuneablesMount.appendChild(orientationStatus);
        }
        if (orientationSections.length > 1) {
            moveNodeToMount(orientationSections[1], hybridOrientationTuneablesMount);
        }
    }

    if (debugControlMount) {
        const debugRoutes = ["direct", "euler", "head", "quaternion"];
        debugRoutes.forEach((controlRoute) => {
            const sourceView = document.querySelector(`[data-view="${controlRoute}"]`);
            const panel = document.createElement("div");
            panel.className = "debug-control-panel";
            panel.dataset.debugControl = controlRoute;
            const sections = collectDirectChildSections(sourceView);
            sections.forEach((section) => panel.appendChild(section));
            debugControlMount.appendChild(panel);
        });
    }

    const modal = document.getElementById("connectionModal");
    if (modal) {
        modal.remove();
    }
}

let debugAudioActionsInitialized = false;
let debugCameraActionsInitialized = false;
function setDebugAudioStatus(message, error = false) {
    const statusEl = document.getElementById("debugAudioStatus");
    if (!statusEl) {
        return;
    }
    statusEl.textContent = String(message || "");
    statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function setDebugCameraRecoveryStatus(message, error = false) {
    const statusEl = document.getElementById("debugCameraCycleStatus");
    if (!statusEl) {
        return;
    }
    statusEl.textContent = String(message || "");
    statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function syncCheckboxToggleButtonState(checkboxEl) {
    if (!checkboxEl) {
        return;
    }
    const buttonLabel = checkboxEl.closest(".toggle-checkbox-btn");
    if (!buttonLabel) {
        return;
    }
    const onText = String(buttonLabel.dataset.onText || "On");
    const offText = String(buttonLabel.dataset.offText || "Off");
    const isOn = !!checkboxEl.checked;

    buttonLabel.classList.toggle("is-on", isOn);
    buttonLabel.classList.toggle("is-off", !isOn);
    buttonLabel.setAttribute("aria-pressed", isOn ? "true" : "false");

    const textEl = buttonLabel.querySelector(".toggle-checkbox-btn-text");
    if (textEl) {
        textEl.textContent = isOn ? onText : offText;
    }
}

function initializeCheckboxToggleButtons(rootNode = document) {
    const root = rootNode || document;
    if (!root || typeof root.querySelectorAll !== "function") {
        return;
    }
    root.querySelectorAll(".toggle-checkbox-btn input[type='checkbox']").forEach((checkboxEl) => {
        if (!checkboxEl.dataset.toggleButtonInit) {
            checkboxEl.addEventListener("change", () => {
                syncCheckboxToggleButtonState(checkboxEl);
            });
            checkboxEl.dataset.toggleButtonInit = "1";
        }
        syncCheckboxToggleButtonState(checkboxEl);
    });
}

function initializeDebugAudioActions() {
    if (debugAudioActionsInitialized) {
        return;
    }
    debugAudioActionsInitialized = true;
    installAudioPlaybackGestureHooks();

    const startBtn = document.getElementById("debugAudioStartBtn");
    const stopBtn = document.getElementById("debugAudioStopBtn");
    const refreshBtn = document.getElementById("debugAudioRefreshBtn");
    if (startBtn) {
        startBtn.addEventListener("click", () => {
            requestAudioAutoplayUnlock().catch(() => {});
            startAudioBridge({ forceRestart: false })
                .then(() => setDebugAudioStatus("Debug start requested"))
                .catch((err) => setDebugAudioStatus(`Start failed: ${err}`, true));
        });
    }
    if (stopBtn) {
        stopBtn.addEventListener("click", () => {
            stopAudioBridge({ keepDesired: false, silent: false })
                .then(() => setDebugAudioStatus("Debug stop requested"))
                .catch((err) => setDebugAudioStatus(`Stop failed: ${err}`, true));
        });
    }
    if (refreshBtn) {
        refreshBtn.addEventListener("click", () => {
            refreshAudioDevices({ silent: false })
                .then((ok) => setDebugAudioStatus(ok ? "Audio devices refreshed" : "Audio device refresh failed", !ok))
                .catch((err) => setDebugAudioStatus(`Refresh failed: ${err}`, true));
        });
    }
}

function initializeDebugCameraActions() {
    if (debugCameraActionsInitialized) {
        return;
    }
    debugCameraActionsInitialized = true;

    const cycleBtn = document.getElementById("debugCameraCycleBtn");
    const forceToggle = document.getElementById("debugCameraForceRecovery");
    if (cycleBtn) {
        cycleBtn.addEventListener("click", () => {
            const forceRecover = !(forceToggle && forceToggle.checked === false);
            cycleCameraAccessRecovery({
                forceRecover,
                trigger: "debug-button",
            }).catch(() => {});
        });
    }
}

// Send HOME command and then reset UI.
function sendHomeCommand() {
    sendCommand("HOME_BRUTE");
    resetSliders({silent: true});
    logToConsole("Sent HOME_BRUTE command");
}

// Send soft HOME command and then reset UI.
function sendHomeSoftCommand() {
    sendCommand("HOME_SOFT");
    resetSliders({silent: true});
    logToConsole("Sent HOME_SOFT command");
}

function getAdapterOrigin() {
    if (getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN) {
        return "";
    }
    if (!HTTP_URL) {
        return null;
    }
    try {
        const parsedHttpUrl = new URL(HTTP_URL.includes("://") ? HTTP_URL : `https://${HTTP_URL}`);
        return parsedHttpUrl.origin;
    } catch (err) {
        return null;
    }
}

function normalizeServicePath(path) {
    const raw = String(path || "").trim();
    if (!raw) {
        return "/";
    }
    if (raw.startsWith("/")) {
        return raw;
    }
    return `/${raw}`;
}

async function adapterApiFetch(path, options = {}) {
    const normalizedPath = normalizeServicePath(path);
    if (getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN) {
        return requestServiceRpcViaNkn("adapter", normalizedPath, options);
    }
    const adapterOrigin = getAdapterOrigin();
    if (!adapterOrigin) {
        throw new Error("Adapter HTTP endpoint is not configured");
    }
    return fetch(`${adapterOrigin}${normalizedPath}`, options);
}

async function resetAdapterPort(triggerHome = false, homeCommand = "HOME") {
    if (!SESSION_KEY) {
        logToConsole("[ERROR] No session key - please authenticate first");
        showConnectionModal();
        return;
    }

    logToConsole("[RESET] Resetting adapter serial port...");
    try {
        const response = await adapterApiFetch("/serial_reset", {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                session_key: SESSION_KEY,
                trigger_home: !!triggerHome,
                home_command: homeCommand
            })
        });

        let data = {};
        try {
            data = await response.json();
        } catch (jsonErr) {}

        if (!response.ok || data.status !== 'success') {
            const msg = data.message || `HTTP ${response.status}`;
            logToConsole("[ERROR] Serial reset failed: " + msg);
            return;
        }

        const homeSent = data.home_sent ? ` + ${data.home_sent}` : "";
        logToConsole(`[OK] Serial reset complete${homeSent}`);
        resetSliders({silent: true});
    } catch (err) {
        logToConsole("[ERROR] Serial reset request failed: " + err);
    }
}

// Authenticate with adapter
async function authenticate(password, wsUrl, httpUrl) {
    const startTime = Date.now();
    const endpointHint = String(httpUrl || wsUrl || "").trim();
    const parsedHint = parseServiceEndpoint(endpointHint);
    const shouldUseNkn =
      parsedHint.transport === SERVICE_TRANSPORT_NKN ||
      getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN;
    try {
        let response = null;
        if (shouldUseNkn) {
            const nknAddress = normalizeNknAddress(
              parsedHint.nknAddress ||
              endpointHint ||
              resolveServiceNknTarget("adapter")
            );
            if (!nknAddress) {
                logToConsole("[ERROR] Adapter NKN address is not configured");
                return false;
            }
            setServiceTransportMode("adapter", SERVICE_TRANSPORT_NKN);
            setServiceNknAddress("adapter", nknAddress);
            response = await requestServiceRpcViaNkn(
              "adapter",
              "/auth",
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password }),
              }
            );
            WS_URL = "";
            HTTP_URL = "";
            localStorage.removeItem("wsUrl");
            localStorage.removeItem("httpUrl");
        } else {
            let authUrl;
            try {
                const parsedHttpUrl = new URL(httpUrl.includes("://") ? httpUrl : `https://${httpUrl}`);
                authUrl = `${parsedHttpUrl.origin}/auth`;
            } catch (urlErr) {
                logToConsole("[ERROR] Invalid HTTP URL: " + httpUrl);
                return false;
            }
            setServiceTransportMode("adapter", SERVICE_TRANSPORT_HTTP);
            setServiceNknAddress("adapter", "");
            response = await fetch(authUrl, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({password: password})
            });
        }
        const data = await response.json();

        if (data.status === 'success') {
            SESSION_KEY = data.session_key;
            PASSWORD = password;
            localStorage.setItem('sessionKey', SESSION_KEY);
            localStorage.setItem('password', password);
            if (getServiceTransportMode("adapter") !== SERVICE_TRANSPORT_NKN) {
                localStorage.setItem('wsUrl', wsUrl);
                localStorage.setItem('httpUrl', httpUrl);
            }

            metrics.latency = Date.now() - startTime;
            metrics.connected = true;
            authenticated = true;

            logToConsole("[OK] Authenticated successfully");
            updateMetrics();
            return true;
        } else {
            logToConsole("[ERROR] Authentication failed: " + data.message);
            return false;
        }
    } catch (err) {
        logToConsole("[ERROR] Authentication error: " + err);
        return false;
    }
}

// Centralized sendCommand: whichever path is currently active.
async function sendCommand(command) {
    if (suppressCommandDispatch) {
        return;
    }

    const startTime = Date.now();
    metrics.commandsSent++;

    if (!SESSION_KEY) {
        logToConsole("[ERROR] No session key - please authenticate first");
        showConnectionModal();
        return;
    }

    const adapterUseNkn = getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN;
    if (adapterUseNkn) {
        disableWebSocketMode();
    }

    if (!adapterUseNkn && useWS && socket && socket.connected) {
        socket.emit('message', {command: command, session_key: SESSION_KEY});
        logToConsole("WS -> " + command);

        const elapsed = (Date.now() - metrics.lastCommandTime) / 1000;
        if (elapsed > 0) {
            metrics.dataRate = 1 / elapsed;
        }
        metrics.lastCommandTime = Date.now();
        metrics.latency = Date.now() - startTime;
        updateMetrics();
    } else {
        if (!adapterUseNkn && !HTTP_URL) {
            logToConsole("[ERROR] No adapter endpoint configured");
            showConnectionModal();
            return;
        }
        try {
            const response = await adapterApiFetch("/send_command", {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({command: command, session_key: SESSION_KEY}),
            });
            metrics.latency = Date.now() - startTime;
            const data = await response.json();
            logToConsole(`${adapterUseNkn ? "NKN" : "HTTP"} -> ` + command);
            if (data.status !== 'success') {
                logToConsole("[ERROR] " + (data.message || JSON.stringify(data)));
                if (data.message && data.message.includes('session')) {
                    SESSION_KEY = "";
                    localStorage.removeItem('sessionKey');
                    showConnectionModal();
                }
            }
            const elapsed = (Date.now() - metrics.lastCommandTime) / 1000;
            if (elapsed > 0) {
                metrics.dataRate = 1 / elapsed;
            }
            metrics.lastCommandTime = Date.now();
            updateMetrics();
        } catch (err) {
            logToConsole("[ERROR] Fetch error: " + err);
            metrics.connected = false;
            updateMetrics();
        }
    }
}



// Initialize the Socket.IO connection.
function initWebSocket() {
  if (getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN) {
    disableWebSocketMode();
    return;
  }
  if (!SESSION_KEY) {
    logToConsole("[WARN] Cannot connect to WebSocket without session key");
    return;
  }

  if (socket) {
    try {
      socket.disconnect();
    } catch (err) {
      console.warn("Socket cleanup failed:", err);
    }
    socket = null;
  }

  try {
    // Extract base URL from WS_URL (remove /ws path)
    const wsBase = WS_URL.replace(/^ws:/, 'http:').replace(/^wss:/, 'https:').replace(/\/ws$/, '');

    // Connect using Socket.IO client
    socket = io(wsBase, {
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      reconnectionAttempts: 5
    });

    socket.on('connect', () => {
      logToConsole("[OK] Socket.IO connected - authenticating...");
      socket.emit('authenticate', {session_key: SESSION_KEY});
    });

    socket.on('message', (data) => {
      try {
        const parsed = typeof data === 'string' ? JSON.parse(data) : data;
        if (parsed.status === 'authenticated') {
          useWS = true;
          metrics.connected = true;
          logToConsole("[OK] WS authenticated - now using WebSocket");
          updateMetrics();
          hideConnectionModal();
        } else if (parsed.status === 'error') {
          logToConsole("[ERROR] WS error: " + parsed.message);
          if (parsed.message && parsed.message.includes('session')) {
            SESSION_KEY = "";
            localStorage.removeItem('sessionKey');
            showConnectionModal();
          }
        } else {
          logToConsole("WS <- " + JSON.stringify(parsed));
        }
      } catch (err) {
        logToConsole("WS <- " + data);
      }
    });

    socket.on('disconnect', () => {
      useWS = false;
      metrics.connected = false;
      logToConsole("[WARN] Socket.IO disconnected - falling back to HTTP");
      updateMetrics();
    });

    socket.on('connect_error', (err) => {
      useWS = false;
      metrics.connected = false;
      logToConsole("[ERROR] Socket.IO connection error - falling back to HTTP");
      updateMetrics();
    });

  } catch (err) {
    console.warn("Socket.IO init failed:", err);
    logToConsole("[ERROR] Socket.IO init failed: " + err);
  }
}

// Show/hide connection modal
function showConnectionModal(userInitiated = false) {
  setRoute("auth");
  ensureEndpointInputBindings();
  initNknRouterUi();
  const routerTargetInput = document.getElementById("routerNknAddressInput");
  syncAdapterConnectionInputs();
  if (routerTargetInput) {
    routerTargetInput.value = routerTargetNknAddress || "";
  }
  ensureBrowserNknIdentity();
  if (userInitiated || document.body.classList.contains("sidebar-collapsed")) {
    setSidebarCollapsed(false, { persist: !!userInitiated });
  }
}

// HTTP-only path used by local orientation streaming.
async function sendCommandHttpOnly(command) {
    if (suppressCommandDispatch) {
        return;
    }

    const startTime = Date.now();
    metrics.commandsSent++;

    if (!SESSION_KEY) {
        logToConsole("[ERROR] No session key - please authenticate first");
        showConnectionModal();
        return;
    }

    const adapterUseNkn = getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN;
    if (!adapterUseNkn && !HTTP_URL) {
        logToConsole("[ERROR] No adapter endpoint configured");
        showConnectionModal();
        return;
    }

    try {
        const response = await adapterApiFetch("/send_command", {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({command: command, session_key: SESSION_KEY}),
        });
        metrics.latency = Date.now() - startTime;
        const data = await response.json();
        logToConsole(`${adapterUseNkn ? "NKN" : "HTTP"} -> ` + command);
        if (data.status !== 'success') {
            logToConsole("[ERROR] " + (data.message || JSON.stringify(data)));
            if (data.message && data.message.includes('session')) {
                SESSION_KEY = "";
                localStorage.removeItem('sessionKey');
                showConnectionModal();
            }
        }
        const elapsed = (Date.now() - metrics.lastCommandTime) / 1000;
        if (elapsed > 0) {
            metrics.dataRate = 1 / elapsed;
        }
        metrics.lastCommandTime = Date.now();
        updateMetrics();
    } catch (err) {
        logToConsole("[ERROR] Fetch error: " + err);
        metrics.connected = false;
        updateMetrics();
    }
}

function hideConnectionModal() {
  const modal = document.getElementById('connectionModal');
  if (modal) {
    modal.classList.remove('active');
  }
  stopRouterQrScanner({ quiet: true });
}

let connectionModalBindingsInstalled = false;
function ensureConnectionModalBindings() {
  if (connectionModalBindingsInstalled) {
    return;
  }
  const modal = document.getElementById('connectionModal');
  const closeBtn = document.getElementById('connectionModalCloseBtn');
  if (!modal) {
    return;
  }
  connectionModalBindingsInstalled = true;
  modal.addEventListener('click', (event) => {
    if (event.target === modal) {
      hideConnectionModal();
    }
  });
  if (closeBtn) {
    closeBtn.addEventListener('click', () => {
      hideConnectionModal();
    });
  }
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      hideConnectionModal();
    }
  });
}

function getServiceAuthLabel(service) {
  return SERVICE_AUTH_LABELS[String(service || "").trim().toLowerCase()] || "Service";
}

function getServiceAuthPassword(service) {
  const key = String(service || "").trim().toLowerCase();
  if (key === "adapter") {
    return String(PASSWORD || localStorage.getItem("password") || "").trim();
  }
  if (key === "camera") {
    return String(cameraRouterPassword || localStorage.getItem("cameraRouterPassword") || "").trim();
  }
  if (key === "audio") {
    return String(audioRouterPassword || localStorage.getItem("audioRouterPassword") || "").trim();
  }
  return "";
}

function setServiceAuthPassword(service, password) {
  const key = String(service || "").trim().toLowerCase();
  const clean = String(password || "").trim();
  if (key === "adapter") {
    PASSWORD = clean;
    if (clean) {
      localStorage.setItem("password", clean);
    }
    const input = document.getElementById("passwordInput");
    if (input) {
      input.value = clean;
    }
    return;
  }
  if (key === "camera") {
    cameraRouterPassword = clean;
    if (clean) {
      localStorage.setItem("cameraRouterPassword", clean);
    }
    const input = document.getElementById("cameraRouterPasswordInput");
    if (input) {
      input.value = clean;
    }
    return;
  }
  if (key === "audio") {
    audioRouterPassword = clean;
    if (clean) {
      localStorage.setItem("audioRouterPassword", clean);
    }
    const input = document.getElementById("audioRouterPasswordInput");
    if (input) {
      input.value = clean;
    }
  }
}

function isServiceSessionReady(service) {
  const key = String(service || "").trim().toLowerCase();
  if (key === "adapter") {
    return isControlTransportReady();
  }
  if (key === "camera") {
    return !!(cameraRouterBaseUrl && cameraRouterSessionKey);
  }
  if (key === "audio") {
    return !!(audioRouterBaseUrl && audioRouterSessionKey);
  }
  return false;
}

function normalizeServiceEndpointForAuth(service, endpoint) {
  const key = String(service || "").trim().toLowerCase();
  const parsedEndpoint = parseServiceEndpoint(endpoint);
  if (parsedEndpoint.transport === SERVICE_TRANSPORT_NKN) {
    return parsedEndpoint.value;
  }

  const raw = String(parsedEndpoint.value || endpoint || "").trim();
  if (!raw || parsedEndpoint.transport !== SERVICE_TRANSPORT_HTTP) {
    return "";
  }
  let origin = "";
  if (key === "adapter") {
    const parsed = buildAdapterEndpoints(raw);
    origin = parsed ? String(parsed.origin || "").trim() : "";
  } else {
    try {
      origin = normalizeServiceOrigin(raw);
    } catch (err) {
      return "";
    }
  }
  if (!isRoutableServiceOrigin(origin)) {
    return "";
  }
  return origin;
}

function buildServiceAuthQueueKey(service, endpoint) {
  return `${String(service || "").trim().toLowerCase()}|${String(endpoint || "").trim().toLowerCase()}`;
}

function isServiceAuthCoolingDown(queueKey) {
  const lastSkippedAt = Number(serviceAuthSkippedAt.get(queueKey) || 0);
  if (!lastSkippedAt) {
    return false;
  }
  return (Date.now() - lastSkippedAt) < SERVICE_AUTH_SKIP_COOLDOWN_MS;
}

function markServiceAuthSkipped(queueKey) {
  if (!queueKey) {
    return;
  }
  serviceAuthSkippedAt.set(queueKey, Date.now());
}

function clearServiceAuthSkipped(queueKey) {
  if (!queueKey) {
    return;
  }
  serviceAuthSkippedAt.delete(queueKey);
}

function ensureServiceAuthModalUi() {
  if (serviceAuthModalDom) {
    return serviceAuthModalDom;
  }
  if (!document.body) {
    return null;
  }

  const modal = document.createElement("div");
  modal.id = "serviceAuthModal";
  modal.className = "modal service-auth-modal";
  modal.innerHTML = `
    <div class="modal-content service-auth-modal-content">
      <button id="serviceAuthModalCloseBtn" class="modal-close-btn" type="button" aria-label="Skip Authentication">x</button>
      <div class="modal-header">Service Authentication</div>
      <div class="modal-section">
        <p id="serviceAuthPromptTitle" class="service-auth-title">Authentication required</p>
        <code id="serviceAuthPromptEndpoint" class="service-auth-endpoint"></code>
        <p id="serviceAuthPromptQueue" class="service-auth-queue"></p>
        <div class="column">
          <label for="serviceAuthPasswordInput">Password:</label>
          <input id="serviceAuthPasswordInput" type="password" autocomplete="current-password" placeholder="Enter password">
        </div>
        <p id="serviceAuthPromptError" class="service-auth-error" aria-live="polite"></p>
        <div class="row">
          <button id="serviceAuthSubmitBtn" class="primary" type="button">Authenticate</button>
          <button id="serviceAuthSkipBtn" type="button">Skip</button>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const closeBtn = modal.querySelector("#serviceAuthModalCloseBtn");
  const skipBtn = modal.querySelector("#serviceAuthSkipBtn");
  const submitBtn = modal.querySelector("#serviceAuthSubmitBtn");
  const passwordInput = modal.querySelector("#serviceAuthPasswordInput");
  const titleEl = modal.querySelector("#serviceAuthPromptTitle");
  const endpointEl = modal.querySelector("#serviceAuthPromptEndpoint");
  const queueEl = modal.querySelector("#serviceAuthPromptQueue");
  const errorEl = modal.querySelector("#serviceAuthPromptError");

  const resolvePrompt = (result) => {
    if (!serviceAuthModalResolver) {
      return;
    }
    const resolver = serviceAuthModalResolver;
    serviceAuthModalResolver = null;
    modal.classList.remove("active");
    resolver(result);
  };

  const onSubmit = () => {
    const password = passwordInput ? String(passwordInput.value || "") : "";
    resolvePrompt({ action: "submit", password });
  };
  const onSkip = () => resolvePrompt({ action: "skip", password: "" });

  if (closeBtn) {
    closeBtn.addEventListener("click", onSkip);
  }
  if (skipBtn) {
    skipBtn.addEventListener("click", onSkip);
  }
  if (submitBtn) {
    submitBtn.addEventListener("click", onSubmit);
  }
  if (passwordInput) {
    passwordInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        onSubmit();
      }
    });
  }
  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      onSkip();
    }
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && modal.classList.contains("active")) {
      onSkip();
    }
  });

  serviceAuthModalDom = {
    modal,
    passwordInput,
    titleEl,
    endpointEl,
    queueEl,
    errorEl,
  };
  return serviceAuthModalDom;
}

async function promptServiceAuthPassword(request, options = {}) {
  const ui = ensureServiceAuthModalUi();
  if (!ui) {
    return { action: "skip", password: "" };
  }

  const service = String((request && request.service) || "").trim().toLowerCase();
  const endpoint = String((request && request.endpoint) || "").trim();
  const label = getServiceAuthLabel(service);
  const queueDepth = Number(options.queueDepth || 0);
  const errorMessage = String(options.errorMessage || "").trim();
  const prefillPassword = String(options.prefillPassword || "").trim();

  if (ui.titleEl) {
    ui.titleEl.textContent = `${label} endpoint requires authentication.`;
  }
  if (ui.endpointEl) {
    ui.endpointEl.textContent = endpoint || "(endpoint unavailable)";
  }
  if (ui.queueEl) {
    ui.queueEl.textContent = queueDepth > 0
      ? `${queueDepth} service${queueDepth === 1 ? "" : "s"} remaining in queue.`
      : "Last service in queue.";
  }
  if (ui.errorEl) {
    ui.errorEl.textContent = errorMessage;
  }
  if (ui.passwordInput) {
    ui.passwordInput.value = prefillPassword;
  }

  ui.modal.classList.add("active");
  if (ui.passwordInput) {
    setTimeout(() => {
      ui.passwordInput.focus();
      ui.passwordInput.select();
    }, 0);
  }

  return new Promise((resolve) => {
    serviceAuthModalResolver = resolve;
  });
}

async function authenticateAdapterWithPassword(password, endpoint) {
  const cleanPassword = String(password || "").trim();
  if (!cleanPassword) {
    return false;
  }
  const normalized = buildAdapterEndpoints(endpoint || HTTP_URL || WS_URL || "");
  if (!normalized) {
    return false;
  }
  const transport = String(normalized.transport || SERVICE_TRANSPORT_HTTP).trim().toLowerCase();
  const adapterNknAddress = normalizeNknAddress(normalized.nknAddress || normalized.origin);
  setServiceTransportMode("adapter", transport);
  setServiceNknAddress("adapter", adapterNknAddress);
  HTTP_URL = transport === SERVICE_TRANSPORT_HTTP ? String(normalized.httpUrl || "").trim() : "";
  WS_URL = transport === SERVICE_TRANSPORT_HTTP ? String(normalized.wsUrl || "").trim() : "";
  if (transport === SERVICE_TRANSPORT_HTTP) {
    localStorage.setItem("httpUrl", HTTP_URL);
    localStorage.setItem("wsUrl", WS_URL);
  } else {
    localStorage.removeItem("httpUrl");
    localStorage.removeItem("wsUrl");
  }
  setServiceAuthPassword("adapter", cleanPassword);
  syncAdapterConnectionInputs({ preserveUserInput: false });

  const success = await authenticate(
    cleanPassword,
    WS_URL,
    transport === SERVICE_TRANSPORT_NKN ? nknEndpointForAddress(adapterNknAddress) : HTTP_URL
  );
  if (!success) {
    return false;
  }

  const activeRoute = getRouteFromLocation();
  if (transport !== SERVICE_TRANSPORT_NKN && WS_URL && routeAllowsWebSocket(activeRoute)) {
    initWebSocket();
  } else {
    useWS = false;
    metrics.connected = true;
    updateMetrics();
  }
  hideConnectionModal();
  return true;
}

async function attemptServiceAuthWithPassword(service, password, endpoint, options = {}) {
  const silent = !!options.silent;
  const key = String(service || "").trim().toLowerCase();
  if (key === "adapter") {
    return authenticateAdapterWithPassword(password, endpoint);
  }
  if (key === "camera") {
    return authenticateCameraRouterWithPassword(password, { baseUrl: endpoint, silent });
  }
  if (key === "audio") {
    return authenticateAudioRouterWithPassword(password, { baseUrl: endpoint, silent });
  }
  return false;
}

async function processServiceAuthQueue() {
  if (serviceAuthQueueRunning) {
    return;
  }
  serviceAuthQueueRunning = true;

  try {
    while (serviceAuthQueue.length > 0) {
      const request = serviceAuthQueue.shift();
      const service = String((request && request.service) || "").trim().toLowerCase();
      const endpoint = normalizeServiceEndpointForAuth(service, request && request.endpoint);
      if (!service || !endpoint) {
        continue;
      }

      const queueKey = buildServiceAuthQueueKey(service, endpoint);
      serviceAuthCurrentRequest = { service, endpoint };
      if (isServiceSessionReady(service)) {
        clearServiceAuthSkipped(queueKey);
        serviceAuthCurrentRequest = null;
        continue;
      }

      let errorMessage = "";
      const storedPassword = getServiceAuthPassword(service);
      if (storedPassword && !request.forcePrompt) {
        const autoSuccess = await attemptServiceAuthWithPassword(service, storedPassword, endpoint, {
          silent: true,
        });
        if (autoSuccess) {
          clearServiceAuthSkipped(queueKey);
          serviceAuthCurrentRequest = null;
          continue;
        }
        errorMessage = `${getServiceAuthLabel(service)} authentication failed with saved password.`;
      }

      while (!isServiceSessionReady(service)) {
        const result = await promptServiceAuthPassword(request, {
          queueDepth: serviceAuthQueue.length,
          errorMessage,
          prefillPassword: getServiceAuthPassword(service),
        });
        if (!result || result.action !== "submit") {
          markServiceAuthSkipped(queueKey);
          break;
        }

        const enteredPassword = String(result.password || "").trim();
        if (!enteredPassword) {
          errorMessage = "Password is required.";
          continue;
        }

        const ok = await attemptServiceAuthWithPassword(service, enteredPassword, endpoint, {
          silent: true,
        });
        if (ok) {
          clearServiceAuthSkipped(queueKey);
          break;
        }
        errorMessage = `${getServiceAuthLabel(service)} authentication failed. Retry or skip.`;
      }

      serviceAuthCurrentRequest = null;
    }
  } finally {
    serviceAuthCurrentRequest = null;
    serviceAuthQueueRunning = false;
  }
}

function enqueueServiceAuthRequest(service, endpoint, options = {}) {
  const key = String(service || "").trim().toLowerCase();
  const normalizedEndpoint = normalizeServiceEndpointForAuth(key, endpoint);
  if (!key || !normalizedEndpoint) {
    return;
  }
  if (isServiceSessionReady(key)) {
    return;
  }

  const queueKey = buildServiceAuthQueueKey(key, normalizedEndpoint);
  if (!options.forcePrompt && isServiceAuthCoolingDown(queueKey)) {
    return;
  }
  if (
    serviceAuthCurrentRequest &&
    buildServiceAuthQueueKey(serviceAuthCurrentRequest.service, serviceAuthCurrentRequest.endpoint) === queueKey
  ) {
    return;
  }
  const alreadyQueued = serviceAuthQueue.some((entry) => {
    return buildServiceAuthQueueKey(entry.service, entry.endpoint) === queueKey;
  });
  if (alreadyQueued) {
    return;
  }

  serviceAuthQueue.push({
    service: key,
    endpoint: normalizedEndpoint,
    forcePrompt: !!options.forcePrompt,
  });
  processServiceAuthQueue().catch((err) => {
    console.warn("Service auth queue failed:", err);
  });
}

function setResolvedServiceAuthEndpoint(service, endpoint) {
  const key = String(service || "").trim().toLowerCase();
  if (key !== "adapter" && key !== "camera" && key !== "audio") {
    return;
  }
  resolvedServiceAuthEndpoints[key] = normalizeServiceEndpointForAuth(key, endpoint || "");
}

function getServicePreferredAuthEndpoint(service) {
  const key = String(service || "").trim().toLowerCase();
  const resolved = String(resolvedServiceAuthEndpoints[key] || "").trim();
  if (!resolved) {
    return "";
  }
  const parsed = parseServiceEndpoint(resolved);
  if (parsed.transport === SERVICE_TRANSPORT_NKN && parsed.value) {
    return parsed.value;
  }
  if (parsed.transport === SERVICE_TRANSPORT_HTTP && parsed.value && isRoutableServiceOrigin(parsed.value)) {
    return parsed.value;
  }
  return "";
}

async function requestServiceAuthForAction(service, options = {}) {
  const key = String(service || "").trim().toLowerCase();
  const endpoint = normalizeServiceEndpointForAuth(
    key,
    options.endpoint || getServicePreferredAuthEndpoint(key)
  );
  if (!endpoint) {
    return false;
  }
  if (isServiceSessionReady(key)) {
    return true;
  }

  enqueueServiceAuthRequest(key, endpoint, { forcePrompt: true });

  const timeoutMs = Math.max(1200, Number(options.timeoutMs) || 45000);
  const deadline = Date.now() + timeoutMs;
  const queueKey = buildServiceAuthQueueKey(key, endpoint);

  while (Date.now() < deadline) {
    if (isServiceSessionReady(key)) {
      return true;
    }
    const activeMatch =
      serviceAuthCurrentRequest &&
      buildServiceAuthQueueKey(serviceAuthCurrentRequest.service, serviceAuthCurrentRequest.endpoint) === queueKey;
    const queuedMatch = serviceAuthQueue.some((entry) => {
      return buildServiceAuthQueueKey(entry.service, entry.endpoint) === queueKey;
    });
    if (!activeMatch && !queuedMatch && !serviceAuthQueueRunning) {
      break;
    }
    await sleepMs(120);
  }
  return isServiceSessionReady(key);
}

function queueResolvedEndpointAuthentications(options = {}) {
  const adapterEndpoint = normalizeServiceEndpointForAuth("adapter", options.adapterEndpoint || "");
  const cameraEndpoint = normalizeServiceEndpointForAuth("camera", options.cameraEndpoint || "");
  const audioEndpoint = normalizeServiceEndpointForAuth("audio", options.audioEndpoint || "");

  setResolvedServiceAuthEndpoint("adapter", adapterEndpoint);
  setResolvedServiceAuthEndpoint("camera", cameraEndpoint);
  setResolvedServiceAuthEndpoint("audio", audioEndpoint);

  if (adapterEndpoint && !isServiceSessionReady("adapter")) {
    enqueueServiceAuthRequest("adapter", adapterEndpoint);
  }
  if (cameraEndpoint && !isServiceSessionReady("camera")) {
    enqueueServiceAuthRequest("camera", cameraEndpoint);
  }
  if (audioEndpoint && !isServiceSessionReady("audio")) {
    enqueueServiceAuthRequest("audio", audioEndpoint);
  }
}

let endpointInputBindingsInstalled = false;
let endpointHydrateTimer = null;

function buildAdapterEndpoints(baseInput) {
  if (!baseInput) {
    return null;
  }

  const raw = baseInput.trim();
  if (!raw) {
    return null;
  }

  if (/^nkn:\/\//i.test(raw) || isLikelyNknAddress(raw)) {
    const nknAddress = normalizeNknAddress(raw);
    if (!nknAddress) {
      return null;
    }
    return {
      transport: SERVICE_TRANSPORT_NKN,
      origin: nknEndpointForAddress(nknAddress),
      nknAddress,
      httpUrl: "",
      wsUrl: "",
    };
  }

  const candidate = raw.includes("://") ? raw : `https://${raw}`;
  let adapterUrl;
  try {
    adapterUrl = new URL(candidate);
  } catch (err) {
    return null;
  }

  let defaultHttpPath = "/send_command";
  let defaultWsPath = "/ws";
  try {
    defaultHttpPath = new URL(SERVER_DEFAULT_HTTP_URL).pathname || "/send_command";
  } catch (err) {}
  try {
    defaultWsPath = new URL(SERVER_DEFAULT_WS_URL).pathname || "/ws";
  } catch (err) {}

  const baseProtocol = adapterUrl.protocol === "wss:"
    ? "https:"
    : adapterUrl.protocol === "ws:"
      ? "http:"
      : adapterUrl.protocol;
  const baseOrigin = `${baseProtocol}//${adapterUrl.host}`;
  const httpUrl = `${baseOrigin}${defaultHttpPath}`;
  const wsProtocol = baseProtocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProtocol}//${adapterUrl.host}${defaultWsPath}`;
  return {
    transport: SERVICE_TRANSPORT_HTTP,
    httpUrl,
    wsUrl,
    origin: baseOrigin,
    nknAddress: "",
  };
}

function setAdapterEndpointPreview(httpUrl = "", wsUrl = "", options = {}) {
  const previewEl = document.getElementById("adapterEndpointPreview");
  if (!previewEl) {
    return;
  }
  const cleanHttp = String(httpUrl || "").trim();
  const cleanWs = String(wsUrl || "").trim();
  const explicitTransport = String(options.transport || "").trim().toLowerCase();
  const nknAddress = normalizeNknAddress(options.nknAddress || cleanHttp);
  if (explicitTransport === SERVICE_TRANSPORT_NKN || nknAddress) {
    previewEl.textContent = `NKN ${nknEndpointForAddress(nknAddress || resolveServiceNknTarget("adapter")) || "(missing address)"} | WS n/a`;
    return;
  }
  if (!cleanHttp && !cleanWs) {
    previewEl.textContent = "No adapter endpoint resolved yet.";
    return;
  }
  previewEl.textContent = `HTTP ${cleanHttp || "(empty)"} | WS ${cleanWs || "(empty)"}`;
}

function syncAdapterConnectionInputs(options = {}) {
  const preserveUserInput = !!options.preserveUserInput;
  const adapterAddressInput = document.getElementById("adapterAddressInput");
  const passInput = document.getElementById("passwordInput");
  const httpInput = document.getElementById("httpUrlInput");
  const wsInput = document.getElementById("wsUrlInput");

  if (passInput && (!preserveUserInput || !passInput.value.trim())) {
    passInput.value = PASSWORD || "";
  }
  if (httpInput && (!preserveUserInput || !httpInput.value.trim())) {
    httpInput.value = HTTP_URL || "";
  }
  if (wsInput && (!preserveUserInput || !wsInput.value.trim())) {
    wsInput.value = WS_URL || "";
  }
  if (getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN) {
    const nknAddress = resolveServiceNknTarget("adapter");
    if (httpInput) {
      httpInput.value = "";
    }
    if (wsInput) {
      wsInput.value = "";
    }
    if (adapterAddressInput && (!preserveUserInput || !adapterAddressInput.value.trim())) {
      adapterAddressInput.value = nknEndpointForAddress(nknAddress);
    }
    setAdapterEndpointPreview("", "", { transport: SERVICE_TRANSPORT_NKN, nknAddress });
    return;
  }
  hydrateEndpointInputs("http");
}

function hydrateEndpointInputs(prefer = "address") {
  const adapterAddressInput = document.getElementById("adapterAddressInput");
  const httpInput = document.getElementById("httpUrlInput");
  const wsInput = document.getElementById("wsUrlInput");
  if (!httpInput || !wsInput) {
    return null;
  }

  const addressRaw = adapterAddressInput ? adapterAddressInput.value.trim() : "";
  const httpRaw = httpInput.value.trim();
  const wsRaw = wsInput.value.trim();
  let source = "";
  if (prefer === "ws") {
    source = wsRaw || httpRaw || addressRaw;
  } else if (prefer === "http") {
    source = httpRaw || wsRaw || addressRaw;
  } else {
    source = addressRaw || httpRaw || wsRaw;
  }
  const endpoints = buildAdapterEndpoints(source);
  if (!endpoints) {
    setAdapterEndpointPreview(httpRaw, wsRaw);
    return null;
  }
  if (endpoints.transport === SERVICE_TRANSPORT_NKN) {
    const nknAddress = normalizeNknAddress(endpoints.nknAddress || endpoints.origin);
    if (!nknAddress) {
      setAdapterEndpointPreview("", "");
      return null;
    }
    httpInput.value = "";
    wsInput.value = "";
    if (adapterAddressInput) {
      adapterAddressInput.value = nknEndpointForAddress(nknAddress);
    }
    setAdapterEndpointPreview("", "", { transport: SERVICE_TRANSPORT_NKN, nknAddress });
    return endpoints;
  }
  if (!isRoutableServiceOrigin(endpoints.origin)) {
    setAdapterEndpointPreview("", "");
    return null;
  }

  httpInput.value = endpoints.httpUrl;
  wsInput.value = endpoints.wsUrl;
  if (adapterAddressInput) {
    adapterAddressInput.value = endpoints.origin;
  }
  setAdapterEndpointPreview(endpoints.httpUrl, endpoints.wsUrl);
  return endpoints;
}

function ensureEndpointInputBindings() {
  if (endpointInputBindingsInstalled) {
    return;
  }

  const adapterAddressInput = document.getElementById("adapterAddressInput");
  const httpInput = document.getElementById("httpUrlInput");
  const wsInput = document.getElementById("wsUrlInput");
  if (!adapterAddressInput || !httpInput || !wsInput) {
    return;
  }
  adapterAddressInput.readOnly = true;
  adapterAddressInput.title = "Resolved from router service endpoints";

  endpointInputBindingsInstalled = true;

  const hydrateFromAddress = () => {
    if (adapterAddressInput.value.trim()) {
      hydrateEndpointInputs("address");
    }
  };
  const scheduleHydrate = (prefer) => {
    if (endpointHydrateTimer) {
      clearTimeout(endpointHydrateTimer);
    }
    endpointHydrateTimer = setTimeout(() => hydrateEndpointInputs(prefer), 120);
  };

  adapterAddressInput.addEventListener("input", () => scheduleHydrate("address"));
  adapterAddressInput.addEventListener("blur", hydrateFromAddress);
  adapterAddressInput.addEventListener("change", hydrateFromAddress);
  adapterAddressInput.addEventListener("paste", () => setTimeout(hydrateFromAddress, 0));
}

// Fill HTTP/WS inputs from a provided adapter/tunnel URL.
function fetchTunnelUrl() {
  const endpoints = hydrateEndpointInputs("address");
  if (!endpoints) {
    alert("Resolve adapter service endpoints first; only router-resolved non-loopback endpoints are allowed.");
    return;
  }
  if (endpoints.transport === SERVICE_TRANSPORT_NKN) {
    logToConsole("Adapter NKN endpoint selected: " + nknEndpointForAddress(endpoints.nknAddress));
  } else {
    logToConsole("Adapter endpoints filled from: " + endpoints.origin);
  }
}

// Handle connection form submission
async function connectToAdapter() {
  const passwordInput = document.getElementById('passwordInput');
  const password = (passwordInput ? passwordInput.value.trim() : "") || PASSWORD;
  const addressInputEl = document.getElementById("adapterAddressInput");
  const httpInputEl = document.getElementById('httpUrlInput');
  const wsInputEl = document.getElementById('wsUrlInput');
  const addressRaw = addressInputEl ? addressInputEl.value.trim() : "";
  const httpInputRaw = httpInputEl ? httpInputEl.value.trim() : "";
  const wsInputRaw = wsInputEl ? wsInputEl.value.trim() : "";

  if (!password || (!addressRaw && !httpInputRaw && !wsInputRaw && !HTTP_URL && !WS_URL && !resolveServiceNknTarget("adapter"))) {
    alert("Please enter password. Adapter endpoint comes from router-resolved Cloudflare service endpoints.");
    return;
  }

  const normalized =
    hydrateEndpointInputs(addressRaw ? "address" : "http") ||
    buildAdapterEndpoints(httpInputRaw || wsInputRaw || HTTP_URL || WS_URL);
  if (!normalized) {
    alert("Adapter endpoint must be a router-resolved non-loopback endpoint");
    return;
  }
  if (normalized.transport === SERVICE_TRANSPORT_HTTP && !isRoutableServiceOrigin(normalized.origin)) {
    alert("Adapter endpoint must be a router-resolved non-loopback endpoint");
    return;
  }
  const resolvedAdapterEndpoint = getServicePreferredAuthEndpoint("adapter");
  const normalizedOrigin = normalizeServiceEndpointForAuth("adapter", normalized.origin);
  if (!resolvedAdapterEndpoint || !normalizedOrigin || normalizedOrigin !== resolvedAdapterEndpoint) {
    alert("Adapter endpoint must match the router-resolved service endpoint");
    logToConsole("[WARN] Adapter connect blocked: endpoint does not match router-resolved adapter endpoint");
    return;
  }

  const httpUrl = normalized.transport === SERVICE_TRANSPORT_HTTP ? normalized.httpUrl : "";
  const wsUrl = normalized.transport === SERVICE_TRANSPORT_HTTP ? normalized.wsUrl : "";
  const adapterNknAddress = normalizeNknAddress(normalized.nknAddress || normalized.origin);

  logToConsole("[CONNECT] Connecting to adapter...");

  // Authenticate first
  setServiceTransportMode("adapter", normalized.transport || SERVICE_TRANSPORT_HTTP);
  setServiceNknAddress("adapter", adapterNknAddress);
  const success = await authenticate(
    password,
    wsUrl,
    normalized.transport === SERVICE_TRANSPORT_NKN ? nknEndpointForAddress(adapterNknAddress) : httpUrl
  );
  if (success) {
    PASSWORD = password;
    localStorage.setItem("password", PASSWORD);
    WS_URL = wsUrl;
    HTTP_URL = httpUrl;
    syncAdapterConnectionInputs({ preserveUserInput: true });
    const activeRoute = getRouteFromLocation();

    // Try WebSocket if URL provided
    if (normalized.transport !== SERVICE_TRANSPORT_NKN && wsUrl && routeAllowsWebSocket(activeRoute)) {
      initWebSocket();
    } else {
      disableWebSocketMode();
      hideConnectionModal();
    }
  }
}

// Parse query parameters for adapter URL
function normalizeRouterNknTarget(rawValue) {
  const raw = String(rawValue || "").trim();
  if (!raw) {
    return "";
  }
  const plainHexMatch = raw.match(/^([0-9a-fA-F]{64})$/);
  if (plainHexMatch) {
    return `router.${plainHexMatch[1].toLowerCase()}`;
  }
  const prefixedMatch = raw.match(/^([a-zA-Z0-9._-]+)\.([0-9a-fA-F]{64})$/);
  if (prefixedMatch) {
    return `${prefixedMatch[1]}.${prefixedMatch[2].toLowerCase()}`;
  }
  return raw;
}

function parseConnectionFromQuery() {
  const urlParams = new URLSearchParams(window.location.search);
  const getFirstParam = (keys) => {
    for (const key of keys) {
      const value = (urlParams.get(key) || "").trim();
      if (value) {
        return value;
      }
    }
    return "";
  };

  // Accept both legacy and shorthand aliases:
  // ?adapter=<url> or ?server=<url>
  // &password=<secret> or &pass=<secret>
  // &nkn=<router.<pubkey-hex> | <pubkey-hex>>
  const adapterParam = getFirstParam(["adapter", "server"]);
  const passwordParam = getFirstParam(["password", "pass"]);
  const routerNknParam = getFirstParam(["nkn", "router_nkn", "nkn_router", "router_nkn_address"]);

  let adapterConfigured = false;
  let passwordProvided = false;

  if (adapterParam) {
    adapterConfigured = false;
    logToConsole("[WARN] Ignored adapter URL query parameter; adapter endpoints are sourced from router service resolution only");
  }

  if (passwordParam) {
    PASSWORD = passwordParam;
    localStorage.setItem('password', PASSWORD);
    passwordProvided = true;
  }

  if (routerNknParam) {
    const normalizedRouterNkn = normalizeRouterNknTarget(routerNknParam);
    if (isLikelyNknAddress(normalizedRouterNkn)) {
      routerTargetNknAddress = normalizedRouterNkn;
      localStorage.setItem("routerTargetNknAddress", routerTargetNknAddress);
      logToConsole(`[OK] Router NKN target configured from query: ${routerTargetNknAddress}`);
    } else {
      logToConsole("[WARN] Invalid nkn query parameter; expected 64-hex or prefix.64-hex");
    }
  }

  return { adapterConfigured, passwordProvided };
}

let routerTargetNknAddress = localStorage.getItem("routerTargetNknAddress") || "";
let browserNknSeedHex = localStorage.getItem("browserNknSeedHex") || "";
let browserNknPubHex = localStorage.getItem("browserNknPubHex") || "";
const ROUTER_NKN_IDENTIFIER = "web";
const ROUTER_NKN_SUBCLIENTS = 4;
const ROUTER_NKN_SUBCLIENT_FALLBACKS = Object.freeze(
  Array.from(new Set([ROUTER_NKN_SUBCLIENTS, 2, 1])).filter(
    (value) => Number.isInteger(value) && value > 0
  )
);
const ROUTER_NKN_READY_TIMEOUT_MS = 16000;
const ROUTER_NKN_RESOLVE_TIMEOUT_MS = 14000;
const ROUTER_NKN_AUTO_RESOLVE_INTERVAL_MS = 45000;
const ROUTER_NKN_SEND_RETRY_MAX_ATTEMPTS = 4;
const ROUTER_NKN_SEND_RETRY_DELAY_MS = 450;
const ROUTER_QR_SCAN_INTERVAL_MS = 140;
const ROUTER_NKN_FRAME_TIMEOUT_MS = 5200;
const ROUTER_NKN_FRAME_POLL_INTERVAL_MS = 280;
const ROUTER_NKN_FRAME_MAX_KBPS = 900;
const ROUTER_NKN_FRAME_MAX_WIDTH = 640;
const ROUTER_NKN_FRAME_MAX_HEIGHT = 360;
const ROUTER_NKN_RPC_TIMEOUT_MS = 10000;

let nknUiInitialized = false;
let browserNknClient = null;
let browserNknClientReady = false;
let browserNknClientAddress = "";
let browserNknSubclientCount = ROUTER_NKN_SUBCLIENTS;
let nknClientInitPromise = null;
let nknResolveInFlight = false;
let routerAutoResolveTimer = null;
const pendingNknResolveRequests = new Map();
const pendingNknFrameRequests = new Map();
const pendingNknServiceRpcRequests = new Map();
let routerQrScannerActive = false;
let routerQrScannerTimer = null;
let routerQrScannerStream = null;
let routerQrScannerCanvas = null;
let routerQrScannerCtx = null;
let routerQrScannerDetector = null;
let routerQrScannerDecoding = false;

function bytesToHex(bytes) {
  return Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function hexToBytes(hex) {
  if (!/^[0-9a-fA-F]+$/.test(hex || "") || (hex || "").length % 2 !== 0) {
    return null;
  }
  const out = new Uint8Array((hex || "").length / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

function generateSeedHex() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return bytesToHex(bytes);
}

function setRouterResolveStatus(message, error = false) {
  const statusEl = document.getElementById("routerResolveStatus");
  if (!statusEl) {
    return;
  }
  statusEl.textContent = message;
  statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function setBrowserNknSeedStatus(message, error = false) {
  const statusEl = document.getElementById("browserNknSeedStatus");
  if (!statusEl) {
    return;
  }
  statusEl.textContent = message;
  statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function renderBrowserNknQr(text) {
  const qrEl = document.getElementById("browserNknQr");
  if (!qrEl) {
    return;
  }
  qrEl.innerHTML = "";
  if (!text) {
    return;
  }
  if (typeof QRCode === "undefined") {
    qrEl.textContent = "QR lib unavailable";
    return;
  }
  new QRCode(qrEl, {
    text,
    width: 112,
    height: 112,
    correctLevel: QRCode.CorrectLevel.M,
  });
}

function setRouterQrScannerStatus(message, error = false) {
  const statusEl = document.getElementById("routerQrScannerStatus");
  if (!statusEl) {
    return;
  }
  statusEl.textContent = String(message || "");
  statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function setRouterQrScannerUiState(active) {
  const scanBtn = document.getElementById("routerScanQrBtn");
  const stopBtn = document.getElementById("routerQrScannerStopBtn");
  if (scanBtn) {
    scanBtn.disabled = !!active;
    scanBtn.textContent = active ? "Scanning..." : "Scan";
  }
  if (stopBtn) {
    stopBtn.disabled = !active;
  }
}

function extractRouterNknAddressFromQr(rawValue) {
  const text = String(rawValue || "").trim();
  if (!text) {
    return "";
  }

  const candidates = [];
  const pushCandidate = (value) => {
    const cleaned = String(value || "").trim();
    if (cleaned) {
      candidates.push(cleaned);
    }
  };

  pushCandidate(text);
  if (/^nkn:/i.test(text)) {
    pushCandidate(text.replace(/^nkn:/i, ""));
  }

  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      pushCandidate(parsed.router_address);
      pushCandidate(parsed.target_address);
      pushCandidate(parsed.nkn_address);
      pushCandidate(parsed.address);
      if (parsed.nkn && typeof parsed.nkn === "object") {
        pushCandidate(parsed.nkn.address);
      }
    }
  } catch (err) {}

  try {
    const parsedUrl = new URL(text);
    ["nkn", "router_nkn", "router_address", "target_address", "address"].forEach((key) => {
      pushCandidate(parsedUrl.searchParams.get(key));
    });
    if (parsedUrl.hash) {
      pushCandidate(parsedUrl.hash.replace(/^#/, ""));
    }
  } catch (err) {}

  const prefixedMatch = text.match(/[a-zA-Z0-9._-]+\.[0-9a-fA-F]{64}/);
  if (prefixedMatch) {
    pushCandidate(prefixedMatch[0]);
  }
  const hexMatch = text.match(/[0-9a-fA-F]{64}/);
  if (hexMatch) {
    pushCandidate(hexMatch[0]);
  }

  for (const candidate of candidates) {
    const normalized = normalizeRouterNknTarget(candidate);
    if (isLikelyNknAddress(normalized)) {
      return normalized;
    }
  }
  return "";
}

function consumeRouterNknAddressFromQr(rawValue) {
  const normalized = extractRouterNknAddressFromQr(rawValue);
  if (!normalized) {
    setRouterQrScannerStatus("QR decoded, but no valid router NKN address found", true);
    return false;
  }

  const targetInput = document.getElementById("routerNknAddressInput");
  if (targetInput) {
    targetInput.value = normalized;
  }
  routerTargetNknAddress = normalized;
  localStorage.setItem("routerTargetNknAddress", routerTargetNknAddress);
  setRouterQrScannerStatus(`Scanned ${normalized}`);
  setRouterResolveStatus(`Scanned router QR: ${normalized}; resolving...`);
  stopRouterQrScanner({ quiet: true });
  resolveEndpointsViaNkn({ auto: false }).catch(() => {});
  return true;
}

function scheduleRouterQrScannerTick(delayMs = ROUTER_QR_SCAN_INTERVAL_MS) {
  if (!routerQrScannerActive) {
    return;
  }
  if (routerQrScannerTimer) {
    clearTimeout(routerQrScannerTimer);
  }
  routerQrScannerTimer = setTimeout(() => {
    routerQrScannerTick().catch((err) => {
      setRouterQrScannerStatus(`Scanner error: ${err}`, true);
      scheduleRouterQrScannerTick(ROUTER_QR_SCAN_INTERVAL_MS * 2);
    });
  }, Math.max(40, Number(delayMs) || ROUTER_QR_SCAN_INTERVAL_MS));
}

async function routerQrScannerTick() {
  if (!routerQrScannerActive) {
    return;
  }
  const videoEl = document.getElementById("routerQrScannerVideo");
  if (!videoEl || videoEl.readyState < 2 || !videoEl.videoWidth || !videoEl.videoHeight) {
    scheduleRouterQrScannerTick();
    return;
  }

  let decodedText = "";
  if (routerQrScannerDetector && !routerQrScannerDecoding) {
    routerQrScannerDecoding = true;
    try {
      const detections = await routerQrScannerDetector.detect(videoEl);
      if (Array.isArray(detections) && detections.length > 0) {
        decodedText = String(detections[0].rawValue || "").trim();
      }
    } catch (err) {}
    routerQrScannerDecoding = false;
  }

  if (!decodedText && typeof jsQR === "function") {
    const width = Math.max(80, Math.min(960, Number(videoEl.videoWidth) || 0));
    const height = Math.max(80, Math.round(width * ((Number(videoEl.videoHeight) || 1) / (Number(videoEl.videoWidth) || 1))));
    if (!routerQrScannerCanvas) {
      routerQrScannerCanvas = document.createElement("canvas");
      routerQrScannerCtx = routerQrScannerCanvas.getContext("2d", { willReadFrequently: true });
    }
    if (routerQrScannerCanvas.width !== width || routerQrScannerCanvas.height !== height) {
      routerQrScannerCanvas.width = width;
      routerQrScannerCanvas.height = height;
    }
    if (routerQrScannerCtx) {
      routerQrScannerCtx.drawImage(videoEl, 0, 0, width, height);
      const imageData = routerQrScannerCtx.getImageData(0, 0, width, height);
      const qrResult = jsQR(imageData.data, width, height, { inversionAttempts: "dontInvert" });
      if (qrResult && qrResult.data) {
        decodedText = String(qrResult.data || "").trim();
      }
    }
  }

  if (decodedText && consumeRouterNknAddressFromQr(decodedText)) {
    return;
  }
  scheduleRouterQrScannerTick();
}

function stopRouterQrScanner(options = {}) {
  const quiet = !!options.quiet;
  const keepPanelOpen = !!options.keepPanelOpen;

  routerQrScannerActive = false;
  routerQrScannerDecoding = false;
  if (routerQrScannerTimer) {
    clearTimeout(routerQrScannerTimer);
    routerQrScannerTimer = null;
  }
  if (routerQrScannerStream) {
    try {
      routerQrScannerStream.getTracks().forEach((track) => track.stop());
    } catch (err) {}
    routerQrScannerStream = null;
  }

  const videoEl = document.getElementById("routerQrScannerVideo");
  if (videoEl) {
    try {
      videoEl.pause();
    } catch (err) {}
    videoEl.srcObject = null;
  }

  const panel = document.getElementById("routerQrScannerPanel");
  if (panel && !keepPanelOpen) {
    panel.hidden = true;
  }
  setRouterQrScannerUiState(false);
  if (!quiet) {
    setRouterQrScannerStatus("Scanner stopped");
  }
}

async function startRouterQrScanner() {
  if (routerQrScannerActive) {
    return true;
  }

  const panel = document.getElementById("routerQrScannerPanel");
  const videoEl = document.getElementById("routerQrScannerVideo");
  if (!panel || !videoEl) {
    setRouterResolveStatus("QR scanner UI unavailable", true);
    return false;
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
    setRouterQrScannerStatus("Camera scanning unsupported in this browser", true);
    setRouterResolveStatus("Camera scanning unsupported in this browser", true);
    return false;
  }

  stopRouterQrScanner({ quiet: true, keepPanelOpen: true });
  panel.hidden = false;
  setRouterQrScannerUiState(true);
  setRouterQrScannerStatus("Requesting camera access...");

  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: { ideal: "environment" },
      },
      audio: false,
    });
  } catch (firstErr) {
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    } catch (secondErr) {
      setRouterQrScannerUiState(false);
      setRouterQrScannerStatus(`Camera permission failed: ${secondErr}`, true);
      setRouterResolveStatus(`Camera permission failed: ${secondErr}`, true);
      return false;
    }
  }

  routerQrScannerStream = stream;
  videoEl.srcObject = stream;
  try {
    await videoEl.play();
  } catch (err) {}

  routerQrScannerDetector = null;
  if (typeof BarcodeDetector !== "undefined") {
    try {
      routerQrScannerDetector = new BarcodeDetector({ formats: ["qr_code"] });
    } catch (err) {
      routerQrScannerDetector = null;
    }
  }

  if (!routerQrScannerDetector && typeof jsQR !== "function") {
    setRouterQrScannerStatus("No QR decode backend available (BarcodeDetector/jsQR missing)", true);
    stopRouterQrScanner({ quiet: true });
    return false;
  }

  routerQrScannerActive = true;
  setRouterQrScannerStatus("Scanning for router QR...");
  scheduleRouterQrScannerTick(120);
  return true;
}

function ensureBrowserNknIdentity() {
  const pubEl = document.getElementById("browserNknPubkey");

  if (!/^[0-9a-f]{64}$/i.test(browserNknSeedHex)) {
    browserNknSeedHex = generateSeedHex();
    localStorage.setItem("browserNknSeedHex", browserNknSeedHex);
  }

  setBrowserNknSeedStatus("Persisted in local storage");

  if (typeof nacl === "undefined" || !nacl.sign || !nacl.sign.keyPair) {
    browserNknPubHex = "";
    if (pubEl) {
      pubEl.textContent = "tweetnacl not loaded";
    }
    renderBrowserNknQr("");
    return;
  }

  const seedBytes = hexToBytes(browserNknSeedHex);
  if (!seedBytes || seedBytes.length !== 32) {
    browserNknPubHex = "";
    if (pubEl) {
      pubEl.textContent = "Invalid seed";
    }
    renderBrowserNknQr("");
    return;
  }

  try {
    const kp = nacl.sign.keyPair.fromSeed(seedBytes);
    browserNknPubHex = bytesToHex(kp.publicKey);
    localStorage.setItem("browserNknPubHex", browserNknPubHex);
    if (pubEl) {
      pubEl.textContent = browserNknPubHex;
    }
    renderBrowserNknQr(browserNknPubHex);
  } catch (err) {
    browserNknPubHex = "";
    if (pubEl) {
      pubEl.textContent = `Key derivation failed: ${err}`;
    }
    renderBrowserNknQr("");
  }
}

function sleepMs(durationMs) {
  return new Promise((resolve) => setTimeout(resolve, durationMs));
}

function isLikelyNknAddress(address) {
  const value = String(address || "").trim();
  if (!value) {
    return false;
  }
  return /^[a-zA-Z0-9._-]+\.[0-9a-fA-F]{64}$/.test(value) || /^[0-9a-fA-F]{64}$/.test(value);
}

function normalizeNknAddress(rawInput) {
  const raw = String(rawInput || "").trim();
  if (!raw) {
    return "";
  }
  const stripped = raw.replace(/^nkn:\/\//i, "").trim();
  if (!stripped) {
    return "";
  }
  if (/^[0-9a-fA-F]{64}$/.test(stripped)) {
    return stripped.toLowerCase();
  }
  const parts = stripped.split(".").filter(Boolean);
  if (parts.length < 2) {
    return "";
  }
  const pub = String(parts[parts.length - 1] || "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(pub)) {
    return "";
  }
  const prefix = parts.slice(0, -1).join(".");
  return prefix ? `${prefix}.${pub}` : pub;
}

function nknEndpointForAddress(address) {
  const normalized = normalizeNknAddress(address);
  return normalized ? `nkn://${normalized}` : "";
}

function parseServiceEndpoint(endpoint) {
  const raw = String(endpoint || "").trim();
  if (!raw) {
    return { transport: "", value: "", nknAddress: "" };
  }
  if (/^nkn:\/\//i.test(raw) || isLikelyNknAddress(raw)) {
    const nknAddress = normalizeNknAddress(raw);
    return {
      transport: nknAddress ? SERVICE_TRANSPORT_NKN : "",
      value: nknAddress ? nknEndpointForAddress(nknAddress) : "",
      nknAddress,
    };
  }
  let origin = "";
  try {
    origin = normalizeServiceOrigin(raw);
  } catch (err) {
    return { transport: "", value: "", nknAddress: "" };
  }
  return {
    transport: origin ? SERVICE_TRANSPORT_HTTP : "",
    value: origin,
    nknAddress: "",
  };
}

function getServiceTransportMode(service) {
  const key = String(service || "").trim().toLowerCase();
  if (key !== "adapter" && key !== "camera" && key !== "audio") {
    return SERVICE_TRANSPORT_HTTP;
  }
  return serviceTransportModes[key] || SERVICE_TRANSPORT_HTTP;
}

function setServiceTransportMode(service, mode) {
  const key = String(service || "").trim().toLowerCase();
  if (key !== "adapter" && key !== "camera" && key !== "audio") {
    return;
  }
  const next = String(mode || "").trim().toLowerCase() === SERVICE_TRANSPORT_NKN
    ? SERVICE_TRANSPORT_NKN
    : SERVICE_TRANSPORT_HTTP;
  serviceTransportModes[key] = next;
}

function setServiceNknAddress(service, address) {
  const key = String(service || "").trim().toLowerCase();
  if (key !== "adapter" && key !== "camera" && key !== "audio") {
    return;
  }
  serviceNknAddresses[key] = normalizeNknAddress(address);
}

function getServiceNknAddress(service) {
  const key = String(service || "").trim().toLowerCase();
  if (key !== "adapter" && key !== "camera" && key !== "audio") {
    return "";
  }
  return normalizeNknAddress(serviceNknAddresses[key]);
}

function resolveServiceNknTarget(service) {
  const preferred = getServiceNknAddress(service);
  if (preferred) {
    return preferred;
  }
  return normalizeNknAddress(routerTargetNknAddress || "");
}

function payloadValueToText(payload) {
  if (typeof payload === "string") {
    return payload;
  }
  if (payload instanceof Uint8Array) {
    try {
      return new TextDecoder().decode(payload);
    } catch (err) {
      return "";
    }
  }
  if (payload instanceof ArrayBuffer) {
    try {
      return new TextDecoder().decode(new Uint8Array(payload));
    } catch (err) {
      return "";
    }
  }
  if (payload && payload.buffer instanceof ArrayBuffer && payload.byteLength !== undefined) {
    try {
      return new TextDecoder().decode(new Uint8Array(payload.buffer, payload.byteOffset || 0, payload.byteLength || 0));
    } catch (err) {
      return "";
    }
  }
  if (payload === null || payload === undefined) {
    return "";
  }
  return String(payload);
}

function parseIncomingNknMessage(a, b) {
  let source = "";
  let payload = "";
  if (a && typeof a === "object" && Object.prototype.hasOwnProperty.call(a, "payload")) {
    source = String(a.src || a.from || "");
    payload = payloadValueToText(a.payload);
  } else {
    source = String(a || "");
    payload = payloadValueToText(b);
  }
  return { source, payload };
}

function asObject(value) {
  return (value && typeof value === "object") ? value : {};
}

function pickFirstNonEmptyString(...values) {
  for (const value of values) {
    const text = String(value || "").trim();
    if (text) {
      return text;
    }
  }
  return "";
}

function normalizeServiceOrigin(rawInput) {
  const raw = String(rawInput || "").trim();
  if (!raw) {
    return "";
  }
  const candidate = raw.includes("://") ? raw : `https://${raw}`;
  const parsed = new URL(candidate);
  const protocol = parsed.protocol === "ws:"
    ? "http:"
    : parsed.protocol === "wss:"
      ? "https:"
      : parsed.protocol;
  return `${protocol}//${parsed.host}`;
}

function isCloudflareTunnelHostname(hostname) {
  const host = String(hostname || "").trim().toLowerCase();
  if (!host) {
    return false;
  }
  return host.endsWith(".trycloudflare.com") || host.endsWith(".cfargotunnel.com");
}

function isCloudflareTunnelOrigin(rawInput) {
  const raw = String(rawInput || "").trim();
  if (!raw) {
    return false;
  }
  try {
    const candidate = raw.includes("://") ? raw : `https://${raw}`;
    const parsed = new URL(candidate);
    return isCloudflareTunnelHostname(parsed.hostname);
  } catch (err) {
    return false;
  }
}

function isLoopbackHostname(hostname) {
  const host = String(hostname || "").trim().toLowerCase();
  if (!host) {
    return false;
  }
  if (host === "localhost" || host === "::1" || host === "[::1]") {
    return true;
  }
  if (host.endsWith(".localhost")) {
    return true;
  }
  if (host.startsWith("127.")) {
    return true;
  }
  return false;
}

function isLoopbackServiceOrigin(rawInput) {
  const raw = String(rawInput || "").trim();
  if (!raw) {
    return false;
  }
  try {
    const candidate = raw.includes("://") ? raw : `https://${raw}`;
    const parsed = new URL(candidate);
    return isLoopbackHostname(parsed.hostname);
  } catch (err) {
    return false;
  }
}

function isRoutableServiceOrigin(rawInput) {
  const raw = String(rawInput || "").trim();
  if (!raw) {
    return false;
  }
  try {
    const origin = normalizeServiceOrigin(raw);
    if (!origin) {
      return false;
    }
    return !isLoopbackServiceOrigin(origin);
  } catch (err) {
    return false;
  }
}

function sanitizeStoredRemoteOrigin(rawOrigin, storageKey = "") {
  const value = String(rawOrigin || "").trim();
  if (!value) {
    return "";
  }
  if (/^nkn:\/\//i.test(value) || isLikelyNknAddress(value)) {
    const endpoint = nknEndpointForAddress(value);
    if (endpoint) {
      return endpoint;
    }
  }
  if (isRoutableServiceOrigin(value)) {
    try {
      return normalizeServiceOrigin(value);
    } catch (err) {
      return "";
    }
  }
  if (storageKey) {
    try {
      localStorage.removeItem(storageKey);
    } catch (err) {}
  }
  return "";
}

function pickPreferredResolvedOrigin(candidates, currentOrigin, serviceLabel) {
  let firstCloudflare = "";
  let firstRoutable = "";
  let firstNkn = "";
  for (const value of candidates) {
    const text = String(value || "").trim();
    if (!text) {
      continue;
    }
    if (/^nkn:\/\//i.test(text) || isLikelyNknAddress(text)) {
      const endpoint = nknEndpointForAddress(text);
      if (endpoint && !firstNkn) {
        firstNkn = endpoint;
      }
      continue;
    }
    let origin = "";
    try {
      origin = normalizeServiceOrigin(text);
    } catch (err) {
      continue;
    }
    if (!origin) {
      continue;
    }
    if (isCloudflareTunnelOrigin(origin)) {
      if (!firstCloudflare) {
        firstCloudflare = origin;
      }
      continue;
    }
    if (isRoutableServiceOrigin(origin) && !firstRoutable) {
      firstRoutable = origin;
    }
  }

  if (firstCloudflare) {
    return firstCloudflare;
  }
  if (firstRoutable) {
    return firstRoutable;
  }
  if (firstNkn) {
    return firstNkn;
  }

  const currentRemote = sanitizeStoredRemoteOrigin(currentOrigin);
  if (currentRemote) {
    return currentRemote;
  }
  if (serviceLabel) {
    logToConsole(`[ROUTER] Ignoring loopback ${serviceLabel} endpoint update`);
  }
  return "";
}

function pickFirstValidServiceOrigin(...values) {
  for (const value of values) {
    const text = String(value || "").trim();
    if (!text) {
      continue;
    }
    try {
      const origin = normalizeServiceOrigin(text);
      if (origin) {
        return origin;
      }
    } catch (err) {}
  }
  return "";
}

function getServiceData(root, serviceName) {
  const source = asObject(root);
  const services = asObject(source.services);
  const entry = asObject(services[serviceName]);
  const data = asObject(entry.data);
  if (Object.keys(data).length > 0) {
    return data;
  }
  return entry;
}

function extractResolvedFromPayload(data) {
  const source = asObject(data);
  const snapshot = asObject(source.snapshot);
  const reply = asObject(source.reply);
  const replySnapshot = asObject(reply.snapshot);
  const resolved = asObject(
    source.resolved ||
    snapshot.resolved ||
    replySnapshot.resolved ||
    {}
  );

  const resolvedAdapter = asObject(resolved.adapter);
  const resolvedCamera = asObject(resolved.camera);
  const resolvedAudio = asObject(resolved.audio);

  const adapterServiceCandidates = [
    getServiceData(snapshot, "adapter"),
    getServiceData(replySnapshot, "adapter"),
    getServiceData(source, "adapter"),
  ];
  const cameraServiceCandidates = [
    getServiceData(snapshot, "camera"),
    getServiceData(replySnapshot, "camera"),
    getServiceData(source, "camera"),
  ];
  const audioServiceCandidates = [
    getServiceData(snapshot, "audio"),
    getServiceData(replySnapshot, "audio"),
    getServiceData(source, "audio"),
  ];

  const adapterService = {};
  for (const candidate of adapterServiceCandidates) {
    const service = asObject(candidate);
    const local = asObject(service.local);
    const tunnel = asObject(service.tunnel);
    const fallback = asObject(service.fallback);
    const fallbackNkn = asObject(fallback.nkn);
    adapterService.tunnel_url = pickFirstNonEmptyString(adapterService.tunnel_url, service.tunnel_url, tunnel.tunnel_url);
    adapterService.transport = pickFirstNonEmptyString(
      adapterService.transport,
      service.transport,
      fallback.selected_transport
    );
    adapterService.nkn_address = pickFirstNonEmptyString(
      adapterService.nkn_address,
      service.nkn_address,
      fallbackNkn.nkn_address,
      fallbackNkn.address
    );
    adapterService.http_endpoint = pickFirstNonEmptyString(
      adapterService.http_endpoint,
      service.http_endpoint,
      tunnel.http_endpoint,
      local.http_endpoint
    );
    adapterService.ws_endpoint = pickFirstNonEmptyString(
      adapterService.ws_endpoint,
      service.ws_endpoint,
      tunnel.ws_endpoint,
      local.ws_endpoint
    );
    adapterService.local_http_endpoint = pickFirstNonEmptyString(
      adapterService.local_http_endpoint,
      service.local_http_endpoint,
      local.http_endpoint
    );
    adapterService.local_ws_endpoint = pickFirstNonEmptyString(
      adapterService.local_ws_endpoint,
      service.local_ws_endpoint,
      local.ws_endpoint
    );
    adapterService.base_url = pickFirstNonEmptyString(adapterService.base_url, service.base_url, local.base_url);
    if (!adapterService.local && Object.keys(local).length > 0) {
      adapterService.local = local;
    }
    if (!adapterService.tunnel && Object.keys(tunnel).length > 0) {
      adapterService.tunnel = tunnel;
    }
    if (!adapterService.fallback && Object.keys(fallback).length > 0) {
      adapterService.fallback = fallback;
    }
  }

  const cameraService = {};
  for (const candidate of cameraServiceCandidates) {
    const service = asObject(candidate);
    const local = asObject(service.local);
    const tunnel = asObject(service.tunnel);
    const fallback = asObject(service.fallback);
    const fallbackNkn = asObject(fallback.nkn);
    cameraService.tunnel_url = pickFirstNonEmptyString(cameraService.tunnel_url, service.tunnel_url, tunnel.tunnel_url);
    cameraService.transport = pickFirstNonEmptyString(
      cameraService.transport,
      service.transport,
      fallback.selected_transport
    );
    cameraService.nkn_address = pickFirstNonEmptyString(
      cameraService.nkn_address,
      service.nkn_address,
      fallbackNkn.nkn_address,
      fallbackNkn.address
    );
    cameraService.base_url = pickFirstNonEmptyString(
      cameraService.base_url,
      tunnel.tunnel_url,
      service.base_url,
      local.base_url
    );
    cameraService.list_url = pickFirstNonEmptyString(cameraService.list_url, service.list_url, tunnel.list_url, local.list_url);
    cameraService.health_url = pickFirstNonEmptyString(
      cameraService.health_url,
      service.health_url,
      tunnel.health_url,
      local.health_url
    );
    cameraService.frame_packet_template = pickFirstNonEmptyString(
      cameraService.frame_packet_template,
      service.frame_packet_template,
      tunnel.frame_packet_template,
      local.frame_packet_template
    );
    cameraService.local_base_url = pickFirstNonEmptyString(cameraService.local_base_url, service.local_base_url, local.base_url);
    if (!cameraService.local && Object.keys(local).length > 0) {
      cameraService.local = local;
    }
    if (!cameraService.tunnel && Object.keys(tunnel).length > 0) {
      cameraService.tunnel = tunnel;
    }
    if (!cameraService.fallback && Object.keys(fallback).length > 0) {
      cameraService.fallback = fallback;
    }
  }

  const audioService = {};
  for (const candidate of audioServiceCandidates) {
    const service = asObject(candidate);
    const local = asObject(service.local);
    const tunnel = asObject(service.tunnel);
    const fallback = asObject(service.fallback);
    const fallbackNkn = asObject(fallback.nkn);
    audioService.tunnel_url = pickFirstNonEmptyString(audioService.tunnel_url, service.tunnel_url, tunnel.tunnel_url);
    audioService.transport = pickFirstNonEmptyString(
      audioService.transport,
      service.transport,
      fallback.selected_transport
    );
    audioService.nkn_address = pickFirstNonEmptyString(
      audioService.nkn_address,
      service.nkn_address,
      fallbackNkn.nkn_address,
      fallbackNkn.address
    );
    audioService.base_url = pickFirstNonEmptyString(
      audioService.base_url,
      tunnel.tunnel_url,
      service.base_url,
      local.base_url
    );
    audioService.list_url = pickFirstNonEmptyString(audioService.list_url, service.list_url, tunnel.list_url, local.list_url);
    audioService.health_url = pickFirstNonEmptyString(
      audioService.health_url,
      service.health_url,
      tunnel.health_url,
      local.health_url
    );
    audioService.webrtc_offer_url = pickFirstNonEmptyString(
      audioService.webrtc_offer_url,
      service.webrtc_offer_url,
      tunnel.webrtc_offer_url,
      local.webrtc_offer_url
    );
    audioService.local_base_url = pickFirstNonEmptyString(audioService.local_base_url, service.local_base_url, local.base_url);
    if (!audioService.local && Object.keys(local).length > 0) {
      audioService.local = local;
    }
    if (!audioService.tunnel && Object.keys(tunnel).length > 0) {
      audioService.tunnel = tunnel;
    }
    if (!audioService.fallback && Object.keys(fallback).length > 0) {
      audioService.fallback = fallback;
    }
  }

  return {
    ...resolved,
    adapter: {
      ...resolvedAdapter,
      transport: pickFirstNonEmptyString(resolvedAdapter.transport, adapterService.transport),
      nkn_address: pickFirstNonEmptyString(resolvedAdapter.nkn_address, adapterService.nkn_address),
      tunnel_url: pickFirstNonEmptyString(resolvedAdapter.tunnel_url, adapterService.tunnel_url),
      http_endpoint: pickFirstNonEmptyString(
        resolvedAdapter.http_endpoint,
        resolvedAdapter.local_http_endpoint,
        adapterService.http_endpoint,
        adapterService.local_http_endpoint
      ),
      ws_endpoint: pickFirstNonEmptyString(
        resolvedAdapter.ws_endpoint,
        resolvedAdapter.local_ws_endpoint,
        adapterService.ws_endpoint,
        adapterService.local_ws_endpoint
      ),
      local_http_endpoint: pickFirstNonEmptyString(
        resolvedAdapter.local_http_endpoint,
        adapterService.local_http_endpoint,
        adapterService.http_endpoint
      ),
      local_ws_endpoint: pickFirstNonEmptyString(
        resolvedAdapter.local_ws_endpoint,
        adapterService.local_ws_endpoint,
        adapterService.ws_endpoint
      ),
      base_url: pickFirstNonEmptyString(resolvedAdapter.base_url, adapterService.base_url),
      fallback: Object.keys(asObject(resolvedAdapter.fallback)).length > 0 ? asObject(resolvedAdapter.fallback) : asObject(adapterService.fallback),
      local: Object.keys(asObject(resolvedAdapter.local)).length > 0 ? asObject(resolvedAdapter.local) : asObject(adapterService.local),
      tunnel: Object.keys(asObject(resolvedAdapter.tunnel)).length > 0 ? asObject(resolvedAdapter.tunnel) : asObject(adapterService.tunnel),
    },
    camera: {
      ...resolvedCamera,
      transport: pickFirstNonEmptyString(resolvedCamera.transport, cameraService.transport),
      nkn_address: pickFirstNonEmptyString(resolvedCamera.nkn_address, cameraService.nkn_address),
      tunnel_url: pickFirstNonEmptyString(resolvedCamera.tunnel_url, cameraService.tunnel_url),
      base_url: pickFirstNonEmptyString(resolvedCamera.base_url, cameraService.base_url),
      list_url: pickFirstNonEmptyString(resolvedCamera.list_url, cameraService.list_url),
      health_url: pickFirstNonEmptyString(resolvedCamera.health_url, cameraService.health_url),
      frame_packet_template: pickFirstNonEmptyString(resolvedCamera.frame_packet_template, cameraService.frame_packet_template),
      local_base_url: pickFirstNonEmptyString(resolvedCamera.local_base_url, cameraService.local_base_url),
      fallback: Object.keys(asObject(resolvedCamera.fallback)).length > 0 ? asObject(resolvedCamera.fallback) : asObject(cameraService.fallback),
      local: Object.keys(asObject(resolvedCamera.local)).length > 0 ? asObject(resolvedCamera.local) : asObject(cameraService.local),
      tunnel: Object.keys(asObject(resolvedCamera.tunnel)).length > 0 ? asObject(resolvedCamera.tunnel) : asObject(cameraService.tunnel),
    },
    audio: {
      ...resolvedAudio,
      transport: pickFirstNonEmptyString(resolvedAudio.transport, audioService.transport),
      nkn_address: pickFirstNonEmptyString(resolvedAudio.nkn_address, audioService.nkn_address),
      tunnel_url: pickFirstNonEmptyString(audioService.tunnel_url, resolvedAudio.tunnel_url),
      base_url: pickFirstNonEmptyString(audioService.base_url, resolvedAudio.base_url),
      list_url: pickFirstNonEmptyString(audioService.list_url, resolvedAudio.list_url),
      health_url: pickFirstNonEmptyString(audioService.health_url, resolvedAudio.health_url),
      webrtc_offer_url: pickFirstNonEmptyString(audioService.webrtc_offer_url, resolvedAudio.webrtc_offer_url),
      local_base_url: pickFirstNonEmptyString(audioService.local_base_url, resolvedAudio.local_base_url),
      fallback: Object.keys(asObject(resolvedAudio.fallback)).length > 0 ? asObject(resolvedAudio.fallback) : asObject(audioService.fallback),
      local: Object.keys(asObject(resolvedAudio.local)).length > 0 ? asObject(resolvedAudio.local) : asObject(audioService.local),
      tunnel: Object.keys(asObject(resolvedAudio.tunnel)).length > 0 ? asObject(resolvedAudio.tunnel) : asObject(audioService.tunnel),
    },
  };
}

function buildResolveRequestPayload(requestId) {
  return {
    event: "resolve_tunnels",
    request_id: requestId,
    from: browserNknClientAddress || browserNknPubHex || "",
    browser_pubkey_hex: browserNknPubHex || "",
    timestamp_ms: Date.now(),
  };
}

function buildCameraFrameRequestPayload(requestId, cameraId, options = {}) {
  const payload = {
    event: "camera_frame_request",
    request_id: requestId,
    timestamp_ms: Date.now(),
    from: browserNknClientAddress || browserNknPubHex || "",
    camera_id: String(cameraId || "").trim(),
    options: {
      max_width: Number(options.max_width) || ROUTER_NKN_FRAME_MAX_WIDTH,
      max_height: Number(options.max_height) || ROUTER_NKN_FRAME_MAX_HEIGHT,
      max_kbps: Number(options.max_kbps) || ROUTER_NKN_FRAME_MAX_KBPS,
      interval_ms: Number(options.interval_ms) || ROUTER_NKN_FRAME_POLL_INTERVAL_MS,
      quality: Number(options.quality) || 56,
      min_quality: Number(options.min_quality) || 22,
      grayscale: !!options.grayscale,
    },
  };
  const auth = {};
  const sessionKey = String(cameraRouterSessionKey || "").trim();
  const password = String(cameraRouterPassword || "").trim();
  if (sessionKey) {
    auth.session_key = sessionKey;
  }
  if (password) {
    auth.password = password;
  }
  if (Object.keys(auth).length > 0) {
    payload.auth = auth;
  }
  return payload;
}

function clearPendingNknResolveRequests(reason) {
  for (const [requestId, pending] of pendingNknResolveRequests.entries()) {
    clearTimeout(pending.timeoutHandle);
    if (typeof pending.reject === "function") {
      pending.reject(new Error(reason || `Resolve ${requestId} cancelled`));
    }
  }
  pendingNknResolveRequests.clear();
}

function clearPendingNknFrameRequests(reason) {
  for (const [requestId, pending] of pendingNknFrameRequests.entries()) {
    clearTimeout(pending.timeoutHandle);
    if (typeof pending.reject === "function") {
      pending.reject(new Error(reason || `Frame request ${requestId} cancelled`));
    }
  }
  pendingNknFrameRequests.clear();
}

function clearPendingNknServiceRpcRequests(reason) {
  for (const [requestId, pending] of pendingNknServiceRpcRequests.entries()) {
    clearTimeout(pending.timeoutHandle);
    if (typeof pending.reject === "function") {
      pending.reject(new Error(reason || `RPC request ${requestId} cancelled`));
    }
  }
  pendingNknServiceRpcRequests.clear();
}

function closeBrowserNknClient() {
  const client = browserNknClient;
  browserNknClient = null;
  browserNknClientReady = false;
  browserNknClientAddress = "";
  if (client && typeof client.close === "function") {
    try {
      client.close();
    } catch (err) {}
  }
  clearPendingNknResolveRequests("Browser NKN client restarted");
  clearPendingNknFrameRequests("Browser NKN client restarted");
  clearPendingNknServiceRpcRequests("Browser NKN client restarted");
}

function handleNknResolveMessage(source, data) {
  const requestId = String((data || {}).request_id || "").trim();
  if (requestId) {
    const pending = pendingNknResolveRequests.get(requestId);
    if (pending) {
      clearTimeout(pending.timeoutHandle);
      pendingNknResolveRequests.delete(requestId);
      pending.resolve({
        source: String(source || ""),
        payload: data,
        resolved: extractResolvedFromPayload(data),
      });
      return;
    }
  }

  const resolved = extractResolvedFromPayload(data);
  const applied = applyResolvedEndpoints(resolved);
  if (applied) {
    setRouterResolveStatus(`Received endpoint update from ${source || "router"} via NKN`);
    updateMetrics();
  }
}

function handleNknCameraFrameMessage(source, data) {
  const requestId = String((data || {}).request_id || "").trim();
  if (!requestId) {
    return;
  }
  const pending = pendingNknFrameRequests.get(requestId);
  if (!pending) {
    return;
  }
  clearTimeout(pending.timeoutHandle);
  pendingNknFrameRequests.delete(requestId);

  const status = String((data || {}).status || "").trim().toLowerCase();
  const framePacket = asObject(data.frame_packet);
  const frame = String(framePacket.frame || "").trim();
  if (status === "success" && frame) {
    pending.resolve({
      source: String(source || ""),
      payload: data,
      framePacket,
    });
    return;
  }
  const message = String((data && data.message) || "Camera frame relay failed");
  pending.reject(new Error(message));
}

function createNknRpcResponse(result) {
  const payload = asObject(result);
  const status = Number(payload.status_code || 0) || 0;
  const ok = typeof payload.ok === "boolean" ? payload.ok : (status >= 200 && status < 300);
  const headers = asObject(payload.headers);
  const contentType = String(headers.content_type || headers["content-type"] || "").trim();
  const bodyKind = String(payload.body_kind || "none").trim().toLowerCase();
  const body = payload.body;

  const toText = () => {
    if (bodyKind === "text") {
      return String(body || "");
    }
    if (bodyKind === "json") {
      try {
        return JSON.stringify(body ?? {});
      } catch (err) {
        return "";
      }
    }
    if (bodyKind === "base64") {
      try {
        return atob(String(body || ""));
      } catch (err) {
        return "";
      }
    }
    return "";
  };

  return {
    ok,
    status,
    statusText: String(payload.reason || ""),
    redirected: false,
    url: "",
    headers: {
      get(name) {
        const key = String(name || "").trim().toLowerCase();
        if (!key) {
          return "";
        }
        if (key === "content-type") {
          return contentType;
        }
        const found = Object.entries(headers).find(([header]) => String(header || "").trim().toLowerCase() === key);
        return found ? String(found[1] || "") : "";
      },
    },
    async json() {
      if (bodyKind === "json") {
        return body;
      }
      if (bodyKind === "text") {
        try {
          return JSON.parse(String(body || ""));
        } catch (err) {
          return {};
        }
      }
      return {};
    },
    async text() {
      return toText();
    },
  };
}

function handleNknServiceRpcMessage(source, data) {
  const requestId = String((data || {}).request_id || "").trim();
  if (!requestId) {
    return;
  }
  const pending = pendingNknServiceRpcRequests.get(requestId);
  if (!pending) {
    return;
  }
  clearTimeout(pending.timeoutHandle);
  pendingNknServiceRpcRequests.delete(requestId);

  const status = String((data || {}).status || "").trim().toLowerCase();
  if (status === "success") {
    pending.resolve(createNknRpcResponse(asObject(data.result)));
    return;
  }
  const message = String((data && data.message) || "Service RPC failed");
  pending.reject(new Error(message));
}

function handleBrowserNknMessage(a, b) {
  const incoming = parseIncomingNknMessage(a, b);
  if (!incoming.payload) {
    return;
  }

  let data = null;
  try {
    data = JSON.parse(incoming.payload);
  } catch (err) {
    return;
  }
  if (!data || typeof data !== "object") {
    return;
  }

  const eventName = String(data.event || data.type || "").trim().toLowerCase();
  if (eventName === "resolve_tunnels_result" || eventName === "router_info_result") {
    handleNknResolveMessage(incoming.source, data);
    return;
  }
  if (eventName === "camera_frame_result") {
    handleNknCameraFrameMessage(incoming.source, data);
    return;
  }
  if (eventName === "service_rpc_result") {
    handleNknServiceRpcMessage(incoming.source, data);
  }
}

function getNknSubclientRetryOrder(initialCount) {
  const seed = Math.max(1, Number(initialCount) || ROUTER_NKN_SUBCLIENTS);
  const order = [seed];
  const lower = ROUTER_NKN_SUBCLIENT_FALLBACKS.filter((value) => Number(value) < seed);
  const higher = ROUTER_NKN_SUBCLIENT_FALLBACKS.filter((value) => Number(value) > seed);
  const combined = [...lower, ...higher];
  combined.forEach((value) => {
    const next = Math.max(1, Number(value) || 0);
    if (!next || order.includes(next)) {
      return;
    }
    order.push(next);
  });
  return order.length > 0 ? order : [ROUTER_NKN_SUBCLIENTS];
}

function syncBrowserNknReadyState(client) {
  if (!client) {
    return;
  }
  if (client.isReady && !browserNknClientReady) {
    browserNknClientReady = true;
    browserNknClientAddress = String(client.addr || "").trim();
  }
  if (client.isFailed && !browserNknClientReady) {
    setBrowserNknSeedStatus("Connect failed", true);
  }
}

function attachBrowserNknClientHandlers(client) {
  const onReady = () => {
    browserNknClientReady = true;
    browserNknClientAddress = String(client.addr || "").trim();
    const statusBits = ["Browser NKN client ready"];
    statusBits.push(`subclients=${browserNknSubclientCount}`);
    if (browserNknClientAddress) {
      statusBits.push(browserNknClientAddress);
    }
    if (routerTargetNknAddress) {
      statusBits.push(`target ${routerTargetNknAddress}`);
    }
    setRouterResolveStatus(statusBits.join(" | "));
    setBrowserNknSeedStatus("Connected to NKN");
  };

  const onWarning = (err) => {
    const message = err && err.message ? err.message : String(err || "unknown");
    setRouterResolveStatus(`NKN transport warning: ${message}`, true);
    setBrowserNknSeedStatus("Connection warning", true);
  };

  if (typeof client.onConnect === "function") {
    client.onConnect(onReady);
  }
  if (typeof client.onConnectFailed === "function") {
    client.onConnectFailed(() => {
      browserNknClientReady = false;
      setRouterResolveStatus("NKN connect failed", true);
      setBrowserNknSeedStatus("Connect failed", true);
    });
  }
  if (typeof client.onWsError === "function") {
    client.onWsError(onWarning);
  }
  if (typeof client.onMessage === "function") {
    client.onMessage(handleBrowserNknMessage);
  }

  if (typeof client.on === "function") {
    client.on("connect", onReady);
    client.on("wsError", onWarning);
    client.on("message", handleBrowserNknMessage);
  }

  // Some sdk builds can become ready before listeners are fully attached.
  syncBrowserNknReadyState(client);
  if (client.isReady) {
    onReady();
  } else if (client.isFailed) {
    browserNknClientReady = false;
    setRouterResolveStatus("NKN connect failed", true);
    setBrowserNknSeedStatus("Connect failed", true);
  }
}

async function ensureBrowserNknClient(options = {}) {
  const forceReconnect = !!options.forceReconnect;
  const requestedSubclients = Math.max(
    1,
    Number(options.subclients) || Number(browserNknSubclientCount) || ROUTER_NKN_SUBCLIENTS
  );
  browserNknSubclientCount = requestedSubclients;
  if (forceReconnect) {
    closeBrowserNknClient();
  }
  if (browserNknClient) {
    syncBrowserNknReadyState(browserNknClient);
    return browserNknClient;
  }
  if (nknClientInitPromise) {
    return nknClientInitPromise;
  }

  nknClientInitPromise = (async () => {
    ensureBrowserNknIdentity();

    if (
      typeof nkn === "undefined" ||
      !nkn ||
      (typeof nkn.MultiClient !== "function" && typeof nkn.Client !== "function")
    ) {
      throw new Error("nkn-sdk browser library not loaded");
    }
    if (!/^[0-9a-f]{64}$/i.test(browserNknSeedHex)) {
      throw new Error("Browser seed is invalid");
    }

    browserNknClientReady = false;
    browserNknClientAddress = "";
    const canUseMultiClient = typeof nkn.MultiClient === "function";
    const nknClientType = canUseMultiClient ? "MultiClient" : "Client";
    const startupSubclientInfo = canUseMultiClient
      ? `subclients=${browserNknSubclientCount}`
      : "single-client fallback";
    setRouterResolveStatus(
      `Starting browser NKN ${nknClientType} (${startupSubclientInfo})...`
    );
    setBrowserNknSeedStatus("Connecting to NKN...");
    let client = null;
    if (canUseMultiClient) {
      client = new nkn.MultiClient({
        seed: browserNknSeedHex,
        identifier: ROUTER_NKN_IDENTIFIER,
        numSubClients: browserNknSubclientCount,
      });
    } else {
      browserNknSubclientCount = 1;
      client = new nkn.Client({
        seed: browserNknSeedHex,
        identifier: ROUTER_NKN_IDENTIFIER,
      });
    }
    browserNknClient = client;
    attachBrowserNknClientHandlers(client);

    if (client && client.addr) {
      browserNknClientAddress = String(client.addr).trim();
    }
    return client;
  })();

  try {
    return await nknClientInitPromise;
  } finally {
    nknClientInitPromise = null;
  }
}

async function waitForBrowserNknReady(timeoutMs = ROUTER_NKN_READY_TIMEOUT_MS) {
  const timeout = Math.max(500, Number(timeoutMs) || ROUTER_NKN_READY_TIMEOUT_MS);
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    syncBrowserNknReadyState(browserNknClient);
    if (browserNknClientReady) {
      return true;
    }
    if (browserNknClient && browserNknClient.isFailed) {
      return false;
    }
    await sleepMs(120);
  }
  syncBrowserNknReadyState(browserNknClient);
  return browserNknClientReady;
}

function applyResolvedEndpoints(resolved) {
  if (!resolved || typeof resolved !== "object") {
    return false;
  }

  let changed = false;
  const adapter = resolved.adapter || {};
  const camera = resolved.camera || {};
  const audio = resolved.audio || {};
  const adapterFallback = asObject(adapter.fallback);
  const cameraFallback = asObject(camera.fallback);
  const audioFallback = asObject(audio.fallback);
  const adapterFallbackNkn = asObject(adapterFallback.nkn);
  const cameraFallbackNkn = asObject(cameraFallback.nkn);
  const audioFallbackNkn = asObject(audioFallback.nkn);
  const adapterTunnel = (adapter.tunnel && typeof adapter.tunnel === "object") ? adapter.tunnel : {};
  const adapterLocal = (adapter.local && typeof adapter.local === "object") ? adapter.local : {};
  const cameraTunnel = (camera.tunnel && typeof camera.tunnel === "object") ? camera.tunnel : {};
  const cameraLocal = (camera.local && typeof camera.local === "object") ? camera.local : {};
  const audioTunnel = (audio.tunnel && typeof audio.tunnel === "object") ? audio.tunnel : {};
  const audioLocal = (audio.local && typeof audio.local === "object") ? audio.local : {};
  let resolvedAdapterEndpoint = "";
  let resolvedCameraEndpoint = "";
  let resolvedAudioEndpoint = "";
  const previousAdapterTransport = getServiceTransportMode("adapter");
  const previousAdapterNknAddress = getServiceNknAddress("adapter");
  const previousCameraTransport = getServiceTransportMode("camera");
  const previousCameraNknAddress = getServiceNknAddress("camera");
  const previousAudioTransport = getServiceTransportMode("audio");
  const previousAudioNknAddress = getServiceNknAddress("audio");

  const adapterCandidates = [
    adapter.tunnel_url,
    adapterTunnel.tunnel_url,
    adapter.http_endpoint,
    adapterTunnel.http_endpoint,
    adapter.local_http_endpoint,
    adapterLocal.http_endpoint,
    adapter.ws_endpoint,
    adapterTunnel.ws_endpoint,
    adapter.local_ws_endpoint,
    adapterLocal.ws_endpoint,
    adapter.base_url,
    adapter.origin,
    adapter.url,
    adapter.local_base_url,
    adapterLocal.base_url,
    adapter.nkn_address,
    adapterFallbackNkn.nkn_address,
    adapterFallbackNkn.address,
  ];
  let firstCloudflareAdapterEndpoints = null;
  let firstRoutableAdapterEndpoints = null;
  let adapterNknAddress = normalizeNknAddress(
    pickFirstNonEmptyString(
      adapter.nkn_address,
      adapterFallbackNkn.nkn_address,
      adapterFallbackNkn.address
    )
  );
  for (const candidate of adapterCandidates) {
    const parsed = buildAdapterEndpoints(candidate);
    if (parsed) {
      if (parsed.transport === SERVICE_TRANSPORT_NKN) {
        if (!adapterNknAddress) {
          adapterNknAddress = normalizeNknAddress(parsed.nknAddress || parsed.origin);
        }
        continue;
      }
      if (isCloudflareTunnelOrigin(parsed.origin) && !firstCloudflareAdapterEndpoints) {
        firstCloudflareAdapterEndpoints = parsed;
      } else if (isRoutableServiceOrigin(parsed.origin) && !firstRoutableAdapterEndpoints) {
        firstRoutableAdapterEndpoints = parsed;
      }
    }
  }
  const currentAdapterEndpoints = buildAdapterEndpoints(HTTP_URL || WS_URL || "");
  let adapterEndpoints = firstCloudflareAdapterEndpoints;
  if (!adapterEndpoints && firstRoutableAdapterEndpoints) {
    adapterEndpoints = firstRoutableAdapterEndpoints;
  }
  if (!adapterEndpoints && currentAdapterEndpoints && isRoutableServiceOrigin(currentAdapterEndpoints.origin)) {
    adapterEndpoints = currentAdapterEndpoints;
  }
  if (!adapterEndpoints && Object.keys(adapter).length > 0) {
    logToConsole("[ROUTER] Ignoring loopback adapter endpoint update");
  }
  const adapterTransportHint = String(
    pickFirstNonEmptyString(adapter.transport, adapterFallback.selected_transport)
  ).trim().toLowerCase();

  if (adapterEndpoints && adapterEndpoints.transport === SERVICE_TRANSPORT_HTTP) {
    const nextHttp = String(adapterEndpoints.httpUrl || "").trim();
    const nextWs = String(adapterEndpoints.wsUrl || "").trim();
    const nextAddress = String(adapterEndpoints.origin || "").trim();
    const adapterChanged =
      HTTP_URL !== nextHttp ||
      WS_URL !== nextWs ||
      previousAdapterTransport !== SERVICE_TRANSPORT_HTTP ||
      !!previousAdapterNknAddress;
    resolvedAdapterEndpoint = nextAddress;

    setServiceTransportMode("adapter", SERVICE_TRANSPORT_HTTP);
    setServiceNknAddress("adapter", "");
    HTTP_URL = nextHttp;
    WS_URL = nextWs;
    localStorage.setItem("httpUrl", HTTP_URL);
    localStorage.setItem("wsUrl", WS_URL);
    const httpInput = document.getElementById("httpUrlInput");
    const wsInput = document.getElementById("wsUrlInput");
    const addressInput = document.getElementById("adapterAddressInput");
    if (httpInput) {
      httpInput.value = HTTP_URL;
    }
    if (wsInput) {
      wsInput.value = WS_URL;
    }
    if (addressInput) {
      addressInput.value = nextAddress;
    }
    setAdapterEndpointPreview(HTTP_URL, WS_URL);
    if (adapterChanged) {
      SESSION_KEY = "";
      authenticated = false;
      localStorage.removeItem("sessionKey");
      disableWebSocketMode();
      publishControlTransportState();
    }
    changed = changed || adapterChanged;
  } else if (adapterTransportHint === SERVICE_TRANSPORT_NKN || adapterNknAddress) {
    const nextNknAddress = normalizeNknAddress(adapterNknAddress || previousAdapterNknAddress || routerTargetNknAddress);
    if (nextNknAddress) {
      const adapterChanged =
        previousAdapterTransport !== SERVICE_TRANSPORT_NKN ||
        previousAdapterNknAddress !== nextNknAddress ||
        !!HTTP_URL ||
        !!WS_URL;
      resolvedAdapterEndpoint = nknEndpointForAddress(nextNknAddress);
      setServiceTransportMode("adapter", SERVICE_TRANSPORT_NKN);
      setServiceNknAddress("adapter", nextNknAddress);
      HTTP_URL = "";
      WS_URL = "";
      localStorage.removeItem("httpUrl");
      localStorage.removeItem("wsUrl");
      const httpInput = document.getElementById("httpUrlInput");
      const wsInput = document.getElementById("wsUrlInput");
      const addressInput = document.getElementById("adapterAddressInput");
      if (httpInput) {
        httpInput.value = "";
      }
      if (wsInput) {
        wsInput.value = "";
      }
      if (addressInput) {
        addressInput.value = resolvedAdapterEndpoint;
      }
      setAdapterEndpointPreview("", "", { transport: SERVICE_TRANSPORT_NKN, nknAddress: nextNknAddress });
      if (adapterChanged || SESSION_KEY || authenticated) {
        SESSION_KEY = "";
        authenticated = false;
        localStorage.removeItem("sessionKey");
        disableWebSocketMode();
        publishControlTransportState();
      }
      changed = changed || adapterChanged;
    }
  } else if (Object.keys(adapter).length > 0 || Object.keys(adapterTunnel).length > 0 || Object.keys(adapterLocal).length > 0) {
    const adapterHadValues = !!(HTTP_URL || WS_URL || previousAdapterNknAddress || previousAdapterTransport === SERVICE_TRANSPORT_NKN);
    setServiceTransportMode("adapter", SERVICE_TRANSPORT_HTTP);
    setServiceNknAddress("adapter", "");
    HTTP_URL = "";
    WS_URL = "";
    localStorage.removeItem("httpUrl");
    localStorage.removeItem("wsUrl");
    const httpInput = document.getElementById("httpUrlInput");
    const wsInput = document.getElementById("wsUrlInput");
    const addressInput = document.getElementById("adapterAddressInput");
    if (httpInput) {
      httpInput.value = "";
    }
    if (wsInput) {
      wsInput.value = "";
    }
    if (addressInput) {
      addressInput.value = "";
    }
    setAdapterEndpointPreview("", "");
    if (adapterHadValues || SESSION_KEY || authenticated) {
      SESSION_KEY = "";
      authenticated = false;
      localStorage.removeItem("sessionKey");
      disableWebSocketMode();
      publishControlTransportState();
    }
    changed = changed || adapterHadValues;
  }

  const cameraCandidate = pickPreferredResolvedOrigin([
    camera.tunnel_url,
    cameraTunnel.tunnel_url,
    camera.base_url,
    camera.nkn_address,
    cameraFallbackNkn.nkn_address,
    cameraFallbackNkn.address,
    camera.local_base_url,
    camera.list_url,
    camera.health_url,
    cameraTunnel.list_url,
    cameraTunnel.health_url,
    cameraLocal.base_url,
    cameraLocal.list_url,
    cameraLocal.health_url
  ], cameraRouterBaseUrl, "camera");
  const cameraTransportHint = String(
    pickFirstNonEmptyString(camera.transport, cameraFallback.selected_transport)
  ).trim().toLowerCase();
  const cameraNknAddress = normalizeNknAddress(
    pickFirstNonEmptyString(
      camera.nkn_address,
      cameraFallbackNkn.nkn_address,
      cameraFallbackNkn.address
    )
  );
  const effectiveCameraCandidate = cameraCandidate || (
    (cameraTransportHint === SERVICE_TRANSPORT_NKN && cameraNknAddress)
      ? nknEndpointForAddress(cameraNknAddress)
      : ""
  );
  if (effectiveCameraCandidate) {
    const parsedCameraEndpoint = parseServiceEndpoint(effectiveCameraCandidate);
    const nextCameraBase = parsedCameraEndpoint.value;
    const nextCameraTransport = parsedCameraEndpoint.transport || SERVICE_TRANSPORT_HTTP;
    const nextCameraNknAddress = parsedCameraEndpoint.nknAddress || "";
    const previousBase = cameraRouterBaseUrl;
    cameraRouterBaseUrl = nextCameraBase;
    setServiceTransportMode("camera", nextCameraTransport);
    setServiceNknAddress("camera", nextCameraNknAddress);
    resolvedCameraEndpoint = cameraRouterBaseUrl;
    const cameraChanged =
      previousBase !== cameraRouterBaseUrl ||
      previousCameraTransport !== nextCameraTransport ||
      previousCameraNknAddress !== nextCameraNknAddress;
    localStorage.setItem("cameraRouterBaseUrl", cameraRouterBaseUrl);
    const camInput = document.getElementById("cameraRouterBaseInput");
    if (camInput) {
      camInput.value = cameraRouterBaseUrl;
    }
    if (cameraChanged) {
      cameraRouterSessionKey = "";
      localStorage.removeItem("cameraRouterSessionKey");
      stopCameraImuStream();
      updateCameraImuReadouts(null, {
        message: "Camera endpoint changed. Re-authenticating...",
        error: false,
      });
      if (cameraPreview.desired) {
        stopCameraPreview({ keepDesired: true });
      } else if (typeof syncPinnedPreviewSource === "function") {
        syncPinnedPreviewSource();
      }
      if (typeof renderHybridFeedOptions === "function") {
        renderHybridFeedOptions();
      }
    }
    if (typeof syncPinnedPreviewSource === "function") {
      syncPinnedPreviewSource();
    }
    changed = changed || cameraChanged;
  } else if (Object.keys(camera).length > 0 || Object.keys(cameraTunnel).length > 0 || Object.keys(cameraLocal).length > 0) {
    const cameraHadValue = !!cameraRouterBaseUrl;
    const cameraHadTransport = previousCameraTransport === SERVICE_TRANSPORT_NKN || !!previousCameraNknAddress;
    setServiceTransportMode("camera", SERVICE_TRANSPORT_HTTP);
    setServiceNknAddress("camera", "");
    cameraRouterBaseUrl = "";
    cameraRouterSessionKey = "";
    localStorage.removeItem("cameraRouterBaseUrl");
    localStorage.removeItem("cameraRouterSessionKey");
    const camInput = document.getElementById("cameraRouterBaseInput");
    if (camInput) {
      camInput.value = "";
    }
    stopCameraImuStream();
    if (cameraPreview.desired) {
      stopCameraPreview({ keepDesired: false });
    } else if (typeof syncPinnedPreviewSource === "function") {
      syncPinnedPreviewSource();
    }
    if (typeof renderHybridFeedOptions === "function") {
      renderHybridFeedOptions();
    }
    changed = changed || cameraHadValue || cameraHadTransport;
  }

  const audioCandidate = pickPreferredResolvedOrigin([
    audio.tunnel_url,
    audioTunnel.tunnel_url,
    audio.nkn_address,
    audioFallbackNkn.nkn_address,
    audioFallbackNkn.address,
    audioTunnel.base_url,
    audioTunnel.list_url,
    audioTunnel.health_url,
    audioTunnel.webrtc_offer_url,
    audio.base_url,
    audio.list_url,
    audio.health_url,
    audio.webrtc_offer_url,
    audio.local_base_url,
    audioLocal.base_url,
    audioLocal.list_url,
    audioLocal.health_url,
    audioLocal.webrtc_offer_url
  ], audioRouterBaseUrl, "audio");
  const audioTransportHint = String(
    pickFirstNonEmptyString(audio.transport, audioFallback.selected_transport)
  ).trim().toLowerCase();
  const audioNknAddress = normalizeNknAddress(
    pickFirstNonEmptyString(
      audio.nkn_address,
      audioFallbackNkn.nkn_address,
      audioFallbackNkn.address
    )
  );
  const effectiveAudioCandidate = audioCandidate || (
    (audioTransportHint === SERVICE_TRANSPORT_NKN && audioNknAddress)
      ? nknEndpointForAddress(audioNknAddress)
      : ""
  );
  if (effectiveAudioCandidate) {
    const parsedAudioEndpoint = parseServiceEndpoint(effectiveAudioCandidate);
    const nextAudioBase = parsedAudioEndpoint.value;
    const nextAudioTransport = parsedAudioEndpoint.transport || SERVICE_TRANSPORT_HTTP;
    const nextAudioNknAddress = parsedAudioEndpoint.nknAddress || "";
    const previousBase = audioRouterBaseUrl;
    audioRouterBaseUrl = nextAudioBase;
    setServiceTransportMode("audio", nextAudioTransport);
    setServiceNknAddress("audio", nextAudioNknAddress);
    resolvedAudioEndpoint = audioRouterBaseUrl;
    const audioChanged =
      previousBase !== audioRouterBaseUrl ||
      previousAudioTransport !== nextAudioTransport ||
      previousAudioNknAddress !== nextAudioNknAddress;
    localStorage.setItem("audioRouterBaseUrl", audioRouterBaseUrl);
    const audioInput = document.getElementById("audioRouterBaseInput");
    if (audioInput) {
      audioInput.value = audioRouterBaseUrl;
    }
    if (audioChanged) {
      audioRouterSessionKey = "";
      localStorage.removeItem("audioRouterSessionKey");
      stopAudioBridge({ keepDesired: true, silent: true }).catch(() => {});
      setAudioConnectionMeta("Audio endpoint changed. Re-authenticating...");
    }
    changed = changed || audioChanged;
  } else if (Object.keys(audio).length > 0 || Object.keys(audioTunnel).length > 0 || Object.keys(audioLocal).length > 0) {
    const audioHadValue = !!audioRouterBaseUrl;
    const audioHadTransport = previousAudioTransport === SERVICE_TRANSPORT_NKN || !!previousAudioNknAddress;
    setServiceTransportMode("audio", SERVICE_TRANSPORT_HTTP);
    setServiceNknAddress("audio", "");
    audioRouterBaseUrl = "";
    audioRouterSessionKey = "";
    localStorage.removeItem("audioRouterBaseUrl");
    localStorage.removeItem("audioRouterSessionKey");
    const audioInput = document.getElementById("audioRouterBaseInput");
    if (audioInput) {
      audioInput.value = "";
    }
    stopAudioBridge({ keepDesired: false, silent: true }).catch(() => {});
    changed = changed || audioHadValue || audioHadTransport;
  }

  syncAdapterConnectionInputs({ preserveUserInput: false });
  queueResolvedEndpointAuthentications({
    adapterEndpoint: resolvedAdapterEndpoint,
    cameraEndpoint: resolvedCameraEndpoint,
    audioEndpoint: resolvedAudioEndpoint,
  });
  return changed;
}

function isNknTransportNotReadyError(err) {
  const message = String((err && err.message) || err || "").toLowerCase();
  return (
    message.includes("failed to send with any client") ||
    message.includes("rtcdatachannel.readystate is not 'open'") ||
    message.includes("readystate is not 'open'") ||
    message.includes("invalidstateerror")
  );
}

async function sendResolvePayloadWithRetry(targetAddress, payload, timeoutMs) {
  const deadline = Date.now() + Math.max(1000, Number(timeoutMs) || ROUTER_NKN_RESOLVE_TIMEOUT_MS);
  let attempt = 0;
  let lastError = null;
  const subclientOrder = getNknSubclientRetryOrder(browserNknSubclientCount);

  while (attempt < ROUTER_NKN_SEND_RETRY_MAX_ATTEMPTS && Date.now() < deadline) {
    attempt += 1;
    const shouldReconnect = attempt > 2;
    const fallbackIndex = shouldReconnect ? Math.min(attempt - 2, subclientOrder.length - 1) : 0;
    const selectedSubclients = subclientOrder[Math.max(0, fallbackIndex)] || browserNknSubclientCount;
    const client = await ensureBrowserNknClient({
      forceReconnect: shouldReconnect,
      subclients: selectedSubclients,
    });
    const waitBudget = Math.max(700, deadline - Date.now());
    const ready = await waitForBrowserNknReady(waitBudget);
    if (!ready) {
      lastError = new Error("Browser NKN client is not ready");
      if (attempt >= ROUTER_NKN_SEND_RETRY_MAX_ATTEMPTS) {
        break;
      }
      await sleepMs(Math.min(ROUTER_NKN_SEND_RETRY_DELAY_MS * attempt, Math.max(200, deadline - Date.now())));
      continue;
    }

    try {
      await client.send(String(targetAddress || "").trim(), JSON.stringify(payload), { noReply: true });
      return;
    } catch (err) {
      lastError = err;
      if (!isNknTransportNotReadyError(err)) {
        throw err;
      }

      if (attempt >= ROUTER_NKN_SEND_RETRY_MAX_ATTEMPTS) {
        break;
      }

      const backoffMs = ROUTER_NKN_SEND_RETRY_DELAY_MS * attempt;
      await sleepMs(Math.min(backoffMs, Math.max(200, deadline - Date.now())));
    }
  }

  throw lastError || new Error("Failed to send NKN resolve request");
}

async function requestResolvedEndpointsViaNkn(targetAddress, timeoutMs = ROUTER_NKN_RESOLVE_TIMEOUT_MS) {
  await ensureBrowserNknClient();
  const ready = await waitForBrowserNknReady(timeoutMs + 3000);
  if (!ready) {
    throw new Error("Browser NKN client is not ready");
  }

  const requestId = `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const payload = buildResolveRequestPayload(requestId);

  return new Promise(async (resolve, reject) => {
    const timeoutHandle = setTimeout(() => {
      pendingNknResolveRequests.delete(requestId);
      reject(new Error("Timed out waiting for NKN resolve reply"));
    }, Math.max(1000, Number(timeoutMs) || ROUTER_NKN_RESOLVE_TIMEOUT_MS));

    pendingNknResolveRequests.set(requestId, { resolve, reject, timeoutHandle });
    try {
      await sendResolvePayloadWithRetry(targetAddress, payload, timeoutMs);
    } catch (err) {
      clearTimeout(timeoutHandle);
      pendingNknResolveRequests.delete(requestId);
      reject(err);
    }
  });
}

async function requestCameraFrameViaNkn(cameraId, options = {}, timeoutMs = ROUTER_NKN_FRAME_TIMEOUT_MS) {
  const targetAddress = resolveServiceNknTarget("camera");
  if (!targetAddress) {
    throw new Error("Camera NKN target address is not configured");
  }
  await ensureBrowserNknClient();
  const ready = await waitForBrowserNknReady(timeoutMs + 2000);
  if (!ready) {
    throw new Error("Browser NKN client is not ready");
  }

  const requestId = `frm-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const payload = buildCameraFrameRequestPayload(requestId, cameraId, options);

  return new Promise(async (resolve, reject) => {
    const timeoutHandle = setTimeout(() => {
      pendingNknFrameRequests.delete(requestId);
      reject(new Error("Timed out waiting for NKN camera frame"));
    }, Math.max(1000, Number(timeoutMs) || ROUTER_NKN_FRAME_TIMEOUT_MS));

    pendingNknFrameRequests.set(requestId, { resolve, reject, timeoutHandle });
    try {
      await sendResolvePayloadWithRetry(targetAddress, payload, timeoutMs);
    } catch (err) {
      clearTimeout(timeoutHandle);
      pendingNknFrameRequests.delete(requestId);
      reject(err);
    }
  });
}

function normalizeNknRpcRequestOptions(options = {}) {
  const opts = (options && typeof options === "object") ? options : {};
  const method = String(opts.method || "GET").trim().toUpperCase() || "GET";
  const headers = {};
  if (opts.headers && typeof opts.headers === "object") {
    Object.entries(opts.headers).forEach(([key, value]) => {
      const header = String(key || "").trim();
      if (!header) {
        return;
      }
      headers[header] = String(value ?? "");
    });
  }

  const normalized = {
    method,
    headers,
    body_kind: "none",
    body: "",
  };

  const hasBody = Object.prototype.hasOwnProperty.call(opts, "body") && opts.body !== undefined && opts.body !== null;
  if (!hasBody) {
    return normalized;
  }

  const body = opts.body;
  if (typeof body === "string") {
    const contentType = String(headers["Content-Type"] || headers["content-type"] || "").toLowerCase();
    if (contentType.includes("application/json")) {
      try {
        normalized.body = JSON.parse(body);
        normalized.body_kind = "json";
        return normalized;
      } catch (err) {}
    }
    normalized.body = body;
    normalized.body_kind = "text";
    return normalized;
  }

  if (body instanceof Uint8Array) {
    let binary = "";
    for (let i = 0; i < body.length; i += 1) {
      binary += String.fromCharCode(body[i]);
    }
    normalized.body = btoa(binary);
    normalized.body_kind = "base64";
    return normalized;
  }

  if (body instanceof ArrayBuffer) {
    const bytes = new Uint8Array(body);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }
    normalized.body = btoa(binary);
    normalized.body_kind = "base64";
    return normalized;
  }

  if (typeof body === "object") {
    normalized.body = body;
    normalized.body_kind = "json";
    return normalized;
  }

  normalized.body = String(body);
  normalized.body_kind = "text";
  return normalized;
}

async function requestServiceRpcViaNkn(service, path, options = {}, timeoutMs = ROUTER_NKN_RPC_TIMEOUT_MS) {
  const serviceKey = String(service || "").trim().toLowerCase();
  if (serviceKey !== "adapter" && serviceKey !== "camera" && serviceKey !== "audio") {
    throw new Error(`Unsupported service '${service}'`);
  }
  const targetAddress = resolveServiceNknTarget(serviceKey);
  if (!targetAddress) {
    throw new Error(`${serviceKey} NKN target address is not configured`);
  }

  const effectiveTimeoutMs = Math.max(
    1000,
    Number((options && options.timeoutMs) || timeoutMs) || ROUTER_NKN_RPC_TIMEOUT_MS
  );

  await ensureBrowserNknClient();
  const ready = await waitForBrowserNknReady(effectiveTimeoutMs + 2000);
  if (!ready) {
    throw new Error("Browser NKN client is not ready");
  }

  const rpcOptions = normalizeNknRpcRequestOptions(options);
  const requestId = `rpc-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const payload = {
    event: "service_rpc_request",
    request_id: requestId,
    timestamp_ms: Date.now(),
    from: browserNknClientAddress || browserNknPubHex || "",
    service: serviceKey,
    path: normalizeServicePath(path),
    method: rpcOptions.method,
    headers: rpcOptions.headers,
    body_kind: rpcOptions.body_kind,
    body: rpcOptions.body,
    timeout_ms: Math.max(500, effectiveTimeoutMs),
  };

  return new Promise(async (resolve, reject) => {
    const timeoutHandle = setTimeout(() => {
      pendingNknServiceRpcRequests.delete(requestId);
      reject(new Error("Timed out waiting for NKN service RPC response"));
    }, effectiveTimeoutMs);

    pendingNknServiceRpcRequests.set(requestId, { resolve, reject, timeoutHandle });
    try {
      await sendResolvePayloadWithRetry(targetAddress, payload, effectiveTimeoutMs);
    } catch (err) {
      clearTimeout(timeoutHandle);
      pendingNknServiceRpcRequests.delete(requestId);
      reject(err);
    }
  });
}

function startRouterAutoResolveTimer() {
  if (routerAutoResolveTimer) {
    clearInterval(routerAutoResolveTimer);
  }
  routerAutoResolveTimer = setInterval(() => {
    resolveEndpointsViaNkn({ auto: true }).catch(() => {});
  }, ROUTER_NKN_AUTO_RESOLVE_INTERVAL_MS);
}

async function refreshNknClientInfo(options = {}) {
  const forceReconnect = !!options.forceReconnect;
  const resolveNow = !!options.resolveNow;
  const quiet = !!options.quiet;

  try {
    const subclientOrder = getNknSubclientRetryOrder(browserNknSubclientCount);
    const attempts = forceReconnect ? subclientOrder.length : 1;
    let ready = false;
    for (let idx = 0; idx < attempts; idx += 1) {
      const subclients = subclientOrder[idx] || browserNknSubclientCount;
      await ensureBrowserNknClient({
        forceReconnect: forceReconnect || idx > 0,
        subclients,
      });
      ready = await waitForBrowserNknReady(
        forceReconnect ? Math.min(7000, ROUTER_NKN_READY_TIMEOUT_MS) : ROUTER_NKN_READY_TIMEOUT_MS
      );
      if (ready) {
        break;
      }
      if (forceReconnect && idx < attempts - 1) {
        setRouterResolveStatus(
          `NKN not ready with ${subclients} subclient(s); retrying...`,
          true
        );
      }
    }

    if (!ready) {
      if (!quiet) {
        if (forceReconnect) {
          setRouterResolveStatus("Browser NKN connect failed after retrying subclient fallbacks", true);
        } else {
          setRouterResolveStatus("Browser NKN client starting...");
        }
      }
      if (forceReconnect) {
        setBrowserNknSeedStatus("Connect failed", true);
      }
      return false;
    }

    const statusBits = ["Browser NKN client ready"];
    statusBits.push(`subclients=${browserNknSubclientCount}`);
    if (browserNknClientAddress) {
      statusBits.push(browserNknClientAddress);
    }
    if (routerTargetNknAddress) {
      statusBits.push(`target ${routerTargetNknAddress}`);
    }
    if (!quiet) {
      setRouterResolveStatus(statusBits.join(" | "));
    }
    if (resolveNow && routerTargetNknAddress) {
      setTimeout(() => {
        resolveEndpointsViaNkn({ auto: true }).catch(() => {});
      }, 200);
    }
    return true;
  } catch (err) {
    setRouterResolveStatus(`Browser NKN init failed: ${err}`, true);
    setBrowserNknSeedStatus("NKN unavailable", true);
    return false;
  }
}

async function resolveEndpointsViaNkn(options = {}) {
  const auto = !!options.auto;
  const targetInput = document.getElementById("routerNknAddressInput");
  if (!targetInput) {
    return;
  }

  routerTargetNknAddress = (targetInput.value || "").trim();
  localStorage.setItem("routerTargetNknAddress", routerTargetNknAddress);

  if (!routerTargetNknAddress) {
    if (!auto) {
      setRouterResolveStatus("Enter Router NKN Address first", true);
    }
    return false;
  }
  if (!isLikelyNknAddress(routerTargetNknAddress)) {
    if (!auto) {
      setRouterResolveStatus("Router NKN address format looks invalid", true);
    }
    return false;
  }
  if (nknResolveInFlight) {
    if (!auto) {
      setRouterResolveStatus("NKN resolve already in progress");
    }
    return false;
  }

  nknResolveInFlight = true;
  try {
    await refreshNknClientInfo({ quiet: true });
    setRouterResolveStatus(
      auto
        ? `Auto-resolving endpoints via NKN (${routerTargetNknAddress})...`
        : `Resolving endpoints via NKN (${routerTargetNknAddress})...`
    );

    const result = await requestResolvedEndpointsViaNkn(routerTargetNknAddress, ROUTER_NKN_RESOLVE_TIMEOUT_MS);
    const resolved = result && result.resolved ? result.resolved : {};
    const applied = applyResolvedEndpoints(resolved || {});
    if (!applied) {
      setRouterResolveStatus("Resolved response received but no endpoints found", true);
      return false;
    }

    const source = (result && result.source) ? result.source : routerTargetNknAddress;
    setRouterResolveStatus(
      auto
        ? `Endpoints auto-updated from ${source} at ${new Date().toLocaleTimeString()}`
        : `Endpoints resolved from ${source} via NKN`
    );
    const cameraBaseInput = document.getElementById("cameraRouterBaseInput");
    if (cameraBaseInput && cameraRouterBaseUrl) {
      cameraBaseInput.value = cameraRouterBaseUrl;
    }
    const audioBaseInput = document.getElementById("audioRouterBaseInput");
    if (audioBaseInput && audioRouterBaseUrl) {
      audioBaseInput.value = audioRouterBaseUrl;
    }
    updateMetrics();
    return true;
  } catch (err) {
    setRouterResolveStatus(`NKN resolve failed: ${err}`, true);
    return false;
  } finally {
    nknResolveInFlight = false;
  }
}

function initNknRouterUi() {
  if (nknUiInitialized) {
    return;
  }
  nknUiInitialized = true;

  const targetInput = document.getElementById("routerNknAddressInput");
  const resolveBtn = document.getElementById("routerResolveBtn");
  const refreshBtn = document.getElementById("routerRefreshInfoBtn");
  const scanBtn = document.getElementById("routerScanQrBtn");
  const scanStopBtn = document.getElementById("routerQrScannerStopBtn");

  if (targetInput) {
    targetInput.value = routerTargetNknAddress;
    targetInput.addEventListener("change", () => {
      routerTargetNknAddress = (targetInput.value || "").trim();
      localStorage.setItem("routerTargetNknAddress", routerTargetNknAddress);
      if (routerTargetNknAddress) {
        resolveEndpointsViaNkn({ auto: true }).catch(() => {});
      }
    });
  }

  if (resolveBtn) {
    resolveBtn.addEventListener("click", () => {
      resolveEndpointsViaNkn({ auto: false }).catch(() => {});
    });
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      refreshNknClientInfo({ forceReconnect: true, resolveNow: true }).catch(() => {});
    });
  }
  if (scanBtn) {
    scanBtn.addEventListener("click", () => {
      startRouterQrScanner().catch((err) => {
        setRouterQrScannerUiState(false);
        setRouterQrScannerStatus(`Scanner start failed: ${err}`, true);
      });
    });
  }
  if (scanStopBtn) {
    scanStopBtn.addEventListener("click", () => {
      stopRouterQrScanner({ quiet: false });
    });
  }

  ensureBrowserNknIdentity();
  setRouterQrScannerUiState(false);
  setRouterQrScannerStatus("Scanner idle");
  window.addEventListener("beforeunload", () => {
    stopRouterQrScanner({ quiet: true });
  });
  startRouterAutoResolveTimer();
  refreshNknClientInfo({ resolveNow: true }).catch(() => {});
}

const CAMERA_ROUTER_DEFAULT_BASE = "";
try {
  localStorage.removeItem("cameraRouterBaseUrl");
} catch (err) {}
const STREAM_MODE_MJPEG = "mjpeg";
const STREAM_MODE_JPEG = "jpeg";
const STREAM_MODE_NKN = "nkn";
const PINNED_PREVIEW_STORAGE_KEY = "cameraPinnedPreviewStateV1";
const CAMERA_FEED_POLL_INTERVAL_MS = 1500;
const CAMERA_IMU_POLL_INTERVAL_MS = 200;
const CAMERA_IMU_STREAM_RETRY_MS = 2200;
const CAMERA_JPEG_POLL_INTERVAL_LOCAL_MS = 180;
const CAMERA_JPEG_POLL_INTERVAL_TUNNEL_MS = 550;
const CAMERA_JPEG_REQUEST_STALL_MS = 3500;
const CAMERA_JPEG_ERROR_RESTART_THRESHOLD_LOCAL = 4;
const CAMERA_JPEG_ERROR_RESTART_THRESHOLD_TUNNEL = 8;
const MIN_PINNED_PREVIEW_WIDTH = 180;
const MIN_PINNED_PREVIEW_HEIGHT = 105;
const DEFAULT_PINNED_PREVIEW_STATE = {
  visible: false,
  cameraId: "",
  left: 16,
  top: 80,
  width: 300,
  height: 168,
};
let cameraRouterBaseUrl = CAMERA_ROUTER_DEFAULT_BASE;
let cameraRouterPassword = localStorage.getItem("cameraRouterPassword") || "";
let cameraRouterSessionKey = localStorage.getItem("cameraRouterSessionKey") || "";
let cameraRouterFeeds = [];
let cameraRouterProtocols = {
  webrtc: false,
  mjpeg: true,
  jpeg_snapshot: true,
  mpegts: false,
};
let cameraRouterRoutes = {
  imu: "/imu",
  camera_recover: "/camera/recover",
};
const cameraPreview = {
  jpegTimer: null,
  peerConnection: null,
  activeCameraId: "",
  targetCameraId: localStorage.getItem("cameraRouterSelectedFeed") || "",
  activeMode: STREAM_MODE_MJPEG,
  jpegFallbackAttempted: false,
  desired: false,
  restartTimer: null,
  restartAttempts: 0,
  monitorTimer: null,
  healthFailStreak: 0,
  zeroClientStreak: 0,
  monitorInFlight: false,
};
const HYBRID_SELECTED_FEED_KEY = "hybridSelectedFeed";
const HYBRID_PREVIEW_DEFAULT_ASPECT = "16 / 9";
const HYBRID_COMMAND_INTERVAL_MS = 55;
const HYBRID_HOLD_INTERVAL_MS = 95;
const HYBRID_TOUCH_DEFAULTS = Object.freeze({
  speed: 0.6,
  accel: 0.4,
  xLimit: 500,
  yLimit: 800,
  zLimit: 800,
  rLimit: 300,
  pLimit: 300,
});
const hybridTouchTuneables = { ...HYBRID_TOUCH_DEFAULTS };

function hybridPoseLimits() {
  const rawXLimit = Number(hybridTouchTuneables.xLimit);
  const rawYLimit = Number(hybridTouchTuneables.yLimit);
  const rawZLimit = Number(hybridTouchTuneables.zLimit);
  const rawRLimit = Number(hybridTouchTuneables.rLimit);
  const rawPLimit = Number(hybridTouchTuneables.pLimit);
  const rawSpeed = Number(hybridTouchTuneables.speed);
  const rawAccel = Number(hybridTouchTuneables.accel);
  const xLimit = Math.max(10, Number.isFinite(rawXLimit) ? rawXLimit : HYBRID_TOUCH_DEFAULTS.xLimit);
  const yLimit = Math.max(10, Number.isFinite(rawYLimit) ? rawYLimit : HYBRID_TOUCH_DEFAULTS.yLimit);
  const zLimit = Math.max(10, Number.isFinite(rawZLimit) ? rawZLimit : HYBRID_TOUCH_DEFAULTS.zLimit);
  const rLimit = Math.max(10, Number.isFinite(rawRLimit) ? rawRLimit : HYBRID_TOUCH_DEFAULTS.rLimit);
  const pLimit = Math.max(10, Number.isFinite(rawPLimit) ? rawPLimit : HYBRID_TOUCH_DEFAULTS.pLimit);
  const speed = Math.max(0, Math.min(10, Number.isFinite(rawSpeed) ? rawSpeed : HYBRID_TOUCH_DEFAULTS.speed));
  const accel = Math.max(0, Math.min(10, Number.isFinite(rawAccel) ? rawAccel : HYBRID_TOUCH_DEFAULTS.accel));
  return {
    X: { min: -xLimit, max: xLimit },
    Y: { min: -yLimit, max: yLimit },
    Z: { min: -zLimit, max: zLimit },
    H: { min: 0, max: 70 },
    S: { min: speed, max: speed },
    A: { min: accel, max: accel },
    R: { min: -rLimit, max: rLimit },
    P: { min: -pLimit, max: pLimit },
  };
}

function hybridPoseDefaults() {
  return {
    X: 0,
    Y: 0,
    Z: 0,
    H: 0,
    S: Number(hybridTouchTuneables.speed),
    A: Number(hybridTouchTuneables.accel),
    R: 0,
    P: 0,
  };
}

const hybridPose = {
  X: 0,
  Y: 0,
  Z: 0,
  H: 0,
  S: Number(hybridTouchTuneables.speed),
  A: Number(hybridTouchTuneables.accel),
  R: 0,
  P: 0,
};
let hybridTouchTuneablesInitialized = false;
let hybridSelectedFeedId = localStorage.getItem(HYBRID_SELECTED_FEED_KEY) || "";
let hybridPreviewSourceKey = "";
let hybridLastCommand = "";
let hybridLastDispatchMs = 0;
const hybridHoldState = {
  timer: null,
  axis: "",
  delta: 0,
  button: null,
};
const hybridDragState = {
  active: false,
  pointerId: null,
  lastX: 0,
  lastY: 0,
  button: 0,
};
let cameraImuPollTimer = null;
let cameraImuPollInFlight = false;
let cameraImuSnapshot = null;
let cameraImuEventSource = null;
let cameraImuStreamUrl = "";
let cameraImuStreamActive = false;
let cameraImuStreamRetryTimer = null;
let cameraFeedPollTimer = null;
let cameraFeedRefreshInFlight = false;
let cameraRecoveryInFlight = false;
let cameraSessionRotateInFlight = false;
let cameraSelectInteractionUntilMs = 0;
const cameraProfileDraftByFeed = Object.create(null);
let cameraShareButtonResetTimer = null;
let streamUiInitialized = false;
let pinnedPreviewUiInitialized = false;
let pinnedPreviewState = {
  ...DEFAULT_PINNED_PREVIEW_STATE,
  ...loadPinnedPreviewStateFromStorage(),
};
const pinnedPreviewInteraction = {
  dragging: false,
  resizing: false,
  pointerId: null,
  startX: 0,
  startY: 0,
  startLeft: 0,
  startTop: 0,
  startWidth: 0,
  startHeight: 0,
};

const AUDIO_ROUTER_DEFAULT_BASE = "";
try {
  localStorage.removeItem("audioRouterBaseUrl");
} catch (err) {}
let audioRouterBaseUrl = AUDIO_ROUTER_DEFAULT_BASE;
let audioRouterPassword = localStorage.getItem("audioRouterPassword") || "";
let audioRouterSessionKey = localStorage.getItem("audioRouterSessionKey") || "";
let audioDevices = { inputs: [], outputs: [] };
let audioDeviceRefreshInFlight = false;
let audioSessionRotateInFlight = false;
let audioUiInitialized = false;
const audioBridge = {
  desired: false,
  starting: false,
  peerConnection: null,
  localStream: null,
  remoteStream: null,
  remoteTrack: null,
  lastError: "",
  active: false,
};
let audioPlaybackUnlockContext = null;
let audioPlaybackUnlockInFlight = null;
let audioPlaybackUnlocked = false;
let audioPlaybackGestureHooksInstalled = false;

function normalizeOrigin(rawInput) {
  const trimmed = (rawInput || "").trim();
  if (!trimmed) {
    return "";
  }
  const candidate = trimmed.includes("://") ? trimmed : `https://${trimmed}`;
  const parsed = new URL(candidate);
  return `${parsed.protocol}//${parsed.host}`;
}

function withCameraSession(path, includeSession = true) {
  if (!includeSession || !cameraRouterSessionKey) {
    return path;
  }
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}session_key=${encodeURIComponent(cameraRouterSessionKey)}`;
}

function cameraRouterUrl(path, includeSession = true) {
  if (!cameraRouterBaseUrl) {
    return "";
  }
  const pathWithSession = withCameraSession(path, includeSession);
  return `${cameraRouterBaseUrl}${pathWithSession}`;
}

function isTryCloudflareBase(url) {
  try {
    const parsed = new URL(url || "");
    return String(parsed.hostname || "").toLowerCase().endsWith(".trycloudflare.com");
  } catch (err) {
    return false;
  }
}

function cameraRouterFetchErrorMessage(err) {
  const detail = err && err.message ? err.message : String(err);
  if (isTryCloudflareBase(cameraRouterBaseUrl)) {
    return (
      "Camera router tunnel unreachable. This trycloudflare URL is likely expired or offline; " +
      "get a fresh URL from /tunnel_info and authenticate again."
    );
  }
  return detail;
}

function setStreamStatus(message, error = false) {
  const statusEl = document.getElementById("streamConfigStatus");
  if (!statusEl) {
    return;
  }
  statusEl.textContent = message;
  statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function cameraRouterImuPath() {
  const raw = String((cameraRouterRoutes && cameraRouterRoutes.imu) || "/imu").trim();
  if (!raw) {
    return "/imu";
  }
  return raw.startsWith("/") ? raw : `/${raw}`;
}

function cameraRouterImuStreamPath() {
  const raw = String((cameraRouterRoutes && cameraRouterRoutes.imu_stream) || "/imu/stream").trim();
  if (!raw) {
    return "/imu/stream";
  }
  return raw.startsWith("/") ? raw : `/${raw}`;
}

function cameraRouterRecoverPath() {
  const raw = String(
    (cameraRouterRoutes && (
      cameraRouterRoutes.camera_recover ||
      cameraRouterRoutes.camera_cycle ||
      cameraRouterRoutes.recover
    )) || "/camera/recover"
  ).trim();
  if (!raw) {
    return "/camera/recover";
  }
  return raw.startsWith("/") ? raw : `/${raw}`;
}

function parseImuVector(raw) {
  if (Array.isArray(raw) && raw.length >= 3) {
    const x = Number(raw[0]);
    const y = Number(raw[1]);
    const z = Number(raw[2]);
    const ts = Number(raw[3]);
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) {
      return null;
    }
    return {
      x,
      y,
      z,
      timestampMs: Number.isFinite(ts) ? ts : null,
    };
  }
  if (raw && typeof raw === "object") {
    const x = Number(raw.x);
    const y = Number(raw.y);
    const z = Number(raw.z);
    const ts = Number(raw.timestamp_ms || raw.t_ms || raw.ts || raw.timestamp);
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) {
      return null;
    }
    return {
      x,
      y,
      z,
      timestampMs: Number.isFinite(ts) ? ts : null,
    };
  }
  return null;
}

function formatImuVector(vector) {
  if (!vector) {
    return "--";
  }
  const base = `${vector.x.toFixed(3)}, ${vector.y.toFixed(3)}, ${vector.z.toFixed(3)}`;
  if (Number.isFinite(vector.timestampMs)) {
    return `${base} | t=${vector.timestampMs.toFixed(1)}ms`;
  }
  return base;
}

function applyCameraImuPayload(rawPayload, sourceLabel = "poll") {
  const payload = (rawPayload && typeof rawPayload === "object") ? rawPayload : {};
  const accel = parseImuVector(payload.accel);
  const gyro = parseImuVector(payload.gyro);
  cameraImuSnapshot = {
    accel,
    gyro,
    fetchedAtMs: Date.now(),
    source: String(sourceLabel || "poll"),
  };

  const anyVector = !!(accel || gyro);
  updateCameraImuReadouts(cameraImuSnapshot, {
    message: anyVector
      ? `IMU ${cameraImuSnapshot.source} live (${new Date(cameraImuSnapshot.fetchedAtMs).toLocaleTimeString()})`
      : "IMU route reachable; waiting for motion samples",
    error: false,
  });
  return anyVector;
}

function updateCameraImuReadouts(payload = null, options = {}) {
  const accel = payload && payload.accel ? payload.accel : null;
  const gyro = payload && payload.gyro ? payload.gyro : null;
  const message = String(options.message || "").trim();
  const error = !!options.error;
  const fallbackMessage = error ? "IMU unavailable" : "IMU idle";
  const metaText = message || fallbackMessage;
  const metaColor = error ? "#ff4444" : "var(--accent)";

  ["streamImuAccel", "hybridImuAccel"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = formatImuVector(accel);
    }
  });
  ["streamImuGyro", "hybridImuGyro"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = formatImuVector(gyro);
    }
  });
  ["streamImuMeta", "hybridImuMeta"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.textContent = metaText;
      el.style.color = metaColor;
    }
  });

  window.dispatchEvent(new CustomEvent("hybrid-touch-imu", {
    detail: {
      accel: accel ? { ...accel } : null,
      gyro: gyro ? { ...gyro } : null,
      fetchedAtMs: payload && Number.isFinite(payload.fetchedAtMs) ? payload.fetchedAtMs : Date.now(),
      source: payload && payload.source ? String(payload.source) : "unknown",
      message: metaText,
      error,
      mode: activeHybridTab,
      timestampMs: Date.now(),
    },
  }));
}

async function refreshCameraImu(options = {}) {
  const silent = !!options.silent;
  const force = !!options.force;
  if (cameraImuPollInFlight) {
    return !!cameraImuSnapshot;
  }
  if (!force && cameraImuStreamActive) {
    return !!cameraImuSnapshot;
  }
  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    cameraImuSnapshot = null;
    updateCameraImuReadouts(null, {
      message: "Authenticate camera router to read /imu",
      error: false,
    });
    return false;
  }

  cameraImuPollInFlight = true;
  try {
    const response = await cameraRouterFetch(cameraRouterImuPath(), { cache: "no-store" }, true);
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = {};
    }

    if (!response.ok) {
      if (response.status === 401) {
        cameraRouterSessionKey = "";
        localStorage.removeItem("cameraRouterSessionKey");
        stopCameraImuStream();
      }
      cameraImuSnapshot = null;
      updateCameraImuReadouts(null, {
        message: `IMU fetch failed: ${data.message || response.status}`,
        error: true,
      });
      return false;
    }

    return applyCameraImuPayload(data, "poll");
  } catch (err) {
    cameraImuSnapshot = null;
    updateCameraImuReadouts(null, {
      message: silent ? "IMU fetch error" : `IMU fetch error: ${err}`,
      error: true,
    });
    return false;
  } finally {
    cameraImuPollInFlight = false;
  }
}

function stopCameraImuStream() {
  if (cameraImuStreamRetryTimer) {
    clearTimeout(cameraImuStreamRetryTimer);
    cameraImuStreamRetryTimer = null;
  }
  cameraImuStreamActive = false;
  cameraImuStreamUrl = "";
  if (cameraImuEventSource) {
    try {
      cameraImuEventSource.close();
    } catch (err) {}
    cameraImuEventSource = null;
  }
}

function scheduleCameraImuStreamRetry() {
  if (cameraImuStreamRetryTimer) {
    return;
  }
  cameraImuStreamRetryTimer = setTimeout(() => {
    cameraImuStreamRetryTimer = null;
    startCameraImuStream();
  }, CAMERA_IMU_STREAM_RETRY_MS);
}

function startCameraImuStream() {
  if (typeof EventSource === "undefined") {
    return false;
  }
  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    stopCameraImuStream();
    return false;
  }
  const parsedEndpoint = parseServiceEndpoint(cameraRouterBaseUrl);
  if (
    parsedEndpoint.transport === SERVICE_TRANSPORT_NKN ||
    getServiceTransportMode("camera") === SERVICE_TRANSPORT_NKN
  ) {
    stopCameraImuStream();
    return false;
  }
  const streamUrl = cameraRouterUrl(cameraRouterImuStreamPath(), true);
  if (!streamUrl) {
    stopCameraImuStream();
    return false;
  }
  if (cameraImuEventSource && cameraImuStreamUrl === streamUrl) {
    return true;
  }
  stopCameraImuStream();
  cameraImuStreamUrl = streamUrl;

  try {
    const eventSource = new EventSource(streamUrl);
    cameraImuEventSource = eventSource;
    cameraImuStreamActive = false;

    const consume = (event) => {
      let data = {};
      try {
        data = JSON.parse(event.data || "{}");
      } catch (err) {
        return;
      }
      cameraImuStreamActive = true;
      applyCameraImuPayload(data, "stream");
    };

    eventSource.addEventListener("open", () => {
      cameraImuStreamActive = true;
    });
    eventSource.addEventListener("imu", consume);
    eventSource.onmessage = consume;
    eventSource.onerror = () => {
      cameraImuStreamActive = false;
      if (cameraImuEventSource === eventSource) {
        try {
          eventSource.close();
        } catch (err) {}
        cameraImuEventSource = null;
      }
      scheduleCameraImuStreamRetry();
    };
    return true;
  } catch (err) {
    cameraImuStreamActive = false;
    cameraImuEventSource = null;
    scheduleCameraImuStreamRetry();
    return false;
  }
}

function startCameraImuPolling() {
  if (cameraImuPollTimer) {
    return;
  }
  startCameraImuStream();
  refreshCameraImu({ silent: true }).catch(() => {});
  cameraImuPollTimer = setInterval(() => {
    if (cameraImuStreamActive) {
      return;
    }
    refreshCameraImu({ silent: true }).catch(() => {});
  }, CAMERA_IMU_POLL_INTERVAL_MS);
}

function markCameraSelectInteraction(durationMs = 1800) {
  const safeDuration = Number(durationMs) || 0;
  const deadline = Date.now() + Math.max(0, safeDuration);
  if (deadline > cameraSelectInteractionUntilMs) {
    cameraSelectInteractionUntilMs = deadline;
  }
}

function isCameraSelectInteractionActive() {
  const activeEl = document.activeElement;
  if (activeEl && (activeEl.id === "cameraFeedSelect" || activeEl.id === "cameraProfileSelect")) {
    return true;
  }
  return Date.now() < cameraSelectInteractionUntilMs;
}

function resetPreviewShareButton() {
  const shareBtn = document.getElementById("cameraPreviewShareBtn");
  if (!shareBtn) {
    return;
  }
  shareBtn.textContent = "Share";
  shareBtn.disabled = false;
}

function showPreviewShareCopiedState() {
  const shareBtn = document.getElementById("cameraPreviewShareBtn");
  if (!shareBtn) {
    return;
  }
  if (cameraShareButtonResetTimer) {
    clearTimeout(cameraShareButtonResetTimer);
    cameraShareButtonResetTimer = null;
  }
  shareBtn.textContent = "Copied";
  shareBtn.disabled = true;
  cameraShareButtonResetTimer = setTimeout(() => {
    cameraShareButtonResetTimer = null;
    resetPreviewShareButton();
  }, 1400);
}

async function copyTextToClipboard(text) {
  const payload = String(text || "").trim();
  if (!payload) {
    return false;
  }
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(payload);
      return true;
    } catch (err) {}
  }

  const node = document.createElement("textarea");
  node.value = payload;
  node.setAttribute("readonly", "");
  node.style.position = "fixed";
  node.style.opacity = "0";
  node.style.left = "-9999px";
  node.style.top = "0";
  document.body.appendChild(node);
  node.select();
  let success = false;
  try {
    success = document.execCommand("copy");
  } catch (err) {
    success = false;
  } finally {
    document.body.removeChild(node);
  }
  return !!success;
}

function loadPinnedPreviewStateFromStorage() {
  try {
    const rawState = localStorage.getItem(PINNED_PREVIEW_STORAGE_KEY);
    if (!rawState) {
      return {};
    }
    const parsed = JSON.parse(rawState);
    if (!parsed || typeof parsed !== "object") {
      return {};
    }

    const restored = {};
    if (typeof parsed.visible === "boolean") {
      restored.visible = parsed.visible;
    }
    if (typeof parsed.cameraId === "string") {
      restored.cameraId = parsed.cameraId;
    }

    ["left", "top", "width", "height"].forEach((key) => {
      const value = Number(parsed[key]);
      if (Number.isFinite(value)) {
        restored[key] = value;
      }
    });
    return restored;
  } catch (err) {
    return {};
  }
}

function persistPinnedPreviewState() {
  try {
    localStorage.setItem(PINNED_PREVIEW_STORAGE_KEY, JSON.stringify(pinnedPreviewState));
  } catch (err) {}
}

function getPinnedPreviewElements() {
  return {
    pane: document.getElementById("pinnedStreamPane"),
    image: document.getElementById("pinnedStreamImage"),
    closeBtn: document.getElementById("pinnedStreamCloseBtn"),
    resizeHandle: document.getElementById("pinnedStreamResizeHandle"),
    pinBtn: document.getElementById("cameraPreviewPinBtn"),
  };
}

function clampPinnedPreviewState() {
  const viewportWidth = Math.max(window.innerWidth || 0, MIN_PINNED_PREVIEW_WIDTH + 16);
  const viewportHeight = Math.max(window.innerHeight || 0, MIN_PINNED_PREVIEW_HEIGHT + 16);

  const maxWidth = Math.max(MIN_PINNED_PREVIEW_WIDTH, viewportWidth - 16);
  const maxHeight = Math.max(MIN_PINNED_PREVIEW_HEIGHT, viewportHeight - 16);
  pinnedPreviewState.width = Math.min(Math.max(pinnedPreviewState.width, MIN_PINNED_PREVIEW_WIDTH), maxWidth);
  pinnedPreviewState.height = Math.min(Math.max(pinnedPreviewState.height, MIN_PINNED_PREVIEW_HEIGHT), maxHeight);

  const maxLeft = Math.max(8, viewportWidth - pinnedPreviewState.width - 8);
  const maxTop = Math.max(8, viewportHeight - pinnedPreviewState.height - 8);
  pinnedPreviewState.left = Math.min(Math.max(pinnedPreviewState.left, 8), maxLeft);
  pinnedPreviewState.top = Math.min(Math.max(pinnedPreviewState.top, 8), maxTop);
}

function applyPinnedPreviewLayout(shouldPersist = false) {
  const { pane } = getPinnedPreviewElements();
  if (!pane) {
    return;
  }

  clampPinnedPreviewState();
  pane.style.left = `${Math.round(pinnedPreviewState.left)}px`;
  pane.style.top = `${Math.round(pinnedPreviewState.top)}px`;
  pane.style.width = `${Math.round(pinnedPreviewState.width)}px`;
  pane.style.height = `${Math.round(pinnedPreviewState.height)}px`;
  pane.classList.toggle("active", !!pinnedPreviewState.visible);

  if (shouldPersist) {
    persistPinnedPreviewState();
  }
}

function buildMjpegPreviewUrl(cameraId) {
  if (!cameraId) {
    return "";
  }
  return cameraRouterUrl(`/mjpeg/${encodeURIComponent(cameraId)}`, true);
}

function getCurrentPreviewShareUrl() {
  const imageEl = document.getElementById("cameraPreviewImage");
  if (imageEl && imageEl.style.display !== "none") {
    const imageSrc = String(imageEl.currentSrc || imageEl.src || "").trim();
    if (imageSrc) {
      return imageSrc;
    }
  }

  const videoEl = document.getElementById("cameraPreviewVideo");
  if (videoEl && videoEl.style.display !== "none") {
    const videoSrc = String(videoEl.currentSrc || videoEl.src || "").trim();
    if (videoSrc) {
      return videoSrc;
    }
  }

  const feedSelect = document.getElementById("cameraFeedSelect");
  const cameraId =
    cameraPreview.activeCameraId ||
    cameraPreview.targetCameraId ||
    (feedSelect ? feedSelect.value : "");
  if (!cameraId) {
    return "";
  }

  if (cameraPreview.activeMode === STREAM_MODE_NKN) {
    return "";
  }
  if (cameraPreview.activeMode === STREAM_MODE_JPEG) {
    return cameraRouterUrl(`/jpeg/${encodeURIComponent(cameraId)}`, true);
  }
  return cameraRouterUrl(`/mjpeg/${encodeURIComponent(cameraId)}`, true);
}

async function shareCurrentPreviewLink() {
  if (cameraPreview.activeMode === STREAM_MODE_NKN) {
    setStreamStatus("NKN relay preview does not expose a direct share URL", true);
    return;
  }
  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    setStreamStatus("Authenticate with camera router before sharing stream links", true);
    return;
  }
  const streamUrl = getCurrentPreviewShareUrl();
  if (!streamUrl) {
    setStreamStatus("Start a preview first to share its stream URL", true);
    return;
  }

  const copied = await copyTextToClipboard(streamUrl);
  if (!copied) {
    setStreamStatus("Unable to copy stream URL to clipboard", true);
    return;
  }
  setStreamStatus("Stream URL copied to clipboard");
  showPreviewShareCopiedState();
}

function syncPinnedPreviewSource(options = {}) {
  const forceRefresh = !!options.forceRefresh;
  const { image } = getPinnedPreviewElements();
  if (!image) {
    return;
  }
  if (!pinnedPreviewState.visible || !pinnedPreviewState.cameraId || !cameraRouterBaseUrl || !cameraRouterSessionKey) {
    image.src = "";
    image.dataset.sourceKey = "";
    return;
  }

  const sourceUrl = buildMjpegPreviewUrl(pinnedPreviewState.cameraId);
  const sourceKey = `${cameraRouterBaseUrl}|${cameraRouterSessionKey}|${pinnedPreviewState.cameraId}`;
  if (forceRefresh || image.dataset.sourceKey !== sourceKey) {
    if (forceRefresh) {
      image.src = "";
    }
    image.src = sourceUrl;
    image.dataset.sourceKey = sourceKey;
  }
}

function setPinButtonState() {
  const { pinBtn } = getPinnedPreviewElements();
  if (!pinBtn) {
    return;
  }
  pinBtn.disabled = !cameraPreview.activeCameraId;
}

function closePinnedPreview() {
  const { image } = getPinnedPreviewElements();
  pinnedPreviewState.visible = false;
  if (image) {
    image.src = "";
    image.dataset.sourceKey = "";
  }
  applyPinnedPreviewLayout(true);
}

function pinCurrentPreview() {
  const feedSelect = document.getElementById("cameraFeedSelect");
  const cameraId = cameraPreview.activeCameraId || (feedSelect ? feedSelect.value : "");
  if (!cameraId) {
    setStreamStatus("Start a preview first, then pin it", true);
    return;
  }
  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    setStreamStatus("Authenticate with camera router before pinning", true);
    return;
  }

  pinnedPreviewState.visible = true;
  pinnedPreviewState.cameraId = cameraId;
  applyPinnedPreviewLayout(true);
  syncPinnedPreviewSource();
  setStreamStatus(`Pinned preview active for ${cameraId}`);
}

function onPinnedPanePointerDown(event) {
  if (event.button !== 0) {
    return;
  }
  if (event.target.closest("#pinnedStreamCloseBtn") || event.target.closest("#pinnedStreamResizeHandle")) {
    return;
  }
  if (!pinnedPreviewState.visible) {
    return;
  }

  pinnedPreviewInteraction.dragging = true;
  pinnedPreviewInteraction.resizing = false;
  pinnedPreviewInteraction.pointerId = event.pointerId;
  pinnedPreviewInteraction.startX = event.clientX;
  pinnedPreviewInteraction.startY = event.clientY;
  pinnedPreviewInteraction.startLeft = pinnedPreviewState.left;
  pinnedPreviewInteraction.startTop = pinnedPreviewState.top;
  event.preventDefault();
}

function onPinnedResizePointerDown(event) {
  if (event.button !== 0) {
    return;
  }
  event.stopPropagation();
  if (!pinnedPreviewState.visible) {
    return;
  }

  pinnedPreviewInteraction.dragging = false;
  pinnedPreviewInteraction.resizing = true;
  pinnedPreviewInteraction.pointerId = event.pointerId;
  pinnedPreviewInteraction.startX = event.clientX;
  pinnedPreviewInteraction.startY = event.clientY;
  pinnedPreviewInteraction.startWidth = pinnedPreviewState.width;
  pinnedPreviewInteraction.startHeight = pinnedPreviewState.height;
  event.preventDefault();
}

function onPinnedPointerMove(event) {
  if (!pinnedPreviewInteraction.dragging && !pinnedPreviewInteraction.resizing) {
    return;
  }
  if (pinnedPreviewInteraction.pointerId !== null && event.pointerId !== pinnedPreviewInteraction.pointerId) {
    return;
  }

  const deltaX = event.clientX - pinnedPreviewInteraction.startX;
  const deltaY = event.clientY - pinnedPreviewInteraction.startY;

  if (pinnedPreviewInteraction.resizing) {
    pinnedPreviewState.width = pinnedPreviewInteraction.startWidth + deltaX;
    pinnedPreviewState.height = pinnedPreviewInteraction.startHeight + deltaY;
  } else {
    pinnedPreviewState.left = pinnedPreviewInteraction.startLeft + deltaX;
    pinnedPreviewState.top = pinnedPreviewInteraction.startTop + deltaY;
  }
  applyPinnedPreviewLayout(false);
  event.preventDefault();
}

function clearPinnedPreviewInteraction() {
  pinnedPreviewInteraction.dragging = false;
  pinnedPreviewInteraction.resizing = false;
  pinnedPreviewInteraction.pointerId = null;
}

function onPinnedPointerUp(event) {
  if (!pinnedPreviewInteraction.dragging && !pinnedPreviewInteraction.resizing) {
    return;
  }
  if (pinnedPreviewInteraction.pointerId !== null && event.pointerId !== pinnedPreviewInteraction.pointerId) {
    return;
  }

  clearPinnedPreviewInteraction();
  applyPinnedPreviewLayout(true);
}

function initializePinnedPreviewUi() {
  if (pinnedPreviewUiInitialized) {
    return;
  }
  pinnedPreviewUiInitialized = true;

  const { pane, closeBtn, resizeHandle, pinBtn } = getPinnedPreviewElements();
  if (!pane) {
    return;
  }

  if (pinBtn) {
    pinBtn.addEventListener("click", pinCurrentPreview);
  }
  if (closeBtn) {
    closeBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      closePinnedPreview();
    });
  }
  if (resizeHandle) {
    resizeHandle.addEventListener("pointerdown", onPinnedResizePointerDown);
  }

  pane.addEventListener("pointerdown", onPinnedPanePointerDown);
  window.addEventListener("pointermove", onPinnedPointerMove);
  window.addEventListener("pointerup", onPinnedPointerUp);
  window.addEventListener("pointercancel", onPinnedPointerUp);
  window.addEventListener("resize", () => {
    applyPinnedPreviewLayout(true);
  });

  applyPinnedPreviewLayout(false);
  syncPinnedPreviewSource();
  setPinButtonState();
}

function clearCameraPreviewRestartTimer() {
  if (cameraPreview.restartTimer) {
    clearTimeout(cameraPreview.restartTimer);
    cameraPreview.restartTimer = null;
  }
}

function stopCameraPreviewMonitor() {
  if (cameraPreview.monitorTimer) {
    clearInterval(cameraPreview.monitorTimer);
    cameraPreview.monitorTimer = null;
  }
}

function stopCameraFeedPolling() {
  if (cameraFeedPollTimer) {
    clearInterval(cameraFeedPollTimer);
    cameraFeedPollTimer = null;
  }
}

function startCameraFeedPolling() {
  if (cameraFeedPollTimer) {
    return;
  }
  cameraFeedPollTimer = setInterval(() => {
    refreshCameraFeeds({ silent: true, suppressErrors: true }).catch(() => {});
  }, CAMERA_FEED_POLL_INTERVAL_MS);
}

async function monitorCameraPreviewHealth() {
  if (!cameraPreview.desired) {
    return;
  }
  if (cameraPreview.activeMode === STREAM_MODE_NKN) {
    if (!resolveServiceNknTarget("camera")) {
      cameraPreview.healthFailStreak += 1;
      if (cameraPreview.healthFailStreak >= 2) {
        scheduleCameraPreviewRestart("router nkn address missing");
      }
    }
    return;
  }
  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    return;
  }
  if (cameraPreview.monitorInFlight) {
    return;
  }
  cameraPreview.monitorInFlight = true;

  try {
    const ok = await refreshCameraFeeds({ silent: true, suppressErrors: true });
    if (!ok) {
      cameraPreview.healthFailStreak += 1;
      if (cameraPreview.healthFailStreak >= 2) {
        scheduleCameraPreviewRestart("router unreachable");
      }
      return;
    }

    cameraPreview.healthFailStreak = 0;
    const watchFeedId =
      cameraPreview.targetCameraId ||
      cameraPreview.activeCameraId ||
      localStorage.getItem("cameraRouterSelectedFeed") ||
      "";
    const selected = cameraRouterFeeds.find((feed) => feed.id === watchFeedId) || null;
    if (!selected || !selected.online) {
      cameraPreview.zeroClientStreak += 1;
      if (cameraPreview.zeroClientStreak >= 2) {
        scheduleCameraPreviewRestart("feed offline");
      }
      return;
    }

    const clients = Number(selected.clients) || 0;
    const fps = Number(selected.fps) || 0;
    const requiresPersistentClient =
      cameraPreview.activeMode !== STREAM_MODE_JPEG &&
      cameraPreview.activeMode !== STREAM_MODE_NKN;
    if ((requiresPersistentClient && clients <= 0) || fps <= 0.2) {
      cameraPreview.zeroClientStreak += 1;
      if (cameraPreview.zeroClientStreak >= 3) {
        scheduleCameraPreviewRestart("stream stalled");
      }
    } else {
      cameraPreview.zeroClientStreak = 0;
    }
  } finally {
    cameraPreview.monitorInFlight = false;
  }
}

function startCameraPreviewMonitor() {
  if (cameraPreview.monitorTimer) {
    return;
  }
  cameraPreview.monitorTimer = setInterval(() => {
    monitorCameraPreviewHealth().catch(() => {});
  }, 3000);
}

function scheduleCameraPreviewRestart(reason = "stream error") {
  if (!cameraPreview.desired || cameraPreview.restartTimer) {
    return;
  }
  const restartFeedId =
    cameraPreview.targetCameraId ||
    cameraPreview.activeCameraId ||
    localStorage.getItem("cameraRouterSelectedFeed") ||
    "";
  cameraPreview.restartAttempts += 1;
  const delayMs = Math.min(10000, 700 * Math.pow(2, Math.max(0, cameraPreview.restartAttempts - 1)));
  const secs = (delayMs / 1000).toFixed(1);
  setStreamStatus(`Preview interrupted (${reason}). Reconnecting ${restartFeedId || "feed"} in ${secs}s...`, true);
  updateMetrics();
  cameraPreview.restartTimer = setTimeout(() => {
    cameraPreview.restartTimer = null;
    if (!cameraPreview.desired) {
      return;
    }
    startCameraPreview({ autoRestart: true, reason, cameraId: restartFeedId }).catch(() => {});
  }, delayMs);
}

function stopCameraPreview(options = {}) {
  const keepDesired = !!options.keepDesired;
  const feedSelect = document.getElementById("cameraFeedSelect");
  const selectedFeed = feedSelect ? (feedSelect.value || "") : "";
  if (selectedFeed) {
    cameraPreview.targetCameraId = selectedFeed;
  }
  if (cameraPreview.jpegTimer) {
    clearInterval(cameraPreview.jpegTimer);
    cameraPreview.jpegTimer = null;
  }
  if (cameraPreview.peerConnection) {
    try {
      cameraPreview.peerConnection.getReceivers().forEach((receiver) => {
        if (receiver.track) {
          receiver.track.stop();
        }
      });
      cameraPreview.peerConnection.close();
    } catch (err) {}
    cameraPreview.peerConnection = null;
  }

  const mjpegImg = document.getElementById("cameraPreviewImage");
  if (mjpegImg) {
    mjpegImg.onload = null;
    mjpegImg.onerror = null;
    mjpegImg.onabort = null;
    mjpegImg.src = "";
    mjpegImg.style.display = "none";
  }
  const videoEl = document.getElementById("cameraPreviewVideo");
  if (videoEl) {
    try {
      videoEl.pause();
      if (videoEl.srcObject) {
        const tracks = videoEl.srcObject.getTracks ? videoEl.srcObject.getTracks() : [];
        tracks.forEach((track) => track.stop());
      }
    } catch (err) {}
    videoEl.srcObject = null;
    videoEl.removeAttribute("src");
    videoEl.load();
    videoEl.style.display = "none";
  }

  cameraPreview.activeCameraId = "";
  cameraPreview.activeMode = STREAM_MODE_MJPEG;
  cameraPreview.jpegFallbackAttempted = false;
  cameraPreview.healthFailStreak = 0;
  cameraPreview.zeroClientStreak = 0;
  cameraPreview.monitorInFlight = false;
  if (!keepDesired) {
    cameraPreview.desired = false;
    cameraPreview.restartAttempts = 0;
    clearCameraPreviewRestartTimer();
    stopCameraPreviewMonitor();
  }
  setPinButtonState();
  updateMetrics();
}

function activatePreviewMode(mode) {
  const mjpegImg = document.getElementById("cameraPreviewImage");
  const videoEl = document.getElementById("cameraPreviewVideo");
  if (mjpegImg) {
    mjpegImg.style.display = mode === "mjpeg" || mode === "jpeg" || mode === "nkn" ? "block" : "none";
  }
  if (videoEl) {
    videoEl.style.display = mode === "webrtc" || mode === "mpegts" ? "block" : "none";
  }
}

async function cameraRouterFetch(path, options = {}, includeSession = true) {
  if (!cameraRouterBaseUrl) {
    throw new Error("Camera Router URL is not configured");
  }
  const parsedEndpoint = parseServiceEndpoint(cameraRouterBaseUrl);
  const useNkn =
    parsedEndpoint.transport === SERVICE_TRANSPORT_NKN ||
    getServiceTransportMode("camera") === SERVICE_TRANSPORT_NKN;
  if (useNkn) {
    const nknAddress = normalizeNknAddress(parsedEndpoint.nknAddress || getServiceNknAddress("camera"));
    setServiceTransportMode("camera", SERVICE_TRANSPORT_NKN);
    if (nknAddress) {
      setServiceNknAddress("camera", nknAddress);
    }
    const rpcPath = withCameraSession(normalizeServicePath(path), includeSession);
    return requestServiceRpcViaNkn("camera", rpcPath, options);
  }
  setServiceTransportMode("camera", SERVICE_TRANSPORT_HTTP);
  const url = cameraRouterUrl(path, includeSession);
  let response = null;
  try {
    response = await fetch(url, options);
  } catch (err) {
    throw new Error(cameraRouterFetchErrorMessage(err));
  }
  if (response.status === 530 && isTryCloudflareBase(cameraRouterBaseUrl)) {
    throw new Error(
      "Camera router tunnel is offline (Cloudflare 530). Refresh the tunnel URL from /tunnel_info and retry."
    );
  }
  return response;
}

async function cycleCameraAccessRecovery(options = {}) {
  if (cameraRecoveryInFlight) {
    setDebugCameraRecoveryStatus("Camera recovery already running");
    return false;
  }

  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    setDebugCameraRecoveryStatus("Authenticate camera router before cycling camera access", true);
    setStreamStatus("Authenticate camera router before cycling camera access", true);
    return false;
  }

  const forceRecover = !(options && options.forceRecover === false);
  const trigger = String((options && options.trigger) || "manual").trim() || "manual";
  const cycleBtn = document.getElementById("debugCameraCycleBtn");
  const modeSelect = document.getElementById("cameraModeSelect");
  const desiredCameraId =
    cameraPreview.targetCameraId ||
    cameraPreview.activeCameraId ||
    localStorage.getItem("cameraRouterSelectedFeed") ||
    "";
  const desiredMode = String((modeSelect && modeSelect.value) || localStorage.getItem("cameraRouterSelectedMode") || STREAM_MODE_MJPEG);
  const wasPreviewDesired = !!cameraPreview.desired;

  cameraRecoveryInFlight = true;
  if (cycleBtn) {
    cycleBtn.disabled = true;
  }

  setDebugCameraRecoveryStatus("Cycling camera access...");
  setStreamStatus("Cycling camera access (release + reacquire)...");

  stopCameraPreview({ keepDesired: false });
  stopCameraImuStream();

  try {
    const response = await cameraRouterFetch(
      cameraRouterRecoverPath(),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          force_recover: forceRecover,
          reason: `frontend-${trigger}`,
          settle_ms: 350,
        }),
      },
      true
    );

    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = {};
    }

    if (!response.ok || data.status !== "success") {
      if (response.status === 401) {
        cameraRouterSessionKey = "";
        localStorage.removeItem("cameraRouterSessionKey");
        setDebugCameraRecoveryStatus("Session expired; re-authenticate camera router", true);
        setStreamStatus("Session expired; re-authenticate camera router", true);
        return false;
      }
      throw new Error(data.message || `HTTP ${response.status}`);
    }

    await refreshCameraFeeds({ silent: true, suppressErrors: true });
    startCameraImuStream();
    refreshCameraImu({ silent: true, force: true }).catch(() => {});

    let previewRestarted = false;
    if (wasPreviewDesired && desiredCameraId) {
      if (
        modeSelect &&
        (desiredMode === STREAM_MODE_MJPEG || desiredMode === STREAM_MODE_JPEG || desiredMode === STREAM_MODE_NKN)
      ) {
        modeSelect.value = desiredMode;
      }
      await startCameraPreview({
        autoRestart: true,
        cameraId: desiredCameraId,
        reason: "camera recovery cycle",
      });
      previewRestarted = true;
    }

    const afterOnline = Number(data.after_online);
    const afterTotal = Number(data.after_total);
    const elapsedMs = Number(data.elapsed_ms);
    const summaryBits = [];
    if (Number.isFinite(afterOnline) && Number.isFinite(afterTotal)) {
      summaryBits.push(`${afterOnline}/${afterTotal} feeds online`);
    } else {
      summaryBits.push("camera cycle complete");
    }
    if (Number.isFinite(elapsedMs) && elapsedMs >= 0) {
      summaryBits.push(`${Math.round(elapsedMs)}ms`);
    }
    if (previewRestarted) {
      summaryBits.push("preview restarted");
    }
    const summary = `Camera recovery complete: ${summaryBits.join(" | ")}`;
    setDebugCameraRecoveryStatus(summary);
    setStreamStatus(summary);
    return true;
  } catch (err) {
    startCameraImuStream();
    refreshCameraImu({ silent: true, force: true }).catch(() => {});
    setDebugCameraRecoveryStatus(`Camera recovery failed: ${err}`, true);
    setStreamStatus(`Camera recovery failed: ${err}`, true);
    return false;
  } finally {
    cameraRecoveryInFlight = false;
    if (cycleBtn) {
      cycleBtn.disabled = false;
    }
    updateMetrics();
  }
}

async function authenticateCameraRouterWithPassword(password, options = {}) {
  const silent = !!options.silent;
  const baseCandidate = String(options.baseUrl || cameraRouterBaseUrl || "").trim();
  const cleanPassword = String(password || "").trim();
  const parsedEndpoint = parseServiceEndpoint(baseCandidate);
  const normalizedBase = String(parsedEndpoint.value || "").trim();

  if (!normalizedBase || !cleanPassword) {
    if (!silent) {
      setStreamStatus("Enter both camera router URL and password", true);
    }
    return false;
  }

  setServiceTransportMode("camera", parsedEndpoint.transport || SERVICE_TRANSPORT_HTTP);
  setServiceNknAddress("camera", parsedEndpoint.nknAddress || "");
  cameraRouterBaseUrl = normalizedBase;
  setServiceAuthPassword("camera", cleanPassword);
  localStorage.setItem("cameraRouterBaseUrl", cameraRouterBaseUrl);
  const baseInput = document.getElementById("cameraRouterBaseInput");
  if (baseInput) {
    baseInput.value = cameraRouterBaseUrl;
  }
  syncPinnedPreviewSource();
  renderHybridFeedOptions();

  if (!silent) {
    setStreamStatus("Authenticating with camera router...");
  }

  try {
    const response = await cameraRouterFetch(
      "/auth",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: cleanPassword }),
      },
      false
    );
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = {};
    }

    if (!response.ok || data.status !== "success") {
      if (!silent) {
        setStreamStatus(`Auth failed: ${data.message || response.status}`, true);
      }
      return false;
    }

    const nextSessionKey = String(data.session_key || "").trim();
    if (!nextSessionKey) {
      if (!silent) {
        setStreamStatus("Auth failed: response missing session key", true);
      }
      return false;
    }

    cameraRouterSessionKey = nextSessionKey;
    localStorage.setItem("cameraRouterSessionKey", cameraRouterSessionKey);
    if (!silent) {
      setStreamStatus(`Authenticated. Session timeout ${Number(data.timeout) || "--"}s`);
    }

    await refreshCameraFeeds({ silent: true, suppressErrors: true });
    startCameraImuStream();
    await refreshCameraImu({ silent: true, force: true });
    syncPinnedPreviewSource();
    renderHybridFeedOptions();
    if (cameraPreview.desired) {
      await startCameraPreview({ autoRestart: true, reason: "session refresh" });
    }
    return true;
  } catch (err) {
    stopCameraImuStream();
    if (!silent) {
      setStreamStatus(`Auth error: ${err}`, true);
      updateCameraImuReadouts(null, {
        message: "IMU unavailable until camera router auth succeeds",
        error: true,
      });
    }
    return false;
  }
}

async function authenticateCameraRouter() {
  const baseInput = document.getElementById("cameraRouterBaseInput");
  const passInput = document.getElementById("cameraRouterPasswordInput");
  if (!baseInput || !passInput) {
    return false;
  }
  return authenticateCameraRouterWithPassword(passInput.value, {
    baseUrl: baseInput.value,
    silent: false,
  });
}

async function rotateCameraRouterSessionKey() {
  if (cameraSessionRotateInFlight) {
    return;
  }
  if (!cameraRouterBaseUrl) {
    setStreamStatus("Set camera router URL first", true);
    return;
  }
  if (!cameraRouterSessionKey) {
    setStreamStatus("Authenticate with camera router before rotating session keys", true);
    return;
  }

  cameraSessionRotateInFlight = true;
  const rotateBtn = document.getElementById("cameraRouterRotateSessionBtn");
  if (rotateBtn) {
    rotateBtn.disabled = true;
  }
  setStreamStatus("Rotating camera session key...");

  try {
    const response = await cameraRouterFetch(
      "/session/rotate",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
      true
    );
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      if (response.status === 401) {
        cameraRouterSessionKey = "";
        localStorage.removeItem("cameraRouterSessionKey");
        stopCameraImuStream();
        syncPinnedPreviewSource();
        renderHybridFeedOptions();
        updateCameraImuReadouts(null, {
          message: "Camera session expired; IMU polling paused",
          error: true,
        });
        if (cameraPreview.desired) {
          stopCameraPreview();
        }
      }
      setStreamStatus(`Session rotate failed: ${data.message || response.status}`, true);
      return;
    }

    const nextSessionKey = String(data.session_key || "").trim();
    if (!nextSessionKey) {
      setStreamStatus("Session rotate failed: response missing session key", true);
      return;
    }

    cameraRouterSessionKey = nextSessionKey;
    localStorage.setItem("cameraRouterSessionKey", cameraRouterSessionKey);
    startCameraImuStream();
    syncPinnedPreviewSource({ forceRefresh: true });
    await refreshCameraFeeds({ silent: true, suppressErrors: true });
    await refreshCameraImu({ silent: true });
    if (cameraPreview.desired) {
      await startCameraPreview({ autoRestart: true, reason: "session rotated" });
    }
    setStreamStatus(`Session key rotated. Invalidated ${Number(data.invalidated_sessions) || 0} session(s).`);
  } catch (err) {
    setStreamStatus(`Session rotate error: ${err}`, true);
  } finally {
    cameraSessionRotateInFlight = false;
    if (rotateBtn) {
      rotateBtn.disabled = false;
    }
  }
}

function renderCameraFeedOptions(options = {}) {
  const preserveSelectors = !!options.preserveSelectors;
  const feedSelect = document.getElementById("cameraFeedSelect");
  const feedList = document.getElementById("cameraFeedList");
  const profileSelect = document.getElementById("cameraProfileSelect");
  if (!feedSelect || !feedList) {
    return;
  }

  const previousValue =
    feedSelect.value ||
    cameraPreview.targetCameraId ||
    localStorage.getItem("cameraRouterSelectedFeed") ||
    "";
  if (!preserveSelectors) {
    feedSelect.innerHTML = "";
    cameraRouterFeeds.forEach((feed) => {
      const opt = document.createElement("option");
      opt.value = feed.id;
      opt.textContent = `${feed.label} (${feed.online ? "online" : "offline"})`;
      feedSelect.appendChild(opt);
    });
  }

  if (cameraRouterFeeds.length > 0) {
    const hasPrevious = previousValue && cameraRouterFeeds.some((feed) => feed.id === previousValue);
    if (!preserveSelectors) {
      if (hasPrevious) {
        feedSelect.value = previousValue;
      } else if (!feedSelect.value) {
        feedSelect.value = cameraRouterFeeds[0].id;
      }
    } else if (!feedSelect.value && hasPrevious) {
      feedSelect.value = previousValue;
    } else if (!feedSelect.value) {
      feedSelect.value = cameraRouterFeeds[0].id;
    }
    localStorage.setItem("cameraRouterSelectedFeed", feedSelect.value);
    cameraPreview.targetCameraId = feedSelect.value || cameraPreview.targetCameraId;
  }

  feedList.innerHTML = "";
  cameraRouterFeeds.forEach((feed) => {
    const line = document.createElement("div");
    line.className = "stream-feed-row";
    line.textContent = `${feed.id} | fps ${feed.fps} | kbps ${feed.kbps} | clients ${feed.clients} | ${feed.online ? "online" : "offline"}`;
    feedList.appendChild(line);
  });

  if (profileSelect && !preserveSelectors) {
    renderCameraProfileOptions(feedSelect.value || "");
  }
  renderHybridFeedOptions();
}

function formatProfileOption(profile) {
  const pix = (profile.pixel_format || "AUTO").toUpperCase();
  const width = Number(profile.width) || 0;
  const height = Number(profile.height) || 0;
  const fps = Number(profile.fps) || 0;
  return `${pix} ${width}x${height} @ ${fps.toFixed(0)}fps`;
}

function profileMatches(a, b) {
  if (!a || !b) {
    return false;
  }
  const pixA = String(a.pixel_format || "").toUpperCase();
  const pixB = String(b.pixel_format || "").toUpperCase();
  const wA = Number(a.width) || 0;
  const wB = Number(b.width) || 0;
  const hA = Number(a.height) || 0;
  const hB = Number(b.height) || 0;
  const fpsA = Number(a.fps) || 0;
  const fpsB = Number(b.fps) || 0;
  return pixA === pixB && wA === wB && hA === hB && Math.abs(fpsA - fpsB) < 0.6;
}

function cameraProfileKey(profile) {
  if (!profile) {
    return "";
  }
  const pix = String(profile.pixel_format || "").toUpperCase();
  const width = Number(profile.width) || 0;
  const height = Number(profile.height) || 0;
  const fps = Number(profile.fps) || 0;
  return `${pix}|${width}|${height}|${fps.toFixed(3)}`;
}

function setCameraProfileDraft(cameraId, profile) {
  const key = String(cameraId || "").trim();
  const profileKey = cameraProfileKey(profile);
  if (!key || !profileKey) {
    return;
  }
  cameraProfileDraftByFeed[key] = profileKey;
}

function clearCameraProfileDraft(cameraId) {
  const key = String(cameraId || "").trim();
  if (!key) {
    return;
  }
  delete cameraProfileDraftByFeed[key];
}

function getCameraProfileDraft(cameraId) {
  const key = String(cameraId || "").trim();
  if (!key) {
    return "";
  }
  return String(cameraProfileDraftByFeed[key] || "");
}

function hasPendingCameraProfileSelection() {
  const feedSelect = document.getElementById("cameraFeedSelect");
  const cameraId = String((feedSelect && feedSelect.value) || "").trim();
  if (!cameraId) {
    return false;
  }
  return !!getCameraProfileDraft(cameraId);
}

function renderCameraProfileOptions(cameraId) {
  const profileSelect = document.getElementById("cameraProfileSelect");
  const profileStatus = document.getElementById("cameraProfileStatus");
  const profileApplyBtn = document.getElementById("cameraProfileApplyBtn");
  if (!profileSelect || !profileStatus) {
    return;
  }

  profileSelect.innerHTML = "";
  profileSelect.disabled = true;
  if (profileApplyBtn) {
    profileApplyBtn.disabled = true;
  }

  const feed = cameraRouterFeeds.find((item) => item.id === cameraId) || null;
  if (!feed) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Select a camera feed";
    profileSelect.appendChild(empty);
    profileStatus.textContent = "No camera profile selected";
    return;
  }

  const profiles = Array.isArray(feed.available_profiles) ? feed.available_profiles : [];
  const currentProfile = feed.capture_profile || null;
  const isMutable = String(feed.source_type || "") === "default";
  const draftKey = getCameraProfileDraft(cameraId);
  let draftMatched = false;
  let selectedIndex = 0;

  if (profiles.length > 0) {
    profiles.forEach((profile, idx) => {
      const opt = document.createElement("option");
      opt.value = String(idx);
      opt.textContent = formatProfileOption(profile);
      opt.dataset.profileKey = cameraProfileKey(profile);
      profileSelect.appendChild(opt);
      if (draftKey && opt.dataset.profileKey === draftKey) {
        selectedIndex = idx;
        draftMatched = true;
        return;
      }
      if (!draftKey && currentProfile && profileMatches(profile, currentProfile)) {
        selectedIndex = idx;
      }
    });
    if (draftKey && !draftMatched) {
      clearCameraProfileDraft(cameraId);
    }
    profileSelect.value = String(selectedIndex);
    profileSelect.disabled = !isMutable;
  } else {
    const fallback = document.createElement("option");
    fallback.value = "";
    fallback.textContent = currentProfile
      ? formatProfileOption(currentProfile)
      : "Using camera defaults";
    profileSelect.appendChild(fallback);
  }

  const active = feed.active_capture || {};
  const activeBackend = active.backend ? `backend ${active.backend}` : "backend pending";
  const activeRes =
    active.width && active.height
      ? `${active.width}x${active.height} @ ${(Number(active.fps) || 0).toFixed(0)}fps`
      : "awaiting frames";
  const profileError = feed.profile_query_error ? ` | ${feed.profile_query_error}` : "";
  profileStatus.textContent = `${feed.device_path || feed.id}: ${activeRes} (${activeBackend})${profileError}`;
  if (profileApplyBtn) {
    profileApplyBtn.disabled = !isMutable;
  }
}

async function applySelectedCameraProfile() {
  const feedSelect = document.getElementById("cameraFeedSelect");
  const profileSelect = document.getElementById("cameraProfileSelect");
  if (!feedSelect || !profileSelect) {
    return;
  }

  const cameraId = feedSelect.value || "";
  if (!cameraId) {
    setStreamStatus("Select a feed before applying a profile", true);
    return;
  }
  const feed = cameraRouterFeeds.find((item) => item.id === cameraId) || null;
  if (!feed) {
    setStreamStatus("Selected feed metadata is unavailable", true);
    return;
  }
  if (String(feed.source_type || "") !== "default") {
    setStreamStatus("Profile changes are only supported for default V4L2 feeds", true);
    return;
  }

  const profiles = Array.isArray(feed.available_profiles) ? feed.available_profiles : [];
  let requestedProfile = feed.capture_profile || null;
  const selectedIndex = parseInt(profileSelect.value, 10);
  if (profiles.length > 0 && Number.isInteger(selectedIndex) && selectedIndex >= 0 && selectedIndex < profiles.length) {
    requestedProfile = profiles[selectedIndex];
  }
  if (!requestedProfile) {
    setStreamStatus("No profile available to apply", true);
    return;
  }
  if (feed.capture_profile && profileMatches(requestedProfile, feed.capture_profile)) {
    clearCameraProfileDraft(cameraId);
    setStreamStatus(`Capture profile already active on ${cameraId}`);
    return;
  }

  try {
    setStreamStatus(`Applying profile to ${cameraId}...`);
    const response = await cameraRouterFetch(
      `/stream_options/${encodeURIComponent(cameraId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: requestedProfile }),
      },
      true
    );
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      throw new Error(data.message || `HTTP ${response.status}`);
    }

    clearCameraProfileDraft(cameraId);
    await refreshCameraFeeds({ silent: true, suppressErrors: true });
    renderCameraProfileOptions(cameraId);
    setStreamStatus(`Applied profile on ${cameraId}`);

    if (cameraPreview.desired && cameraPreview.targetCameraId === cameraId) {
      await startCameraPreview({ autoRestart: true, cameraId, reason: "profile change" });
    }
  } catch (err) {
    setStreamStatus(`Profile apply failed: ${err}`, true);
  }
}

async function refreshCameraFeeds(options = {}) {
  const silent = !!options.silent;
  const suppressErrors = !!options.suppressErrors;
  if (cameraFeedRefreshInFlight) {
    updateMetrics();
    return cameraRouterFeeds.length > 0;
  }
  if (!cameraRouterBaseUrl) {
    if (!suppressErrors) {
      setStreamStatus("Set camera router URL first", true);
    }
    return false;
  }
  cameraFeedRefreshInFlight = true;
  try {
    const response = await cameraRouterFetch("/list", {}, true);
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      if (response.status === 401) {
        cameraRouterSessionKey = "";
        localStorage.removeItem("cameraRouterSessionKey");
        stopCameraImuStream();
        syncPinnedPreviewSource();
        renderHybridFeedOptions();
        updateCameraImuReadouts(null, {
          message: "Camera session expired; IMU polling paused",
          error: true,
        });
        if (cameraPreview.desired) {
          stopCameraPreview();
          setStreamStatus("Camera session expired. Re-authenticate to resume preview.", true);
        }
      }
      if (!suppressErrors) {
        setStreamStatus(`List failed: ${data.message || response.status}`, true);
      }
      return false;
    }

    cameraRouterFeeds = Array.isArray(data.cameras) ? data.cameras : [];
    cameraRouterProtocols = data.protocols || cameraRouterProtocols;
    if (data.routes && typeof data.routes === "object") {
      cameraRouterRoutes = {
        ...cameraRouterRoutes,
        ...data.routes,
      };
      startCameraImuStream();
    }
    const preserveSelectors = silent && (isCameraSelectInteractionActive() || hasPendingCameraProfileSelection());
    renderCameraFeedOptions({ preserveSelectors });
    refreshCameraImu({ silent: true }).catch(() => {});
    syncPinnedPreviewSource();
    if (!silent) {
      setStreamStatus(`Loaded ${cameraRouterFeeds.length} feeds`);
    }
    updateMetrics();
    return true;
  } catch (err) {
    if (!suppressErrors) {
      setStreamStatus(`List error: ${err}`, true);
    }
    return false;
  } finally {
    cameraFeedRefreshInFlight = false;
  }
}

function ensureCameraFeedOption(cameraId) {
  const id = String(cameraId || "").trim();
  if (!id) {
    return;
  }
  const feedSelect = document.getElementById("cameraFeedSelect");
  if (feedSelect) {
    let option = Array.from(feedSelect.options).find((item) => String(item.value || "") === id);
    if (!option) {
      option = document.createElement("option");
      option.value = id;
      option.textContent = `${id} (NKN)`;
      feedSelect.appendChild(option);
    }
    feedSelect.value = id;
  }
  if (!Array.isArray(cameraRouterFeeds)) {
    cameraRouterFeeds = [];
  }
  const existing = cameraRouterFeeds.find((feed) => String(feed && feed.id || "") === id);
  if (!existing) {
    cameraRouterFeeds.push({
      id,
      label: `${id} (NKN)`,
      online: true,
      fps: 0,
      kbps: 0,
      clients: 0,
      modes: {},
      protocols: cameraRouterProtocols,
    });
  }
}

async function startNknPreview(cameraId) {
  activatePreviewMode("nkn");
  const imageEl = document.getElementById("cameraPreviewImage");
  if (!imageEl) {
    return;
  }
  if (!resolveServiceNknTarget("camera")) {
    throw new Error("Set camera/router NKN address before starting NKN preview");
  }

  let frameInFlight = false;
  const pollIntervalMs = ROUTER_NKN_FRAME_POLL_INTERVAL_MS;
  const requestTimeoutMs = Math.max(
    ROUTER_NKN_FRAME_TIMEOUT_MS,
    Math.round(pollIntervalMs * 3.5)
  );

  cameraPreview.activeCameraId = String(cameraId || "").trim();
  cameraPreview.targetCameraId = String(cameraId || "").trim();
  cameraPreview.activeMode = STREAM_MODE_NKN;
  cameraPreview.jpegFallbackAttempted = false;

  const pullFrame = async () => {
    if (!cameraPreview.desired || cameraPreview.activeMode !== STREAM_MODE_NKN) {
      return;
    }
    if (frameInFlight) {
      return;
    }
    frameInFlight = true;
    try {
      const requestedCameraId = String(
        cameraPreview.targetCameraId ||
        cameraPreview.activeCameraId ||
        cameraId ||
        ""
      ).trim();
      const response = await requestCameraFrameViaNkn(
        requestedCameraId,
        {
          max_width: ROUTER_NKN_FRAME_MAX_WIDTH,
          max_height: ROUTER_NKN_FRAME_MAX_HEIGHT,
          max_kbps: ROUTER_NKN_FRAME_MAX_KBPS,
          interval_ms: pollIntervalMs,
          quality: 56,
          min_quality: 22,
          grayscale: false,
        },
        requestTimeoutMs
      );
      const packet = asObject(
        (response && response.framePacket) ||
        (response && response.payload && response.payload.frame_packet) ||
        {}
      );
      const frameB64 = String(packet.frame || "").trim();
      if (!frameB64) {
        throw new Error("Missing frame data in NKN reply");
      }
      const resolvedCameraId = String(packet.camera_id || requestedCameraId || "").trim();
      if (resolvedCameraId) {
        cameraPreview.activeCameraId = resolvedCameraId;
        cameraPreview.targetCameraId = resolvedCameraId;
        localStorage.setItem("cameraRouterSelectedFeed", resolvedCameraId);
        ensureCameraFeedOption(resolvedCameraId);
      }

      imageEl.src = `data:image/jpeg;base64,${frameB64}`;
      cameraPreview.healthFailStreak = 0;
      cameraPreview.zeroClientStreak = 0;
      updateMetrics();
    } catch (err) {
      const message = err && err.message ? err.message : String(err || "NKN frame error");
      cameraPreview.healthFailStreak += 1;
      if (cameraPreview.healthFailStreak <= 2) {
        setStreamStatus(`NKN frame error: ${message}`, true);
      }
      if (cameraPreview.desired && cameraPreview.activeMode === STREAM_MODE_NKN && cameraPreview.healthFailStreak >= 3) {
        scheduleCameraPreviewRestart("nkn frame timeout");
      }
    } finally {
      frameInFlight = false;
    }
  };

  imageEl.onload = () => {
    cameraPreview.healthFailStreak = 0;
    cameraPreview.zeroClientStreak = 0;
    updateMetrics();
  };
  imageEl.onerror = () => {
    cameraPreview.healthFailStreak += 1;
    if (cameraPreview.desired && cameraPreview.activeMode === STREAM_MODE_NKN && cameraPreview.healthFailStreak >= 3) {
      scheduleCameraPreviewRestart("nkn frame decode failure");
    }
  };
  imageEl.onabort = imageEl.onerror;

  await pullFrame();
  cameraPreview.jpegTimer = setInterval(() => {
    pullFrame().catch(() => {});
  }, pollIntervalMs);
  syncPinnedPreviewSource({ forceRefresh: true });
  setPinButtonState();
  updateMetrics();
}

async function startJpegPreview(cameraId) {
  activatePreviewMode("jpeg");
  const imageEl = document.getElementById("cameraPreviewImage");
  if (!imageEl) {
    return;
  }

  const usingTunnel = isTryCloudflareBase(cameraRouterBaseUrl);
  const pollIntervalMs = usingTunnel ? CAMERA_JPEG_POLL_INTERVAL_TUNNEL_MS : CAMERA_JPEG_POLL_INTERVAL_LOCAL_MS;
  const restartErrorThreshold = usingTunnel
    ? CAMERA_JPEG_ERROR_RESTART_THRESHOLD_TUNNEL
    : CAMERA_JPEG_ERROR_RESTART_THRESHOLD_LOCAL;
  let jpegFrameInFlight = false;
  let jpegLastRequestAt = 0;
  let jpegErrorStreak = 0;

  cameraPreview.activeCameraId = cameraId;
  cameraPreview.targetCameraId = cameraId;
  cameraPreview.activeMode = STREAM_MODE_JPEG;

  imageEl.onload = () => {
    jpegFrameInFlight = false;
    jpegErrorStreak = 0;
    cameraPreview.healthFailStreak = 0;
    cameraPreview.zeroClientStreak = 0;
    updateMetrics();
  };
  imageEl.onerror = () => {
    jpegFrameInFlight = false;
    jpegErrorStreak += 1;
    if (cameraPreview.desired && cameraPreview.activeCameraId === cameraId) {
      if (jpegErrorStreak >= restartErrorThreshold) {
        jpegErrorStreak = 0;
        if (usingTunnel && !cameraPreview.jpegFallbackAttempted) {
          cameraPreview.jpegFallbackAttempted = true;
          const modeSelect = document.getElementById("cameraModeSelect");
          if (modeSelect) {
            modeSelect.value = STREAM_MODE_MJPEG;
          }
          localStorage.setItem("cameraRouterSelectedMode", STREAM_MODE_MJPEG);
          setStreamStatus("JPEG preview unstable over tunnel; switching to MJPEG", true);
          startCameraPreview({ autoRestart: true, cameraId, reason: "jpeg tunnel fallback" }).catch(() => {});
          return;
        }
        scheduleCameraPreviewRestart("jpeg transport unstable");
      }
    }
  };
  imageEl.onabort = imageEl.onerror;

  const refresh = () => {
    if (!cameraPreview.desired || cameraPreview.activeMode !== STREAM_MODE_JPEG || cameraPreview.activeCameraId !== cameraId) {
      return;
    }
    const now = Date.now();
    if (jpegFrameInFlight) {
      if (now - jpegLastRequestAt < CAMERA_JPEG_REQUEST_STALL_MS) {
        return;
      }
      jpegFrameInFlight = false;
    }
    jpegFrameInFlight = true;
    jpegLastRequestAt = now;
    imageEl.src = cameraRouterUrl(`/jpeg/${encodeURIComponent(cameraId)}?t=${now}`, true);
  };
  refresh();
  cameraPreview.jpegTimer = setInterval(refresh, pollIntervalMs);
  syncPinnedPreviewSource({ forceRefresh: true });
  setPinButtonState();
  updateMetrics();
}

async function startMjpegPreview(cameraId) {
  activatePreviewMode("mjpeg");
  const imageEl = document.getElementById("cameraPreviewImage");
  if (!imageEl) {
    return;
  }
  imageEl.onload = () => {
    cameraPreview.healthFailStreak = 0;
    cameraPreview.zeroClientStreak = 0;
    updateMetrics();
  };
  imageEl.onerror = () => {
    if (cameraPreview.desired && cameraPreview.activeCameraId === cameraId) {
      scheduleCameraPreviewRestart("image stream error");
    }
  };
  imageEl.onabort = imageEl.onerror;
  imageEl.src = cameraRouterUrl(`/mjpeg/${encodeURIComponent(cameraId)}`, true);
  cameraPreview.activeCameraId = cameraId;
  cameraPreview.targetCameraId = cameraId;
  cameraPreview.activeMode = STREAM_MODE_MJPEG;
  cameraPreview.jpegFallbackAttempted = false;
  syncPinnedPreviewSource({ forceRefresh: true });
  setPinButtonState();
  updateMetrics();
}

async function startMpegTsPreview(cameraId) {
  activatePreviewMode("mpegts");
  const videoEl = document.getElementById("cameraPreviewVideo");
  if (!videoEl) {
    return;
  }
  videoEl.src = cameraRouterUrl(`/mpegts/${encodeURIComponent(cameraId)}`, true);
  videoEl.muted = true;
  videoEl.playsInline = true;
  try {
    await videoEl.play();
    setStreamStatus("MPEG-TS preview started");
  } catch (err) {
    setStreamStatus(`MPEG-TS preview failed to autoplay: ${err}`, true);
  }
}

async function startWebRtcPreview(cameraId) {
  if (!cameraRouterProtocols.webrtc) {
    throw new Error("WebRTC is not available on camera router");
  }

  activatePreviewMode("webrtc");
  const videoEl = document.getElementById("cameraPreviewVideo");
  if (!videoEl) {
    return;
  }

  const pc = new RTCPeerConnection();
  cameraPreview.peerConnection = pc;
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = (event) => {
    if (event.streams && event.streams[0]) {
      videoEl.srcObject = event.streams[0];
      videoEl.muted = true;
      videoEl.playsInline = true;
      videoEl.play().catch(() => {});
    }
  };
  pc.onconnectionstatechange = () => {
    setStreamStatus(`WebRTC state: ${pc.connectionState}`, pc.connectionState === "failed");
  };

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  const response = await cameraRouterFetch(
    `/webrtc/offer/${encodeURIComponent(cameraId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: offer.sdp, type: offer.type }),
    },
    true
  );
  const data = await response.json();
  if (!response.ok || data.status !== "success") {
    throw new Error(data.message || `HTTP ${response.status}`);
  }
  await pc.setRemoteDescription(data.answer);
  setStreamStatus("WebRTC preview started");
}

async function startCameraPreview(options = {}) {
  const autoRestart = !!options.autoRestart;
  const requestedCameraId = typeof options.cameraId === "string" ? options.cameraId.trim() : "";
  const feedSelect = document.getElementById("cameraFeedSelect");
  const modeSelect = document.getElementById("cameraModeSelect");
  if (!feedSelect || !modeSelect) {
    return;
  }

  let mode = modeSelect.value || STREAM_MODE_MJPEG;
  const allowedModes = new Set([STREAM_MODE_MJPEG, STREAM_MODE_JPEG, STREAM_MODE_NKN]);
  if (!allowedModes.has(mode)) {
    mode = STREAM_MODE_MJPEG;
    modeSelect.value = mode;
  }
  if (
    getServiceTransportMode("camera") === SERVICE_TRANSPORT_NKN ||
    parseServiceEndpoint(cameraRouterBaseUrl).transport === SERVICE_TRANSPORT_NKN
  ) {
    mode = STREAM_MODE_NKN;
    modeSelect.value = mode;
  }
  if (mode === STREAM_MODE_JPEG && isTryCloudflareBase(cameraRouterBaseUrl)) {
    mode = STREAM_MODE_MJPEG;
    modeSelect.value = mode;
    localStorage.setItem("cameraRouterSelectedMode", mode);
  }
  if (mode !== STREAM_MODE_JPEG) {
    cameraPreview.jpegFallbackAttempted = false;
  }
  if (mode !== STREAM_MODE_NKN && (!cameraRouterBaseUrl || !cameraRouterSessionKey)) {
    if (autoRestart) {
      setStreamStatus("Camera router authentication required before preview restart", true);
      scheduleCameraPreviewRestart("camera auth required");
      return;
    }
    const authEndpoint = getServicePreferredAuthEndpoint("camera");
    if (!authEndpoint) {
      setStreamStatus("Set camera router URL first", true);
      return;
    }
    setStreamStatus("Camera auth required. Opening prompt...");
    const authOk = await requestServiceAuthForAction("camera", {
      endpoint: authEndpoint,
      timeoutMs: 45000,
    });
    if (!authOk) {
      setStreamStatus("Camera router authentication required before starting preview", true);
      return;
    }
  }

  const cameraId =
    requestedCameraId ||
    cameraPreview.targetCameraId ||
    feedSelect.value ||
    cameraPreview.activeCameraId ||
    localStorage.getItem("cameraRouterSelectedFeed") ||
    "";
  if (requestedCameraId && feedSelect.value !== requestedCameraId && requestedCameraId) {
    feedSelect.value = requestedCameraId;
  }
  if (!cameraId && mode !== STREAM_MODE_NKN) {
    setStreamStatus("Select a feed first", true);
    if (autoRestart) {
      scheduleCameraPreviewRestart("waiting for feed selection");
    } else {
      cameraPreview.desired = false;
      clearCameraPreviewRestartTimer();
      stopCameraPreviewMonitor();
      updateMetrics();
    }
    return;
  }

  if (cameraId) {
    localStorage.setItem("cameraRouterSelectedFeed", cameraId);
  }
  localStorage.setItem("cameraRouterSelectedMode", mode);
  cameraPreview.targetCameraId = cameraId;

  cameraPreview.desired = true;
  clearCameraPreviewRestartTimer();
  stopCameraPreview({ keepDesired: true });
  setStreamStatus(`${autoRestart ? "Restarting" : "Starting"} ${mode} preview for ${cameraId}...`);

  try {
    if (mode === STREAM_MODE_NKN) {
      await startNknPreview(cameraId);
      cameraPreview.activeMode = STREAM_MODE_NKN;
    } else if (mode === STREAM_MODE_JPEG) {
      await startJpegPreview(cameraId);
      cameraPreview.activeMode = STREAM_MODE_JPEG;
    } else {
      await startMjpegPreview(cameraId);
      cameraPreview.activeMode = STREAM_MODE_MJPEG;
    }
    cameraPreview.restartAttempts = 0;
    cameraPreview.healthFailStreak = 0;
    cameraPreview.zeroClientStreak = 0;
    startCameraPreviewMonitor();
    monitorCameraPreviewHealth().catch(() => {});
    setStreamStatus(`${mode.toUpperCase()} preview started`);
  } catch (err) {
    setStreamStatus(`Preview failed: ${err}`, true);
    scheduleCameraPreviewRestart("startup failure");
  }
}

function setupStreamConfigUi() {
  setupAudioConfigUi();
  if (streamUiInitialized) {
    return;
  }
  streamUiInitialized = true;

  const baseInput = document.getElementById("cameraRouterBaseInput");
  const passInput = document.getElementById("cameraRouterPasswordInput");
  const authBtn = document.getElementById("cameraRouterAuthBtn");
  const refreshBtn = document.getElementById("cameraRouterRefreshBtn");
  const rotateSessionBtn = document.getElementById("cameraRouterRotateSessionBtn");
  const startBtn = document.getElementById("cameraPreviewStartBtn");
  const stopBtn = document.getElementById("cameraPreviewStopBtn");
  const shareBtn = document.getElementById("cameraPreviewShareBtn");
  const profileSelect = document.getElementById("cameraProfileSelect");
  const profileApplyBtn = document.getElementById("cameraProfileApplyBtn");
  const modeSelect = document.getElementById("cameraModeSelect");
  const feedSelect = document.getElementById("cameraFeedSelect");
  initializePinnedPreviewUi();
  startCameraFeedPolling();
  startCameraImuPolling();
  window.addEventListener("beforeunload", stopCameraImuStream, { once: true });

  if (baseInput) {
    baseInput.value = cameraRouterBaseUrl;
    baseInput.addEventListener("change", () => {
      const parsed = parseServiceEndpoint(baseInput.value);
      if (!parsed.value) {
        return;
      }
      cameraRouterBaseUrl = parsed.value;
      setServiceTransportMode("camera", parsed.transport || SERVICE_TRANSPORT_HTTP);
      setServiceNknAddress("camera", parsed.nknAddress || "");
      localStorage.setItem("cameraRouterBaseUrl", cameraRouterBaseUrl);
      hybridPreviewSourceKey = "";
      stopCameraImuStream();
      syncPinnedPreviewSource();
      renderHybridFeedOptions();
      startCameraImuStream();
      refreshCameraImu({ silent: true }).catch(() => {});
    });
  }
  if (passInput) {
    passInput.value = cameraRouterPassword;
  }
  if (!cameraPreview.targetCameraId) {
    cameraPreview.targetCameraId = localStorage.getItem("cameraRouterSelectedFeed") || "";
  }
  if (modeSelect) {
    const savedMode = localStorage.getItem("cameraRouterSelectedMode") || STREAM_MODE_MJPEG;
    const allowedModes = new Set([STREAM_MODE_MJPEG, STREAM_MODE_JPEG, STREAM_MODE_NKN]);
    modeSelect.value = allowedModes.has(savedMode) ? savedMode : STREAM_MODE_MJPEG;
    localStorage.setItem("cameraRouterSelectedMode", modeSelect.value);
  }

  if (authBtn) {
    authBtn.addEventListener("click", authenticateCameraRouter);
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", refreshCameraFeeds);
  }
  if (rotateSessionBtn) {
    rotateSessionBtn.addEventListener("click", rotateCameraRouterSessionKey);
  }
  if (startBtn) {
    startBtn.addEventListener("click", startCameraPreview);
  }
  if (stopBtn) {
    stopBtn.addEventListener("click", () => {
      stopCameraPreview();
      setStreamStatus("Preview stopped");
    });
  }
  if (shareBtn) {
    resetPreviewShareButton();
    shareBtn.addEventListener("click", shareCurrentPreviewLink);
  }
  if (feedSelect) {
    feedSelect.addEventListener("pointerdown", () => {
      markCameraSelectInteraction(2500);
    });
    feedSelect.addEventListener("focus", () => {
      markCameraSelectInteraction(2500);
    });
    feedSelect.addEventListener("keydown", () => {
      markCameraSelectInteraction(1800);
    });
    feedSelect.addEventListener("blur", () => {
      markCameraSelectInteraction(350);
    });
    feedSelect.addEventListener("change", () => {
      const nextFeed = feedSelect.value || "";
      localStorage.setItem("cameraRouterSelectedFeed", nextFeed);
      cameraPreview.targetCameraId = nextFeed;
      renderCameraProfileOptions(nextFeed);
      if (cameraPreview.desired && nextFeed) {
        startCameraPreview({ autoRestart: true, cameraId: nextFeed, reason: "feed change" }).catch(() => {});
      } else {
        updateMetrics();
      }
    });
  }
  if (profileSelect) {
    profileSelect.addEventListener("pointerdown", () => {
      markCameraSelectInteraction(2500);
    });
    profileSelect.addEventListener("focus", () => {
      markCameraSelectInteraction(2500);
    });
    profileSelect.addEventListener("keydown", () => {
      markCameraSelectInteraction(1800);
    });
    profileSelect.addEventListener("blur", () => {
      markCameraSelectInteraction(350);
    });
    profileSelect.addEventListener("change", () => {
      markCameraSelectInteraction(12000);
      const cameraId = String((feedSelect && feedSelect.value) || "").trim();
      const feed = cameraRouterFeeds.find((item) => item.id === cameraId) || null;
      if (!cameraId || !feed) {
        return;
      }
      const profiles = Array.isArray(feed.available_profiles) ? feed.available_profiles : [];
      const selectedIndex = parseInt(profileSelect.value, 10);
      if (profiles.length > 0 && Number.isInteger(selectedIndex) && selectedIndex >= 0 && selectedIndex < profiles.length) {
        const selectedProfile = profiles[selectedIndex];
        if (feed.capture_profile && profileMatches(selectedProfile, feed.capture_profile)) {
          clearCameraProfileDraft(cameraId);
        } else {
          setCameraProfileDraft(cameraId, selectedProfile);
        }
      } else {
        clearCameraProfileDraft(cameraId);
      }
    });
  }
  if (modeSelect) {
    modeSelect.addEventListener("change", () => {
      const allowedModes = new Set([STREAM_MODE_MJPEG, STREAM_MODE_JPEG, STREAM_MODE_NKN]);
      if (!allowedModes.has(modeSelect.value)) {
        modeSelect.value = STREAM_MODE_MJPEG;
      }
      localStorage.setItem("cameraRouterSelectedMode", modeSelect.value);
      if (cameraPreview.desired) {
        startCameraPreview({ autoRestart: true, reason: "mode change" }).catch(() => {});
      }
    });
  }
  if (profileApplyBtn) {
    profileApplyBtn.addEventListener("click", applySelectedCameraProfile);
  }

  if (cameraRouterBaseUrl && cameraRouterSessionKey) {
    startCameraImuStream();
    refreshCameraFeeds();
    refreshCameraImu({ silent: true }).catch(() => {});
  } else {
    stopCameraImuStream();
    if (
      (modeSelect && modeSelect.value === STREAM_MODE_NKN) ||
      getServiceTransportMode("camera") === SERVICE_TRANSPORT_NKN
    ) {
      setStreamStatus("NKN relay mode ready. Set Router NKN address in Settings.");
    } else {
      setStreamStatus("Configure camera router URL + password, then authenticate");
    }
    renderHybridFeedOptions();
    updateCameraImuReadouts(null, {
      message: "Authenticate camera router to read /imu",
      error: false,
    });
  }
}

function withAudioSession(path, includeSession = true) {
  if (!includeSession || !audioRouterSessionKey) {
    return path;
  }
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}session_key=${encodeURIComponent(audioRouterSessionKey)}`;
}

function audioRouterUrl(path, includeSession = true) {
  if (!audioRouterBaseUrl) {
    return "";
  }
  return `${audioRouterBaseUrl}${withAudioSession(path, includeSession)}`;
}

function setAudioStatus(message, error = false) {
  const statusEl = document.getElementById("audioStreamStatus");
  if (!statusEl) {
    return;
  }
  statusEl.textContent = String(message || "");
  statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function setAudioConnectionMeta(message, error = false) {
  const metaEl = document.getElementById("audioConnectionMeta");
  if (!metaEl) {
    return;
  }
  metaEl.textContent = String(message || "");
  metaEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function installAudioPlaybackGestureHooks() {
  if (audioPlaybackGestureHooksInstalled) {
    return;
  }
  audioPlaybackGestureHooksInstalled = true;
  const resumePlayback = () => {
    requestAudioAutoplayUnlock().catch(() => {});
    const audioEl = document.getElementById("audioRemotePlayer");
    if (audioEl && audioEl.srcObject) {
      playRemoteAudioElement(audioEl).catch(() => {});
    }
  };
  ["pointerdown", "touchstart", "keydown"].forEach((eventName) => {
    window.addEventListener(eventName, resumePlayback, { passive: true });
  });
}

function requestAudioAutoplayUnlock() {
  if (audioPlaybackUnlocked) {
    return Promise.resolve(true);
  }
  if (audioPlaybackUnlockInFlight) {
    return audioPlaybackUnlockInFlight;
  }

  audioPlaybackUnlockInFlight = (async () => {
    let unlocked = false;
    const audioEl = document.getElementById("audioRemotePlayer");
    if (audioEl) {
      audioEl.autoplay = true;
      audioEl.setAttribute("playsinline", "");
    }

    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    if (AudioContextCtor) {
      try {
        if (!audioPlaybackUnlockContext) {
          audioPlaybackUnlockContext = new AudioContextCtor();
        }
        if (audioPlaybackUnlockContext && audioPlaybackUnlockContext.state === "suspended") {
          await audioPlaybackUnlockContext.resume();
        }
        if (audioPlaybackUnlockContext && audioPlaybackUnlockContext.state === "running") {
          const oscillator = audioPlaybackUnlockContext.createOscillator();
          const gain = audioPlaybackUnlockContext.createGain();
          gain.gain.value = 0.00001;
          oscillator.connect(gain);
          gain.connect(audioPlaybackUnlockContext.destination);
          oscillator.start();
          oscillator.stop(audioPlaybackUnlockContext.currentTime + 0.01);
          unlocked = true;
        }
      } catch (err) {}
    }

    if (audioEl) {
      const previousMuted = !!audioEl.muted;
      try {
        audioEl.muted = true;
        const playPromise = audioEl.play();
        if (playPromise && typeof playPromise.then === "function") {
          await playPromise;
        }
        audioEl.pause();
        unlocked = true;
      } catch (err) {
      } finally {
        audioEl.muted = previousMuted;
      }
    }

    audioPlaybackUnlocked = audioPlaybackUnlocked || unlocked;
    if (audioPlaybackUnlocked && audioEl && audioEl.srcObject) {
      await playRemoteAudioElement(audioEl).catch(() => false);
    }
    return audioPlaybackUnlocked;
  })().finally(() => {
    audioPlaybackUnlockInFlight = null;
  });

  return audioPlaybackUnlockInFlight;
}

async function playRemoteAudioElement(audioEl) {
  if (!audioEl) {
    return false;
  }
  try {
    const playPromise = audioEl.play();
    if (playPromise && typeof playPromise.then === "function") {
      await playPromise;
    }
    return true;
  } catch (err) {
    setAudioConnectionMeta(
      "Remote audio playback blocked by browser policy. Click Start Audio Bridge again.",
      true
    );
    return false;
  }
}

function resetAudioPlayer() {
  const audioEl = document.getElementById("audioRemotePlayer");
  if (!audioEl) {
    return;
  }
  audioEl.srcObject = null;
  audioEl.removeAttribute("src");
  try {
    audioEl.load();
  } catch (err) {}
}

function renderAudioDeviceSelect(selectEl, devices, selectedIndex) {
  if (!selectEl) {
    return;
  }
  selectEl.innerHTML = "";
  const defaultOpt = document.createElement("option");
  defaultOpt.value = "default";
  defaultOpt.textContent = "System Default";
  selectEl.appendChild(defaultOpt);

  (devices || []).forEach((device) => {
    const opt = document.createElement("option");
    const id = Number(device.id);
    const host = String(device.hostapi || "").trim();
    const name = String(device.name || `Device ${id}`);
    const channelCount = Number(device.max_input_channels || device.max_output_channels || 0) || 0;
    const defaultTag = device.is_default ? " [default]" : "";
    opt.value = String(id);
    opt.textContent = `${name}${defaultTag}${host ? ` (${host})` : ""} | ch ${channelCount}`;
    selectEl.appendChild(opt);
  });

  if (Number.isInteger(selectedIndex) && String(selectedIndex) !== "") {
    selectEl.value = String(selectedIndex);
  } else {
    selectEl.value = "default";
  }
}

function renderAudioDeviceRows(payload) {
  const rowsEl = document.getElementById("audioDeviceList");
  if (!rowsEl) {
    return;
  }
  rowsEl.innerHTML = "";

  const audioInfo = asObject(payload.audio);
  const inputInfo = asObject(audioInfo.input_device_info);
  const outputInfo = asObject(audioInfo.output_device_info);
  const peers = Number(asObject(payload.connections).active_webrtc_peers || 0) || 0;
  const sampleRate = Number(audioInfo.sample_rate || 0) || 0;
  const channels = Number(audioInfo.channels || 0) || 0;

  const rows = [
    `Input: ${inputInfo.name || "N/A"}${Number.isInteger(audioInfo.input_index) ? ` (#${audioInfo.input_index})` : ""}`,
    `Output: ${outputInfo.name || "N/A"}${Number.isInteger(audioInfo.output_index) ? ` (#${audioInfo.output_index})` : ""}`,
    `Format: ${sampleRate || "--"} Hz | ${channels || "--"} channel(s)`,
    `Active peers: ${peers}`,
  ];
  rows.forEach((text) => {
    const line = document.createElement("div");
    line.className = "stream-feed-row";
    line.textContent = text;
    rowsEl.appendChild(line);
  });
}

async function audioRouterFetch(path, options = {}, includeSession = true) {
  if (!audioRouterBaseUrl) {
    throw new Error("Audio Router URL is not configured");
  }
  const parsedEndpoint = parseServiceEndpoint(audioRouterBaseUrl);
  const useNkn =
    parsedEndpoint.transport === SERVICE_TRANSPORT_NKN ||
    getServiceTransportMode("audio") === SERVICE_TRANSPORT_NKN;
  if (useNkn) {
    const nknAddress = normalizeNknAddress(parsedEndpoint.nknAddress || getServiceNknAddress("audio"));
    setServiceTransportMode("audio", SERVICE_TRANSPORT_NKN);
    if (nknAddress) {
      setServiceNknAddress("audio", nknAddress);
    }
    const rpcPath = withAudioSession(normalizeServicePath(path), includeSession);
    return requestServiceRpcViaNkn("audio", rpcPath, options);
  }
  setServiceTransportMode("audio", SERVICE_TRANSPORT_HTTP);
  const url = audioRouterUrl(path, includeSession);
  let response = null;
  try {
    response = await fetch(url, options);
  } catch (err) {
    const detail = err && err.message ? err.message : String(err);
    if (isTryCloudflareBase(audioRouterBaseUrl)) {
      throw new Error(
        "Audio router tunnel unreachable. This trycloudflare URL is likely expired or offline; refresh endpoint discovery."
      );
    }
    throw new Error(detail);
  }
  if (response.status === 530 && isTryCloudflareBase(audioRouterBaseUrl)) {
    throw new Error(
      "Audio router tunnel is offline (Cloudflare 530). Refresh the tunnel URL from /router_info and retry."
    );
  }
  return response;
}

async function stopAudioBridge(options = {}) {
  const keepDesired = !!options.keepDesired;
  const silent = !!options.silent;
  audioBridge.starting = false;
  if (!keepDesired) {
    audioBridge.desired = false;
  }

  const pc = audioBridge.peerConnection;
  audioBridge.peerConnection = null;
  if (pc) {
    try {
      pc.ontrack = null;
      pc.onconnectionstatechange = null;
    } catch (err) {}
    try {
      pc.getSenders().forEach((sender) => {
        if (sender && sender.track) {
          sender.track.stop();
        }
      });
    } catch (err) {}
    try {
      pc.close();
    } catch (err) {}
  }

  if (audioBridge.localStream) {
    try {
      audioBridge.localStream.getTracks().forEach((track) => track.stop());
    } catch (err) {}
  }
  if (audioBridge.remoteStream) {
    try {
      audioBridge.remoteStream.getTracks().forEach((track) => track.stop());
    } catch (err) {}
  }
  audioBridge.localStream = null;
  audioBridge.remoteStream = null;
  audioBridge.remoteTrack = null;
  audioBridge.active = false;
  resetAudioPlayer();
  updateMetrics();
  if (!silent) {
    setAudioStatus("Audio bridge stopped");
    setAudioConnectionMeta("No active audio bridge.");
  }
}

async function refreshAudioDevices(options = {}) {
  const silent = !!options.silent;
  if (audioDeviceRefreshInFlight) {
    return false;
  }
  if (!audioRouterBaseUrl) {
    if (!silent) {
      setAudioStatus("Set audio router URL first", true);
    }
    return false;
  }
  if (!audioRouterSessionKey) {
    if (!silent) {
      setAudioStatus("Authenticate with audio router first", true);
    }
    return false;
  }

  audioDeviceRefreshInFlight = true;
  try {
    const response = await audioRouterFetch("/list", { cache: "no-store" }, true);
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = {};
    }
    if (!response.ok || data.status !== "success") {
      if (response.status === 401) {
        audioRouterSessionKey = "";
        localStorage.removeItem("audioRouterSessionKey");
        await stopAudioBridge({ silent: true });
      }
      if (!silent) {
        setAudioStatus(`Device refresh failed: ${data.message || response.status}`, true);
      }
      return false;
    }

    audioDevices = {
      inputs: Array.isArray(asObject(data.devices).inputs) ? asObject(data.devices).inputs : [],
      outputs: Array.isArray(asObject(data.devices).outputs) ? asObject(data.devices).outputs : [],
    };

    const audioInfo = asObject(data.audio);
    const inputIndex = Number.isInteger(audioInfo.input_index) ? audioInfo.input_index : null;
    const outputIndex = Number.isInteger(audioInfo.output_index) ? audioInfo.output_index : null;

    renderAudioDeviceSelect(
      document.getElementById("audioInputDeviceSelect"),
      audioDevices.inputs,
      inputIndex
    );
    renderAudioDeviceSelect(
      document.getElementById("audioOutputDeviceSelect"),
      audioDevices.outputs,
      outputIndex
    );
    renderAudioDeviceRows(data);

    const peers = Number(asObject(data.connections).active_webrtc_peers || 0) || 0;
    setAudioConnectionMeta(
      peers > 0
        ? `Audio peer active (${peers})`
        : `Ready (${new Date().toLocaleTimeString()})`
    );

    if (!silent) {
      setAudioStatus(`Loaded ${audioDevices.inputs.length} input and ${audioDevices.outputs.length} output device(s)`);
    }
    return true;
  } catch (err) {
    if (!silent) {
      setAudioStatus(`Device refresh error: ${err}`, true);
    }
    return false;
  } finally {
    audioDeviceRefreshInFlight = false;
  }
}

async function authenticateAudioRouterWithPassword(password, options = {}) {
  const silent = !!options.silent;
  const baseCandidate = String(options.baseUrl || audioRouterBaseUrl || "").trim();
  const cleanPassword = String(password || "").trim();
  const parsedEndpoint = parseServiceEndpoint(baseCandidate);
  const normalizedBase = String(parsedEndpoint.value || "").trim();

  if (!normalizedBase || !cleanPassword) {
    if (!silent) {
      setAudioStatus("Enter both audio router URL and password", true);
    }
    return false;
  }

  setServiceTransportMode("audio", parsedEndpoint.transport || SERVICE_TRANSPORT_HTTP);
  setServiceNknAddress("audio", parsedEndpoint.nknAddress || "");
  audioRouterBaseUrl = normalizedBase;
  setServiceAuthPassword("audio", cleanPassword);
  localStorage.setItem("audioRouterBaseUrl", audioRouterBaseUrl);
  const baseInput = document.getElementById("audioRouterBaseInput");
  if (baseInput) {
    baseInput.value = audioRouterBaseUrl;
  }

  if (!silent) {
    setAudioStatus("Authenticating with audio router...");
  }

  try {
    const response = await audioRouterFetch(
      "/auth",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: cleanPassword }),
      },
      false
    );
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = {};
    }

    if (!response.ok || data.status !== "success") {
      if (!silent) {
        setAudioStatus(`Auth failed: ${data.message || response.status}`, true);
      }
      return false;
    }

    const nextSessionKey = String(data.session_key || "").trim();
    if (!nextSessionKey) {
      if (!silent) {
        setAudioStatus("Auth failed: response missing session key", true);
      }
      return false;
    }

    audioRouterSessionKey = nextSessionKey;
    localStorage.setItem("audioRouterSessionKey", audioRouterSessionKey);
    if (!silent) {
      setAudioStatus(`Authenticated. Session timeout ${Number(data.timeout) || "--"}s`);
    }

    await refreshAudioDevices({ silent: true });
    if (audioBridge.desired) {
      await startAudioBridge({ forceRestart: true });
    }
    return true;
  } catch (err) {
    if (!silent) {
      setAudioStatus(`Auth error: ${err}`, true);
    }
    return false;
  }
}

async function authenticateAudioRouter() {
  const baseInput = document.getElementById("audioRouterBaseInput");
  const passInput = document.getElementById("audioRouterPasswordInput");
  if (!baseInput || !passInput) {
    return false;
  }
  return authenticateAudioRouterWithPassword(passInput.value, {
    baseUrl: baseInput.value,
    silent: false,
  });
}

async function rotateAudioRouterSessionKey() {
  if (audioSessionRotateInFlight) {
    return;
  }
  if (!audioRouterBaseUrl) {
    setAudioStatus("Set audio router URL first", true);
    return;
  }
  if (!audioRouterSessionKey) {
    setAudioStatus("Authenticate with audio router before rotating session keys", true);
    return;
  }

  audioSessionRotateInFlight = true;
  const rotateBtn = document.getElementById("audioRouterRotateSessionBtn");
  if (rotateBtn) {
    rotateBtn.disabled = true;
  }
  setAudioStatus("Rotating audio session key...");

  try {
    const response = await audioRouterFetch(
      "/session/rotate",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
      true
    );
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      if (response.status === 401) {
        audioRouterSessionKey = "";
        localStorage.removeItem("audioRouterSessionKey");
        await stopAudioBridge({ silent: true });
      }
      setAudioStatus(`Session rotate failed: ${data.message || response.status}`, true);
      return;
    }

    const nextSessionKey = String(data.session_key || "").trim();
    if (!nextSessionKey) {
      setAudioStatus("Session rotate failed: missing session key", true);
      return;
    }
    audioRouterSessionKey = nextSessionKey;
    localStorage.setItem("audioRouterSessionKey", audioRouterSessionKey);
    await refreshAudioDevices({ silent: true });
    setAudioStatus(`Session key rotated. Invalidated ${Number(data.invalidated_sessions) || 0} session(s).`);
  } catch (err) {
    setAudioStatus(`Session rotate error: ${err}`, true);
  } finally {
    audioSessionRotateInFlight = false;
    if (rotateBtn) {
      rotateBtn.disabled = false;
    }
  }
}

async function applyAudioDeviceSelection() {
  if (!audioRouterBaseUrl || !audioRouterSessionKey) {
    setAudioStatus("Authenticate with audio router before applying device selection", true);
    return;
  }
  const inputSelect = document.getElementById("audioInputDeviceSelect");
  const outputSelect = document.getElementById("audioOutputDeviceSelect");
  if (!inputSelect || !outputSelect) {
    return;
  }

  const parseDeviceValue = (raw) => {
    const value = String(raw || "").trim();
    if (!value || value === "default") {
      return "default";
    }
    const asNum = Number(value);
    if (Number.isInteger(asNum)) {
      return asNum;
    }
    return "default";
  };

  const payload = {
    input_device: parseDeviceValue(inputSelect.value),
    output_device: parseDeviceValue(outputSelect.value),
  };

  try {
    setAudioStatus("Applying audio device selection...");
    const response = await audioRouterFetch(
      "/devices/select",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
      true
    );
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      throw new Error(data.message || `HTTP ${response.status}`);
    }
    renderAudioDeviceRows(data);
    await refreshAudioDevices({ silent: true });
    setAudioStatus("Audio device selection applied");
    if (audioBridge.desired) {
      await startAudioBridge({ forceRestart: true });
    }
  } catch (err) {
    setAudioStatus(`Device apply failed: ${err}`, true);
  }
}

async function startAudioBridge(options = {}) {
  const forceRestart = !!options.forceRestart;
  if (audioBridge.starting) {
    return;
  }
  const autoplayUnlocked = await requestAudioAutoplayUnlock().catch(() => false);
  if (!autoplayUnlocked) {
    setAudioConnectionMeta(
      "Audio autoplay may be blocked; click Start Audio Bridge to grant playback.",
      true
    );
  }
  if (!audioRouterBaseUrl || !audioRouterSessionKey) {
    const authEndpoint = getServicePreferredAuthEndpoint("audio");
    if (!authEndpoint) {
      setAudioStatus("Set audio router URL first", true);
      return;
    }
    setAudioStatus("Audio auth required. Opening prompt...");
    const authOk = await requestServiceAuthForAction("audio", {
      endpoint: authEndpoint,
      timeoutMs: 45000,
    });
    if (!authOk || !audioRouterBaseUrl || !audioRouterSessionKey) {
      setAudioStatus("Audio router authentication required before starting bridge", true);
      return;
    }
  }
  if (audioBridge.active && !forceRestart) {
    setAudioStatus("Audio bridge already active");
    return;
  }

  audioBridge.starting = true;
  audioBridge.desired = true;

  try {
    await stopAudioBridge({ keepDesired: true, silent: true });
    const sendBrowserMic = !!(document.getElementById("audioBrowserMicToggle") || {}).checked;

    setAudioStatus("Starting bidirectional audio bridge...");
    if (!window.RTCPeerConnection) {
      throw new Error("WebRTC is not available in this browser");
    }

    const peer = new RTCPeerConnection();
    audioBridge.peerConnection = peer;
    peer.addTransceiver("audio", { direction: "recvonly" });

    if (sendBrowserMic) {
      audioBridge.localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      audioBridge.localStream.getAudioTracks().forEach((track) => {
        peer.addTrack(track, audioBridge.localStream);
      });
    }

    peer.ontrack = (event) => {
      if (!event.streams || !event.streams[0]) {
        return;
      }
      const stream = event.streams[0];
      audioBridge.remoteStream = stream;
      audioBridge.remoteTrack = stream.getAudioTracks()[0] || null;
      const audioEl = document.getElementById("audioRemotePlayer");
      if (!audioEl) {
        return;
      }
      audioEl.srcObject = stream;
      audioEl.autoplay = true;
      audioEl.setAttribute("playsinline", "");
      audioEl.volume = 1;
      audioEl.muted = false;
      const replay = () => {
        playRemoteAudioElement(audioEl).catch(() => {});
      };
      audioEl.addEventListener("loadedmetadata", replay, { once: true });
      audioEl.addEventListener("canplay", replay, { once: true });
      requestAudioAutoplayUnlock()
        .catch(() => false)
        .finally(replay);
    };

    peer.onconnectionstatechange = () => {
      const state = String(peer.connectionState || "");
      if (state === "failed" || state === "disconnected" || state === "closed") {
        setAudioConnectionMeta(`Audio bridge ${state}`, true);
        if (audioBridge.desired) {
          setAudioStatus(`Audio bridge ${state}`, true);
        }
      }
    };

    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    const response = await audioRouterFetch(
      "/webrtc/offer",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp: offer.sdp, type: offer.type }),
      },
      true
    );
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      throw new Error(data.message || `HTTP ${response.status}`);
    }

    await peer.setRemoteDescription(data.answer);
    audioBridge.active = true;
    audioBridge.lastError = "";
    setAudioStatus("Bidirectional audio bridge active");
    setAudioConnectionMeta(`Bridge live${sendBrowserMic ? " | browser mic uplink on" : ""}`);
    updateMetrics();
  } catch (err) {
    audioBridge.lastError = String(err || "");
    setAudioStatus(`Audio bridge failed: ${err}`, true);
    setAudioConnectionMeta(`Bridge error: ${err}`, true);
    await stopAudioBridge({ keepDesired: false, silent: true });
  } finally {
    audioBridge.starting = false;
  }
}

function setupAudioConfigUi() {
  if (audioUiInitialized) {
    return;
  }
  audioUiInitialized = true;
  installAudioPlaybackGestureHooks();

  const baseInput = document.getElementById("audioRouterBaseInput");
  const passInput = document.getElementById("audioRouterPasswordInput");
  const authBtn = document.getElementById("audioRouterAuthBtn");
  const refreshBtn = document.getElementById("audioRouterRefreshBtn");
  const rotateBtn = document.getElementById("audioRouterRotateSessionBtn");
  const applyDevicesBtn = document.getElementById("audioDeviceApplyBtn");
  const startBtn = document.getElementById("audioBridgeStartBtn");
  const stopBtn = document.getElementById("audioBridgeStopBtn");
  const browserMicToggle = document.getElementById("audioBrowserMicToggle");

  if (baseInput) {
    baseInput.value = audioRouterBaseUrl;
    baseInput.addEventListener("change", () => {
      const parsed = parseServiceEndpoint(baseInput.value);
      if (!parsed.value) {
        return;
      }
      audioRouterBaseUrl = parsed.value;
      setServiceTransportMode("audio", parsed.transport || SERVICE_TRANSPORT_HTTP);
      setServiceNknAddress("audio", parsed.nknAddress || "");
      localStorage.setItem("audioRouterBaseUrl", audioRouterBaseUrl);
    });
  }
  if (passInput) {
    passInput.value = audioRouterPassword;
  }

  if (authBtn) {
    authBtn.addEventListener("click", authenticateAudioRouter);
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      refreshAudioDevices({ silent: false }).catch(() => {});
    });
  }
  if (rotateBtn) {
    rotateBtn.addEventListener("click", rotateAudioRouterSessionKey);
  }
  if (applyDevicesBtn) {
    applyDevicesBtn.addEventListener("click", applyAudioDeviceSelection);
  }
  if (startBtn) {
    startBtn.addEventListener("click", () => {
      requestAudioAutoplayUnlock().catch(() => {});
      startAudioBridge({ forceRestart: false }).catch(() => {});
    });
  }
  if (stopBtn) {
    stopBtn.addEventListener("click", () => {
      stopAudioBridge({ keepDesired: false, silent: false }).catch(() => {});
    });
  }

  window.addEventListener("beforeunload", () => {
    stopAudioBridge({ keepDesired: false, silent: true }).catch(() => {});
  }, { once: true });

  if (browserMicToggle) {
    initializeCheckboxToggleButtons(browserMicToggle.closest(".toggle-checkbox-btn") || document);
  }

  if (audioRouterBaseUrl && audioRouterSessionKey) {
    refreshAudioDevices({ silent: true }).catch(() => {});
    setAudioStatus("Audio router session restored");
  } else {
    setAudioStatus("Configure audio router URL + password, then authenticate");
    setAudioConnectionMeta("No active audio bridge.");
  }
}

function setHybridStatus(message, error = false) {
  const statusEl = document.getElementById("hybridStatus");
  if (!statusEl) {
    return;
  }
  statusEl.textContent = String(message || "");
  statusEl.style.color = error ? "#ff4444" : "var(--accent)";
}

function notifyHybridPreviewResize(source = "layout") {
  window.dispatchEvent(new CustomEvent("hybrid-preview-resize", {
    detail: { source, mode: activeHybridTab },
  }));
}

function setHybridPreviewAspect(width, height) {
  const wrapEl = document.getElementById("hybridPreviewWrap");
  if (!wrapEl) {
    return;
  }
  const w = Number(width);
  const h = Number(height);
  if (Number.isFinite(w) && w > 0 && Number.isFinite(h) && h > 0) {
    wrapEl.style.setProperty("--hybrid-stream-aspect", `${w} / ${h}`);
    notifyHybridPreviewResize("aspect-known");
    return;
  }
  wrapEl.style.setProperty("--hybrid-stream-aspect", HYBRID_PREVIEW_DEFAULT_ASPECT);
  notifyHybridPreviewResize("aspect-default");
}

function resetHybridPoseState() {
  const defaults = hybridPoseDefaults();
  Object.keys(defaults).forEach((axis) => {
    hybridPose[axis] = defaults[axis];
  });
  hybridLastCommand = "";
  hybridLastDispatchMs = 0;
  renderHybridReadout();
}

function hybridClampValue(axis, value) {
  const limits = hybridPoseLimits()[axis];
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return limits ? limits.min : 0;
  }
  if (!limits) {
    return numeric;
  }
  return Math.min(limits.max, Math.max(limits.min, numeric));
}

function hybridSyncPoseFromHeadControls() {
  const defaults = hybridPoseDefaults();
  Object.keys(defaults).forEach((axis) => {
    const el = document.getElementById(axis);
    const fallback = defaults[axis];
    const parsed = el ? Number(el.value) : fallback;
    const normalized = Number.isFinite(parsed) ? parsed : fallback;
    hybridPose[axis] = hybridClampValue(axis, normalized);
  });
}

function normalizeHybridTouchTuneable(key, rawValue) {
  const numeric = Number(rawValue);
  const fallback = Number(HYBRID_TOUCH_DEFAULTS[key]);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  const bounds = {
    speed: { min: 0, max: 10, step: 0.1 },
    accel: { min: 0, max: 10, step: 0.1 },
    xLimit: { min: 50, max: 1200, step: 1 },
    yLimit: { min: 50, max: 1200, step: 1 },
    zLimit: { min: 50, max: 1200, step: 1 },
    rLimit: { min: 50, max: 1200, step: 1 },
    pLimit: { min: 50, max: 1200, step: 1 },
  }[key];
  if (!bounds) {
    return fallback;
  }
  const clamped = Math.min(bounds.max, Math.max(bounds.min, numeric));
  if (bounds.step >= 1) {
    return Math.round(clamped);
  }
  return Number(clamped.toFixed(2));
}

function applyHybridTouchTuneables(options = {}) {
  hybridPose.S = Number(hybridTouchTuneables.speed);
  hybridPose.A = Number(hybridTouchTuneables.accel);
  Object.keys(hybridPose).forEach((axis) => {
    hybridPose[axis] = hybridClampValue(axis, hybridPose[axis]);
  });
  const forceDispatch = !!options.forceDispatch;
  if (forceDispatch) {
    dispatchHybridCommand({ force: true, minIntervalMs: 0 });
    return;
  }
  renderHybridReadout();
}

function bindHybridTouchTuneablePair(numberId, rangeId, key) {
  const numberEl = document.getElementById(numberId);
  const rangeEl = document.getElementById(rangeId);
  if (!numberEl || !rangeEl) {
    return;
  }

  const syncFields = (value) => {
    const bounds = {
      speed: { step: 0.1 },
      accel: { step: 0.1 },
      xLimit: { step: 1 },
      yLimit: { step: 1 },
      zLimit: { step: 1 },
      rLimit: { step: 1 },
      pLimit: { step: 1 },
    }[key] || { step: 1 };
    const text = bounds.step >= 1 ? String(Math.round(value)) : String(Number(value).toFixed(2));
    numberEl.value = text;
    rangeEl.value = text;
  };

  const applyRaw = (rawValue, options = {}) => {
    const normalized = normalizeHybridTouchTuneable(key, rawValue);
    hybridTouchTuneables[key] = normalized;
    syncFields(normalized);
    const shouldDispatch = options.forceDispatch === true;
    applyHybridTouchTuneables({ forceDispatch: shouldDispatch });
  };

  numberEl.addEventListener("input", () => applyRaw(numberEl.value, { forceDispatch: activeHybridTab === "touch" }));
  rangeEl.addEventListener("input", () => applyRaw(rangeEl.value, { forceDispatch: activeHybridTab === "touch" }));
  applyRaw(hybridTouchTuneables[key], { forceDispatch: false });
}

function setupHybridTouchTuneables() {
  if (hybridTouchTuneablesInitialized) {
    return;
  }
  hybridTouchTuneablesInitialized = true;

  bindHybridTouchTuneablePair("touchTuneSpeed", "touchTuneSpeedRange", "speed");
  bindHybridTouchTuneablePair("touchTuneAccel", "touchTuneAccelRange", "accel");
  bindHybridTouchTuneablePair("touchTuneXLimit", "touchTuneXLimitRange", "xLimit");
  bindHybridTouchTuneablePair("touchTuneYLimit", "touchTuneYLimitRange", "yLimit");
  bindHybridTouchTuneablePair("touchTuneZLimit", "touchTuneZLimitRange", "zLimit");
  bindHybridTouchTuneablePair("touchTuneRLimit", "touchTuneRLimitRange", "rLimit");
  bindHybridTouchTuneablePair("touchTunePLimit", "touchTunePLimitRange", "pLimit");

  const resetBtn = document.getElementById("touchTuneResetBtn");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      Object.assign(hybridTouchTuneables, HYBRID_TOUCH_DEFAULTS);
      [
        ["touchTuneSpeed", "touchTuneSpeedRange", "speed"],
        ["touchTuneAccel", "touchTuneAccelRange", "accel"],
        ["touchTuneXLimit", "touchTuneXLimitRange", "xLimit"],
        ["touchTuneYLimit", "touchTuneYLimitRange", "yLimit"],
        ["touchTuneZLimit", "touchTuneZLimitRange", "zLimit"],
        ["touchTuneRLimit", "touchTuneRLimitRange", "rLimit"],
        ["touchTunePLimit", "touchTunePLimitRange", "pLimit"],
      ].forEach(([numberId, rangeId, key]) => {
        const numberEl = document.getElementById(numberId);
        const rangeEl = document.getElementById(rangeId);
        const value = hybridTouchTuneables[key];
        if (numberEl) {
          numberEl.value = String(value);
        }
        if (rangeEl) {
          rangeEl.value = String(value);
        }
      });
      applyHybridTouchTuneables({ forceDispatch: activeHybridTab === "touch" });
    });
  }
}

function buildHybridCommandPayload() {
  const values = {
    X: Math.round(hybridClampValue("X", hybridPose.X)),
    Y: Math.round(hybridClampValue("Y", hybridPose.Y)),
    Z: Math.round(hybridClampValue("Z", hybridPose.Z)),
    H: Math.round(hybridClampValue("H", hybridPose.H)),
    S: Number(hybridClampValue("S", hybridPose.S).toFixed(2)),
    A: Number(hybridClampValue("A", hybridPose.A).toFixed(2)),
    R: Math.round(hybridClampValue("R", hybridPose.R)),
    P: Math.round(hybridClampValue("P", hybridPose.P)),
  };
  return {
    values,
    command: `X${values.X},Y${values.Y},Z${values.Z},H${values.H},S${values.S},A${values.A},R${values.R},P${values.P}`,
  };
}

function syncHybridHeightControl(value) {
  const sliderEl = document.getElementById("hybridHeightSlider");
  const valueEl = document.getElementById("hybridHeightValue");
  const nextHeight = Number.isFinite(Number(value))
    ? Math.round(hybridClampValue("H", Number(value)))
    : Math.round(hybridClampValue("H", hybridPose.H));

  if (sliderEl) {
    sliderEl.value = String(nextHeight);
  }
  if (valueEl) {
    valueEl.textContent = `H${nextHeight}`;
  }
}

function renderHybridReadout() {
  const readoutEl = document.getElementById("hybridControlReadout");
  if (!readoutEl) {
    syncHybridHeightControl();
    return;
  }
  const payload = buildHybridCommandPayload();
  syncHybridHeightControl(payload.values.H);
  readoutEl.textContent = payload.command;
}

function dispatchHybridCommand(options = {}) {
  const force = !!options.force;
  const minIntervalMs = Math.max(0, Number(options.minIntervalMs) || 0);
  const readoutOnly = !!options.readoutOnly;
  const payload = buildHybridCommandPayload();
  const nowMs = Date.now();

  renderHybridReadout();
  if (readoutOnly) {
    return;
  }
  if (!force && minIntervalMs > 0 && nowMs - hybridLastDispatchMs < minIntervalMs) {
    return;
  }
  if (!force && payload.command === hybridLastCommand) {
    return;
  }
  hybridLastDispatchMs = nowMs;
  hybridLastCommand = payload.command;
  sendCommand(payload.command);
}

function applyHybridDeltas(deltas, options = {}) {
  let changed = false;
  const appliedDeltas = {};
  Object.entries(deltas || {}).forEach(([axis, rawDelta]) => {
    if (!Object.prototype.hasOwnProperty.call(hybridPose, axis)) {
      return;
    }
    const delta = Number(rawDelta);
    if (!Number.isFinite(delta) || delta === 0) {
      return;
    }
    appliedDeltas[axis] = delta;
    const nextValue = hybridClampValue(axis, (Number(hybridPose[axis]) || 0) + delta);
    if (nextValue !== hybridPose[axis]) {
      hybridPose[axis] = nextValue;
      changed = true;
    }
  });

  const source = String(options.source || "unspecified");
  if (Object.keys(appliedDeltas).length) {
    window.dispatchEvent(new CustomEvent("hybrid-touch-deltas", {
      detail: {
        deltas: appliedDeltas,
        pose: { ...hybridPose },
        changed,
        source,
        mode: activeHybridTab,
        timestampMs: Date.now(),
      },
    }));
  }

  if (changed) {
    dispatchHybridCommand({
      force: !!options.force,
      minIntervalMs: options.minIntervalMs,
    });
  } else {
    renderHybridReadout();
  }
}

function setHybridSelectedFeed(cameraId) {
  hybridSelectedFeedId = String(cameraId || "").trim();
  localStorage.setItem(HYBRID_SELECTED_FEED_KEY, hybridSelectedFeedId);
  if (hybridSelectedFeedId) {
    localStorage.setItem("cameraRouterSelectedFeed", hybridSelectedFeedId);
    cameraPreview.targetCameraId = hybridSelectedFeedId;
    const feedSelect = document.getElementById("cameraFeedSelect");
    if (feedSelect && feedSelect.value !== hybridSelectedFeedId) {
      feedSelect.value = hybridSelectedFeedId;
    }
  }
}

function showHybridPreviewPlaceholder(message) {
  const imageEl = document.getElementById("hybridPreviewImage");
  const emptyEl = document.getElementById("hybridPreviewEmpty");
  setHybridPreviewAspect(null, null);
  if (imageEl) {
    imageEl.style.display = "none";
    imageEl.removeAttribute("src");
  }
  if (emptyEl) {
    emptyEl.textContent = String(message || "Select a camera feed.");
    emptyEl.style.display = "flex";
  }
}

function startHybridPreview(cameraId, options = {}) {
  const force = !!options.force;
  const selectedId = String(cameraId || "").trim();
  const imageEl = document.getElementById("hybridPreviewImage");
  const emptyEl = document.getElementById("hybridPreviewEmpty");
  if (!imageEl || !emptyEl) {
    return;
  }

  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    hybridPreviewSourceKey = "";
    showHybridPreviewPlaceholder("Authenticate and select a camera feed.");
    setHybridStatus("Authenticate with camera router to load Hybrid preview.");
    return;
  }
  if (!selectedId) {
    hybridPreviewSourceKey = "";
    showHybridPreviewPlaceholder("Select a feed from the row above.");
    setHybridStatus("Select a feed to start Hybrid preview.");
    return;
  }

  const sourceKey = `${cameraRouterBaseUrl}|${cameraRouterSessionKey}|${selectedId}`;
  if (!force && sourceKey === hybridPreviewSourceKey) {
    return;
  }
  hybridPreviewSourceKey = sourceKey;

  setHybridStatus(`Loading preview for ${selectedId}...`);
  emptyEl.textContent = `Loading ${selectedId}...`;
  emptyEl.style.display = "flex";
  imageEl.style.display = "block";

  imageEl.onload = () => {
    if (hybridPreviewSourceKey !== sourceKey) {
      return;
    }
    setHybridPreviewAspect(imageEl.naturalWidth, imageEl.naturalHeight);
    emptyEl.style.display = "none";
    const resolution = imageEl.naturalWidth && imageEl.naturalHeight
      ? ` ${imageEl.naturalWidth}x${imageEl.naturalHeight}`
      : "";
    setHybridStatus(`Preview active: ${selectedId}${resolution}`);
  };
  imageEl.onerror = () => {
    if (hybridPreviewSourceKey !== sourceKey) {
      return;
    }
    showHybridPreviewPlaceholder(`Preview failed for ${selectedId}.`);
    setHybridStatus(`Preview failed for ${selectedId}`, true);
  };
  imageEl.src = cameraRouterUrl(`/mjpeg/${encodeURIComponent(selectedId)}`, true);
}

function renderHybridFeedOptions() {
  const feedButtonsEl = document.getElementById("hybridFeedButtons");
  if (!feedButtonsEl) {
    return;
  }

  feedButtonsEl.innerHTML = "";

  if (!cameraRouterBaseUrl || !cameraRouterSessionKey) {
    const note = document.createElement("div");
    note.className = "hybrid-feed-note";
    note.textContent = "Authenticate camera router in Streams to load feeds.";
    feedButtonsEl.appendChild(note);
    showHybridPreviewPlaceholder("Authenticate and select a camera feed.");
    return;
  }

  if (!Array.isArray(cameraRouterFeeds) || cameraRouterFeeds.length === 0) {
    const note = document.createElement("div");
    note.className = "hybrid-feed-note";
    note.textContent = "No feeds discovered yet. Use Refresh Feeds.";
    feedButtonsEl.appendChild(note);
    showHybridPreviewPlaceholder("No feeds discovered.");
    setHybridStatus("No feeds discovered. Refresh the list.");
    return;
  }

  const hasStoredSelection = hybridSelectedFeedId && cameraRouterFeeds.some((feed) => feed.id === hybridSelectedFeedId);
  if (!hasStoredSelection) {
    const fallbackId =
      localStorage.getItem("cameraRouterSelectedFeed") ||
      cameraPreview.targetCameraId ||
      cameraRouterFeeds[0].id;
    setHybridSelectedFeed(fallbackId);
  }

  cameraRouterFeeds.forEach((feed) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "hybrid-feed-btn";
    if (!feed.online) {
      btn.classList.add("offline");
    }
    if (feed.id === hybridSelectedFeedId) {
      btn.classList.add("active");
    }
    btn.setAttribute("aria-label", `Camera feed ${feed.label || feed.id}`);
    btn.title = `${feed.id} | fps ${feed.fps} | kbps ${feed.kbps} | clients ${feed.clients}`;

    const iconEl = document.createElement("span");
    iconEl.className = "hybrid-feed-btn-icon";
    iconEl.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="6" width="14" height="12" rx="2"></rect><path d="m17 10 4-2v8l-4-2"></path></svg>';

    const labelEl = document.createElement("span");
    labelEl.className = "hybrid-feed-btn-label";
    labelEl.textContent = String(feed.label || feed.id || "Camera");

    const metaEl = document.createElement("span");
    metaEl.className = "hybrid-feed-btn-meta";
    if (!feed.online) {
      metaEl.textContent = "Offline";
    } else {
      const fps = Math.round(Number(feed.fps) || 0);
      metaEl.textContent = fps > 0 ? `${fps} fps` : "Ready";
    }

    btn.appendChild(iconEl);
    btn.appendChild(labelEl);
    btn.appendChild(metaEl);
    btn.addEventListener("click", () => {
      setHybridSelectedFeed(feed.id);
      renderHybridFeedOptions();
      startHybridPreview(feed.id, { force: true });
    });
    feedButtonsEl.appendChild(btn);
  });

  startHybridPreview(hybridSelectedFeedId, { force: false });
}

function stopHybridArrowHold() {
  if (hybridHoldState.timer) {
    clearInterval(hybridHoldState.timer);
    hybridHoldState.timer = null;
  }
  if (hybridHoldState.button) {
    hybridHoldState.button.classList.remove("active");
  }
  hybridHoldState.axis = "";
  hybridHoldState.delta = 0;
  hybridHoldState.button = null;
}

function startHybridArrowHold(axis, delta, buttonEl) {
  stopHybridArrowHold();
  hybridHoldState.axis = String(axis || "").trim();
  hybridHoldState.delta = Number(delta) || 0;
  hybridHoldState.button = buttonEl || null;
  if (!hybridHoldState.axis || !hybridHoldState.delta) {
    return;
  }
  if (hybridHoldState.button) {
    hybridHoldState.button.classList.add("active");
  }
  applyHybridDeltas({ [hybridHoldState.axis]: hybridHoldState.delta }, {
    force: true,
    source: "arrow-hold",
  });
  hybridHoldState.timer = setInterval(() => {
    applyHybridDeltas({ [hybridHoldState.axis]: hybridHoldState.delta }, {
      force: true,
      source: "arrow-hold",
    });
  }, HYBRID_HOLD_INTERVAL_MS);
}

function onHybridArrowPointerDown(event) {
  if (event.button !== 0) {
    return;
  }
  const buttonEl = event.currentTarget;
  const axis = buttonEl.dataset.axis || "";
  const delta = Number(buttonEl.dataset.delta) || 0;
  if (!axis || !delta) {
    return;
  }
  event.preventDefault();
  startHybridArrowHold(axis, delta, buttonEl);
  if (typeof buttonEl.setPointerCapture === "function") {
    try {
      buttonEl.setPointerCapture(event.pointerId);
    } catch (err) {}
  }
}

function onHybridArrowPointerStop(event) {
  event.preventDefault();
  stopHybridArrowHold();
}

function hybridDragMode(event, dragButton = 0) {
  if (dragButton === 2) {
    return "translate";
  }
  if (dragButton === 1) {
    return "roll_height";
  }
  if (event.shiftKey) {
    return "roll_height";
  }
  if (event.ctrlKey || event.metaKey) {
    return "translate";
  }
  return "yaw_pitch";
}

function buildHybridDragPointerPayload(type, event = null) {
  const dragSurface = document.getElementById("hybridDragSurface");
  if (!dragSurface) {
    return null;
  }
  const rect = dragSurface.getBoundingClientRect();
  if (!rect || rect.width <= 0 || rect.height <= 0) {
    return null;
  }
  const clientX = Number(event && Number.isFinite(event.clientX) ? event.clientX : hybridDragState.lastX);
  const clientY = Number(event && Number.isFinite(event.clientY) ? event.clientY : hybridDragState.lastY);
  if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) {
    return null;
  }

  const normalizedX = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
  const normalizedY = Math.min(1, Math.max(0, (clientY - rect.top) / rect.height));
  const pointerId = event && Number.isFinite(Number(event.pointerId))
    ? Number(event.pointerId)
    : hybridDragState.pointerId;
  return {
    type: String(type || "move"),
    pointerId,
    pointerType: event && event.pointerType ? String(event.pointerType) : "",
    clientX,
    clientY,
    normalizedX,
    normalizedY,
    ndcX: (normalizedX * 2) - 1,
    ndcY: 1 - (normalizedY * 2),
    active: type === "start" || type === "move",
    mode: activeHybridTab,
    timestampMs: Date.now(),
  };
}

function dispatchHybridDragPointer(type, event = null) {
  const payload = buildHybridDragPointerPayload(type, event);
  if (!payload) {
    return;
  }
  window.dispatchEvent(new CustomEvent("hybrid-touch-pointer", {
    detail: payload,
  }));
}

function endHybridDragSession(reason = "end", event = null) {
  if (!hybridDragState.active) {
    return;
  }
  dispatchHybridDragPointer(reason, event);
  hybridDragState.active = false;
  hybridDragState.pointerId = null;
  hybridDragState.button = 0;
  const dragSurface = document.getElementById("hybridDragSurface");
  if (dragSurface) {
    dragSurface.classList.remove("dragging");
  }
}

function onHybridDragPointerDown(event) {
  if (event.button !== 0 && event.button !== 1 && event.button !== 2) {
    return;
  }
  hybridDragState.active = true;
  hybridDragState.pointerId = event.pointerId;
  hybridDragState.lastX = event.clientX;
  hybridDragState.lastY = event.clientY;
  hybridDragState.button = event.button;
  const dragSurface = event.currentTarget;
  dragSurface.classList.add("dragging");
  if (typeof dragSurface.setPointerCapture === "function") {
    try {
      dragSurface.setPointerCapture(event.pointerId);
    } catch (err) {}
  }
  dispatchHybridDragPointer("start", event);
  event.preventDefault();
}

function onHybridDragPointerMove(event) {
  if (!hybridDragState.active || event.pointerId !== hybridDragState.pointerId) {
    return;
  }

  const dx = event.clientX - hybridDragState.lastX;
  const dy = event.clientY - hybridDragState.lastY;
  hybridDragState.lastX = event.clientX;
  hybridDragState.lastY = event.clientY;
  if (!dx && !dy) {
    return;
  }

  const mode = hybridDragMode(event, hybridDragState.button);
  dispatchHybridDragPointer("move", event);
  const deltas = {};
  if (mode === "translate") {
    const lateralGain = hybridDragState.button === 2 ? -2.0 : 2.0;
    const dorsalGain = hybridDragState.button === 2 ? 2.0 : -2.0;
    deltas.Y = dx * lateralGain;
    deltas.Z = dy * dorsalGain;
  } else if (mode === "roll_height") {
    const rollGain = hybridDragState.button === 1 ? 2.4 : -2.4;
    deltas.R = dx * rollGain;
    deltas.H = -dy * 0.2;
  } else {
    deltas.X = dx * 2.4;
    deltas.P = dy * 2.4;
  }

  applyHybridDeltas(deltas, {
    force: false,
    minIntervalMs: HYBRID_COMMAND_INTERVAL_MS,
    source: mode === "translate"
      ? "drag-translate"
      : (mode === "roll_height" ? "drag-roll-height" : "drag-yaw-pitch"),
  });
  event.preventDefault();
}

function onHybridDragPointerStop(event) {
  if (!hybridDragState.active || event.pointerId !== hybridDragState.pointerId) {
    return;
  }
  event.preventDefault();
  const reason = event.type === "pointercancel" ? "cancel" : "end";
  endHybridDragSession(reason, event);
}

function onHybridDragWheel(event) {
  event.preventDefault();
  const direction = event.deltaY < 0 ? 1 : -1;
  const step = event.shiftKey ? 4 : 2;
  applyHybridDeltas({ H: direction * step }, {
    force: true,
    minIntervalMs: 25,
    source: "wheel-height",
  });
}

function setupHybridUi() {
  if (hybridUiInitialized) {
    return;
  }
  hybridUiInitialized = true;

  setupHybridTouchTuneables();
  hybridSyncPoseFromHeadControls();
  renderHybridReadout();

  const refreshBtn = document.getElementById("hybridRefreshFeedsBtn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      refreshCameraFeeds().catch(() => {});
    });
  }

  const homeSoftBtn = document.getElementById("hybridHomeSoftBtn");
  if (homeSoftBtn) {
    homeSoftBtn.addEventListener("click", () => {
      sendHomeSoftCommand();
    });
  }

  const heightSlider = document.getElementById("hybridHeightSlider");
  if (heightSlider) {
    const onHeightSlide = () => {
      const nextHeight = hybridClampValue("H", Number(heightSlider.value));
      if (nextHeight !== hybridPose.H) {
        hybridPose.H = nextHeight;
        dispatchHybridCommand({
          force: false,
          minIntervalMs: 20,
        });
      } else {
        renderHybridReadout();
      }
    };
    heightSlider.addEventListener("input", onHeightSlide);
    heightSlider.addEventListener("change", onHeightSlide);
  }

  const dragSurface = document.getElementById("hybridDragSurface");
  if (dragSurface) {
    dragSurface.addEventListener("pointerdown", onHybridDragPointerDown);
    dragSurface.addEventListener("pointermove", onHybridDragPointerMove);
    dragSurface.addEventListener("pointerup", onHybridDragPointerStop);
    dragSurface.addEventListener("pointercancel", onHybridDragPointerStop);
    dragSurface.addEventListener("pointerleave", onHybridDragPointerStop);
    dragSurface.addEventListener("wheel", onHybridDragWheel, { passive: false });
    dragSurface.addEventListener("contextmenu", (event) => event.preventDefault());
  }

  document.querySelectorAll(".hybrid-arrow").forEach((buttonEl) => {
    buttonEl.addEventListener("pointerdown", onHybridArrowPointerDown);
    buttonEl.addEventListener("pointerup", onHybridArrowPointerStop);
    buttonEl.addEventListener("pointercancel", onHybridArrowPointerStop);
    buttonEl.addEventListener("pointerleave", onHybridArrowPointerStop);
  });

  window.addEventListener("pointerup", stopHybridArrowHold);
  window.addEventListener("blur", () => {
    stopHybridArrowHold();
    endHybridDragSession("cancel");
  });

  syncHybridHeightControl();
  renderHybridFeedOptions();
}

function getNumberValue(id, fallback = 0) {
    const el = document.getElementById(id);
    if (!el) {
        return fallback;
    }
    const value = parseFloat(el.value);
    return Number.isFinite(value) ? value : fallback;
}

function setInputValue(id, value) {
    const el = document.getElementById(id);
    if (!el) {
        return;
    }
    el.value = value;
}

function updateDirect() {
    const cmdParts = [];
    for (let i = 1; i <= 6; i++) {
        const val = Math.round(getNumberValue(`motor${i}`, 0));
        setInputValue(`slider${i}`, val);
        cmdParts.push(`${i}:${val}`);
    }
    const cmd = cmdParts.join(",");
    const current = document.getElementById("directCurrentCmd");
    if (current) {
        current.textContent = cmd;
    }
    sendCommand(cmd);
}

function incMotor(id) {
    const el = document.getElementById(`motor${id}`);
    if (!el) {
        return;
    }
    el.value = Math.round(getNumberValue(`motor${id}`, 0) + 1);
    updateDirect();
}

function decMotor(id) {
    const el = document.getElementById(`motor${id}`);
    if (!el) {
        return;
    }
    el.value = Math.round(getNumberValue(`motor${id}`, 0) - 1);
    updateDirect();
}

function updateEulerView() {
    const yaw = Math.round(getNumberValue("yaw", 0));
    const rollAsY = Math.round(getNumberValue("roll", 0));
    const pitchAsZ = Math.round(getNumberValue("pitch", 0));
    const height = Math.round(getNumberValue("height", 0));

    setInputValue("yawSlider", yaw);
    setInputValue("rollSlider", rollAsY);
    setInputValue("pitchSlider", pitchAsZ);
    setInputValue("heightSlider", height);

    const cmd = `X${yaw},Y${rollAsY},Z${pitchAsZ},H${height}`;
    const current = document.getElementById("eulerCurrentCmd");
    if (current) {
        current.textContent = cmd;
    }
    sendCommand(cmd);
}

function incEulerField(field) {
    setInputValue(field, Math.round(getNumberValue(field, 0) + 1));
    updateEulerView();
}

function decEulerField(field) {
    setInputValue(field, Math.round(getNumberValue(field, 0) - 1));
    updateEulerView();
}

function updateHeadView() {
    const values = {
        X: Math.round(getNumberValue("X", 0)),
        Y: Math.round(getNumberValue("Y", 0)),
        Z: Math.round(getNumberValue("Z", 0)),
        H: Math.round(getNumberValue("H", 0)),
        S: getNumberValue("S", 0.8),
        A: getNumberValue("A", 0.8),
        R: Math.round(getNumberValue("R", 0)),
        P: Math.round(getNumberValue("P", 0)),
    };

    Object.entries({
        XSlider: values.X,
        YSlider: values.Y,
        ZSlider: values.Z,
        HSlider: values.H,
        SSlider: values.S,
        ASlider: values.A,
        RSlider: values.R,
        PSlider: values.P,
    }).forEach(([id, value]) => setInputValue(id, value));

    const cmd = `X${values.X},Y${values.Y},Z${values.Z},H${values.H},S${values.S},A${values.A},R${values.R},P${values.P}`;
    const current = document.getElementById("headCurrentCmd");
    if (current) {
        current.textContent = cmd;
    }
    sendCommand(cmd);
}

function incHeadField(field, step) {
    const current = getNumberValue(field, field === "S" || field === "A" ? 0.8 : 0);
    const next = current + step;
    setInputValue(field, field === "S" || field === "A" ? next.toFixed(1) : Math.round(next));
    updateHeadView();
}

function decHeadField(field, step) {
    const current = getNumberValue(field, field === "S" || field === "A" ? 0.8 : 0);
    const next = current - step;
    setInputValue(field, field === "S" || field === "A" ? next.toFixed(1) : Math.round(next));
    updateHeadView();
}

function updateQuatView() {
    const values = {
        w: getNumberValue("w", 1),
        x: getNumberValue("x", 0),
        y: getNumberValue("y", 0),
        z: getNumberValue("z", 0),
        H: Math.round(getNumberValue("qH", 0)),
        S: getNumberValue("qS", 1),
        A: getNumberValue("qA", 1),
    };

    Object.entries({
        wSlider: values.w,
        xSlider: values.x,
        ySlider: values.y,
        zSlider: values.z,
        qHSlider: values.H,
        qSSlider: values.S,
        qASlider: values.A,
    }).forEach(([id, value]) => setInputValue(id, value));

    let cmd = `Q:${values.w},${values.x},${values.y},${values.z},H${values.H}`;
    if (document.getElementById("qS")) {
        cmd += `,S${values.S}`;
    }
    if (document.getElementById("qA")) {
        cmd += `,A${values.A}`;
    }

    const current = document.getElementById("quatCurrentCmd");
    if (current) {
        current.textContent = cmd;
    }
    sendCommand(cmd);
}

function incQuatField(field, step) {
    const fallback = field === "w" || field === "qS" || field === "qA" ? 1 : 0;
    const next = getNumberValue(field, fallback) + step;
    if (field === "qH") {
        setInputValue(field, Math.round(next));
    } else {
        setInputValue(field, next.toFixed(2));
    }
    updateQuatView();
}

function decQuatField(field, step) {
    const fallback = field === "w" || field === "qS" || field === "qA" ? 1 : 0;
    const next = getNumberValue(field, fallback) - step;
    if (field === "qH") {
        setInputValue(field, Math.round(next));
    } else {
        setInputValue(field, next.toFixed(2));
    }
    updateQuatView();
}

async function ensureHybridAutoReady(initialRoute) {
  if (initialRoute !== "hybrid") {
    return;
  }

  const adapterUseNkn = getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN;
  const adapterEndpoint = adapterUseNkn
    ? nknEndpointForAddress(resolveServiceNknTarget("adapter"))
    : HTTP_URL;
  if (!authenticated && adapterEndpoint && PASSWORD) {
    try {
      const adapterReady = await authenticate(PASSWORD, WS_URL, adapterEndpoint);
      if (adapterReady) {
        if (!adapterUseNkn && WS_URL) {
          initWebSocket();
        }
        hideConnectionModal();
      }
    } catch (err) {}
  }

  if (!cameraRouterBaseUrl) {
    return;
  }
  if (!cameraRouterSessionKey && !cameraRouterPassword) {
    return;
  }

  try {
    if (!cameraRouterSessionKey) {
      await authenticateCameraRouter();
    } else {
      await refreshCameraFeeds({ silent: true, suppressErrors: true });
      await refreshCameraImu({ silent: true, force: true });
    }
  } catch (err) {}

  if (!cameraRouterSessionKey) {
    return;
  }

  if (!Array.isArray(cameraRouterFeeds) || cameraRouterFeeds.length === 0) {
    try {
      await refreshCameraFeeds({ silent: true, suppressErrors: true });
    } catch (err) {}
  }

  if (!hybridSelectedFeedId) {
    const fallbackFeed =
      localStorage.getItem("cameraRouterSelectedFeed") ||
      cameraPreview.targetCameraId ||
      (cameraRouterFeeds[0] ? cameraRouterFeeds[0].id : "");
    if (fallbackFeed) {
      setHybridSelectedFeed(fallbackFeed);
    }
  }

  renderHybridFeedOptions();
  if (hybridSelectedFeedId) {
    startHybridPreview(hybridSelectedFeedId, { force: true });
    setHybridStatus(`Hybrid auto-ready on ${hybridSelectedFeedId}`);
  }
}

window.addEventListener('load', async () => {
  reorganizeUnifiedViews();
  initializeSidebarUi();
  initializeActionIcons();
  initializeCheckboxToggleButtons();
  initializeControlsNav();
  initializeRouting();
  const initialRoute = getRouteFromLocation();
  const queryConnection = parseConnectionFromQuery();
  ensureEndpointInputBindings();
  syncAdapterConnectionInputs({ preserveUserInput: true });
  initNknRouterUi();
  setupStreamConfigUi();
  setupHybridUi();
  initializeDebugAudioActions();
  initializeDebugCameraActions();
  updateMetrics();

  if (queryConnection.adapterConfigured && queryConnection.passwordProvided) {
    logToConsole("[CONNECT] Adapter and password found in query; attempting auto-connect...");
    const autoConnected = await authenticate(
      PASSWORD,
      WS_URL,
      getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN
        ? nknEndpointForAddress(resolveServiceNknTarget("adapter"))
        : HTTP_URL
    );
    if (autoConnected) {
      if (getServiceTransportMode("adapter") !== SERVICE_TRANSPORT_NKN && WS_URL && routeAllowsWebSocket(initialRoute)) {
        initWebSocket();
      }
      await ensureHybridAutoReady(initialRoute);
      return;
    }
    showConnectionModal();
    await ensureHybridAutoReady(initialRoute);
    return;
  }

  const hasAdapterEndpoint = getServiceTransportMode("adapter") === SERVICE_TRANSPORT_NKN
    ? !!resolveServiceNknTarget("adapter")
    : !!HTTP_URL;
  if (SESSION_KEY && hasAdapterEndpoint) {
    logToConsole("[SESSION] Found saved session, attempting to reconnect...");
    metrics.connected = true;
    authenticated = true;
    updateMetrics();

    if (getServiceTransportMode("adapter") !== SERVICE_TRANSPORT_NKN && WS_URL && routeAllowsWebSocket(initialRoute)) {
      initWebSocket();
    }
  } else if (queryConnection.adapterConfigured) {
    logToConsole("[CONNECT] Adapter URL configured, please authenticate...");
    showConnectionModal();
  } else {
    logToConsole("[CONNECT] Configure service credentials on the Auth page.");
  }

  await ensureHybridAutoReady(initialRoute);
});

window.sendCommand = sendCommand;
window.sendCommandHttpOnly = sendCommandHttpOnly;
window.sendHomeCommand = sendHomeCommand;
window.sendHomeSoftCommand = sendHomeSoftCommand;
window.resetAdapterPort = resetAdapterPort;
window.showConnectionModal = showConnectionModal;
window.hideConnectionModal = hideConnectionModal;
window.fetchTunnelUrl = fetchTunnelUrl;
window.connectToAdapter = connectToAdapter;
window.updateDirect = updateDirect;
window.incMotor = incMotor;
window.decMotor = decMotor;
window.updateEulerView = updateEulerView;
window.incEulerField = incEulerField;
window.decEulerField = decEulerField;
window.updateHeadView = updateHeadView;
window.incHeadField = incHeadField;
window.decHeadField = decHeadField;
window.updateQuatView = updateQuatView;
window.incQuatField = incQuatField;
window.decQuatField = decQuatField;
window.setRoute = setRoute;
window.authenticateCameraRouter = authenticateCameraRouter;
window.refreshCameraFeeds = refreshCameraFeeds;
window.cycleCameraAccessRecovery = cycleCameraAccessRecovery;
window.startCameraPreview = startCameraPreview;
window.stopCameraPreview = stopCameraPreview;
window.isControlTransportReady = isControlTransportReady;
