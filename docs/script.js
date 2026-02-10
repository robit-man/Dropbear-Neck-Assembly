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
const ROUTES = new Set(["connect", "home", "direct", "euler", "head", "quaternion", "headstream"]);
let headstreamInitTriggered = false;

// Metrics tracking
let metrics = {
    connected: false,
    lastPing: 0,
    latency: 0,
    commandsSent: 0,
    dataRate: 0,
    lastCommandTime: 0
};

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
        latencyEl.textContent = metrics.latency + 'ms';
        latencyEl.className = 'metric-value ' + (metrics.latency < 100 ? 'good' : metrics.latency < 300 ? 'warning' : 'error');
    }

    if (rateEl) {
        rateEl.textContent = metrics.dataRate.toFixed(1) + ' cmd/s';
        rateEl.className = 'metric-value';
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
    // Pre-fill only saved values, no defaults
    const passInput = document.getElementById('passwordInput');
    const wsInput = document.getElementById('wsUrlInput');
    const httpInput = document.getElementById('httpUrlInput');

    if (passInput) passInput.value = PASSWORD || '';
    if (wsInput) wsInput.value = WS_URL || '';
    if (httpInput) httpInput.value = HTTP_URL || '';
    hydrateEndpointInputs("http");
  }
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

    // Try WebSocket if URL provided
    if (wsUrl) {
      initWebSocket();
    } else {
      hideConnectionModal();
    }
  }
}

// Parse query parameters for adapter URL
function parseConnectionFromQuery() {
  const urlParams = new URLSearchParams(window.location.search);
  const adapterParam = (urlParams.get('adapter') || "").trim();
  const passwordParam = (urlParams.get('password') || "").trim();

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

  return { adapterConfigured, passwordProvided };
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
  ensureEndpointInputBindings();
  const queryConnection = parseConnectionFromQuery();

  if (queryConnection.adapterConfigured && queryConnection.passwordProvided) {
    logToConsole("[CONNECT] Adapter and password found in query; attempting auto-connect...");
    const autoConnected = await authenticate(PASSWORD, WS_URL, HTTP_URL);
    if (autoConnected) {
      if (WS_URL) {
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
    if (WS_URL) {
      initWebSocket();
    } else {
      hideConnectionModal();
    }
  } else if (queryConnection.adapterConfigured) {
    // We have adapter URL but no session - show connection modal
    logToConsole("[CONNECT] Adapter URL configured, please authenticate...");
    showConnectionModal();
  } else {
    // Show connection modal on first load
    showConnectionModal();
  }
});

window.sendCommand = sendCommand;
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
