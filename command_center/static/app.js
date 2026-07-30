const state = {
  data: null,
  selectedSignal: null,
  signalFilter: "actionable",
  selectedReasonIds: new Set(),
  editingDecisionId: null,
  decisionSort: { key: "updated", direction: "desc" },
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
  renderJournal();
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
    ${signal.decision_id ? `<p class="microcopy">Decision #${signal.decision_id}: ${esc(signal.decision_thesis)}</p>` : ""}
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

function openDecision(signal = null, trade = null, decision = null) {
  const form = $("#decisionForm");
  form.reset();
  state.editingDecisionId = decision?.id || null;
  state.selectedReasonIds = new Set(decision?.reason_ids || []);
  $("#decisionSignalId").value = decision?.signal_id || signal?.id || "";
  $("#decisionTradeId").value = decision ? "" : trade?.id || "";
  $("#decisionTradeId").dataset.lifecycle = trade?.is_lifecycle ? "1" : "0";
  $("#dialogEyebrow").textContent = decision ? "Edit journal entry" : trade ? "Fast trade journal" : "Pre-trade record";
  $("#dialogTitle").textContent = decision
    ? `Edit decision #${decision.id}`
    : trade
    ? `${trade.side.toUpperCase()} ${trade.symbol}`
    : signal ? signal.title : "New standalone decision";
  form.elements.direction.value = decision?.direction || trade?.side || signal?.direction || "long";
  if (signal) form.elements.thesis.value = `${signal.title} — `;
  if (trade) {
    form.elements.thesis.value = `${trade.side.toUpperCase()} ${trade.symbol} — `;
    form.elements.entry.value = trade.entry ?? "";
    form.elements.notes.value = [
      `Imported ${trade.source} trade`,
      trade.exit != null ? `Exit ${trade.exit}` : "",
      trade.pnl != null ? `Realized P&L ${money(trade.pnl, 2)} (${pct(trade.pnl_pct)})` : "",
      trade.management_style || "",
      trade.fill_count ? `${trade.fill_count} fills in ${(trade.entry_batch_count || 0) + (trade.exit_batch_count || 0)} execution batches` : "",
      trade.partial_exit_count ? `${trade.partial_exit_count} partial exit${trade.partial_exit_count === 1 ? "" : "s"}` : ""
    ].filter(Boolean).join(" · ");
  }
  if (decision) {
    for (const field of [
      "thesis", "direction", "confidence", "entry", "invalidation",
      "target", "max_risk_usd", "notes"
    ]) {
      form.elements[field].value = decision[field] ?? "";
    }
  }
  $(".primary-button[type='submit']", form).textContent = decision ? "Save changes" : "Save decision";
  renderReasonPicker();
  $("#decisionDialog").showModal();
  form.elements.thesis.focus();
}

function renderJournal() {
  const decisions = state.data.decisions || [];
  const trades = state.data.trades || [];
  const statusCounts = decisions.reduce((acc, item) => {
    const status = item.effective_status || item.status;
    acc[status] = (acc[status] || 0) + 1;
    return acc;
  }, {});
  const linked = trades.filter(item => item.decision_id).length;
  $("#journalStats").innerHTML = [
    ["PLANNED", statusCounts.planned || 0],
    ["ACTIVE", statusCounts.active || 0],
    ["CLOSED", statusCounts.closed || 0],
    ["TRADES LINKED", `${linked} / ${trades.length}`],
    ["DATA QUALITY", state.data.evaluation ? `${state.data.evaluation.score}%` : "—"]
  ].map(([label, value], index) => `<div class="stat-tone-${index + 1}"><span>${label}</span><b>${value}</b></div>`).join("");
  renderDecisionTable();
  const unlinked = trades.filter(item => !item.decision_id);
  $("#unlinkedCount").textContent = `${unlinked.length} pending`;
  $("#unlinkedTrades").innerHTML = unlinked.length ? unlinked.map(trade => `
    <article class="trade-card lifecycle-card ${sideClass(trade.side)}">
      <header>
        <div class="trade-badges"><span class="badge neutral">${esc(trade.source)}</span><span class="status-pill ${trade.status === "closed" ? "closed" : "active"}">${esc(trade.status)}</span></div>
        <b class="${valueClass(trade.pnl)}">${money(trade.pnl, 2)}</b>
      </header>
      <h3>${sidePill(trade.side)} <span>${esc(trade.symbol)}</span></h3>
      <p>${when(trade.opened_at || trade.occurred_at)}${trade.closed_at ? ` → ${when(trade.closed_at)}` : " · still open"} · ${holdTime(trade.opened_at, trade.closed_at)}</p>
      <div class="lifecycle-metrics">
        <div><span>AVG ENTRY</span><b>${price(trade.entry)}</b></div>
        <div><span>AVG EXIT</span><b>${price(trade.exit)}</b></div>
        <div><span>MAX SIZE</span><b>${compactNumber(trade.max_size)}</b></div>
        <div><span>RETURN</span><b class="${valueClass(trade.pnl)}">${pct(trade.pnl_pct)}</b></div>
      </div>
      <div class="management-tags">
        <span>${esc(trade.management_style || "Execution grouped")}</span>
        <span>${trade.fill_count || 1} raw fill${trade.fill_count === 1 ? "" : "s"}</span>
        <span>${(trade.entry_batch_count || 0) + (trade.exit_batch_count || 0)} execution batch${((trade.entry_batch_count || 0) + (trade.exit_batch_count || 0)) === 1 ? "" : "es"}</span>
        ${trade.partial_exit_count ? `<span class="profit-tag">${trade.partial_exit_count} partial exit${trade.partial_exit_count === 1 ? "" : "s"}</span>` : ""}
      </div>
      ${renderExecutionTimeline(trade)}
      <div class="trade-card-actions">
        <button class="row-button primary-journal" data-journal-trade="${trade.id}">Journal trade</button>
        <button class="row-button" data-link-trade="${trade.id}">Link existing</button>
      </div>
    </article>`).join("") : `<div class="blank">Every imported trade is linked. The feedback loop is complete.</div>`;
  $$("[data-journal-trade]").forEach(button => button.addEventListener("click", () => {
    const trade = trades.find(item => item.id === Number(button.dataset.journalTrade));
    openDecision(null, trade);
  }));
  $$("[data-link-trade]").forEach(button => button.addEventListener("click", () => openLink(Number(button.dataset.linkTrade))));
}

function renderExecutionTimeline(trade) {
  const batches = trade.batches || [];
  if (!batches.length) return "";
  return `<details class="execution-details">
    <summary>Execution timeline <span>${batches.length} steps</span></summary>
    <div class="execution-timeline">${batches.map((batch, index) => `
      <div class="execution-step ${batch.family} ${valueClass(batch.pnl)}">
        <i></i>
        <div>
          <header><b>${esc(batch.label)}</b><time>${when(batch.started_at)}</time></header>
          <p>${compactNumber(batch.size)} @ ${price(batch.price)} · ${batch.fill_count} fill${batch.fill_count === 1 ? "" : "s"}</p>
        </div>
        <strong class="${valueClass(batch.pnl)}">${
          batch.family === "exit" && batch.pnl != null ? money(batch.pnl, 2)
          : batch.family === "management" ? "TURN"
          : batch.label === "Entry" ? "OPEN" : "ADD"
        }</strong>
      </div>`).join("")}
    </div>
  </details>`;
}

function compactNumber(value) {
  if (value == null) return "—";
  return Intl.NumberFormat(undefined, { notation: Math.abs(value) >= 10000 ? "compact" : "standard", maximumFractionDigits: 4 }).format(value);
}

function price(value) {
  if (value == null) return "—";
  const digits = Math.abs(value) < 0.01 ? 6 : Math.abs(value) < 1 ? 4 : 2;
  return `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: digits })}`;
}

function holdTime(openedAt, closedAt) {
  if (!openedAt) return "Unknown duration";
  const ms = Math.max(0, new Date(closedAt || Date.now()) - new Date(openedAt));
  const minutes = Math.floor(ms / 60000);
  if (minutes < 60) return `${minutes}m held`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ${minutes % 60}m held`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h held`;
}

function renderDecisionTable() {
  const query = ($("#journalSearch")?.value || "").toLowerCase();
  const status = $("#journalStatus")?.value || "all";
  const direction = $("#journalDirection")?.value || "all";
  const linkage = $("#journalLinkage")?.value || "all";
  const items = (state.data.decisions || []).filter(item => {
    const haystack = `${item.thesis} ${item.signal_title || ""} ${item.signal_symbol || ""} ${item.reason_labels || ""}`.toLowerCase();
    const effectiveStatus = item.effective_status || item.status;
    return (!query || haystack.includes(query))
      && (status === "all" || effectiveStatus === status)
      && (direction === "all" || item.direction === direction)
      && (linkage === "all" || (linkage === "linked" ? item.linked_trades > 0 : item.linked_trades === 0));
  }).sort(compareDecisions);
  updateDecisionSortHeaders();
  $("#decisionTable").innerHTML = items.length ? items.map(item => `
    <tr class="decision-row ${esc(item.effective_status || item.status)} ${sideClass(item.direction)}">
      <td>${when(item.updated_at)}<small>#${item.id}</small></td>
      <td><strong>${esc(item.signal_symbol || item.direction.toUpperCase())}</strong><small>${esc(item.signal_source || "standalone")}</small></td>
      <td>
        <strong>${esc(item.thesis)}</strong>
        <small>${esc(item.signal_title || "No linked signal")}</small>
        ${item.reason_labels ? `<div class="reason-tags">${item.reason_labels.split(" · ").map(reason => `<span>${esc(reason)}</span>`).join("")}</div>` : ""}
      </td>
      <td>${sidePill(item.direction)}<small><b class="risk-value">${money(item.max_risk_usd)}</b> risk · ${item.confidence}% confidence</small></td>
      <td><span class="status-pill ${esc(item.effective_status || item.status)}">${esc(item.effective_status || item.status)}</span></td>
      <td class="${valueClass(item.display_pnl)}">${money(item.display_pnl, 2)}<small>${item.effective_status === "active" && item.unrealized_pnl != null ? `live incl. ${money(item.unrealized_pnl, 2)} unrealized` : `${item.linked_trades} trade${item.linked_trades === 1 ? "" : "s"}`}</small></td>
      <td class="decision-actions"><button class="row-button" data-edit="${item.id}">Edit</button>${item.linked_trades ? "" : `<button class="row-button" data-cycle="${item.id}" data-current="${esc(item.status)}">${item.status === "closed" ? "Closed" : "Advance"}</button>`}</td>
    </tr>`).join("") : `<tr><td colspan="7"><div class="blank">No decisions match this filter.</div></td></tr>`;
  $$("[data-cycle]").forEach(button => button.addEventListener("click", () => advanceDecision(button)));
  $$("[data-edit]").forEach(button => button.addEventListener("click", () => {
    const decision = (state.data.decisions || []).find(item => item.id === Number(button.dataset.edit));
    if (decision) openDecision(null, null, decision);
  }));
}

function decisionSortValue(item, key) {
  if (key === "updated") return new Date(item.updated_at || 0).getTime();
  if (key === "asset") return item.signal_symbol || item.direction || "";
  if (key === "thesis") return item.thesis || "";
  if (key === "plan") return Number(item.max_risk_usd ?? item.confidence ?? 0);
  if (key === "status") return item.effective_status || item.status || "";
  if (key === "result") return item.display_pnl == null ? null : Number(item.display_pnl);
  return "";
}

function compareDecisions(a, b) {
  const { key, direction } = state.decisionSort;
  const left = decisionSortValue(a, key);
  const right = decisionSortValue(b, key);
  if (left == null && right == null) return Number(b.id || 0) - Number(a.id || 0);
  if (left == null) return 1;
  if (right == null) return -1;
  const comparison = typeof left === "number" && typeof right === "number"
    ? left - right
    : String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: "base" });
  return (direction === "asc" ? comparison : -comparison)
    || Number(b.id || 0) - Number(a.id || 0);
}

function updateDecisionSortHeaders() {
  $$("[data-decision-sort]").forEach(header => {
    const active = header.dataset.decisionSort === state.decisionSort.key;
    header.classList.toggle("sorted", active);
    header.setAttribute("aria-sort", active
      ? (state.decisionSort.direction === "asc" ? "ascending" : "descending")
      : "none");
    $(".sort-indicator", header).textContent = active
      ? (state.decisionSort.direction === "asc" ? "▲" : "▼")
      : "↕";
  });
}

function setDecisionSort(key) {
  if (state.decisionSort.key === key) {
    state.decisionSort.direction = state.decisionSort.direction === "asc" ? "desc" : "asc";
  } else {
    state.decisionSort = {
      key,
      direction: ["updated", "plan", "result"].includes(key) ? "desc" : "asc"
    };
  }
  renderDecisionTable();
}

async function advanceDecision(button) {
  const next = { planned: "active", active: "closed", invalidated: "closed", skipped: "planned" }[button.dataset.current];
  if (!next) return;
  try {
    await api(`/api/decisions/${button.dataset.cycle}`, { method: "PATCH", body: JSON.stringify({ status: next }) });
    await load();
    toast(`Decision moved to ${next}`);
  } catch (error) { toast(error.message); }
}

function openLink(tradeId) {
  const select = $("#linkDecisionId");
  const candidates = (state.data.decisions || []).filter(item => item.status !== "closed");
  if (!candidates.length) {
    const trade = (state.data.trades || []).find(item => item.id === tradeId);
    toast("Start a journal entry for this trade");
    openDecision(null, trade);
    return;
  }
  select.innerHTML = candidates.map(item => `<option value="${item.id}">#${item.id} · ${esc(item.signal_symbol || item.direction)} · ${esc(item.thesis.slice(0, 70))}</option>`).join("");
  $("#linkTradeId").value = tradeId;
  const trade = (state.data.trades || []).find(item => item.id === tradeId);
  $("#linkTradeId").dataset.lifecycle = trade?.is_lifecycle ? "1" : "0";
  $("#linkDialog").showModal();
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
  journal: ["Research ledger", "Every decision, connected"],
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
  const tradeId = Number($("#decisionTradeId").value || 0);
  const trade = (state.data.trades || []).find(item => item.id === tradeId);
  if (tradeId) payload.status = trade?.status === "closed" ? "closed" : "active";
  try {
    const editing = state.editingDecisionId;
    const decision = await api(
      editing ? `/api/decisions/${editing}` : "/api/decisions",
      { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) }
    );
    if (tradeId) {
      const linkPayload = $("#decisionTradeId").dataset.lifecycle === "1"
        ? { lifecycle_id: tradeId } : { trade_id: tradeId };
      await api(`/api/decisions/${decision.id}/trades`, {
        method: "POST", body: JSON.stringify(linkPayload)
      });
    }
    $("#decisionDialog").close();
    await load();
    toast(editing ? "Journal entry updated" : tradeId ? "Trade journal saved and linked" : "Decision saved");
  } catch (error) { toast(error.message); }
});

$("#linkForm").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const tradeId = Number($("#linkTradeId").value);
    const linkPayload = $("#linkTradeId").dataset.lifecycle === "1"
      ? { lifecycle_id: tradeId } : { trade_id: tradeId };
    await api(`/api/decisions/${$("#linkDecisionId").value}/trades`, {
      method: "POST", body: JSON.stringify(linkPayload)
    });
    $("#linkDialog").close();
    await load();
    toast("Trade linked—the feedback loop is complete");
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
$("#journalSearch").addEventListener("input", renderDecisionTable);
$("#journalStatus").addEventListener("change", renderDecisionTable);
$("#journalDirection").addEventListener("change", renderDecisionTable);
$("#journalLinkage").addEventListener("change", renderDecisionTable);
$$("[data-decision-sort]").forEach(header => {
  header.addEventListener("click", () => setDecisionSort(header.dataset.decisionSort));
  header.addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    setDecisionSort(header.dataset.decisionSort);
  });
});
$$("[data-dialog-close]").forEach(button => button.addEventListener("click", () => {
  const dialog = document.getElementById(button.dataset.dialogClose);
  if (dialog?.open) dialog.close();
}));
$$(".nav-item").forEach(item => item.addEventListener("click", () => switchView(item.dataset.view)));
$$("[data-filter]").forEach(item => item.addEventListener("click", () => {
  state.signalFilter = item.dataset.filter;
  $$("[data-filter]").forEach(button => button.classList.toggle("active", button === item));
  renderSignals();
}));
setInterval(() => $("#clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), 1000);
$("#clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
load();
