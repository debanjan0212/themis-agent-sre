// Themis dashboard. No build step and no framework: one module.
// Agent, tool and journey names come from telemetry, so they are untrusted. Every piece of text goes through
// h() / s(), which use text nodes and setAttribute. Nothing here ever builds HTML from a string.

const view = document.getElementById("view");
const statusBar = document.getElementById("status");
const HUES = ["#58d5ff", "#a67cff", "#ff7eb6", "#ffb54a", "#c6f36b", "#5cf2c0", "#3fe3ea", "#7d9bff", "#e07bff", "#ff9a6b"];
const SLI_NAMES = { task_success: "Tasks done", handoff_success: "Clean handoffs", safe_actions: "Safe actions", cost: "Within cost" };
let key = "";
let timer = null;
const hues = new Map();

function build(el, attrs, kids) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") for (const [p, x] of Object.entries(v)) p.startsWith("--") ? el.style.setProperty(p, x) : (el.style[p] = x);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(3)) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}
const h = (tag, attrs, ...kids) => build(document.createElement(tag), attrs, kids);
// replaceChildren() prints "null" for a child that is not there; this drops it, as h() does
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(3).filter((k) => k != null && k !== false));
const s = (tag, attrs, ...kids) => build(document.createElementNS("http://www.w3.org/2000/svg", tag), attrs, kids);

const hue = (agent) => {
  if (!hues.has(agent)) hues.set(agent, HUES[hues.size % HUES.length]);
  return hues.get(agent);
};
const pct = (x, d = 1) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const ms = (x) => (x == null ? "–" : x >= 1000 ? `${(x / 1000).toFixed(2)} s` : `${Math.round(x)} ms`);
const usd = (x) => (x == null ? "–" : x >= 1 ? `$${x.toFixed(2)}` : `$${x.toFixed(4)}`);
const clock = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour12: false }) : "–");
const ago = (t) => {
  const d = Math.max(0, Date.now() / 1000 - t);
  return d < 90 ? `${Math.round(d)}s ago` : d < 5400 ? `${Math.round(d / 60)} min ago` : `${Math.round(d / 3600)} h ago`;
};
const chip = (text, tone) => h("span", { class: `state ${tone || text}` }, text);

async function api(path, opts = {}) {
  const headers = { Authorization: `Bearer ${key}` };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    askForKey("That key was not accepted. Paste a key with the read scope.");
    throw new Error("Not signed in");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `The request failed (${res.status}).`);
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body ?? {}) });

function every(period, fn) {
  const note = h("p", { class: "error", role: "alert" });
  const run = () =>
    fn().then(
      () => note.remove(),
      (e) => {
        note.textContent = `Could not refresh: ${e.message}`;
        if (!note.isConnected && view.isConnected) view.prepend(note);
      },
    );
  run();
  timer = setInterval(run, period);
}

// ---------------------------------------------------------------- sign in

function askForKey(message) {
  clearInterval(timer);
  cancelAnimationFrame(frame);
  const input = h("input", { type: "password", autocomplete: "off", "aria-label": "API key", placeholder: "thm_…" });
  const open = () => {
    key = input.value.trim();
    try {
      sessionStorage.setItem("themis.key", key);
    } catch {
      /* private mode: the key lives for this page only */
    }
    route();
  };
  fill(
    view,
    h(
      "div",
      { class: "signin stack" },
      h("h1", {}, "Open the dashboard"),
      h("p", { class: "mute" }, message || "Paste an API key. `themis demo` prints a link that already contains one; `themis key` creates one."),
      h("div", { class: "row" }, input, h("button", { class: "go", onclick: open }, "Open dashboard")),
    ),
  );
  input.addEventListener("keydown", (e) => e.key === "Enter" && open());
  input.focus();
}

// ---------------------------------------------------------------- fleet: the orrery

const RADIUS = { autonomous: 200, supervised: 125, suspended: 80 };
const polar = (deg, r) => [Math.cos((deg * Math.PI) / 180) * r, Math.sin((deg * Math.PI) / 180) * r];
const VIEW = { x: -372, y: -340, w: 744, h: 680 };
const calm = matchMedia("(prefers-reduced-motion: reduce)");
let frame = 0; // the fleet map's animation; every page change stops it

// glow for lines, bloom for halos, and the sphere at the centre
function lights() {
  return s(
    "defs",
    {},
    s(
      "filter",
      { id: "glow", x: "-50%", y: "-50%", width: "200%", height: "200%" },
      s("feGaussianBlur", { stdDeviation: "2.2", result: "b" }),
      s("feMerge", {}, s("feMergeNode", { in: "b" }), s("feMergeNode", { in: "SourceGraphic" })),
    ),
    s("filter", { id: "bloom", x: "-100%", y: "-100%", width: "300%", height: "300%" }, s("feGaussianBlur", { stdDeviation: "7" })),
    s(
      "radialGradient",
      { id: "orb", cx: "50%", cy: "50%", r: "50%" },
      s("stop", { offset: "0%", "stop-color": "#140d33" }),
      s("stop", { offset: "64%", "stop-color": "#2a1d68" }),
      s("stop", { offset: "88%", "stop-color": "#8f6dff" }),
      s("stop", { offset: "100%", "stop-color": "#e9e0ff" }),
    ),
    HUES.map((c, i) =>
      s(
        "radialGradient",
        { id: `halo-${i}` },
        s("stop", { offset: "0%", "stop-color": c, "stop-opacity": 0.55 }),
        s("stop", { offset: "100%", "stop-color": c, "stop-opacity": 0 }),
      ),
    ),
  );
}

// the rings around the map. They carry no data: they turn so the map reads as a running instrument.
// Each moving ring is its own <svg> layer behind the map, turned as a whole, so the browser can move it without
// redrawing the map (and the map's glow) on every frame.
function dial() {
  const layer = (cls, tilt, ...kids) =>
    s(
      "svg",
      { class: cls, viewBox: `${VIEW.x} ${VIEW.y} ${VIEW.w} ${VIEW.h}`, "data-tilt": tilt, style: { "--tilt": `${tilt}deg` } },
      s("g", { class: "instrument" }, kids),
    );
  const spokes = Array.from({ length: 90 }, (_, i) => {
    const [x1, y1] = polar(i * 4, 214);
    const [x2, y2] = polar(i * 4, i % 5 ? 221 : 229);
    return s("line", { x1, y1, x2, y2 });
  });
  const bristles = Array.from({ length: 72 }, (_, i) => {
    const len = 3 + ((i * 37) % 11); // fixed jitter, so every screenshot of the map is the same
    const [x1, y1] = polar(i * 5, 37);
    const [x2, y2] = polar(i * 5, 37 + len);
    return s("line", { x1, y1, x2, y2 });
  });
  const ring = (r, cls) => s("circle", { class: cls, r });
  return h(
    "div",
    { class: "dial", "aria-hidden": "true" },
    layer("still", 0, s("g", { class: "spokes" }, spokes)),
    layer("breathe", 0, ring(58, "aura")),
    layer("gyro a", -24, s("ellipse", { rx: 318, ry: 86 })),
    layer("gyro b", 36, s("ellipse", { rx: 318, ry: 86 })),
    layer("gyro c", 96, s("ellipse", { rx: 318, ry: 86 })),
    layer("turn slow", 0, ring(266, "band")),
    layer("turn rev", 0, ring(252, "ticks")),
    layer("turn", 0, ring(238, "band thin")),
    layer("turn fast", 0, s("g", { class: "bristles" }, bristles)),
  );
}

// the human at the centre: a sphere lit from the rim, with the count of what waits for a person
function human(waiting) {
  return s(
    "g",
    { class: "human" },
    s("circle", { r: 30, fill: "url(#orb)" }),
    s("circle", { class: "core", r: 30 }),
    waiting,
    s("text", { class: "core-sub", y: 15 }, "waiting"),
  );
}

// a little history under each vital, kept in the page while it is open
function sparkline(values, color) {
  const w = 120;
  const hgt = 26;
  const svg = s("svg", { class: "spark", viewBox: `0 0 ${w} ${hgt}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  if (values.length < 2) return svg;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * w, hgt - 2 - ((v - lo) / span) * (hgt - 6)]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  svg.append(s("path", { class: "fill", d: `${d} L${w},${hgt} L0,${hgt} Z`, fill: color }), s("path", { class: "line", d, stroke: color }));
  return svg;
}

// journeys flowing along the lines: one particle per few calls, drawn on a canvas under the map
function flow(canvas, svg) {
  const ctx = canvas.getContext("2d");
  let lines = [];
  let sparks = [];
  let last = performance.now();
  let fit = { k: 1, ox: 0, oy: 0 };
  const size = () => {
    const box = svg.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(box.width * dpr));
    canvas.height = Math.max(1, Math.round(box.height * dpr));
    canvas.style.width = `${box.width}px`;
    canvas.style.height = `${box.height}px`;
    const k = Math.min(box.width / VIEW.w, box.height / VIEW.h) * dpr;
    fit = { k, ox: (canvas.width - VIEW.w * k) / 2, oy: (canvas.height - VIEW.h * k) / 2 };
  };
  new ResizeObserver(size).observe(svg);
  const point = (l, t) => {
    const u = 1 - t;
    const x = u * u * l.x0 + 2 * u * t * l.cx + t * t * l.x1;
    const y = u * u * l.y0 + 2 * u * t * l.cy + t * t * l.y1;
    return [fit.ox + (x - VIEW.x) * fit.k, fit.oy + (y - VIEW.y) * fit.k];
  };
  const draw = (now) => {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    for (const l of lines) {
      l.due += l.rate * dt;
      while (l.due >= 1 && sparks.length < 700) {
        l.due -= 1;
        sparks.push({ l, t: 0, v: 0.32 + Math.random() * 0.3 });
      }
    }
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.globalCompositeOperation = "lighter";
    ctx.lineCap = "round";
    sparks = sparks.filter((p) => (p.t += p.v * dt) < 1);
    for (const p of sparks) {
      const [x, y] = point(p.l, p.t);
      const [tx, ty] = point(p.l, Math.max(0, p.t - 0.07));
      const fade = Math.sin(Math.PI * p.t);
      ctx.globalAlpha = 0.55 * fade;
      ctx.strokeStyle = p.l.color;
      ctx.lineWidth = 1.4 * fit.k;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.globalAlpha = 0.95 * fade;
      ctx.fillStyle = p.l.color;
      ctx.beginPath();
      ctx.arc(x, y, 1.5 * fit.k, 0, 2 * Math.PI);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    frame = requestAnimationFrame(draw);
  };
  return {
    set(next) {
      const before = new Map(lines.map((l) => [l.id, l.due]));
      lines = next.map((l) => ({ ...l, due: before.get(l.id) ?? Math.random() }));
      sparks = sparks.filter((p) => lines.some((l) => l.id === p.l.id));
    },
    start() {
      cancelAnimationFrame(frame);
      if (!calm.matches) frame = requestAnimationFrame(draw);
    },
  };
}

function fleet() {
  const lanes = s(
    "g",
    {},
    s("circle", { class: "lane", r: RADIUS.autonomous }),
    s("circle", { class: "lane watched", r: RADIUS.supervised }),
  );
  const edges = s("g");
  const deps = s("g");
  const bodies = s("g");
  const waiting = s("text", { class: "core-text", y: 5 }, "0");
  const svg = s(
    "svg",
    {
      class: "map",
      viewBox: `${VIEW.x} ${VIEW.y} ${VIEW.w} ${VIEW.h}`,
      role: "img",
      "aria-label": "Fleet map. Agents sit closer to the human at the centre as they lose autonomy.",
    },
    lights(),
    lanes,
    edges,
    deps,
    human(waiting),
    bodies,
  );
  const canvas = h("canvas", { "aria-hidden": "true" });
  const particles = flow(canvas, svg);
  const banner = h("div", {});
  const vitals = h("div", { class: "vitals" });
  const ladder = h("ul", { class: "ladder" });
  const feed = h("ul", { class: "feed" });
  view.append(
    h(
      "div",
      { class: "stack" },
      banner,
      h(
        "div",
        { class: "fleet" },
        h(
          "div",
          { class: "panel orrery" },
          h("div", { class: "stage" }, dial(), canvas, svg),
          h(
            "p",
            { class: "legend" },
            "The human is at the centre. Outer ring: the agent acts alone. Inner ring: a human approves its risky actions. Docked at the centre: suspended. Circles on the rim are tools, diamonds are models. The moving light is traffic, red where calls fail.",
          ),
        ),
        h(
          "div",
          { class: "stack" },
          h("div", { class: "panel" }, h("h2", {}, "Right now"), vitals),
          h("div", { class: "panel" }, h("h2", {}, "Autonomy"), ladder),
          h("div", { class: "panel" }, h("h2", {}, "What just happened"), feed),
        ),
      ),
    ),
  );
  particles.start();

  const nodes = new Map();
  const history = new Map();
  let depAngles = new Map();

  every(1500, async () => {
    const o = await api("/api/overview");
    showStatus(o);
    const names = o.agents.map((a) => a.name);
    const angle = new Map(names.map((n, i) => [n, -90 + (i * 360) / Math.max(names.length, 1)]));
    const at = new Map(o.agents.map((a) => [a.name, polar(angle.get(a.name), RADIUS[a.autonomy])]));

    for (const a of o.agents) {
      let node = nodes.get(a.name);
      if (!node) {
        const mode = s("text", { class: "mode", y: 47 });
        const name = s("text", { y: 35 }, a.name);
        const g = s(
          "g",
          { class: "body", tabindex: 0, role: "link", onclick: () => (location.hash = `#/agents/${encodeURIComponent(a.name)}`) },
          s("circle", { class: "halo", r: 30, fill: `url(#halo-${HUES.indexOf(hue(a.name))})` }),
          s("circle", { class: "ring", r: 24 }),
          s("circle", { class: "disc", r: 17, fill: hue(a.name), "fill-opacity": 0.16, stroke: hue(a.name) }),
          s("circle", { r: 3.2, fill: hue(a.name) }),
          name,
          mode,
        );
        g.addEventListener("keydown", (e) => e.key === "Enter" && g.dispatchEvent(new Event("click")));
        node = { g, mode, name };
        nodes.set(a.name, node);
        bodies.append(g);
      }
      const [x, y] = at.get(a.name);
      node.g.setAttribute("class", `body ${a.autonomy}`);
      node.g.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
      node.mode.textContent = a.autonomy === "autonomous" ? "" : a.autonomy;
      // near the centre the name goes on the side away from the human, so it never sits on the core
      const above = a.autonomy !== "autonomous" && y < -20;
      node.name.setAttribute("y", above ? -36 : 35);
      node.mode.setAttribute("y", above ? -26 : 47);
    }

    // tools and models on the outer ring, each near the first agent that uses it
    const held = new Set(o.quarantine.map((q) => q.component));
    const depKeys = [...new Set(o.edges.filter((e) => e.kind !== "handoff").map((e) => `${e.kind}:${e.to}`))];
    if (depKeys.length !== depAngles.size || depKeys.some((k) => !depAngles.has(k))) {
      const first = (k) => Math.min(...o.edges.filter((e) => `${e.kind}:${e.to}` === k).map((e) => angle.get(e.from) ?? 0));
      const sorted = depKeys.sort((a, b) => first(a) - first(b) || a.localeCompare(b));
      depAngles = new Map(sorted.map((k, i) => [k, -90 + ((i + 0.5) * 360) / sorted.length]));
    }
    fill(
      deps,
      ...[...depAngles].map(([k, deg]) => {
        const [x, y] = polar(deg, 285);
        const [tx, ty] = polar(deg, 297);
        const isModel = k.startsWith("model:");
        const mark = isModel
          ? s("rect", { class: "mark", x: x - 5, y: y - 5, width: 10, height: 10, transform: `rotate(45 ${x} ${y})` })
          : s("circle", { class: "mark", cx: x, cy: y, r: 4.5 });
        const anchor = Math.abs(tx) < 40 ? "middle" : tx > 0 ? "start" : "end";
        const label = k.slice(k.indexOf(":") + 1) + (held.has(k) ? " (quarantined)" : "");
        return s("g", { class: `dep ${held.has(k) ? "quarantined" : ""}` }, mark, s("text", { x: tx, y: ty + (ty > 250 ? 8 : 3), "text-anchor": anchor }, label));
      }),
    );

    // what uses what: curves that bow towards the centre, and the traffic that runs along them
    const most = Math.max(1, ...o.edges.map((e) => e.count));
    const flowing = [];
    fill(
      edges,
      ...o.edges.map((e) => {
        const from = at.get(e.from);
        const bad = e.errors / e.count > 0.05;
        const strength = e.count / most;
        if (!from) return null;
        let to;
        let bow;
        if (e.kind === "handoff") {
          to = at.get(e.to);
          if (!to) return null;
          bow = 0.72;
        } else {
          const deg = depAngles.get(`${e.kind}:${e.to}`);
          if (deg == null) return null;
          to = polar(deg, 279);
          bow = 0.8;
        }
        const c = [((from[0] + to[0]) / 2) * bow, ((from[1] + to[1]) / 2) * bow];
        const color = bad ? "#ff4f6e" : hue(e.from);
        flowing.push({ id: `${e.from}>${e.kind}:${e.to}`, x0: from[0], y0: from[1], cx: c[0], cy: c[1], x1: to[0], y1: to[1], color, rate: 0.25 + strength * 4.5 });
        const d = `M${from[0].toFixed(1)},${from[1].toFixed(1)} Q${c[0].toFixed(1)},${c[1].toFixed(1)} ${to[0].toFixed(1)},${to[1].toFixed(1)}`;
        const opacity = e.kind === "handoff" ? 0.3 + strength * 0.45 : 0.1 + strength * 0.28;
        return s("path", { class: `edge ${bad ? "bad" : ""}`, d, stroke: hue(e.from), "stroke-opacity": opacity, "stroke-width": e.kind === "handoff" ? 1.4 : 0.9 });
      }),
    );
    particles.set(flowing);
    waiting.textContent = o.pending_approvals;

    fill(
      banner,
      o.experiments.length
        ? h(
            "div",
            { class: "banner" },
            o.experiments.map((x) => h("div", {}, `Chaos experiment running: ${x.name}. ${Math.max(0, Math.round(x.started_at + x.duration_s - o.at))}s left.`)),
          )
        : null,
    );
    const v = o.vitals;
    const vital = (id, value, shown, label, color) => {
      const past = history.get(id) || [];
      if (value != null) past.push(value);
      history.set(id, past.slice(-80));
      return h("div", { class: "vital" }, h("span", { class: "num" }, shown), h("span", {}, label), sparkline(history.get(id), color));
    };
    fill(
      vitals,
      vital("rate", v.per_min ?? 0, v.per_min ?? 0, "journeys a minute", "#58d5ff"),
      vital("ok", v.success, pct(v.success), "ended well", "#5cf2c0"),
      vital("p95", v.p95_ms, ms(v.p95_ms), "95th percentile time", "#a67cff"),
      vital("spend", v.cost_per_min, usd(v.cost_per_min), "model spend a minute", "#ffb54a"),
      vital("failed", v.failed, pct(v.failed), "failed", "#ff4f6e"),
      vital("blocked", v.blocked, pct(v.blocked), "stopped safely", "#ff7a3a"),
    );
    fill(
      ladder,
      ...o.agents.map((a) => {
        const worst = [...a.slis].sort((p, q) => q.burn_long - p.burn_long)[0];
        const why = a.autonomy !== "autonomous" ? a.reason : worst && worst.burn_long >= 1 ? `${SLI_NAMES[worst.sli]} burning ${worst.burn_long}x` : "";
        return h(
          "li",
          {},
          h("span", { class: "dot", style: { background: hue(a.name), color: hue(a.name) } }),
          h("a", { href: `#/agents/${encodeURIComponent(a.name)}` }, a.name),
          chip(a.autonomy),
          h("span", { class: "why", title: why || "" }, why || ""),
        );
      }),
    );
    fill(
      feed,
      ...(o.events.length
        ? o.events.map((e) => h("li", {}, h("time", {}, clock(e.at)), e.text, e.detail ? h("div", { class: "mute" }, e.detail) : null))
        : [h("li", { class: "empty" }, "Nothing yet. Demotions, quarantines and chaos experiments will show up here.")]),
    );
  });
}

function showStatus(o) {
  fill(
    statusBar,
    o.window_scale !== 1 ? h("span", { title: "SLO windows are compressed for the demo" }, `Time runs ${o.window_scale} times faster`) : null,
    h("a", { href: "#/incidents" }, h("span", { class: o.pending_approvals ? "lit" : "" }, o.pending_approvals), " waiting for a human"),
    h("a", { href: "#/incidents" }, h("span", { class: o.open_incidents ? "lit" : "" }, o.open_incidents), " open incidents"),
  );
}

// ---------------------------------------------------------------- journeys (the RUM view)

function journeys(openId) {
  let outcome = "";
  const table = h("div", { class: "panel scroll" });
  const detail = h("div", { class: "stack" }, h("div", { class: "panel empty" }, "Pick a journey to see every agent, model, tool and pod it used."));
  const filters = h("div", { class: "row" });
  const drawFilters = () =>
    fill(
      filters,
      ...[["", "All"], ["failed", "Failed"], ["blocked", "Stopped safely"], ["success", "Ended well"]].map(([value, label]) =>
        h("button", { class: outcome === value ? "on" : "", onclick: () => ((outcome = value), drawFilters(), refresh()) }, label),
      ),
    );
  drawFilters();
  view.append(
    h(
      "div",
      { class: "stack" },
      h("div", { class: "row" }, h("h1", { class: "grow" }, "Journeys"), filters),
      h("p", { class: "mute" }, "One row is one thing a user asked for, followed across every agent that worked on it."),
      h("div", { class: "split" }, table, detail),
    ),
  );

  async function refresh() {
    const { journeys: rows } = await api(`/api/journeys?limit=40&outcome=${outcome}`);
    if (!rows.length) return fill(table, h("p", { class: "empty" }, "No journeys match. Start the world with `themis demo`."));
    fill(
      table,
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, ...["Ended", "User", "Journey", "Took", "Cost", "Result", "Cause"].map((t) => h("th", {}, t)))),
        h(
          "tbody",
          {},
          rows.map((j) =>
            h(
              "tr",
              { class: "pick", tabindex: 0, onclick: () => open(j.trace_id), onkeydown: (e) => e.key === "Enter" && open(j.trace_id) },
              h("td", {}, clock(j.end)),
              h("td", {}, j.user_id || "–"),
              h("td", {}, j.name),
              h("td", { class: "n" }, ms(j.duration_ms)),
              h("td", { class: "n" }, usd(j.cost)),
              h("td", {}, chip(j.outcome === "blocked" ? "stopped safely" : j.outcome, j.outcome)),
              h("td", {}, j.culprit || (j.outcome === "success" ? "" : "not known yet")),
            ),
          ),
        ),
      ),
    );
  }

  async function open(traceId) {
    let d;
    try {
      d = await api(`/api/journeys/${encodeURIComponent(traceId)}`);
    } catch (e) {
      return fill(detail, h("div", { class: "panel error" }, e.message));
    }
    const j = d.journey;
    const total = Math.max(j.duration_ms, 1);
    const depth = new Map();
    for (const sp of d.spans) depth.set(sp.span_id, sp.parent_id && depth.has(sp.parent_id) ? depth.get(sp.parent_id) + 1 : 0);
    const flags = (sp) =>
      [
        sp.attrs["themis.gate.decision"] === "deny" && "denied by policy",
        sp.attrs["themis.gate.decision"] === "denied_by_human" && "rejected by a human",
        sp.attrs["themis.gate.decision"] === "approved_by_human" && "approved by a human",
        sp.attrs["themis.check.passed"] === false && "check failed",
        sp.attrs["themis.handoff.valid"] === false && "invalid handoff",
        sp.attrs["themis.fallback"] && "fallback",
        sp.error_type,
      ].filter(Boolean);
    fill(
      detail,
      h(
        "div",
        { class: "panel stack" },
        h("div", { class: "row" }, h("h2", { class: "grow" }, j.name), chip(j.outcome === "blocked" ? "stopped safely" : j.outcome, j.outcome)),
        h(
          "dl",
          { class: "kv" },
          h("dt", {}, "User"), h("dd", {}, `${j.user_id || "–"}, session ${j.session_id || "–"}`),
          h("dt", {}, "Took"), h("dd", {}, `${ms(j.duration_ms)} across ${j.span_count} steps`),
          h("dt", {}, "Spent"), h("dd", {}, `${usd(j.cost)} on ${j.tokens.toLocaleString()} tokens`),
          j.culprit && h("dt", {}, "Cause"),
          j.culprit && h("dd", {}, h("strong", {}, j.culprit), ` (${j.culprit_source}, ${pct(j.confidence, 0)} confident). `, j.culprit_reason),
        ),
      ),
      h(
        "div",
        { class: "panel stack" },
        h("h2", {}, "What it used"),
        h(
          "div",
          { class: "used" },
          d.uses.map((u) =>
            h(
              "div",
              {},
              h("strong", {}, h("span", { class: "dot", style: { background: hue(u.agent), color: hue(u.agent) } }), u.agent),
              h("div", { class: "mute" }, u.pod ? `${u.pod} on ${u.node || "?"}` : "pod not reported"),
              u.models.length ? h("div", {}, `model ${u.models.join(", ")}`) : null,
              u.tools.length ? h("div", {}, `tools ${u.tools.join(", ")}`) : null,
              h("div", { class: "mute" }, `${usd(u.cost)}, ${u.tokens.toLocaleString()} tokens`),
            ),
          ),
        ),
      ),
      h(
        "div",
        { class: "panel stack" },
        h("h2", {}, "Step by step"),
        h(
          "div",
          { class: "water" },
          d.spans.map((sp) => {
            const wrong = !sp.ok || sp.attrs["themis.check.passed"] === false || sp.attrs["themis.handoff.valid"] === false;
            const label = sp.kind === "other" ? sp.name : `${sp.kind} ${sp.target || sp.agent || ""}`;
            const notes = flags(sp);
            return [
              h("div", { class: `name ${wrong ? "bad" : ""}`, style: { paddingLeft: `${depth.get(sp.span_id) * 0.8}rem` }, title: sp.name }, label, notes.length ? ` (${notes.join(", ")})` : ""),
              h(
                "div",
                { class: "lane-row" },
                h("i", {
                  style: {
                    left: `${(sp.offset_ms / total) * 100}%`,
                    width: `${Math.max((sp.duration_ms / total) * 100, 0.4)}%`,
                    background: wrong ? "var(--held)" : sp.agent ? hue(sp.agent) : "var(--amber)",
                    color: wrong ? "var(--held)" : sp.agent ? hue(sp.agent) : "var(--amber)",
                  },
                }),
              ),
              h("div", { class: "n mute" }, ms(sp.duration_ms)),
            ];
          }),
        ),
      ),
    );
  }

  every(2500, refresh);
  if (openId) open(openId);
}

// ---------------------------------------------------------------- agents

const budgetBar = (left) => h("div", { class: `bar ${left <= 0 ? "out" : left < 0.3 ? "low" : ""}` }, h("i", { style: { width: `${Math.max(0, Math.min(1, left)) * 100}%` } }));

function sliTable(slis) {
  if (!slis.length) return h("p", { class: "empty" }, "No journeys in the SLO window right now.");
  return h(
    "table",
    {},
    h("thead", {}, h("tr", {}, h("th", {}, "Indicator"), ...["Now", "Target", "Burn 1h", "Burn 5m"].map((t) => h("th", { class: "n" }, t)), h("th", {}, "Budget left"))),
    h(
      "tbody",
      {},
      slis.map((x) =>
        h(
          "tr",
          {},
          h("td", {}, SLI_NAMES[x.sli] || x.sli, x.firing ? [" ", chip(x.firing === "page" ? "burning fast" : "slow burn", x.firing === "page" ? "failed" : "warning")] : null),
          h("td", { class: "n" }, pct(x.value)),
          h("td", { class: "n" }, pct(x.target)),
          h("td", { class: "n" }, `${x.burn_long}x`),
          h("td", { class: "n" }, `${x.burn_short}x`),
          h("td", { style: { minWidth: "6rem" } }, budgetBar(x.budget_left), h("span", { class: "mute" }, pct(Math.max(x.budget_left, 0), 0))),
        ),
      ),
    ),
  );
}

function agents(name) {
  if (name) return agentDetail(name);
  const cards = h("div", { class: "cards" });
  view.append(
    h("div", { class: "stack" }, h("h1", {}, "Agents"), h("p", { class: "mute" }, "Each agent has its own targets. Spend the error budget too fast and it loses autonomy until it behaves again."), cards),
  );
  every(2500, async () => {
    const o = await api("/api/overview");
    showStatus(o);
    fill(
      cards,
      ...o.agents.map((a) =>
        h(
          "a",
          { class: "stack", href: `#/agents/${encodeURIComponent(a.name)}` },
          h("div", { class: "row" }, h("h3", { class: "grow" }, h("span", { class: "dot", style: { background: hue(a.name), color: hue(a.name) } }), a.name), chip(a.autonomy)),
          h("div", { class: "mute" }, `${a.model || "model not declared"}. ${a.tasks ?? 0} tasks lately, ${a.errors ?? 0} failed.`),
          ...a.slis.map((x) => h("div", {}, h("div", { class: "row" }, h("span", { class: "grow" }, SLI_NAMES[x.sli] || x.sli), h("span", { class: "num" }, pct(x.value))), budgetBar(x.budget_left))),
        ),
      ),
    );
  });
}

function agentDetail(name) {
  const head = h("div", { class: "panel stack" });
  const slos = h("div", { class: "panel stack scroll" });
  const history = h("div", { class: "panel stack scroll" });
  const blamed = h("div", { class: "panel stack scroll" });
  const reason = h("input", { placeholder: "Why? This goes in the audit log.", "aria-label": "Reason", class: "grow" });
  const result = h("span", { class: "mute", role: "status" });
  const setLevel = async (level) => {
    try {
      await post(`/api/agents/${encodeURIComponent(name)}/autonomy`, { level, reason: reason.value.trim() || "changed from the dashboard" });
      result.textContent = `Set to ${level}.`;
      reason.value = "";
    } catch (e) {
      result.textContent = e.message;
    }
  };
  const controls = h(
    "div",
    { class: "panel stack" },
    h("h2", {}, "Change its autonomy"),
    h("div", { class: "row" }, reason),
    h(
      "div",
      { class: "row" },
      h("button", { onclick: () => setLevel("supervised") }, "Supervise"),
      h("button", { class: "stop", onclick: () => setLevel("suspended") }, "Suspend"),
      h("button", { onclick: () => setLevel("autonomous") }, "Give back autonomy"),
      result,
    ),
  );
  view.append(h("div", { class: "stack" }, head, h("div", { class: "split" }, h("div", { class: "stack" }, slos, controls), h("div", { class: "stack" }, history, blamed))));
  every(2500, async () => {
    const d = await api(`/api/agents/${encodeURIComponent(name)}`);
    const a = d.agent;
    const list = (xs) => (xs && xs.length ? xs.join(", ") : "none declared");
    fill(
      head,
      h("div", { class: "row" }, h("span", { class: "dot", style: { background: hue(a.name), color: hue(a.name) } }), h("h1", { class: "grow" }, a.name), chip(a.autonomy)),
      a.autonomy !== "autonomous" ? h("p", {}, `${a.reason || ""} Since ${clock(a.since)}.`) : null,
      h(
        "dl",
        { class: "kv" },
        h("dt", {}, "Model"), h("dd", {}, d.config.model || "not declared"),
        h("dt", {}, "May use"), h("dd", {}, list(d.config.tools)),
        h("dt", {}, "May hand to"), h("dd", {}, list(d.config.handoffs)),
        h("dt", {}, "High risk"), h("dd", {}, list(d.config.high_risk)),
      ),
    );
    fill(slos, h("h2", {}, "Targets and error budget"), sliTable(a.slis));
    fill(
      history,
      h("h2", {}, "Autonomy history"),
      d.events.length
        ? h("table", {}, h("tbody", {}, d.events.map((e) => h("tr", {}, h("td", {}, clock(e.at)), h("td", {}, chip(e.new)), h("td", {}, e.reason, h("div", { class: "mute" }, `by ${e.actor}`))))))
        : h("p", { class: "empty" }, "Never demoted."),
    );
    fill(
      blamed,
      h("h2", {}, "Journeys it broke"),
      d.blamed_journeys.length
        ? h("table", {}, h("tbody", {}, d.blamed_journeys.map((j) => h("tr", {}, h("td", {}, clock(j.end)), h("td", {}, h("a", { href: `#/journeys/${j.trace_id}` }, j.name)), h("td", {}, j.culprit_reason)))))
        : h("p", { class: "empty" }, "None in the last while."),
    );
  });
}

// ---------------------------------------------------------------- chaos lab

const FAULT_HELP = {
  model_rate_limit: "The model answers 429 some of the time.",
  tool_wrong_result: "The tool returns a wrong answer, mostly without any error.",
  context_poison: "Injected instructions push the agent off its task.",
  rogue_agent: "The agent takes unsafe high-risk actions and burns tokens.",
  latency: "The model or tool slows down.",
};

function chaos() {
  const fault = h("select", { "aria-label": "Fault" }, Object.keys(FAULT_HELP).map((f) => h("option", { value: f }, f)));
  const target = h("input", { list: "targets", placeholder: "model:sim-large", "aria-label": "Target" });
  const targets = h("datalist", { id: "targets" });
  const intensity = h("input", { type: "number", min: 0.05, max: 1, step: 0.05, value: 0.6, "aria-label": "Intensity" });
  const duration = h("input", { type: "number", min: 5, max: 600, step: 5, value: 40, "aria-label": "Seconds" });
  const help = h("p", { class: "mute" }, FAULT_HELP[fault.value]);
  const note = h("span", { role: "status", class: "mute" });
  fault.addEventListener("change", () => (help.textContent = FAULT_HELP[fault.value]));
  const start = async () => {
    try {
      await post("/api/experiments", { fault: fault.value, target: target.value.trim(), intensity: Number(intensity.value), duration_s: Number(duration.value) });
      note.textContent = "Started. Watch the Fleet page.";
      refresh();
    } catch (e) {
      note.textContent = e.message;
    }
  };
  const list = h("div", { class: "stack" });
  view.append(
    h(
      "div",
      { class: "stack" },
      h("h1", {}, "Chaos lab"),
      h("p", { class: "mute" }, "Break something on purpose and see whether the fleet contains it. Each finished experiment is scored below."),
      h(
        "div",
        { class: "panel stack" },
        h("div", { class: "form" }, h("label", {}, "Fault", fault), h("label", {}, "Target", target), h("label", {}, "Share of calls hit (0 to 1)", intensity), h("label", {}, "Seconds", duration)),
        targets,
        help,
        h("div", { class: "row" }, h("button", { class: "go", onclick: start }, "Start experiment"), note),
      ),
      list,
    ),
  );

  const before = (c) => `${pct(c.before_response.failed_rate)} of journeys failed before Themis responded, ${pct(c.after_response.failed_rate)} after.`;
  async function refresh() {
    const [{ experiments }, o] = await Promise.all([api("/api/experiments"), api("/api/overview")]);
    showStatus(o);
    const options = [...o.agents.map((a) => `agent:${a.name}`), ...new Set(o.edges.filter((e) => e.kind !== "handoff").map((e) => `${e.kind}:${e.to}`))];
    fill(targets, ...options.map((v) => h("option", { value: v })));
    fill(
      list,
      ...(experiments.length ? [] : [h("p", { class: "empty" }, "No experiments yet.")]),
      ...experiments.map((x) => {
        const r = x.result;
        return h(
          "div",
          { class: "panel stack" },
          h("div", { class: "row" }, h("h3", { class: "grow" }, x.name), h("span", { class: "mute" }, clock(x.started_at)), chip(x.status === "done" ? "scored" : x.status, x.status === "done" ? "success" : "pending")),
          x.status === "running" ? h("button", { class: "stop", onclick: () => post(`/api/experiments/${x.id}/stop`).then(refresh) }, "Stop now") : null,
          r &&
            h(
              "dl",
              { class: "kv" },
              h("dt", {}, "Response"),
              h("dd", {}, r.detection.detected ? `${r.detection.response} after ${r.detection.seconds}s.` : "Themis did not act on the target."),
              h("dt", {}, "Containment"),
              h("dd", {}, before(r.containment), ` Unsafe actions: ${r.containment.unsafe_actions_before} before, ${r.containment.unsafe_actions_after} after.`),
              h("dt", {}, "Root cause"),
              h("dd", {}, `Named the target in ${r.rca.named_target} of ${r.rca.fault_caused} journeys the fault broke (${r.rca.by_evidence} from evidence, ${r.rca.by_statistics} from statistics, ${r.rca.unknown} unanswered).`),
              h("dt", {}, "Blast radius"),
              h("dd", {}, `${r.journeys.failed} failed and ${r.journeys.blocked} stopped safely out of ${r.journeys.total}. ${r.blast_radius.users} users, ${r.blast_radius.agents.length} agents in failed journeys.`),
            ),
        );
      }),
    );
  }
  every(3000, refresh);
}

// ---------------------------------------------------------------- incidents and approvals

function incidents() {
  const waiting = h("div", { class: "panel stack scroll" });
  const open = h("div", { class: "panel stack scroll" });
  const causes = h("div", { class: "panel stack scroll" });
  view.append(h("div", { class: "stack" }, h("h1", {}, "Incidents"), waiting, h("div", { class: "split" }, open, causes)));
  const decide = (id, approve) => post(`/api/approvals/${id}/decision`, { approve, by: "dashboard" }).then(refresh, refresh);

  async function refresh() {
    const [a, i, r] = await Promise.all([api("/api/approvals"), api("/api/incidents"), api("/api/rca")]);
    fill(
      waiting,
      h("h2", {}, "Waiting for your go or no-go"),
      a.approvals.length
        ? h(
            "table",
            {},
            h(
              "tbody",
              {},
              a.approvals.map((p) =>
                h(
                  "tr",
                  {},
                  h("td", {}, ago(p.requested_at)),
                  h("td", {}, h("strong", {}, p.agent), ` wants to run ${p.action}`),
                  h("td", {}, p.detail.unsafe_params ? chip("safety check failed", "failed") : chip("safety check passed", "success")),
                  h("td", {}, h("div", { class: "row" }, h("button", { class: "go", onclick: () => decide(p.id, true) }, "Go"), h("button", { class: "stop", onclick: () => decide(p.id, false) }, "No-go"))),
                ),
              ),
            ),
          )
        : h("p", { class: "empty" }, "Nothing is waiting. Supervised agents ask here before a high-risk action."),
    );
    fill(
      open,
      h("h2", {}, "Incidents"),
      i.incidents.length
        ? h("table", {}, h("tbody", {}, i.incidents.map((x) => h("tr", {}, h("td", {}, clock(x.opened_at)), h("td", {}, chip(x.status), h("div", { class: "mute" }, x.severity)), h("td", {}, x.title, h("div", { class: "mute" }, `webhook: ${x.notify_state}`))))))
        : h("p", { class: "empty" }, "No incidents. One opens when an agent is demoted or a dependency is quarantined."),
    );
    fill(
      causes,
      h("h2", {}, "What is breaking journeys"),
      h("p", { class: "mute" }, `${r.not_successful} of the last ${r.window_journeys} journeys did not end well.`),
      r.culprits.length
        ? h(
            "table",
            {},
            h("thead", {}, h("tr", {}, h("th", {}, "Cause"), h("th", { class: "n" }, "Journeys"), h("th", { class: "n" }, "From evidence"), h("th", { class: "n" }, "From statistics"))),
            h("tbody", {}, r.culprits.map((c) => h("tr", {}, h("td", {}, c.component), h("td", { class: "n" }, c.journeys), h("td", { class: "n" }, c.evidence), h("td", { class: "n" }, c.statistics)))),
          )
        : null,
    );
  }
  every(2000, refresh);
}

// ---------------------------------------------------------------- routing

// The page name comes from the address bar, so it is never used to look anything up: a plain switch means no
// input can reach an inherited method such as "constructor", or anything else that is not listed here.
const PAGE_NAMES = ["journeys", "agents", "chaos", "incidents"];

function pageFor(name) {
  switch (name) {
    case "journeys":
      return journeys;
    case "agents":
      return agents;
    case "chaos":
      return chaos;
    case "incidents":
      return incidents;
    default:
      return fleet;
  }
}

function route() {
  clearInterval(timer);
  cancelAnimationFrame(frame);
  const [name, arg] = location.hash.replace(/^#\/?/, "").split("/");
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.getAttribute("href") === `#/${PAGE_NAMES.includes(name) ? name : ""}`) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  fill(view);
  if (!key) return askForKey();
  pageFor(name)(arg ? decodeURIComponent(arg) : undefined);
}

// `themis demo` prints a link with the key in the fragment. A fragment is never sent to the server; take the
// key out of the address bar straight away so it does not stay in the history either.
const fromLink = location.hash.match(/key=([\w-]+)/);
if (fromLink) {
  key = fromLink[1];
  try {
    sessionStorage.setItem("themis.key", key);
  } catch {
    /* fine */
  }
  history.replaceState(null, "", location.pathname + "#/");
} else {
  try {
    key = sessionStorage.getItem("themis.key") || "";
  } catch {
    key = "";
  }
}
addEventListener("hashchange", route);
route();
