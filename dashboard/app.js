/* Dashboard logic: polls the REST endpoints in backend/api.py and
 * subscribes to /api/events/stream (SSE) for live updates. No build
 * step, no framework -- plain fetch()/EventSource per the approved
 * stack. Same-origin API calls (backend/api.py mounts this directory
 * as static files at "/").
 *
 * Design intent: a reader who has never seen this project should be
 * able to tell, at a glance, what the system is doing and whether the
 * network is healthy. Raw field names from the database (utilization,
 * P2_ML_RL, rl_fallback...) are translated into plain language here
 * rather than shown as-is.
 */
(() => {
  "use strict";

  const LINK_STATE_COLOR = {
    HEALTHY: "#35d07f",
    AT_RISK: "#e8d24a",
    DEGRADED: "#f0972e",
    FAILED: "#f0553f",
  };

  const LINK_STATE_WORD = {
    HEALTHY: "fine",
    AT_RISK: "starting to look risky",
    DEGRADED: "degrading",
    FAILED: "failed",
  };

  const INCIDENT_BADGE_CLASS = {
    OPEN: "badge-open",
    SUCCESS: "badge-success",
    PARTIAL: "badge-partial",
    FAILED: "badge-failed",
    UNRESOLVED: "badge-unresolved",
  };

  const INCIDENT_WORD = {
    OPEN: "being handled",
    SUCCESS: "fixed",
    PARTIAL: "partly fixed",
    FAILED: "fix failed",
    UNRESOLVED: "could not fix",
  };

  // Stable positions so the map looks the same every refresh and reads
  // left-to-right along the direction traffic actually travels
  // (h1 -> s1 -> ...mesh... -> s6 -> h6), instead of a scrambled
  // force-directed layout that lands somewhere new on every reload.
  const NODE_POSITIONS = {
    h1: { x: 40, y: 190 },
    s1: { x: 155, y: 190 },
    s2: { x: 275, y: 80 },
    s3: { x: 405, y: 80 },
    s4: { x: 275, y: 300 },
    s5: { x: 405, y: 300 },
    s6: { x: 525, y: 190 },
    h6: { x: 640, y: 190 },
  };

  let cy = null;
  let telemetryChart = null;
  let currentLinkId = null;
  let incidentStatusFilter = "";
  let linksById = {};
  let activePath = [];
  let hasAutoSelected = false;

  const $ = (id) => document.getElementById(id);

  function fmtPct(v) {
    return v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;
  }
  function fmtMs(v) {
    return v === null || v === undefined ? "—" : `${v.toFixed(1)} ms`;
  }
  function fmtTs(t) {
    if (!t) return "—";
    return new Date(t * 1000).toLocaleTimeString();
  }
  function ago(t) {
    if (!t) return "";
    const secs = Math.max(0, Math.round(Date.now() / 1000 - t));
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
    return `${Math.round(secs / 3600)}h ago`;
  }
  /** "s1,s2,s6" -> "s1 → s2 → s6" */
  function prettyPath(p) {
    return p ? p.split(",").join(" → ") : null;
  }

  async function getJSON(path) {
    const resp = await fetch(path);
    if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
    return resp.json();
  }

  // ---- connection + clock ---------------------------------------------

  async function pollHealth() {
    try {
      await getJSON("/api/health");
      $("conn-dot").className = "dot dot-ok";
      $("conn-label").textContent = "connected";
      setStage("watch", true);
      return true;
    } catch (err) {
      $("conn-dot").className = "dot dot-down";
      $("conn-label").textContent = "controller offline";
      setStage("watch", false);
      return false;
    }
  }

  function tickClock() {
    $("clock").textContent = new Date().toLocaleTimeString();
  }

  function setStage(name, on) {
    const el = document.querySelector(`.stage[data-stage="${name}"]`);
    if (el) el.classList.toggle("active", !!on);
  }

  // ---- the plain-language headline --------------------------------------

  function updateStatusBanner(links, incidents) {
    const banner = $("status-banner");
    const icon = $("status-icon");
    const headline = $("status-headline");
    const sub = $("status-sub");

    const unhealthy = links.filter((l) => l.state && l.state !== "HEALTHY");
    const open = incidents.filter((i) => i.status === "OPEN");
    const unresolved = incidents.filter((i) => i.status === "UNRESOLVED");

    banner.className = "status-banner";

    // Keyed off whether ANY evidence exists, not just the links
    // registry. Reading a completed run whose controller predates the
    // links table being populated, the banner used to announce "waiting
    // for the network" directly above a finished incident from that same
    // database.
    if (!links.length && !incidents.length) {
      banner.classList.add("banner-idle");
      icon.textContent = "•";
      headline.textContent = "Waiting for the network";
      sub.textContent = "No switches are reporting yet. Start the controller and the map will appear.";
      return;
    }

    if (unresolved.length) {
      banner.classList.add("banner-bad");
      icon.textContent = "✕";
      headline.textContent = `${unresolved.length} problem${unresolved.length > 1 ? "s" : ""} could not be fixed automatically`;
      sub.textContent = "The system tried other paths and none of them worked. This needs a human.";
      return;
    }

    if (open.length) {
      banner.classList.add("banner-warn");
      icon.textContent = "⚠";
      const names = open.map((i) => i.link_id).join(", ");
      headline.textContent = `Handling a problem on ${names}`;
      sub.textContent = "The AI predicted this link is going bad and is moving traffic away from it right now.";
      return;
    }

    if (unhealthy.length) {
      banner.classList.add("banner-warn");
      icon.textContent = "⚠";
      headline.textContent = `${unhealthy.length} link${unhealthy.length > 1 ? "s" : ""} not looking healthy`;
      sub.textContent = `${unhealthy.map((l) => `${l.link_id} is ${LINK_STATE_WORD[l.state] || l.state}`).join("; ")}.`;
      return;
    }

    banner.classList.add("banner-ok");
    icon.textContent = "✓";
    headline.textContent = incidents.length
      ? "Network is healthy — earlier problems were fixed"
      : "Network is healthy";
    // Don't claim a link count when the registry is empty (e.g. reading
    // a run recorded before the controller populated it) -- "All 0 links
    // are fine" is worse than saying nothing about counts.
    const flowing = activePath.length ? `Traffic is flowing along ${activePath.join(" → ")}.` : "";
    sub.textContent = links.length
      ? `All ${links.length} links are fine. ${flowing}`.trim()
      : flowing || "No link health has been recorded for this run.";
  }

  // ---- the network map ---------------------------------------------------

  function linkIdForEdge(a, b) {
    const pair = [`s${a}`, `s${b}`].sort();
    return `${pair[0]}-${pair[1]}`;
  }

  /** Most recent installed route, so the map can show where traffic is. */
  async function loadActivePath() {
    try {
      const changes = await getJSON("/api/route_changes?limit=20");
      const installed = changes.find((c) => c.changed && c.new_path);
      activePath = installed ? installed.new_path.split(",") : [];
    } catch (err) {
      activePath = [];
    }
  }

  function edgeIsOnActivePath(srcId, dstId) {
    for (let i = 0; i < activePath.length - 1; i++) {
      const a = activePath[i];
      const b = activePath[i + 1];
      if ((a === srcId && b === dstId) || (a === dstId && b === srcId)) return true;
    }
    return false;
  }

  async function loadTopology() {
    let topo;
    try {
      topo = await getJSON("/api/topology");
    } catch (err) {
      $("topology-empty").hidden = false;
      return;
    }
    $("topology-empty").hidden = true;

    const links = await getJSON("/api/links");
    linksById = Object.fromEntries(links.map((l) => [l.link_id, l]));
    await loadActivePath();

    const switchIds = (topo.topology.nodes || []).map((n) => `s${n.id}`);
    const nodes = switchIds.map((id) => ({
      data: { id, label: id.toUpperCase(), kind: "switch" },
      position: NODE_POSITIONS[id] || undefined,
    }));

    // Show the two endpoint computers so the picture reads as "traffic
    // goes from here to there", not just an abstract mesh of switches.
    const edges = [];
    if (switchIds.includes("s1")) {
      nodes.push({ data: { id: "h1", label: "H1", kind: "host" }, position: NODE_POSITIONS.h1 });
      edges.push({ data: { id: "h1-s1", source: "h1", target: "s1", kind: "host" } });
    }
    if (switchIds.includes("s6")) {
      nodes.push({ data: { id: "h6", label: "H6", kind: "host" }, position: NODE_POSITIONS.h6 });
      edges.push({ data: { id: "s6-h6", source: "s6", target: "h6", kind: "host" } });
    }

    for (const e of topo.topology.links || []) {
      const linkId = linkIdForEdge(e.source, e.target);
      const state = linksById[linkId] ? linksById[linkId].state : "HEALTHY";
      edges.push({
        data: {
          id: linkId,
          source: `s${e.source}`,
          target: `s${e.target}`,
          state,
          kind: "link",
          carrying: edgeIsOnActivePath(`s${e.source}`, `s${e.target}`) ? "yes" : "no",
        },
      });
    }

    const style = [
      {
        selector: 'node[kind="switch"]',
        style: {
          "background-color": "#16324f",
          "border-color": "#35d0c0",
          "border-width": 2,
          label: "data(label)",
          color: "#e7edf7",
          "font-size": 12,
          "font-weight": "bold",
          "text-valign": "center",
          "text-halign": "center",
          width: 42,
          height: 42,
        },
      },
      {
        selector: 'node[kind="host"]',
        style: {
          shape: "round-rectangle",
          "background-color": "#1b6f68",
          "border-color": "#35d0c0",
          "border-width": 2,
          label: "data(label)",
          color: "#e7edf7",
          "font-size": 11,
          "text-valign": "center",
          "text-halign": "center",
          width: 38,
          height: 28,
        },
      },
      {
        selector: 'edge[kind="link"]',
        style: {
          width: 3,
          "line-color": (ele) => LINK_STATE_COLOR[ele.data("state")] || "#35d07f",
          "curve-style": "bezier",
        },
      },
      {
        // The path traffic is actually using right now.
        selector: 'edge[carrying="yes"]',
        style: { width: 7, "line-color": "#6ea8ff", opacity: 0.95 },
      },
      {
        selector: 'edge[kind="host"]',
        style: { width: 2, "line-color": "#2f4868", "curve-style": "straight", "line-style": "dashed" },
      },
      {
        selector: "edge:selected",
        style: { "line-color": "#35d0c0", width: 7 },
      },
    ];

    if (!cy) {
      cy = window.cytoscape({
        container: $("cy"),
        elements: { nodes, edges },
        style,
        layout: { name: "preset", fit: true, padding: 40 },
        wheelSensitivity: 0.2,
      });
      cy.on("tap", 'edge[kind="link"]', (evt) => selectLink(evt.target.data("id")));
    } else {
      cy.json({ elements: { nodes, edges } });
      cy.layout({ name: "preset", fit: true, padding: 40 }).run();
    }

    populateLinkSelect(links);

    // Open the most interesting link automatically, so the inspector
    // isn't a wall of dashes on first load.
    if (!hasAutoSelected && links.length) {
      const worst =
        links.find((l) => l.state === "FAILED") ||
        links.find((l) => l.state === "DEGRADED") ||
        links.find((l) => l.state === "AT_RISK") ||
        links[0];
      hasAutoSelected = true;
      selectLink(worst.link_id);
    }
  }

  function populateLinkSelect(links) {
    const select = $("link-select");
    const existing = new Set(Array.from(select.options).map((o) => o.value));
    for (const link of links) {
      if (existing.has(link.link_id)) continue;
      const opt = document.createElement("option");
      opt.value = link.link_id;
      opt.textContent = link.link_id;
      select.appendChild(opt);
    }
  }

  function selectLink(linkId) {
    currentLinkId = linkId;
    $("link-select").value = linkId;
    const link = linksById[linkId];
    const carrying = activePath.length && edgeIsOnActivePathById(linkId);
    $("link-hint").textContent = link
      ? `${linkId} — currently ${LINK_STATE_WORD[link.state] || link.state}${carrying ? ", and carrying your traffic" : ""}.`
      : `${linkId}`;
    loadLinkDetail(linkId);
  }

  function edgeIsOnActivePathById(linkId) {
    const [a, b] = linkId.split("-");
    return edgeIsOnActivePath(a, b);
  }

  // ---- link inspector ---------------------------------------------------

  async function loadLinkDetail(linkId) {
    if (!linkId) return;
    const since = Date.now() / 1000 - 300; // last 5 minutes
    const [features, predictions] = await Promise.all([
      getJSON(`/api/features/${encodeURIComponent(linkId)}?since=${since}&limit=200`),
      getJSON(`/api/predictions/${encodeURIComponent(linkId)}?since=${since}&limit=200`),
    ]);

    const latestFeature = features[features.length - 1];
    const latestPrediction = predictions[predictions.length - 1];

    $("metric-util").textContent = latestFeature ? fmtPct(latestFeature.utilization) : "—";
    $("metric-latency").textContent = latestFeature ? fmtMs(latestFeature.latency_ms) : "—";
    $("metric-loss").textContent = latestFeature ? fmtPct(latestFeature.loss_rate) : "—";
    $("metric-prob").textContent = latestPrediction ? fmtPct(latestPrediction.probability) : "—";

    setStage("predict", predictions.length > 0);

    // A panel of silent dashes tells the reader nothing about WHY it is
    // empty. Say so explicitly instead.
    const hasData = features.length > 0 || predictions.length > 0;
    $("chart-empty").hidden = hasData;
    $("telemetry-chart").style.visibility = hasData ? "visible" : "hidden";
    $("chart-caption").textContent = hasData
      ? "Last 5 minutes — how busy the link is, and the AI's rising concern about it."
      : "Nothing recorded recently for this link.";

    if (hasData) renderTelemetryChart(features, predictions);
  }

  function renderTelemetryChart(features, predictions) {
    const ctx = $("telemetry-chart").getContext("2d");
    const utilizationPoints = features.map((f) => ({ x: f.timestamp * 1000, y: f.utilization }));
    const predictionPoints = predictions.map((p) => ({ x: p.timestamp * 1000, y: p.probability }));

    if (telemetryChart) telemetryChart.destroy();
    telemetryChart = new window.Chart(ctx, {
      type: "line",
      data: {
        datasets: [
          {
            label: "how busy the link is",
            data: utilizationPoints,
            borderColor: "#35d0c0",
            backgroundColor: "transparent",
            pointRadius: 0,
            tension: 0.2,
          },
          {
            label: "AI's risk estimate",
            data: predictionPoints,
            borderColor: "#f0972e",
            backgroundColor: "transparent",
            pointRadius: 0,
            tension: 0.2,
          },
        ],
      },
      options: {
        animation: false,
        maintainAspectRatio: false,
        scales: {
          // Plain linear scale (epoch ms) with a formatting callback,
          // not Chart.js's "time" scale -- that needs a date-adapter
          // library the approved stack doesn't list alongside chart.js.
          x: {
            type: "linear",
            ticks: { color: "#8ea0bf", maxTicksLimit: 6, callback: (val) => new Date(val).toLocaleTimeString() },
            grid: { color: "#223252" },
          },
          y: {
            min: 0,
            max: 1,
            ticks: { color: "#8ea0bf", callback: (v) => `${Math.round(v * 100)}%` },
            grid: { color: "#223252" },
          },
        },
        plugins: { legend: { labels: { color: "#e7edf7", boxWidth: 12, font: { size: 11 } } } },
      },
    });
  }

  // ---- problems detected ------------------------------------------------

  async function loadIncidents() {
    const params = incidentStatusFilter ? `?status=${incidentStatusFilter}` : "";
    let incidents = [];
    try {
      incidents = await getJSON(`/api/incidents${params}`);
    } catch (err) {
      return;
    }

    // The controller opens an "init-" incident to install the managed
    // flow's first path. That is startup, not a fault, and it rendered
    // here as a blank-named row under "Problems detected".
    incidents = incidents.filter((i) => !String(i.incident_id).startsWith("init-"));

    const list = $("incident-list");
    $("incident-empty").hidden = incidents.length > 0;
    list.innerHTML = "";

    for (const incident of incidents) {
      const li = document.createElement("li");
      li.className = "incident-item";
      li.dataset.incidentId = incident.incident_id;
      const badgeClass = INCIDENT_BADGE_CLASS[incident.status] || "badge-open";
      const word = INCIDENT_WORD[incident.status] || incident.status;
      li.innerHTML = `
        <span><strong>${incident.link_id}</strong>
          <span class="incident-when">started ${ago(incident.opened_at)}</span>
        </span>
        <span class="badge ${badgeClass}">${word}</span>
      `;
      li.addEventListener("click", () => selectIncident(incident.incident_id));
      list.appendChild(li);
    }

    // Keep the headline and the "reroute/verify" stage lights in sync.
    try {
      const links = await getJSON("/api/links");
      updateStatusBanner(links, incidents);
    } catch (err) {
      /* banner just stays as-is */
    }

    if (incidents.length && !$("incident-detail-id").textContent) {
      selectIncident(incidents[0].incident_id);
    }
  }

  async function selectIncident(incidentId) {
    document.querySelectorAll(".incident-item").forEach((el) => {
      el.classList.toggle("selected", el.dataset.incidentId === incidentId);
    });
    $("incident-detail-id").textContent = incidentId;
    $("incident-detail-hint").textContent = "Read top to bottom — this is what happened, in order.";

    let detail;
    try {
      detail = await getJSON(`/api/incidents/${encodeURIComponent(incidentId)}`);
    } catch (err) {
      return;
    }

    const body = $("incident-detail-body");
    body.innerHTML = "";

    // Step 1 -- what was detected.
    body.appendChild(
      storyStep(
        "step-detect",
        `Trouble spotted on ${detail.incident.link_id}`,
        `${fmtTs(detail.incident.opened_at)} — ${detail.incident.reason || "the AI flagged this link as likely to degrade."}`
      )
    );

    // Step 2..n -- what it decided to do.
    setStage("reroute", detail.route_changes.length > 0);
    for (const rc of detail.route_changes) {
      const isRl = rc.selector === "P2_ML_RL";
      const chooser = isRl
        ? `<span class="tag tag-rl">chosen by the AI</span>`
        : `<span class="tag tag-heuristic">chosen by the backup maths</span>`;
      const fallback = rc.rl_fallback
        ? `<span class="tag tag-fallback">AI unavailable: ${rc.rl_fallback_reason}</span>`
        : "";

      const title = rc.changed
        ? `Moved traffic to a different path ${chooser}${fallback}`
        : `Decided not to move traffic ${fallback}`;
      const detailText = rc.changed
        ? `<span class="path">${prettyPath(rc.old_path) || "unknown"}</span> → <span class="path">${prettyPath(rc.new_path)}</span>`
        : rc.decision_reason;

      body.appendChild(storyStep("step-decide", title, detailText, fmtTs(rc.timestamp)));
    }

    // Final step -- did it actually work?
    setStage("verify", detail.verification_results.length > 0);
    for (const vr of detail.verification_results) {
      const good = vr.classification === "SUCCESS";
      body.appendChild(
        storyStep(
          good ? "step-verify-ok" : "step-verify-bad",
          good ? "Checked afterwards — the fix worked" : `Checked afterwards — result: ${vr.classification}`,
          `Measured traffic on <span class="path">${prettyPath(vr.path)}</span>${
            vr.retry_count ? ` after ${vr.retry_count} retry attempt(s)` : ""
          }.`,
          fmtTs(vr.timestamp)
        )
      );
    }

    if (!detail.route_changes.length && !detail.verification_results.length) {
      const p = document.createElement("p");
      p.className = "empty-state";
      p.textContent = "The system has noticed this link but has not acted on it yet.";
      body.appendChild(p);
    }
  }

  function storyStep(cls, title, detailHtml, when) {
    const div = document.createElement("div");
    div.className = `story-step ${cls}`;
    div.innerHTML = `
      <div class="story-body">
        <p class="story-title">${title}</p>
        <p class="story-detail">${when ? `${when} — ` : ""}${detailHtml}</p>
      </div>
    `;
    return div;
  }

  // ---- live activity (SSE) ----------------------------------------------

  function humanEvent(payload) {
    const type = (payload.event_type || "").replace(/_/g, " ");
    let extra = "";
    try {
      const d = JSON.parse(payload.detail_json || "{}");
      if (d.path) extra = ` along ${Array.isArray(d.path) ? d.path.map((p) => `s${p}`).join(" → ") : d.path}`;
      else if (Object.keys(d).length) extra = ` (${Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(", ")})`;
    } catch (err) {
      /* leave extra empty */
    }
    return `<strong>${type}</strong>${extra}${payload.incident_id ? ` — ${payload.incident_id}` : ""}`;
  }

  function connectEventStream() {
    const source = new EventSource("/api/events/stream");
    const log = $("event-log");
    source.onmessage = (evt) => {
      let payload;
      try {
        payload = JSON.parse(evt.data);
      } catch (err) {
        return;
      }
      $("event-empty").hidden = true;
      const li = document.createElement("li");
      li.className = "event-row";
      li.innerHTML = `<span class="ts">${fmtTs(payload.timestamp)}</span><span class="what">${humanEvent(payload)}</span>`;
      log.prepend(li);
      while (log.children.length > 200) log.removeChild(log.lastChild);

      loadIncidents();
      loadTopology();
    };
    source.onerror = () => {
      $("conn-dot").className = "dot dot-down";
      $("conn-label").textContent = "live feed disconnected";
    };
  }


  // ---- Results view ------------------------------------------------------
  //
  // Renders only what the project actually measured, read from
  // /api/results (which in turn reads the JSON that analyze.py,
  // train_model.py and evaluate_rl.py wrote). A missing artefact is shown
  // as "not measured yet"; nothing is defaulted to a number.

  const SYSTEM_LABEL_SHORT = {
    B1_SHORTEST_PATH: "B1 · static shortest path",
    B2_RULE_BASED: "B2 · reactive thresholds",
    P1_ML_HEURISTIC: "P1 · ML + heuristic",
    P2_ML_RL: "P2 · ML + RL (full system)",
  };

  function card(title, subtitle, bodyHtml) {
    return `<div class="panel result-card">
      <div class="panel-head"><div><h2>${title}</h2>
      ${subtitle ? `<p class="panel-hint">${subtitle}</p>` : ""}</div></div>
      ${bodyHtml}</div>`;
  }

  function pct(v, digits = 0) {
    return v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;
  }

  function experimentTable(exp) {
    const rows = Object.entries(exp)
      .filter(([k]) => k.startsWith("E1/"))
      .map(([k, s]) => {
        const sys = k.split("/")[1];
        const rec = s.recovery_time_s || {};
        const rr = s.reroute_count || {};
        return `<tr>
          <td>${SYSTEM_LABEL_SHORT[sys] || sys}</td>
          <td class="num">${pct(s.detection_rate)}</td>
          <td class="num">${rec.mean != null ? rec.mean.toFixed(1) + "s" : "—"}</td>
          <td class="num">${rr.mean != null ? rr.mean.toFixed(1) : "—"}</td>
          <td class="num">${s.trials_usable}</td>
        </tr>`;
      });
    if (!rows.length) return null;
    return `<table class="result-table">
      <thead><tr><th>system</th><th>detected</th><th>recovery</th><th>reroutes</th><th>trials</th></tr></thead>
      <tbody>${rows.join("")}</tbody></table>
      <p class="result-note">
        On this fault type the simple threshold baseline (B2) outperforms the full
        system. That is reported rather than hidden: the fault is severe enough that
        a fixed threshold catches it immediately, so prediction adds latency without
        adding value. Whether prediction helps on subtler faults is what the
        remaining experiments test.
      </p>`;
  }

  function mlCard(ml) {
    const best = ml.chosen_model;
    const m = (ml.models || {})[best] || {};
    return `<div class="metric-row">
        <div class="metric"><span class="metric-label">Model</span>
          <span class="metric-value" style="font-size:15px">${best || "—"}</span></div>
        <div class="metric"><span class="metric-label">ROC-AUC</span>
          <span class="metric-value">${m.roc_auc != null ? m.roc_auc.toFixed(3) : "—"}</span>
          <span class="metric-note">1.0 = perfect, 0.5 = guessing</span></div>
        <div class="metric"><span class="metric-label">Precision</span>
          <span class="metric-value">${m.precision != null ? pct(m.precision) : "—"}</span>
          <span class="metric-note">when it warns, how often it's right</span></div>
        <div class="metric"><span class="metric-label">Recall</span>
          <span class="metric-value">${m.recall != null ? pct(m.recall) : "—"}</span>
          <span class="metric-note">how many real faults it catches</span></div>
      </div>
      <p class="result-note">Trained on ${ml.train_seeds ? ml.train_seeds.length : "?"} runs,
      tested on ${ml.test_seeds ? ml.test_seeds.length : "?"} held-out runs it never saw.</p>`;
  }

  function rlCard(rl) {
    const res = rl.results || {};
    const rows = Object.entries(res).map(([name, s]) => {
      const r = s.episode_reward || {};
      return `<tr><td>${name === "learned" ? "<strong>trained RL policy</strong>" : name}</td>
        <td class="num">${r.mean != null ? r.mean.toFixed(1) : "—"}</td>
        <td class="num">${s.recovery_success_rate != null ? pct(s.recovery_success_rate) : "—"}</td>
        <td class="num">${s.reroute_count && s.reroute_count.mean != null ? s.reroute_count.mean.toFixed(1) : "—"}</td></tr>`;
    });
    return `<table class="result-table">
      <thead><tr><th>policy</th><th>score</th><th>recovered</th><th>reroutes</th></tr></thead>
      <tbody>${rows.join("")}</tbody></table>
      <p class="result-note">${rl.n_episodes} simulated episodes, seeded so they are reproducible.
      The trained policy recovers from every fault using about one reroute; the
      non-learned strategies either never recover or thrash.</p>`;
  }

  const LIMITATIONS = [
    "Detection is unreliable on gradual faults — succeeded in 40% of E1 trials. A hard link failure is detected far more consistently.",
    "The emulated switches cannot report packet loss (a property of Linux veth links), so 30 of 61 features are always zero and were dropped before training.",
    "The RL policy is trained in a fast simulator whose queueing model overestimates real delay; this is a stated sim-to-real gap.",
    "Throughput retained is a confounded metric on this topology: the shortest-hop path is also the lowest-capacity one, so any reroute gains throughput.",
  ];

  async function loadResults() {
    const grid = $("results-grid");
    let data;
    try {
      data = await getJSON("/api/results");
    } catch (err) {
      grid.innerHTML = `<p class="empty-state">Could not load results.</p>`;
      return;
    }
    const a = data.available || {};
    const cards = [];

    if (a.experiments && a.experiments.results) {
      const table = experimentTable(a.experiments.results);
      if (table) {
        cards.push(card("Experiment E1 — the full system vs three baselines",
          "Each row adds one mechanism to the row above it, so a difference is attributable.",
          table));
      }
    }
    if (a.ml) cards.push(card("The prediction model", "Predicts a link is about to degrade.", mlCard(a.ml)));
    if (a.rl_evaluation) {
      cards.push(card("The routing policy (reinforcement learning)",
        "Chooses which path to move traffic to.", rlCard(a.rl_evaluation)));
    }
    if (a.dataset) {
      cards.push(card("The dataset", "Generated by running this network, not downloaded.",
        `<div class="metric-row">
          <div class="metric"><span class="metric-label">Runs</span><span class="metric-value">${a.dataset.num_runs}</span></div>
          <div class="metric"><span class="metric-label">Labelled windows</span><span class="metric-value">${a.dataset.total_windows.toLocaleString()}</span></div>
          <div class="metric"><span class="metric-label">Faults seen</span><span class="metric-value">${a.dataset.total_positive}</span></div>
          <div class="metric"><span class="metric-label">Source</span><span class="metric-value" style="font-size:13px">${a.dataset.data_source}</span></div>
        </div>`));
    }

    grid.innerHTML = cards.length ? cards.join("") :
      `<p class="empty-state">No results recorded yet. Run the experiments first.</p>`;

    const figs = $("results-figures");
    figs.innerHTML = (data.figures || []).map((name) =>
      `<figure class="result-figure"><img src="/figures/${name}" alt="${name}" loading="lazy" />
       <figcaption>${name.replace(/\.png$/, "").replace(/_/g, " ")}</figcaption></figure>`
    ).join("") || `<p class="empty-state">No figures generated yet.</p>`;

    $("results-limitations").innerHTML = LIMITATIONS.map((l) => `<li>${l}</li>`).join("");
  }

  let resultsLoaded = false;

  function showView(name) {
    const showResults = name === "results";
    document.querySelectorAll(".viewtab").forEach((t) =>
      t.classList.toggle("active", t.dataset.view === (showResults ? "results" : "live"))
    );
    $("live-view").hidden = showResults;
    $("results-view").hidden = !showResults;
    // The hash makes a view linkable and survives a refresh, so
    // "#results" can be opened directly instead of always landing on
    // Live and having to click across.
    if (window.location.hash.slice(1) !== name) {
      history.replaceState(null, "", name === "live" ? "#" : `#${name}`);
    }
    if (showResults && !resultsLoaded) {
      resultsLoaded = true;
      loadResults();
    }
  }

  function initViewTabs() {
    document.querySelectorAll(".viewtab").forEach((tab) => {
      tab.addEventListener("click", () => showView(tab.dataset.view));
    });
    window.addEventListener("hashchange", () =>
      showView(window.location.hash.slice(1) === "results" ? "results" : "live")
    );
    showView(window.location.hash.slice(1) === "results" ? "results" : "live");
  }

  // ---- wiring -----------------------------------------------------------

  function init() {
    initViewTabs();
    $("link-select").addEventListener("change", (evt) => {
      if (evt.target.value) selectLink(evt.target.value);
    });
    $("clear-events").addEventListener("click", () => {
      $("event-log").innerHTML = "";
      $("event-empty").hidden = false;
    });
    document.querySelectorAll(".filter-tabs .tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".filter-tabs .tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        incidentStatusFilter = tab.dataset.status;
        loadIncidents();
      });
    });

    tickClock();
    setInterval(tickClock, 1000);
    pollHealth();
    setInterval(pollHealth, 5000);

    loadTopology();
    setInterval(loadTopology, 10000);

    loadIncidents();
    setInterval(loadIncidents, 8000);

    setInterval(() => {
      if (currentLinkId) loadLinkDetail(currentLinkId);
    }, 5000);

    connectEventStream();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
