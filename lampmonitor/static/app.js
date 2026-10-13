"use strict";

/* ------------------------------------------------------------------ helpers */

const app = document.getElementById("app");
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// Each navigation bumps routeToken; async work from a previous page checks it before touching the DOM.
let routeToken = 0;
let teardown = [];

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function errorText(data, status) {
  if (data && data.detail) {
    if (Array.isArray(data.detail)) {
      return data.detail
        .map((d) => {
          const where = (d.loc || []).filter((part) => part !== "body").join(".");
          return where ? `${where}: ${d.msg}` : d.msg;
        })
        .join("; ");
    }
    return String(data.detail);
  }
  return `Request failed (HTTP ${status})`;
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const isJson = (response.headers.get("content-type") || "").includes("json");
  const data = isJson ? await response.json() : await response.text();
  if (!response.ok) throw new Error(errorText(data, response.status));
  return data;
}

function toast(message, kind = "ok") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), kind === "error" ? 9000 : 3500);
}

function fmtTime(iso) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function duration(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const STATE_LABEL = { on: "ON", off: "OFF", unknown: "UNKNOWN", disabled: "DISABLED" };

function lampStateKey(lamp, live) {
  if (!lamp.enabled) return "disabled";
  return (live && live.state) || lamp.state;
}

/** Run fn now and then every ms milliseconds until the user navigates away. */
function poll(ms, fn) {
  const token = routeToken;
  let timer;
  const tick = async () => {
    if (token !== routeToken) return;
    try {
      await fn();
    } catch (err) {
      console.error(err);
    }
    if (token === routeToken) timer = setTimeout(tick, ms);
  };
  tick();
  teardown.push(() => clearTimeout(timer));
}

async function withBusy(container, fn) {
  const buttons = $$("button", container);
  buttons.forEach((b) => (b.disabled = true));
  try {
    return await fn();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

function meter(value, threshold, hysteresis) {
  const pct = (v) => clamp((v / 255) * 100, 0, 100);
  const bandStart = pct(threshold - hysteresis);
  return `<div class="meter" title="Brightness 0–255. Line: ON threshold. Striped: hysteresis band.">
    <div class="meter-fill" style="width:${value == null ? 0 : pct(value).toFixed(1)}%"></div>
    <div class="meter-band" style="left:${bandStart.toFixed(1)}%;width:${(pct(threshold) - bandStart).toFixed(1)}%"></div>
    <div class="meter-mark" style="left:${pct(threshold).toFixed(1)}%"></div>
  </div>`;
}

const EVENT_KINDS = {
  lamp_off: ["off", "Lamp off"],
  lamp_on: ["on", "Lamp on"],
  camera_offline: ["off", "Camera offline"],
  camera_online: ["on", "Camera online"],
  email_sent: ["info", "Email sent"],
  email_failed: ["warn", "Email failed"],
};

function eventsTable(events) {
  if (!events.length) return `<div class="empty">No events yet.</div>`;
  const rows = events
    .map((e) => {
      const [cls, label] = EVENT_KINDS[e.kind] || ["unknown", e.kind];
      return `<tr>
        <td class="nowrap">${esc(fmtTime(e.ts))}</td>
        <td><span class="badge badge-${cls}">${esc(label)}</span></td>
        <td>${esc(e.message)}</td>
      </tr>`;
    })
    .join("");
  return `<div class="table-wrap"><table class="table">
    <thead><tr><th>Time</th><th>Event</th><th>Details</th></tr></thead><tbody>${rows}</tbody>
  </table></div>`;
}

function cameraState(camera) {
  if (!camera.enabled) return ["unknown", "Disabled"];
  if (camera.online === true) return ["on", "Online"];
  if (camera.online === false) return ["off", "Offline"];
  return camera.last_error ? ["warn", "Retrying"] : ["unknown", "Connecting"];
}

/* ------------------------------------------------------------------- router */

const routes = [
  [/^#\/?$/, renderDashboard],
  [/^#\/cameras\/?$/, renderCameras],
  [/^#\/cameras\/(\d+)(?:\?lamp=(\d+))?$/, renderCamera],
  [/^#\/events\/?$/, renderEvents],
  [/^#\/settings\/?$/, renderSettings],
];

function route() {
  teardown.forEach((fn) => fn());
  teardown = [];
  routeToken += 1;
  const token = routeToken;
  const hash = location.hash || "#/";
  for (const link of $$("[data-nav]")) {
    const href = link.getAttribute("href");
    link.classList.toggle("active", href === "#/" ? hash === "#/" || hash === "#" : hash.startsWith(href));
  }
  for (const [pattern, render] of routes) {
    const match = hash.match(pattern);
    if (match) {
      render(...match.slice(1)).catch((err) => {
        if (token === routeToken) app.innerHTML = `<div class="banner error">${esc(err.message)}</div>`;
      });
      return;
    }
  }
  app.innerHTML = `<div class="empty">Page not found. <a href="#/">Go to the dashboard</a>.</div>`;
}

window.addEventListener("hashchange", route);

/* ---------------------------------------------------------------- dashboard */

async function renderDashboard() {
  app.innerHTML = `
    <div class="page-head"><h1>Dashboard</h1></div>
    <div id="dash-error"></div>
    <div id="summary" class="summary"></div>
    <h2>Lamps</h2>
    <div id="lamp-grid" class="lamp-grid"></div>
    <h2>Cameras</h2>
    <div id="camera-grid" class="camera-grid"></div>
    <h2>Recent events <a class="small" href="#/events">View all</a></h2>
    <div id="recent-events"></div>`;

  poll(3000, async () => {
    let status, events;
    try {
      [status, events] = await Promise.all([api("GET", "/api/status"), api("GET", "/api/events?limit=10")]);
    } catch (err) {
      $("#dash-error").innerHTML = `<div class="banner error">Can't reach LampMonitor: ${esc(err.message)}</div>`;
      return;
    }
    $("#dash-error").innerHTML = "";
    const cameras = new Map(status.cameras.map((c) => [c.id, c]));
    const lamps = status.lamps;
    const active = lamps.filter((l) => l.enabled);
    const count = (state) => active.filter((l) => l.state === state).length;
    const offline = status.cameras.filter((c) => c.enabled && c.online === false).length;
    const enabledCams = status.cameras.filter((c) => c.enabled).length;

    $("#summary").innerHTML = `
      <div class="tile ${count("off") ? "alert" : ""}"><div class="num">${count("off")}</div><div class="label">Lamps OFF</div></div>
      <div class="tile"><div class="num">${count("on")}</div><div class="label">Lamps ON</div></div>
      <div class="tile"><div class="num">${count("unknown")}</div><div class="label">Lamps not yet read</div></div>
      <div class="tile ${offline ? "alert" : ""}"><div class="num">${enabledCams - offline}/${enabledCams}</div><div class="label">Cameras online</div></div>`;

    if (!status.cameras.length) {
      $("#lamp-grid").innerHTML = `<div class="empty">No cameras yet. <a href="#/cameras">Add a camera</a> to get started.</div>`;
    } else if (!lamps.length) {
      $("#lamp-grid").innerHTML = `<div class="empty">No lamps yet. Open a camera and drag a box around each lamp you want to watch.</div>`;
    } else {
      // OFF lamps first so problems are at the top.
      const order = { off: 0, unknown: 1, on: 2, disabled: 3 };
      const sorted = [...lamps].sort(
        (a, b) => order[lampStateKey(a)] - order[lampStateKey(b)] || a.name.localeCompare(b.name)
      );
      $("#lamp-grid").innerHTML = sorted.map((l) => lampCard(l, cameras.get(l.camera_id))).join("");
    }

    $("#camera-grid").innerHTML = status.cameras.length
      ? status.cameras.map(cameraCard).join("")
      : `<div class="empty">No cameras configured.</div>`;
    $("#recent-events").innerHTML = eventsTable(events);
  });
}

function lampCard(lamp, camera) {
  const state = lampStateKey(lamp);
  const cameraOffline = camera && camera.enabled && camera.online === false;
  const pending =
    lamp.pending && lamp.enabled
      ? `<div class="pending">Confirming ${STATE_LABEL[lamp.pending]}… ${Math.floor(lamp.pending_seconds)} of ${lamp.confirm_seconds} s</div>`
      : "";
  return `<a class="lamp-card state-${state}" href="#/cameras/${lamp.camera_id}?lamp=${lamp.id}">
    <div class="lamp-card-head">
      <span class="lamp-dot"></span>
      <span class="lamp-name" title="${esc(lamp.name)}">${esc(lamp.name)}</span>
      <span class="badge badge-${state}">${STATE_LABEL[state]}</span>
    </div>
    <div class="muted">${esc(camera ? camera.name : "")}${cameraOffline ? ' · <span class="text-off">camera offline</span>' : ""}</div>
    ${meter(lamp.value, lamp.threshold, lamp.hysteresis)}
    <div class="lamp-card-foot">
      <span title="Threshold ${Math.round(lamp.threshold)}">${lamp.value == null ? "No reading yet" : `Brightness ${Math.round(lamp.value)}`}</span>
      <span title="Since ${esc(fmtTime(lamp.state_since))}">${lamp.state_since && lamp.enabled ? `${STATE_LABEL[state]} for ${esc(duration(lamp.state_since))}` : ""}</span>
    </div>
    ${pending}
  </a>`;
}

function cameraCard(camera) {
  const [cls, label] = cameraState(camera);
  return `<a class="camera-card" href="#/cameras/${camera.id}">
    <div class="lamp-card-head"><span class="lamp-name">${esc(camera.name)}</span><span class="badge badge-${cls}">${label}</span></div>
    <div class="muted small">${camera.last_frame_at ? `Last image ${esc(ago(camera.last_frame_at))}` : "No image yet"}${camera.width ? ` · ${camera.width}×${camera.height}` : ""}</div>
    ${camera.last_error ? `<div class="error">${esc(camera.last_error)}</div>` : ""}
  </a>`;
}

/* ------------------------------------------------------------------ cameras */

const SOURCE_LABEL = { protect: "UniFi Protect", rtsp: "RTSP stream", http: "Snapshot URL" };

async function renderCameras() {
  app.innerHTML = `
    <div class="page-head"><h1>Cameras</h1></div>
    <div id="camera-table"></div>
    <section class="card" style="margin-top:1rem">
      <h2>Add a camera</h2>
      ${cameraFormHtml(null)}
    </section>`;

  bindCameraForm($("#camera-form"), null, async (payload) => {
    const camera = await api("POST", "/api/cameras", payload);
    toast(`Added ${camera.name}. Now drag a box around each lamp.`);
    location.hash = `#/cameras/${camera.id}`;
  });

  poll(5000, async () => {
    const status = await api("GET", "/api/status");
    if (!status.cameras.length) {
      $("#camera-table").innerHTML = `<div class="empty">No cameras yet. Add one below.</div>`;
      return;
    }
    const lampCount = (id) => status.lamps.filter((l) => l.camera_id === id).length;
    $("#camera-table").innerHTML = `<div class="table-wrap"><table class="table">
      <thead><tr><th>Camera</th><th>Source</th><th>Status</th><th>Lamps</th><th>Last image</th></tr></thead>
      <tbody>${status.cameras
        .map((c) => {
          const [cls, label] = cameraState(c);
          return `<tr>
            <td><a href="#/cameras/${c.id}">${esc(c.name)}</a></td>
            <td>${SOURCE_LABEL[c.source_type] || esc(c.source_type)}</td>
            <td><span class="badge badge-${cls}">${label}</span>${c.last_error ? `<div class="muted small">${esc(c.last_error)}</div>` : ""}</td>
            <td>${lampCount(c.id)}</td>
            <td class="nowrap">${c.last_frame_at ? esc(ago(c.last_frame_at)) : "—"}</td>
          </tr>`;
        })
        .join("")}</tbody></table></div>`;
  });
}

function cameraFormHtml(camera) {
  const c = camera || { name: "", source_type: "protect", protect_camera_id: "", url: "", poll_interval: 5, enabled: true };
  return `<form id="camera-form" class="form" autocomplete="off" novalidate>
    <label>Image source
      <select name="source_type">
        <option value="protect">UniFi Protect (API key)</option>
        <option value="rtsp">RTSP / RTSPS stream (full resolution)</option>
        <option value="http">Snapshot URL (JPEG over HTTP)</option>
      </select>
    </label>
    <div data-source="protect">
      <label>Protect camera
        <div class="row">
          <select name="protect_pick"><option value="">Loading cameras…</option></select>
          <button type="button" data-action="reload-protect">Reload</button>
        </div>
      </label>
      <p class="hint" data-role="protect-msg"></p>
      <label>Protect camera ID
        <input name="protect_camera_id" value="${esc(c.protect_camera_id)}" placeholder="Filled in when you choose a camera above">
      </label>
      <p class="hint">Protect's snapshot API can return reduced-resolution images (as low as 640×360).
        If your lamps are only a few pixels across, use the camera's RTSPS stream instead.</p>
    </div>
    <div data-source="rtsp http">
      <label><span data-role="url-label">Stream URL</span>
        <input name="url" value="${esc(c.url)}" spellcheck="false">
      </label>
      <p class="hint" data-role="url-hint"></p>
    </div>
    <label>Camera name
      <input name="name" maxlength="100" value="${esc(c.name)}" placeholder="Electrical room – Panel A">
    </label>
    <label>Check every (seconds)
      <input type="number" name="poll_interval" min="1" max="3600" step="1" value="${esc(c.poll_interval)}">
    </label>
    <label class="check"><input type="checkbox" name="enabled" ${c.enabled ? "checked" : ""}> Monitoring enabled</label>
    <div class="actions">
      <button class="primary" type="submit">${camera ? "Save camera" : "Add camera"}</button>
      ${camera ? '<span class="spacer"></span><button type="button" class="danger" data-action="delete-camera">Delete camera</button>' : ""}
    </div>
  </form>`;
}

const URL_HELP = {
  rtsp: {
    label: "Stream URL",
    placeholder: "rtsps://192.168.1.1:7441/AbCdEf0123456789?enableSrtp",
    hint: "In UniFi Protect open the camera → Settings → Advanced → RTSP, turn on the High stream and copy its URL. Requires ffmpeg (included in the Docker image).",
  },
  http: {
    label: "Snapshot URL",
    placeholder: "http://user:password@192.168.1.50/snap.jpeg",
    hint: "Any URL that returns a JPEG or PNG still image. Credentials can be included in the URL.",
  },
};

function bindCameraForm(form, camera, onSubmit) {
  const f = form.elements;
  f.source_type.value = camera ? camera.source_type : "protect";
  let protectLoaded = false;

  async function loadProtect() {
    protectLoaded = true;
    const pick = f.protect_pick;
    const msg = $('[data-role="protect-msg"]', form);
    pick.innerHTML = `<option value="">Loading cameras…</option>`;
    msg.textContent = "";
    try {
      const cameras = await api("GET", "/api/protect/cameras");
      pick.innerHTML =
        `<option value="">${cameras.length ? "Choose a camera…" : "No cameras found"}</option>` +
        cameras
          .map((c) => {
            const state = c.state && c.state !== "CONNECTED" ? ` (${c.state.toLowerCase()})` : "";
            return `<option value="${esc(c.id)}" data-name="${esc(c.name)}">${esc(c.name)}${esc(state)}</option>`;
          })
          .join("");
      pick.value = f.protect_camera_id.value;
    } catch (err) {
      pick.innerHTML = `<option value="">Camera list unavailable</option>`;
      msg.innerHTML = `<span class="text-off">${esc(err.message)}</span> <a href="#/settings">Open Settings</a>, or paste a camera ID below.`;
    }
  }

  function showSource() {
    const type = f.source_type.value;
    for (const el of $$("[data-source]", form)) el.hidden = !el.dataset.source.split(" ").includes(type);
    if (URL_HELP[type]) {
      $('[data-role="url-label"]', form).textContent = URL_HELP[type].label;
      $('[data-role="url-hint"]', form).textContent = URL_HELP[type].hint;
      f.url.placeholder = URL_HELP[type].placeholder;
    }
    if (type === "protect" && !protectLoaded) loadProtect();
  }

  f.protect_pick.addEventListener("change", () => {
    const option = f.protect_pick.selectedOptions[0];
    if (!f.protect_pick.value) return;
    f.protect_camera_id.value = f.protect_pick.value;
    if (!f.name.value.trim()) f.name.value = option.dataset.name;
  });
  $('[data-action="reload-protect"]', form).addEventListener("click", loadProtect);
  f.source_type.addEventListener("change", showSource);
  showSource();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const type = f.source_type.value;
    const payload = {
      name: f.name.value,
      source_type: type,
      protect_camera_id: type === "protect" ? f.protect_camera_id.value : "",
      url: type === "protect" ? "" : f.url.value,
      poll_interval: Number(f.poll_interval.value) || 5,
      enabled: f.enabled.checked,
    };
    withBusy(form, () => onSubmit(payload));
  });
}

/* ------------------------------------------------------- camera / lamp editor */

const LAMP_DEFAULTS = {
  name: "",
  metric: "peak",
  threshold: 128,
  hysteresis: 10,
  confirm_seconds: 15,
  email_to: "",
  notes: "",
  notify_on_restore: true,
  repeat_minutes: 0,
  enabled: true,
};

const BOX_COLORS = { on: "#16a34a", off: "#dc2626", unknown: "#9ca3af", disabled: "#6b7280", selected: "#3b82f6" };

async function renderCamera(cameraIdText, lampParam) {
  const cameraId = Number(cameraIdText);
  const token = routeToken;
  const alive = () => token === routeToken;
  const [camera, lamps, settings] = await Promise.all([
    api("GET", `/api/cameras/${cameraId}`),
    api("GET", `/api/cameras/${cameraId}/lamps`),
    api("GET", "/api/settings"),
  ]);
  if (!alive()) return;
  const adminEmails = settings.admin_emails;

  const st = {
    camera,
    lamps,
    live: new Map(), // lamp id -> status from /api/status
    selected: null, // lamp id, "new", or null
    draft: null, // region being edited {x, y, w, h}
    dirty: false,
    reading: null,
    readingError: null,
  };

  app.innerHTML = `
    <div class="page-head">
      <a class="back" href="#/cameras">← Cameras</a>
      <h1 id="cam-title">${esc(camera.name)}</h1>
      <span id="cam-badge" class="badge">…</span>
    </div>
    <div id="cam-error"></div>
    <div class="editor">
      <section class="card viewer-card">
        <div class="toolbar">
          <span class="muted small">Drag on the image to outline a lamp. Click a box to edit it.</span>
          <span class="spacer"></span>
          <label>Zoom
            <select id="zoom">
              <option value="fit">Fit</option><option value="1">100%</option><option value="2">200%</option>
              <option value="3">300%</option><option value="4">400%</option>
            </select>
          </label>
          <button type="button" id="refresh">Refresh image</button>
        </div>
        <div class="viewer" id="viewer">
          <div class="stage" id="stage"><img id="snap" alt="Camera image" draggable="false"><canvas id="overlay"></canvas></div>
          <div class="viewer-msg" id="viewer-msg">Loading image…</div>
        </div>
        <div class="viewer-foot muted"><span id="img-info"></span><span id="img-time"></span></div>
      </section>
      <aside>
        <section class="card">
          <h2>Lamps on this camera</h2>
          <ul class="lamp-list" id="lamp-list"></ul>
        </section>
        <section class="card" id="lamp-panel"></section>
        <details class="card"><summary>Camera settings</summary>${cameraFormHtml(camera)}</details>
      </aside>
    </div>`;

  const img = $("#snap");
  const canvas = $("#overlay");
  const ctx = canvas.getContext("2d");
  const viewerMsg = $("#viewer-msg");
  const zoom = $("#zoom");
  try {
    zoom.value = localStorage.getItem("lampmonitor.zoom") || "fit";
  } catch {
    /* storage unavailable */
  }

  /* ---- image loading and layout ---- */

  let objectUrl = null;
  async function loadImage(fresh = false) {
    try {
      const response = await fetch(`/api/cameras/${cameraId}/snapshot${fresh ? "?fresh=1" : ""}`, { cache: "no-store" });
      if (!response.ok) throw new Error(errorText(await response.json().catch(() => null), response.status));
      const blob = await response.blob();
      if (!alive()) return;
      const url = URL.createObjectURL(blob);
      img.onload = () => {
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        objectUrl = url;
        viewerMsg.hidden = true;
        $("#img-info").textContent = `${img.naturalWidth}×${img.naturalHeight} px`;
        $("#img-time").textContent = `Image from ${new Date().toLocaleTimeString()}`;
        layout();
      };
      img.src = url;
    } catch (err) {
      if (!alive()) return;
      if (!img.naturalWidth) {
        viewerMsg.hidden = false;
        viewerMsg.textContent = err.message;
      } else {
        $("#img-time").textContent = `Image refresh failed: ${err.message}`;
      }
    }
  }

  function layout() {
    if (!img.naturalWidth) return;
    const z = zoom.value;
    let width = img.naturalWidth * Number(z);
    if (z === "fit") {
      // Fit both dimensions so "fit" never needs scrollbars (which would change the width again).
      const availableHeight = Math.max(240, window.innerHeight - 230);
      width = Math.min($("#viewer").clientWidth, (availableHeight * img.naturalWidth) / img.naturalHeight);
    }
    img.style.width = `${Math.floor(width)}px`;
    img.classList.toggle("pixelated", img.clientWidth / img.naturalWidth >= 2);
    const dpr = window.devicePixelRatio || 1;
    canvas.style.width = `${img.clientWidth}px`;
    canvas.style.height = `${img.clientHeight}px`;
    canvas.width = Math.round(img.clientWidth * dpr);
    canvas.height = Math.round(img.clientHeight * dpr);
    draw();
  }

  zoom.addEventListener("change", () => {
    try {
      localStorage.setItem("lampmonitor.zoom", zoom.value);
    } catch {
      /* storage unavailable */
    }
    layout();
  });
  let viewerWidth = 0;
  const resizeObserver = new ResizeObserver(() => {
    const width = $("#viewer").clientWidth;
    if (width !== viewerWidth && zoom.value === "fit") layout();
    viewerWidth = width;
  });
  resizeObserver.observe($("#viewer"));
  teardown.push(() => resizeObserver.disconnect());
  teardown.push(() => objectUrl && URL.revokeObjectURL(objectUrl));
  $("#refresh").addEventListener("click", () => loadImage(true));

  /* ---- drawing ---- */

  function lampLabel() {
    const form = $("#lamp-form");
    const name = form && form.dataset.lamp === String(st.selected) && form.elements.name.value.trim();
    return name || (st.selected === "new" ? "New lamp" : "Lamp");
  }

  function drawBox(r, color, label, selected) {
    const W = canvas.clientWidth;
    const H = canvas.clientHeight;
    const x = r.x * W;
    const y = r.y * H;
    const w = r.w * W;
    const h = r.h * H;
    // Brightness is measured inside the oval inscribed in the box.
    const oval = () => {
      ctx.beginPath();
      ctx.ellipse(x + w / 2, y + h / 2, Math.max(w / 2, 0.5), Math.max(h / 2, 0.5), 0, 0, Math.PI * 2);
    };
    ctx.setLineDash([]);
    ctx.lineWidth = 4;
    ctx.strokeStyle = "rgba(0,0,0,.55)";
    oval();
    ctx.stroke();
    ctx.lineWidth = 2;
    ctx.strokeStyle = color;
    oval();
    ctx.stroke();
    if (selected) {
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([]);
    }

    ctx.font = "600 12px system-ui, sans-serif";
    if (!selected) {
      const room = Math.max(w + 16, 90);
      if (ctx.measureText(label).width + 10 > room) {
        while (label.length > 1 && ctx.measureText(`${label}…`).width + 10 > room) label = label.slice(0, -1);
        label = `${label.trimEnd()}…`;
      }
    }
    const tw = ctx.measureText(label).width + 10;
    const lx = clamp(x, 0, Math.max(0, W - tw));
    let ly = y - 20;
    if (ly < 0) ly = y + h + 3;
    ctx.fillStyle = color;
    ctx.fillRect(lx, ly, tw, 17);
    ctx.fillStyle = "#fff";
    ctx.fillText(label, lx + 5, ly + 13);

    if (selected) {
      for (const [hx, hy] of [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]) {
        ctx.fillStyle = "#fff";
        ctx.fillRect(hx - 4, hy - 4, 8, 8);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.strokeRect(hx - 4, hy - 4, 8, 8);
      }
    }
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    for (const lamp of st.lamps) {
      if (lamp.id === st.selected) continue;
      const state = lampStateKey(lamp, st.live.get(lamp.id));
      drawBox(lamp, BOX_COLORS[state], lamp.name, false);
    }
    if (st.draft) drawBox(st.draft, BOX_COLORS.selected, lampLabel(), true);
  }

  /* ---- pointer interaction: draw, select, move, resize ---- */

  const HANDLE = 9; // px
  let drag = null;

  function pointer(event) {
    const rect = canvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    return { px, py, x: clamp(px / rect.width, 0, 1), y: clamp(py / rect.height, 0, 1) };
  }

  function cornerAt(r, p) {
    const W = canvas.clientWidth;
    const H = canvas.clientHeight;
    const corners = { nw: [r.x, r.y], ne: [r.x + r.w, r.y], sw: [r.x, r.y + r.h], se: [r.x + r.w, r.y + r.h] };
    for (const [name, [cx, cy]] of Object.entries(corners)) {
      if (Math.abs(cx * W - p.px) <= HANDLE && Math.abs(cy * H - p.py) <= HANDLE) return name;
    }
    return null;
  }

  function contains(r, p, slackPx = 3) {
    const sx = slackPx / canvas.clientWidth;
    const sy = slackPx / canvas.clientHeight;
    return p.x >= r.x - sx && p.x <= r.x + r.w + sx && p.y >= r.y - sy && p.y <= r.y + r.h + sy;
  }

  const rectBetween = (a, b) => ({
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    w: Math.abs(a.x - b.x),
    h: Math.abs(a.y - b.y),
  });

  function startDrag(event, mode, p, extra = {}) {
    drag = { mode, start: p, orig: st.draft ? { ...st.draft } : null, moved: false, ...extra };
    canvas.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !img.naturalWidth) return;
    const p = pointer(event);
    if (st.draft) {
      const corner = cornerAt(st.draft, p);
      if (corner) return startDrag(event, "resize", p, { corner });
      if (contains(st.draft, p)) return startDrag(event, "move", p);
    }
    // Prefer the smallest box under the pointer so nested/overlapping lamps stay selectable.
    const hit = st.lamps
      .filter((l) => l.id !== st.selected && contains(l, p))
      .sort((a, b) => a.w * a.h - b.w * b.h)[0];
    if (hit) {
      if (selectLamp(hit.id)) startDrag(event, "move", p);
      return;
    }
    if (!confirmDiscard()) return;
    startDrag(event, "draw", p, { previous: st.selected });
  });

  canvas.addEventListener("pointermove", (event) => {
    const p = pointer(event);
    if (!drag) {
      let cursor = "crosshair";
      if (st.draft) {
        const corner = cornerAt(st.draft, p);
        if (corner) cursor = corner === "nw" || corner === "se" ? "nwse-resize" : "nesw-resize";
        else if (contains(st.draft, p)) cursor = "move";
      }
      if (cursor === "crosshair" && st.lamps.some((l) => l.id !== st.selected && contains(l, p))) cursor = "pointer";
      canvas.style.cursor = cursor;
      return;
    }
    if (!drag.moved && Math.hypot(p.px - drag.start.px, p.py - drag.start.py) < 4) return;
    drag.moved = true;
    const o = drag.orig;
    if (drag.mode === "draw") {
      if (st.selected !== "new") {
        st.selected = "new";
        renderLampList();
      }
      st.draft = rectBetween(drag.start, p);
    } else if (drag.mode === "move") {
      st.draft = {
        x: clamp(o.x + p.x - drag.start.x, 0, 1 - o.w),
        y: clamp(o.y + p.y - drag.start.y, 0, 1 - o.h),
        w: o.w,
        h: o.h,
      };
    } else {
      const fixed = {
        x: drag.corner.includes("w") ? o.x + o.w : o.x,
        y: drag.corner.includes("n") ? o.y + o.h : o.y,
      };
      st.draft = rectBetween(fixed, p);
    }
    markDirty();
    draw();
    scheduleMeasure();
  });

  function endDrag() {
    if (!drag) return;
    const d = drag;
    drag = null;
    if (d.mode !== "draw") return;
    const tooSmall = !st.draft || st.draft.w * canvas.clientWidth < 3 || st.draft.h * canvas.clientHeight < 3;
    if (!d.moved || tooSmall) {
      deselect(); // a click on empty image area
      return;
    }
    st.selected = "new";
    st.reading = null;
    renderLampPanel();
    renderLampList();
    draw();
    scheduleMeasure(0);
    $("#lamp-form").elements.name.focus();
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  /* ---- selection ---- */

  function confirmDiscard() {
    return !st.dirty || confirm("Discard unsaved changes to this lamp?");
  }

  function setUrlLamp(id) {
    history.replaceState(null, "", id ? `#/cameras/${cameraId}?lamp=${id}` : `#/cameras/${cameraId}`);
  }

  function selectLamp(id) {
    if (st.selected === id) return true;
    if (!confirmDiscard()) return false;
    const lamp = st.lamps.find((l) => l.id === id);
    if (!lamp) return false;
    st.selected = id;
    st.draft = { x: lamp.x, y: lamp.y, w: lamp.w, h: lamp.h };
    st.dirty = false;
    st.reading = null;
    setUrlLamp(id);
    renderLampPanel();
    renderLampList();
    draw();
    scheduleMeasure(0);
    return true;
  }

  function deselect() {
    st.selected = null;
    st.draft = null;
    st.dirty = false;
    st.reading = null;
    setUrlLamp(null);
    renderLampPanel();
    renderLampList();
    draw();
  }

  function markDirty() {
    st.dirty = true;
    const note = $("#dirty-note");
    if (note) note.hidden = false;
  }

  /* ---- lamp list ---- */

  function renderLampList() {
    const list = $("#lamp-list");
    if (!st.lamps.length && st.selected !== "new") {
      list.innerHTML = `<li class="muted small">No lamps yet. Drag a box around a lamp on the image.</li>`;
      return;
    }
    const items = st.lamps.map((lamp) => {
      const live = st.live.get(lamp.id);
      const state = lampStateKey(lamp, live);
      const value = live && live.value != null ? Math.round(live.value) : "";
      return `<li><button type="button" class="lamp-item state-${state} ${lamp.id === st.selected ? "selected" : ""}" data-id="${lamp.id}">
        <span class="lamp-dot"></span><span class="grow">${esc(lamp.name)}</span>
        <span class="value" title="Current brightness">${value}</span>
        <span class="badge badge-${state}">${STATE_LABEL[state]}</span>
      </button></li>`;
    });
    if (st.selected === "new") {
      items.push(`<li><button type="button" class="lamp-item selected"><span class="lamp-dot"></span><span class="grow">${esc(lampLabel())}</span><span class="badge badge-info">NEW</span></button></li>`);
    }
    list.innerHTML = items.join("");
  }

  $("#lamp-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-id]");
    if (button) selectLamp(Number(button.dataset.id));
  });

  /* ---- lamp form ---- */

  function renderLampPanel() {
    const panel = $("#lamp-panel");
    if (!st.selected) {
      panel.innerHTML = `<h2>Lamp settings</h2>
        <p class="muted">Select a lamp on the image or in the list above, or drag a box around a lamp on the image to add one.</p>
        <p class="muted small">Draw the box around the lamp's lens. Brightness is measured inside the oval that fits the box, so the panel around a round lamp is left out.</p>`;
      return;
    }
    const isNew = st.selected === "new";
    const lamp = isNew ? LAMP_DEFAULTS : st.lamps.find((l) => l.id === st.selected);
    panel.innerHTML = `<form id="lamp-form" class="form" autocomplete="off" data-lamp="${esc(st.selected)}">
      <h2>${isNew ? "New lamp" : "Edit lamp"} <span id="dirty-note" class="dirty-note" ${st.dirty ? "" : "hidden"}>Unsaved changes</span></h2>
      <div class="reading">
        <div class="reading-row">
          <span class="muted">Brightness now</span>
          <strong id="reading-value">—</strong>
          <span id="reading-verdict" class="badge"></span>
        </div>
        <div id="reading-meter"></div>
        <p class="hint" id="reading-hint">Measured from the latest camera image (0 = black, 255 = full brightness).</p>
        <button type="button" class="link small" id="suggest">Lamp is lit right now? Set the threshold to 60% of this reading</button>
      </div>
      <label>Friendly name
        <input name="name" required maxlength="100" placeholder="Panel A – Circuit 12 (Freezer)">
      </label>
      <fieldset>
        <legend>Detection</legend>
        <label>Measure
          <select name="metric">
            <option value="peak">Brightest spot in box (recommended)</option>
            <option value="mean">Average of whole box</option>
          </select>
        </label>
        <label>ON threshold
          <div class="range-row">
            <input type="range" name="threshold_slider" min="0" max="255" step="1">
            <input type="number" name="threshold" min="0" max="255" step="1" required>
          </div>
        </label>
        <p class="hint">The lamp counts as ON at or above this brightness. For the most reliable setting, note the
          reading with the lamp on and off (e.g. switch the circuit off briefly) and choose a value in between.</p>
        <div class="grid2">
          <label>Hysteresis
            <input type="number" name="hysteresis" min="0" max="255" step="1" required>
          </label>
          <label>Confirm after (s)
            <input type="number" name="confirm_seconds" min="0" max="86400" step="1" required>
          </label>
        </div>
        <p class="hint">OFF means below threshold − hysteresis. A change must last “confirm after” seconds before it's reported, which ignores flicker and people walking past.</p>
      </fieldset>
      <fieldset>
        <legend>Email alerts</legend>
        <label>Send alerts to
          <textarea name="email_to" rows="2" placeholder="${esc(adminEmails ? `Blank = ${adminEmails}` : "ops@example.com, oncall@example.com")}"></textarea>
        </label>
        <p class="hint">Separate addresses with commas. Leave blank to use the admin recipients from Settings.</p>
        <label>Notes for the alert email
          <textarea name="notes" rows="3" maxlength="2000" placeholder="What this circuit feeds and who to call"></textarea>
        </label>
        <label class="check"><input type="checkbox" name="notify_on_restore"> Also email when the lamp comes back on</label>
        <label>Repeat the alert every (minutes, 0 = once)
          <input type="number" name="repeat_minutes" min="0" max="10080" step="1" required>
        </label>
      </fieldset>
      <label class="check"><input type="checkbox" name="enabled"> Monitor this lamp</label>
      <div class="actions">
        <button type="submit" class="primary">${isNew ? "Add lamp" : "Save"}</button>
        ${isNew ? "" : '<button type="button" id="test-alert">Send test alert</button>'}
        <button type="button" id="cancel">${isNew ? "Discard" : "Close"}</button>
        ${isNew ? "" : '<span class="spacer"></span><button type="button" class="danger" id="delete-lamp">Delete</button>'}
      </div>
    </form>`;

    const form = $("#lamp-form");
    const f = form.elements;
    for (const key of ["name", "metric", "threshold", "hysteresis", "confirm_seconds", "email_to", "notes", "repeat_minutes"]) {
      f[key].value = lamp[key];
    }
    f.notify_on_restore.checked = lamp.notify_on_restore;
    f.enabled.checked = lamp.enabled;
    f.threshold_slider.value = lamp.threshold;

    form.addEventListener("input", (event) => {
      if (event.target.name === "threshold_slider") f.threshold.value = f.threshold_slider.value;
      if (event.target.name === "threshold") f.threshold_slider.value = f.threshold.value;
      if (event.target.name === "name") {
        draw();
        renderLampList();
      }
      markDirty();
      showReading();
    });
    f.metric.addEventListener("change", () => scheduleMeasure(0));
    $("#suggest").addEventListener("click", () => {
      if (st.reading == null) return toast("No brightness reading yet.", "error");
      const value = clamp(Math.round(st.reading * 0.6), 1, 255);
      f.threshold.value = value;
      f.threshold_slider.value = value;
      markDirty();
      showReading();
    });
    $("#cancel").addEventListener("click", () => {
      if (confirmDiscard()) {
        st.dirty = false;
        deselect();
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      withBusy(form, saveLamp);
    });
    if (!isNew) {
      $("#test-alert").addEventListener("click", () =>
        withBusy(form, async () => {
          if (st.dirty) throw new Error("Save your changes before sending a test alert.");
          const result = await api("POST", `/api/lamps/${st.selected}/test-alert`);
          toast(`Test alert sent to ${result.sent_to.join(", ")}`);
        })
      );
      $("#delete-lamp").addEventListener("click", () =>
        withBusy(form, async () => {
          if (!confirm(`Delete lamp “${lamp.name}”? It will no longer be monitored.`)) return;
          await api("DELETE", `/api/lamps/${lamp.id}`);
          st.lamps = st.lamps.filter((l) => l.id !== lamp.id);
          st.dirty = false;
          deselect();
          toast(`Deleted ${lamp.name}`);
        })
      );
    }
    showReading();
  }

  async function saveLamp() {
    const f = $("#lamp-form").elements;
    const r = st.draft;
    const round = (v) => Math.round(v * 1e5) / 1e5;
    const x = round(r.x);
    const y = round(r.y);
    const payload = {
      name: f.name.value,
      x,
      y,
      w: Math.min(round(r.w), round(1 - x)),
      h: Math.min(round(r.h), round(1 - y)),
      metric: f.metric.value,
      threshold: Number(f.threshold.value),
      hysteresis: Number(f.hysteresis.value),
      confirm_seconds: Number(f.confirm_seconds.value),
      email_to: f.email_to.value,
      notes: f.notes.value,
      notify_on_restore: f.notify_on_restore.checked,
      repeat_minutes: Number(f.repeat_minutes.value),
      enabled: f.enabled.checked,
    };
    const saved =
      st.selected === "new"
        ? await api("POST", `/api/cameras/${cameraId}/lamps`, payload)
        : await api("PUT", `/api/lamps/${st.selected}`, payload);
    st.lamps = await api("GET", `/api/cameras/${cameraId}/lamps`);
    st.dirty = false;
    st.selected = null;
    selectLamp(saved.id);
    toast(`Saved ${saved.name}`);
  }

  /* ---- live brightness reading for the box being edited ---- */

  function showReading() {
    const form = $("#lamp-form");
    if (!form) return;
    const threshold = Number(form.elements.threshold.value);
    const hysteresis = Number(form.elements.hysteresis.value);
    const value = st.reading;
    $("#reading-value").textContent = value == null ? "—" : Math.round(value);
    const verdict = $("#reading-verdict");
    if (value == null) {
      verdict.className = "badge";
      verdict.textContent = st.readingError ? "NO IMAGE" : "";
    } else if (value >= threshold) {
      verdict.className = "badge badge-on";
      verdict.textContent = "READS ON";
    } else if (value < threshold - hysteresis) {
      verdict.className = "badge badge-off";
      verdict.textContent = "READS OFF";
    } else {
      verdict.className = "badge badge-warn";
      verdict.textContent = "IN BAND";
    }
    $("#reading-meter").innerHTML = meter(value, threshold, hysteresis);
    $("#reading-hint").textContent = st.readingError
      ? st.readingError
      : "Measured from the latest camera image (0 = black, 255 = full brightness).";
  }

  let measureTimer = null;
  let measureSeq = 0;
  teardown.push(() => clearTimeout(measureTimer));

  function scheduleMeasure(delay = 250) {
    clearTimeout(measureTimer);
    measureTimer = setTimeout(measureNow, delay);
  }

  async function measureNow() {
    const form = $("#lamp-form");
    const r = st.draft;
    if (!r || r.w <= 0 || r.h <= 0) return;
    const seq = ++measureSeq;
    const x = Math.min(r.x, 0.99999);
    const y = Math.min(r.y, 0.99999);
    const body = {
      x,
      y,
      w: Math.max(1e-5, Math.min(r.w, 1 - x)),
      h: Math.max(1e-5, Math.min(r.h, 1 - y)),
      metric: form ? form.elements.metric.value : "peak",
    };
    try {
      const result = await api("POST", `/api/cameras/${cameraId}/measure`, body);
      if (seq !== measureSeq || !alive()) return;
      st.reading = result.value;
      st.readingError = null;
    } catch (err) {
      if (seq !== measureSeq || !alive()) return;
      st.reading = null;
      st.readingError = err.message;
    }
    showReading();
  }

  /* ---- camera settings ---- */

  const cameraForm = $("#camera-form");
  bindCameraForm(cameraForm, camera, async (payload) => {
    st.camera = await api("PUT", `/api/cameras/${cameraId}`, payload);
    $("#cam-title").textContent = st.camera.name;
    toast("Camera saved");
    loadImage(true);
  });
  $('[data-action="delete-camera"]', cameraForm).addEventListener("click", () =>
    withBusy(cameraForm, async () => {
      const count = st.lamps.length;
      const extra = count ? ` and its ${count} lamp${count === 1 ? "" : "s"}` : "";
      if (!confirm(`Delete camera “${st.camera.name}”${extra}? This can't be undone.`)) return;
      await api("DELETE", `/api/cameras/${cameraId}`);
      toast(`Deleted ${st.camera.name}`);
      location.hash = "#/cameras";
    })
  );

  /* ---- start ---- */

  renderLampPanel();
  renderLampList();
  if (lampParam) selectLamp(Number(lampParam));

  poll(3000, async () => {
    const status = await api("GET", "/api/status");
    if (!alive()) return;
    st.live = new Map(status.lamps.map((l) => [l.id, l]));
    const cam = status.cameras.find((c) => c.id === cameraId);
    if (cam) {
      const [cls, label] = cameraState(cam);
      const badge = $("#cam-badge");
      badge.className = `badge badge-${cls}`;
      badge.textContent = label;
      $("#cam-error").innerHTML =
        cam.last_error && cam.online !== true ? `<div class="banner warn">${esc(cam.last_error)}</div>` : "";
    }
    renderLampList();
    draw();
    if (st.draft && !drag) measureNow();
  });
  poll(Math.max(5, camera.poll_interval) * 1000, () => loadImage());
}

/* ------------------------------------------------------------------- events */

async function renderEvents() {
  app.innerHTML = `<div class="page-head"><h1>Events</h1><span class="muted small">Most recent 300</span></div><div id="events"></div>`;
  poll(10000, async () => {
    $("#events").innerHTML = eventsTable(await api("GET", "/api/events?limit=300"));
  });
}

/* ----------------------------------------------------------------- settings */

const SETTINGS_FIELDS = [
  "protect_host",
  "protect_verify_ssl",
  "smtp_host",
  "smtp_port",
  "smtp_security",
  "smtp_username",
  "email_from",
  "admin_emails",
  "subject_prefix",
  "public_url",
  "camera_offline_seconds",
];
const SECRET_SETTINGS = ["protect_api_key", "smtp_password"];

async function renderSettings() {
  const settings = await api("GET", "/api/settings");
  app.innerHTML = `
    <div class="page-head"><h1>Settings</h1></div>
    <form id="settings-form" class="form" autocomplete="off">
      <section class="card">
        <h2>UniFi Protect</h2>
        <p class="hint" style="margin-top:0">Needed for cameras that use the “UniFi Protect” source. Create an API key in UniFi OS under
          Settings → Control Plane → Integrations.</p>
        <label>Console address <input name="protect_host" placeholder="192.168.1.1 or https://unifi.example.lan"></label>
        <label>API key <input name="protect_api_key" type="password" autocomplete="new-password"></label>
        <label class="check"><input type="checkbox" name="protect_verify_ssl"> Verify the console's TLS certificate (leave off for the default self-signed certificate)</label>
        <div class="actions">
          <button type="button" data-action="test-protect">Save and test connection</button>
          <span class="result" id="protect-result"></span>
        </div>
      </section>

      <section class="card">
        <h2>Email</h2>
        <div class="grid2">
          <label>SMTP server <input name="smtp_host" placeholder="smtp.example.com"></label>
          <label>Port <input name="smtp_port" type="number" min="1" max="65535" required></label>
        </div>
        <label>Security
          <select name="smtp_security">
            <option value="starttls">STARTTLS (usually port 587)</option>
            <option value="ssl">SSL/TLS (usually port 465)</option>
            <option value="none">None (usually port 25, internal relays only)</option>
          </select>
        </label>
        <div class="grid2">
          <label>Username <input name="smtp_username" autocomplete="off"></label>
          <label>Password <input name="smtp_password" type="password" autocomplete="new-password"></label>
        </div>
        <label>From address <input name="email_from" placeholder="LampMonitor &lt;lampmonitor@example.com&gt;"></label>
        <label>Admin recipients <textarea name="admin_emails" rows="2" placeholder="facilities@example.com"></textarea></label>
        <p class="hint">Get camera-offline alerts, and lamp alerts for lamps that don't list their own recipients.</p>
        <label>Subject prefix <input name="subject_prefix" maxlength="50"></label>
        <div class="row" style="margin-top:.25rem">
          <input name="test_to" placeholder="Send test to… (blank = admin recipients)">
          <button type="button" data-action="test-email">Save and send test email</button>
        </div>
        <p class="result" id="email-result"></p>
      </section>

      <section class="card">
        <h2>Monitoring</h2>
        <label>Report a camera as offline after (seconds)
          <input name="camera_offline_seconds" type="number" min="10" max="86400" required>
        </label>
        <p class="hint">If a camera stops sending images for this long, an alert goes to the admin recipients and to the recipients of every lamp it watches.</p>
        <label>LampMonitor web address for email links (optional)
          <input name="public_url" placeholder="http://lampmonitor.example.lan:8080">
        </label>
      </section>

      <div class="actions"><button class="primary" type="submit">Save settings</button></div>
    </form>`;

  const form = $("#settings-form");
  const f = form.elements;

  function fill(s) {
    for (const key of SETTINGS_FIELDS) {
      if (f[key].type === "checkbox") f[key].checked = s[key];
      else f[key].value = s[key];
    }
    for (const key of SECRET_SETTINGS) {
      f[key].value = "";
      f[key].placeholder = s[`${key}_set`] ? "Saved. Leave blank to keep it." : "";
    }
  }

  async function save() {
    const payload = {};
    for (const key of SETTINGS_FIELDS) {
      const el = f[key];
      payload[key] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
    }
    for (const key of SECRET_SETTINGS) if (f[key].value) payload[key] = f[key].value;
    fill(await api("PUT", "/api/settings", payload));
  }

  function result(id, message, ok) {
    const el = $(id);
    el.className = `result ${ok ? "ok" : "error"}`;
    el.textContent = message;
  }

  fill(settings);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    withBusy(form, async () => {
      await save();
      toast("Settings saved");
    });
  });
  $('[data-action="test-protect"]').addEventListener("click", () =>
    withBusy(form, async () => {
      result("#protect-result", "Connecting…", true);
      try {
        await save();
        const cameras = await api("GET", "/api/protect/cameras");
        result("#protect-result", `Connected. Found ${cameras.length} camera${cameras.length === 1 ? "" : "s"}.`, true);
      } catch (err) {
        result("#protect-result", err.message, false);
      }
    })
  );
  $('[data-action="test-email"]').addEventListener("click", () =>
    withBusy(form, async () => {
      result("#email-result", "Sending…", true);
      try {
        await save();
        const sent = await api("POST", "/api/settings/test-email", { to: f.test_to.value });
        result("#email-result", `Test email sent to ${sent.sent_to.join(", ")}.`, true);
      } catch (err) {
        result("#email-result", err.message, false);
      }
    })
  );
}

route();
