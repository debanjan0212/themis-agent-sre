// Themis dashboard. No build step and no framework: one module.
// Agent, tool and journey names come from telemetry, so they are untrusted. Every piece of text goes through
// h() / s(), which use text nodes and setAttribute. Nothing here ever builds HTML from a string.

const view = document.getElementById("view");
const statusBar = document.getElementById("status");
const HUES = ["#7CC4FF", "#B49CFF", "#FF9EC4", "#E8C36B", "#C5E86B", "#7BE0A0", "#59D8E6", "#8FA2FF", "#E29CFF", "#FFA98F"];
const SLI_NAMES = { task_success: "Tasks done", handoff_success: "Clean handoffs", safe_actions: "Safe actions", cost: "Within cost" };
let key = "";
let timer = null;
const hues = new Map();

function build(el, attrs, kids) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(3)) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}
const h = (tag, attrs, ...kids) => build(document.createElement(tag), attrs, kids);
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
  view.replaceChildren(
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
  const waiting = s("text", { class: "core-text", y: 3 }, "0");
  const core = s("g", {}, s("circle", { class: "core", r: 30 }), waiting, s("text", { class: "core-sub", y: 16 }, "waiting"));
  const svg = s(
    "svg",
    { viewBox: "-372 -340 744 680", role: "img", "aria-label": "Fleet map. Agents sit closer to the human at the centre as they lose autonomy." },
    lanes,
    edges,
    deps,
    core,
    bodies,
  );
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
          svg,
          h(
            "p",
            { class: "legend" },
            "The human is at the centre. Outer ring: the agent acts alone. Inner ring: a human approves its risky actions. Docked at the centre: suspended. Circles on the rim are tools, diamonds are models.",
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

  const nodes = new Map();
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
        const g = s(
          "g",
          { class: "body", tabindex: 0, role: "link", onclick: () => (location.hash = `#/agents/${encodeURIComponent(a.name)}`) },
          s("circle", { class: "disc", r: 19, fill: hue(a.name), "fill-opacity": 0.3, stroke: hue(a.name) }),
          s("text", { y: 35 }, a.name),
          mode,
        );
        g.addEventListener("keydown", (e) => e.key === "Enter" && g.dispatchEvent(new Event("click")));
        node = { g, mode };
        nodes.set(a.name, node);
        bodies.append(g);
      }
      const [x, y] = at.get(a.name);
      node.g.setAttribute("class", `body ${a.autonomy}`);
      node.g.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
      node.mode.textContent = a.autonomy === "autonomous" ? "" : a.autonomy;
    }

    // tools and models on the outer ring, each near the first agent that uses it
    const held = new Set(o.quarantine.map((q) => q.component));
    const depKeys = [...new Set(o.edges.filter((e) => e.kind !== "handoff").map((e) => `${e.kind}:${e.to}`))];
    if (depKeys.length !== depAngles.size || depKeys.some((k) => !depAngles.has(k))) {
      const first = (k) => Math.min(...o.edges.filter((e) => `${e.kind}:${e.to}` === k).map((e) => angle.get(e.from) ?? 0));
      const sorted = depKeys.sort((a, b) => first(a) - first(b) || a.localeCompare(b));
      depAngles = new Map(sorted.map((k, i) => [k, -90 + ((i + 0.5) * 360) / sorted.length]));
    }
    deps.replaceChildren(
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

    const most = Math.max(1, ...o.edges.map((e) => e.count));
    edges.replaceChildren(
      ...o.edges.map((e) => {
        const from = at.get(e.from);
        const bad = e.errors / e.count > 0.05;
        const strength = e.count / most;
        if (!from) return null;
        if (e.kind === "handoff") {
          const to = at.get(e.to);
          if (!to) return null;
          const mid = [((from[0] + to[0]) / 2) * 0.72, ((from[1] + to[1]) / 2) * 0.72];
          const d = `M${from[0]},${from[1]} Q${mid[0]},${mid[1]} ${to[0]},${to[1]}`;
          return s("path", { class: `edge ${bad ? "bad" : ""}`, d, stroke: hue(e.from), "stroke-opacity": 0.35 + strength * 0.5, "stroke-width": 1.6 });
        }
        const deg = depAngles.get(`${e.kind}:${e.to}`);
        if (deg == null) return null;
        const [x, y] = polar(deg, 279);
        return s("line", { class: `edge ${bad ? "bad" : ""}`, x1: from[0], y1: from[1], x2: x, y2: y, stroke: hue(e.from), "stroke-opacity": 0.14 + strength * 0.3, "stroke-width": 1 });
      }),
    );
    waiting.textContent = o.pending_approvals;

    banner.replaceChildren(
      ...o.experiments.map((x) =>
        h("div", { class: "banner" }, `Chaos experiment running: ${x.name}. ${Math.max(0, Math.round(x.started_at + x.duration_s - o.at))}s left.`),
      ),
    );
    const v = o.vitals;
    const vital = (value, label) => h("div", { class: "vital" }, h("span", { class: "num" }, value), h("span", {}, label));
    vitals.replaceChildren(
      vital(v.per_min ?? 0, "journeys a minute"),
      vital(pct(v.success), "ended well"),
      vital(ms(v.p95_ms), "95th percentile time"),
      vital(usd(v.cost_per_min), "model spend a minute"),
      vital(pct(v.failed), "failed"),
      vital(pct(v.blocked), "stopped safely"),
    );
    ladder.replaceChildren(
      ...o.agents.map((a) => {
        const worst = [...a.slis].sort((p, q) => q.burn_long - p.burn_long)[0];
        const why = a.autonomy !== "autonomous" ? a.reason : worst && worst.burn_long >= 1 ? `${SLI_NAMES[worst.sli]} burning ${worst.burn_long}x` : "";
        return h(
          "li",
          {},
          h("span", { class: "dot", style: { background: hue(a.name) } }),
          h("a", { href: `#/agents/${encodeURIComponent(a.name)}` }, a.name),
          chip(a.autonomy),
          h("span", { class: "why", title: why || "" }, why || ""),
        );
      }),
    );
    feed.replaceChildren(
      ...(o.events.length
        ? o.events.map((e) => h("li", {}, h("time", {}, clock(e.at)), e.text, e.detail ? h("div", { class: "mute" }, e.detail) : null))
        : [h("li", { class: "empty" }, "Nothing yet. Demotions, quarantines and chaos experiments will show up here.")]),
    );
  });
}

function showStatus(o) {
  statusBar.replaceChildren(
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
    filters.replaceChildren(
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
    if (!rows.length) return table.replaceChildren(h("p", { class: "empty" }, "No journeys match. Start the world with `themis demo`."));
    table.replaceChildren(
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
      return detail.replaceChildren(h("div", { class: "panel error" }, e.message));
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
    detail.replaceChildren(
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
              h("strong", {}, h("span", { class: "dot", style: { background: hue(u.agent) } }), u.agent),
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
                h("i", { style: { left: `${(sp.offset_ms / total) * 100}%`, width: `${Math.max((sp.duration_ms / total) * 100, 0.4)}%`, background: wrong ? "var(--held)" : sp.agent ? hue(sp.agent) : "var(--brass)" } }),
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
  if (!slis.length) return h("p", { class: "empty" }, "No traffic yet.");
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
    cards.replaceChildren(
      ...o.agents.map((a) =>
        h(
          "a",
          { class: "stack", href: `#/agents/${encodeURIComponent(a.name)}` },
          h("div", { class: "row" }, h("h3", { class: "grow" }, h("span", { class: "dot", style: { background: hue(a.name) } }), a.name), chip(a.autonomy)),
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
    head.replaceChildren(
      h("div", { class: "row" }, h("span", { class: "dot", style: { background: hue(a.name) } }), h("h1", { class: "grow" }, a.name), chip(a.autonomy)),
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
    slos.replaceChildren(h("h2", {}, "Targets and error budget"), sliTable(a.slis));
    history.replaceChildren(
      h("h2", {}, "Autonomy history"),
      d.events.length
        ? h("table", {}, h("tbody", {}, d.events.map((e) => h("tr", {}, h("td", {}, clock(e.at)), h("td", {}, chip(e.new)), h("td", {}, e.reason, h("div", { class: "mute" }, `by ${e.actor}`))))))
        : h("p", { class: "empty" }, "Never demoted."),
    );
    blamed.replaceChildren(
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
    targets.replaceChildren(...options.map((v) => h("option", { value: v })));
    list.replaceChildren(
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
    waiting.replaceChildren(
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
    open.replaceChildren(
      h("h2", {}, "Incidents"),
      i.incidents.length
        ? h("table", {}, h("tbody", {}, i.incidents.map((x) => h("tr", {}, h("td", {}, clock(x.opened_at)), h("td", {}, chip(x.status), h("div", { class: "mute" }, x.severity)), h("td", {}, x.title, h("div", { class: "mute" }, `webhook: ${x.notify_state}`))))))
        : h("p", { class: "empty" }, "No incidents. One opens when an agent is demoted or a dependency is quarantined."),
    );
    causes.replaceChildren(
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
  const [name, arg] = location.hash.replace(/^#\/?/, "").split("/");
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.getAttribute("href") === `#/${PAGE_NAMES.includes(name) ? name : ""}`) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  view.replaceChildren();
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
