/* v3ai web console */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const state = {
    last: null,
    chatBusy: false,
    decisionBusy: false,
    pending: 0,
    lastWarnings: "",
    timer: null,
    clock: null,
    status: null,
    settings: null,
    loop: null,
    lastLoopTs: 0,
    service: null,
    serviceBusy: false,
    names: {},
    lastCountryId: null,
    scanActive: false,
    refreshMs: Number(localStorage.getItem("v3ai.refreshMs") || 3000) || 3000,
  };

  const BUILDING_NAMES = {
    building_banana_plantation: "香蕉种植园",
    building_tea_plantation: "茶叶种植园",
    building_rice_farm: "稻田",
    building_wheat_farm: "麦田",
    building_rye_farm: "黑麦田",
    building_livestock_ranch: "牧场",
    building_logging_camp: "伐木营地",
    building_iron_mine: "铁矿",
    building_coal_mine: "煤矿",
    building_lead_mine: "铅矿",
    building_sulfur_mine: "硫矿",
    building_construction_sector: "建造部门",
    building_government_administration: "政府行政",
    building_textile_mill: "纺织厂",
    building_food_industry: "食品厂",
    building_furniture_manufactory: "家具厂",
    building_tooling_workshop: "工具厂",
    building_steel_mill: "钢铁厂",
    building_glassworks: "玻璃厂",
    building_paper_mill: "造纸厂",
    building_chemical_industry: "化工厂",
    building_arms_industry: "兵工厂",
    building_port: "港口",
    building_railway: "铁路",
    building_university: "大学",
    building_barrack: "兵营",
    building_conscription_center: "征兵中心",
    building_urban_center: "城市中心",
    building_trade_center: "贸易中心",
    building_manor_house: "庄园",
    building_financial_district: "金融区",
    building_company_headquarters: "公司总部",
    building_sky_scraper: "摩天楼",
  };

  const LAW_NAMES = {
    law_land_based_taxation: "土地税",
    law_per_capita_based_taxation: "人头税",
    law_income_tax: "所得税",
    law_consumption_tax: "消费税",
    law_traditionalism: "传统主义",
    law_interventionism: "干预主义",
    law_laissez_faire: "自由放任",
    law_mercantilism: "重商主义",
    law_free_trade: "自由贸易",
    law_protectionism: "保护主义",
    law_slavery: "奴隶制",
    law_serfdom: "农奴制",
    law_autocracy: "专制",
    law_oligarchy: "寡头",
    law_wealth_voting: "财富选举",
    law_census_voting: "人口选举",
    law_universal_suffrage: "普选",
  };

  const IG_NAMES = {
    ig_landowners: "地主",
    ig_industrialists: "工业家",
    ig_intelligentsia: "知识分子",
    ig_armed_forces: "军队",
    ig_rural_folk: "农民",
    ig_devout: "虔诚者",
    ig_petty_bourgeoisie: "小资产阶级",
    ig_trade_unions: "工会",
  };

  const TOOL_NAMES = {
    oneshot_propose: "单次提议",
    get_overview: "国家概况",
    list_states: "州列表",
    state_detail: "州详情",
    building_catalog: "建筑目录",
    build_candidates: "建造候选",
    pm_candidates: "生产方式候选",
    propose_build: "提议建造",
    propose_pm: "提议生产方式",
    review_pending: "审查待办",
    execute_pending: "执行待办",
    finish_cycle: "结束回合",
    get_market: "市场行情",
    get_politics: "政治概况",
    get_techs: "科技概况",
    get_laws: "法律概况",
    strategy_propose: "提议方略",
    gdp_breakdown: "产值构成",
    construction_capacity: "建造力",
    queue: "建造队列",
  };

  const GOOD_NAMES = {    grain: "谷物", tea: "茶叶", banana: "香蕉", wood: "木材", iron: "铁",
    coal: "煤", tools: "工具", clothes: "衣物", furniture: "家具",
    liquor: "酒", meat: "肉类", fruit: "水果", silk: "丝绸",
    porcelain: "瓷器", opium: "鸦片", services: "服务", transportation: "运输",
    paper: "纸张", sulfur: "硫磺", lead: "铅", steel: "钢", glass: "玻璃",
    fertilizer: "肥料", explosives: "炸药", small_arms: "轻武器",
    artillery: "火炮", oil: "石油", rubber: "橡胶", dyes: "染料",
    cotton: "棉花", sugar: "糖", coffee: "咖啡", tobacco: "烟草",
  };

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtNum(v, digits = 0) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    return new Intl.NumberFormat("zh-CN", {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    }).format(Number(v));
  }

  function fmtCompact(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    const abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(digits) + "B";
    if (abs >= 1e6) return (n / 1e6).toFixed(digits) + "M";
    if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return fmtNum(n, 0);
  }

  function fmtMoney(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    return "£" + fmtCompact(n, digits);
  }

  function fmtDelta(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    return (n >= 0 ? "+" : "−") + "£" + fmtCompact(Math.abs(n), 1);
  }

  function fmtPct(v, digits = 1) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    return Number(v).toFixed(digits) + "%";
  }

  function loc(kind, key) {
    const m = state.names && state.names[kind];
    return (m && key && m[key]) || "";
  }

  function buildingName(key) {
    if (!key) return "未知建筑";
    return loc("buildings", key) || BUILDING_NAMES[key]
      || String(key).replace(/^building_/, "").replace(/_/g, " ");
  }

  function lawName(key) {
    if (!key) return "未知法律";
    return loc("laws", key) || LAW_NAMES[key]
      || String(key).replace(/^law_/, "").replace(/_/g, " ");
  }

  function goodName(key) {
    if (!key) return "未知商品";
    return loc("goods", key) || GOOD_NAMES[key]
      || String(key).replace(/_/g, " ");
  }

  function igName(key) {
    if (!key) return "利益集团";
    return loc("interest_groups", key) || IG_NAMES[key]
      || String(key).replace(/^ig_/, "").replace(/_/g, " ");
  }

  function toolName(key) {
    return TOOL_NAMES[key] || String(key || "工具").replace(/_/g, " ");
  }

  function movementName(key) {
    if (!key) return "政治运动";
    return loc("movements", key)
      || String(key).replace(/^movement_/, "").replace(/_/g, " ");
  }

  function pmName(key) {
    if (!key) return "";
    return loc("pms", key) || String(key).replace(/^pm_/, "").replace(/_/g, " ");
  }

  function prettyText(text) {
    return String(text ?? "").replace(/\b(building_[A-Za-z0-9_]+|law_[A-Za-z0-9_]+|pm_[A-Za-z0-9_]+|ig_[A-Za-z0-9_]+)\b/g, (m) => {
      if (m.startsWith("building_")) return buildingName(m);
      if (m.startsWith("law_")) return lawName(m);
      if (m.startsWith("pm_")) return pmName(m);
      if (m.startsWith("ig_")) return igName(m);
      return m;
    });
  }

  async function loadNames() {
    try {
      const res = await fetch("/api/names", { cache: "force-cache" });
      const data = await res.json();
      if (data.ok) state.names = data.maps || {};
    } catch (err) { /* fallback maps */ }
  }

  function timeOf(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function toast(message, ms = 2600) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), ms);
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function setHTML(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }

  function setClass(id, cls, on) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle(cls, !!on);
  }

  // ---------------------------------------------------------------
  // country data
  // ---------------------------------------------------------------
  async function loadCountry() {
    try {
      const res = await fetch("/api/country", { cache: "no-store" });
      const data = await res.json();
      if (!data.ok) {
        setLiveOffline(data.error || "快照不可用");
        return;
      }
      state.last = data;
      renderCountry(data);
      const cid = (data.meta || {}).country_id;
      if (state.lastCountryId !== null && state.lastCountryId !== cid) {
        loadStrategy();
      }
      state.lastCountryId = cid;
    } catch (err) {
      setLiveOffline("无法连接服务");
    }
  }

  function setLiveOffline(reason) {
    const dot = $("#live-dot");
    if (dot) dot.style.background = "var(--red-bright)";
    setText("live-label", "离线");
    setText("live-time", reason || "离线");
    setText("live-date", "检查 v3ai web 服务");
  }

  function renderCountry(d) {
    const meta = d.meta || {};
    const h = d.headline || {};
    const eco = d.economy || {};
    const con = d.construction || {};
    const market = d.market || {};
    const pol = d.politics || {};
    const cname = meta.country_name || "当前国家";
    const displayName = cname === "大清" ? "大清帝国" : cname;
    setText("brand-kicker", displayName);
    setText("brand-name", displayName);
    setText("country-name", displayName);
    setText("brand-seal", displayName.startsWith("国家 #") ? "国" : Array.from(displayName)[0]);
    setText("hero-seal", displayName.startsWith("国家 #") ? "国" : Array.from(displayName)[0]);
    document.title = `${displayName} · 御前会议`;

    const dot = $("#live-dot");
    if (dot) dot.style.background = meta.stale ? "var(--gold)" : "var(--green)";
    setText("live-label", meta.stale ? "快照过期" : "实时");
    setText("live-time", timeOf(meta.captured_at) + (meta.age_s > 2 ? ` · ${Math.round(meta.age_s)}s 前` : ""));
    setText("live-date", `${meta.date_label || "实时"} · v${meta.game_version || "?"}`);
    setText("game-version", meta.game_version || "1.15.1");

    setText("stat-gdp", fmtMoney(h.gdp_annual, 1));
    setText("stat-pop", fmtCompact(h.population, 1));
    setText("stat-treasury", fmtMoney(h.treasury, 1));
    const bal = Number(h.balance || 0);
    const balEl = $("#stat-balance");
    if (balEl) {
      balEl.textContent = fmtDelta(bal);
      balEl.style.color = bal >= 0 ? "var(--green)" : "var(--red-bright)";
    }
    setText("stat-construction", fmtNum(h.construction_capacity, 1));
    setText("stat-queue", `${h.queue_count || 0}`);

    setText("gdp-value", fmtMoney(eco.gdp_annual, 1));
    setText("gdp-sub", `人均 ${fmtMoney(eco.gdp_per_capita, 2)} · 本周产出 ${fmtMoney(eco.gdp_weekly, 1)}`);
    setText("income-total", fmtMoney(eco.income_total, 1));
    setText("expense-total", fmtMoney(eco.expense_total, 1));
    const be = $("#balance-total");
    if (be) {
      be.textContent = fmtDelta(eco.balance);
      be.style.color = Number(eco.balance) >= 0 ? "var(--green)" : "var(--red-bright)";
    }
    const income = Math.max(1, Number(eco.income_total || 0));
    const fill = $("#balance-fill");
    if (fill) {
      const pct = Math.max(0, Math.min(100, Math.abs(Number(eco.balance || 0)) / income * 180));
      fill.style.width = pct + "%";
      fill.style.background = Number(eco.balance) >= 0
        ? "linear-gradient(90deg,#7f9f6a,#c9a45c)"
        : "linear-gradient(90deg,#8e302b,#c05248)";
    }
    setText("treasury", fmtMoney(eco.treasury, 2));
    setText("credit", fmtMoney(eco.credit_limit, 1));
    setText("investment-pool", fmtMoney(eco.investment_pool, 1));
    setText("tax-level", eco.tax_level === null || eco.tax_level === undefined ? "—" : `第 ${Number(eco.tax_level) + 1} 级`);

    renderMiniList("expense-list", eco.top_expenses || [], Math.max(1, Number(eco.expense_total || 1)));

    setText("construction-cap", fmtNum(con.capacity_per_week, 1));
    setText("queue-count", fmtNum(con.queue_count, 0));
    setText("private-queue-count", fmtNum(con.private_queue_count, 0));
    const cap = Number(con.capacity_per_week || 0);
    const base = Number(con.base || 0);
    const fromB = Number(con.from_buildings || 0);
    const mult = Number(con.mult || 0);
    const sectors = Number(con.sector_count || 0);
    setText("cap-breakdown", `建筑 ${fmtNum(fromB, 1)}（快照内 ${sectors} 个建造部门）+ 基础 ${fmtNum(base, 1)}${mult ? ` × ${(1 + mult).toFixed(2)}` : ""} = ${fmtNum(cap, 1)}/周`);
    const eta = Number(con.queue_weeks || 0);
    setText("queue-eta", con.queue_count
      ? `当前队列剩余约 ${fmtNum(con.queue_remaining_points, 0)} 建造点 · 约 ${eta.toFixed(1)} 周`
      : "当前没有政府建造队列");
    const ring = $("#cap-ring");
    if (ring) ring.style.setProperty("--p", Math.max(0, Math.min(100, cap / 50 * 100)));
    setText("cap-value", fmtNum(cap, 0));
    renderQueue(con.queue || [], "queue-list", "暂无政府建造队列");
    renderQueue(con.private_queue || [], "private-queue-list", "暂无私人建造");

    renderMarket(market);
    renderPolitics(pol);
    setText("hero-pop", fmtCompact(h.population, 1));
    setText("hero-gdp", fmtMoney(h.gdp_annual, 1));
    setText("hero-techs", `${h.techs || 0}`);
    setText("hero-laws", `${h.laws || 0}`);
    setText("tech-count", `${h.techs || 0}`);
    setText("building-levels", fmtCompact((d.buildings || {}).levels, 1));
    setText("building-count", fmtNum((d.buildings || {}).count, 0));
    renderTopBuildings((d.buildings || {}).top_types || []);

    const warnings = d.warnings || [];
    const w = warnings.join(" | ");
    if (w && w !== state.lastWarnings) {
      state.lastWarnings = w;
      toast(warnings[0], 4200);
    }
  }

  function renderMiniList(id, rows, total) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!rows.length) {
      el.innerHTML = '<div class="empty">暂无数据</div>';
      return;
    }
    el.innerHTML = rows.slice(0, 6).map((r) => {
      const amt = Number(r.amount || 0);
      const pct = Math.max(2, Math.min(100, Math.abs(amt) / total * 260));
      return `<div class="mini-row"><span>${escapeHtml(r.label || r.slot || "")}</span>
        <span class="bar"><i style="width:${pct}%"></i></span>
        <span class="amt">${fmtMoney(amt, 1)}</span></div>`;
    }).join("");
  }

  function renderQueue(queue, id = "queue-list", emptyText = "暂无建造队列") {
    const el = document.getElementById(id);
    if (!el) return;
    if (!queue.length) {
      el.innerHTML = `<div class="empty">${emptyText}</div>`;
      return;
    }
    el.innerHTML = queue.map((q, i) => {
      const pct = Math.max(0, Math.min(100, Number(q.progress || 0) * 100));
      return `<div class="queue-item">
        <span class="idx">#${i + 1}</span>
        <div><div class="qname">${escapeHtml(buildingName(q.building))}</div>
          <div class="queue-track"><i style="width:${pct}%"></i></div></div>
        <span class="qmeta">${fmtNum(q.pts_remaining, 0)} / ${fmtNum(q.pts_total, 0)}</span>
      </div>`;
    }).join("");
  }

  function renderTopBuildings(rows) {
    const el = $("#top-building-list");
    if (!el) return;
    if (!rows.length) {
      el.innerHTML = '<div class="empty">暂无数据</div>';
      return;
    }
    const max = Math.max(1, ...rows.map((x) => Number(x.levels || 0)));
    el.innerHTML = rows.slice(0, 6).map((x) => {
      const pct = Math.max(3, Math.min(100, Number(x.levels || 0) / max * 100));
      return `<div class="mini-row"><span>${escapeHtml(buildingName(x.type))}</span>
        <span class="bar"><i style="width:${pct}%"></i></span>
        <span class="amt">${fmtNum(x.levels, 0)}</span></div>`;
    }).join("");
  }

  function renderMarket(market) {    const exp = $("#market-expensive");
    const cheap = $("#market-cheap");
    const row = (x, cls) => {
      const ratio = Number(x.ratio || 1);
      const label = ratio >= 1.35 ? "紧俏" : ratio <= 0.75 ? "过剩" : "";
      return `<div class="market-row"><span class="g" title="${escapeHtml(x.good)}">${escapeHtml(goodName(x.good))}</span>
        <span class="p ${cls}">${fmtMoney(x.price, 1)} ${label}</span></div>`;
    };
    if (exp) exp.innerHTML = (market.most_expensive || []).slice(0, 4).map((x) => row(x, "up")).join("");
    if (cheap) cheap.innerHTML = (market.cheapest || []).slice(0, 4).map((x) => row(x, "down")).join("");
  }

  function renderPolitics(pol) {
    const igs = pol.interest_groups || [];
    const maxClout = Math.max(0.001, ...igs.map((x) => Number(x.clout || 0)));
    setHTML("ig-list", igs.slice(0, 5).map((x) => {
      const clout = Number(x.clout || 0);
      const pct = Math.max(3, Math.min(100, clout / maxClout * 100));
      return `<div class="ig-row"><span class="n">${escapeHtml(igName(x.name))}</span>
        <span class="c">${(clout * 100).toFixed(1)}%</span>
        <span class="ig-bar"><i style="width:${pct}%"></i></span></div>`;
    }).join("") || '<div class="empty">暂无数据</div>');

    const moves = pol.movements || [];
    setHTML("movement-list", moves.slice(0, 5).map((x) => (
      `<div class="movement-row"><span>${escapeHtml(movementName(x.type))}</span>
        <span class="r">${fmtPct(Number(x.radicalism || 0) * 100, 2)}</span></div>`
    )).join("") || '<div class="empty">暂无运动</div>');

    const laws = (pol.laws || []).map((x) => x.name || x).slice(0, 14);
    setHTML("law-chips", laws.map((x) => `<span class="chip" title="${escapeHtml(x)}">${escapeHtml(lawName(x))}</span>`).join(""));
  }

  // ---------------------------------------------------------------
  // chat
  // ---------------------------------------------------------------
  function scrollMessages() {
    const el = $("#messages");
    if (el) el.scrollTop = el.scrollHeight;
  }

  function appendMessage(role, content, opts = {}) {
    const messages = $("#messages");
    if (!messages) return null;
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;
    if (opts.welcome) wrap.dataset.welcome = "1";
    const avatar = role === "user" ? "君" : "御";
    const name = role === "user" ? "阁下" : "御前顾问";
    const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    wrap.innerHTML = `<div class="avatar">${avatar}</div>
      <div class="msg-body">
        <div class="msg-meta"><span>${name}</span><time>${time}</time></div>
        <div class="bubble"></div>
      </div>`;
    const bubble = $(".bubble", wrap);
    if (opts.decision) {
      bubble.innerHTML = content;
      if (opts.reasoning) appendReasoning(bubble, opts.reasoning);
    } else {
      bubble.innerHTML = markdown(content);
      if (opts.reasoning) appendReasoning(bubble, opts.reasoning);
    }
    messages.appendChild(wrap);
    scrollMessages();
    return wrap;
  }

  function appendReasoning(bubble, reasoning) {
    if (!reasoning) return;
    const details = document.createElement("details");
    details.className = "reasoning";
    details.innerHTML = `<summary>思考过程</summary><p>${escapeHtml(reasoning)}</p>`;
    bubble.appendChild(details);
  }

  function markdown(text) {
    const raw = String(text ?? "").trim();
    if (!raw) return "<p>（无内容）</p>";
    const chunks = raw.split(/\n{2,}/);
    return chunks.map((chunk) => {
      let s = escapeHtml(chunk);
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
      const lines = s.split("\n");
      if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
        return "<ul>" + lines.map((l) => `<li>${l.replace(/^\s*[-*]\s+/, "")}</li>`).join("") + "</ul>";
      }
      return "<p>" + lines.join("<br>") + "</p>";
    }).join("");
  }

  function addTyping() {
    const messages = $("#messages");
    if (!messages) return null;
    const el = document.createElement("div");
    el.className = "msg assistant";
    el.innerHTML = `<div class="avatar">御</div><div class="msg-body">
      <div class="msg-meta"><span>御前顾问</span><time>此刻</time></div>
      <div class="bubble"><span class="typing"><i></i><i></i><i></i></span></div></div>`;
    messages.appendChild(el);
    scrollMessages();
    return el;
  }

  async function sendChat() {
    if (state.chatBusy || state.decisionBusy) return;
    const input = $("#chat-input");
    const text = (input?.value || "").trim();
    if (!text) return;
    input.value = "";
    appendMessage("user", text);
    state.chatBusy = true;
    setBusy(true);
    const typing = addTyping();
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const data = await res.json();
      typing?.remove();
      if (!data.ok) {
        appendMessage("assistant", data.error || "御前顾问暂时无法应答。");
      } else {
        appendMessage("assistant", data.reply, { reasoning: data.reasoning });
      }
    } catch (err) {
      typing?.remove();
      appendMessage("assistant", "连接御前顾问失败：" + err.message);
    } finally {
      state.chatBusy = false;
      setBusy(false);
      scrollMessages();
    }
  }

  function setBusy(busy) {
    const locked = !!state.scanActive;
    const btn = $("#send-btn");
    if (btn) btn.disabled = busy || state.decisionBusy;
    const plan = $("#decide-plan");
    const exec = $("#decide-exec");
    if (plan) plan.disabled = locked || busy || state.decisionBusy;
    if (exec) exec.disabled = locked || busy || state.decisionBusy;
    setText("composer-hint", busy ? "御前顾问正在思考…" : "模型只读取当前国家概况");
  }

  // ---------------------------------------------------------------
  // decision
  // ---------------------------------------------------------------
  async function runDecision(execute) {
    if (state.decisionBusy || state.chatBusy) return;
    state.decisionBusy = true;
    setBusy(true);
    const typing = addTyping();
    try {
      const res = await fetch("/api/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ execute: !!execute }),
      });
      const data = await res.json();
      typing?.remove();
      if (!data.ok) {
        appendMessage("assistant", "御前议事失败：" + (data.error || "未知错误"));
      } else {
        renderDecision(data, execute);
      }
      await loadCountry();
      await loadStrategy();
    } catch (err) {
      typing?.remove();
      appendMessage("assistant", "御前议事失败：" + err.message);
    } finally {
      state.decisionBusy = false;
      setBusy(false);
    }
  }

  function renderDecision(d, executed) {
    state.pending = Number(d.pending_count || 0);
    const latency = d.latency_s ? `${Number(d.latency_s).toFixed(1)}s` : "";
    const proposals = (d.proposals || []).map((p) => proposalHTML(p)).join("");
    const executions = (d.executions || []).map((e) => (
      `<div class="exec-line ${e.ok ? "ok" : "fail"}">${e.ok ? "✓" : "✗"} ${escapeHtml(prettyText(e.message || e.status || ""))}</div>`
    )).join("");
    const trace = (d.tool_calls || []).map((t) => {
      const args = t.args ? JSON.stringify(t.args) : "";
      return `<div class="trace-row">
        <span class="trace-name">${escapeHtml(toolName(t.name))}</span>
        <span class="trace-arg">${escapeHtml(args.slice(0, 220))}</span>
        <span class="trace-ok ${t.ok === false ? "bad" : ""}">${t.ok === false ? "✗" : "✓"} ${escapeHtml(t.code || "")}</span>
      </div>`;
    }).join("");
    const usage = d.llm_usage && d.llm_usage.total_tokens
      ? `<span class="trace-usage">${d.llm_usage.total_tokens} 令牌</span>` : "";
    const chain = `<details class="thought-chain" open>
      <summary>AI 思考链 / 决策轨迹 ${d.tool_calls ? `· ${d.tool_calls.length} 步` : ""} ${usage}</summary>
      <div class="thought-body">
        ${d.summary ? `<div class="thought-line"><b>结论</b>${escapeHtml(d.summary)}</div>` : ""}
        ${d.rationale ? `<div class="thought-line"><b>依据</b>${escapeHtml(d.rationale)}</div>` : ""}
        ${trace || '<div class="preason">本轮没有工具调用（单次决策直出）。</div>'}
      </div>
    </details>`;
    const pendingBtn = (!executed && state.pending > 0)
      ? `<button class="gold-btn pending-action" id="execute-pending">执行 ${state.pending} 条待办</button>`
      : "";
    const html = `<div class="decision-card">
      <h4>御前决议 · 回合 ${d.cycle || 1} ${latency ? `· ${latency}` : ""}</h4>
      <div class="rationale">${escapeHtml(prettyText(d.summary || ""))}</div>
      ${d.rationale ? `<div class="rationale">${escapeHtml(prettyText(d.rationale))}</div>` : ""}
      ${chain}
      ${proposals || '<div class="preason">本轮无动作。</div>'}
      ${executions}
      ${pendingBtn}
    </div>`;
    appendMessage("assistant", html, { decision: true, reasoning: d.reasoning });
    const btn = $("#execute-pending");
    if (btn) btn.addEventListener("click", executePending);
    if (executed && state.pending === 0) state.pending = 0;
  }

  function proposalHTML(p) {
    const args = p.args || {};
    const kind = args.kind || (args.pm ? "pm" : "build");
    let label;
    if (kind === "build") {
      label = `建造 ${buildingName(args.building)} ×${args.levels || 1}`;
    } else {
      label = `换生产方式 ${buildingName(args.building)} → ${pmName(args.pm)}`;
    }
    const approved = !!p.approved;
    const reasons = (p.reasons || []).slice(0, 3).join("；");
    return `<div class="proposal ${approved ? "" : "rejected"}">
      <div class="phead"><span>${escapeHtml(label)}</span>
        <span class="ptag">${approved ? "✓ 通过" : "✗ 拒绝"}</span></div>
      ${reasons ? `<div class="preason">${escapeHtml(prettyText(reasons))}</div>` : ""}
    </div>`;
  }

  async function executePending() {
    if (state.decisionBusy || state.pending <= 0) return;
    state.decisionBusy = true;
    setBusy(true);
    try {
      const res = await fetch("/api/execute", { method: "POST" });
      const data = await res.json();
      if (!data.ok) {
        appendMessage("assistant", "执行失败：" + (data.error || "未知错误"));
      } else {
        const lines = (data.executions || []).map((e) => (
          `<div class="exec-line ${e.ok ? "ok" : "fail"}">${e.ok ? "✓" : "✗"} ${escapeHtml(e.message || e.status || "")}</div>`
        )).join("");
        appendMessage("assistant", `<div class="decision-card"><h4>执行反馈</h4>${lines || "<div class='preason'>无结果</div>"}</div>`, { decision: true });
        state.pending = 0;
      }
      await loadCountry();
      await loadStrategy();
    } catch (err) {
      appendMessage("assistant", "执行失败：" + err.message);
    } finally {
      state.decisionBusy = false;
      setBusy(false);
    }
  }

  // ---------------------------------------------------------------
  // settings
  // ---------------------------------------------------------------
  async function loadSettings() {
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      const data = await res.json();
      if (!data.ok) return;
      state.settings = data;
      fillSettings(data);
    } catch (err) {
      /* ignore */
    }
  }

  function fillSettings(s) {
    const sel = $("#set-provider");
    if (sel) {
      sel.innerHTML = (s.providers || []).map((p) => (
        `<option value="${escapeHtml(p.name)}">${escapeHtml(p.label)} · ${escapeHtml(p.model)}</option>`
      )).join("");
      sel.value = s.active_provider || "";
    }
    const prov = (s.providers || []).find((p) => p.name === (s.active_provider || ""));
    if (prov) {
      $("#set-model").value = prov.model || "";
      $("#set-base-url").value = prov.base_url || "";
    }
    $("#set-api-key").value = "";
    $("#set-api-key").placeholder = prov && prov.has_key
      ? "已配置；留空保持不变"
      : "未配置；输入 API Key（仅本地保存）";
    const a = s.agent || {};
    $("#set-mode").value = a.mode || "oneshot";
    $("#set-toolset").value = a.toolset || "core";
    $("#set-actions").value = a.max_actions_per_cycle ?? 1;
    $("#set-levels").value = a.max_levels_per_action ?? 5;
    $("#set-context").value = a.max_context_chars ?? 0;
    $("#set-max-tokens").value = a.max_tokens ?? 0;
    $("#set-tool-chars").value = a.max_tool_result_chars ?? 16000;
    $("#set-history").value = a.max_history_cycles ?? 12;
    $("#set-rounds").value = a.max_tool_rounds ?? 3;
    const lp = s.loop || {};
    $("#set-loop-enabled").checked = lp.enabled !== false;
    $("#set-loop-interval").value = lp.interval_s ?? 20;
    $("#set-loop-execute").checked = lp.execute !== false;
    $("#set-loop-stale").checked = lp.pause_if_stale !== false;
    $("#set-freeze").checked = !!a.freeze_cycle;
    $("#set-refresh").value = String(state.refreshMs);
  }

  function openSettings() {
    const modal = $("#settings-modal");
    if (!modal) return;
    modal.hidden = false;
    loadSettings();
  }

  function closeSettings() {
    const modal = $("#settings-modal");
    if (modal) modal.hidden = true;
  }

  function settingsPayload() {
    return {
      provider: $("#set-provider")?.value || "",
      model: $("#set-model")?.value?.trim() || "",
      base_url: $("#set-base-url")?.value?.trim() || "",
      api_key: $("#set-api-key")?.value || "",
      mode: $("#set-mode")?.value || "oneshot",
      toolset: $("#set-toolset")?.value || "core",
      max_actions: Number($("#set-actions")?.value ?? 1),
      max_levels: Number($("#set-levels")?.value ?? 5),
      max_context_chars: Number($("#set-context")?.value ?? 0),
      max_tokens: Number($("#set-max-tokens")?.value ?? 0),
      max_tool_result_chars: Number($("#set-tool-chars")?.value ?? 16000),
      max_history_cycles: Number($("#set-history")?.value ?? 12),
      max_tool_rounds: Number($("#set-rounds")?.value ?? 3),
      freeze: !!$("#set-freeze")?.checked,
      loop_enabled: !!$("#set-loop-enabled")?.checked,
      loop_interval: Number($("#set-loop-interval")?.value ?? 20),
      loop_execute: !!$("#set-loop-execute")?.checked,
      loop_stale: !!$("#set-loop-stale")?.checked,
    };
  }

  function setSettingsStatus(text, cls) {
    const el = $("#settings-status");
    if (!el) return;
    el.textContent = text;
    el.className = "settings-note" + (cls ? " " + cls : "");
  }

  async function saveSettings() {
    const refresh = Number($("#set-refresh")?.value || 3000);
    setSettingsStatus("保存中…");
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settingsPayload()),
      });
      const data = await res.json();
      if (!data.ok) {
        setSettingsStatus(data.error || "保存失败", "fail");
        return;
      }
      state.settings = data;
      state.refreshMs = refresh;
      localStorage.setItem("v3ai.refreshMs", String(refresh));
      startPolling();
      fillSettings(data);
      setSettingsStatus("已保存到 v3ai/config.json；模型切换立即生效。", "ok");
      await loadStatus();
      await loadCountry();
      toast("设置已保存");
      setTimeout(closeSettings, 650);
    } catch (err) {
      setSettingsStatus("保存失败：" + err.message, "fail");
    }
  }

  async function testSettings() {
    setSettingsStatus("正在测试连接…");
    try {
      const res = await fetch("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settingsPayload()),
      });
      const data = await res.json();
      if (data.ok) {
        setSettingsStatus("连接成功：" + (data.message || "ok"), "ok");
      } else {
        setSettingsStatus("连接失败：" + (data.error || data.message || "未知错误"), "fail");
      }
    } catch (err) {
      setSettingsStatus("连接失败：" + err.message, "fail");
    }
  }

  function startPolling() {
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(loadCountry, state.refreshMs);
  }

  function bindSettings() {
    $("#settings-open")?.addEventListener("click", openSettings);
    $("#settings-close")?.addEventListener("click", closeSettings);
    $("#settings-save")?.addEventListener("click", saveSettings);
    $("#settings-test")?.addEventListener("click", testSettings);
    $("#set-provider")?.addEventListener("change", () => {
      const s = state.settings;
      if (!s) return;
      const prov = (s.providers || []).find((p) => p.name === $("#set-provider").value);
      if (prov) {
        $("#set-model").value = prov.model || "";
        $("#set-base-url").value = prov.base_url || "";
        $("#set-api-key").value = "";
        $("#set-api-key").placeholder = prov.has_key
          ? "已配置；留空保持不变"
          : "未配置；输入 API Key（仅本地保存）";
      }
    });
    $("#settings-modal")?.addEventListener("click", (e) => {
      if (e.target.id === "settings-modal") closeSettings();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeSettings();
        closeStrategy();
      }
    });
  }

  // ---------------------------------------------------------------
  // strategy
  // ---------------------------------------------------------------
  const METRIC_NAMES = {
    gdp_growth: "产值增长",
    weekly_balance: "周结余",
    standard_of_living: "生活水平",
    radicals: "激进派",
    construction_capacity: "建造力",
    employment: "就业",
    bureaucracy: "行政力",
    literacy: "识字率",
    innovation: "创新力",
  };

  function metricName(k) { return METRIC_NAMES[k] || String(k || ""); }

  const DEV_MODES = {
    nationwide: "全国共同发展",
    core: "核心省份优先",
    coastal: "沿海省份优先",
    agriculture: "农业省份优先",
    resources: "资源省份优先",
    capital: "京畿/首都优先",
    balanced: "区域均衡",
    custom: "自定义省份",
  };

  function strategySource(source) {
    return ({ default: "默认", player: "玩家", ai: "AI" })[source] || source || "未知";
  }

  async function loadStrategy() {
    try {
      const res = await fetch("/api/strategy", { cache: "no-store" });
      const data = await res.json();
      if (!data.ok) return;
      state.strategy = data;
      renderStrategy(data.active || {});
      renderStrategySelect(data);
    } catch (err) { /* ignore */ }
  }

  function renderStrategySelect(data) {
    const sel = $("#strategy-select");
    if (!sel || document.activeElement === sel) return;
    const prev = sel.value;
    const active = data.active || {};
    const opts = [`<option value="active">当前：v${active.version || 1} · ${escapeHtml(active.name || "")}</option>`];
    const hist = (data.history || []).slice(-12).reverse();
    hist.forEach((h) => {
      opts.push(`<option value="${h.index}">v${h.version || 1} · ${escapeHtml(h.name || "")} · ${escapeHtml(strategySource(h.source))}</option>`);
    });
    sel.innerHTML = opts.join("");
    sel.value = (prev && prev !== "active" && sel.querySelector(`option[value="${prev}"]`))
      ? prev : "active";
  }

  async function activateStrategy() {
    const sel = $("#strategy-select");
    if (!sel || sel.value === "active") return;
    setStrategyStatus("切换中…");
    try {
      const res = await fetch("/api/strategy/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ index: Number(sel.value) }),
      });
      const data = await res.json();
      if (!data.ok) {
        setStrategyStatus(data.error || "切换失败", "fail");
        return;
      }
      state.strategy = { ...data, buildings: (state.strategy && state.strategy.buildings) || [], states: (state.strategy && state.strategy.states) || [] };
      renderStrategy(data.active || {});
      renderStrategySelect(data);
      if (!$("#strategy-modal").hidden) renderStrategyForm(data.active || {});
      toast("已切换方略：" + (data.active.name || ""));
    } catch (err) {
      setStrategyStatus("切换失败：" + err.message, "fail");
    }
  }

  function renderStrategy(st) {
    setText("strategy-name", st.name || "未命名策略");
    const dev = st.development || {};
    setText("strategy-meta", `来源：${strategySource(st.source)} · 版本 ${st.version || 1}`);
    const allStates = (state.strategy && state.strategy.states) || [];
    const wanted = new Set((dev.priority_states || []).map(String));
    const chosen = allStates.filter((x) => wanted.has(String(x.id)));
    const focusIds = new Set(((state.strategy && state.strategy.focus_states) || allStates.map((x) => x.id)).map(String));
    const focused = allStates.filter((x) => focusIds.has(String(x.id)));
    const regionMode = DEV_MODES[dev.mode] || "全国共同发展";
    setText("region-mode", regionMode);
    setText("region-count", `当前 ${allStates.length} 个州 · ${dev.mode === "custom" ? "已选" : "重点"} ${dev.mode === "custom" ? chosen.length : focused.length} 个`);
    const shown = dev.mode === "custom" ? chosen : focused;
    const chips = shown.slice(0, 14).map((x) =>
      `<span class="region-chip">${escapeHtml(x.label || ("州 " + x.id))}</span>`);
    if (shown.length > 14) chips.push('<span class="region-chip more">更多…</span>');
    setHTML("region-list", chips.join("") || '<span class="region-chip">未选择重点州</span>');
    setText("region-notes", dev.notes || (dev.max_states_per_cycle
      ? `每轮最多涉及 ${dev.max_states_per_cycle} 个州`
      : "按上方区域策略选择建造州"));
    const objs = (st.objectives || []).slice(0, 5);
    setHTML("strategy-objs", objs.map((o) => {
      const w = Math.max(0, Math.min(100, Number(o.weight || 0) * 100));
      return `<div class="strategy-obj"><span>${escapeHtml(metricName(o.metric))}</span>
        <span class="w">${w.toFixed(0)}%</span>
        <span class="ob"><i style="width:${w}%"></i></span></div>`;
    }).join("") || '<div class="empty">暂无目标</div>');
    const c = st.constraints || {};
    const bits = [];
    bits.push(`动作 ${c.max_actions_per_cycle === 0 ? "不限" : (c.max_actions_per_cycle ?? "?")}`);
    bits.push(`等级 ${c.max_levels_per_action === 0 ? "不限" : (c.max_levels_per_action ?? "?")}`);
    bits.push(`区域 ${DEV_MODES[dev.mode] || "全国共同发展"}`);
    if (dev.mode === "custom") bits.push(`${(dev.priority_states || []).length} 州`);
    if (dev.max_states_per_cycle) bits.push(`最多 ${dev.max_states_per_cycle} 州`);
    if (c.allow_conditional_pm) bits.push("允许条件PM");
    if ((c.block_pm_patterns || []).length) bits.push("禁 " + c.block_pm_patterns.join(","));
    setText("strategy-notes", (st.rationale || st.notes || bits.join(" · ")).slice(0, 220));
  }

  function buildingPickerValues(id) {
    return Array.from(document.querySelectorAll(`#${id} input.building-check:checked`))
      .map((el) => el.value);
  }

  function renderBuildingPicker(id, selected, query = "") {
    const el = document.getElementById(id);
    if (!el) return;
    const chosen = new Set(selected || []);
    const q = String(query || "").trim().toLowerCase();
    const buildings = (state.strategy && state.strategy.buildings) || [];
    const rows = buildings.filter((key) => {
      if (!q) return true;
      return String(key).toLowerCase().includes(q) || buildingName(key).toLowerCase().includes(q);
    });
    el.innerHTML = rows.map((key) =>
      `<label title="${escapeHtml(key)}"><input type="checkbox" class="building-check" value="${escapeHtml(key)}" ${chosen.has(key) ? "checked" : ""}><span>${escapeHtml(buildingName(key))}</span></label>`
    ).join("") || '<div class="preason">没有匹配建筑</div>';
  }

  function renderStatePicker(states, selected) {
    const list = $("#st-state-list");
    if (!list) return;
    const chosen = new Set((selected || []).map(String));
    list.innerHTML = (states || []).map((s) => {
      const pop = Number(s.population || 0) / 1e6;
      return `<label title="州 ${s.id} · ${escapeHtml(s.region || "")}">
        <input type="checkbox" class="st-state" value="${s.id}" ${chosen.has(String(s.id)) ? "checked" : ""}>
        <span>${escapeHtml(s.label || ("州 " + s.id))}</span>
        <small>${pop.toFixed(1)}M</small>
      </label>`;
    }).join("") || '<div class="preason">暂无州列表</div>';
  }

  function addObjectiveRow(obj = {}) {
    const list = $("#st-objectives");
    if (!list) return;
    const metrics = ["gdp_growth", "weekly_balance", "standard_of_living", "radicals",
      "construction_capacity", "employment", "bureaucracy", "literacy", "innovation"];
    const metric = obj.metric || "gdp_growth";
    if (!metrics.includes(metric)) metrics.push(metric);
    const row = document.createElement("div");
    row.className = "obj-row";
    row.innerHTML = `<select class="obj-metric">${metrics.map((m) => (
      `<option value="${m}" ${m === metric ? "selected" : ""}>${escapeHtml(metricName(m))}</option>`
    )).join("")}</select>
      <input class="obj-weight" type="number" min="0" max="10" step="0.05" value="${Number(obj.weight ?? 0.2)}" title="权重">
      <select class="obj-dir">
        <option value="max" ${(obj.direction || "max") === "max" ? "selected" : ""}>最大化</option>
        <option value="min" ${obj.direction === "min" ? "selected" : ""}>最小化</option>
      </select>
      <button class="obj-remove" type="button" title="删除">×</button>`;
    $(".obj-remove", row).addEventListener("click", () => row.remove());
    list.appendChild(row);
  }

  function renderStrategyForm(st) {
    $("#st-name").value = st.name || "新策略";
    $("#st-rationale").value = st.rationale || "";
    $("#st-notes").value = st.notes || "";
    const c = st.constraints || {};
    const dev = st.development || {};
    $("#st-dev-mode").value = dev.mode || "nationwide";
    $("#st-dev-max").value = dev.max_states_per_cycle ?? 0;
    $("#st-dev-notes").value = dev.notes || "";
    renderStatePicker(state.strategy?.states || [], dev.priority_states || []);
    $("#st-max-actions").value = c.max_actions_per_cycle ?? 0;
    $("#st-max-levels").value = c.max_levels_per_action ?? 0;
    $("#st-min-treasury").value = c.min_treasury ?? 0;
    $("#st-cond-pm").checked = !!c.allow_conditional_pm;
    $("#st-block-pm").value = (c.block_pm_patterns || []).join(", ");
    renderBuildingPicker("st-priority-list", c.priority_buildings || []);
    renderBuildingPicker("st-forbidden-list", c.forbidden_buildings || []);
    const list = $("#st-objectives");
    if (list) list.innerHTML = "";
    const objs = st.objectives || [];
    if (!objs.length) addObjectiveRow({ metric: "gdp_growth", weight: 0.35 });
    objs.forEach((o) => addObjectiveRow(o));
  }

  function collectStrategyForm() {
    const objectives = [];
    $$("#st-objectives .obj-row").forEach((row) => {
      const metric = $(".obj-metric", row)?.value;
      const weight = Number($(".obj-weight", row)?.value || 0);
      const direction = $(".obj-dir", row)?.value || "max";
      if (metric) objectives.push({ metric, weight, direction });
    });
    const base = state.strategy?.active?.constraints || {};
    const selected = (id) => buildingPickerValues(id);
    const constraints = {
      ...base,
      max_actions_per_cycle: Number($("#st-max-actions").value || 0),
      max_levels_per_action: Number($("#st-max-levels").value || 0),
      min_treasury: Number($("#st-min-treasury").value || 0),
      allow_conditional_pm: !!$("#st-cond-pm").checked,
      block_pm_patterns: ($("#st-block-pm").value || "").split(",").map((s) => s.trim()).filter(Boolean),
      priority_buildings: selected("st-priority-list"),
      forbidden_buildings: selected("st-forbidden-list"),
    };
    return {
      name: ($("#st-name").value || "新策略").trim(),
      rationale: ($("#st-rationale").value || "").trim(),
      objectives,
      constraints,
      development: {
        mode: $("#st-dev-mode").value || "nationwide",
        max_states_per_cycle: Number($("#st-dev-max").value || 0),
        priority_states: Array.from(document.querySelectorAll("#st-state-list .st-state:checked"))
          .map((el) => Number(el.value)),
        notes: ($("#st-dev-notes").value || "").trim(),
      },
      notes: ($("#st-notes").value || "").trim(),
    };
  }

  async function openStrategy(focus = "build") {
    const modal = $("#strategy-modal");
    if (!modal) return;
    modal.hidden = false;
    const body = modal.querySelector(".modal-body");
    if (body) body.scrollTop = 0;
    setStrategyStatus("加载中…");
    try {
      const res = await fetch("/api/strategy", { cache: "no-store" });
      const data = await res.json();
      if (!data.ok) {
        setStrategyStatus(data.error || "读取策略失败", "fail");
        return;
      }
      state.strategy = data;
      renderStrategy(data.active || {});
      renderStrategyForm(data.active || {});
      const body = modal.querySelector(".modal-body");
      if (body) {
        const target = focus === "region" ? $("#st-dev-section") : null;
        body.scrollTop = target ? Math.max(0, target.offsetTop - 12) : 0;
      }
      setStrategyStatus("填表后保存；审查层立即使用。");
    } catch (err) {
      setStrategyStatus("读取策略失败：" + err.message, "fail");
    }
  }

  function closeStrategy() {
    const modal = $("#strategy-modal");
    if (modal) modal.hidden = true;
  }

  function setStrategyStatus(text, cls) {
    const el = $("#strategy-status");
    if (!el) return;
    el.textContent = text;
    el.className = "settings-note" + (cls ? " " + cls : "");
  }

  async function saveStrategy() {
    const obj = collectStrategyForm();
    if (!obj.objectives.length) {
      setStrategyStatus("至少保留一个目标。", "fail");
      return;
    }
    setStrategyStatus("保存中…");
    try {
      const res = await fetch("/api/strategy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(obj),
      });
      const data = await res.json();
      if (!data.ok) {
        setStrategyStatus(data.error || "保存失败", "fail");
        return;
      }
      state.strategy = data;
      renderStrategy(data.active || {});
      setStrategyStatus("已保存；下一轮决策使用新策略。", "ok");
      toast("方略已更新");
      setTimeout(closeStrategy, 650);
    } catch (err) {
      setStrategyStatus("保存失败：" + err.message, "fail");
    }
  }

  async function draftStrategy(apply = false) {
    setStrategyStatus("AI 正在起草方略…");
    try {
      const res = await fetch("/api/strategy/draft", { method: "POST" });
      const data = await res.json();
      if (!data.ok) {
        setStrategyStatus("起草失败：" + (data.error || "未知错误"), "fail");
        return;
      }
      renderStrategyForm(data.strategy || {});
      const usage = data.usage && data.usage.total_tokens
        ? ` · ${data.usage.total_tokens} tokens` : "";
      if (apply) {
        const save = await fetch("/api/strategy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ strategy: data.strategy || {}, source: "ai" }),
        });
        const saved = await save.json();
        if (!saved.ok) {
          setStrategyStatus("AI 草稿已生成，但应用失败：" + (saved.error || ""), "fail");
          return;
        }
        state.strategy = { ...saved, buildings: (state.strategy && state.strategy.buildings) || [], states: (state.strategy && state.strategy.states) || [] };
        renderStrategy(saved.active || {});
        renderStrategyForm(saved.active || {});
        setStrategyStatus(`AI 方略已直接应用${usage}。`, "ok");
        toast("AI 方略已应用");
      } else {
        setStrategyStatus(`AI 草稿已填入（未保存）${usage}；请审阅后保存。`, "ok");
      }
    } catch (err) {
      setStrategyStatus("起草失败：" + err.message, "fail");
    }
  }

  async function resetStrategy() {
    setStrategyStatus("重置中…");
    try {
      const res = await fetch("/api/strategy/reset", { method: "POST" });
      const data = await res.json();
      if (!data.ok) {
        setStrategyStatus(data.error || "重置失败", "fail");
        return;
      }
      state.strategy = { ...data, buildings: (state.strategy && state.strategy.buildings) || [], states: (state.strategy && state.strategy.states) || [] };
      renderStrategy(data.active || {});
      renderStrategyForm(data.active || {});
      setStrategyStatus("已重置为默认策略。", "ok");
      toast("方略已重置");
    } catch (err) {
      setStrategyStatus("重置失败：" + err.message, "fail");
    }
  }

  function bindStrategy() {
    $("#strategy-edit")?.addEventListener("click", () => openStrategy("build"));
    $("#strategy-edit-region")?.addEventListener("click", () => openStrategy("region"));
    $("#strategy-activate")?.addEventListener("click", activateStrategy);
    $("#strategy-close")?.addEventListener("click", closeStrategy);
    $("#strategy-save")?.addEventListener("click", saveStrategy);
    $("#strategy-ai")?.addEventListener("click", () => draftStrategy(false));
    $("#strategy-ai-apply")?.addEventListener("click", () => draftStrategy(true));
    $("#strategy-reset")?.addEventListener("click", resetStrategy);
    $("#st-add-obj")?.addEventListener("click", () => addObjectiveRow({}));
    $("#st-priority-search")?.addEventListener("input", (e) => {
      renderBuildingPicker("st-priority-list", buildingPickerValues("st-priority-list"), e.target.value);
    });
    $("#st-forbidden-search")?.addEventListener("input", (e) => {
      renderBuildingPicker("st-forbidden-list", buildingPickerValues("st-forbidden-list"), e.target.value);
    });
    $("#st-states-all")?.addEventListener("click", () => {
      document.querySelectorAll("#st-state-list .st-state").forEach((el) => { el.checked = true; });
    });
    $("#st-states-none")?.addEventListener("click", () => {
      document.querySelectorAll("#st-state-list .st-state").forEach((el) => { el.checked = false; });
    });
    $("#strategy-modal")?.addEventListener("click", (e) => {
      if (e.target.id === "strategy-modal") closeStrategy();
    });
  }

  // ---------------------------------------------------------------
  // service control
  // ---------------------------------------------------------------
  async function loadService() {
    try {
      const res = await fetch("/api/service", { cache: "no-store" });
      const data = await res.json();
      if (data.ok) {
        state.service = data.service;
        renderService(data.service);
      }
    } catch (err) { /* ignore */ }
  }

  function setServiceLock(active) {
    ["decide-plan", "decide-exec", "loop-toggle", "execute-pending"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.disabled = !!active;
    });
    document.body.classList.toggle("service-locked", !!active);
  }

  function renderScanOverlay(s, st, scan) {
    const modal = $("#scan-modal");
    if (!modal) return;
    const phase = String(scan.phase || "");
    const canScan = st === "starting";
    const active = canScan && (phase === "scanning" || phase === "waiting");
    if (active) {
      modal.hidden = false;
      let pct;
      let step;
      let detail;
      pct = Math.max(3, Math.round(Number(scan.progress || 0) * 100));
      step = phase === "waiting" ? "等待进入存档…"
        : phase === "scanning" ? "扫描类型表…"
        : "注入 DLL…";
      const regions = scan.regions;
      detail = regions
        ? `区域 ${regions.current}/${regions.total} · ${regions.elapsed_s}s · 已找到 ${regions.keys} 个键`
        : (scan.message || "DLL 扫描完成前，决策与连续模式已锁定。");
      setText("scan-step", step);
      setText("scan-percent", `${pct}%`);
      const fill = $("#scan-big-fill");
      if (fill) fill.style.width = `${pct}%`;
      setText("scan-detail", detail);
      setServiceLock(true);
      state.scanActive = true;
    } else {
      if (state.scanActive && phase === "ready") {
        toast("DLL 扫描完成，服务已就绪", 5000);
      }
      modal.hidden = true;
      setServiceLock(false);
      state.scanActive = false;
    }
  }

  function renderService(s) {
    if (!s) return;
    const dot = $("#service-dot");
    const label = $("#service-label");
    const detail = $("#service-detail");
    const btn = $("#service-toggle");
    const st = String(s.state || "unknown");
    const dll = s.dll || {};
    const watch = s.watch || {};
    const loop = s.loop || {};
    const agent = s.agent || {};
    const scan = s.scan || {};
    let text = "服务未知";
    let cls = "";
    if (st === "starting") { text = "服务启动中…"; cls = "busy"; }
    else if (st === "stopping") { text = "服务停止中…"; cls = "busy"; }
    else if (st === "error") { text = "服务启动失败"; cls = "err"; }
    else if (st === "partial") { text = "部分停止（DLL 保留）"; cls = "err"; }
    else if (dll.ready || st === "running") { text = "服务运行中"; cls = "on"; }
    else if (st === "stopped") { text = "服务已停止"; cls = ""; }
    if (dot) dot.className = "service-dot " + cls;
    if (label) label.textContent = text;
    if (detail) {
      const bits = [];
      if (agent.online && agent.game_visible) bits.push("主机代理在线");
      else if (s.game_visible === false) bits.push(agent.online ? "⚠ 代理看不到游戏" : "⚠ 看不到游戏进程");
      bits.push(dll.ready ? "DLL 就绪" : (dll.loaded ? "DLL 注入中" : "DLL 未载入"));
      bits.push(watch.running ? "watch 运行" : (watch.external ? "外部快照" : "watch 停"));
      bits.push(loop.enabled ? "连续开" : "连续关");
      detail.textContent = bits.join(" · ");
      detail.title = s.error || (dll.log_tail || []).join("\n");
    }
    const scanEl = $("#service-scan");
    const pct = Math.round(Number(scan.progress || 0) * 100);
    if (scanEl) {
      if (scan.phase === "ready") {
        scanEl.textContent = "扫描 100% · 类型表已就绪";
        scanEl.className = "service-scan ready";
      } else if (scan.phase === "scanning") {
        scanEl.textContent = `扫描 ${pct}% · ${scan.elapsed_s || 0}s / 预计 ~${scan.estimate_s || 125}s`;
        scanEl.className = "service-scan scanning";
      } else if (scan.phase === "waiting") {
        scanEl.textContent = "扫描等待 · DLL 已载入";
        scanEl.className = "service-scan";
      } else {
        scanEl.textContent = "扫描 0% · DLL 未注入";
        scanEl.className = "service-scan";
      }
    }
    const fill = $("#service-progress-fill");
    if (fill) {
      fill.style.width = `${Math.round(Number(scan.progress || 0) * 100)}%`;
      fill.style.background = scan.phase === "ready" ? "var(--green)"
        : scan.phase === "scanning" ? "var(--gold)" : "var(--muted)";
    }
    renderScanOverlay(s, st, scan);
    if (btn) {
      const running = !!(dll.loaded && st === "partial")
        || !!(dll.ready && (watch.running || watch.external || s.live))
        || st === "starting";
      btn.textContent = running ? "停止服务" : "启动服务";
      btn.classList.toggle("stop", running);
      btn.classList.toggle("gold-btn", !running);
      btn.disabled = state.serviceBusy || st === "starting" || st === "stopping";
    }
  }

  async function toggleService() {
    if (state.serviceBusy) return;
    const s = state.service || {};
    const running = !!(s.dll && s.dll.loaded && s.state === "partial")
      || !!(s.dll && s.dll.ready &&
        ((s.watch && (s.watch.running || s.watch.external)) || s.live));
    state.serviceBusy = true;
    renderService(s);
    try {
      const res = await fetch(running ? "/api/service/stop" : "/api/service/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await res.json();
      if (!data.ok) {
        toast((running ? "停止失败：" : "启动失败：") + (data.error || "未知错误"), 5000);
      } else {
        toast(data.message || (running ? "正在停止服务" : "正在启动服务"));
      }
      await loadService();
    } catch (err) {
      toast("服务操作失败：" + err.message, 5000);
    } finally {
      state.serviceBusy = false;
      renderService(state.service);
    }
  }

  // ---------------------------------------------------------------
  // status / init
  // ---------------------------------------------------------------
  async function loadStatus() {
    try {
      const res = await fetch("/api/status", { cache: "no-store" });
      const data = await res.json();
      state.status = data;
      const dot = $("#model-dot");
      if (dot) dot.classList.toggle("off", !data.ok);
      setText("model-name", data.model || "本地模型");
      const mode = data.agent_mode === "oneshot" ? "单次决策" : "工具决策";
      setText("agent-mode", mode);
      if (data.loop) updateLoop(data.loop);
    } catch (err) {
      const dot = $("#model-dot");
      if (dot) dot.classList.add("off");
      setText("model-name", "模型离线");
    }
  }

  function updateLoop(loop) {
    state.loop = loop;
    const btn = $("#loop-toggle");
    const st = $("#loop-state");
    if (btn) {
      btn.textContent = loop.enabled ? "连续：开" : "连续：关";
      btn.classList.toggle("gold-btn", !!loop.enabled);
      btn.classList.toggle("ghost-btn", !loop.enabled);
    }
    if (st) {
      if (!loop.enabled) {
        st.textContent = "连续：关";
        st.classList.add("paused");
      } else if (loop.paused_reason) {
        st.textContent = "连续：暂停 · " + loop.paused_reason;
        st.classList.add("paused");
      } else {
        const n = Number(loop.next_in || 0);
        st.textContent = `连续：开 · 下一轮 ${n <= 0 ? "立刻" : n.toFixed(0) + "s"}`;
        st.classList.remove("paused");
      }
    }
    if (loop.last && loop.last.ts && loop.last.ts !== state.lastLoopTs) {
      state.lastLoopTs = loop.last.ts;
      renderDecision({ ...loop.last, pending_count: 0 }, true);
    }
  }

  async function toggleLoop() {
    const enabled = !(state.loop && state.loop.enabled);
    try {
      const res = await fetch("/api/loop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      const data = await res.json();
      if (data.ok) {
        updateLoop(data.loop);
        toast(enabled ? "连续模式已开启" : "连续模式已关闭");
      }
    } catch (err) {
      toast("切换连续模式失败：" + err.message);
    }
  }

  function bind() {
    bindSettings();
    bindStrategy();
    $("#loop-toggle")?.addEventListener("click", toggleLoop);
    $("#service-toggle")?.addEventListener("click", toggleService);
    $("#send-btn")?.addEventListener("click", sendChat);
    $("#chat-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });
    $("#decide-plan")?.addEventListener("click", () => runDecision(false));
    $("#decide-exec")?.addEventListener("click", () => runDecision(true));
    $("#clear-chat")?.addEventListener("click", () => {
      const messages = $("#messages");
      if (messages) messages.innerHTML = "";
      const welcome = `<div class="msg assistant welcome"><div class="avatar">御</div><div class="msg-body">
        <div class="msg-meta"><span>御前顾问</span><time>此刻</time></div>
        <div class="bubble"><p>阁下。臣已阅国库、市场与营建诸事。可问国是，亦可命臣议一轮建造。</p></div></div></div>`;
      if (messages) messages.innerHTML = welcome;
      toast("对话已清空");
    });
    $$("#quick-chips button").forEach((btn) => {
      btn.addEventListener("click", () => {
        const input = $("#chat-input");
        if (input) {
          input.value = btn.dataset.prompt || btn.textContent;
          input.focus();
        }
      });
    });
    setText("live-time", "连接中…");
  }

  function tickClock() {
    const el = $("#live-date");
    if (!el || !state.last) return;
    if (state.loop && state.loop.enabled && !state.loop.paused_reason) {
      const left = Math.max(0, Number(state.loop.next_at || 0) - Date.now() / 1000);
      const st = $("#loop-state");
      if (st) st.textContent = `连续：开 · 下一轮 ${left <= 0 ? "立刻" : left.toFixed(0) + "s"}`;
    }
  }

  function init() {
    bind();
    loadSettings();
    loadNames().then(() => {
      loadStrategy();
      loadCountry();
    });
    loadStatus();
    loadService();
    startPolling();
    state.clock = setInterval(tickClock, 1000);
    setInterval(loadStatus, 5000);
    setInterval(loadService, 3000);
    setInterval(loadStrategy, 10000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
