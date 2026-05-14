// Dashboard client — Alpine stores for login + dashboard state.
// Polling is intentionally dumb and resilient: one timer, exponential backoff
// on network errors, auto-logout on 401.

const TOKEN_KEY = "forex_ea_token";
const USER_KEY  = "forex_ea_user";
const ROLE_KEY  = "forex_ea_role";
const POLL_MS   = 4000;

function _decimalsFor(symbol) {
  const s = (symbol || "").toUpperCase();
  if (s.includes("XAU") || s.includes("GOLD")) return 2;
  if (s.includes("OIL") || s.includes("WTI") || s.includes("BRENT")) return 2;
  if (s.endsWith("JPY") || s.endsWith("JPYM")) return 3;
  if (s.includes("BTC") || s.includes("ETH")) return 1;
  return 5;
}

async function api(path, { method = "GET", body, token } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(ROLE_KEY);
    location.reload();
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

document.addEventListener("alpine:init", () => {

  // ---------- THEME ----------
  // Pre-hydration script in index.html has already applied data-theme to <html>;
  // this store keeps Alpine in sync for the toggle UI.
  Alpine.store("theme", {
    isLight: document.documentElement.getAttribute("data-theme") === "light",
    toggle() {
      this.isLight = !this.isLight;
      const next = this.isLight ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("antigreed:theme", next); } catch (_) {}
    },
  });

  // ---------- LOGIN ----------
  Alpine.data("auth", () => ({
    username: "",
    password: "",
    error: "",
    busy: false,
    get token() { return localStorage.getItem(TOKEN_KEY); },

    async login() {
      this.busy = true;
      this.error = "";
      try {
        const data = await api("/auth/login", {
          method: "POST",
          body: { username: this.username, password: this.password },
        });
        localStorage.setItem(TOKEN_KEY, data.access_token);
        localStorage.setItem(USER_KEY, data.username);
        localStorage.setItem(ROLE_KEY, data.role);
        // Full reload to drop login, mount dashboard fresh.
        location.reload();
      } catch (e) {
        this.error = e.message.includes("401")
          ? "Invalid username or password."
          : e.message.includes("429")
          ? "Too many attempts. Try again shortly."
          : "Login failed.";
      } finally {
        this.busy = false;
      }
    },
  }));

  // ---------- DASHBOARD ----------
  Alpine.data("dashboard", () => ({
    token: localStorage.getItem(TOKEN_KEY),
    me: localStorage.getItem(USER_KEY) || "",
    role: localStorage.getItem(ROLE_KEY) || "admin",
    navOpen: false,
    // Collapsible-panel state persisted in localStorage so a user's
    // chosen layout sticks across refreshes. Default open on first run.
    strategiesExpanded: localStorage.getItem("antigreed:strategiesExpanded") !== "0",
    correlationsExpanded: localStorage.getItem("antigreed:correlationsExpanded") !== "0",
    // Active symbol tab for the Strategy-drift card. Null means
    // "first symbol in the response" — initialised on first render so
    // we don't have to wait for /drift to resolve.
    driftSymbol: null,
    togglePanel(name) {
      const k = name + "Expanded";
      this[k] = !this[k];
      try { localStorage.setItem("antigreed:" + k, this[k] ? "1" : "0"); } catch (_) {}
    },
    goToSection(id) {
      this.navOpen = false;
      // Defer the scroll a tick so the drawer's close transition has
      // already started — feels more natural than scrolling mid-slide.
      requestAnimationFrame(() => {
        const el = document.getElementById(id);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    },
    get isAdmin() { return this.role === "admin"; },
    // True once the operator has saved their own broker config — used
    // to gate the panels that show global bot data. Admin always sees
    // them; non-admin sees them after their broker form is saved.
    get hasBroker() {
      const c = this.brokerConfig;
      return !!(c && c.login && c.login > 0);
    },
    get canSeeDashboard() { return this.isAdmin || this.hasBroker; },
    version: "1.0.0",
    buildTag: "b13",
    myPicks: { signal: [], execute: [] },
    pollMs: POLL_MS,
    paletteOpen: false,

    status: null,
    account: null,
    strategies: [],
    trades: [],
    pending: [],
    brokerConfig: null,        // user's own saved broker_config (per-username)
    tradeTab: 'open',     // 'open' | 'pending' | 'closed'
    tradeDate: '',        // 'YYYY-MM-DD' or '' for any
    heartbeat: "—",
    blackout: null,  // { blackout, current_event, next_event, minutes_until_next, ... }
    blackoutSymbol: localStorage.getItem("antigreed:blackoutSymbol") || "EURUSD",
    regime: null,    // { trend, volatility, label, adx, atr_pct, ... }
    correlation: null, // { pairs: [{ symbol_a, symbol_b, value, computed_at, ... }], count }
    drift: null,       // { reports: [{ strategy, symbol, status, metrics, baseline, note }], count }
    fillStats: null,   // { symbols: [{ symbol, fill_count, avg_slippage_pips, avg_latency_ms, ... }], window_hours }
    allocator: null,   // { allocations: [{ strategy, symbol, role, weight, avg_r, win_rate, ... }], count }
    // Trade-explanation state — populated lazily on row click, not on every poll.
    explainOpenId: null,                     // currently expanded trade id
    explanations: {},                        // tradeId -> explanation payload
    explainStatus: {},                       // tradeId -> 'loading' | 'missing' | 'error' (success removes it)
    _chart: null,
    _backoff: 0,
    _prevTradeCount: 0,

    async boot() {
      if (!this.token) return;
      window.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
          e.preventDefault();
          this.paletteOpen = !this.paletteOpen;
        }
        if (this.paletteOpen) {
          if (e.key === "s") this.runCmd("start");
          if (e.key === "x") this.runCmd("stop");
          if (e.key === "r") this.runCmd("refresh");
        }
      });
      await this.tick();
      this.loop();
    },

    loop() {
      setTimeout(async () => {
        await this.tick();
        this.loop();
      }, this.pollMs + this._backoff);
    },

    async tick() {
      try {
        const [status, account, strategies, trades, blackout, regime, correlation, drift, fillStats, allocator, pending, brokerConfig, myPicks] = await Promise.all([
          api("/status",     { token: this.token }),
          api("/account",    { token: this.token }),
          api("/strategies", { token: this.token }),
          api("/trades?limit=50", { token: this.token }),
          api("/calendar/blackout/" + encodeURIComponent(this.blackoutSymbol),
              { token: this.token }).catch(() => null),
          api("/regime/" + encodeURIComponent(this.blackoutSymbol),
              { token: this.token }).catch(() => null),
          api("/correlation", { token: this.token }).catch(() => null),
          api("/drift", { token: this.token }).catch(() => null),
          api("/fills/stats?window_hours=24", { token: this.token }).catch(() => null),
          api("/allocator", { token: this.token }).catch(() => null),
          api("/orders/pending", { token: this.token }).catch(() => []),
          api("/broker/config", { token: this.token }).catch(() => null),
          api("/me/picks", { token: this.token }).catch(() => ({signal: [], execute: []})),
        ]);
        this.status = status;
        this.account = account;
        this.strategies = strategies;
        this.myPicks = myPicks;
        this.blackout = blackout;
        this.regime = regime;
        this.correlation = correlation;
        this.drift = drift;
        this.fillStats = fillStats;
        this.allocator = allocator;

        // Detect new winning trade -> confetti burst.
        const newCount = trades.length;
        if (this._prevTradeCount && newCount > this._prevTradeCount) {
          const fresh = trades.slice(0, newCount - this._prevTradeCount);
          if (fresh.some(t => (t.pnl || 0) > 0)) confetti();
        }
        this._prevTradeCount = newCount;
        this.trades = trades;
        this.pending = Array.isArray(pending) ? pending : [];
        this.brokerConfig = brokerConfig || null;

        this.heartbeat = status?.last_heartbeat
          ? `heartbeat · ${this.fmtTime(status.last_heartbeat)}`
          : "no heartbeat";
        this.renderChart();
        this._backoff = 0;
      } catch (e) {
        this.heartbeat = "offline · retrying";
        this._backoff = Math.min(30000, (this._backoff || 1000) * 2);
      }
    },

    kpis() {
      const acc = this.account || {};
      const pnl = acc.daily_pnl ?? 0;
      const wins = this.trades.filter(t => t.closed_at && (t.pnl || 0) > 0).length;
      const closed = this.trades.filter(t => t.closed_at).length;
      const wr = closed ? Math.round((wins / closed) * 100) : 0;
      const equityUp = (acc.equity ?? 0) >= (acc.balance ?? 0);
      return [
        {
          label: "Balance",
          value: this.fmtMoney(acc.balance ?? 0),
          sub: "starting equity",
          cls: "neutral",
          glow: "",
        },
        {
          label: "Equity",
          value: this.fmtMoney(acc.equity ?? 0),
          sub: "balance + open PnL",
          cls: equityUp ? "green" : "red",
          glow: equityUp ? "glow-green" : "glow-red",
        },
        {
          label: "Today PnL",
          value: this.fmtPnl(pnl),
          sub: "realized + floating",
          cls: pnl >= 0 ? "green" : "red",
          glow: pnl >= 0 ? "glow-green" : "glow-red",
        },
        {
          label: "Win rate",
          value: `${wr}%`,
          sub: `${wins} / ${closed} closed`,
          cls: wr >= 50 ? "green" : "neutral",
          glow: "",
        },
      ];
    },

    get sessionPnl() {
      return this.trades.reduce((s, t) => s + (t.pnl || 0), 0);
    },

    // ---- Correlations: magnitude → tier, sign → direction phrasing ----
    correlationTier(value) {
      const m = Math.abs(value || 0);
      if (m >= 0.6) return "tier-strong";
      if (m >= 0.3) return "tier-mod";
      return "tier-low";
    },
    correlationLabel(value) {
      const m = Math.abs(value || 0);
      const direction = value >= 0 ? "same direction" : "inverse";
      if (m >= 0.6) return `Strong · ${direction} — same trade twice if both same-side`;
      if (m >= 0.3) return `Moderate · ${direction} — bot will throttle pile-ons`;
      return `Low · ${direction} — independent enough to stack`;
    },
    get correlationCounts() {
      const pairs = this.correlation?.pairs || [];
      let strong = 0, moderate = 0, low = 0;
      for (const p of pairs) {
        const m = Math.abs(p.value || 0);
        if (m >= 0.6) strong++;
        else if (m >= 0.3) moderate++;
        else low++;
      }
      return { strong, moderate, low };
    },
    // Distinct symbols across the current drift response, in first-seen
    // order so the tab strip stays stable across polls.
    get driftSymbols() {
      const out = [];
      for (const r of (this.drift?.reports || [])) {
        if (!out.includes(r.symbol)) out.push(r.symbol);
      }
      return out;
    },
    get driftActiveSymbol() {
      const syms = this.driftSymbols;
      if (this.driftSymbol && syms.includes(this.driftSymbol)) return this.driftSymbol;
      return syms[0] || null;
    },
    get driftReportsFiltered() {
      const active = this.driftActiveSymbol;
      if (!active) return [];
      return (this.drift?.reports || []).filter(r => r.symbol === active);
    },

    // ---- Trade filtering (tab + date) ----
    _matchesDate(iso) {
      if (!this.tradeDate || !iso) return true;
      // iso is the full ISO timestamp (UTC); take the YYYY-MM-DD prefix in the
      // user's local timezone so a trade opened at 23:30 doesn't fall on the
      // wrong day.
      const d = new Date(iso);
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return `${y}-${m}-${day}` === this.tradeDate;
    },
    get tradeCounts() {
      const open   = this.trades.filter(t => !t.closed_at).length;
      const closed = this.trades.filter(t =>  t.closed_at).length;
      return { open, closed, pending: this.pending.length };
    },
    get filteredOpen() {
      return this.trades
        .filter(t => !t.closed_at && this._matchesDate(t.opened_at))
        .sort((a, b) => new Date(b.opened_at) - new Date(a.opened_at));
    },
    get filteredClosed() {
      return this.trades
        .filter(t =>  t.closed_at && this._matchesDate(t.opened_at))
        .sort((a, b) => new Date(b.closed_at) - new Date(a.closed_at));
    },
    get filteredPending() {
      return this.pending
        .filter(o => this._matchesDate(o.placed_at))
        .sort((a, b) => new Date(b.placed_at) - new Date(a.placed_at));
    },

    renderChart() {
      const closed = [...this.trades]
        .filter(t => t.closed_at)
        .sort((a, b) => new Date(a.closed_at) - new Date(b.closed_at));
      const labels = closed.map((_, i) => i + 1);
      // The /account endpoint reports the *current* balance — i.e. after every
      // closed trade has already settled. Walking forward from that anchor
      // ends the curve at current+totalPnl, which is the wrong direction.
      // Instead, derive the historical starting balance and walk forward from
      // there so the curve ends at today's balance.
      const total = closed.reduce((s, t) => s + (t.pnl || 0), 0);
      const starting = (this.account?.balance ?? 10000) - total;
      let running = starting;
      const series = closed.map(t => (running += (t.pnl || 0)));

      const ctx = document.getElementById("equity-chart");
      if (!ctx) return;
      const isLight = document.documentElement.getAttribute("data-theme") === "light";
      const lineColor = isLight ? "#059669" : "#22ee88";
      if (!this._chart) {
        const grad = ctx.getContext("2d").createLinearGradient(0, 0, 0, 256);
        grad.addColorStop(0, isLight ? "rgba(5,150,105,0.28)" : "rgba(34,238,136,0.32)");
        grad.addColorStop(1, "rgba(34,238,136,0.00)");
        this._chart = new Chart(ctx, {
          type: "line",
          data: {
            labels,
            datasets: [{
              data: series,
              borderColor: lineColor,
              backgroundColor: grad,
              borderWidth: 2.5,
              fill: true,
              tension: 0.28,
              pointRadius: 0,
              pointHoverRadius: 4,
              pointHoverBackgroundColor: lineColor,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: { legend: { display: false }, tooltip: {
              backgroundColor: "#0f1523", borderColor: "#1e293b", borderWidth: 1,
              titleColor: "#94a3b8", bodyColor: "#e2e8f0",
            }},
            scales: {
              x: { display: false },
              y: {
                grid: { color: "rgba(148,163,184,0.08)" },
                ticks: { color: "#64748b", font: { family: "JetBrains Mono", size: 10 } },
              },
            },
          },
          plugins: [{
            id: "baseline",
            beforeDraw(c) {
              const { ctx, chartArea, scales } = c;
              if (!chartArea) return;
              const y = scales.y.getPixelForValue(starting);
              ctx.save();
              ctx.strokeStyle = "rgba(148,163,184,0.3)";
              ctx.setLineDash([4, 4]);
              ctx.beginPath();
              ctx.moveTo(chartArea.left, y);
              ctx.lineTo(chartArea.right, y);
              ctx.stroke();
              ctx.restore();
            }
          }],
        });
      } else {
        this._chart.data.labels = labels;
        this._chart.data.datasets[0].data = series;
        this._chart.update("none");
      }
    },

    async toggleStrategy(name) {
      if (!this.isAdmin) return;
      try {
        const updated = await api(`/strategies/${encodeURIComponent(name)}/toggle`,
                                  { method: "POST", token: this.token });
        const i = this.strategies.findIndex(s => s.name === updated.name);
        if (i >= 0) this.strategies[i] = updated;
      } catch (_) { /* swallow, next tick will reconcile */ }
    },

    async setStrategyMode(name, mode) {
      if (!this.isAdmin) return;
      try {
        const updated = await api(`/strategies/${encodeURIComponent(name)}/mode`,
          { method: "POST", token: this.token, body: { mode } });
        const i = this.strategies.findIndex(s => s.name === updated.name);
        if (i >= 0) this.strategies[i] = updated;
      } catch (_) { /* next tick will reconcile */ }
    },

    async setStrategyCopyable(name, value) {
      if (!this.isAdmin) return;
      try {
        const updated = await api(
          `/strategies/${encodeURIComponent(name)}/user-copyable`,
          { method: "POST", token: this.token, body: { user_copyable: !!value } },
        );
        const i = this.strategies.findIndex(s => s.name === updated.name);
        if (i >= 0) this.strategies[i] = updated;
      } catch (_) { /* next tick will reconcile */ }
    },

    get executeStrategies() {
      return this.strategies.filter(s => (s.mode || 'execute') === 'execute');
    },
    get signalStrategies() {
      return this.strategies.filter(s => s.mode === 'signal');
    },
    // Non-admin view: strategies the operator personally picked at
    // signup. The 3 signal picks and the 2 execute picks, deduped if
    // the same strategy is in both lists. Admin-side user_copyable is
    // also enforced — if admin flipped a pick to admin-only after the
    // user picked it, the row is dropped so the UI doesn't pretend the
    // user still receives that strategy.
    get userCopyableStrategies() {
      const picked = new Set([
        ...(this.myPicks?.signal || []),
        ...(this.myPicks?.execute || []),
      ]);
      return this.strategies.filter(
        s => picked.has(s.name) && s.user_copyable !== false,
      );
    },
    // Mode lookup so the read-only row can show "signal alerts" /
    // "auto-copy" based on which kind the user picked this strategy as.
    pickKindFor(name) {
      const inSignal  = (this.myPicks?.signal  || []).includes(name);
      const inExecute = (this.myPicks?.execute || []).includes(name);
      if (inExecute && inSignal) return "both";
      if (inExecute) return "execute";
      if (inSignal)  return "signal";
      return null;
    },

    async toggleBot() {
      if (!this.isAdmin) return;
      const path = this.status?.running ? "/bot/stop" : "/bot/start";
      try {
        await api(path, { method: "POST", token: this.token });
      } catch (err) {
        // api() throws "<status>: <body>" — surface it so the user can see
        // *why* the click didn't take. Most common cause: 2FA enrolled but
        // the dashboard didn't prompt for a code.
        const msg = (err && err.message) || "request failed";
        try { window.alert(`Start/Stop failed — ${msg}`); } catch (_) {}
      } finally {
        this.tick();
      }
    },

    /**
     * Expand/collapse the "why this trade?" panel under a trade row.
     * Lazy-fetches the explanation the first time the row is opened.
     */
    async toggleExplain(tradeId) {
      if (this.explainOpenId === tradeId) {
        this.explainOpenId = null;
        return;
      }
      this.explainOpenId = tradeId;
      // Already cached or in flight — nothing to do.
      if (this.explanations[tradeId] || this.explainStatus[tradeId] === "loading") return;
      this.explainStatus = { ...this.explainStatus, [tradeId]: "loading" };
      try {
        const exp = await api("/trades/" + tradeId + "/explain", { token: this.token });
        this.explanations = { ...this.explanations, [tradeId]: exp };
        const next = { ...this.explainStatus }; delete next[tradeId];
        this.explainStatus = next;
      } catch (err) {
        // api() throws "404: ..." — pre-feature trades surface as missing,
        // anything else is a real network/server problem.
        const isMissing = err && /^404\b/.test(err.message || "");
        this.explainStatus = { ...this.explainStatus, [tradeId]: isMissing ? "missing" : "error" };
      }
    },

    async runCmd(cmd) {
      this.paletteOpen = false;
      if ((cmd === "start" || cmd === "stop") && !this.isAdmin) return;
      if (cmd === "start")   await api("/bot/start", { method: "POST", token: this.token });
      if (cmd === "stop")    await api("/bot/stop",  { method: "POST", token: this.token });
      if (cmd === "refresh") await this.tick();
      if (cmd === "start" || cmd === "stop") this.tick();
    },

    logout() {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(ROLE_KEY);
      location.reload();
    },

    fmtMoney(n) {
      const ccy = this.account?.currency || "USD";
      try {
        return new Intl.NumberFormat(undefined, {
          style: "currency", currency: ccy,
          minimumFractionDigits: 2, maximumFractionDigits: 2,
        }).format(n ?? 0);
      } catch (_) {
        // Unknown ISO code from broker — fall back to amount + suffix.
        return (n ?? 0).toLocaleString(undefined,
          { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " " + ccy;
      }
    },
    fmtPnl(n) {
      const sign = n > 0 ? "+" : n < 0 ? "−" : "";
      return sign + this.fmtMoney(Math.abs(n ?? 0));
    },
    fmtTime(iso) {
      if (!iso) return "—";
      const d = new Date(iso);
      const now = new Date();
      const same = d.toDateString() === now.toDateString();
      return same
        ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : d.toLocaleDateString();
    },

    /**
     * Render the strategy's signal chart as inline SVG: candlesticks +
     * EMA/BB overlays + entry/SL/TP lines + a marker on the signal bar.
     * Returns an HTML string Alpine drops in via x-html — keeps the
     * dashboard dependency-free (no chart library to vendor).
     */
    renderStrategyChart(exp, symbol) {
      const bars = exp?.bars || [];
      if (bars.length < 2) return "";
      const subplots = exp?.subplots || [];
      const subH = subplots.length ? 70 : 0;
      const subGap = subplots.length ? 8 : 0;
      const W = 760, H = 220 + subplots.length * (subH + subGap);
      const padL = 6, padR = 6, padT = 6, padB = 14;
      const priceH = 220 - padT - padB;  // candle pane
      const innerW = W - padL - padR;
      const innerH = priceH;
      const n = bars.length;
      const step = innerW / n;
      const candleW = Math.max(1, step * 0.6);

      // Pull every value we need to scale the y-axis: bar highs/lows,
      // every overlay value, and the entry/SL/TP levels.
      const allY = [];
      for (const b of bars) {
        if (b.h != null) allY.push(b.h);
        if (b.l != null) allY.push(b.l);
      }
      const overlays = exp?.overlays || [];
      for (const o of overlays) {
        if (o.kind === "line" && o.values) {
          for (const v of o.values) if (v != null) allY.push(v);
        } else if (o.kind === "band") {
          for (const v of (o.upper || [])) if (v != null) allY.push(v);
          for (const v of (o.lower || [])) if (v != null) allY.push(v);
        }
      }
      const entry = exp.signal_price, sl = exp.signal_stop, tp = exp.signal_target;
      [entry, sl, tp].forEach(v => { if (v != null) allY.push(v); });
      if (!allY.length) return "";
      const yMin = Math.min(...allY), yMax = Math.max(...allY);
      const yRange = (yMax - yMin) || 1;
      const pad = yRange * 0.04;
      const yTop = yMax + pad, yBottom = yMin - pad;
      const yScale = v => padT + (yTop - v) / (yTop - yBottom) * innerH;
      const xScale = i => padL + i * step + step / 2;

      const isBuy = exp?.side === "BUY";
      const sigColor = isBuy ? "#22ee88" : "#ff3355";

      // Candles
      let candlesSvg = "";
      bars.forEach((b, i) => {
        if (b.o == null || b.c == null || b.h == null || b.l == null) return;
        const up = b.c >= b.o;
        const color = up ? "rgba(34,238,136,0.85)" : "rgba(255,51,85,0.85)";
        const x = xScale(i);
        const wickTop = yScale(b.h), wickBot = yScale(b.l);
        const bodyHi = yScale(Math.max(b.o, b.c));
        const bodyLo = yScale(Math.min(b.o, b.c));
        const bodyH = Math.max(1, bodyLo - bodyHi);
        candlesSvg += `<line x1="${x}" y1="${wickTop}" x2="${x}" y2="${wickBot}" stroke="${color}" stroke-width="1"/>`;
        candlesSvg += `<rect x="${x - candleW / 2}" y="${bodyHi}" width="${candleW}" height="${bodyH}" fill="${color}" />`;
      });

      // Overlays
      const polyline = (vals, color, dash = "") => {
        const pts = [];
        vals.forEach((v, i) => {
          if (v != null && i < n) pts.push(`${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`);
        });
        if (pts.length < 2) return "";
        return `<polyline fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-dasharray="${dash}" points="${pts.join(" ")}"/>`;
      };
      let overlaysSvg = "";
      const legend = [];
      for (const o of overlays) {
        if (o.kind === "line") {
          overlaysSvg += polyline(o.values || [], o.color || "#8fa0aa");
          legend.push(`<span><span class="swatch" style="background:${o.color}"></span>${o.name}</span>`);
        } else if (o.kind === "band") {
          // Filled BB envelope: upper polyline + lower polyline reversed.
          const up = (o.upper || []);
          const dn = (o.lower || []);
          const pts = [];
          up.forEach((v, i) => { if (v != null) pts.push(`${xScale(i).toFixed(1)},${yScale(v).toFixed(1)}`); });
          for (let i = dn.length - 1; i >= 0; i--) {
            if (dn[i] != null) pts.push(`${xScale(i).toFixed(1)},${yScale(dn[i]).toFixed(1)}`);
          }
          if (pts.length >= 4) {
            overlaysSvg += `<polygon points="${pts.join(" ")}" fill="${o.color}" fill-opacity="0.06" stroke="none"/>`;
          }
          overlaysSvg += polyline(up, o.color || "#8fa0aa", "3 3");
          overlaysSvg += polyline(dn, o.color || "#8fa0aa", "3 3");
          legend.push(`<span><span class="swatch" style="background:${o.color}"></span>${o.name}</span>`);
        }
      }

      // Entry / SL / TP horizontal lines and labels
      const horiz = (y, color, label) => {
        if (y == null) return "";
        const py = yScale(y);
        return `<line x1="${padL}" y1="${py}" x2="${W - padR}" y2="${py}" stroke="${color}" stroke-width="1" stroke-dasharray="4 4" opacity="0.85"/>` +
          `<rect x="${W - padR - 60}" y="${py - 7}" width="60" height="13" rx="3" fill="${color}" opacity="0.85"/>` +
          `<text x="${W - padR - 4}" y="${py + 3}" text-anchor="end" font-size="10" font-family="monospace" fill="#000" font-weight="700">${label}</text>`;
      };
      const fmt = v => v == null ? "" : v.toFixed(_decimalsFor(symbol));
      let levelsSvg = "";
      levelsSvg += horiz(entry, sigColor, "ENTRY " + fmt(entry));
      levelsSvg += horiz(sl, "#ff3355", "SL " + fmt(sl));
      levelsSvg += horiz(tp, "#22ee88", "TP " + fmt(tp));

      // Signal arrow on the most recent bar
      const sigX = xScale(n - 1);
      const sigY = yScale(entry);
      const arrow = isBuy
        ? `<path d="M ${sigX} ${sigY + 14} l -6 8 l 12 0 z" fill="${sigColor}"/>`
        : `<path d="M ${sigX} ${sigY - 14} l -6 -8 l 12 0 z" fill="${sigColor}"/>`;

      const legendHtml = legend.length
        ? `<div class="strategy-chart-legend">${legend.join("")}` +
          `<span><span class="swatch" style="background:${sigColor}"></span>Entry</span>` +
          `<span><span class="swatch" style="background:#ff3355"></span>SL</span>` +
          `<span><span class="swatch" style="background:#22ee88"></span>TP</span></div>`
        : "";

      // Subplot panes (RSI, MACD, Stochastic, ADX) below the candles.
      let subplotsSvg = "";
      const paneTop = priceH + padT + padB;
      subplots.forEach((sp, idx) => {
        const top = paneTop + idx * (subH + subGap) + subGap;
        const bottom = top + subH;
        // Frame
        subplotsSvg += `<line x1="${padL}" y1="${top}" x2="${W - padR}" y2="${top}" stroke="rgba(143,160,170,0.2)" stroke-width="1"/>`;
        const yMin = sp.y_min != null ? sp.y_min : 0;
        const yMax = sp.y_max != null ? sp.y_max : 100;
        const yRange = (yMax - yMin) || 1;
        const sy = v => top + (yMax - v) / yRange * subH;
        // Guides (OB/OS/threshold lines)
        for (const g of (sp.guides || [])) {
          const gy = sy(g.y);
          subplotsSvg += `<line x1="${padL}" y1="${gy}" x2="${W - padR}" y2="${gy}" stroke="${g.color || '#8fa0aa'}" stroke-width="1" stroke-dasharray="3 3" opacity="0.5"/>`;
          subplotsSvg += `<text x="${W - padR - 4}" y="${gy - 3}" text-anchor="end" font-size="9" fill="${g.color || '#8fa0aa'}" font-family="monospace">${g.label} ${g.y}</text>`;
        }
        // Title
        subplotsSvg += `<text x="${padL + 4}" y="${top + 11}" font-size="9" fill="#8fa0aa" font-family="monospace" letter-spacing="1.5">${sp.name}</text>`;
        const subPolyline = (vals, color, dash = "") => {
          const pts = [];
          (vals || []).forEach((v, i) => {
            if (v != null && i < n) pts.push(`${xScale(i).toFixed(1)},${sy(v).toFixed(1)}`);
          });
          if (pts.length < 2) return "";
          return `<polyline fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-dasharray="${dash}" points="${pts.join(" ")}"/>`;
        };
        if (sp.kind === "line") {
          subplotsSvg += subPolyline(sp.values, sp.color || "#22ee88");
        } else if (sp.kind === "double_line") {
          subplotsSvg += subPolyline(sp.primary, sp.primary_color || "#22ee88");
          subplotsSvg += subPolyline(sp.secondary, sp.secondary_color || "#ffc73a");
          if (sp.tertiary) subplotsSvg += subPolyline(sp.tertiary, sp.tertiary_color || "#ff3355");
        } else if (sp.kind === "macd") {
          // Histogram bars then macd + signal lines.
          const zeroY = sy(0);
          (sp.histogram || []).forEach((v, i) => {
            if (v == null) return;
            const x = xScale(i) - candleW / 2;
            const y0 = sy(0), y1 = sy(v);
            const top2 = Math.min(y0, y1), h2 = Math.abs(y1 - y0);
            const hcolor = v >= 0 ? "rgba(34,238,136,0.5)" : "rgba(255,51,85,0.5)";
            subplotsSvg += `<rect x="${x}" y="${top2}" width="${candleW}" height="${Math.max(1, h2)}" fill="${hcolor}"/>`;
          });
          subplotsSvg += `<line x1="${padL}" y1="${zeroY}" x2="${W - padR}" y2="${zeroY}" stroke="rgba(143,160,170,0.4)" stroke-width="1"/>`;
          subplotsSvg += subPolyline(sp.macd, sp.color || "#22ee88");
          subplotsSvg += subPolyline(sp.signal, sp.signal_color || "#ff3355");
        }
      });

      return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">` +
        overlaysSvg + candlesSvg + levelsSvg + arrow + subplotsSvg +
        `</svg>${legendHtml}`;
    },

    formatIndicator(value) {
      if (value === null || value === undefined) return "—";
      if (typeof value === "boolean") return value ? "yes" : "no";
      if (typeof value === "number") {
        const abs = Math.abs(value);
        if (Number.isInteger(value) && abs < 1000) return String(value);
        if (abs < 10) return value.toFixed(2);
        if (abs < 100) return value.toFixed(1);
        return value.toFixed(4);
      }
      return String(value);
    },

    // --- Calendar blackout pill ---
    // Returns { text, tone } where tone is used to colorize the pill:
    //   'muted' = no upcoming event / calendar disabled,
    //   'ok'    = event > 30 min out,
    //   'warn'  = within 30 min,
    //   'danger'= actively inside a blackout window.
    get blackoutPill() {
      if (!this.blackout || this.blackout.enabled === false) {
        return { text: "Calendar off", tone: "muted" };
      }
      if (this.blackout.blackout) {
        const e = this.blackout.current_event;
        return { text: `Blackout · ${e?.currency ?? ""} · ${e?.title ?? ""}`.trim(), tone: "danger" };
      }
      const nxt = this.blackout.next_event;
      if (!nxt) return { text: `${this.blackoutSymbol} · clear`, tone: "muted" };
      const mins = this.blackout.minutes_until_next ?? 0;
      const label = `${nxt.title} · ${nxt.currency}`;
      return {
        text: `Next: ${label} · ${this.fmtDuration(mins)}`,
        tone: mins <= 30 ? "warn" : "ok",
      };
    },
    fmtDuration(totalMinutes) {
      const m = Math.max(0, Math.round(totalMinutes));
      if (m < 60) return `${m}m`;
      const h = Math.floor(m / 60);
      const rm = m % 60;
      return rm ? `${h}h ${rm}m` : `${h}h`;
    },

    // --- Regime pill ---
    // Tone:
    //   'muted'  = unknown (bot not running or not enough bars),
    //   'ok'     = trend_up (favorable for trend strategies),
    //   'warn'   = range (mean-rev territory),
    //   'danger' = trend_down.
    get regimePill() {
      const r = this.regime;
      if (!r || r.trend === "unknown") {
        return {
          text: `${this.blackoutSymbol} · regime ?`,
          tone: "muted",
          title: "Waiting for the bot to classify this symbol.",
        };
      }
      const adx = r.adx != null ? r.adx.toFixed(0) : "—";
      const vol = r.volatility && r.volatility !== "unknown" ? ` · ${r.volatility}` : "";
      const labels = {
        trend_up:   { text: `Trend ↑${vol} · ADX ${adx}`, tone: "ok" },
        trend_down: { text: `Trend ↓${vol} · ADX ${adx}`, tone: "danger" },
        range:      { text: `Range${vol} · ADX ${adx}`,   tone: "warn" },
      };
      const pill = labels[r.trend] ?? { text: r.label, tone: "muted" };
      return { ...pill, title: `Trend=${r.trend}, vol=${r.volatility}, ADX=${adx}` };
    },
  }));

  // ---------- BROKER ----------
  Alpine.data("broker", () => ({
    token: localStorage.getItem(TOKEN_KEY),
    role: localStorage.getItem(ROLE_KEY) || "admin",
    get isAdmin() { return this.role === "admin"; },
    presets: [],
    savedConfig: null,
    status: null,
    testResult: null,
    form: { broker: "exness", login: 0, password: "", server: "", mt5_path: "" },
    testing: false,
    saving: false,
    message: "",
    messageTone: "text-slate-400",
    _statusTimer: null,

    async load() {
      // No token → don't hit protected endpoints. Otherwise api() sees 401
      // and calls location.reload(), which re-mounts this component and
      // re-fires load() — an infinite reload loop.
      if (!this.token) return;
      try {
        const [presets, saved, status] = await Promise.all([
          api("/brokers",        { token: this.token }),
          api("/broker/config",  { token: this.token }),
          api("/broker/status",  { token: this.token }),
        ]);
        this.presets = presets;
        this.savedConfig = saved;
        this.status = status;
        if (saved) {
          this.form.broker   = saved.broker;
          this.form.login    = saved.login;
          this.form.server   = saved.server;
          this.form.mt5_path = saved.mt5_path || "";
        }
      } catch (e) {
        this.flash("Could not load broker panel.", false);
      }
      // Status polls on its own beat — faster than the main dashboard.
      this._statusTimer = setInterval(() => this.refreshStatus(), 6000);
    },

    async refreshStatus() {
      if (!this.token) return;
      try { this.status = await api("/broker/status", { token: this.token }); }
      catch (_) { /* offline: next tick */ }
    },

    activePreset() {
      return this.presets.find(p => p.id === this.form.broker) || null;
    },

    onBrokerChange() {
      const p = this.activePreset();
      if (p && p.servers.length && !this.form.server) this.form.server = p.servers[0];
    },

    canSubmit() {
      if (!this.form.broker) return false;
      if (!this.form.login || this.form.login <= 0) return false;
      if (!this.form.server) return false;
      // Password required only when there isn't one saved.
      if (!this.savedConfig?.password_set && !this.form.password) return false;
      return true;
    },

    busy() { return this.testing || this.saving; },
    disableSubmit() { return this.busy() || !this.canSubmit(); },

    get statusLabel() {
      if (!this.status) return "unknown";
      if (this.status.connected) return "connected";
      return this.status.last_error ? "disconnected" : "idle";
    },

    get staleLabel() {
      const s = this.status?.stale_s;
      if (s == null) return "";
      if (s < 60)  return `· ${Math.round(s)}s ago`;
      if (s < 3600) return `· ${Math.round(s / 60)}m ago`;
      return `· ${Math.round(s / 3600)}h ago`;
    },

    accountFields() {
      const a = this.status?.account_info || {};
      return [
        { label: "Server",   value: this.status?.server || "—" },
        { label: "Login",    value: this.status?.login || "—" },
        { label: "Balance",  value: (a.balance != null) ? `${a.balance.toFixed(2)} ${a.currency || ""}` : "—" },
        { label: "Leverage", value: a.leverage != null ? `1:${a.leverage}` : "—" },
      ];
    },

    // Use the saved (but still encrypted on disk) password if the user didn't
    // re-enter one. The backend doesn't accept empty password, so we ask the
    // user to re-enter when changing creds — but Test is lenient when only
    // testing the existing saved config, so we'll block that case in canSubmit.
    _payload() {
      return {
        broker: this.form.broker,
        login: Number(this.form.login),
        password: this.form.password,
        server: this.form.server,
        mt5_path: this.form.mt5_path || "",
      };
    },

    async test() {
      if (!this.canSubmit()) return;
      this.testing = true; this.testResult = null; this.message = "";
      try {
        const r = await api("/broker/test", {
          method: "POST", token: this.token, body: this._payload(),
        });
        this.testResult = r;
        this.flash(r.ok ? "Connection OK." : "Connection failed.", r.ok);
      } catch (e) {
        this.testResult = { ok: false, error: e.message };
        this.flash("Test request failed.", false);
      } finally {
        this.testing = false;
      }
    },

    async save() {
      if (!this.canSubmit()) return;
      this.saving = true; this.message = "";
      try {
        const saved = await api("/broker/config", {
          method: "PUT", token: this.token, body: this._payload(),
        });
        this.savedConfig = saved;
        this.form.password = "";
        this.flash("Saved. Restart the bot to pick up new creds.", true);
      } catch (e) {
        this.flash(`Save failed: ${e.message}`, false);
      } finally {
        this.saving = false;
      }
    },

    async clear() {
      if (!confirm("Remove saved broker credentials?")) return;
      try {
        await api("/broker/config", { method: "DELETE", token: this.token });
        this.savedConfig = null;
        this.form.password = "";
        this.flash("Credentials removed.", true);
      } catch (e) {
        this.flash(`Remove failed: ${e.message}`, false);
      }
    },

    flash(msg, ok) {
      this.message = msg;
      this.messageTone = ok ? "text-win" : "text-loss";
      setTimeout(() => { this.message = ""; }, 4000);
    },
  }));

  // ---------- USERS (admin only) ----------
  // ---------- COPY-TRADER EA SETUP ----------
  Alpine.data("eaSetup", () => ({
    token: localStorage.getItem(TOKEN_KEY),
    cfg: { api_base_url: "", api_key: "", ad_id: "" },
    show_key: false,
    busy: false,
    msg: "",
    msgOk: true,
    expanded: localStorage.getItem("ea_panel_expanded") === "1",
    loaded: false,
    toggle() {
      this.expanded = !this.expanded;
      localStorage.setItem("ea_panel_expanded", this.expanded ? "1" : "0");
      if (this.expanded && !this.loaded) this.load();
    },
    async load() {
      if (!this.token) return;
      try {
        this.cfg = await api("/me/ea-config", { token: this.token });
        this.loaded = true;
      } catch (e) {
        this.msg = `Couldn't load EA config: ${e.message || e}`;
        this.msgOk = false;
      }
    },
    async rotate() {
      if (!confirm("Rotate your EA API key? Your installed EA will stop working until you paste the new key.")) return;
      this.busy = true;
      try {
        this.cfg = await api("/me/ea-config/rotate", { method: "POST", token: this.token });
        this.msg = "New key generated. Paste it into your EA's Inputs tab.";
        this.msgOk = true;
      } catch (e) {
        this.msg = `Rotate failed: ${e.message || e}`;
        this.msgOk = false;
      } finally {
        this.busy = false;
        setTimeout(() => { this.msg = ""; }, 6000);
      }
    },
    async copy(value, label) {
      if (!value) return;
      try {
        await navigator.clipboard.writeText(value);
        this.msg = `${label} copied to clipboard.`;
        this.msgOk = true;
      } catch (e) {
        this.msg = `Copy failed — select & copy the field manually.`;
        this.msgOk = false;
      }
      setTimeout(() => { this.msg = ""; }, 3000);
    },
  }));

  Alpine.data("users", () => ({
    token: localStorage.getItem(TOKEN_KEY),
    role: localStorage.getItem(ROLE_KEY) || "admin",
    me: localStorage.getItem(USER_KEY) || "",
    list: [],
    pool: { unclaimed: [], size: 0 },
    form: { ad_id: "", email: "", duration: "1m" },
    reset: { username: "", password: "" },
    extendChoice: {},          // ad_id -> selected duration code in the dropdown
    requests: [],              // pending Telegram signup requests
    approveDuration: {},       // request_id -> duration selected by admin (defaults to user's pick)
    approveDelivery: {},       // request_id -> 'telegram' | 'email' | 'both'
    lastSetupUrl: "",
    busy: false,
    message: "",
    messageTone: "text-slate-400",

    get isAdmin() { return this.role === "admin"; },

    async load() {
      if (!this.token || !this.isAdmin) return;
      try {
        const [list, pool, requests] = await Promise.all([
          api("/users", { token: this.token }),
          api("/users/pool", { token: this.token }),
          api("/subscription-requests", { token: this.token }).catch(() => []),
        ]);
        this.list = list;
        this.pool = pool;
        this.requests = requests || [];
        // Initialise the approval-duration dropdown with each user's pick,
        // and default delivery to telegram (avoids the email path entirely
        // for admins who don't want to deal with mailer config).
        const dur = {}, delivery = {};
        for (const r of this.requests) {
          dur[r.id] = r.duration;
          if (!this.approveDelivery[r.id]) delivery[r.id] = "telegram";
        }
        this.approveDuration = { ...this.approveDuration, ...dur };
        this.approveDelivery = { ...this.approveDelivery, ...delivery };
      } catch (e) {
        this.flash(`Could not load operators: ${e.message}`, false);
      }
    },

    async approveRequest(r) {
      const dur = this.approveDuration[r.id] || r.duration;
      const who = r.phone_number || r.telegram_first_name || r.email || `chat ${r.telegram_chat_id}`;
      if (!confirm(`Approve ${who} for ${dur}? Setup link will be sent via Telegram.`)) return;
      this.busy = true;
      try {
        const resp = await api(`/subscription-requests/${r.id}/approve`, {
          method: "POST", token: this.token,
          body: { duration: dur, delivery: "telegram" },
        });
        this.flash(`Approved · setup link sent on Telegram.`, true);
        if (resp.setup_url) this.lastSetupUrl = resp.setup_url;
      } catch (e) {
        this.flash(`Approve failed: ${this._humanize(e)}`, false);
      } finally {
        // Always refresh — stale rows from a previously-succeeded approve
        // were causing "already approved" errors on retry.
        await this.load();
        this.busy = false;
      }
    },

    async rejectRequest(r) {
      const reason = prompt(
        `Reject the request from ${r.email}?\nGive a short reason — it'll be sent to them on Telegram:`,
        "Sorry, we can't accept new operators right now.",
      );
      if (!reason) return;
      this.busy = true;
      try {
        await api(`/subscription-requests/${r.id}/reject`, {
          method: "POST", token: this.token, body: { reason },
        });
        this.flash(`Request rejected. User notified on Telegram.`, true);
      } catch (e) {
        this.flash(`Reject failed: ${this._humanize(e)}`, false);
      } finally {
        await this.load();
        this.busy = false;
      }
    },

    async refillPool() {
      this.busy = true;
      try {
        this.pool = await api("/users/pool/refill?target=100",
                              { method: "POST", token: this.token });
        this.flash("Pool refilled to 100.", true);
      } catch (e) {
        this.flash(`Refill failed: ${this._humanize(e)}`, false);
      } finally {
        this.busy = false;
      }
    },

    async assign() {
      if (!this.form.ad_id || !this.form.email) return;
      this.busy = true;
      this.lastSetupUrl = "";
      try {
        const resp = await api("/users/assign", {
          method: "POST", token: this.token,
          body: {
            ad_id: this.form.ad_id,
            email: this.form.email.trim(),
            duration: this.form.duration || "1m",
          },
        });
        this.form = { ad_id: "", email: "", duration: "1m" };
        await this.load();
        if (resp.setup_url) {
          this.lastSetupUrl = resp.setup_url;
          this.flash(`Assigned ${resp.ad_id} — SMTP off, copy the link above.`, true);
        } else {
          this.flash(`Setup link emailed to ${resp.email}.`, true);
        }
      } catch (e) {
        this.flash(`Assign failed: ${this._humanize(e)}`, false);
      } finally {
        this.busy = false;
      }
    },

    async extend(u) {
      const code = this.extendChoice[u.username];
      if (!code) return;
      this.busy = true;
      try {
        await api(`/users/${encodeURIComponent(u.username)}/extend`,
          { method: "POST", token: this.token, body: { duration: code } });
        this.extendChoice = { ...this.extendChoice, [u.username]: "" };
        await this.load();
        this.flash(`${u.username} extended by ${code}.`, true);
      } catch (e) {
        this.flash(`Extend failed: ${this._humanize(e)}`, false);
      } finally {
        this.busy = false;
      }
    },

    statusLabel(u) {
      if (!u.password_set) return "pending";
      if (u.expired) return "expired";
      return "active";
    },
    statusColor(u) {
      if (u.expired) return "var(--red)";
      if (!u.password_set) return "var(--amber)";
      return "var(--green)";
    },
    formatSubscription(u) {
      if (!u.expires_at) return "—";
      const exp = new Date(u.expires_at);
      const now = new Date();
      const diffMs = exp.getTime() - now.getTime();
      const diffDays = Math.round(diffMs / (1000 * 60 * 60 * 24));
      const diffHours = Math.round(diffMs / (1000 * 60 * 60));
      const dateStr = exp.toLocaleDateString();
      if (diffMs < 0) {
        const daysAgo = Math.abs(diffDays);
        return daysAgo > 0
          ? `expired ${daysAgo}d ago · ${dateStr}`
          : `expired ${Math.abs(diffHours)}h ago`;
      }
      if (diffHours < 24) return `${diffHours}h left · ${dateStr}`;
      return `${diffDays}d left · ${dateStr}`;
    },
    subscriptionWarning(u) {
      // Amber when within 48 hours of expiry.
      if (!u.expires_at || u.expired) return false;
      const exp = new Date(u.expires_at);
      return (exp.getTime() - Date.now()) < 48 * 3600 * 1000;
    },

    async resend(u) {
      this.busy = true;
      this.lastSetupUrl = "";
      try {
        const resp = await api(`/users/${encodeURIComponent(u.username)}/resend`,
                               { method: "POST", token: this.token });
        if (resp.setup_url) {
          this.lastSetupUrl = resp.setup_url;
          this.flash(`Fresh link ready — SMTP off, copy it above.`, true);
        } else {
          this.flash(`Fresh setup link emailed to ${resp.email}.`, true);
        }
      } catch (e) {
        this.flash(`Resend failed: ${this._humanize(e)}`, false);
      } finally {
        this.busy = false;
      }
    },

    async remove(u) {
      if (u.username === this.me || u.role === "admin") return;
      if (!confirm(`Delete operator "${u.username}"?`)) return;
      try {
        await api(`/users/${encodeURIComponent(u.username)}`,
                  { method: "DELETE", token: this.token });
        await this.load();
        this.flash("Operator deleted.", true);
      } catch (e) {
        this.flash(`Delete failed: ${this._humanize(e)}`, false);
      }
    },

    async resetPassword() {
      if (!this.reset.username || !this.reset.password) return;
      this.busy = true;
      try {
        await api(`/users/${encodeURIComponent(this.reset.username)}/reset-password`, {
          method: "POST", token: this.token,
          body: { password: this.reset.password },
        });
        this.reset = { username: "", password: "" };
        this.flash("Password updated.", true);
      } catch (e) {
        this.flash(`Reset failed: ${this._humanize(e)}`, false);
      } finally {
        this.busy = false;
      }
    },

    _humanize(e) {
      // Backend returns HTTPException detail; api() prepends status like "409: ..."
      return (e.message || "").replace(/^\d+:\s*/, "") || "error";
    },

    flash(msg, ok) {
      this.message = msg;
      this.messageTone = ok ? "text-win" : "text-loss";
      setTimeout(() => { this.message = ""; }, 4000);
    },
  }));
});

// -------- Confetti (vanilla, no dep) --------
function confetti() {
  const colors = ["#22ee88", "#ffc73a", "#d8e8e0", "#22ee88", "#ff3355"];
  const n = 60;
  for (let i = 0; i < n; i++) {
    const piece = document.createElement("div");
    piece.className = "confetti-piece";
    piece.style.left = Math.random() * 100 + "vw";
    piece.style.background = colors[i % colors.length];
    piece.style.transform = `rotate(${Math.random() * 360}deg)`;
    document.body.appendChild(piece);
    const fall = piece.animate([
      { transform: `translateY(0) rotate(0deg)`,   opacity: 1 },
      { transform: `translateY(100vh) rotate(${720 * (Math.random() - 0.5)}deg)`, opacity: 0 },
    ], { duration: 1600 + Math.random() * 1200, easing: "cubic-bezier(.2,.6,.4,1)" });
    fall.onfinish = () => piece.remove();
  }
}
