const state = {
  data: null,
  selectedSignal: null,
  signalFilter: "actionable",
  selectedReasonIds: new Set(),
  view: "today"
};
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const money = (value, digits = 0) => value == null ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: digits }).format(value);
const pct = (value, digits = 1) => value == null ? "—" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(digits)}%`;
const compact = value => value == null ? "—" : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value);
const when = value => {
  const date = new Date(value);
  const diff = Date.now() - date.getTime();
  if (diff < 60_000) return "now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
};
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const valueClass = value => Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";
const sideClass = side => ["long", "short"].includes(String(side || "").toLowerCase())
  ? String(side).toLowerCase() : "neutral";
const sidePill = side => {
  const normalized = sideClass(side);
  const marker = normalized === "long" ? "▲" : normalized === "short" ? "▼" : "•";
  return `<span class="side-pill ${normalized}">${marker} ${esc(String(side || "neutral").toUpperCase())}</span>`;
};
const qualityClass = value => Number(value) >= 55 ? "positive" : Number(value) < 45 ? "negative" : "warning";
const tradeJournalUrl = params => {
  const host = window.location.hostname;
  const local = host === "127.0.0.1" || host === "localhost";
  const journalHost = local ? host : host.replace(/^command\./, "journal.");
  const url = new URL(`${window.location.protocol}//${journalHost}/`);
  if (local) url.port = "8811";
  url.search = "";
  url.hash = "";
  Object.entries(params || {}).forEach(([key, value]) => url.searchParams.set(key, value));
  return url.toString();
};
const tradeJournalLink = $("[data-trade-journal-link]");
if (tradeJournalLink) tradeJournalLink.href = tradeJournalUrl({});

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 2600);
}

async function load(showToast = false) {
  try {
    state.data = await api("/api/bootstrap");
    renderAll();
    if (showToast) toast("All sources synced");
  } catch (error) {
    toast(error.message);
    $("#signalQueue").className = "signal-list";
    $("#signalQueue").innerHTML = `<div class="blank">The command center could not load. ${esc(error.message)}</div>`;
  }
}

function renderAll() {
  renderSummary();
  renderSignals();
  renderEdge();
  renderReview();
  switchView(state.view);
}

function renderSummary() {
  const s = state.data.summary;
  const quality = s.signal_quality || {};
  $("#riskAvailable").textContent = money(s.risk.available);
  $("#riskAvailable").className = `metric-value ${s.risk.utilization_pct > 100 ? "negative" : s.risk.utilization_pct > 80 ? "warning" : "positive"}`;
  $("#riskBudget").textContent = `of ${money(s.risk.daily_budget)} daily budget`;
  $("#riskMeter").style.width = `${Math.min(100, Math.max(0, s.risk.utilization_pct))}%`;
  $("#riskMeter").style.background = s.risk.utilization_pct > 100 ? "var(--red)" : "var(--acid)";
  $("#openPositions").textContent = s.positions.fresh_count || 0;
  $("#positionNotional").textContent = s.positions.stale_count
    ? `${s.positions.stale_count} stale snapshot${s.positions.stale_count === 1 ? "" : "s"} excluded`
    : `${money(s.positions.notional)} notional`;
  $(".positions-metric").classList.toggle("has-warning", Boolean(s.positions.stale_count));
  $("#realizedPnl").textContent = money(s.trades.realized_pnl, 2);
  $("#realizedPnl").className = `metric-value ${valueClass(s.trades.realized_pnl)}`;
  $("#signalEdge").textContent = pct(s.edge.average_edge_24h);
  $("#signalEdge").className = `metric-value ${valueClass(s.edge.average_edge_24h)}`;
  $("#pulseSignals").textContent = quality.actionable_count || 0;
  $("#pulseOutcomes").textContent = s.edge.sample_size;
  $("#pulseExcluded").textContent = quality.simulation_count || 0;
  const latest = quality.latest_production_at;
  $("#integrityNote").textContent = latest
    ? `Latest production signal ${when(latest)}. ${quality.archived_count || 0} older signals are kept in the archive.`
    : "No production signals have been imported yet. Simulations remain excluded from edge statistics.";
  const run = s.last_sync;
  $("#lastSync").textContent = run?.finished_at ? `Last sync ${when(run.finished_at)} · ${run.status}` : "Waiting for first sync";
}

function visibleSignals() {
  const all = state.data.signals || [];
  if (state.signalFilter === "actionable") {
    return all.filter(item => !item.is_simulation && ["live", "recent"].includes(item.freshness));
  }
  if (state.signalFilter === "archive") {
    return all.filter(item => !item.is_simulation && item.freshness === "archive");
  }
  if (state.signalFilter === "simulation") {
    return all.filter(item => Boolean(item.is_simulation));
  }
  return all.filter(item => !item.is_simulation && item.source === state.signalFilter);
}

function renderSignals() {
  const target = $("#signalQueue");
  target.className = "signal-list";
  const signals = visibleSignals();
  if (!signals.length) {
    const emptyCopy = state.signalFilter === "actionable"
      ? "No production signals from the last seven days need attention."
      : "No signals in this view yet.";
    target.innerHTML = `<div class="blank">${emptyCopy}</div>`;
    state.selectedSignal = null;
    renderFocus(null);
    return;
  }
  if (!state.selectedSignal || !signals.some(item => item.id === state.selectedSignal.id)) {
    state.selectedSignal = signals[0];
  }
  target.innerHTML = signals.map(signal => `
    <article class="signal-row ${sideClass(signal.direction)} ${state.selectedSignal?.id === signal.id ? "selected" : ""}" data-signal="${signal.id}" tabindex="0">
      <span class="severity-dot ${esc(signal.severity)}"></span>
      <div>
        <div class="signal-meta"><span>${esc(signal.source)}</span>${sidePill(signal.direction)}<span>${esc(signal.detector || signal.event_type)}</span><span class="freshness ${esc(signal.freshness)}">${esc(signal.freshness)}</span><span>${when(signal.occurred_at)}</span><span>${esc(signal.status)}</span></div>
        <h3>${esc(signal.title)}</h3>
        <p>${esc(signal.summary)}</p>
      </div>
      <div class="signal-outcome">
        <b class="${valueClass(signal.return_24h)}">${signal.return_24h == null ? "Pending" : pct(signal.return_24h)}</b>
        <small>${signal.return_24h == null ? `${signal.outcome_count} measured` : "24h edge"}</small>
      </div>
    </article>`).join("");
  $$("[data-signal]", target).forEach(row => {
    const choose = () => {
      state.selectedSignal = signals.find(item => item.id === Number(row.dataset.signal));
      renderSignals();
    };
    row.addEventListener("click", choose);
    row.addEventListener("keydown", event => { if (event.key === "Enter") choose(); });
  });
  renderFocus(state.selectedSignal);
}

function renderFocus(signal) {
  if (!signal) {
    $("#focusSource").textContent = "Clear";
    $("#focusSource").className = "badge green";
    $("#focusCard").className = "empty-state";
    $("#focusCard").innerHTML = `<span class="empty-glyph">✓</span><p>Nothing in this queue needs review. Check the archive or simulations for research.</p>`;
    return;
  }
  $("#focusSource").textContent = signal.is_simulation ? "simulation" : signal.source;
  $("#focusSource").className = `badge ${signal.is_simulation ? "amber" : signal.severity === "critical" || signal.severity === "high" ? "red" : "neutral"}`;
  const confidence = signal.confidence == null ? "—" : `${Math.round(signal.confidence * 100)}%`;
  const impact = signal.baseline_value && signal.observed_value
    ? pct((signal.observed_value / signal.baseline_value - 1) * 100)
    : signal.observed_value == null ? "—" : Number(signal.observed_value).toFixed(2);
  $("#focusCard").className = "focus-content";
  $("#focusCard").innerHTML = `
    <div class="signal-meta"><span>${esc(signal.symbol || "MARKET")}</span>${sidePill(signal.direction)}<span>${new Date(signal.occurred_at).toLocaleString()}</span></div>
    <h3>${esc(signal.title)}</h3>
    <p>${esc(signal.summary)}</p>
    ${signal.is_simulation ? `<div class="integrity-alert">Simulation evidence · excluded from production edge statistics and weekly discipline.</div>` : ""}
    ${signal.freshness === "archive" ? `<div class="integrity-alert archive-alert">Historical signal · use for retrospective research, not a live entry.</div>` : ""}
    <div class="evidence-grid">
      <div><span>Severity</span><b>${esc(signal.severity)}</b></div>
      <div><span>Confidence</span><b>${confidence}</b></div>
      <div><span>Observed</span><b class="${valueClass(impact)}">${impact}</b></div>
      <div><span>Priority</span><b>${Math.round(signal.priority_score || 0)}</b></div>
    </div>
    ${signal.decision_id ? `<p class="microcopy">Decision #${signal.decision_id}: ${esc(signal.decision_thesis)} · <a href="${esc(tradeJournalUrl({ decision_id: signal.decision_id }))}">Open in Trade Journal</a></p>` : ""}
    <div class="focus-actions">
      ${signal.is_simulation ? "" : `<button class="primary" id="focusDecide">${signal.freshness === "archive" ? "Add retrospective" : signal.decision_id ? "Add another thesis" : "Record decision"}</button>`}
      <button data-status="ignored">Ignore + track</button>
      <button data-status="dismissed">Dismiss</button>
    </div>`;
  $("#focusDecide")?.addEventListener("click", () => openDecision(signal));
  $$("[data-status]", $("#focusCard")).forEach(button => button.addEventListener("click", () => setSignalStatus(signal.id, button.dataset.status)));
}

async function setSignalStatus(id, status) {
  try {
    await api(`/api/signals/${id}/status`, { method: "PATCH", body: JSON.stringify({ status }) });
    state.data.signals.find(item => item.id === id).status = status;
    renderSignals();
    renderSummary();
    toast(`Signal marked ${status}`);
  } catch (error) { toast(error.message); }
}

function renderReasonPicker() {
  const categories = state.data?.reasons?.categories || [];
  $("#reasonGroups").innerHTML = categories.map(group => `
    <div class="reason-group">
      <span class="reason-group-label">${esc(group.name)}</span>
      <div class="reason-chips">
        ${group.reasons.map(reason => `
          <button type="button"
            class="reason-chip ${reason.is_custom ? "custom" : ""} ${state.selectedReasonIds.has(reason.id) ? "selected" : ""}"
            data-reason-id="${reason.id}"
            aria-pressed="${state.selectedReasonIds.has(reason.id)}">${esc(reason.label)}</button>
        `).join("")}
      </div>
    </div>
  `).join("");
  $("#reasonCount").textContent = `${state.selectedReasonIds.size} selected`;
  $$("[data-reason-id]", $("#reasonGroups")).forEach(button => button.addEventListener("click", () => {
    const id = Number(button.dataset.reasonId);
    if (state.selectedReasonIds.has(id)) state.selectedReasonIds.delete(id);
    else if (state.selectedReasonIds.size < 12) state.selectedReasonIds.add(id);
    else return toast("Choose at most 12 reasons");
    renderReasonPicker();
  }));
}

function openDecision(signal = null) {
  const form = $("#decisionForm");
  form.reset();
  state.selectedReasonIds = new Set();
  $("#decisionSignalId").value = signal?.id || "";
  $("#dialogEyebrow").textContent = "Pre-trade record";
  $("#dialogTitle").textContent = signal ? signal.title : "New standalone decision";
  form.elements.direction.value = signal?.direction || "long";
  if (signal) form.elements.thesis.value = `${signal.title} — `;
  $(".primary-button[type='submit']", form).textContent = "Save decision";
  renderReasonPicker();
  $("#decisionDialog").showModal();
  form.elements.thesis.focus();
}

function renderEdge() {
  const summary = state.data.summary.edge;
  const edge = state.data.edge;
  $("#edgePrecision").textContent = summary.precision_24h == null ? "—" : `${summary.precision_24h.toFixed(0)}%`;
  $("#edgeSample").textContent = summary.sample_size ? `${summary.sample_size} directional signals measured at 24 hours` : "Waiting for completed directional outcomes";
  const quality = edge.decision_quality || {};
  $("#actedEdge").textContent = pct(quality.acted_edge);
  $("#ignoredEdge").textContent = pct(quality.ignored_edge);
  const values = [Math.abs(quality.acted_edge || 0), Math.abs(quality.ignored_edge || 0), 1];
  const max = Math.max(...values);
  $("#actedBar").style.width = `${Math.min(100, Math.abs(quality.acted_edge || 0) / max * 100)}%`;
  $("#ignoredBar").style.width = `${Math.min(100, Math.abs(quality.ignored_edge || 0) / max * 100)}%`;
  if (quality.acted_edge != null && quality.ignored_edge != null) {
    const delta = quality.acted_edge - quality.ignored_edge;
    $("#decisionVerdict").textContent = delta >= 0
      ? `Your selection added ${delta.toFixed(2)} percentage points of 24h edge.`
      : `Ignored signals outperformed acted signals by ${Math.abs(delta).toFixed(2)} points—review your selection rules.`;
  }
  $("#strategyTable").innerHTML = edge.strategies.length ? edge.strategies.map((item, index) => `
    <div class="strategy-row ${index === 0 ? "top-strategy" : ""}">
      <div><b>${esc(item.strategy)}</b><small>${esc(item.source)} · ${item.samples} samples</small></div>
      <div><b class="${valueClass(item.avg_return)}">${pct(item.avg_return)}</b><small>avg edge</small></div>
      <div><b class="${qualityClass(item.hit_rate)}">${item.hit_rate.toFixed(0)}%</b><small>hit rate</small></div>
      <div><b>${pct(item.avg_mfe)}</b><small>avg mfe</small></div>
    </div>`).join("") : `<div class="blank">The strategy leaderboard appears after directional 24h outcomes complete.</div>`;
  const labels = { 0: "Impact", 60: "1H", 360: "6H", 1440: "24H", 10080: "7D" };
  $("#horizonChart").innerHTML = edge.horizons.length ? edge.horizons.map(item => `
    <div class="horizon-column">
      <b class="${qualityClass(item.hit_rate)}">${item.hit_rate.toFixed(0)}%</b>
      <i><em style="height:${Math.max(2, Math.min(100, item.hit_rate))}%"></em></i>
      <span>${labels[item.horizon_minutes] || item.horizon_minutes}</span>
    </div>`).join("") : `<div class="blank">Outcome horizons will fill automatically as market data arrives.</div>`;
}

function renderReview() {
  const review = state.data.weekly;
  const start = new Date(review.period_start);
  const end = new Date(review.period_end);
  $("#reviewDates").textContent = `${start.toLocaleDateString([], { month: "short", day: "numeric" })} — ${end.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}`;
  $("#disciplineScore").textContent = `${review.discipline_score.toFixed(0)}%`;
  $("#reviewKpis").innerHTML = [
    ["SIGNALS", review.signals.total || 0],
    ["CLOSED TRADES", review.trades.total || 0],
    ["REALIZED P&L", money(review.trades.pnl, 2)]
  ].map(([label, value], index) => `<div><span>${label}</span><b class="${index === 2 ? valueClass(review.trades.pnl) : ""}">${value}</b></div>`).join("");
  $("#reviewObservations").innerHTML = review.observations.map(item => `<li>${esc(item)}</li>`).join("");
  if (review.best_signal) $("#bestSignal").textContent = `${review.best_signal.symbol || review.best_signal.title} · ${pct(review.best_signal.signed_return_pct)}`;
  if (review.best_ignored) $("#bestIgnored").textContent = `${review.best_ignored.symbol || review.best_ignored.title} · ${pct(review.best_ignored.signed_return_pct)}`;
}

const viewCopy = {
  today: ["Decision queue", "What needs your attention"],
  edge: ["Signal intelligence", "Find what actually works"],
  review: ["Seven-day retrospective", "Turn experience into process"]
};

function switchView(name) {
  state.view = name;
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === name));
  $$(".view").forEach(item => item.classList.toggle("active", item.id === `${name}View`));
  const eyebrow = name === "today"
    ? `${new Date().toLocaleDateString([], {weekday:"long"})} · ${viewCopy[name][0]}`
    : viewCopy[name][0];
  $("#viewEyebrow").textContent = eyebrow;
  $("#viewTitle").textContent = viewCopy[name][1];
}

$("#decisionForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const raw = Object.fromEntries(new FormData(form));
  const payload = {};
  for (const [key, value] of Object.entries(raw)) {
    if (value !== "") payload[key] = ["signal_id", "confidence"].includes(key) ? Number(value) : ["entry", "invalidation", "target", "max_risk_usd"].includes(key) ? Number(value) : value;
  }
  payload.reason_ids = [...state.selectedReasonIds];
  try {
    await api("/api/decisions", { method: "POST", body: JSON.stringify(payload) });
    $("#decisionDialog").close();
    await load();
    toast("Decision saved");
  } catch (error) { toast(error.message); }
});


$("#riskForm").addEventListener("submit", async event => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget));
  payload.daily_risk_budget = Number(payload.daily_risk_budget);
  payload.max_open_decisions = Number(payload.max_open_decisions);
  try {
    state.data.settings = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    $("#riskDialog").close();
    await load();
    toast("Risk limits updated");
  } catch (error) { toast(error.message); }
});

$("#syncButton").addEventListener("click", async event => {
  event.currentTarget.disabled = true;
  event.currentTarget.textContent = "Syncing…";
  try { await api("/api/sync", { method: "POST" }); await load(true); }
  catch (error) { toast(error.message); }
  finally { event.currentTarget.disabled = false; event.currentTarget.textContent = "Sync all sources"; }
});
$("#newDecision").addEventListener("click", () => openDecision());
$("#addCustomReason").addEventListener("click", async () => {
  const category = $("#customReasonCategory").value;
  const label = $("#customReasonLabel").value.trim();
  if (!label) return toast("Write a reason first");
  try {
    const created = await api("/api/reasons", {
      method: "POST", body: JSON.stringify({ category, label })
    });
    state.data.reasons = await api("/api/reasons");
    state.selectedReasonIds.add(created.id);
    $("#customReasonLabel").value = "";
    renderReasonPicker();
    toast("Reusable reason added");
  } catch (error) { toast(error.message); }
});
$("#editRisk").addEventListener("click", () => {
  $("#riskBudgetInput").value = state.data.settings.daily_risk_budget;
  $("#maxDecisionsInput").value = state.data.settings.max_open_decisions;
  $("#riskDialog").showModal();
});
$("#copyReview").addEventListener("click", async () => {
  const r = state.data.weekly;
  const text = [`Crypto Scientist Weekly Review`, ...r.observations, `Discipline: ${r.discipline_score.toFixed(0)}%`, `Realized P&L: ${money(r.trades.pnl,2)}`].join("\n");
  await navigator.clipboard.writeText(text);
  toast("Weekly review copied");
});
$$("[data-dialog-close]").forEach(button => button.addEventListener("click", () => {
  const dialog = document.getElementById(button.dataset.dialogClose);
  if (dialog?.open) dialog.close();
}));
$$(".nav-item[data-view]").forEach(item => item.addEventListener("click", () => switchView(item.dataset.view)));
$$("[data-filter]").forEach(item => item.addEventListener("click", () => {
  state.signalFilter = item.dataset.filter;
  $$("[data-filter]").forEach(button => button.classList.toggle("active", button === item));
  renderSignals();
}));
const updateClock = () => { $("#clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
updateClock();
let clockTimer;
const scheduleClock = () => {
  clearTimeout(clockTimer);
  if (document.hidden) return;
  const delay = 60_000 - (Date.now() % 60_000) + 50;
  clockTimer = setTimeout(() => { updateClock(); scheduleClock(); }, delay);
};
document.addEventListener("visibilitychange", scheduleClock);
scheduleClock();
$$("[data-trade-journal-link]").forEach(link => { link.href = tradeJournalUrl(); });
load();
