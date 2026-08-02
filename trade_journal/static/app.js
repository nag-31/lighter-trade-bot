const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  summary: {},
  trades: [],
  decisions: [],
  positions: [],
  reasons: { categories: [], total: 0 },
  sort: { key: "updated", direction: "desc" },
  editingDecision: null,
  v2: { enabled: false, lifecycles: [] },
  v2SelectedId: null,
};

const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function numeric(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function pnlClass(value) {
  const amount = numeric(value);
  return amount > 0 ? "positive" : amount < 0 ? "negative" : "flat";
}

function formatMoney(value) {
  return money.format(numeric(value));
}

function formatTime(value) {
  if (!value) return "Not synced yet";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function relativeTime(value) {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  const seconds = Math.round((date.valueOf() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  const ranges = [
    ["year", 31536000],
    ["month", 2592000],
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  for (const [unit, size] of ranges) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return formatter.format(seconds, "second");
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 3000);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function positionFor(trade) {
  return state.positions.find((position) =>
    String(position.source || "").toLowerCase() === String(trade.source || "").toLowerCase()
    && String(position.symbol || "").toLowerCase() === String(trade.symbol || "").toLowerCase()
    && String(position.side || "").toLowerCase() === String(trade.side || "").toLowerCase()
  );
}

function isOpen(trade) {
  return !["closed", "reversed", "superseded"].includes(String(trade.status || "").toLowerCase());
}

function tradePnl(trade) {
  const realized = numeric(trade.pnl);
  const position = isOpen(trade) ? positionFor(trade) : null;
  return {
    realized,
    live: position && position.unrealized_pnl != null
      ? numeric(position.unrealized_pnl)
      : null,
  };
}

function currentPosition(trade) {
  return isOpen(trade) ? positionFor(trade) : null;
}

function currentPriceFor(trade) {
  const position = currentPosition(trade);
  return position?.current_price ?? trade.current_price ?? null;
}

function positionValueFor(trade) {
  const position = currentPosition(trade);
  if (position) return position.position_value != null ? numeric(position.position_value) : null;
  if (trade.position_value != null) return numeric(trade.position_value);
  return !isOpen(trade) && trade.notional != null ? numeric(trade.notional) : null;
}

function renderSummary() {
  const items = [
    ["Position chains", state.summary.trades || 0, `${state.summary.open_trades || 0} active`],
    ["Journal coverage", state.summary.journaled || 0, `${Math.max(0, numeric(state.summary.trades) - numeric(state.summary.journaled))} need review`],
    ["Realized PnL", formatMoney(state.summary.realized_pnl), "closed lifecycles", pnlClass(state.summary.realized_pnl)],
    [
      "Live PnL",
      state.summary.live_pnl == null ? "Awaiting marks" : formatMoney(state.summary.live_pnl),
      state.summary.live_pnl == null ? "source has no live marks" : `${state.summary.live_marks || 0} marked positions`,
      state.summary.live_pnl == null ? "warning" : pnlClass(state.summary.live_pnl),
    ],
  ];
  $("#summaryGrid").innerHTML = items.map(([label, value, note, tone = ""]) => `
    <article class="summary-card ${tone}">
      <span>${escapeHtml(label)}</span>
      <b>${escapeHtml(value)}</b>
      <small>${escapeHtml(note)}</small>
    </article>
  `).join("");
  $("#tradeCount").textContent = state.trades.length;
  $("#entryCount").textContent = state.decisions.length;
}

function filteredTrades() {
  const query = $("#tradeSearch").value.trim().toLowerCase();
  const status = $("#tradeStatus").value;
  const side = $("#tradeSide").value;
  const journal = $("#tradeJournalState").value;
  return state.trades.filter((trade) => {
    const haystack = `${trade.symbol || ""} ${trade.source || ""} ${trade.account || ""}`.toLowerCase();
    const tradeStatus = String(trade.status || "").toLowerCase();
    return (!query || haystack.includes(query))
      && (status === "all" || (status === "open" ? isOpen(trade) : tradeStatus === status))
      && (side === "all" || String(trade.side).toLowerCase() === side)
      && (journal === "all"
        || (journal === "journaled" ? Boolean(trade.decision_id) : !trade.decision_id));
  });
}

function executionEffect(item) {
  const family = String(item?.family || "").toLowerCase();
  const raw = String(item?.action || item?.kind || item?.type || "").toLowerCase();
  if (family === "entry") return "increased size";
  if (family === "exit") return "decreased size";

  const before = Number(item?.position_before);
  const after = Number(item?.position_after);
  if (Number.isFinite(before) && Number.isFinite(after) && before !== after) {
    return after > before ? "increased size" : "decreased size";
  }
  if (/(open|entry|add|increase|scale.?in)/.test(raw)) return "increased size";
  if (/(close|exit|reduce|decrease|scale.?out|partial)/.test(raw)) return "decreased size";
  return "position update";
}

function executionTransactionSide(item, trade, effect = executionEffect(item)) {
  const explicit = String(item?.execution_side || item?.order_side || item?.fill_side || (item?.side === "BUY" || item?.side === "SELL" ? item.side : "")).toUpperCase();
  if (explicit === "BUY" || explicit === "SELL") return explicit;
  const raw = String(item?.action || item?.kind || item?.type || "").toUpperCase();
  if (/\bBUY\b/.test(raw)) return "BUY";
  if (/\bSELL\b/.test(raw)) return "SELL";
  const positionSide = String(item?.position_side || item?.lifecycle_side || trade?.side || (/_(LONG|SHORT)$/.test(raw) ? raw.split("_").pop() : "")).toLowerCase();
  if (positionSide === "long") return effect === "decreased size" ? "SELL" : "BUY";
  if (positionSide === "short") return effect === "decreased size" ? "BUY" : "SELL";
  return "";
}

function executionLabel(item, trade, index = 0) {
  const effect = executionEffect(item);
  const side = executionTransactionSide(item, trade, effect);
  if (side) return `${side} · ${effect}`;
  const fallback = item?.label || item?.action || item?.kind || item?.type || `Fill ${index + 1}`;
  return `${fallback} · ${effect}`;
}

function batchRows(trade) {
  const batches = Array.isArray(trade.batches) ? trade.batches : [];
  if (batches.length) {
    return batches.slice(0, 7).map((batch, index) => {
      const action = executionLabel(batch, trade, index);
      const time = batch.occurred_at || batch.time || batch.started_at;
      const price = batch.price ?? batch.vwap;
      return `<li><span>${escapeHtml(action)}</span><b>${price != null ? formatMoney(price) : ""}</b><small>${escapeHtml(time ? relativeTime(time) : "")}</small></li>`;
    }).join("");
  }
  const executions = Array.isArray(trade.executions) ? trade.executions : [];
  return executions.slice(0, 7).map((execution, index) => `
    <li>
      <span>${escapeHtml(executionLabel(execution, trade, index))}</span>
      <b>${execution.price != null ? formatMoney(execution.price) : ""}</b>
      <small>${escapeHtml(execution.occurred_at ? relativeTime(execution.occurred_at) : "")}</small>
    </li>
  `).join("");
}

function executionRows(trade) {
  const batches = Array.isArray(trade.batches) ? trade.batches : [];
  const executions = Array.isArray(trade.executions) ? trade.executions : [];
  const source = batches.length ? batches : executions;
  return source.map((item, index) => ({
    action: executionLabel(item, trade, index),
    price: item.price ?? item.vwap,
    time: item.occurred_at || item.time || item.started_at,
  }));
}

function openTradeInspector(trade) {
  if (!trade) return;
  const dialog = $("#tradeInspectDialog");
  $("#inspectTitle").textContent = `${trade.symbol || "Unknown asset"} lifecycle`;
  $("#inspectMeta").textContent = `${trade.source || "Trade source"} Â· opened ${formatTime(trade.opened_at || trade.occurred_at)} Â· ${isOpen(trade) ? "ACTIVE" : String(trade.status || "CLOSED").toUpperCase()}`;
  const rows = executionRows(trade);
  const pnl = tradePnl(trade);
  $("#inspectBody").innerHTML = `
    <div class="inspect-summary">
      <div><span>Direction</span><b class="${escapeHtml(String(trade.side || "neutral").toLowerCase())}">${escapeHtml(String(trade.side || "neutral").toUpperCase())}</b></div>
      <div><span>${isOpen(trade) ? "Live PnL" : "Realized PnL"}</span><b class="${pnlClass(isOpen(trade) ? pnl.live : pnl.realized)}">${isOpen(trade) && pnl.live == null ? "Awaiting mark" : formatMoney(isOpen(trade) ? pnl.live : pnl.realized)}</b></div>
      <div><span>Trace completeness</span><b>${rows.length ? `${rows.length} events` : "No fills available"}</b></div>
    </div>
    <ol class="trace-list">${rows.length ? rows.map((row, index) => `
      <li><span class="trace-index">${String(index + 1).padStart(2, "0")}</span><span><b>${escapeHtml(row.action)}</b><small>${escapeHtml(row.time ? formatTime(row.time) : "Time unavailable")}</small></span><strong>${row.price != null ? formatMoney(row.price) : "—"}</strong></li>
    `).join("") : `<li class="trace-empty">No execution events were returned for this lifecycle.</li>`}</ol>`;
  if (!dialog.open) dialog.showModal();
}

function renderTrades() {
  const trades = filteredTrades();
  const grid = $("#tradeGrid");
  if (!trades.length) {
    grid.innerHTML = `<div class="empty-state"><b>No position lifecycles match.</b><span>Change the filters or run an explicit sync.</span></div>`;
    return;
  }
  grid.innerHTML = trades.map((trade) => {
    const side = String(trade.side || "neutral").toLowerCase();
    const pnl = tradePnl(trade);
    const open = isOpen(trade);
    const batches = batchRows(trade);
    const returnPct = trade.return_pct ?? trade.pnl_pct;
    const currentPrice = currentPriceFor(trade);
    const positionValue = positionValueFor(trade);
    return `
      <article class="trade-card ${escapeHtml(side)}">
        <div class="execution-tape" aria-hidden="true"></div>
        <header>
          <div>
            <span class="side ${escapeHtml(side)}">${escapeHtml(side.toUpperCase())}</span>
            <h2>${escapeHtml(trade.symbol || "Unknown asset")}</h2>
            <p>${escapeHtml(trade.source || "trade source")} · ${escapeHtml(formatTime(trade.opened_at || trade.occurred_at))}</p>
          </div>
          <span class="status ${open ? "active" : "closed"}">${open ? "ACTIVE" : escapeHtml(String(trade.status || "closed").toUpperCase())}</span>
        </header>
        <div class="metrics">
          <span><small>AVG ENTRY</small><b>${trade.entry != null ? formatMoney(trade.entry) : "—"}</b></span>
          <span><small>${open ? "CURRENT" : "AVG EXIT"}</small><b>${open ? (currentPrice != null ? formatMoney(currentPrice) : "Awaiting mark") : (trade.exit != null ? formatMoney(trade.exit) : "—")}</b></span>
          <span><small>POSITION VALUE</small><b>${positionValue != null ? formatMoney(positionValue) : "Awaiting mark"}</b></span>
          <span><small>${open ? "LIVE PNL" : "REALIZED PNL"}</small><b class="${pnlClass(open ? pnl.live : pnl.realized)}">${open && pnl.live == null ? "Awaiting mark" : formatMoney(open ? pnl.live : pnl.realized)}</b></span>
        </div>
        <div class="trade-meta">
          <span>${trade.fill_count || trade.executions?.length || 0} raw fills</span>
          <span>${trade.batch_count || trade.batches?.length || 0} execution batches</span>
          ${returnPct != null ? `<span class="${pnlClass(returnPct)}">${numeric(returnPct) >= 0 ? "+" : ""}${numeric(returnPct).toFixed(2)}%</span>` : ""}
        </div>
        ${batches ? `<details><summary>Execution tape <span>${trade.batches?.length || trade.executions?.length || 0} steps</span></summary><ol class="tape-list">${batches}</ol></details>` : ""}
        <footer>
          <button class="button quiet" data-inspect="${trade.id}">Review trace</button>
          ${trade.decision_id
            ? `<button class="button quiet" data-edit="${trade.decision_id}">Edit reasons &amp; notes</button>`
            : `<button class="button primary" data-journal="${trade.id}">Journal this lifecycle</button>`}
        </footer>
      </article>
    `;
  }).join("");
  $$("[data-inspect]", grid).forEach((button) => button.addEventListener("click", () => {
    openTradeInspector(state.trades.find((trade) => trade.id === Number(button.dataset.inspect)));
  }));
  $$("[data-journal]", grid).forEach((button) => button.addEventListener("click", () => {
    openEntry(null, state.trades.find((trade) => trade.id === Number(button.dataset.journal)));
  }));
  $$("[data-edit]", grid).forEach((button) => button.addEventListener("click", () => {
    openEntry(state.decisions.find((decision) => decision.id === Number(button.dataset.edit)));
  }));
}

function decisionAsset(decision) {
  return decision.signal_symbol
    || decision.trades?.[0]?.symbol
    || state.trades.find((trade) => trade.decision_id === decision.id)?.symbol
    || "—";
}

function filteredDecisions() {
  const query = $("#entrySearch").value.trim().toLowerCase();
  const status = $("#entryStatus").value;
  const side = $("#entrySide").value;
  const filtered = state.decisions.filter((decision) => {
    const effective = String(decision.effective_status || decision.status || "").toLowerCase();
    const haystack = `${decisionAsset(decision)} ${decision.thesis || ""} ${decision.reason_labels || ""}`.toLowerCase();
    return (!query || haystack.includes(query))
      && (status === "all" || effective === status)
      && (side === "all" || String(decision.direction || "neutral").toLowerCase() === side);
  });
  const key = state.sort.key;
  const multiplier = state.sort.direction === "asc" ? 1 : -1;
  return filtered.sort((a, b) => {
    let left;
    let right;
    if (key === "updated") [left, right] = [Date.parse(a.updated_at) || 0, Date.parse(b.updated_at) || 0];
    else if (key === "asset") [left, right] = [decisionAsset(a), decisionAsset(b)];
    else if (key === "thesis") [left, right] = [a.thesis || "", b.thesis || ""];
    else if (key === "side") [left, right] = [a.direction || "", b.direction || ""];
    else if (key === "status") [left, right] = [a.effective_status || a.status || "", b.effective_status || b.status || ""];
    else [left, right] = [numeric(a.display_pnl), numeric(b.display_pnl)];
    if (typeof left === "number") return (left - right) * multiplier;
    return String(left).localeCompare(String(right)) * multiplier;
  });
}

function renderDecisions() {
  const rows = filteredDecisions();
  $("#entryTable").innerHTML = rows.length ? rows.map((decision) => {
    const status = decision.effective_status || decision.status;
    const pnl = decision.display_pnl;
    return `
      <tr>
        <td><b>${escapeHtml(relativeTime(decision.updated_at))}</b><small>#${decision.id}</small></td>
        <td><b>${escapeHtml(decisionAsset(decision))}</b><small>${decision.linked_trades ? `${decision.linked_trades} lifecycle link${decision.linked_trades === 1 ? "" : "s"}` : "Standalone"}</small></td>
        <td><b>${escapeHtml(decision.thesis)}</b><small>${escapeHtml(decision.reason_labels || "No reasons selected")}</small></td>
        <td><span class="side ${escapeHtml(decision.direction)}">${escapeHtml(String(decision.direction || "neutral").toUpperCase())}</span></td>
        <td><span class="status ${status === "active" ? "active" : "closed"}">${escapeHtml(String(status || "planned").toUpperCase())}</span></td>
        <td><b class="${pnlClass(pnl)}">${pnl == null ? "—" : formatMoney(pnl)}</b><small>${status === "active" ? "realized + live" : "realized"}</small></td>
        <td><button class="button quiet compact" data-edit-entry="${decision.id}">Edit</button></td>
      </tr>
    `;
  }).join("") : `<tr><td colspan="7"><div class="empty-state"><b>No journal entries match.</b><span>Change the filters or create a journal entry.</span></div></td></tr>`;
  $$("[data-edit-entry]").forEach((button) => button.addEventListener("click", () => {
    openEntry(state.decisions.find((decision) => decision.id === Number(button.dataset.editEntry)));
  }));
}

function renderReasons(selected = []) {
  const selectedIds = new Set(selected.map(Number));
  $("#reasonGroups").innerHTML = state.reasons.categories.map((category) => `
    <fieldset>
      <legend>${escapeHtml(category.name)}</legend>
      <div class="reason-options">
        ${category.reasons.map((reason) => `
          <label class="reason-chip">
            <input type="checkbox" name="reason_ids" value="${reason.id}" ${selectedIds.has(Number(reason.id)) ? "checked" : ""}>
            <span>${escapeHtml(reason.label)}</span>
          </label>
        `).join("")}
      </div>
    </fieldset>
  `).join("");
  updateReasonCount();
  $$('input[name="reason_ids"]').forEach((input) => input.addEventListener("change", updateReasonCount));
}

function updateReasonCount() {
  const count = $$('input[name="reason_ids"]:checked').length;
  $("#reasonCount").textContent = `${count} selected`;
}

function openEntry(decision = null, trade = null) {
  state.editingDecision = decision || null;
  const form = $("#entryForm");
  form.reset();
  $("#tradeId").value = trade?.id || "";
  $("#tradeKind").value = trade?.is_lifecycle ? "lifecycle" : "trade";
  $("#dialogEyebrow").textContent = decision ? "EDIT JOURNAL RECORD" : trade ? "POSITION REVIEW" : "PRE-TRADE RECORD";
  $("#dialogTitle").textContent = decision
    ? `Edit ${decisionAsset(decision)} entry`
    : trade ? `Journal ${String(trade.side || "").toUpperCase()} ${trade.symbol}` : "New journal entry";
  if (decision) {
    for (const name of ["thesis", "direction", "entry", "invalidation", "target", "max_risk_usd", "confidence", "notes"]) {
      if (form.elements[name]) form.elements[name].value = decision[name] ?? "";
    }
  } else if (trade) {
    form.elements.direction.value = String(trade.side || "neutral").toLowerCase();
    form.elements.entry.value = trade.entry ?? "";
    form.elements.thesis.value = `${String(trade.side || "").toUpperCase()} ${trade.symbol} — `;
  }
  renderReasons(decision?.reason_ids || []);
  $("#entryDialog").showModal();
}

async function saveEntry(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    thesis: form.elements.thesis.value.trim(),
    direction: form.elements.direction.value,
    confidence: numeric(form.elements.confidence.value),
    entry: form.elements.entry.value || null,
    invalidation: form.elements.invalidation.value || null,
    target: form.elements.target.value || null,
    max_risk_usd: form.elements.max_risk_usd.value || null,
    notes: form.elements.notes.value.trim(),
    reason_ids: $$('input[name="reason_ids"]:checked').map((input) => Number(input.value)),
  };
  if (!state.editingDecision) payload.status = $("#tradeId").value ? "active" : "planned";
  try {
    let decision;
    if (state.editingDecision) {
      decision = await request(`/api/decisions/${state.editingDecision.id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
    } else {
      decision = await request("/api/decisions", { method: "POST", body: JSON.stringify(payload) });
      if ($("#tradeId").value) {
        const key = $("#tradeKind").value === "lifecycle" ? "lifecycle_id" : "trade_id";
        await request(`/api/decisions/${decision.id}/trades`, {
          method: "POST",
          body: JSON.stringify({ [key]: Number($("#tradeId").value) }),
        });
      }
    }
    $("#entryDialog").close();
    toast(state.editingDecision ? "Journal entry updated" : "Journal entry saved");
    await load();
  } catch (error) {
    toast(error.message, true);
  }
}

async function addReason() {
  const category = $("#customReasonCategory").value;
  const label = $("#customReasonLabel").value.trim();
  if (!label) return toast("Enter a reason first", true);
  try {
    const created = await request("/api/reasons", {
      method: "POST",
      body: JSON.stringify({ category, label }),
    });
    state.reasons = await request("/api/reasons");
    const selected = $$('input[name="reason_ids"]:checked').map((input) => Number(input.value));
    selected.push(created.id);
    renderReasons(selected);
    $("#customReasonLabel").value = "";
    toast("Reusable reason added");
  } catch (error) {
    toast(error.message, true);
  }
}

function v2Selected() {
  return state.v2.lifecycles.find((item) => String(item.id) === String(state.v2SelectedId))
    || state.v2.lifecycles[0]
    || null;
}

function v2Price(value) {
  const amount = numeric(value);
  if (Math.abs(amount) >= 1000) return amount.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (Math.abs(amount) >= 1) return amount.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return amount.toLocaleString(undefined, { maximumFractionDigits: 8 });
}

function v2ChartSvg(chart) {
  if (!chart) return `<div class="v2-chart-empty"><b>No V2 executions available</b><span>Sync the journal to build the immutable read model.</span></div>`;
  const width = 920;
  const height = 420;
  const left = 24;
  const right = 860;
  const top = 26;
  const bottom = 358;
  const candles = Array.isArray(chart.candles) ? chart.candles : [];
  const markers = Array.isArray(chart.markers) ? chart.markers : [];
  const values = [numeric(chart.entry_vwap), numeric(chart.exit_vwap), ...candles.flatMap((item) => [numeric(item.high), numeric(item.low)]), ...markers.map((item) => numeric(item.price_vwap))].filter(Number.isFinite);
  if (!values.length) return `<div class="v2-chart-empty"><b>Chart data is incomplete</b><span>The V2 contract is present, but no valid prices were returned.</span></div>`;
  let min = Math.min(...values);
  let max = Math.max(...values);
  const pad = Math.max((max - min) * 0.12, Math.abs(max || 1) * 0.002);
  min -= pad;
  max += pad;
  const points = [...candles.map((item) => new Date(item.opened_at).valueOf()), ...markers.map((item) => new Date(item.first_at).valueOf())].filter(Number.isFinite);
  const start = Math.min(...points);
  const end = Math.max(...points, start + 60_000);
  const x = (value) => left + ((new Date(value).valueOf() - start) / Math.max(1, end - start)) * (right - left);
  const y = (value) => bottom - ((numeric(value) - min) / Math.max(0.00000001, max - min)) * (bottom - top);
  const line = (x1, y1, x2, y2, attrs = "") => `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" ${attrs}/>`;
  const grid = Array.from({ length: 5 }, (_, index) => {
    const gy = top + (index / 4) * (bottom - top);
    const value = max - ((max - min) * index / 4);
    return `${line(left, gy, right, gy, 'class="v2-grid-line"')}<text x="${right + 12}" y="${gy + 4}" class="v2-axis-label">${escapeHtml(v2Price(value))}</text>`;
  }).join("");
  const candleWidth = Math.max(5, Math.min(20, (right - left) / Math.max(1, candles.length) * 0.55));
  const candleSvg = candles.map((item) => {
    const cx = x(item.opened_at);
    const open = y(item.open);
    const close = y(item.close);
    const high = y(item.high);
    const low = y(item.low);
    const color = numeric(item.close) >= numeric(item.open) ? "#61e6a8" : "#ff7187";
    return `${line(cx, high, cx, low, `class="v2-candle-wick" stroke="${color}"`)}<rect x="${cx - candleWidth / 2}" y="${Math.min(open, close)}" width="${candleWidth}" height="${Math.max(2, Math.abs(close - open))}" fill="${color}" rx="1"/>`;
  }).join("");
  const markerPoints = markers.map((marker) => `${x(marker.first_at)},${y(marker.price_vwap)}`).join(" ");
  const executionPath = markerPoints ? `<polyline points="${markerPoints}" class="v2-execution-path"/>` : "";
  const markerSvg = markers.map((marker) => {
    const cx = x(marker.first_at);
    const cy = y(marker.price_vwap);
    const buy = marker.side === "BUY";
    const color = buy ? "#61e6a8" : "#ff7187";
    const triangle = buy ? `${cx},${cy - 9} ${cx - 6},${cy + 3} ${cx + 6},${cy + 3}` : `${cx},${cy + 9} ${cx - 6},${cy - 3} ${cx + 6},${cy - 3}`;
    const label = String(marker.action || "FILL").replaceAll("_", " ").replace("LONG", "").replace("SHORT", "").trim();
    return `<polygon points="${triangle}" fill="${color}"/><text x="${cx}" y="${buy ? cy + 22 : cy - 14}" text-anchor="middle" class="v2-marker-label" fill="${color}">${escapeHtml(label)}${marker.raw_fill_count > 1 ? ` ×${marker.raw_fill_count}` : ""}</text>`;
  }).join("");
  const overlay = (value, label, className) => value == null ? "" : `${line(left, y(value), right, y(value), `class="${className}"`)}<text x="${left + 8}" y="${y(value) - 7}" class="v2-overlay-label">${label} ${escapeHtml(v2Price(value))}</text>`;
  return `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="V2 execution chart"><rect width="${width}" height="${height}" class="v2-chart-bg"/>${grid}${overlay(chart.entry_vwap, "ENTRY", "v2-entry-line")}${overlay(chart.exit_vwap, "EXIT", "v2-exit-line")}${candleSvg}${executionPath}${markerSvg}<text x="${left}" y="${height - 24}" class="v2-chart-foot">${escapeHtml(chart.candle_provenance)} · ${escapeHtml(chart.completeness)}</text></svg>`;
}

function renderV2() {
  const selected = v2Selected();
  const lifecycles = state.v2.lifecycles || [];
  $("#v2LifecycleCount").textContent = lifecycles.length;
  $("#v2LifecycleList").innerHTML = lifecycles.length ? lifecycles.map((item) => {
    const active = String(item.id) === String(selected?.id);
    const side = String(item.side || "").toLowerCase();
    return `<button type="button" class="v2-lifecycle-row ${active ? "active" : ""} ${escapeHtml(side)}" data-v2-lifecycle="${escapeHtml(item.id)}"><span class="v2-row-symbol">${escapeHtml(item.symbol || "Unknown")}</span><span class="v2-row-meta">${escapeHtml(String(item.side || "—").toUpperCase())} · ${escapeHtml(item.status || "OPEN")}</span><strong class="${pnlClass(item.pnl)}">${item.pnl == null ? "—" : formatMoney(item.pnl)}</strong></button>`;
  }).join("") : `<div class="v2-empty-list">No lifecycle rows yet.<br>Run an explicit sync.</div>`;
  $$('[data-v2-lifecycle]').forEach((button) => button.addEventListener("click", () => {
    state.v2SelectedId = Number(button.dataset.v2Lifecycle);
    renderV2();
  }));
  const chart = selected?.chart;
  $("#v2Venue").textContent = selected?.source || "—";
  $("#v2Symbol").textContent = selected?.symbol || "Select a lifecycle";
  $("#v2Direction").textContent = selected?.side ? String(selected.side).toUpperCase() : "—";
  $("#v2Direction").className = `v2-direction ${String(selected?.side || "").toLowerCase()}`;
  $("#v2ChartMeta").textContent = chart ? `${chart.markers.length} grouped events · ${chart.completeness} · interval ${Math.max(1, Number(chart.interval_seconds || 60) / 60)}m` : "Immutable V2 chart spec unavailable for this lifecycle.";
  $("#v2Chart").innerHTML = v2ChartSvg(chart);
  $("#v2StateBadge").textContent = selected?.status ? String(selected.status).toUpperCase() : "—";
  $("#v2Provenance").textContent = chart?.candle_provenance || "—";
  $("#v2Metrics").innerHTML = selected ? [["Entry", selected.entry == null ? "—" : v2Price(selected.entry)], ["Exit", selected.exit == null ? "—" : v2Price(selected.exit)], ["Position value", selected.position_value == null ? "—" : formatMoney(selected.position_value)], ["Fills", selected.fill_count || selected.execution_count || 0], ["PnL", selected.pnl == null ? "—" : formatMoney(selected.pnl)], ["Return", selected.pnl_pct == null ? "—" : `${numeric(selected.pnl_pct).toFixed(2)}%`]].map(([label, value]) => `<div><span>${label}</span><b>${escapeHtml(value)}</b></div>`).join("") : "";
  $("#v2ExecutionStrip").innerHTML = chart?.markers?.length ? chart.markers.map((marker, index) => `<div class="v2-execution-step"><span>${String(index + 1).padStart(2, "0")}</span><b>${escapeHtml(executionLabel(marker, selected))}</b><strong>${escapeHtml(v2Price(marker.price_vwap))}</strong><small>${escapeHtml(new Date(marker.first_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))}</small></div>`).join("") : `<div class="v2-execution-empty">No execution batches were serialized for this lifecycle.</div>`;
}

function showV2(enabled) {
  const workspace = $("#v2Workspace");
  workspace.hidden = !enabled;
  $("#summaryGrid").hidden = enabled;
  $(".workspace-tabs").hidden = enabled;
  $$(".workspace").forEach((item) => { item.hidden = enabled; });
  $("#v2ModeButton").hidden = !state.v2.enabled && !enabled;
  $("#v2ModeButton").textContent = enabled ? "Legacy view" : "V2 review";
  if (enabled) renderV2();
}

async function loadV2() {
  try {
    const payload = await request("/api/v2/bootstrap");
    state.v2 = payload;
    const requested = new URLSearchParams(window.location.search).get("ui") === "v2";
    $("#v2ModeButton").hidden = !payload.enabled && !requested;
    if (payload.enabled || requested) showV2(true);
  } catch (_error) {
    // The legacy Journal must remain usable if the optional V2 read model is unavailable.
    $("#v2ModeButton").hidden = true;
  }
}

async function load() {
  try {
    const payload = await request("/api/bootstrap");
    Object.assign(state, payload);
    $("#stateDot").classList.remove("error");
    $("#stateDot").classList.add("ok");
    $("#stateLabel").textContent = "VM journal state";
    const last = state.summary.last_sync;
    $("#lastSync").textContent = last?.finished_at
      ? `Updated ${relativeTime(last.finished_at)} · page load is read-only`
      : "No completed sync · page load is read-only";
    renderSummary();
    renderTrades();
    renderDecisions();
    await loadV2();
    const decisionId = Number(new URLSearchParams(window.location.search).get("decision_id") || 0);
    if (decisionId) {
      const decision = state.decisions.find((item) => item.id === decisionId);
      if (decision) openEntry(decision);
    }
  } catch (error) {
    $("#stateDot").classList.remove("ok");
    $("#stateDot").classList.add("error");
    $("#stateLabel").textContent = "Journal unavailable";
    $("#lastSync").textContent = error.message;
    toast(error.message, true);
  }
}

async function syncNow() {
  const button = $("#syncButton");
  button.disabled = true;
  button.textContent = "Syncing…";
  try {
    await request("/api/sync", { method: "POST", body: "{}" });
    await load();
    toast("Trading state synchronized");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Sync now";
  }
}

$$(".tab").forEach((tab) => tab.addEventListener("click", () => {
  $$(".tab").forEach((item) => {
    const active = item === tab;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  });
  $$(".workspace").forEach((workspace) => {
    const active = workspace.id === `${tab.dataset.tab}Workspace`;
    workspace.classList.toggle("active", active);
    workspace.hidden = !active;
  });
}));

for (const selector of ["#tradeSearch", "#tradeStatus", "#tradeSide", "#tradeJournalState"]) {
  $(selector).addEventListener(selector === "#tradeSearch" ? "input" : "change", renderTrades);
}
for (const selector of ["#entrySearch", "#entryStatus", "#entrySide"]) {
  $(selector).addEventListener(selector === "#entrySearch" ? "input" : "change", renderDecisions);
}
$$("th[data-sort]").forEach((header) => {
  const sort = () => {
    const key = header.dataset.sort;
    state.sort.direction = state.sort.key === key && state.sort.direction === "desc" ? "asc" : "desc";
    state.sort.key = key;
    $$("th[data-sort]").forEach((item) => {
      const active = item === header;
      item.setAttribute("aria-sort", active ? (state.sort.direction === "asc" ? "ascending" : "descending") : "none");
      $("i", item).textContent = active ? (state.sort.direction === "asc" ? "▲" : "▼") : "↕";
    });
    renderDecisions();
  };
  header.addEventListener("click", sort);
  header.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      sort();
    }
  });
});

$("#newEntry").addEventListener("click", () => openEntry());
$("#syncButton").addEventListener("click", syncNow);
$("#v2ModeButton").addEventListener("click", () => {
  const enabled = $("#v2Workspace").hidden;
  history.replaceState(null, "", enabled ? "?ui=v2" : "?ui=legacy");
  showV2(enabled);
});
$("#v2LegacyButton").addEventListener("click", () => {
  history.replaceState(null, "", "?ui=legacy");
  showV2(false);
});
$("#entryForm").addEventListener("submit", saveEntry);
$("#addReason").addEventListener("click", addReason);
$$("[data-close]").forEach((button) => button.addEventListener("click", () => $("#entryDialog").close()));
$("#entryDialog").addEventListener("click", (event) => {
  if (event.target === $("#entryDialog")) $("#entryDialog").close();
});
$("#closeInspect").addEventListener("click", () => $("#tradeInspectDialog").close());
$("#closeInspectFooter").addEventListener("click", () => $("#tradeInspectDialog").close());

load();
