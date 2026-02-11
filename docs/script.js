// Define PI if you need quaternion math.
const PI = Math.PI;

// Defaults injected from backend config
const SERVER_DEFAULT_WS_URL = "ws://127.0.0.1:5001/ws";
const SERVER_DEFAULT_HTTP_URL = "http://127.0.0.1:5001/send_command";

// Connection state - no auto-fill, only use saved or query params
let WS_URL = localStorage.getItem('wsUrl') || "";
let HTTP_URL = localStorage.getItem('httpUrl') || "";
if (WS_URL === SERVER_DEFAULT_WS_URL) {
    WS_URL = "";
}
if (HTTP_URL === SERVER_DEFAULT_HTTP_URL) {
    HTTP_URL = "";
}
let SESSION_KEY = localStorage.getItem('sessionKey') || "";
let PASSWORD = localStorage.getItem('password') || "";
let socket = null;
let useWS = false;
let authenticated = false;
let suppressCommandDispatch = false;
const ROUTES = new Set(["connect", "home", "direct", "euler", "head", "quaternion", "headstream", "orientation", "streams"]);
let headstreamInitTriggered = false;
let orientationInitTriggered = false;

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

    const requiresPersistentClient = cameraPreview.activeMode !== STREAM_MODE_JPEG;
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
            statusEl.textContent = useWS ? 'WebSocket' : 'HTTP';
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
    'X': 0,'Y': 0,'Z': 0,'H': 0,'S': 1,'A': 1,'R': 0,'P': 0,
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
    } finally {
        suppressCommandDispatch = previousSuppress;
    }
}

function getRouteFromLocation() {
    const hashRoute = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
    if (ROUTES.has(hashRoute)) {
        return hashRoute;
    }
    const pathRoute = window.location.pathname.split("/").filter(Boolean).pop();
    if (pathRoute && ROUTES.has(pathRoute.toLowerCase())) {
        return pathRoute.toLowerCase();
    }
    return "connect";
}

function applyRoute(route) {
    document.querySelectorAll("[data-view]").forEach((view) => {
        view.classList.toggle("active", view.dataset.view === route);
    });
    document.querySelectorAll(".nav-link[data-route]").forEach((link) => {
        link.classList.toggle("active", link.dataset.route === route);
    });

    if (route === "headstream" && !headstreamInitTriggered && typeof window.initHeadstreamApp === "function") {
        window.initHeadstreamApp();
        headstreamInitTriggered = true;
    }

    if (route === "orientation" && !orientationInitTriggered && typeof window.initOrientationApp === "function") {
        window.initOrientationApp();
        orientationInitTriggered = true;
    }

    if (route === "streams") {
        setupStreamConfigUi();
        hideConnectionModal();
    }
    if (route === "orientation") {
        disableWebSocketMode();
        hideConnectionModal();
    } else if (
        routeAllowsWebSocket(route) &&
        WS_URL &&
        SESSION_KEY &&
        authenticated &&
        (!socket || socket.disconnected)
    ) {
        initWebSocket();
    }
}

function setRoute(route, updateHash = true) {
    const normalized = ROUTES.has(route) ? route : "connect";
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

async function resetAdapterPort(triggerHome = false, homeCommand = "HOME") {
    if (!SESSION_KEY) {
        logToConsole("[ERROR] No session key - please authenticate first");
        showConnectionModal();
        return;
    }

    const adapterOrigin = getAdapterOrigin();
    if (!adapterOrigin) {
        logToConsole("[ERROR] Cannot reset port: invalid adapter HTTP URL");
        showConnectionModal();
        return;
    }

    logToConsole("[RESET] Resetting adapter serial port...");
    try {
        const response = await fetch(`${adapterOrigin}/serial_reset`, {
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
    try {
        let authUrl;
        try {
            const parsedHttpUrl = new URL(httpUrl.includes("://") ? httpUrl : `https://${httpUrl}`);
            authUrl = `${parsedHttpUrl.origin}/auth`;
        } catch (urlErr) {
            logToConsole("[ERROR] Invalid HTTP URL: " + httpUrl);
            return false;
        }
        const startTime = Date.now();
        const response = await fetch(authUrl, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: password})
        });
        const data = await response.json();

        if (data.status === 'success') {
            SESSION_KEY = data.session_key;
            PASSWORD = password;
            localStorage.setItem('sessionKey', SESSION_KEY);
            localStorage.setItem('password', password);
            localStorage.setItem('wsUrl', wsUrl);
            localStorage.setItem('httpUrl', httpUrl);

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
function sendCommand(command) {
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

    if (useWS && socket && socket.connected) {
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
        // If WS not yet open (or closed), do HTTP POST
        if (!HTTP_URL) {
            logToConsole("[ERROR] No HTTP URL configured");
            showConnectionModal();
            return;
        }

        fetch(HTTP_URL, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({command: command, session_key: SESSION_KEY})
        })
        .then(r => {
            metrics.latency = Date.now() - startTime;
            return r.json();
        })
        .then(data => {
            logToConsole("HTTP -> " + command);
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
        })
        .catch(err => {
            logToConsole("[ERROR] Fetch error: " + err);
            metrics.connected = false;
            updateMetrics();
        });
    }
}



// Initialize the Socket.IO connection.
function initWebSocket() {
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
function showConnectionModal() {
  const modal = document.getElementById('connectionModal');
  if (modal) {
    modal.classList.add('active');
    ensureEndpointInputBindings();
    initNknRouterUi();
    // Pre-fill only saved values, no defaults
    const passInput = document.getElementById('passwordInput');
    const wsInput = document.getElementById('wsUrlInput');
    const httpInput = document.getElementById('httpUrlInput');
    const routerTargetInput = document.getElementById("routerNknAddressInput");

    if (passInput) passInput.value = PASSWORD || '';
    if (wsInput) wsInput.value = WS_URL || '';
    if (httpInput) httpInput.value = HTTP_URL || '';
    if (routerTargetInput) routerTargetInput.value = routerTargetNknAddress || '';
    ensureBrowserNknIdentity();
    hydrateEndpointInputs("http");
  }
}

// HTTP-only path used by local orientation streaming.
function sendCommandHttpOnly(command) {
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

    if (!HTTP_URL) {
        logToConsole("[ERROR] No HTTP URL configured");
        showConnectionModal();
        return;
    }

    fetch(HTTP_URL, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({command: command, session_key: SESSION_KEY})
    })
    .then(r => {
        metrics.latency = Date.now() - startTime;
        return r.json();
    })
    .then(data => {
        logToConsole("HTTP -> " + command);
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
    })
    .catch(err => {
        logToConsole("[ERROR] Fetch error: " + err);
        metrics.connected = false;
        updateMetrics();
    });
}

function hideConnectionModal() {
  const modal = document.getElementById('connectionModal');
  if (modal) {
    modal.classList.remove('active');
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
  return { httpUrl, wsUrl, origin: baseOrigin };
}

function hydrateEndpointInputs(prefer = "http") {
  const httpInput = document.getElementById("httpUrlInput");
  const wsInput = document.getElementById("wsUrlInput");
  if (!httpInput || !wsInput) {
    return null;
  }

  const httpRaw = httpInput.value.trim();
  const wsRaw = wsInput.value.trim();
  const source = prefer === "ws" ? (wsRaw || httpRaw) : (httpRaw || wsRaw);
  const endpoints = buildAdapterEndpoints(source);
  if (!endpoints) {
    return null;
  }

  httpInput.value = endpoints.httpUrl;
  wsInput.value = endpoints.wsUrl;
  return endpoints;
}

function ensureEndpointInputBindings() {
  if (endpointInputBindingsInstalled) {
    return;
  }

  const httpInput = document.getElementById("httpUrlInput");
  const wsInput = document.getElementById("wsUrlInput");
  if (!httpInput || !wsInput) {
    return;
  }

  endpointInputBindingsInstalled = true;

  const hydrateFromHttp = () => {
    if (httpInput.value.trim()) {
      hydrateEndpointInputs("http");
    }
  };
  const hydrateFromWs = () => {
    if (wsInput.value.trim()) {
      hydrateEndpointInputs("ws");
    }
  };
  const scheduleHydrate = (prefer) => {
    if (endpointHydrateTimer) {
      clearTimeout(endpointHydrateTimer);
    }
    endpointHydrateTimer = setTimeout(() => hydrateEndpointInputs(prefer), 120);
  };

  httpInput.addEventListener("input", () => scheduleHydrate("http"));
  httpInput.addEventListener("blur", hydrateFromHttp);
  httpInput.addEventListener("change", hydrateFromHttp);
  httpInput.addEventListener("paste", () => setTimeout(hydrateFromHttp, 0));

  wsInput.addEventListener("input", () => scheduleHydrate("ws"));
  wsInput.addEventListener("blur", hydrateFromWs);
  wsInput.addEventListener("change", hydrateFromWs);
  wsInput.addEventListener("paste", () => setTimeout(hydrateFromWs, 0));
}

// Fill HTTP/WS inputs from a provided adapter/tunnel URL.
function fetchTunnelUrl() {
  const endpoints = hydrateEndpointInputs("http");
  if (!endpoints) {
    alert("Enter a valid adapter URL first (for example https://example.trycloudflare.com).");
    return;
  }

  logToConsole("Adapter endpoints filled from: " + endpoints.origin);
}

// Handle connection form submission
async function connectToAdapter() {
  const password = document.getElementById('passwordInput').value.trim();
  const httpInputEl = document.getElementById('httpUrlInput');
  const wsInputEl = document.getElementById('wsUrlInput');
  const httpInputRaw = httpInputEl ? httpInputEl.value.trim() : "";
  const wsInputRaw = wsInputEl ? wsInputEl.value.trim() : "";

  if (!password || (!httpInputRaw && !wsInputRaw)) {
    alert("Please enter password and adapter URL");
    return;
  }

  const normalized = hydrateEndpointInputs("http");
  if (!normalized) {
    alert("Please enter a valid adapter URL");
    return;
  }

  const httpUrl = normalized.httpUrl;
  const wsUrl = normalized.wsUrl;

  logToConsole("[CONNECT] Connecting to adapter...");

  // Authenticate first
  const success = await authenticate(password, wsUrl, httpUrl);
  if (success) {
    WS_URL = wsUrl;
    HTTP_URL = httpUrl;
    const activeRoute = getRouteFromLocation();

    // Try WebSocket if URL provided
    if (wsUrl && routeAllowsWebSocket(activeRoute)) {
      initWebSocket();
    } else {
      disableWebSocketMode();
      hideConnectionModal();
    }
  }
}

// Parse query parameters for adapter URL
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
  const adapterParam = getFirstParam(["adapter", "server"]);
  const passwordParam = getFirstParam(["password", "pass"]);
  const routerNknParam = getFirstParam(["router_nkn", "nkn_router", "router_nkn_address"]);

  let adapterConfigured = false;
  let passwordProvided = false;

  if (adapterParam) {
    const endpoints = buildAdapterEndpoints(adapterParam);
    if (endpoints) {
      localStorage.setItem('httpUrl', endpoints.httpUrl);
      localStorage.setItem('wsUrl', endpoints.wsUrl);
      HTTP_URL = endpoints.httpUrl;
      WS_URL = endpoints.wsUrl;
      adapterConfigured = true;
      logToConsole(`[OK] Adapter configured from URL: ${endpoints.origin}`);
    } else {
      console.error('Invalid adapter URL in query parameter:', adapterParam);
      logToConsole('[WARN] Invalid adapter URL in query parameter');
    }
  }

  if (passwordParam) {
    PASSWORD = passwordParam;
    localStorage.setItem('password', PASSWORD);
    passwordProvided = true;
  }

  if (routerNknParam) {
    routerTargetNknAddress = routerNknParam;
    localStorage.setItem("routerTargetNknAddress", routerTargetNknAddress);
  }

  return { adapterConfigured, passwordProvided };
}

let routerTargetNknAddress = localStorage.getItem("routerTargetNknAddress") || "";
let browserNknSeedHex = localStorage.getItem("browserNknSeedHex") || "";
let browserNknPubHex = localStorage.getItem("browserNknPubHex") || "";
const ROUTER_NKN_IDENTIFIER = "web";
const ROUTER_NKN_SUBCLIENTS = 4;
const ROUTER_NKN_READY_TIMEOUT_MS = 16000;
const ROUTER_NKN_RESOLVE_TIMEOUT_MS = 14000;
const ROUTER_NKN_AUTO_RESOLVE_INTERVAL_MS = 45000;

let nknUiInitialized = false;
let browserNknClient = null;
let browserNknClientReady = false;
let browserNknClientAddress = "";
let nknClientInitPromise = null;
let nknResolveInFlight = false;
let routerAutoResolveTimer = null;
const pendingNknResolveRequests = new Map();

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

function extractResolvedFromPayload(data) {
  return (
    data.resolved ||
    (((data.snapshot || {}).resolved) || {}) ||
    ((((data.reply || {}).snapshot || {}).resolved) || {})
  );
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

function clearPendingNknResolveRequests(reason) {
  for (const [requestId, pending] of pendingNknResolveRequests.entries()) {
    clearTimeout(pending.timeoutHandle);
    if (typeof pending.reject === "function") {
      pending.reject(new Error(reason || `Resolve ${requestId} cancelled`));
    }
  }
  pendingNknResolveRequests.clear();
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
  }
}

function attachBrowserNknClientHandlers(client) {
  const onReady = () => {
    browserNknClientReady = true;
    browserNknClientAddress = String(client.addr || "").trim();
    const statusBits = ["Browser NKN client ready"];
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
}

async function ensureBrowserNknClient(options = {}) {
  const forceReconnect = !!options.forceReconnect;
  if (forceReconnect) {
    closeBrowserNknClient();
  }
  if (browserNknClient) {
    return browserNknClient;
  }
  if (nknClientInitPromise) {
    return nknClientInitPromise;
  }

  nknClientInitPromise = (async () => {
    ensureBrowserNknIdentity();

    if (typeof nkn === "undefined" || !nkn || typeof nkn.MultiClient !== "function") {
      throw new Error("nkn-sdk browser library not loaded");
    }
    if (!/^[0-9a-f]{64}$/i.test(browserNknSeedHex)) {
      throw new Error("Browser seed is invalid");
    }

    browserNknClientReady = false;
    browserNknClientAddress = "";
    setRouterResolveStatus("Starting browser NKN client...");
    setBrowserNknSeedStatus("Connecting to NKN...");
    const client = new nkn.MultiClient({
      seed: browserNknSeedHex,
      identifier: ROUTER_NKN_IDENTIFIER,
      numSubClients: ROUTER_NKN_SUBCLIENTS,
    });
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
    if (browserNknClientReady) {
      return true;
    }
    await sleepMs(120);
  }
  return browserNknClientReady;
}

function applyResolvedEndpoints(resolved) {
  if (!resolved || typeof resolved !== "object") {
    return false;
  }

  let changed = false;
  const adapter = resolved.adapter || {};
  const camera = resolved.camera || {};

  const httpCandidate = (adapter.http_endpoint || "").trim();
  const wsCandidate = (adapter.ws_endpoint || "").trim();
  if (httpCandidate) {
    HTTP_URL = httpCandidate;
    localStorage.setItem("httpUrl", HTTP_URL);
    const httpInput = document.getElementById("httpUrlInput");
    if (httpInput) {
      httpInput.value = HTTP_URL;
    }
    changed = true;
  }
  if (wsCandidate) {
    WS_URL = wsCandidate;
    localStorage.setItem("wsUrl", WS_URL);
    const wsInput = document.getElementById("wsUrlInput");
    if (wsInput) {
      wsInput.value = WS_URL;
    }
    changed = true;
  }
  if (httpCandidate || wsCandidate) {
    hydrateEndpointInputs("http");
  }

  const cameraCandidate = (camera.tunnel_url || camera.base_url || "").trim();
  if (cameraCandidate) {
    try {
      cameraRouterBaseUrl = normalizeOrigin(cameraCandidate);
      localStorage.setItem("cameraRouterBaseUrl", cameraRouterBaseUrl);
      const camInput = document.getElementById("cameraRouterBaseInput");
      if (camInput) {
        camInput.value = cameraRouterBaseUrl;
      }
      if (typeof syncPinnedPreviewSource === "function") {
        syncPinnedPreviewSource();
      }
      changed = true;
    } catch (err) {}
  }

  return changed;
}

async function requestResolvedEndpointsViaNkn(targetAddress, timeoutMs = ROUTER_NKN_RESOLVE_TIMEOUT_MS) {
  const client = await ensureBrowserNknClient();
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
      await client.send(String(targetAddress || "").trim(), JSON.stringify(payload), { noReply: true });
    } catch (err) {
      clearTimeout(timeoutHandle);
      pendingNknResolveRequests.delete(requestId);
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
    await ensureBrowserNknClient({ forceReconnect });
    const ready = await waitForBrowserNknReady();
    if (!ready) {
      if (!quiet) {
        setRouterResolveStatus("Browser NKN client starting...");
      }
      return false;
    }

    const statusBits = ["Browser NKN client ready"];
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

  ensureBrowserNknIdentity();
  startRouterAutoResolveTimer();
  refreshNknClientInfo({ resolveNow: true }).catch(() => {});
}

const CAMERA_ROUTER_DEFAULT_BASE = localStorage.getItem("cameraRouterBaseUrl") || "";
const STREAM_MODE_MJPEG = "mjpeg";
const STREAM_MODE_JPEG = "jpeg";
const PINNED_PREVIEW_STORAGE_KEY = "cameraPinnedPreviewStateV1";
const CAMERA_FEED_POLL_INTERVAL_MS = 1500;
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
const cameraPreview = {
  jpegTimer: null,
  peerConnection: null,
  activeCameraId: "",
  targetCameraId: localStorage.getItem("cameraRouterSelectedFeed") || "",
  activeMode: STREAM_MODE_MJPEG,
  desired: false,
  restartTimer: null,
  restartAttempts: 0,
  monitorTimer: null,
  healthFailStreak: 0,
  zeroClientStreak: 0,
  monitorInFlight: false,
};
let cameraFeedPollTimer = null;
let cameraFeedRefreshInFlight = false;
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
  if (!cameraPreview.desired || !cameraRouterBaseUrl || !cameraRouterSessionKey) {
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
    const requiresPersistentClient = cameraPreview.activeMode !== STREAM_MODE_JPEG;
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
    mjpegImg.style.display = mode === "mjpeg" || mode === "jpeg" ? "block" : "none";
  }
  if (videoEl) {
    videoEl.style.display = mode === "webrtc" || mode === "mpegts" ? "block" : "none";
  }
}

async function cameraRouterFetch(path, options = {}, includeSession = true) {
  if (!cameraRouterBaseUrl) {
    throw new Error("Camera Router URL is not configured");
  }
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

async function authenticateCameraRouter() {
  const baseInput = document.getElementById("cameraRouterBaseInput");
  const passInput = document.getElementById("cameraRouterPasswordInput");
  if (!baseInput || !passInput) {
    return;
  }

  try {
    cameraRouterBaseUrl = normalizeOrigin(baseInput.value);
  } catch (err) {
    setStreamStatus(`Invalid camera router URL: ${err}`, true);
    return;
  }

  cameraRouterPassword = passInput.value.trim();
  if (!cameraRouterBaseUrl || !cameraRouterPassword) {
    setStreamStatus("Enter both camera router URL and password", true);
    return;
  }

  const authPath = "/auth";
  setStreamStatus("Authenticating with camera router...");

  try {
    const response = await cameraRouterFetch(authPath, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: cameraRouterPassword }),
    }, false);
    const data = await response.json();
    if (!response.ok || data.status !== "success") {
      setStreamStatus(`Auth failed: ${data.message || response.status}`, true);
      return;
    }

    cameraRouterSessionKey = data.session_key;
    localStorage.setItem("cameraRouterBaseUrl", cameraRouterBaseUrl);
    localStorage.setItem("cameraRouterPassword", cameraRouterPassword);
    localStorage.setItem("cameraRouterSessionKey", cameraRouterSessionKey);

    setStreamStatus(`Authenticated. Session timeout ${data.timeout}s`);
    await refreshCameraFeeds();
    syncPinnedPreviewSource();
    if (cameraPreview.desired) {
      await startCameraPreview({ autoRestart: true, reason: "session refresh" });
    }
  } catch (err) {
    setStreamStatus(`Auth error: ${err}`, true);
  }
}

function renderCameraFeedOptions() {
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
  feedSelect.innerHTML = "";
  cameraRouterFeeds.forEach((feed) => {
    const opt = document.createElement("option");
    opt.value = feed.id;
    opt.textContent = `${feed.label} (${feed.online ? "online" : "offline"})`;
    feedSelect.appendChild(opt);
  });

  if (cameraRouterFeeds.length > 0) {
    if (previousValue && cameraRouterFeeds.some((feed) => feed.id === previousValue)) {
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

  if (profileSelect) {
    renderCameraProfileOptions(feedSelect.value || "");
  }
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
  let selectedIndex = 0;

  if (profiles.length > 0) {
    profiles.forEach((profile, idx) => {
      const opt = document.createElement("option");
      opt.value = String(idx);
      opt.textContent = formatProfileOption(profile);
      profileSelect.appendChild(opt);
      if (currentProfile && profileMatches(profile, currentProfile)) {
        selectedIndex = idx;
      }
    });
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
        syncPinnedPreviewSource();
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
    renderCameraFeedOptions();
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

async function startJpegPreview(cameraId) {
  activatePreviewMode("jpeg");
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
      scheduleCameraPreviewRestart("jpeg polling error");
    }
  };
  imageEl.onabort = imageEl.onerror;

  const refresh = () => {
    const t = Date.now();
    imageEl.src = cameraRouterUrl(`/jpeg/${encodeURIComponent(cameraId)}?t=${t}`, true);
  };
  refresh();
  cameraPreview.jpegTimer = setInterval(refresh, 120);
  cameraPreview.activeCameraId = cameraId;
  cameraPreview.targetCameraId = cameraId;
  cameraPreview.activeMode = STREAM_MODE_JPEG;
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
  const cameraId =
    requestedCameraId ||
    cameraPreview.targetCameraId ||
    feedSelect.value ||
    cameraPreview.activeCameraId ||
    localStorage.getItem("cameraRouterSelectedFeed") ||
    "";
  if (requestedCameraId && feedSelect.value !== requestedCameraId) {
    feedSelect.value = requestedCameraId;
  }
  let mode = modeSelect.value || STREAM_MODE_MJPEG;
  const allowedModes = new Set([STREAM_MODE_MJPEG, STREAM_MODE_JPEG]);
  if (!allowedModes.has(mode)) {
    mode = STREAM_MODE_MJPEG;
    modeSelect.value = mode;
  }
  if (!cameraId) {
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

  localStorage.setItem("cameraRouterSelectedFeed", cameraId);
  localStorage.setItem("cameraRouterSelectedMode", mode);
  cameraPreview.targetCameraId = cameraId;

  cameraPreview.desired = true;
  clearCameraPreviewRestartTimer();
  stopCameraPreview({ keepDesired: true });
  setStreamStatus(`${autoRestart ? "Restarting" : "Starting"} ${mode} preview for ${cameraId}...`);

  try {
    if (mode === STREAM_MODE_JPEG) {
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
  if (streamUiInitialized) {
    return;
  }
  streamUiInitialized = true;

  const baseInput = document.getElementById("cameraRouterBaseInput");
  const passInput = document.getElementById("cameraRouterPasswordInput");
  const authBtn = document.getElementById("cameraRouterAuthBtn");
  const refreshBtn = document.getElementById("cameraRouterRefreshBtn");
  const startBtn = document.getElementById("cameraPreviewStartBtn");
  const stopBtn = document.getElementById("cameraPreviewStopBtn");
  const profileApplyBtn = document.getElementById("cameraProfileApplyBtn");
  const modeSelect = document.getElementById("cameraModeSelect");
  const feedSelect = document.getElementById("cameraFeedSelect");
  initializePinnedPreviewUi();
  startCameraFeedPolling();

  if (baseInput) {
    baseInput.value = cameraRouterBaseUrl;
    baseInput.addEventListener("change", () => {
      try {
        cameraRouterBaseUrl = normalizeOrigin(baseInput.value);
        syncPinnedPreviewSource();
      } catch (err) {}
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
    const allowedModes = new Set([STREAM_MODE_MJPEG, STREAM_MODE_JPEG]);
    modeSelect.value = allowedModes.has(savedMode) ? savedMode : STREAM_MODE_MJPEG;
    localStorage.setItem("cameraRouterSelectedMode", modeSelect.value);
  }

  if (authBtn) {
    authBtn.addEventListener("click", authenticateCameraRouter);
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", refreshCameraFeeds);
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
  if (feedSelect) {
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
  if (modeSelect) {
    modeSelect.addEventListener("change", () => {
      const allowedModes = new Set([STREAM_MODE_MJPEG, STREAM_MODE_JPEG]);
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
    refreshCameraFeeds();
  } else {
    setStreamStatus("Configure camera router URL + password, then authenticate");
  }
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
        S: getNumberValue("S", 1),
        A: getNumberValue("A", 1),
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
    const current = getNumberValue(field, field === "S" || field === "A" ? 1 : 0);
    const next = current + step;
    setInputValue(field, field === "S" || field === "A" ? next.toFixed(1) : Math.round(next));
    updateHeadView();
}

function decHeadField(field, step) {
    const current = getNumberValue(field, field === "S" || field === "A" ? 1 : 0);
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

window.addEventListener('load', async () => {
  initializeRouting();
  const initialRoute = getRouteFromLocation();
  const queryConnection = parseConnectionFromQuery();
  ensureEndpointInputBindings();
  initNknRouterUi();
  setupStreamConfigUi();

  if (queryConnection.adapterConfigured && queryConnection.passwordProvided) {
    logToConsole("[CONNECT] Adapter and password found in query; attempting auto-connect...");
    const autoConnected = await authenticate(PASSWORD, WS_URL, HTTP_URL);
    if (autoConnected) {
      if (WS_URL && initialRoute !== "orientation") {
        initWebSocket();
      } else {
        hideConnectionModal();
      }
      return;
    }
    showConnectionModal();
    return;
  }

  // Check if we have a valid session
  if (SESSION_KEY && HTTP_URL) {
    logToConsole("[SESSION] Found saved session, attempting to reconnect...");
    metrics.connected = true;
    authenticated = true;
    updateMetrics();

    // Try WebSocket if configured
    if (WS_URL && initialRoute !== "orientation") {
      initWebSocket();
    } else {
      hideConnectionModal();
    }
  } else if (queryConnection.adapterConfigured) {
    // We have adapter URL but no session - show connection modal
    logToConsole("[CONNECT] Adapter URL configured, please authenticate...");
    if (initialRoute !== "streams" && initialRoute !== "orientation") {
      showConnectionModal();
    } else {
      hideConnectionModal();
    }
  } else {
    // Show connection modal on first load
    if (initialRoute !== "streams" && initialRoute !== "orientation") {
      showConnectionModal();
    } else {
      hideConnectionModal();
    }
  }
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
window.startCameraPreview = startCameraPreview;
window.stopCameraPreview = stopCameraPreview;
