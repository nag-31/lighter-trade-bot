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

function batchRows(trade) {
  const batches = Array.isArray(trade.batches) ? trade.batches : [];
  if (batches.length) {
    return batches.slice(0, 7).map((batch, index) => {
      const action = String(batch.action || batch.kind || batch.type || `Batch ${index + 1}`);
      const time = batch.occurred_at || batch.time || batch.started_at;
      const price = batch.price ?? batch.vwap;
      const size = batch.size ?? batch.quantity;
      return `<li><span>${escapeHtml(action)}</span><b>${price != null ? formatMoney(price) : ""}</b><small>${size != null ? number.format(numeric(size)) : ""} ${escapeHtml(time ? relativeTime(time) : "")}</small></li>`;
    }).join("");
  }
  const executions = Array.isArray(trade.executions) ? trade.executions : [];
  return executions.slice(0, 7).map((execution, index) => `
    <li>
      <span>${escapeHtml(execution.action || execution.kind || `Fill ${index + 1}`)}</span>
      <b>${execution.price != null ? formatMoney(execution.price) : ""}</b>
      <small>${execution.size != null ? number.format(numeric(execution.size)) : ""} ${escapeHtml(relativeTime(execution.occurred_at))}</small>
    </li>
  `).join("");
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
          <span><small>${open ? "MARK / LIVE" : "AVG EXIT"}</small><b>${!open && trade.exit != null ? formatMoney(trade.exit) : "—"}</b></span>
          <span><small>MAX SIZE</small><b>${number.format(numeric(trade.size))}</b></span>
          <span><small>${open ? "LIVE PNL" : "REALIZED PNL"}</small><b class="${pnlClass(open ? pnl.live : pnl.realized)}">${open && pnl.live == null ? "Awaiting mark" : formatMoney(open ? pnl.live : pnl.realized)}</b></span>
        </div>
        <div class="trade-meta">
          <span>${trade.fill_count || trade.executions?.length || 0} raw fills</span>
          <span>${trade.batch_count || trade.batches?.length || 0} execution batches</span>
          ${returnPct != null ? `<span class="${pnlClass(returnPct)}">${numeric(returnPct) >= 0 ? "+" : ""}${numeric(returnPct).toFixed(2)}%</span>` : ""}
        </div>
        ${batches ? `<details><summary>Execution tape <span>${trade.batches?.length || trade.executions?.length || 0} steps</span></summary><ol class="tape-list">${batches}</ol></details>` : ""}
        <footer>
          ${trade.decision_id
            ? `<button class="button quiet" data-edit="${trade.decision_id}">Edit reasons &amp; notes</button>`
            : `<button class="button primary" data-journal="${trade.id}">Journal this lifecycle</button>`}
        </footer>
      </article>
    `;
  }).join("");
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
  $$(".workspace").forEach((workspace) => workspace.classList.remove("active"));
  $(`#${tab.dataset.tab}Workspace`).classList.add("active");
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
$("#entryForm").addEventListener("submit", saveEntry);
$("#addReason").addEventListener("click", addReason);
$$("[data-close]").forEach((button) => button.addEventListener("click", () => $("#entryDialog").close()));
$("#entryDialog").addEventListener("click", (event) => {
  if (event.target === $("#entryDialog")) $("#entryDialog").close();
});

load();
