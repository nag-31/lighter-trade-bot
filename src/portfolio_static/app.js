/* ============================================================
   Portfolio dashboard — app.js (vanilla, no build step)
   Sections:
     1. Constants & config (explorer map, venue colors)
     2. Pure helpers (format, escape, dom, storage)
     3. State
     4. Data layer (fetch summary / history / refresh job)
     5. Derivations (holdings merge, allocation, filters)
     6. Renderers (kpi, chart, donut, filters, addresses, detail, holdings)
     7. Toasts / overlays / settings / shortcuts
     8. Event delegation & init
   All untrusted text is escaped via esc()/attr(). No inline handlers.
   ============================================================ */
(function () {
  "use strict";

  /* ===================== 1. CONSTANTS ===================== */

  // Chain key -> explorer base URL (keys mirror EVM CHAINS in portfolio_fetcher.py).
  var EXPLORERS = {
    ethereum: "https://etherscan.io",
    base: "https://basescan.org",
    arbitrum: "https://arbiscan.io",
    optimism: "https://optimistic.etherscan.io",
    polygon: "https://polygonscan.com",
    bnb: "https://bscscan.com",
    avalanche: "https://snowtrace.io",
    gnosis: "https://gnosisscan.io",
    celo: "https://celoscan.io",
    linea: "https://lineascan.build",
    scroll: "https://scrollscan.com",
    zksync: "https://explorer.zksync.io",
    mantle: "https://mantlescan.xyz",
    blast: "https://blastscan.io",
    fantom: "https://ftmscan.com",
    cronos: "https://cronoscan.com",
    moonbeam: "https://moonbeam.moonscan.io",
    metis: "https://explorer.metis.io",
    opbnb: "https://opbnb.bscscan.com",
    kava: "https://kavascan.com",
    sonic: "https://sonicscan.org",
    hyperevm: "https://hyperevmscan.io",
    unichain: "https://uniscan.xyz",
    xlayer: "https://www.oklink.com/x-layer",
    robinhood: "https://robinhoodchain.blockscout.com"
  };

  var VENUE_COLORS = {
    "EVM Chains": "var(--venue-evm)",
    DeFi: "var(--venue-defi)",
    Lighter: "var(--venue-lighter)",
    Hyperliquid: "var(--venue-hl)"
  };
  // Palette for donut segments (chain/token groupings).
  var DONUT_PALETTE = [
    "#7c5cff", "#38bdf8", "#22c55e", "#f5a623", "#ff5fa2",
    "#00d4a0", "#627eea", "#f5455c", "#a78bfa", "#facc15", "#4ade80", "#fb7185"
  ];

  var LS = "pf:"; // localStorage namespace
  var TOTAL_KEYS = [
    "total_usd", "chains_usd", "lighter_usd", "hyperliquid_usd",
    "defi_usd", "defi_gross_assets_usd", "defi_supplied_usd",
    "defi_collateral_usd", "defi_borrowed_usd", "lit_staked",
    "lit_staked_usd", "lit_spot", "lit_spot_usd", "lit_locked",
    "lit_locked_usd", "lit_total", "lit_total_usd"
  ];

  /* ===================== 2. PURE HELPERS ===================== */

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var MINUS = "−"; // U+2212

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // Attribute-safe (same as esc, kept separate for intent clarity).
  var attr = esc;

  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }

  // Money formatting per brief.
  function fmtUsd(v, opts) {
    opts = opts || {};
    var n = num(v);
    var neg = n < 0;
    var a = Math.abs(n);
    var s;
    if (!opts.noAbbrev && a >= 100000) {
      if (a >= 1e12) s = (a / 1e12).toFixed(2) + "T";
      else if (a >= 1e9) s = (a / 1e9).toFixed(2) + "B";
      else if (a >= 1e6) s = (a / 1e6).toFixed(2) + "M";
      else s = (a / 1e3).toFixed(1) + "K";
      s = "$" + s;
    } else {
      s = "$" + a.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return (neg ? MINUS : "") + s;
  }

  // Signed money with explicit + / U+2212.
  function fmtUsdSigned(v) {
    var n = num(v);
    var body = fmtUsd(Math.abs(n));
    if (n > 0) return "+" + body;
    if (n < 0) return MINUS + body;
    return body;
  }

  // Percent, 1 decimal, signed with U+2212.
  function fmtPct(v) {
    var n = num(v);
    var sign = n > 0 ? "+" : n < 0 ? MINUS : "";
    return sign + Math.abs(n).toFixed(1) + "%";
  }

  // Token quantity: <=6 decimals trimmed; <0.01 -> "<0.01".
  function fmtQty(v) {
    var n = num(v);
    if (n === 0) return "0";
    var a = Math.abs(n);
    if (a < 0.01) return (n < 0 ? MINUS : "") + "<0.01";
    var s = a.toLocaleString("en-US", { maximumFractionDigits: 6 });
    return (n < 0 ? MINUS : "") + s;
  }

  function fullNum(v) {
    var n = num(v);
    return n.toLocaleString("en-US", { maximumFractionDigits: 18 });
  }

  function fmtUnlockTime(ts) {
    var n = num(ts);
    if (!n) return "—";
    // Accept seconds or milliseconds epochs.
    var ms = n > 1e12 ? n : n * 1000;
    var d = new Date(ms);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  }

  function maskAddr(a) {
    if (!a) return "";
    if (a.length <= 12) return a;
    return a.slice(0, 6) + "…" + a.slice(-4);
  }

  function relTime(iso) {
    if (!iso) return "never";
    var t = Date.parse(iso);
    if (isNaN(t)) return "never";
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 45) return "just now";
    if (s < 90) return "1m ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 5400) return "1h ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  // Deterministic color-hash from address -> hsl gradient avatar.
  function avatarGradient(addr) {
    var h = 0;
    for (var i = 0; i < (addr || "").length; i++) h = (h * 31 + addr.charCodeAt(i)) >>> 0;
    var h1 = h % 360, h2 = (h1 + 60) % 360;
    return "linear-gradient(135deg, hsl(" + h1 + " 70% 55%), hsl(" + h2 + " 70% 45%))";
  }

  function deltaClass(n) { return n > 0 ? "pos" : n < 0 ? "neg" : "muted"; }

  // Colored ±$ / ±% delta span (shared by hero + per-card/row); d = {abs, pct, has}.
  function deltaSpan(d) {
    if (!d || !d.has) return '<span class="delta muted"><span class="num">' + MINUS + '</span></span>';
    return '<span class="delta ' + deltaClass(d.abs) + '"><span class="num sensitive">' + fmtUsdSigned(d.abs) + '</span>' +
      '<span class="delta-pct num">' + fmtPct(d.pct) + '</span></span>';
  }

  function store(key, val) { try { localStorage.setItem(LS + key, JSON.stringify(val)); } catch (e) {} }
  function load(key, def) {
    try { var v = localStorage.getItem(LS + key); return v === null ? def : JSON.parse(v); }
    catch (e) { return def; }
  }

  function el(html) { var t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }

  function download(name, mime, text) {
    var blob = new Blob([text], { type: mime });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click();
    a.remove(); setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function csvCell(v) {
    var s = v === null || v === undefined ? "" : String(v);
    if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  /* ===================== 3. STATE ===================== */

  var state = {
    storageMode: "local",
    runtimeConfig: {},
    summary: null,
    history: [],        // [{ts, total_usd}]
    prefs: {
      theme: load("theme", null),
      view: load("view", "cards"),
      dustOn: load("dustOn", false),
      dust: load("dust", 1.0),
      hideEmpty: load("hideEmpty", false),
      privacy: load("privacy", false),
      range: load("range", "30D"),
      holdingsOpen: load("holdingsOpen", false),
      allocGroup: load("allocGroup", "venue"),
      sort: load("sort", "value-desc"),
      status: load("status", "all"),
      excludedOpen: load("excludedOpen", false),
      scopeIds: load("scopeIds", null)
    },
    filters: { search: "", chains: new Set(), venues: new Set() },
    tableSort: { key: "value", dir: "desc" },
    openDetails: new Set(),    // address ids with open accordion
    activeTab: {},             // id -> tab name
    addrHistory: {},           // id -> [{ts,total_usd,...}] ascending (session cache)
    addrHistoryPending: {},    // id -> true while fetching
    countUpDone: false,
    refreshTimer: null,
    uplot: null
  };

  /* ===================== 4. DATA LAYER ===================== */

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        return { status: r.status, ok: r.ok, body: body };
      });
    });
  }

  function guestStore() {
    if (!window.PortfolioGuestStore) throw new Error("Guest storage module is unavailable");
    return window.PortfolioGuestStore;
  }

  function loadRuntimeConfig() {
    return api("/api/config").then(function (res) {
      if (!res.ok || !res.body || res.body.ok === false) throw new Error("config failed");
      state.runtimeConfig = res.body;
      var configuredMode = String(res.body.storage_mode || "local");
      state.storageMode = configuredMode === "guest" || configuredMode === "private"
        ? configuredMode : "local";
      document.body.setAttribute("data-storage-mode", state.storageMode);
      var backup = $("#guest-backup-setting");
      if (backup) backup.hidden = state.storageMode !== "guest";
      var logout = $("#menu-logout");
      if (logout) logout.hidden = state.storageMode !== "private";
      var note = $("#storage-note");
      if (note) {
        note.textContent = state.storageMode === "guest"
          ? "Wallets, labels, settings, and history stay in this browser. Refresh requests are processed without server-side storage."
          : state.storageMode === "private"
            ? "Wallets and history are stored in your password-protected private database."
            : "Wallets and history are stored by your local portfolio server.";
      }
      return res.body;
    }).catch(function () {
      state.storageMode = "local";
      state.runtimeConfig = { storage_mode: "local" };
      return state.runtimeConfig;
    });
  }

  function emptyTotals() {
    var totals = {};
    TOTAL_KEYS.forEach(function (key) { totals[key] = 0; });
    return totals;
  }

  function buildGuestSummary() {
    return Promise.all([guestStore().listWallets(), guestStore().latestSnapshots()]).then(function (parts) {
      var wallets = parts[0], snapshots = parts[1];
      var totals = emptyTotals(), included = emptyTotals();
      var rows = [], statuses = [], lastRefresh = null, tokenCatalog = {};
      wallets.forEach(function (wallet) {
        var id = Number(wallet.id);
        var attempt = snapshots.latest[id] || null;
        var lastGood = snapshots.successful[id] || null;
        var valuation = attempt && attempt.status === "error" && lastGood ? lastGood : attempt;
        var payload = valuation && valuation.payload || null;
        var stale = !!(attempt && attempt.status === "error" && lastGood);
        var latest = {
          status: attempt ? attempt.status : "idle",
          ts: attempt ? attempt.ts : null,
          total_usd: valuation ? valuation.total_usd : null,
          attempted_total_usd: attempt ? attempt.total_usd : null,
          error: attempt ? attempt.error : null,
          stale: stale,
          last_good_ts: lastGood ? lastGood.ts : null
        };
        if (payload) {
          TOTAL_KEYS.forEach(function (key) {
            var value = num((payload.totals || {})[key]);
            totals[key] += value;
            if (!wallet.excluded) included[key] += value;
          });
          statuses.push(latest.status || payload.status || "idle");
          if (latest.ts && (!lastRefresh || latest.ts > lastRefresh)) lastRefresh = latest.ts;
          tokenCatalog = payload.token_catalog || tokenCatalog;
        } else statuses.push("idle");
        rows.push({
          id: id,
          address: wallet.address,
          address_masked: maskAddr(wallet.address),
          label: wallet.label || null,
          excluded: !!wallet.excluded,
          latest: latest,
          snapshot: payload
        });
      });
      var status = !rows.length ? "idle"
        : statuses.indexOf("error") >= 0 ? "error"
        : statuses.indexOf("degraded") >= 0 ? "degraded"
        : statuses.every(function (value) { return value === "idle"; }) ? "idle" : "ok";
      return {
        ok: true, status: status, addresses: rows, totals: totals,
        totals_included: included, last_refresh: lastRefresh, token_catalog: tokenCatalog
      };
    });
  }

  function loadSummary() {
    var task = state.storageMode === "guest"
      ? buildGuestSummary()
      : api("/api/summary").then(function (res) {
          if (!res.ok || !res.body || res.body.ok === false) throw new Error("summary failed");
          return res.body;
        });
    return task.then(function (summary) {
      state.summary = summary;
      normalizeScope();
      return summary;
    });
  }

  function loadHistory() {
    var ids = selectedScopeIds();
    var task = state.storageMode === "guest"
      ? guestStore().aggregateHistory(ids, 1000)
      : api("/api/history?limit=1000&address_ids=" + encodeURIComponent(ids.join(","))).then(function (res) {
          return (res.body && res.body.history) || [];
        });
    return task.then(function (history) {
      state.history = history || [];
      return state.history;
    }).catch(function () { state.history = []; });
  }

  function loadAddrHistory(id) {
    if (id == null || state.addrHistory[id] || state.addrHistoryPending[id]) return;
    state.addrHistoryPending[id] = true;
    var task = state.storageMode === "guest"
      ? guestStore().addressHistory(id, 300)
      : api("/api/addresses/" + id + "/history?limit=300").then(function (res) {
          return (res.body && res.body.history) || [];
        });
    task.then(function (history) {
      state.addrHistoryPending[id] = false;
      state.addrHistory[id] = history || [];
      renderAddresses();
    }).catch(function () {
      state.addrHistoryPending[id] = false;
      state.addrHistory[id] = [];
    });
  }
  // Prefetch per-address history for all known addresses so per-card/row deltas
  // populate without needing an expand. Cheap (last-N points, no payloads) and cached.
  function prefetchAddrHistories() {
    var addrs = (state.summary && state.summary.addresses) || [];
    addrs.forEach(function (a) { loadAddrHistory(a.id); });
  }

  // Drop cached history for an address so the next expand refetches (after a refresh).
  function invalidateAddrHistory(id) {
    if (id == null) return;
    delete state.addrHistory[id];
    delete state.addrHistoryPending[id];
  }

  /* ===================== 5. DERIVATIONS ===================== */

  function activeScopeAddresses() {
    var addrs = (state.summary && state.summary.addresses) || [];
    return addrs.filter(function (a) { return !a.excluded; });
  }

  function normalizeScope() {
    var valid = activeScopeAddresses().map(function (a) { return Number(a.id); });
    if (!Array.isArray(state.prefs.scopeIds)) {
      state.prefs.scopeIds = valid;
    } else {
      state.prefs.scopeIds = state.prefs.scopeIds.map(Number).filter(function (id) { return valid.indexOf(id) >= 0; });
    }
    store("scopeIds", state.prefs.scopeIds);
  }

  function selectedScopeIds() {
    normalizeScope();
    return state.prefs.scopeIds || [];
  }

  function includedAddresses() {
    var ids = selectedScopeIds();
    return activeScopeAddresses().filter(function (a) { return ids.indexOf(Number(a.id)) >= 0; });
  }

  function selectedTotals() {
    var totals = {};
    includedAddresses().forEach(function (a) {
      var source = (a.snapshot && a.snapshot.totals) || {};
      Object.keys(source).forEach(function (key) { totals[key] = num(totals[key]) + num(source[key]); });
    });
    return totals;
  }

  function addrTotal(a) {
    var snap = a.snapshot;
    if (snap && snap.totals && snap.totals.total_usd != null) return num(snap.totals.total_usd);
    if (a.latest && a.latest.total_usd != null) return num(a.latest.total_usd);
    return 0;
  }

  function addrStatus(a) {
    if (a.snapshot && a.snapshot.status) return String(a.snapshot.status);
    return (a.latest && a.latest.status) || "idle";
  }

  // Which venues an address touches (for dots + filtering).
  function addrVenues(a) {
    var v = { evm: false, defi: false, lighter: false, hl: false };
    var s = a.snapshot;
    if (!s) return v;
    var t = s.totals || {};
    if (num(t.chains_usd) > 0 || (s.chains && s.chains.length)) v.evm = true;
    if (Math.abs(num(t.defi_usd)) > 0 || (s.defi && s.defi.positions && s.defi.positions.length)) v.defi = true;
    if (num(t.lighter_usd) > 0 || (s.lighter && s.lighter.ok)) v.lighter = true;
    if (num(t.hyperliquid_usd) > 0 || (s.hyperliquid && s.hyperliquid.ok)) v.hl = true;
    return v;
  }

  // Distinct chains with nonzero balance across all included addresses.
  function activeChains() {
    var map = {};
    includedAddresses().forEach(function (a) {
      var chains = (a.snapshot && a.snapshot.chains) || [];
      chains.forEach(function (c) {
        var val = num(c.total_usd);
        if (val > 0) {
          if (!map[c.key]) map[c.key] = { key: c.key, name: c.name || c.key, usd: 0 };
          map[c.key].usd += val;
        }
      });
    });
    return Object.keys(map).map(function (k) { return map[k]; })
      .sort(function (x, y) { return y.usd - x.usd; });
  }

  // Allocation buckets by grouping.
  function allocation(group) {
    var buckets = {};
    function add(name, usd, color) {
      usd = num(usd);
      if (usd <= 0) return;
      if (!buckets[name]) buckets[name] = { name: name, usd: 0, color: color };
      buckets[name].usd += usd;
    }
    includedAddresses().forEach(function (a) {
      var s = a.snapshot; if (!s) return;
      var t = s.totals || {};
      if (group === "venue") {
        add("EVM Chains", t.chains_usd, VENUE_COLORS["EVM Chains"]);
        add("DeFi", t.defi_usd, VENUE_COLORS.DeFi);
        add("Lighter", t.lighter_usd, VENUE_COLORS.Lighter);
        add("Hyperliquid", t.hyperliquid_usd, VENUE_COLORS.Hyperliquid);
      } else if (group === "chain") {
        (s.chains || []).forEach(function (c) { add(c.name || c.key, c.total_usd); });
        (((s.defi || {}).positions) || []).forEach(function (p) { add(p.chain_name || p.chain || "DeFi", p.total_usd); });
        add("Lighter", t.lighter_usd);
        add("Hyperliquid", t.hyperliquid_usd);
      } else { // token
        (s.chains || []).forEach(function (c) {
          if (c.native && num(c.native.value_usd) > 0) add(c.native.symbol || "native", c.native.value_usd);
          (c.tokens || []).forEach(function (tk) { add(tk.symbol || "?", tk.value_usd); });
        });
        var hl = s.hyperliquid || {};
        if (hl.spot && hl.spot.balances) hl.spot.balances.forEach(function (b) { add(b.coin || "?", b.value_usd); });
        add("DeFi net", t.defi_usd);
      }
    });
    var arr = Object.keys(buckets).map(function (k) { return buckets[k]; })
      .sort(function (x, y) { return y.usd - x.usd; });
    // top-6 + Other
    if (arr.length > 6) {
      var top = arr.slice(0, 6);
      var other = arr.slice(6).reduce(function (s2, b) { return s2 + b.usd; }, 0);
      top.push({ name: "Other", usd: other, color: "var(--text-disabled)" });
      arr = top;
    }
    var total = arr.reduce(function (s2, b) { return s2 + b.usd; }, 0);
    arr.forEach(function (b, i) {
      if (!b.color) b.color = DONUT_PALETTE[i % DONUT_PALETTE.length];
      b.pct = total > 0 ? (b.usd / total) * 100 : 0;
    });
    return { items: arr, total: total };
  }

  // 24h delta for hero from aggregate history.
  function heroDelta() {
    var h = state.history;
    if (!h || h.length < 2) return { abs: 0, pct: 0, has: false };
    var last = num(h[h.length - 1].total_usd);
    var cutoff = Date.parse(h[h.length - 1].ts) - 24 * 3600 * 1000;
    var base = null;
    for (var i = h.length - 1; i >= 0; i--) {
      if (Date.parse(h[i].ts) <= cutoff) { base = num(h[i].total_usd); break; }
    }
    if (base === null) base = num(h[0].total_usd);
    var abs = last - base;
    return { abs: abs, pct: base > 0 ? (abs / base) * 100 : 0, has: true };
  }

  // Per-address delta vs previous snapshot, using the last two cached history points.
  function addrDelta(a) {
    if (!a) return { abs: 0, pct: 0, has: false };
    var h = state.addrHistory[a && a.id];
    if (!h || h.length < 2) return { abs: 0, pct: 0, has: false };
    var last = num(h[h.length - 1].total_usd);
    var prev = num(h[h.length - 2].total_usd);
    var abs = last - prev;
    return { abs: abs, pct: prev !== 0 ? (abs / Math.abs(prev)) * 100 : 0, has: true };
  }
  // Numeric delta for sorting (0 when unknown).
  function addrDeltaAbs(a) { var d = addrDelta(a); return d.has ? d.abs : 0; }

  // Merge all token holdings by symbol for the global-holdings section.
  function mergedHoldings() {
    var map = {};
    function add(sym, chain, addrId, balance, price, value) {
      value = num(value);
      sym = sym || "?";
      if (!map[sym]) map[sym] = { symbol: sym, balance: 0, value: 0, price: num(price), addrs: {}, chains: {}, rows: [] };
      var m = map[sym];
      m.balance += num(balance); m.value += value;
      if (price) m.price = num(price);
      if (addrId != null) m.addrs[addrId] = true;
      if (chain) m.chains[chain] = true;
      m.rows.push({ chain: chain, addrId: addrId, balance: num(balance), value: value });
    }
    includedAddresses().forEach(function (a) {
      var s = a.snapshot; if (!s) return;
      (s.chains || []).forEach(function (c) {
        if (c.native && num(c.native.balance) > 0) add(c.native.symbol, c.name || c.key, a.id, c.native.balance, c.native.price_usd, c.native.value_usd);
        (c.tokens || []).forEach(function (tk) { add(tk.symbol, c.name || c.key, a.id, tk.balance, tk.price_usd, tk.value_usd); });
      });
      var hl = s.hyperliquid || {};
      if (hl.spot && hl.spot.balances) hl.spot.balances.forEach(function (b) { add(b.coin, "HL Spot", a.id, b.total, b.price_usd, b.value_usd); });
      var lt = s.lighter || {};
      (lt.accounts || []).forEach(function (ac) {
        (ac.assets || []).forEach(function (as) {
          if (String(as.symbol || "").toUpperCase() !== "LIT") return;
          // Full spot balance (locked is a slice of it). Staked LIT is the
          // public-pool share value which lives OUTSIDE the balance, so
          // adding both never overlaps.
          if (num(as.spot_balance) > 0) add("LIT", "Lighter spot", a.id, as.spot_balance, as.price_usd, as.spot_value_usd);
        });
      });
      if (lt.staking && num(lt.staking.staked_lit) > 0) add("LIT", "Lighter staked", a.id, lt.staking.staked_lit, lt.staking.lit_price_usd, lt.staking.staked_lit_value_usd);
    });
    return Object.keys(map).map(function (k) {
      var m = map[k];
      m.addrCount = Object.keys(m.addrs).length;
      m.chainCount = Object.keys(m.chains).length;
      return m;
    }).sort(function (x, y) { return y.value - x.value; });
  }

  // Apply filters/sort to addresses (excluding excluded, which render separately).
  function visibleAddresses() {
    var f = state.filters, p = state.prefs;
    var q = f.search.trim().toLowerCase();
    var dust = p.dustOn ? num(p.dust) : 0;
    var list = activeScopeAddresses().filter(function (a) {
      // search
      if (q) {
        var hay = ((a.label || "") + " " + (a.address || "")).toLowerCase();
        var tokMatch = false;
        var s = a.snapshot;
        if (s && s.chains) {
          for (var i = 0; i < s.chains.length && !tokMatch; i++) {
            var toks = s.chains[i].tokens || [];
            for (var j = 0; j < toks.length; j++) {
              if ((toks[j].symbol || "").toLowerCase().indexOf(q) >= 0) { tokMatch = true; break; }
            }
          }
        }
        if (hay.indexOf(q) < 0 && !tokMatch) return false;
      }
      // status filter
      if (p.status !== "all" && addrStatus(a) !== p.status) return false;
      // hide empty
      if (p.hideEmpty && addrTotal(a) <= 0) return false;
      // dust
      if (dust > 0 && addrTotal(a) < dust) return false;
      // venue chips
      if (f.venues.size) {
        var v = addrVenues(a);
        var ok = false;
        if (f.venues.has("defi") && v.defi) ok = true;
        if (f.venues.has("lighter") && v.lighter) ok = true;
        if (f.venues.has("hl") && v.hl) ok = true;
        if (!ok) return false;
      }
      // chain chips
      if (f.chains.size) {
        var chains = (a.snapshot && a.snapshot.chains) || [];
        var hasChain = chains.some(function (c) { return f.chains.has(c.key) && num(c.total_usd) > 0; });
        if (!hasChain) return false;
      }
      return true;
    });
    return sortAddresses(list);
  }

  function sortAddresses(list) {
    var s = state.prefs.sort;
    var copy = list.slice();
    copy.sort(function (a, b) {
      switch (s) {
        case "value-asc": return addrTotal(a) - addrTotal(b);
        case "label-asc": return (a.label || a.address || "").localeCompare(b.label || b.address || "");
        case "delta-desc": return addrDeltaAbs(b) - addrDeltaAbs(a);
        case "status": return addrStatus(a).localeCompare(addrStatus(b));
        default: return addrTotal(b) - addrTotal(a); // value-desc
      }
    });
    return copy;
  }

  function filtersActive() {
    var f = state.filters, p = state.prefs;
    return !!(f.search || f.chains.size || f.venues.size || p.dustOn || p.hideEmpty || p.status !== "all");
  }

  /* ===================== 6. RENDERERS ===================== */

  function renderAll() {
    safe(renderKpi, "kpi");
    safe(renderScope, "scope");
    safe(renderChart, "chart");
    safe(renderAllocation, "allocation");
    safe(renderFilters, "filters");
    safe(renderAddresses, "addresses");
    safe(renderHoldings, "holdings");
  }

  // Isolate section renders so one failure never blanks the whole dashboard.
  function safe(fn, label) {
    try { fn(); }
    catch (e) {
      if (window.console && console.error) console.error("render error [" + label + "]", e);
    }
  }

  /* ---- KPI hero ---- */
  function renderKpi() {
    var host = $("#kpi-row");
    var sum = state.summary;
    var totals = selectedTotals();
    var total = num(totals.total_usd);
    var incl = includedAddresses();
    var d = heroDelta();

    var statusCounts = { ok: 0, degraded: 0, error: 0, idle: 0 };
    incl.forEach(function (a) { var st = addrStatus(a); if (statusCounts[st] === undefined) statusCounts[st] = 0; statusCounts[st]++; });

    var spark = sparkline(state.history);
    var asOf = relTime(sum && sum.last_refresh);

    host.innerHTML =
      '<div class="hero-card">' +
        '<div class="kpi-label">Total value</div>' +
        '<div class="hero-total num sensitive" id="hero-total" data-target="' + total + '">' + fmtUsd(total) + '</div>' +
        '<div class="hero-meta">' +
          (d.has
            ? '<span class="delta ' + deltaClass(d.abs) + '"><span class="num sensitive">' + fmtUsdSigned(d.abs) + '</span>' +
              '<span class="delta-pct num">' + fmtPct(d.pct) + '</span></span>'
            : '<span class="muted">No 24h data yet</span>') +
          '<span class="hero-asof">Data as of ' + esc(asOf) + '</span>' +
        '</div>' + spark +
      '</div>' +
      statCard("24h change", d.has ? fmtUsdSigned(d.abs) : "—", d.has ? fmtPct(d.pct) : "Refresh twice for history", d.has ? deltaClass(d.abs) : "muted") +
      statCard("Active addresses", String(incl.length), (sum && sum.addresses ? sum.addresses.length : 0) + " total", "") +
      statCard("DeFi net", fmtUsd(totals.defi_usd), fmtUsd(totals.defi_gross_assets_usd) + " assets / " + fmtUsd(totals.defi_borrowed_usd) + " debt", "") +
      statCard("Lighter LIT", fmtUsd(totals.lit_total_usd), fmtQty(totals.lit_spot) + " spot / " + fmtQty(totals.lit_staked) + " staked", "") +
      '<div class="card stat-card">' +
        '<div class="kpi-label">Sources</div>' +
        '<div class="status-dots">' +
          statusDot("ok", statusCounts.ok) + statusDot("degraded", statusCounts.degraded) +
          statusDot("error", statusCounts.error) + statusDot("idle", statusCounts.idle) +
        '</div>' +
      '</div>';

    if (!state.countUpDone && total > 0) { countUp($("#hero-total"), total); state.countUpDone = true; }
  }

  function renderScope() {
    var host = $("#scope-host");
    var all = activeScopeAddresses();
    var selected = selectedScopeIds();
    if (!all.length) { host.innerHTML = ""; return; }
    host.innerHTML = '<div class="scope-copy"><span class="scope-title">Overview wallets</span><span class="scope-note">' +
      selected.length + ' of ' + all.length + ' selected</span></div>' +
      '<div class="scope-actions"><button class="btn btn-ghost btn-sm" data-action="scope-all">All</button><button class="btn btn-ghost btn-sm" data-action="scope-none">None</button></div>' +
      '<div class="scope-wallets">' + all.map(function (a) {
        var picked = selected.indexOf(Number(a.id)) >= 0;
        return '<button class="scope-wallet ' + (picked ? 'is-selected' : '') + '" data-action="scope-toggle" data-id="' + attr(a.id) + '" aria-pressed="' + picked + '">' +
          '<span class="scope-check">' + (picked ? '&#10003;' : '') + '</span>' + esc(a.label || maskAddr(a.address)) + '</button>';
      }).join('') + '</div>';
  }

  function statCard(label, value, sub, cls) {
    return '<div class="card stat-card"><div class="kpi-label">' + esc(label) + '</div>' +
      '<div class="kpi-value num sensitive ' + (cls || "") + '">' + esc(value) + '</div>' +
      '<div class="stat-sub">' + esc(sub) + '</div></div>';
  }
  function statusDot(kind, n) {
    return '<span class="status-dot"><span class="dot dot-' + kind + '"></span>' + n + ' ' + kind + '</span>';
  }

  function countUp(node, target) {
    var start = performance.now(), dur = 900;
    function step(now) {
      var t = Math.min(1, (now - start) / dur);
      var e = 1 - Math.pow(1 - t, 3);
      node.textContent = fmtUsd(target * e);
      if (t < 1) requestAnimationFrame(step);
      else node.textContent = fmtUsd(target);
    }
    requestAnimationFrame(step);
  }

  // Inline SVG sparkline of last ~30 aggregate points.
  function sparkline(hist) {
    if (!hist || hist.length < 2) return "";
    var pts = hist.slice(-30).map(function (p) { return num(p.total_usd); });
    var w = 260, h = 62, min = Math.min.apply(null, pts), max = Math.max.apply(null, pts);
    var range = max - min || 1;
    var step = w / (pts.length - 1);
    var d = pts.map(function (v, i) {
      var x = i * step, y = h - 4 - ((v - min) / range) * (h - 10);
      return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }).join(" ");
    var up = pts[pts.length - 1] >= pts[0];
    var col = up ? "var(--success)" : "var(--danger)";
    return '<svg class="hero-spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<defs><linearGradient id="spg" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="' + col + '" stop-opacity=".25"/><stop offset="1" stop-color="' + col + '" stop-opacity="0"/>' +
      '</linearGradient></defs>' +
      '<path d="' + d + ' L' + w + ' ' + h + ' L0 ' + h + ' Z" fill="url(#spg)"/>' +
      '<path d="' + d + '" fill="none" stroke="' + col + '" stroke-width="1.6"/></svg>';
  }

  /* ---- History chart (uPlot with SVG fallback) ---- */
  function rangeCutoff(range) {
    var map = { "1D": 1, "7D": 7, "30D": 30, "90D": 90 };
    if (range === "ALL" || !map[range]) return 0;
    return Date.now() - map[range] * 86400 * 1000;
  }

  function chartData() {
    var cutoff = rangeCutoff(state.prefs.range);
    var pts = state.history.filter(function (p) { return cutoff === 0 || Date.parse(p.ts) >= cutoff; });
    return pts;
  }

  function renderChart() {
    // active range pill
    Array.prototype.forEach.call(document.querySelectorAll("#range-pills .pill"), function (b) {
      b.classList.toggle("is-active", b.getAttribute("data-range") === state.prefs.range);
    });
    var body = $("#chart-body");
    var pts = chartData();
    if (state.uplot) { state.uplot.destroy(); state.uplot = null; }
    body.innerHTML = "";

    if (!pts || pts.length < 2) {
      body.innerHTML = '<div class="chart-empty"><div>No history yet</div>' +
        '<div class="muted">Refresh at least twice to see history</div></div>';
      return;
    }
    if (typeof window.uPlot === "function") {
      try { renderUplot(body, pts); }
      catch (e) { body.innerHTML = ""; renderSvgChart(body, pts); }
    } else { renderSvgChart(body, pts); }
  }

  function renderUplot(body, pts) {
    var xs = pts.map(function (p) { return Math.floor(Date.parse(p.ts) / 1000); });
    var ys = pts.map(function (p) { return num(p.total_usd); });
    var w = body.clientWidth || 800;
    var tt = el('<div class="u-tooltip" style="display:none"></div>');
    body.appendChild(tt);

    var opts = {
      width: w, height: 300,
      cursor: { y: false, points: { size: 6 } },
      scales: { x: { time: true } },
      axes: [
        { stroke: "var(--text-tertiary)", grid: { stroke: "var(--border-subtle)" }, ticks: { stroke: "var(--border-subtle)" }, font: "11px var(--font-ui)" },
        { stroke: "var(--text-tertiary)", grid: { stroke: "var(--border-subtle)" }, ticks: { stroke: "var(--border-subtle)" }, font: "11px var(--font-ui)",
          values: function (u, splits) { return splits.map(function (v) { return fmtUsd(v); }); } }
      ],
      series: [
        {},
        {
          stroke: "var(--accent)", width: 2, fill: fillGrad,
          points: { show: false }
        }
      ],
      hooks: {
        setCursor: [function (u) {
          var idx = u.cursor.idx;
          if (idx == null || idx < 0) { tt.style.display = "none"; return; }
          var x = u.valToPos(u.data[0][idx], "x");
          var y = u.valToPos(u.data[1][idx], "y");
          tt.style.display = "block";
          tt.style.left = x + "px"; tt.style.top = y + "px";
          var d = new Date(u.data[0][idx] * 1000);
          tt.innerHTML = '<div class="tt-val num">' + fmtUsd(u.data[1][idx]) + '</div>' +
            '<div class="tt-time">' + esc(d.toLocaleString()) + '</div>';
        }]
      }
    };
    state.uplot = new window.uPlot(opts, [xs, ys], body);
  }

  function fillGrad(u) {
    var ctx = u.ctx;
    var bb = u.bbox || {};
    var top = isFinite(bb.top) ? bb.top : 0;
    var height = isFinite(bb.height) && bb.height > 0 ? bb.height : (u.height || 300);
    var g = ctx.createLinearGradient(0, top, 0, top + height);
    g.addColorStop(0, "rgba(124,92,255,.35)");
    g.addColorStop(1, "rgba(124,92,255,0)");
    return g;
  }

  // Fallback hand-rolled SVG chart with hover tooltip.
  function renderSvgChart(body, pts) {
    var w = body.clientWidth || 800, h = 300, pad = 8;
    var ys = pts.map(function (p) { return num(p.total_usd); });
    var xs = pts.map(function (p) { return Date.parse(p.ts); });
    var min = Math.min.apply(null, ys), max = Math.max.apply(null, ys), range = max - min || 1;
    var x0 = xs[0], x1 = xs[xs.length - 1], xr = x1 - x0 || 1;
    function px(i) { return pad + ((xs[i] - x0) / xr) * (w - pad * 2); }
    function py(i) { return h - pad - ((ys[i] - min) / range) * (h - pad * 2 - 20); }
    var d = pts.map(function (p, i) { return (i ? "L" : "M") + px(i).toFixed(1) + " " + py(i).toFixed(1); }).join(" ");
    var svg = el('<svg class="svg-chart" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '">' +
      '<defs><linearGradient id="cg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--accent)" stop-opacity=".35"/><stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + d + ' L' + px(pts.length - 1) + ' ' + (h - pad) + ' L' + px(0) + ' ' + (h - pad) + ' Z" fill="url(#cg)"/>' +
      '<path d="' + d + '" fill="none" stroke="var(--accent)" stroke-width="2"/>' +
      '<line class="cursor-line" x1="0" y1="0" x2="0" y2="' + h + '" stroke="var(--border-strong)" stroke-width="1" style="display:none"/>' +
      '<circle class="cursor-dot" r="4" fill="var(--accent)" style="display:none"/></svg>');
    var tt = el('<div class="u-tooltip" style="display:none"></div>');
    body.appendChild(svg); body.appendChild(tt);
    var line = svg.querySelector(".cursor-line"), dot = svg.querySelector(".cursor-dot");
    svg.addEventListener("mousemove", function (ev) {
      var rect = svg.getBoundingClientRect();
      var mx = ev.clientX - rect.left;
      var best = 0, bd = Infinity;
      for (var i = 0; i < pts.length; i++) { var dd = Math.abs(px(i) - mx); if (dd < bd) { bd = dd; best = i; } }
      line.style.display = ""; line.setAttribute("x1", px(best)); line.setAttribute("x2", px(best));
      dot.style.display = ""; dot.setAttribute("cx", px(best)); dot.setAttribute("cy", py(best));
      tt.style.display = "block"; tt.style.left = px(best) + "px"; tt.style.top = py(best) + "px";
      tt.innerHTML = '<div class="tt-val num">' + fmtUsd(ys[best]) + '</div><div class="tt-time">' + esc(new Date(xs[best]).toLocaleString()) + '</div>';
    });
    svg.addEventListener("mouseleave", function () { line.style.display = "none"; dot.style.display = "none"; tt.style.display = "none"; });
  }

  /* ---- Allocation donut ---- */
  var allocAnim = { from: [], raf: null };
  function renderAllocation() {
    Array.prototype.forEach.call(document.querySelectorAll("#alloc-seg .seg-btn"), function (b) {
      b.classList.toggle("is-active", b.getAttribute("data-group") === state.prefs.allocGroup);
    });
    var data = allocation(state.prefs.allocGroup);
    drawDonut(data);
    drawLegend(data);
  }

  function drawDonut(data) {
    var wrap = $("#donut-wrap");
    var size = 170, r = 68, cx = size / 2, cy = size / 2, sw = 22;
    var items = data.items.length ? data.items : [{ name: "Empty", usd: 1, pct: 100, color: "var(--bg-4)" }];
    var svg = wrap.querySelector("svg");
    if (!svg) {
      wrap.innerHTML = '<svg viewBox="0 0 ' + size + ' ' + size + '"></svg>' +
        '<div class="donut-center"><div class="dc-val num">' + fmtUsd(data.total) + '</div><div class="dc-label">' + esc(cap(state.prefs.allocGroup)) + '</div></div>';
      svg = wrap.querySelector("svg");
    } else {
      wrap.querySelector(".dc-val").textContent = fmtUsd(data.total);
      wrap.querySelector(".dc-label").textContent = cap(state.prefs.allocGroup);
    }
    var circ = 2 * Math.PI * r;
    var targets = items.map(function (it) { return it.pct / 100; });
    var from = allocAnim.from.length === targets.length ? allocAnim.from : targets.map(function () { return 0; });
    if (allocAnim.raf) cancelAnimationFrame(allocAnim.raf);

    // Build the SVG for an arbitrary set of fractions.
    function paint(cur) {
      var offset = 0, html = '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="var(--bg-3)" stroke-width="' + sw + '"/>';
      cur.forEach(function (frac, i) {
        var len = Math.max(0, frac) * circ;
        html += '<circle class="donut-arc" data-i="' + i + '" cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" ' +
          'stroke="' + items[i].color + '" stroke-width="' + sw + '" stroke-linecap="butt" ' +
          'stroke-dasharray="' + len.toFixed(2) + ' ' + (circ - len).toFixed(2) + '" ' +
          'stroke-dashoffset="' + (-offset).toFixed(2) + '" transform="rotate(-90 ' + cx + ' ' + cy + ')" style="transition:opacity var(--d1)"/>';
        offset += len;
      });
      svg.innerHTML = html;
    }

    // Paint the final state synchronously first so the donut is never blank
    // (rAF may be throttled in background tabs / reduced-motion), then animate.
    paint(targets);
    allocAnim.from = targets;

    var start = performance.now(), dur = 250;
    function frame(now) {
      var t = Math.min(1, (now - start) / dur), e = 1 - Math.pow(1 - t, 3);
      paint(targets.map(function (tg, i) { return from[i] + (tg - from[i]) * e; }));
      if (t < 1) allocAnim.raf = requestAnimationFrame(frame);
    }
    allocAnim.raf = requestAnimationFrame(frame);
  }

  function drawLegend(data) {
    var host = $("#alloc-legend");
    if (!data.items.length) { host.innerHTML = '<li class="inline-empty">No balances</li>'; return; }
    host.innerHTML = data.items.map(function (it, i) {
      return '<li class="legend-row" data-i="' + i + '">' +
        '<span class="swatch" style="background:' + it.color + '"></span>' +
        '<span class="lg-name">' + esc(it.name) + '</span>' +
        '<span class="lg-val num sensitive">' + fmtUsd(it.usd) + '</span>' +
        '<span class="lg-pct num">' + it.pct.toFixed(1) + '%</span></li>';
    }).join("");
  }

  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ""; }

  /* ---- Filters ---- */
  function renderFilters() {
    // venue chips
    var venues = [
      { key: "defi", name: "DeFi lending", color: "var(--venue-defi)" },
      { key: "lighter", name: "Lighter", color: "var(--venue-lighter)" },
      { key: "hl", name: "Hyperliquid", color: "var(--venue-hl)" }
    ];
    $("#venue-chips").innerHTML = venues.map(function (v) {
      return '<button class="chip ' + (state.filters.venues.has(v.key) ? "is-active" : "") + '" data-venue="' + attr(v.key) + '">' +
        '<span class="dot" style="background:' + v.color + '"></span>' + esc(v.name) + '</button>';
    }).join("");

    // chain chips
    var chains = activeChains();
    $("#chain-chips").innerHTML = chains.length ? chains.map(function (c) {
      return '<button class="chip ' + (state.filters.chains.has(c.key) ? "is-active" : "") + '" data-chain="' + attr(c.key) + '" title="' + attr(c.name) + '">' +
        '<span class="dot" style="background:var(--venue-evm)"></span>' + esc(c.name) +
        '<span class="chip-count num">' + fmtUsd(c.usd) + '</span></button>';
    }).join("") : '<span class="muted" style="font-size:12px">No EVM balances</span>';

    $("#sort-select").value = state.prefs.sort;
    $("#status-select").value = state.prefs.status;
    $("#dust-toggle").checked = state.prefs.dustOn;
    $("#dust-threshold").value = num(state.prefs.dust).toFixed(2);
    $("#hide-empty").checked = state.prefs.hideEmpty;
    Array.prototype.forEach.call(document.querySelectorAll(".view-btn"), function (b) {
      b.classList.toggle("is-active", b.getAttribute("data-view") === state.prefs.view);
    });
    $("#btn-clear-filters").hidden = !filtersActive();
  }

  /* ---- Addresses (cards or table) ---- */
  function renderAddresses() {
    var host = $("#addresses-host");
    var sum = state.summary;
    if (!sum || !sum.addresses || !sum.addresses.length) {
      host.innerHTML = '<div class="empty-state"><div class="es-title">No addresses yet</div>' +
        '<div class="es-sub">Add your first wallet address to start tracking your portfolio.</div>' +
        '<button class="btn btn-accent" data-action="open-add">Add your first address</button></div>';
      $("#addr-count").textContent = "0";
      renderExcluded();
      return;
    }
    var list = visibleAddresses();
    $("#addr-count").textContent = String(list.length);

    if (!list.length) {
      host.innerHTML = '<div class="inline-empty">No matches — <button class="btn btn-ghost btn-sm" data-action="clear-filters">clear filters</button></div>';
    } else if (state.prefs.view === "table") {
      host.innerHTML = renderTable(list);
    } else {
      host.innerHTML = '<div class="cards-grid">' + list.map(renderCard).join("") + '</div>';
    }
    reopenDetails();
    renderExcluded();
  }

  function renderExcluded() {
    var host = $("#excluded-host");
    var ex = ((state.summary && state.summary.addresses) || []).filter(function (a) { return a.excluded; });
    if (!ex.length) { host.innerHTML = ""; return; }
    var open = state.prefs.excludedOpen;
    host.innerHTML = '<div class="excluded-group">' +
      '<button class="excluded-head" id="excluded-toggle" aria-expanded="' + open + '">' +
      '<span class="chevron">▸</span> Excluded <span class="count-badge">' + ex.length + '</span></button>' +
      (open ? '<div class="cards-grid" style="margin-top:var(--s4)">' + ex.map(renderCard).join("") + '</div>' : '') +
      '</div>';
  }

  function renderCard(a) {
    var total = addrTotal(a);
    var st = addrStatus(a);
    var v = addrVenues(a);
    var label = a.label || maskAddr(a.address);
    var full = a.address || "";
    var explorers = explorerLinks(full);
    var open = state.openDetails.has(a.id);
    return '<div class="addr-card ' + (a.excluded ? "excluded" : "") + '" data-id="' + attr(a.id) + '">' +
      '<div class="card-loadbar"></div>' +
      '<div class="addr-top">' +
        '<div class="avatar" style="background:' + avatarGradient(full) + '"></div>' +
        '<div class="addr-idmeta">' +
          '<div class="addr-labelrow">' +
            '<span class="addr-label" data-role="label" title="' + attr(label) + '">' + esc(label) + '</span>' +
            '<button class="mini-btn" data-action="edit-label" title="Edit label">' + iconEdit() + '</button>' +
          '</div>' +
          '<div class="addr-sub">' +
            '<span class="addr-hash mono" title="' + attr(full) + '">' + esc(maskAddr(full)) + '</span>' +
            '<button class="mini-btn" data-action="copy-addr" data-addr="' + attr(full) + '" title="Copy address">' + iconCopy() + '</button>' +
            '<span class="explorer-links">' + explorers + '</span>' +
          '</div>' +
        '</div>' +
        '<button class="mini-btn" data-action="toggle-detail" title="Details" aria-expanded="' + open + '">' + iconChevron() + '</button>' +
      '</div>' +
      '<div class="addr-figures">' +
        '<div><div class="addr-total num sensitive">' + fmtUsd(total) + '</div>' + deltaSpan(addrDelta(a)) + '</div>' +
        statusPill(st) +
      '</div>' +
      '<div class="addr-foot">' +
        '<div class="venue-dots">' + venueDots(v) + '</div>' +
        '<span class="addr-refreshed" data-role="reltime" data-ts="' + attr((a.latest && a.latest.ts) || "") + '">' + esc(relTime(a.latest && a.latest.ts)) + '</span>' +
        '<div class="addr-actions">' +
          '<button class="mini-btn" data-action="refresh-addr" title="Refresh">' + iconRefresh() + '</button>' +
          '<button class="mini-btn" data-action="toggle-exclude" title="' + (a.excluded ? "Include" : "Exclude") + '">' + (a.excluded ? iconEye() : iconEyeOff()) + '</button>' +
          '<button class="mini-btn" data-action="remove-addr" title="Remove">' + iconTrash() + '</button>' +
        '</div>' +
      '</div>' +
      '<div class="accordion ' + (open ? "open" : "") + '" data-role="accordion">' +
        '<div class="accordion-inner">' + (open ? renderDetail(a) : "") + '</div>' +
      '</div>' +
    '</div>';
  }

  function statusPill(st) {
    var labels = { ok: "OK", degraded: "Degraded", error: "Error", idle: "Idle" };
    return '<span class="status-pill status-' + esc(st) + '"><span class="dot dot-' + esc(st) + '"></span>' + esc(labels[st] || st) + '</span>';
  }
  function venueDots(v) {
    var out = "";
    if (v.evm) out += '<span class="venue-dot vd-evm" title="EVM chains"></span>';
    if (v.defi) out += '<span class="venue-dot vd-defi" title="DeFi lending"></span>';
    if (v.lighter) out += '<span class="venue-dot vd-lighter" title="Lighter"></span>';
    if (v.hl) out += '<span class="venue-dot vd-hl" title="Hyperliquid"></span>';
    return out || '<span class="muted" style="font-size:11px">—</span>';
  }

  function explorerLinks(addr) {
    if (!addr) return "";
    // Show a couple of primary explorers; more available in detail per-chain.
    var primary = ["ethereum", "base", "arbitrum"];
    return primary.map(function (k) {
      var base = EXPLORERS[k];
      if (!base) return "";
      return '<a href="' + attr(base + "/address/" + addr) + '" target="_blank" rel="noopener" title="' + attr(cap(k)) + '">' + iconExt() + '</a>';
    }).join("");
  }

  /* ---- Table view ---- */
  function renderTable(list) {
    var ts = state.tableSort;
    function caret(key) { return ts.key === key ? '<span class="sort-caret">' + (ts.dir === "asc" ? "▲" : "▼") + '</span>' : '<span class="sort-caret">▴</span>'; }
    function th(key, label, n) { return '<th class="' + (n ? "n " : "") + (ts.key === key ? "sorted" : "") + '" data-sort="' + key + '">' + esc(label) + " " + caret(key) + '</th>'; }
    var sorted = list.slice().sort(function (a, b) {
      var dir = ts.dir === "asc" ? 1 : -1, r = 0;
      if (ts.key === "value") r = addrTotal(a) - addrTotal(b);
      else if (ts.key === "label") r = (a.label || a.address || "").localeCompare(b.label || b.address || "");
      else if (ts.key === "status") r = addrStatus(a).localeCompare(addrStatus(b));
      return r * dir;
    });
    var rows = sorted.map(function (a) {
      var open = state.openDetails.has(a.id);
      return '<tr data-id="' + attr(a.id) + '" data-action="toggle-detail">' +
        '<td><div style="display:flex;align-items:center;gap:8px"><span class="avatar" style="width:22px;height:22px;border-radius:6px;background:' + avatarGradient(a.address) + '"></span>' + esc(a.label || maskAddr(a.address)) + '</div></td>' +
        '<td class="mono" title="' + attr(a.address) + '">' + esc(maskAddr(a.address)) + '</td>' +
        '<td class="n num sensitive">' + fmtUsd(addrTotal(a)) + '</td>' +
        '<td class="n">' + deltaSpan(addrDelta(a)) + '</td>' +
        '<td>' + statusPill(addrStatus(a)) + '</td>' +
        '<td><div class="venue-dots">' + venueDots(addrVenues(a)) + '</div></td>' +
        '<td class="n"><span data-role="reltime" data-ts="' + attr((a.latest && a.latest.ts) || "") + '">' + esc(relTime(a.latest && a.latest.ts)) + '</span></td>' +
        '</tr>' +
        (open ? '<tr class="detail-row"><td class="row-detail-cell" colspan="7"><div class="accordion open" data-role="accordion"><div class="accordion-inner">' + renderDetail(a) + '</div></div></td></tr>' : '');
    }).join("");
    return '<div class="addr-table-wrap"><table class="addr-table"><thead><tr>' +
      th("label", "Address") + '<th>Hash</th>' + th("value", "Value", true) + '<th class="n">Δ prev</th>' + th("status", "Status") +
      '<th>Venues</th><th class="n">Updated</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }

  /* ---- Accordion detail (tabs) ---- */
  function renderDetail(a) {
    var s = a.snapshot;
    var tab = state.activeTab[a.id] || "chains";
    var errs = (s && s.errors) || [];
    var tabs = [["chains", "Chains"], ["tokens", "Tokens"], ["defi", "DeFi"], ["lighter", "Lighter"], ["hyperliquid", "Hyperliquid"]];
    var tabsHtml = tabs.map(function (t) {
      return '<button class="tab ' + (tab === t[0] ? "is-active" : "") + '" data-tab="' + t[0] + '">' + esc(t[1]) + '</button>';
    }).join("");
    var panels =
      '<div class="tab-panel ' + (tab === "chains" ? "is-active" : "") + '" data-panel="chains">' + renderChainsTab(s) + '</div>' +
      '<div class="tab-panel ' + (tab === "tokens" ? "is-active" : "") + '" data-panel="tokens">' + renderTokensTab(a, s) + '</div>' +
      '<div class="tab-panel ' + (tab === "defi" ? "is-active" : "") + '" data-panel="defi">' + renderDefiTab(s) + '</div>' +
      '<div class="tab-panel ' + (tab === "lighter" ? "is-active" : "") + '" data-panel="lighter">' + renderLighterTab(s) + '</div>' +
      '<div class="tab-panel ' + (tab === "hyperliquid" ? "is-active" : "") + '" data-panel="hyperliquid">' + renderHlTab(s) + '</div>';
    var errPanel = errs.length ? '<div class="error-panel" style="margin-top:var(--s4)"><div class="ep-title">Errors (' + errs.length + ')</div><ul>' +
      errs.map(function (e) { return '<li>' + esc(e) + '</li>'; }).join("") + '</ul></div>' : "";
    return '<div class="detail">' + renderAddrSpark(a) + '<div class="tabs">' + tabsHtml + '</div>' + panels + errPanel + '</div>';
  }

  // Small SVG sparkline of this address's own total over time (from lazy per-address history).
  function renderAddrSpark(a) {
    var h = state.addrHistory[a && a.id];
    var d = addrDelta(a);
    if (!h) {
      // Pending fetch: kick it off (once) and show a placeholder.
      loadAddrHistory(a && a.id);
      return '<div class="detail-spark"><div class="detail-spark-empty">Loading history…</div></div>';
    }
    if (h.length < 2) {
      return '<div class="detail-spark"><div class="detail-spark-empty">Refresh at least twice to see this address’s history.</div></div>';
    }
    var pts = h.slice(-60).map(function (p) { return num(p.total_usd); });
    var w = 480, hgt = 48, min = Math.min.apply(null, pts), max = Math.max.apply(null, pts);
    var range = max - min || 1;
    var step = pts.length > 1 ? w / (pts.length - 1) : 0;
    var path = pts.map(function (v, i) {
      var x = i * step, y = hgt - 3 - ((v - min) / range) * (hgt - 6);
      return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }).join(" ");
    var up = pts[pts.length - 1] >= pts[0];
    var col = up ? "var(--success)" : "var(--danger)";
    var gid = "aspg-" + (a && a.id);
    return '<div class="detail-spark">' +
      '<div class="detail-spark-head">' +
        '<span class="detail-spark-label">Total over time</span>' +
        '<span class="detail-spark-delta">vs previous ' + deltaSpan(d) + '</span>' +
      '</div>' +
      '<svg class="detail-spark-svg" viewBox="0 0 ' + w + ' ' + hgt + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="' + col + '" stop-opacity=".22"/><stop offset="1" stop-color="' + col + '" stop-opacity="0"/>' +
      '</linearGradient></defs>' +
      '<path d="' + path + ' L' + w + ' ' + hgt + ' L0 ' + hgt + ' Z" fill="url(#' + gid + ')"/>' +
      '<path d="' + path + '" fill="none" stroke="' + col + '" stroke-width="1.6"/></svg>' +
    '</div>';
  }

  function renderChainsTab(s) {
    var chains = ((s && s.chains) || []).slice().sort(function (a, b) { return num(b.total_usd) - num(a.total_usd); });
    if (!chains.length) return '<div class="tab-empty">No chain data</div>';
    var max = Math.max.apply(null, chains.map(function (c) { return num(c.total_usd); })) || 1;
    return chains.map(function (c) {
      var val = num(c.total_usd), w = Math.max(2, (val / max) * 100);
      var cov = c.coverage || {};
      var link = EXPLORERS[c.key] ? '<a href="' + attr(EXPLORERS[c.key]) + '" target="_blank" rel="noopener" title="Explorer">' + iconExt() + '</a>' : "";
      var errBadge = c.error ? '<span class="status-pill status-error" style="margin-left:6px">err</span>' : "";
      return '<div class="chain-bar-row">' +
        '<div class="chain-bar-name">' + esc(c.name || c.key) + link + errBadge + '</div>' +
        '<div class="chain-bar-track"><div class="chain-bar-fill" style="width:' + w + '%"></div></div>' +
        '<div class="chain-meta num sensitive">' + fmtUsd(val) +
        (cov.tokens_nonzero != null ? ' <span class="muted">· ' + num(cov.tokens_nonzero) + "/" + num(cov.tokens_checked) + ' tok</span>' : "") +
        '</div>' +
        (c.error ? '<div class="chain-meta neg" style="grid-column:1/-1">' + esc(c.error) + '</div>' : "") +
      '</div>';
    }).join("");
  }

  function renderTokensTab(a, s) {
    var rows = [];
    var addrTot = addrTotal(a) || 1;
    ((s && s.chains) || []).forEach(function (c) {
      if (c.native && num(c.native.balance) > 0) rows.push({ sym: c.native.symbol, chain: c.name || c.key, bal: c.native.balance, price: c.native.price_usd, val: c.native.value_usd, ch24: null });
      (c.tokens || []).forEach(function (t) { rows.push({ sym: t.symbol, chain: c.name || c.key, bal: t.balance, price: t.price_usd, val: t.value_usd, ch24: t.change_24h }); });
    });
    if (!rows.length) return '<div class="tab-empty">No tokens</div>';
    rows.sort(function (x, y) { return num(y.val) - num(x.val); });
    var body = rows.map(function (r) {
      var ch = r.ch24 == null ? "" : '<span class="' + deltaClass(num(r.ch24)) + '">' + fmtPct(num(r.ch24)) + '</span>';
      return '<tr>' +
        '<td class="sym">' + esc(r.sym) + '</td>' +
        '<td>' + esc(r.chain) + '</td>' +
        '<td class="n num sensitive" title="' + attr(fullNum(r.bal)) + '">' + fmtQty(r.bal) + '</td>' +
        '<td class="n num">' + fmtUsd(r.price, { noAbbrev: true }) + '</td>' +
        '<td class="n num">' + ch + '</td>' +
        '<td class="n num sensitive">' + fmtUsd(r.val) + '</td>' +
        '<td class="n num">' + (num(r.val) / addrTot * 100).toFixed(1) + '%</td>' +
      '</tr>';
    }).join("");
    return '<table class="dtable"><thead><tr><th>Symbol</th><th>Chain</th><th class="n">Balance</th><th class="n">Price</th><th class="n">24h</th><th class="n">Value</th><th class="n">% addr</th></tr></thead><tbody>' + body + '</tbody></table>';
  }

  function renderDefiTab(s) {
    var defi = (s && s.defi) || {};
    var positions = defi.positions || [];
    var errors = defi.errors || [];
    if (!positions.length && !errors.length) return '<div class="tab-empty">No Morpho, Aave, or Spark lending positions</div>';

    var out = kvGrid([
      ["Net equity", fmtUsd(defi.total_usd), true],
      ["Gross assets", fmtUsd(defi.gross_assets_usd), true],
      ["Supplied", fmtUsd(defi.supplied_usd), true],
      ["Collateral enabled", fmtUsd(defi.collateral_usd), true],
      ["Debt", fmtUsd(defi.borrowed_usd), true]
    ]);
    var protocols = defi.protocols || [];
    if (protocols.length) {
      out += '<div class="defi-protocols">' + protocols.map(function (p) {
        var active = (p.positions || []).length;
        return '<div class="defi-protocol"><span class="sym">' + esc(p.name || p.key) + '</span>' +
          '<span class="num sensitive">' + fmtUsd(p.total_usd) + '</span>' +
          '<span class="muted">' + active + ' position' + (active === 1 ? '' : 's') + '</span></div>';
      }).join("") + '</div>';
    }
    if (positions.length) {
      positions.sort(function (a, b) { return Math.abs(num(b.total_usd)) - Math.abs(num(a.total_usd)); });
      var body = positions.map(function (p) {
        var hf = p.health_factor == null ? "-" : (num(p.health_factor) > 1000000 ? "Safe" : num(p.health_factor).toFixed(2));
        var label = p.pair || p.asset || "?";
        return '<tr><td class="sym">' + esc(p.protocol) + '</td><td>' + esc(p.market) + '</td>' +
          '<td>' + esc(p.chain_name || p.chain) + '</td><td>' + esc(p.position_type) + '</td><td class="sym">' + esc(label) + '</td>' +
          '<td class="n num sensitive">' + fmtUsd(p.supplied_usd) + '</td>' +
          '<td class="n num sensitive">' + fmtUsd(p.collateral_usd) + '</td>' +
          '<td class="n num sensitive">' + fmtUsd(p.borrowed_usd) + '</td>' +
          '<td class="n num sensitive ' + deltaClass(num(p.total_usd)) + '">' + fmtUsd(p.total_usd) + '</td>' +
          '<td class="n num">' + esc(hf) + '</td></tr>';
      }).join("");
      out += '<div class="defi-table-wrap"><table class="dtable"><thead><tr><th>Protocol</th><th>Market</th><th>Chain</th><th>Type</th><th>Asset / pair</th><th class="n">Supplied</th><th class="n">Collateral</th><th class="n">Debt</th><th class="n">Net</th><th class="n">Health</th></tr></thead><tbody>' + body + '</tbody></table></div>';
    }
    if (errors.length) out += errList("DeFi source errors", errors);
    return out;
  }

  function renderLighterTab(s) {
    var lt = (s && s.lighter) || {};
    if (!lt.ok && !(lt.accounts && lt.accounts.length) && lt.total_usd == null) {
      if (lt.errors && lt.errors.length) return errList("Lighter errors", lt.errors);
      return '<div class="tab-empty">No Lighter account</div>';
    }
    var stk = lt.staking || {};
    var lit = lt.lit || {};
    var litTotal = lit.total_lit != null ? lit.total_lit : num(stk.staked_lit);
    var litTotalUsd = lit.total_value_usd != null ? lit.total_value_usd : stk.staked_lit_value_usd;
    var litStaked = lit.staked_lit != null ? lit.staked_lit : stk.staked_lit;
    var litStakedUsd = lit.staked_value_usd != null ? lit.staked_value_usd : stk.staked_lit_value_usd;
    var kvRows = [
      ["Total", fmtUsd(lt.total_usd), true],
      ["Collateral", fmtUsd(lt.collateral), true],
      ["Available", fmtUsd(lt.available_balance), true],
      ["LIT total", fmtQty(litTotal) + (litTotalUsd != null ? " (" + fmtUsd(litTotalUsd) + ")" : ""), true],
      ["LIT spot", fmtQty(lit.spot_lit) + (lit.spot_value_usd != null ? " (" + fmtUsd(lit.spot_value_usd) + ")" : ""), true],
      ["Staked LIT", fmtQty(litStaked) + (litStakedUsd != null ? " (" + fmtUsd(litStakedUsd) + ")" : ""), true]
    ];
    if (num(lit.locked_lit) > 0) {
      kvRows.push(["LIT locked (exchange)", fmtQty(lit.locked_lit) + (lit.locked_value_usd != null ? " (" + fmtUsd(lit.locked_value_usd) + ")" : ""), true]);
    }
    if (num(stk.pending_unstake_lit) > 0) {
      kvRows.push(["Unstaking (pending unlock)", fmtQty(stk.pending_unstake_lit) + (stk.pending_unstake_lit_value_usd != null ? " (" + fmtUsd(stk.pending_unstake_lit_value_usd) + ")" : ""), true]);
    }
    if (num(lt.pool_deposits_usd) > 0) {
      kvRows.push(["Pool deposits (LLP)", fmtUsd(lt.pool_deposits_usd), true]);
    }
    if (num(lt.operator_pool_value_usd) > 0) {
      kvRows.push(["Owned pool operator equity", fmtUsd(lt.operator_pool_value_usd), true]);
    }
    if (num(stk.staking_pnl) !== 0) {
      kvRows.push(["Staking PnL", pnlSpan(stk.staking_pnl), false]);
    }
    var kv = kvGrid(kvRows);
    // Per-unlock breakdown with human-readable unlock time.
    var unlocks = (stk.pending_unlocks || []).filter(function (u) { return num(u.amount) > 0; });
    if (unlocks.length) {
      kv += '<div class="kpi-label" style="margin:12px 0 4px">Pending unlocks</div>' +
        '<table class="dtable"><thead><tr><th class="n">LIT</th><th>Unlocks at</th></tr></thead><tbody>' +
        unlocks.map(function (u) {
          return '<tr><td class="n num sensitive">' + fmtQty(u.amount) + '</td><td>' + esc(fmtUnlockTime(u.unlock_timestamp)) + '</td></tr>';
        }).join("") + '</tbody></table>';
    }
    var pools = (lt.pool_deposits || []).filter(function (d) { return num(d.value_usd) > 0 || num(d.principal_amount) > 0 || num(d.shares_amount) > 0; });
    if (pools.length) {
      kv += '<div class=kpi-label>Pool deposits</div>' +
        '<table class=dtable><thead><tr><th>Pool</th><th class=n>Supplied</th><th class=n>Shares</th><th class=n>Value</th></tr></thead><tbody>' +
        pools.map(function (d) {
          var under = (d.underlying || [])[0] || {};
          var label = d.pool_name || (d.is_lit_staking ? 'LIT staking' : ('Pool ' + d.public_pool_index));
          var supplied = under.amount != null ? fmtQty(under.amount) + ' ' + esc(under.symbol || '') : fmtQty(d.principal_amount);
          return '<tr><td class=sym>' + esc(label) + '</td><td class=n>' + supplied + '</td><td class=n>' + fmtQty(d.shares_amount) + '</td><td class=n>' + fmtUsd(d.value_usd) + '</td></tr>';
        }).join('') + '</tbody></table>';
    }
    var accounts = (lt.accounts || []).map(function (ac) {
      var ownedPool = Boolean(ac.is_public_pool);
      var accountName = ac.name || (ownedPool ? ("Owned public pool " + (ac.index != null ? ac.index : "")) : ("Subaccount " + (ac.index != null ? ac.index : "")));
      var displayedValue = ownedPool ? ac.operator_share_value_usd : ac.total_asset_value;
      var head = '<div class="subacc-head"><span class="subacc-name">' + esc(accountName) + '</span>' +
        '<span class="num sensitive">' + fmtUsd(displayedValue) + '</span></div>';
      var ackv = kvGrid(ownedPool ? [
        ["Operator equity", fmtUsd(ac.operator_share_value_usd), true],
        ["Full pool TVL", fmtUsd(ac.total_asset_value), true],
        ["Operator shares", fmtQty(ac.operator_shares), true],
        ["Total pool shares", fmtQty(ac.pool_total_shares), true]
      ] : [
        ["Collateral", fmtUsd(ac.collateral), true],
        ["Available", fmtUsd(ac.available_balance), true]
      ]);
      var pos = (ac.positions || []);
      var posTable = pos.length ? posTableLighter(pos) : '';
      var assets = (ac.assets || []);
      var assetLabel = ownedPool && assets.length ? '<div class="kpi-label" style="margin-top:8px">Full pool assets</div>' : '';
      var assetTable = assets.length ? '<table class="dtable" style="margin-top:8px"><thead><tr><th>Asset</th><th class="n">Spot</th><th class="n">Locked</th><th class="n">Available</th><th class="n">Margin</th><th class="n">Price</th><th class="n">Value</th></tr></thead><tbody>' +
        assets.map(function (as) { return '<tr><td class="sym">' + esc(as.symbol) + '</td><td class="n num sensitive">' + fmtQty(as.spot_balance != null ? as.spot_balance : as.balance) + '</td><td class="n num sensitive">' + fmtQty(as.locked_balance) + '</td><td class="n num sensitive">' + fmtQty(as.available_balance) + '</td><td class="n num sensitive">' + fmtQty(as.margin_balance) + '</td><td class="n num">' + fmtUsd(as.price_usd, { noAbbrev: true }) + '</td><td class="n num sensitive">' + fmtUsd(as.value_usd) + '</td></tr>'; }).join("") + '</tbody></table>' : '';
      return '<div class="subacc">' + head + ackv + posTable + assetLabel + assetTable + '</div>';
    }).join("");
    var errPanel = lt.errors && lt.errors.length ? errList("Lighter errors", lt.errors) : "";
    return kv + accounts + errPanel;
  }

  function posTableLighter(pos) {
    var body = pos.map(function (p) {
      return '<tr><td class="sym">' + esc(p.symbol) + '</td>' + sideCell(p.side) +
        '<td class="n num sensitive">' + fmtQty(p.size) + '</td>' +
        '<td class="n num">' + fmtUsd(p.entry_price, { noAbbrev: true }) + '</td>' +
        '<td class="n num sensitive">' + fmtUsd(p.position_value) + '</td>' +
        '<td class="n num ' + deltaClass(num(p.unrealized_pnl)) + '">' + fmtUsdSigned(p.unrealized_pnl) + '</td>' +
        '<td class="n num">' + fmtUsd(p.liquidation_price, { noAbbrev: true }) + '</td>' +
        '<td class="n num">' + (p.leverage != null ? num(p.leverage).toFixed(1) + "x" : "—") + '</td></tr>';
    }).join("");
    return '<table class="dtable" style="margin-top:8px"><thead><tr><th>Symbol</th><th>Side</th><th class="n">Size</th><th class="n">Entry</th><th class="n">Value</th><th class="n">uPnL</th><th class="n">Liq</th><th class="n">Lev</th></tr></thead><tbody>' + body + '</tbody></table>';
  }

  function renderHlTab(s) {
    var hl = (s && s.hyperliquid) || {};
    var perp = hl.perp, spot = hl.spot;
    if (!perp && !spot) {
      if (hl.errors && hl.errors.length) return errList("Hyperliquid errors", hl.errors);
      return '<div class="tab-empty">No Hyperliquid account</div>';
    }
    var out = "";
    if (perp) {
      var ms = perp.margin_summary || {};
      var vaults = hl.vaults || {};
      var staking = hl.staking || {};
      var modeLabels = {
        disabled: "Standard",
        unifiedAccount: "Unified",
        portfolioMargin: "Portfolio margin"
      };
      var modeLabel = modeLabels[hl.account_mode] || "Unknown";
      out += kvGrid([
        ["HL portfolio total", fmtUsd(hl.total_usd), true],
        ["Account mode", esc(modeLabel), false],
        ["Direct trading equity", fmtUsd(hl.direct_equity_usd), true],
        ["Spot / collateral", fmtUsd(hl.spot_usd != null ? hl.spot_usd : (spot && spot.total_usd)), true],
        ["Perp equity", fmtUsd(perp.account_value), true],
        ["Vault equity", fmtUsd(vaults.total_usd), true],
        ["Staked HYPE", fmtQty(staking.hype || 0) + " / " + fmtUsd(staking.total_usd), true],
        ["Balance before uPnL", fmtUsd(perp.balance_without_upnl), true],
        ["Open uPnL", fmtUsdSigned(perp.total_unrealized_pnl), false],
        ["Withdrawable", fmtUsd(perp.withdrawable), true],
        ["Margin used", fmtUsd(ms.total_margin_used), true],
        ["Notional", fmtUsd(ms.total_notional_position), true]
      ]);

      var dexes = (perp.dexes || []).filter(function (d) {
        return Math.abs(num(d.account_value)) > 0;
      });
      if (dexes.length > 1) {
        var dbody = dexes.map(function (d) {
          return '<tr><td class="sym">' + esc(d.dex === "default" ? "Default" : d.dex) + '</td>' +
            '<td class="n num sensitive">' + fmtUsd(d.account_value) + '</td></tr>';
        }).join("");
        out += '<div class="kpi-label" style="margin:12px 0 4px">Perp DEX equity</div>' +
          '<table class="dtable"><thead><tr><th>DEX</th><th class="n">Equity</th></tr></thead><tbody>' + dbody + '</tbody></table>';
      }

      var pos = perp.positions || [];
      if (pos.length) {
        var showDex = dexes.length > 1 || pos.some(function (p) { return p.dex && p.dex !== "default"; });
        var body = pos.map(function (p) {
          return '<tr>' + (showDex ? '<td class="sym">' + esc(p.dex === "default" ? "Default" : p.dex) + '</td>' : '') +
            '<td class="sym">' + esc(p.coin) + '</td>' + sideCell(p.side) +
            '<td class="n num sensitive">' + fmtQty(p.size) + '</td>' +
            '<td class="n num">' + fmtUsd(p.entry_price, { noAbbrev: true }) + '</td>' +
            '<td class="n num sensitive">' + fmtUsd(p.position_value) + '</td>' +
            '<td class="n num sensitive">' + fmtUsd(p.margin_used) + '</td>' +
            '<td class="n num ' + deltaClass(num(p.unrealized_pnl)) + '">' + fmtUsdSigned(p.unrealized_pnl) + '</td>' +
            '<td class="n num ' + deltaClass(num(p.return_on_equity)) + '">' + fmtPct(num(p.return_on_equity) * 100) + '</td>' +
            '<td class="n num">' + fmtUsd(p.liquidation_price, { noAbbrev: true }) + '</td></tr>';
        }).join("");
        out += '<table class="dtable" style="margin-top:8px"><thead><tr>' +
          (showDex ? '<th>DEX</th>' : '') +
          '<th>Coin</th><th>Side</th><th class="n">Size</th><th class="n">Entry</th><th class="n">Value</th><th class="n">Margin</th><th class="n">uPnL</th><th class="n">ROE</th><th class="n">Liq</th></tr></thead><tbody>' + body + '</tbody></table>';
      }
    }
    if (spot && spot.balances && spot.balances.length) {
      var sbody = spot.balances.map(function (b) {
        return '<tr><td class="sym">' + esc(b.coin) + '</td>' +
          '<td class="n num sensitive">' + fmtQty(b.total) + '</td>' +
          '<td class="n num sensitive">' + fmtQty(b.hold) + '</td>' +
          '<td class="n num">' + fmtUsd(b.price_usd, { noAbbrev: true }) + '</td>' +
          '<td class="n num sensitive">' + fmtUsd(b.value_usd) + '</td></tr>';
      }).join("");
      out += '<div class="kpi-label" style="margin:12px 0 4px">Spot - ' + fmtUsd(spot.total_usd) + '</div>' +
        '<table class="dtable"><thead><tr><th>Coin</th><th class="n">Total</th><th class="n">Hold</th><th class="n">Price</th><th class="n">Value</th></tr></thead><tbody>' + sbody + '</tbody></table>';
    }
    if (hl.errors && hl.errors.length) out += errList("Hyperliquid errors", hl.errors);
    return out || '<div class="tab-empty">No positions</div>';
  }

  function sideCell(side) {
    var s = String(side || "").toLowerCase();
    var isLong = s === "long" || s === "buy" || s === "b";
    var isShort = s === "short" || s === "sell" || s === "a";
    var cls = isLong ? "side-long" : isShort ? "side-short" : "";
    var lbl = isLong ? "LONG" : isShort ? "SHORT" : (side || "—");
    return '<td><span class="side-badge ' + cls + '">' + esc(lbl) + '</span></td>';
  }
  function pnlSpan(v) { return '<span class="' + deltaClass(num(v)) + '">' + fmtUsdSigned(v) + '</span>'; }
  function kvGrid(pairs) {
    return '<div class="kv-grid">' + pairs.map(function (p) {
      return '<div class="kv"><span class="kv-k">' + esc(p[0]) + '</span><span class="kv-v num ' + (p[2] ? "sensitive" : "") + '">' + p[1] + '</span></div>';
    }).join("") + '</div>';
  }
  function errList(title, errs) {
    return '<div class="error-panel"><div class="ep-title">' + esc(title) + ' (' + errs.length + ')</div><ul>' +
      errs.map(function (e) { return '<li>' + esc(e) + '</li>'; }).join("") + '</ul></div>';
  }

  /* ---- Global holdings ---- */
  function renderHoldings() {
    var open = state.prefs.holdingsOpen;
    var host = $("#holdings-inner");
    var toggle = $("#holdings-toggle");
    toggle.setAttribute("aria-expanded", String(open));
    $("#holdings-body").classList.toggle("open", open);

    var dust = state.prefs.dustOn ? num(state.prefs.dust) : 0;
    var holdings = mergedHoldings().filter(function (m) { return dust <= 0 || m.value >= dust; });
    $("#holdings-count").textContent = String(holdings.length);
    if (!open) { host.innerHTML = ""; return; }
    if (!holdings.length) { host.innerHTML = '<div class="inline-empty">No holdings</div>'; return; }
    var rows = holdings.map(function (m, i) {
      var breakRows = m.rows.filter(function (r) { return r.value > 0; }).sort(function (a, b) { return b.value - a.value; })
        .map(function (r) { return '<tr class="holdings-break" data-parent="' + i + '" hidden><td colspan="5">' + esc(r.chain || "?") + ' · ' + fmtQty(r.balance) + ' · <span class="sensitive">' + fmtUsd(r.value) + '</span></td></tr>'; }).join("");
      return '<tr class="holdings-row expandable" data-hrow="' + i + '">' +
        '<td class="sym">' + esc(m.symbol) + '</td>' +
        '<td class="n num sensitive" title="' + attr(fullNum(m.balance)) + '">' + fmtQty(m.balance) + '</td>' +
        '<td class="n num">' + fmtUsd(m.price, { noAbbrev: true }) + '</td>' +
        '<td class="n num sensitive">' + fmtUsd(m.value) + '</td>' +
        '<td class="n muted">' + m.addrCount + ' addr · ' + m.chainCount + ' src</td>' +
      '</tr>' + breakRows;
    }).join("");
    host.innerHTML = '<table class="holdings-table"><thead><tr><th>Symbol</th><th class="n">Balance</th><th class="n">Price</th><th class="n">Value</th><th class="n">Sources</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }

  /* ---- icons ---- */
  function iconEdit() { return '<svg viewBox="0 0 24 24" width="13" height="13"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>'; }
  function iconCopy() { return '<svg viewBox="0 0 24 24" width="13" height="13"><rect x="9" y="9" width="11" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path fill="none" stroke="currentColor" stroke-width="2" d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>'; }
  function iconExt() { return '<svg viewBox="0 0 24 24" width="12" height="12"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>'; }
  function iconChevron() { return '<svg viewBox="0 0 24 24" width="15" height="15"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6"/></svg>'; }
  function iconRefresh() { return '<svg viewBox="0 0 24 24" width="14" height="14"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15"/></svg>'; }
  function iconTrash() { return '<svg viewBox="0 0 24 24" width="14" height="14"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>'; }
  function iconEye() { return '<svg viewBox="0 0 24 24" width="14" height="14"><path fill="none" stroke="currentColor" stroke-width="2" d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="2"/></svg>'; }
  function iconEyeOff() { return '<svg viewBox="0 0 24 24" width="14" height="14"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M17.9 17.9A10.7 10.7 0 0 1 12 20C5 20 1 12 1 12a19.6 19.6 0 0 1 5.1-6M1 1l22 22M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>'; }

  /* ===================== 7. TOASTS / OVERLAYS ===================== */

  function toast(msg, kind, ms) {
    var stack = $("#toast-stack");
    while (stack.children.length >= 3) stack.removeChild(stack.firstChild);
    var t = el('<div class="toast ' + (kind || "") + '"><span class="toast-msg">' + esc(msg) + '</span></div>');
    stack.appendChild(t);
    var life = ms || (kind === "error" ? 8000 : 5000);
    setTimeout(function () {
      t.classList.add("leaving");
      setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 240);
    }, life);
  }

  function closeMenus() {
    Array.prototype.forEach.call(document.querySelectorAll(".menu"), function (m) { m.hidden = true; });
    Array.prototype.forEach.call(document.querySelectorAll('[aria-haspopup="true"]'), function (b) { b.setAttribute("aria-expanded", "false"); });
  }

  function openDrawer() {
    var d = $("#settings-drawer"), sc = $("#drawer-scrim");
    syncSettings();
    d.hidden = false; sc.hidden = false; d.setAttribute("aria-hidden", "false");
  }
  function closeDrawer() { $("#settings-drawer").hidden = true; $("#drawer-scrim").hidden = true; $("#settings-drawer").setAttribute("aria-hidden", "true"); }
  function openShortcuts() { $("#shortcuts-scrim").hidden = false; }
  function closeShortcuts() { $("#shortcuts-scrim").hidden = true; }

  function syncSettings() {
    setSeg("#set-theme", "theme-val", document.documentElement.getAttribute("data-theme"));
    setSeg("#set-view", "view-val", state.prefs.view);
    setSeg("#set-range", "range-val", state.prefs.range);
    $("#set-dust-toggle").checked = state.prefs.dustOn;
    $("#set-dust-threshold").value = num(state.prefs.dust).toFixed(2);
  }
  function setSeg(sel, attrName, val) {
    Array.prototype.forEach.call(document.querySelectorAll(sel + " .seg-btn"), function (b) {
      b.classList.toggle("is-active", b.getAttribute("data-" + attrName) === val);
    });
  }

  /* ===================== 8. ACTIONS ===================== */

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    state.prefs.theme = theme; store("theme", theme);
  }
  function applyPrivacy(on) {
    state.prefs.privacy = on; store("privacy", on);
    document.body.classList.toggle("privacy", on);
    $("#btn-privacy").setAttribute("aria-pressed", String(on));
  }
  function setView(view) {
    state.prefs.view = view; store("view", view);
    renderFilters(); renderAddresses();
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast("Copied", "success"); }, function () { fallbackCopy(text); });
    } else fallbackCopy(text);
  }
  function fallbackCopy(text) {
    var ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast("Copied", "success"); } catch (e) { toast("Copy failed", "error"); }
    ta.remove();
  }

  /* ---- Add-address modal ---- */
  var ADDR_RE = /0x[a-fA-F0-9]{40}/;

  function parseGuestWallets(raw, defaultLabel) {
    var imported = {}, skipped = 0;
    String(raw || "").split(/\r?\n/).forEach(function (line, index) {
      var text = line.trim();
      if (!text) return;
      if (index === 0 && /^(address|wallet|wallet_address|evm_address)(,|$)/i.test(text)) return;
      var matches = text.match(/0x[a-fA-F0-9]{40}/g) || [];
      if (!matches.length) { skipped += 1; return; }
      var cells = text.split(",");
      var rowLabel = cells.length > 1 ? cells.slice(1).join(",").trim() : "";
      matches.forEach(function (address) {
        address = address.toLowerCase();
        if (!imported[address]) imported[address] = { address: address, label: rowLabel || defaultLabel || null };
      });
    });
    return { wallets: Object.keys(imported).map(function (address) { return imported[address]; }), skipped: skipped };
  }

  function openAddModal() {
    closeMenus();
    var scrim = $("#add-scrim");
    scrim.hidden = false;
    setAddError("");
    $("#add-address-input").value = "";
    $("#add-label-input").value = "";
    $("#add-csv-file").value = "";
    $("#add-csv-status").textContent = "No file selected";
    $("#add-address-input").classList.remove("invalid");
    $("#btn-submit-add").disabled = false;
    // focus after paint
    setTimeout(function () { $("#add-address-input").focus(); }, 0);
  }
  function closeAddModal() {
    $("#add-scrim").hidden = true;
  }
  function isAddOpen() { return !$("#add-scrim").hidden; }

  function setAddError(msg) {
    var box = $("#add-error");
    if (msg) { box.textContent = msg; box.hidden = false; $("#add-address-input").classList.add("invalid"); }
    else { box.textContent = ""; box.hidden = true; $("#add-address-input").classList.remove("invalid"); }
  }

  function loadCsvFile(file) {
    if (!file) return;
    var status = $("#add-csv-status");
    if (!/\.csv$/i.test(file.name || "")) {
      setAddError("Choose a .csv file.");
      status.textContent = "Unsupported file";
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      setAddError("CSV files are limited to 2 MB.");
      status.textContent = "File is too large";
      return;
    }
    var reader = new FileReader();
    status.textContent = "Reading " + file.name + "...";
    reader.onload = function () {
      var text = String(reader.result || "");
      if (!text.trim()) {
        setAddError("The selected CSV file is empty.");
        status.textContent = file.name + " is empty";
        return;
      }
      $("#add-address-input").value = text;
      status.textContent = file.name + " - " + Math.max(1, text.split(/\r?\n/).filter(Boolean).length) + " rows loaded";
      setAddError("");
      $("#add-address-input").focus();
    };
    reader.onerror = function () {
      setAddError("Could not read the selected CSV file.");
      status.textContent = "Read failed";
    };
    reader.readAsText(file);
  }

  function submitAddAddress() {
    var raw = ($("#add-address-input").value || "").trim();
    if (!raw || !ADDR_RE.test(raw)) {
      setAddError("No valid EVM address found in the pasted data.");
      $("#add-address-input").focus();
      return;
    }
    var label = ($("#add-label-input").value || "").trim() || null;
    var submitBtn = $("#btn-submit-add");
    submitBtn.disabled = true; setAddError("");

    if (state.storageMode === "guest") {
      var parsed = parseGuestWallets(raw, label);
      if (!parsed.wallets.length) {
        setAddError("No valid EVM address found in the pasted data.");
        submitBtn.disabled = false;
        return;
      }
      guestStore().listWallets().then(function (existing) {
        var known = {};
        existing.forEach(function (wallet) { known[wallet.address] = true; });
        parsed.wallets.forEach(function (wallet) { known[wallet.address] = true; });
        if (Object.keys(known).length > 1000) throw new Error("Import is limited to 1000 local wallets.");
        return guestStore().upsertWallets(parsed.wallets);
      }).then(function (rows) {
        var importedIds = rows.map(function (wallet) { return Number(wallet.id); });
        closeAddModal();
        toast("Imported " + rows.length + " wallet" + (rows.length === 1 ? "" : "s") +
          (parsed.skipped ? " (" + parsed.skipped + " skipped)" : ""), "success");
        return loadSummary().then(function () {
          var selected = selectedScopeIds().slice();
          importedIds.forEach(function (id) { if (selected.indexOf(id) < 0) selected.push(id); });
          state.prefs.scopeIds = selected;
          store("scopeIds", selected);
          return loadHistory();
        }).then(function () {
          renderAll();
          prefetchAddrHistories();
          startRefreshAll();
        });
      }).catch(function (error) {
        setAddError(error.message || "Could not save wallets in this browser.");
        submitBtn.disabled = false;
      });
      return;
    }

    api("/api/addresses/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: raw, label: label })
    }).then(function (res) {
      var body = res.body || {};
      if (!res.ok || body.ok === false || !body.addresses || !body.addresses.length) {
        setAddError(body.error || "Could not import wallets.");
        submitBtn.disabled = false;
        return;
      }
      var importedIds = body.addresses.map(function (a) { return Number(a.id); });
      closeAddModal();
      toast("Imported " + body.imported + " wallet" + (body.imported === 1 ? "" : "s") +
        (body.skipped ? " (" + body.skipped + " row" + (body.skipped === 1 ? "" : "s") + " skipped)" : ""), "success");
      return loadSummary().then(function () {
        var selected = selectedScopeIds().slice();
        importedIds.forEach(function (id) { if (selected.indexOf(id) < 0) selected.push(id); });
        state.prefs.scopeIds = selected;
        store("scopeIds", selected);
        return loadHistory();
      }).then(function () {
        renderAll();
        prefetchAddrHistories();
        startRefreshAll();
      });
    }).catch(function () {
      setAddError("Network error - please try again.");
      submitBtn.disabled = false;
    });
  }

  // After adding, pull summary so the new card exists, mark it busy, run the blocking
  // per-address refresh, then re-fetch summary + history and re-render.
  function refreshAddedAddress(id) {
    return loadSummary().then(function () {
      renderAll();
      prefetchAddrHistories();
      maybeRefreshOnLoad();
      var card = document.querySelector('.addr-card[data-id="' + id + '"]');
      if (card) card.classList.add("busy");
      invalidateAddrHistory(numOrStr(id));
      return api("/api/addresses/" + id + "/refresh", { method: "POST" }).then(function (res) {
        if (!res.ok || (res.body && res.body.ok === false)) {
          toast((res.body && res.body.error) || "Added, but refresh failed", "warn");
        }
      }).catch(function () {
        toast("Added, but refresh failed", "warn");
      }).then(function () {
        return loadSummary().then(function () { return loadHistory(); }).then(function () {
          renderAll();
          loadAddrHistory(numOrStr(id));
        });
      });
    });
  }

  function patchAddress(id, fields) {
    if (state.storageMode === "guest") {
      return guestStore().updateWallet(id, fields).then(function (wallet) {
        return { ok: !!wallet, status: wallet ? 200 : 404, body: { ok: !!wallet, address: wallet } };
      });
    }
    return api("/api/addresses/" + id, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fields)
    });
  }

  function refreshGuestWallets(wallets, onProgress) {
    var max = num(state.runtimeConfig.max_addresses_per_refresh) || 25;
    var chunks = [];
    for (var i = 0; i < wallets.length; i += max) chunks.push(wallets.slice(i, i + max));
    var completed = 0, degraded = 0;
    var chain = Promise.resolve();
    chunks.forEach(function (chunk) {
      chain = chain.then(function () {
        return api("/api/guest/refresh", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ addresses: chunk.map(function (wallet) { return wallet.address; }) })
        }).then(function (res) {
          if (!res.ok || !res.body || res.body.ok === false) {
            throw new Error((res.body && res.body.error) || "Refresh failed");
          }
          var byAddress = {};
          chunk.forEach(function (wallet) { byAddress[wallet.address] = wallet; });
          return Promise.all((res.body.results || []).map(function (result) {
            var wallet = byAddress[String(result.address || "").toLowerCase()];
            if (!wallet || !result.payload) return null;
            if (result.payload.status === "error" || result.payload.status === "degraded") degraded += 1;
            completed += 1;
            if (onProgress) onProgress(completed, wallets.length);
            return guestStore().saveSnapshot(wallet.id, result.payload);
          }));
        });
      });
    });
    return chain.then(function () { return { completed: completed, degraded: degraded }; });
  }

  function refreshOne(id, card) {
    if (card) card.classList.add("busy");
    if (state.storageMode === "guest") {
      var wallet = findAddr(id);
      if (!wallet) return Promise.resolve();
      return refreshGuestWallets([wallet]).then(function () {
        toast("Address refreshed", "success");
        invalidateAddrHistory(numOrStr(id));
        return loadSummary().then(function () { return loadHistory(); }).then(function () {
          renderAll();
          loadAddrHistory(numOrStr(id));
        });
      }).catch(function (error) {
        toast(error.message || "Refresh failed", "error");
      }).then(function () { if (card) card.classList.remove("busy"); });
    }
    return api("/api/addresses/" + id + "/refresh", { method: "POST" }).then(function (res) {
      if (res.ok) {
        toast("Address refreshed", "success");
        invalidateAddrHistory(numOrStr(id));
        return loadSummary().then(function () {
          renderAll();
          loadAddrHistory(numOrStr(id));
        });
      }
      toast((res.body && res.body.error) || "Refresh failed", "error");
    }).catch(function () { toast("Refresh failed", "error"); })
      .then(function () { if (card) card.classList.remove("busy"); });
  }

  function removeAddress(id) {
    if (!window.confirm("Remove this address and its local history? This cannot be undone.")) return;
    if (state.storageMode === "guest") {
      guestStore().removeWallet(id).then(function () {
        toast("Address removed", "success");
        state.openDetails.delete(id);
        invalidateAddrHistory(id);
        return loadSummary().then(function () { return loadHistory(); }).then(renderAll);
      }).catch(function () { toast("Remove failed", "error"); });
      return;
    }
    api("/api/addresses/" + id, { method: "DELETE" }).then(function (res) {
      if (res.ok) { toast("Address removed", "success"); state.openDetails.delete(id); return loadSummary().then(function () { loadHistory().then(renderAll); }); }
      toast((res.body && res.body.error) || "Remove failed", "error");
    });
  }

  function toggleExclude(id, a) {
    patchAddress(id, { excluded: !a.excluded }).then(function (res) {
      if (res.ok) { toast(a.excluded ? "Address included" : "Address excluded", "success"); return loadSummary().then(function () { return loadHistory(); }).then(renderAll); }
      toast("Update failed", "error");
    });
  }

  /* ---- Refresh-all job with polling ---- */
  function maybeRefreshOnLoad() {
    if (!state.runtimeConfig.auto_refresh_on_load || !state.summary) return;
    var addresses = state.summary.addresses || [];
    if (!addresses.length) return;
    var maxAge = num(state.runtimeConfig.auto_refresh_max_age_seconds) || 86400;
    var stamp = state.summary.last_refresh ? Date.parse(state.summary.last_refresh) : NaN;
    var stale = !isFinite(stamp) || (Date.now() - stamp) >= maxAge * 1000;
    if (stale) setTimeout(startRefreshAll, 300);
  }

  function logoutPrivate() {
    api("/api/auth/logout", { method: "POST" }).then(function () {
      window.location.replace("/login");
    }).catch(function () {
      window.location.replace("/login");
    });
  }

  function startRefreshAll() {
    var btn = $("#btn-refresh");
    if (btn.classList.contains("busy")) return;
    if (state.storageMode === "guest") {
      var wallets = (state.summary && state.summary.addresses) || [];
      if (!wallets.length) { toast("Add an address first", "warn"); return; }
      btn.classList.add("busy");
      setRing(0, wallets.length);
      refreshGuestWallets(wallets, setRing).then(function (result) {
        state.addrHistory = {};
        state.addrHistoryPending = {};
        return loadSummary().then(function () { return loadHistory(); }).then(function () {
          renderAll();
          prefetchAddrHistories();
          toast("Refreshed " + result.completed + " addresses" +
            (result.degraded ? " - " + result.degraded + " degraded" : ""),
            result.degraded ? "warn" : "success");
        });
      }).catch(function (error) {
        toast(error.message || "Could not refresh wallets", "error");
      }).then(function () {
        btn.classList.remove("busy");
        setRing(0, 1);
      });
      return;
    }
    api("/api/refresh", { method: "POST" }).then(function (res) {
      if (res.status === 409 || (res.body && res.body.ok === false)) {
        // already running: attach to poll anyway
        toast("Refresh already running", "warn");
        btn.classList.add("busy"); pollRefresh();
        return;
      }
      if (!res.ok) { toast("Could not start refresh", "error"); return; }
      btn.classList.add("busy"); setRing(0, 1); pollRefresh();
    }).catch(function () { toast("Could not start refresh", "error"); });
  }

  function setRing(completed, total) {
    var frac = total > 0 ? completed / total : 0;
    var circ = 2 * Math.PI * 15.5;
    var ring = document.querySelector(".ring-progress");
    if (ring) { ring.style.strokeDasharray = circ.toFixed(1); ring.style.strokeDashoffset = (circ * (1 - frac)).toFixed(1); }
  }

  function pollRefresh() {
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(function () {
      api("/api/refresh/status").then(function (res) {
        var st = res.body || {};
        setRing(num(st.completed), num(st.total) || 1);
        if (!st.running) {
          clearInterval(state.refreshTimer); state.refreshTimer = null;
          $("#btn-refresh").classList.remove("busy");
          var results = st.results || [];
          var degraded = results.filter(function (r) { return r.status === "degraded" || r.status === "error"; }).length;
          state.addrHistory = {}; state.addrHistoryPending = {};
          loadSummary().then(function () { return loadHistory(); }).then(function () {
            renderAll();
            prefetchAddrHistories();
            toast("Refreshed " + (num(st.completed) || results.length) + " addresses" + (degraded ? " — " + degraded + " degraded" : ""), degraded ? "warn" : "success");
          });
        }
      }).catch(function () {
        clearInterval(state.refreshTimer); state.refreshTimer = null;
        $("#btn-refresh").classList.remove("busy");
        toast("Lost refresh status", "error");
      });
    }, 1000);
  }

  /* ---- Exports ---- */
  function exportJson() {
    if (!state.summary) return;
    download("portfolio-" + Date.now() + ".json", "application/json", JSON.stringify(state.summary, null, 2));
    toast("Exported JSON", "success");
  }
  function exportHoldingsCsv() {
    var lines = [["address", "label", "venue", "chain", "symbol", "balance", "price_usd", "value_usd"].join(",")];
    includedAddresses().forEach(function (a) {
      var s = a.snapshot; if (!s) return;
      var lbl = a.label || "";
      (s.chains || []).forEach(function (c) {
        if (c.native && num(c.native.balance) > 0) lines.push(row(a.address, lbl, "EVM", c.name || c.key, c.native.symbol, c.native.balance, c.native.price_usd, c.native.value_usd));
        (c.tokens || []).forEach(function (t) { lines.push(row(a.address, lbl, "EVM", c.name || c.key, t.symbol, t.balance, t.price_usd, t.value_usd)); });
      });
      var hl = s.hyperliquid || {};
      if (hl.spot && hl.spot.balances) hl.spot.balances.forEach(function (b) { lines.push(row(a.address, lbl, "HL Spot", "hyperliquid", b.coin, b.total, b.price_usd, b.value_usd)); });
      (hl.perp && hl.perp.positions || []).forEach(function (p) { lines.push(row(a.address, lbl, "HL Perp", "hyperliquid", p.coin, p.size, p.entry_price, p.position_value)); });
      var lt = s.lighter || {};
      (lt.accounts || []).forEach(function (ac) {
        (ac.assets || []).forEach(function (as) {
          if (num(as.spot_balance) > 0) lines.push(row(a.address, lbl, "Lighter Spot", "lighter", as.symbol, as.spot_balance, as.price_usd, as.spot_value_usd));
        });
        (ac.positions || []).forEach(function (p) { lines.push(row(a.address, lbl, "Lighter", "lighter", p.symbol, p.size, p.entry_price, p.position_value)); });
      });
    });
    function row() { return Array.prototype.map.call(arguments, csvCell).join(","); }
    download("holdings-" + Date.now() + ".csv", "text/csv", lines.join("\n"));
    toast("Exported holdings CSV", "success");
  }
  function exportHistory(fmt) {
    if (fmt === "csv") {
      var lines = ["ts,total_usd"].concat(state.history.map(function (p) { return csvCell(p.ts) + "," + num(p.total_usd); }));
      download("history-" + Date.now() + ".csv", "text/csv", lines.join("\n"));
    } else {
      download("history-" + Date.now() + ".json", "application/json", JSON.stringify(state.history, null, 2));
    }
    toast("Exported history", "success");
  }

  function bytesToBase64(bytes) {
    var binary = "";
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  function base64ToBytes(value) {
    var binary = atob(value);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function deriveBackupKey(passphrase, salt, usage) {
    return crypto.subtle.importKey(
      "raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]
    ).then(function (baseKey) {
      return crypto.subtle.deriveKey(
        { name: "PBKDF2", salt: salt, iterations: 250000, hash: "SHA-256" },
        baseKey, { name: "AES-GCM", length: 256 }, false, usage
      );
    });
  }

  function localPreferences() {
    var preferences = {};
    for (var i = 0; i < localStorage.length; i++) {
      var key = localStorage.key(i);
      if (key && key.indexOf(LS) === 0) preferences[key] = localStorage.getItem(key);
    }
    return preferences;
  }

  function exportEncryptedBackup() {
    if (state.storageMode !== "guest") return;
    var input = $("#backup-passphrase");
    var passphrase = input.value;
    if (passphrase.length < 8) {
      toast("Use a backup passphrase of at least 8 characters", "warn");
      input.focus();
      return;
    }
    if (!window.crypto || !crypto.subtle) {
      toast("Encrypted backup requires a secure HTTPS connection", "error");
      return;
    }
    var salt = crypto.getRandomValues(new Uint8Array(16));
    var iv = crypto.getRandomValues(new Uint8Array(12));
    guestStore().exportData().then(function (data) {
      data.preferences = localPreferences();
      var plaintext = new TextEncoder().encode(JSON.stringify(data));
      return deriveBackupKey(passphrase, salt, ["encrypt"]).then(function (key) {
        return crypto.subtle.encrypt({ name: "AES-GCM", iv: iv }, key, plaintext);
      });
    }).then(function (ciphertext) {
      var envelope = {
        format: "portfolio-guest-backup", version: 1,
        kdf: "PBKDF2-SHA-256", iterations: 250000,
        cipher: "AES-256-GCM", salt: bytesToBase64(salt),
        iv: bytesToBase64(iv),
        ciphertext: bytesToBase64(new Uint8Array(ciphertext))
      };
      download("portfolio-private-backup-" + Date.now() + ".json", "application/json", JSON.stringify(envelope));
      input.value = "";
      toast("Encrypted backup downloaded", "success");
    }).catch(function () { toast("Could not create encrypted backup", "error"); });
  }

  function restoreEncryptedBackup(file) {
    if (!file || state.storageMode !== "guest") return;
    var input = $("#backup-passphrase");
    var passphrase = input.value;
    if (passphrase.length < 8) {
      toast("Enter the backup passphrase first", "warn");
      input.focus();
      return;
    }
    if (!window.confirm("Replace wallets and history stored in this browser with this backup?")) {
      $("#backup-file").value = "";
      return;
    }
    file.text().then(function (text) {
      var envelope = JSON.parse(text);
      if (envelope.format !== "portfolio-guest-backup" || envelope.version !== 1) throw new Error("Unsupported backup");
      var salt = base64ToBytes(envelope.salt);
      var iv = base64ToBytes(envelope.iv);
      var ciphertext = base64ToBytes(envelope.ciphertext);
      return deriveBackupKey(passphrase, salt, ["decrypt"]).then(function (key) {
        return crypto.subtle.decrypt({ name: "AES-GCM", iv: iv }, key, ciphertext);
      });
    }).then(function (plaintext) {
      var data = JSON.parse(new TextDecoder().decode(plaintext));
      return guestStore().replaceData(data).then(function () {
        Object.keys(data.preferences || {}).forEach(function (key) {
          if (key.indexOf(LS) === 0) localStorage.setItem(key, data.preferences[key]);
        });
      });
    }).then(function () {
      toast("Backup restored", "success");
      setTimeout(function () { window.location.reload(); }, 400);
    }).catch(function () {
      toast("Restore failed - check the file and passphrase", "error");
      $("#backup-file").value = "";
    });
  }

  /* ===================== 9. EVENT DELEGATION ===================== */

  function findAddr(id) {
    return ((state.summary && state.summary.addresses) || []).filter(function (a) { return String(a.id) === String(id); })[0];
  }
  function idFromEl(node) {
    var host = node.closest("[data-id]");
    return host ? host.getAttribute("data-id") : null;
  }

  function reopenDetails() { /* details render inline via renderCard/renderTable open flag */ }

  function toggleDetail(id) {
    var real = findAddr(id);
    var key = real ? real.id : id;
    if (state.openDetails.has(key)) state.openDetails.delete(key);
    else state.openDetails.add(key);
    renderAddresses();
  }

  function bindEvents() {
    // Safety-net global listeners FIRST, so Esc/close-menus always work even
    // if a later per-element binding below throws (e.g. a missing DOM id).
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("click", closeMenus);

    // Header buttons
    $("#btn-theme").addEventListener("click", function () {
      applyTheme(document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light");
    });
    $("#btn-privacy").addEventListener("click", function () { applyPrivacy(!state.prefs.privacy); });
    $("#btn-refresh").addEventListener("click", startRefreshAll);
    $("#btn-add-address").addEventListener("click", openAddModal);

    // Add-address modal
    $("#btn-close-add").addEventListener("click", closeAddModal);
    $("#btn-cancel-add").addEventListener("click", closeAddModal);
    $("#add-scrim").addEventListener("click", function (e) { if (e.target === this) closeAddModal(); });
    $("#add-form").addEventListener("submit", function (e) { e.preventDefault(); submitAddAddress(); });
    $("#add-address-input").addEventListener("input", function () { if (!$("#add-error").hidden) setAddError(""); });
    $("#btn-choose-csv").addEventListener("click", function () { $("#add-csv-file").click(); });
    $("#add-csv-file").addEventListener("change", function () { loadCsvFile(this.files && this.files[0]); });
    $("#add-address-input").addEventListener("dragover", function (e) {
      if (e.dataTransfer && e.dataTransfer.types && Array.prototype.indexOf.call(e.dataTransfer.types, "Files") >= 0) {
        e.preventDefault();
        this.classList.add("is-dragging");
      }
    });
    $("#add-address-input").addEventListener("dragleave", function () { this.classList.remove("is-dragging"); });
    $("#add-address-input").addEventListener("drop", function (e) {
      this.classList.remove("is-dragging");
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        e.preventDefault();
        loadCsvFile(e.dataTransfer.files[0]);
      }
    });

    // Overflow + chart-export menus (toggle)
    $("#btn-overflow").addEventListener("click", function (e) {
      e.stopPropagation(); var m = $("#overflow-menu"); var show = m.hidden; closeMenus();
      if (show) { m.hidden = false; this.setAttribute("aria-expanded", "true"); }
    });
    $("#btn-chart-export").addEventListener("click", function (e) {
      e.stopPropagation(); var m = $("#chart-export-menu"); var show = m.hidden; closeMenus(); if (show) m.hidden = false;
    });

    // Menu item actions (overflow + chart export)
    $("#overflow-menu").addEventListener("click", function (e) {
      var it = e.target.closest(".menu-item"); if (!it) return;
      var act = it.getAttribute("data-action"); closeMenus();
      if (act === "export-json") exportJson();
      else if (act === "export-csv") exportHoldingsCsv();
      else if (act === "copy-addresses") copyText(((state.summary && state.summary.addresses) || []).map(function (a) { return a.address; }).join("\n"));
      else if (act === "shortcuts") openShortcuts();
      else if (act === "settings") openDrawer();
      else if (act === "logout") logoutPrivate();
    });
    $("#chart-export-menu").addEventListener("click", function (e) {
      var it = e.target.closest(".menu-item"); if (!it) return;
      closeMenus();
      exportHistory(it.getAttribute("data-action") === "history-csv" ? "csv" : "json");
    });

    // Search
    $("#search").addEventListener("input", function () { state.filters.search = this.value; renderAddresses(); renderFilters(); });

    // Range pills
    $("#range-pills").addEventListener("click", function (e) {
      var b = e.target.closest(".pill"); if (!b) return;
      state.prefs.range = b.getAttribute("data-range"); store("range", state.prefs.range); renderChart();
    });

    // Allocation segments
    $("#alloc-seg").addEventListener("click", function (e) {
      var b = e.target.closest(".seg-btn"); if (!b) return;
      state.prefs.allocGroup = b.getAttribute("data-group"); store("allocGroup", state.prefs.allocGroup);
      allocAnim.from = []; renderAllocation();
    });
    // Legend cross-highlight
    $("#alloc-legend").addEventListener("mouseover", function (e) {
      var row = e.target.closest(".legend-row"); if (!row) return;
      var i = row.getAttribute("data-i");
      this.classList.add("has-hover");
      Array.prototype.forEach.call(this.children, function (c) { c.classList.toggle("is-hover", c.getAttribute("data-i") === i); });
      Array.prototype.forEach.call(document.querySelectorAll(".donut-arc"), function (arc) { arc.style.opacity = arc.getAttribute("data-i") === i ? "1" : ".25"; });
    });
    $("#alloc-legend").addEventListener("mouseleave", function () {
      this.classList.remove("has-hover");
      Array.prototype.forEach.call(document.querySelectorAll(".donut-arc"), function (arc) { arc.style.opacity = "1"; });
    });

    // Filter controls
    $("#venue-chips").addEventListener("click", function (e) {
      var c = e.target.closest(".chip"); if (!c) return; toggleSet(state.filters.venues, c.getAttribute("data-venue")); renderFilters(); renderAddresses();
    });
    $("#chain-chips").addEventListener("click", function (e) {
      var c = e.target.closest(".chip"); if (!c) return; toggleSet(state.filters.chains, c.getAttribute("data-chain")); renderFilters(); renderAddresses();
    });
    $("#sort-select").addEventListener("change", function () { state.prefs.sort = this.value; store("sort", this.value); renderAddresses(); });
    $("#status-select").addEventListener("change", function () { state.prefs.status = this.value; store("status", this.value); renderFilters(); renderAddresses(); });
    $("#dust-toggle").addEventListener("change", function () { state.prefs.dustOn = this.checked; store("dustOn", this.checked); renderFilters(); renderAddresses(); renderHoldings(); });
    $("#dust-threshold").addEventListener("change", function () { state.prefs.dust = num(this.value); store("dust", state.prefs.dust); renderFilters(); renderAddresses(); renderHoldings(); });
    $("#hide-empty").addEventListener("change", function () { state.prefs.hideEmpty = this.checked; store("hideEmpty", this.checked); renderFilters(); renderAddresses(); });
    $("#filter-card").addEventListener("click", function (e) {
      var b = e.target.closest(".view-btn"); if (b) { setView(b.getAttribute("data-view")); return; }
      if (e.target.id === "btn-clear-filters") clearFilters();
    });

    $("#scope-host").addEventListener("click", function (e) {
      var button = e.target.closest("[data-action]"); if (!button) return;
      var all = activeScopeAddresses().map(function (a) { return Number(a.id); });
      var action = button.getAttribute("data-action");
      if (action === "scope-all") state.prefs.scopeIds = all;
      else if (action === "scope-none") state.prefs.scopeIds = [];
      else if (action === "scope-toggle") {
        var id = Number(button.getAttribute("data-id"));
        var ids = selectedScopeIds().slice();
        var at = ids.indexOf(id);
        if (at >= 0) ids.splice(at, 1); else ids.push(id);
        state.prefs.scopeIds = ids;
      } else return;
      store("scopeIds", state.prefs.scopeIds);
      loadHistory().then(renderAll);
    });

    // Addresses host (delegated card + table actions)
    $("#addresses-host").addEventListener("click", onAddressClick);
    $("#excluded-host").addEventListener("click", function (e) {
      var t = e.target.closest("#excluded-toggle");
      if (t) { state.prefs.excludedOpen = !state.prefs.excludedOpen; store("excludedOpen", state.prefs.excludedOpen); renderExcluded(); return; }
      onAddressClick(e);
    });
    $("#addresses-host").addEventListener("keydown", onLabelKeydown);
    $("#excluded-host").addEventListener("keydown", onLabelKeydown);

    // Global holdings
    $("#holdings-toggle").addEventListener("click", function () {
      state.prefs.holdingsOpen = !state.prefs.holdingsOpen; store("holdingsOpen", state.prefs.holdingsOpen); renderHoldings();
    });
    $("#holdings-inner").addEventListener("click", function (e) {
      var r = e.target.closest(".holdings-row"); if (!r) return;
      var i = r.getAttribute("data-hrow");
      Array.prototype.forEach.call(document.querySelectorAll('.holdings-break[data-parent="' + i + '"]'), function (br) { br.hidden = !br.hidden; });
    });

    // Drawer / modal / scrims
    $("#btn-close-drawer").addEventListener("click", closeDrawer);
    $("#drawer-scrim").addEventListener("click", closeDrawer);
    $("#btn-close-shortcuts").addEventListener("click", closeShortcuts);
    $("#shortcuts-scrim").addEventListener("click", function (e) { if (e.target === this) closeShortcuts(); });
    bindSettings();

    // Table header sort (delegated at host)
    $("#addresses-host").addEventListener("click", function (e) {
      var th = e.target.closest("th[data-sort]"); if (!th) return;
      var key = th.getAttribute("data-sort");
      if (state.tableSort.key === key) state.tableSort.dir = state.tableSort.dir === "asc" ? "desc" : "asc";
      else { state.tableSort.key = key; state.tableSort.dir = "desc"; }
      renderAddresses();
    });

    // (global keydown + click listeners are attached at the top of bindEvents)

    // 30s relative-time tick
    setInterval(tickRelTimes, 30000);
  }

  function toggleSet(set, v) { if (set.has(v)) set.delete(v); else set.add(v); }

  function clearFilters() {
    state.filters.search = ""; state.filters.chains.clear(); state.filters.venues.clear();
    state.prefs.dustOn = false; state.prefs.hideEmpty = false; state.prefs.status = "all";
    store("dustOn", false); store("hideEmpty", false); store("status", "all");
    $("#search").value = "";
    renderFilters(); renderAddresses(); renderHoldings();
  }

  function onAddressClick(e) {
    var actionEl = e.target.closest("[data-action]");
    var id = idFromEl(e.target);
    if (!actionEl) {
      // table row click (whole row toggles)
      var tr = e.target.closest("tr[data-id]");
      if (tr && tr.getAttribute("data-action") === "toggle-detail") { toggleDetail(numOrStr(tr.getAttribute("data-id"))); }
      // detail tab clicks
      var tab = e.target.closest(".tab");
      if (tab && id != null) { state.activeTab[numOrStr(id)] = tab.getAttribute("data-tab"); renderAddresses(); }
      return;
    }
    e.stopPropagation();
    var act = actionEl.getAttribute("data-action");
    if (act === "open-add") { openAddModal(); return; }
    var a = id != null ? findAddr(id) : null;
    switch (act) {
      case "toggle-detail": toggleDetail(numOrStr(id)); break;
      case "copy-addr": copyText(actionEl.getAttribute("data-addr")); break;
      case "refresh-addr": refreshOne(a.id, actionEl.closest(".addr-card")); break;
      case "remove-addr": removeAddress(a.id); break;
      case "toggle-exclude": toggleExclude(a.id, a); break;
      case "edit-label": startEditLabel(actionEl, a); break;
      case "clear-filters": clearFilters(); break;
    }
  }

  function numOrStr(v) { var n = Number(v); return String(n) === String(v) ? n : v; }

  function startEditLabel(btn, a) {
    var card = btn.closest("[data-id]");
    var lbl = card.querySelector('[data-role="label"]');
    if (!lbl) return;
    lbl.setAttribute("contenteditable", "true");
    lbl.dataset.editing = "1";
    lbl.focus();
    document.execCommand && document.execCommand("selectAll", false, null);
  }

  function onLabelKeydown(e) {
    var lbl = e.target.closest('[data-role="label"][data-editing="1"]');
    if (!lbl) return;
    if (e.key === "Enter") { e.preventDefault(); commitLabel(lbl); }
    else if (e.key === "Escape") { e.preventDefault(); lbl.dataset.editing = ""; lbl.removeAttribute("contenteditable"); renderAddresses(); }
  }
  // commit on blur too
  document.addEventListener("blur", function (e) {
    var lbl = e.target && e.target.closest && e.target.closest('[data-role="label"][data-editing="1"]');
    if (lbl) commitLabel(lbl);
  }, true);

  function commitLabel(lbl) {
    if (lbl.dataset.committing) return;
    lbl.dataset.committing = "1";
    var id = idFromEl(lbl);
    var a = findAddr(id);
    var val = lbl.textContent.trim();
    lbl.removeAttribute("contenteditable"); lbl.dataset.editing = "";
    if (!a || val === (a.label || "")) { lbl.dataset.committing = ""; return; }
    patchAddress(a.id, { label: val }).then(function (res) {
      if (res.ok) { a.label = val; toast("Label updated", "success"); }
      else toast("Label update failed", "error");
      lbl.dataset.committing = "";
      renderAddresses();
    });
  }

  function onKeydown(e) {
    var tag = (e.target.tagName || "").toLowerCase();
    var typing = tag === "input" || tag === "textarea" || e.target.isContentEditable;
    // Ctrl/Cmd+K
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("#search").focus(); return; }
    // Ctrl+Shift+P
    if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "p") { e.preventDefault(); applyPrivacy(!state.prefs.privacy); return; }
    if (e.key === "Escape") {
      if (isAddOpen()) { closeAddModal(); return; }
      if (!$("#shortcuts-scrim").hidden) { closeShortcuts(); return; }
      if (!$("#settings-drawer").hidden) { closeDrawer(); return; }
      closeMenus();
      if (state.openDetails.size) { state.openDetails.clear(); renderAddresses(); }
      return;
    }
    if (typing) return;
    if (e.key === "r" || e.key === "R") { startRefreshAll(); }
    else if (e.key === "1") { setView("cards"); }
    else if (e.key === "2") { setView("table"); }
    else if (e.key === "?") { openShortcuts(); }
  }

  function bindSettings() {
    $("#set-theme").addEventListener("click", function (e) { var b = e.target.closest(".seg-btn"); if (b) { applyTheme(b.getAttribute("data-theme-val")); syncSettings(); } });
    $("#set-view").addEventListener("click", function (e) { var b = e.target.closest(".seg-btn"); if (b) { setView(b.getAttribute("data-view-val")); syncSettings(); } });
    $("#set-range").addEventListener("click", function (e) { var b = e.target.closest(".seg-btn"); if (b) { state.prefs.range = b.getAttribute("data-range-val"); store("range", state.prefs.range); renderChart(); syncSettings(); } });
    $("#set-dust-toggle").addEventListener("change", function () { state.prefs.dustOn = this.checked; store("dustOn", this.checked); renderFilters(); renderAddresses(); renderHoldings(); });
    $("#set-dust-threshold").addEventListener("change", function () { state.prefs.dust = num(this.value); store("dust", state.prefs.dust); renderFilters(); renderAddresses(); renderHoldings(); });
    $("#btn-backup-export").addEventListener("click", exportEncryptedBackup);
    $("#btn-backup-restore").addEventListener("click", function () { $("#backup-file").click(); });
    $("#backup-file").addEventListener("change", function () {
      restoreEncryptedBackup(this.files && this.files[0]);
    });
  }

  function tickRelTimes() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-role="reltime"]'), function (n) {
      n.textContent = relTime(n.getAttribute("data-ts") || null);
    });
  }

  /* ===================== 10. INIT ===================== */

  function initPrefs() {
    var theme = state.prefs.theme;
    if (!theme) theme = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";
    applyTheme(theme);
    applyPrivacy(state.prefs.privacy);
  }

  function showSkeleton() {
    $("#kpi-row").innerHTML = '<div class="card skel skel-card"></div><div class="card skel skel-card"></div><div class="card skel skel-card"></div><div class="card skel skel-card"></div>';
    $("#chart-body").innerHTML = '<div class="skel" style="height:300px;border-radius:12px"></div>';
    var skels = "";
    for (var i = 0; i < 6; i++) skels += '<div class="card skel skel-card"></div>';
    $("#addresses-host").innerHTML = '<div class="cards-grid">' + skels + '</div>';
  }

  // Force every overlay closed on load. Belt-and-suspenders against a stray
  // markup/CSS state leaving a modal or drawer covering the page.
  function closeAllOverlays() {
    ["#drawer-scrim", "#settings-drawer", "#shortcuts-scrim", "#add-scrim"].forEach(function (sel) {
      var el = $(sel); if (el) el.hidden = true;
    });
    var d = $("#settings-drawer"); if (d) d.setAttribute("aria-hidden", "true");
    try { closeMenus(); } catch (e) { /* ignore */ }
  }

  function init() {
    // Each phase isolated (reusing the section-render safe()): one failure
    // can't abort the rest of init or leave global Esc/close handlers unattached.
    safe(closeAllOverlays, "closeAllOverlays");
    safe(bindEvents, "bindEvents");
    safe(initPrefs, "initPrefs");
    safe(showSkeleton, "showSkeleton");
    loadRuntimeConfig().then(function () {
      return state.storageMode === "guest" ? guestStore().open() : null;
    }).then(function () {
      return Promise.all([loadSummary().catch(function () { return null; }), loadHistory()]);
    }).then(function () {
      if (!state.summary) {
        $("#kpi-row").innerHTML = '';
        $("#addresses-host").innerHTML = '<div class="empty-state"><div class="es-title">Could not load portfolio</div><div class="es-sub">The server did not respond. Try refreshing the page.</div></div>';
        $("#chart-body").innerHTML = '<div class="chart-empty">No data</div>';
        return;
      }
      renderAll();
      prefetchAddrHistories();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();



