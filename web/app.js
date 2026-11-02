/* ThemeHunter 操作台前端（Vanilla JS + anime.js） */
const $ = (id) => document.getElementById(id);
const state = {
  date: "",            // 实际使用的交易日（休市日会被后端回退到上一交易日）
  requestedDate: "",   // 用户在日期框里选的日期（可能休市）
  dayInfo: null,       // /api/day 返回：是否休市、实际数据日
  themes: [],
  leaders: [],
  activeTheme: null,
  iso: null,           // 独立个股题材数据，视图来回切换时要重渲染
  view: "zt",          // zt=涨停题材 / burst=爆量题材
  burstMult: 2,        // 爆量档位：2 或 3（纯前端切换，不发请求）
  burst: null,         // /api/volume/surge 原始 payload
  burstTheme: null,
  notes: null,         // {date, day, themes:{}, stocks:{}, count}
  noteScope: "day",    // day | theme | stock
  noteKey: "",         // 当前 scope 对应的题材名 / 股票代码
  noteStock: null,     // {code, name}：点表格行选中的个股
  noteTimer: null,
  noteOpen: false,     // 笔记区是否展开（记 localStorage）
  noteAutoOpened: false,  // 只在首次渲染时按「有没有笔记」自动展开一次
};

const HOLIDAY_KIND = { weekend: "周末", holiday: "节假日" };

/* ---------- 设备判断 ---------- */
/* 用 CSS 媒体查询而不是 UA：iPad 横屏、桌面浏览器缩窗口都能正确切换 */
const isNarrow = () => window.matchMedia("(max-width: 720px)").matches;
const isTouch = () => window.matchMedia("(hover: none) and (pointer: coarse)").matches;

/* 本地日期（不能用 toISOString：那是 UTC，东八区凌晨会退一天） */
function localToday() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/* ---------- 主题切换（浅色/深色，Apple 式） ---------- */
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  $("btnTheme").textContent = theme === "dark" ? "☀" : "☾";
  localStorage.setItem("theme", theme);
}
function initTheme() {
  applyTheme(localStorage.getItem("theme") || "dark");
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  applyTheme(cur === "dark" ? "light" : "dark");
}

/* ---------- 动画封装（anime.js，非线性） ---------- */
const A = {
  stagger(elements) {
    if (!elements || !elements.length) return;
    const list = [...elements];
    /* 兜底：标签页在后台/被遮挡时 rAF 会被暂停，动画卡在 opacity:0，
       切回来之前内容一直"消失"。超时后清掉内联样式，保证最终一定可见。 */
    const guard = setTimeout(() => {
      list.forEach((el) => { el.style.opacity = ""; el.style.transform = ""; });
    }, Math.min(480 + list.length * 30 + 500, 4000));
    if (typeof anime === "undefined") { clearTimeout(guard); return; }
    anime({
      targets: elements,
      translateY: [12, 0],
      opacity: [0, 1],
      duration: 480,
      delay: anime.stagger(30),
      easing: "easeOutExpo",
      complete: () => clearTimeout(guard),
    });
  },
  pulse(el) {
    if (typeof anime === "undefined" || !el) return;
    anime({ targets: el, scale: [1, 1.05, 1], duration: 380, easing: "easeOutQuad" });
  },
  number(el, to) {
    if (typeof anime === "undefined") { el.textContent = to; return; }
    const from = parseFloat(el.textContent) || 0;
    anime({
      targets: { v: from },
      v: to,
      duration: 560,
      easing: "easeOutExpo",
      update: (a) => { el.textContent = a.animations[0].currentValue.toFixed(1); },
    });
  },
};

/* ---------- 工具 ---------- */
function setStatus(msg) { $("statusBar").textContent = msg; }
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

/* ---------- 日期解析：休市日标注 + 自动用上一交易日 ---------- */
async function applyDate(dateStr, opts = {}) {
  await flushNote();      // 切日前先把还没落库的笔记发出去（快照带原日期，不会写错天）
  state.requestedDate = dateStr;
  let info;
  try {
    info = await api(`/api/day?date=${dateStr}`);
  } catch (e) {
    // 后端日期接口异常也不能挡住复盘：按用户所选日期直连
    info = {
      requested: dateStr, trade_date: dateStr, is_trading_day: true,
      is_holiday: false, holiday_kind: "", adjusted: false, has_data: true,
    };
  }
  state.dayInfo = info;
  state.date = info.trade_date;
  state.activeTheme = null;

  // 日期框文案：休市日显示「2026-10-24（休市中）」
  $("tradeDate").value = dateStr;
  $("dateSuffix").textContent = info.is_holiday ? "（休市中）" : "";
  const chip = $("dateData");
  const kind = HOLIDAY_KIND[info.holiday_kind] || "";
  const parts = [];
  if (info.adjusted) parts.push(`数据日 ${info.trade_date}`);
  if (info.is_snapshot) parts.push("盘中快照");
  if (parts.length) {
    chip.hidden = false;
    chip.textContent = parts.join(" · ");
    chip.classList.toggle("warn", !!info.is_snapshot);
    $("dateField").title = info.adjusted
      ? `${dateStr} ${kind}休市，自动使用上一交易日 ${info.trade_date} 的数据`
      : "选择交易日";
  } else {
    chip.hidden = true;
    chip.textContent = "";
    chip.classList.remove("warn");
    $("dateField").title = "选择交易日";
  }

  if (!opts.silent) {
    if (info.is_snapshot && !info.adjusted) {
      setStatus(`${dateStr} 为盘中快照（数据不完整），收盘后请重新采集`);
    } else if (info.adjusted && info.has_data) {
      setStatus(`${dateStr} ${kind}休市 · 已用 ${info.trade_date} 数据`);
    } else if (info.adjusted) {
      setStatus(`${dateStr} ${kind}休市 · ${info.trade_date} 无数据，请点采集`);
    } else if (info.is_holiday) {
      setStatus(`${dateStr} ${kind}休市 · 暂无数据`);
    } else {
      setStatus(`已切换到 ${dateStr}`);
    }
  }
  await loadResults();
}

/* ---------- 数据加载 ---------- */
async function loadResults() {
  const d = state.date;
  if (!d) return;
  const [themes, leaders, iso, notes] = await Promise.all([
    api(`/api/themes?date=${d}`),
    api(`/api/leaders?date=${d}`),
    api(`/api/isolated?date=${d}`).catch(() => null),
    api(`/api/notes?date=${d}`).catch(() => null),
  ]);
  // 独立个股题材：1~2 只题材里「没在别的板块出现过」的股票，从主列表拆出来。
  // hidden 是成员全在别处露过面的小题材，主列表也不再显示（避免占位）。
  const isoItems = (iso && iso.items) || [];
  const hidden = new Set((iso && iso.hidden) || []);
  const isoNames = new Set([...isoItems.map((i) => i.theme_name), ...hidden]);
  state.themes = themes.filter((t) => !isoNames.has(t.theme_name));
  state.leaders = leaders;
  state.iso = iso;
  state.notes = notes || { date: d, day: "", themes: {}, stocks: {}, count: 0 };
  state.noteStock = null;
  renderNoteBar();

  loadBreadth(d);                     // 情绪宽度两个视图共用，放分叉之前
  if (state.view === "burst") {       // 爆量视图只多拉一个接口，不跑涨停那套渲染
    await loadBurst(d);
    return;
  }
  renderThemes();
  renderIsolated(iso);
  loadUnassigned(d);
  if (state.activeTheme) renderLeaders(state.activeTheme);
  else if (state.themes.length) selectTheme(state.themes[0].theme_name);
}

/* 独立个股题材：默认收起，点标题展开；点条目照样能在右侧看梯队 */
function renderIsolated(iso) {
  const box = $("isolatedBox");
  const items = (iso && iso.items) || [];
  $("isolatedCount").textContent = items.length;
  if (!items.length) { box.hidden = true; return; }
  box.hidden = false;
  $("isolatedList").innerHTML = items.map((it) => `
    <div class="iso-item" data-theme="${esc(it.theme_name)}" title="点击查看梯队">
      <span class="iso-theme">${esc(it.theme_name)}</span>
      <span class="iso-count">${it.count}</span>
      <span class="iso-stocks">${it.members.map((m) => esc(m.name)).join("、")}</span>
    </div>`).join("");
  $("isolatedList").onclick = (e) => {
    const el = e.target.closest(".iso-item");
    if (el) selectTheme(el.dataset.theme);
  };
  $("btnIsoToggle").onclick = () => {
    const list = $("isolatedList");
    const open = list.hidden;
    list.hidden = !open;
    $("btnIsoToggle").querySelector(".iso-caret").textContent = open ? "▾" : "▸";
  };
}

/* 全市场情绪宽度：涨幅>=9.90 / 涨停 / 创科北>10 */
async function loadBreadth(d) {
  try {
    const r = await api(`/api/breadth?date=${d}`);
    renderBreadth(r);
  } catch (e) { /* 静默，不影响主流程 */ }
}

function renderBreadth(r) {
  const set = (id, v) => {
    const el = $(id);
    if (!el) return;
    const na = (v === null || v === undefined);
    el.textContent = na ? "—" : v;
    el.classList.toggle("na", na);
  };
  set("bdUp", r.up_99);
  set("bdZt", r.limit_up);
  set("bdTech", r.tech_10);
  set("bdAssigned", r.assigned);
  const tip = r.partial
    ? (r.hint || "该日没有全市场快照")
    : `涨幅≥9.90%：${r.up_99} 只\n涨停（封板）：${r.limit_up} 只\n创/科/北 >10%：${r.tech_10} 只\n已归纳：${r.assigned} 只（进了任一题材，含概念补全）\n快照 ${r.total} 只 · ${r.captured_at}`;
  ["bdUpCard", "bdZtCard", "bdTechCard", "bdAssignedCard"].forEach((id) => {
    const el = $(id);
    if (el) el.title = tip;
  });
  const hint = $("bdHint");
  if (hint) hint.textContent = r.partial ? (r.hint || "") : "";
}

/* 未归纳个股：涨停但没归入任何 >=2 只主线板块的（供人工观察） */
async function loadUnassigned(d) {
  try {
    const r = await api(`/api/unassigned?date=${d}`);
    renderUnassigned(r);
  } catch (e) { /* 静默 */ }
}

function renderUnassigned(r) {
  $("unassignedCount").textContent = r.count;
  const box = $("unassignedList");
  if (!r.count) {
    box.innerHTML = `<div class="unassigned-empty">今日涨停股均已归纳到题材</div>`;
    return;
  }
  r.stocks.forEach((s) => {
    const row = document.createElement("div");
    row.className = "unassigned-item";
    row.title = "点击查看详情 · 悬停看最近涨停";
    row.innerHTML = `
      <span class="unassigned-code">${s.code}</span>
      <span class="unassigned-name">${esc(s.name)}</span>
      ${s.status !== "涨停" ? `<span class="unassigned-status">异动${Number(s.high_pct).toFixed(1)}%</span>` : ""}
      <span class="unassigned-reason">${esc(s.reason || "无原因")}</span>
      <button class="unassigned-add" title="手动归纳到题材">归纳</button>`;
    row.onclick = (e) => {
      if (e.target.closest(".unassigned-add")) return;
      pickStock(s.code);
    };
    row.querySelector(".unassigned-add").onclick = (e) => {
      e.stopPropagation();
      openAssign(s.code, s.name);
    };
    bindRowCard(row, s.code);
    box.appendChild(row);
  });
}

/* ---------- 手动归纳到题材 ---------- */
let assignCode = null;
let assignName = "";

function openAssign(code, name) {
  assignCode = code;
  assignName = name;
  $("assignSub").textContent = `${name} ${code}`;
  $("assignFilter").value = "";
  renderAssignList("");
  $("assignModal").classList.add("open");
  setTimeout(() => { const f = $("assignFilter"); if (f) f.focus(); }, 40);
}

function closeAssign() { $("assignModal").classList.remove("open"); }

function renderAssignList(q) {
  const box = $("assignList");
  if (!box) return;
  const kw = (q || "").trim();
  const list = state.themes.filter((t) => !kw || t.theme_name.includes(kw)).slice(0, 80);
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = `<div class="assign-empty">没有匹配的题材</div>`;
    return;
  }
  list.forEach((t) => {
    const item = document.createElement("div");
    item.className = "assign-item";
    item.innerHTML = `<span class="assign-name">${esc(t.theme_name)}</span>
                      <span class="assign-count">${t.zt_count} 只涨停</span>`;
    item.onclick = () => doAssign(t.theme_name);
    box.appendChild(item);
  });
}

async function doAssign(theme) {
  if (!assignCode) return;
  const code = assignCode;
  try {
    const r = await post("/api/override/add_to_theme", { date: state.date, theme, code });
    closeAssign();
    cardCache.clear();                       // 归属变了，缓存作废
    setStatus(r.added ? `已把 ${assignName}(${code}) 归纳到「${theme}」` : (r.hint || "未变更"));
    await loadResults();
  } catch (e) {
    setStatus("归纳失败: " + e.message);
  }
}

function renderThemes() {
  $("themeCount").textContent = `${state.themes.length} 个题材 · 按涨停+异动`;
  const list = $("themeList");
  list.innerHTML = "";
  state.themes.forEach((t) => list.appendChild(makeThemeBlock(t)));
  A.stagger(list.querySelectorAll(".theme-item"));
  // 渲染后判断是否还有内容在视口外，决定要不要提示「向下滑动」
  requestAnimationFrame(updateScrollHint);
}

/* 列表超出可视高度且没滚到底时，显示「↓ 向下滑动查看更多」提示 */
function updateScrollHint() {
  const list = $("themeList");
  const hint = $("themeScrollHint");
  if (!list || !hint) return;
  const canScroll = list.scrollHeight > list.clientHeight + 4;
  const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 4;
  hint.classList.toggle("show", canScroll && !atBottom);
}

/* 题材块 = 标题行 + 可展开的龙头区。
   桌面用不上展开区（右侧一直是表格），手机才就地展开。 */
function makeThemeBlock(t) {
  const wrap = document.createElement("div");
  wrap.className = "theme-block";
  wrap.dataset.theme = t.theme_name;

  const zt = t.zt_count || 0;
  const strong = t.strong_count || 0;
  const total = zt + strong;
  const split = strong ? `<i class="t-split">${zt}+${strong}</i>` : "";

  const item = document.createElement("div");
  item.className = "theme-item";
  item.title = `${t.theme_name}：涨停 ${zt} 只` +
    (strong ? ` + 创/科异动 ${strong} 只 = ${total} 只` : "") + "（按涨停+异动排序）";
  item.innerHTML = `<span class="t-name">${t.theme_name}
                     <span class="t-count">${total} 只${split}</span></span>
                     <span class="t-caret">▸</span>`;
  item.onclick = () => onThemeClick(wrap);

  const detail = document.createElement("div");
  detail.className = "theme-detail";

  wrap.append(item, detail);
  return wrap;
}

function onThemeClick(wrap) {
  const theme = wrap.dataset.theme;
  const wasOpen = wrap.classList.contains("open");
  const list = $("themeList");

  // 手风琴：同时只展开一个，页面不会越拉越长
  list.querySelectorAll(".theme-block.open").forEach((b) => b.classList.remove("open"));
  list.querySelectorAll(".theme-block.active").forEach((b) => b.classList.remove("active"));

  if (wasOpen) {                    // 再点一次 = 收起
    state.activeTheme = null;
    hideCard();
    return;
  }

  wrap.classList.add("active");
  state.activeTheme = theme;
  if (state.noteScope === "theme") syncNoteInput();

  if (isNarrow()) {
    wrap.classList.add("open");
    renderDetail(wrap);
    /* 只保证第一行龙头露出来，不做整页跳转 */
    const first = wrap.querySelector(".theme-detail").firstElementChild;
    if (first) {
      const fr = first.getBoundingClientRect();
      if (fr.bottom > window.innerHeight - 8) {
        window.scrollBy({ top: fr.top - window.innerHeight * 0.52, behavior: "smooth" });
      }
    }
  } else {
    renderLeaders(theme);
  }
}

/* 展开区里的龙头：两行卡片，手机上点一下可看个股详情 */
function renderDetail(wrap) {
  const theme = wrap.dataset.theme;
  const detail = wrap.querySelector(".theme-detail");
  const rows = state.leaders
    .filter((l) => l.theme_name === theme)
    .sort((a, b) => a.final_rank - b.final_rank);
  detail.innerHTML = "";
  if (!rows.length) {
    detail.innerHTML = `<div class="ldr-empty">这个题材暂无龙头</div>`;
    return;
  }
  rows.forEach((l) => {
    const row = document.createElement("div");
    row.className = "ldr-row";
    const isLeader = !!l.leader_label;
    const badgeCls = "rank-badge" + (isLeader ? "" : " dim") + (l.is_override ? " overridden" : "");
    const label = isLeader ? l.leader_label : "#" + l.final_rank;
    const fd = l.float_mv > 0 ? (l.fengdan_amt / l.float_mv * 100).toFixed(2) + "%" : "-";
    const pct = Number(l.high_pct) || 0;
    row.innerHTML = `
      <span class="ldr-rank"><span class="${badgeCls}">${label}</span></span>
      <span class="ldr-main">
        <span class="stock-code">${l.code}</span><span class="stock-name">${l.name}</span>
      </span>
      <span class="ldr-lb">${l.lianban_height ? l.lianban_height + "板" : "-"}</span>
      <span class="ldr-pct">${pct ? pct.toFixed(2) + "%" : "-"}</span>
      <span class="ldr-sub">封单 ${fd} · 上板 ${l.zt_time || "-"}</span>`;
    bindRowCard(row, l.code);
    row.addEventListener("click", () => selectStockForNote(l.code, l.name));
    detail.appendChild(row);
  });
}

function selectTheme(theme) {
  flushNote();
  state.activeTheme = theme;
  const list = $("themeList");
  list.querySelectorAll(".theme-block.active").forEach((b) => b.classList.remove("active"));
  const cur = [...list.querySelectorAll(".theme-block")]
    .find((b) => b.dataset.theme === theme);
  if (cur) cur.classList.add("active");
  renderLeaders(theme);
  if (state.noteScope === "theme") syncNoteInput();   // 题材页签跟着板块走
}

function renderLeaders(theme) {
  hideCard();
  const themeInfo = state.themes.find((t) => t.theme_name === theme);
  const countHint = themeInfo
    ? `${themeInfo.zt_count} 只涨停` +
      (themeInfo.strong_count ? ` + ${themeInfo.strong_count} 异动` : "") + " · "
    : "";
  $("leaderTitle").innerHTML = `龙头梯队 · ${theme} <span class="hint">${countHint}可调序/移出</span>`;
  const tbody = $("leaderTable").querySelector("tbody");
  tbody.innerHTML = "";
  const rows = state.leaders
    .filter((l) => l.theme_name === theme)
    .sort((a, b) => a.final_rank - b.final_rank);
  // 按二级细分分组（sub_tags 第一个为主细分），组顺序按组内最高龙序；
  // 没有细分的（纯一级归属）归到最后一组，不显示标题
  const groups = new Map();
  rows.forEach((l) => {
    const tags = String(l.sub_tags || "").split(",").filter(Boolean);
    l._subs = tags;
    const key = tags[0] || "";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(l);
  });
  const ordered = [...groups.entries()].sort((a, b) => {
    if (!a[0]) return 1;          // 无细分永远排最后
    if (!b[0]) return -1;
    return a[1][0].final_rank - b[1][0].final_rank;
  });
  const multi = ordered.filter(([k]) => k).length > 1;
  ordered.forEach(([tag, list]) => {
    if (tag && multi) {
      const head = document.createElement("tr");
      head.className = "sub-head";
      head.innerHTML = `<td colspan="6"><span class="sub-title">${esc(tag)}</span>` +
        `<span class="sub-count">${list.length}</span></td>`;
      tbody.appendChild(head);
    }
    list.forEach((l) => {
      const tr = document.createElement("tr");
      const isLeader = !!l.leader_label;
      const badgeCls = "rank-badge" + (isLeader ? "" : " dim") + (l.is_override ? " overridden" : "");
      const label = isLeader ? l.leader_label : "#" + l.final_rank;
      const fd = l.float_mv > 0 ? (l.fengdan_amt / l.float_mv * 100).toFixed(2) + "%" : "-";
      const pct = Number(l.high_pct) || 0;
      const extra = (l._subs || []).slice(1, 3)
        .map((s) => `<span class="sub-extra">${esc(s)}</span>`).join("");
      tr.innerHTML = `
        <td><span class="${badgeCls}">${label}</span>
            ${l.is_override ? `<span class="sub-rank">原#${l.algo_rank}</span>` : ""}</td>
        <td><span class="stock-code">${l.code}</span><span class="stock-name">${l.name}</span>${extra}</td>
        <td>${l.lianban_height ? l.lianban_height + "板" : "-"}</td>
        <td class="pct">${pct ? pct.toFixed(2) + "%" : "-"}</td>
        <td>${fd}</td><td>${l.zt_time || "-"}</td>`;
      tbody.appendChild(tr);
      bindRowCard(tr, l.code);
      tr.addEventListener("click", () => selectStockForNote(l.code, l.name));
    });
  });
  A.stagger(tbody.querySelectorAll("tr"));
}

/* ---------- 爆量题材（当日量 >= 前5日最低量 × 2/3） ---------- */
async function loadBurst(d) {
  const hint = $("burstHint");
  if (hint) { hint.hidden = false; hint.textContent = "加载爆量数据…"; }
  // 先清空：否则加载期间上一个视图的涨停题材列表会挂在「爆量板块」标题下，明显误导
  $("themeList").innerHTML = `<div class="unassigned-empty">加载中…</div>`;
  $("themeCount").textContent = "";
  let r;
  try {
    r = await api(`/api/volume/surge?date=${d}`);
  } catch (e) {
    if (hint) hint.textContent = "爆量数据加载失败: " + e.message;
    return;
  }
  state.burst = r;
  if (!r.ready) {
    $("themeCount").textContent = "";
    $("themeList").innerHTML =
      `<div class="unassigned-empty">${esc(r.hint || "该日还没有成交量数据")}</div>`;
    clearBurstTable();
    if (hint) { hint.hidden = false; hint.textContent = r.hint || ""; }
    return;
  }
  if (hint) {
    const bw = (r.base_window || []).map((x) => x.slice(5)).join(" / ");
    hint.textContent = (bw ? `${r.base_desc}（${bw}）` : r.base_desc) +
      (r.partial ? " · 盘中快照，量不完整" : "");
    hint.classList.toggle("warn", !!r.partial);
  }
  renderBurstThemes();
}

function burstSortedThemes() {
  const m = state.burstMult;
  return (state.burst.themes || [])
    .filter((t) => (t["count_" + m] || 0) > 0)
    .sort((a, b) => ((b["count_" + m] || 0) - (a["count_" + m] || 0)) ||
                    ((b["max_" + m] || 0) - (a["max_" + m] || 0)));
}

function renderBurstThemes() {
  const list = $("themeList");
  list.innerHTML = "";
  const m = state.burstMult;
  const th = burstSortedThemes();
  const tot = (state.burst && state.burst.totals) || {};
  $("themeCount").textContent =
    `${th.length} 个板块 · ≥${m}倍 · 去重 ${tot["surge_" + m] || 0} 只` +
    (tot["unmapped_" + m] ? ` · 未归类 ${tot["unmapped_" + m]}` : "");
  th.forEach((t) => list.appendChild(makeBurstBlock(t)));
  A.stagger(list.querySelectorAll(".theme-item"));
  requestAnimationFrame(updateScrollHint);
  if (th.length) selectBurstTheme(th[0].theme_name);
  else clearBurstTable();
}

function makeBurstBlock(t) {
  const m = state.burstMult;
  const n = t["count_" + m] || 0;
  const mx = t["max_" + m] || 0;
  const zt = t.zt_2 || 0;
  const wrap = document.createElement("div");
  wrap.className = "theme-block";
  wrap.dataset.theme = t.theme_name;

  const item = document.createElement("div");
  item.className = "theme-item";
  item.title = `${t.theme_name}：≥${m}倍爆量 ${n} 只` +
    (zt ? `（其中涨停 ${zt} 只）` : "") + `，板块内最高 ${mx.toFixed(2)} 倍`;
  item.innerHTML = `<span class="t-name">${esc(t.theme_name)}
      <span class="t-count">${n} 只${zt ? `<i class="t-split">涨停${zt}</i>` : ""}\
<i class="t-ratio">${mx.toFixed(1)}×</i></span></span>
      <span class="t-caret">▸</span>`;
  item.onclick = () => onBurstClick(wrap);

  const detail = document.createElement("div");
  detail.className = "theme-detail";
  wrap.append(item, detail);
  return wrap;
}

function onBurstClick(wrap) {
  const list = $("themeList");
  const wasOpen = wrap.classList.contains("open");
  list.querySelectorAll(".theme-block.open").forEach((b) => b.classList.remove("open"));
  list.querySelectorAll(".theme-block.active").forEach((b) => b.classList.remove("active"));
  if (wasOpen) { state.burstTheme = null; hideCard(); return; }
  wrap.classList.add("active");
  state.burstTheme = wrap.dataset.theme;
  if (isNarrow()) {
    wrap.classList.add("open");
    renderBurstDetail(wrap);
    const first = wrap.querySelector(".theme-detail").firstElementChild;
    if (first) {
      const fr = first.getBoundingClientRect();
      if (fr.bottom > window.innerHeight - 8) {
        window.scrollBy({ top: fr.top - window.innerHeight * 0.52, behavior: "smooth" });
      }
    }
  } else {
    renderBurstTable(wrap.dataset.theme);
  }
}

function burstRows(theme) {
  const m = state.burstMult;
  return (state.burst.stocks || [])
    .filter((s) => s.theme === theme && s.ratio >= m)
    .sort((a, b) => b.ratio - a.ratio);
}

/* 成交量（股）格式化：亿 / 万 */
function fmtVol(v) {
  const n = Number(v) || 0;
  if (!n) return "-";
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return String(Math.round(n));
}

function burstTip(s) {
  const src = s.src === "reason"
    ? `涨停原因（${s.reason_date}）：${s.reason}`
    : s.src === "zt_today" ? "当日已在涨停池，归属沿用涨停题材"
      : s.src === "concept" ? "无涨停原因，按同花顺概念归类" : "未归类";
  return src + (s.suspect ? " · 窗口数据不全" : "");
}

/* 手机：就地展开，复用龙头行的两行卡片布局，零新增 CSS */
function renderBurstDetail(wrap) {
  const detail = wrap.querySelector(".theme-detail");
  const rows = burstRows(wrap.dataset.theme);
  detail.innerHTML = "";
  if (!rows.length) {
    detail.innerHTML = `<div class="ldr-empty">该倍数下没有个股</div>`;
    return;
  }
  rows.forEach((s) => {
    const row = document.createElement("div");
    row.className = "ldr-row";
    row.title = burstTip(s);
    row.innerHTML = `
      <span class="ldr-rank"><span class="burst-ratio">${s.ratio.toFixed(1)}×</span></span>
      <span class="ldr-main"><span class="stock-code">${s.code}</span>
        <span class="stock-name">${esc(s.name)}</span></span>
      <span class="ldr-lb">${fmtVol(s.volume)}</span>
      <span class="ldr-pct">${Number(s.high_pct).toFixed(1)}%</span>
      <span class="ldr-sub">基准 ${fmtVol(s.base_volume)}（${(s.base_date || "").slice(5)}）</span>`;
    bindRowCard(row, s.code);
    row.addEventListener("click", () => selectStockForNote(s.code, s.name));
    detail.appendChild(row);
  });
}

function selectBurstTheme(theme) {
  flushNote();
  state.burstTheme = theme;
  const list = $("themeList");
  list.querySelectorAll(".theme-block.active").forEach((b) => b.classList.remove("active"));
  const cur = [...list.querySelectorAll(".theme-block")].find((b) => b.dataset.theme === theme);
  if (cur) cur.classList.add("active");
  renderBurstTable(theme);
  if (state.noteScope === "theme") syncNoteInput();
}

function renderBurstTable(theme) {
  hideCard();
  const rows = burstRows(theme);
  const m = state.burstMult;
  const info = (state.burst.themes || []).find((t) => t.theme_name === theme);
  const mx = info ? (info["max_" + m] || 0) : 0;
  $("leaderTitle").innerHTML = `爆量明细 · ${esc(theme)} ` +
    `<span class="hint">${rows.length} 只 · 最高 ${mx.toFixed(2)} 倍</span>`;
  const tbody = $("burstTable").querySelector("tbody");
  tbody.innerHTML = "";
  rows.forEach((s) => {
    const tr = document.createElement("tr");
    const srcTag = s.src === "reason" ? "原因"
      : s.src === "zt_today" ? "涨停" : (s.src === "concept" ? "概念" : "—");
    tr.title = burstTip(s);
    tr.innerHTML = `
      <td><span class="stock-code">${s.code}</span><span class="stock-name">${esc(s.name)}</span>
          <span class="burst-src">${srcTag}</span></td>
      <td class="pct">${Number(s.high_pct).toFixed(2)}%</td>
      <td class="burst-vol">${fmtVol(s.volume)}</td>
      <td class="burst-vol">${fmtVol(s.base_volume)}</td>
      <td class="burst-ratio">${s.ratio.toFixed(2)}×</td>
      <td class="burst-date">${(s.base_date || "").slice(5)}</td>`;
    tbody.appendChild(tr);
    bindRowCard(tr, s.code);
    tr.addEventListener("click", () => selectStockForNote(s.code, s.name));
  });
  A.stagger(tbody.querySelectorAll("tr"));
}

function clearBurstTable() {
  $("burstTable").querySelector("tbody").innerHTML = "";
  $("leaderTitle").innerHTML = `爆量明细 <span class="hint">← 点击左侧板块查看</span>`;
}

/* 视图切换：只切 DOM 显隐 + 重渲染，涨停那套状态原样保留 */
async function setView(v) {
  if (state.view === v) return;
  state.view = v;
  document.body.classList.toggle("burst", v === "burst");
  document.querySelectorAll("#viewScope .seg-btn")
    .forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  $("themeHeadLabel").textContent = v === "burst" ? "爆量板块" : "题材强度";
  $("burstMult").hidden = v !== "burst";
  $("burstHint").hidden = v !== "burst";
  $("leaderTable").hidden = v === "burst";
  $("burstTable").hidden = v !== "burst";
  const cand = $("btnCandidates");
  if (cand) cand.hidden = v === "burst";
  hideCard();
  // 两张表共用 right 容器，不归零会继承上一张表的滚动位置（表头被顶出视口）
  const right = document.querySelector("main .right");
  if (right) right.scrollTop = 0;
  const scroller = document.querySelector(".theme-scroll");
  if (scroller) scroller.scrollTop = 0;

  if (v === "burst") {
    state.activeTheme = null;
    $("leaderTitle").innerHTML = `爆量明细 <span class="hint">← 点击左侧板块查看</span>`;
    if (state.date) await loadBurst(state.date);
  } else {
    state.burstTheme = null;
    renderThemes();
    renderIsolated(state.iso);
    if (state.date) loadUnassigned(state.date);
    if (state.themes.length) selectTheme(state.themes[0].theme_name);
  }
}

/* ---------- 复盘笔记（日 / 题材 / 个股三级） ---------- */
/* 保存一律用「输入时的快照」，不是保存时的 state——
   否则切日期/切板块之后，防抖才触发，内容会写到错误的日期上。 */
let notePending = null;          // {date, scope, key, content}

function noteKeyNow() {
  if (state.noteScope === "theme") return state.activeTheme || "";
  if (state.noteScope === "stock") return state.noteStock ? state.noteStock.code : "";
  return "";
}

function noteValueOf(scope, key) {
  const n = state.notes || {};
  if (scope === "day") return n.day || "";
  if (scope === "theme") return (n.themes || {})[key] || "";
  return (n.stocks || {})[key] || "";
}

function noteCountOf(n) {
  return ((n && n.day) ? 1 : 0)
    + Object.keys((n && n.themes) || {}).length
    + Object.keys((n && n.stocks) || {}).length;
}

function renderNoteBar() {
  const bar = $("noteBar");
  if (!bar) return;
  // 只在本会话首次渲染时按「有没有笔记」自动展开一次，之后听用户的
  if (!state.noteAutoOpened) {
    state.noteAutoOpened = true;
    if (noteCountOf(state.notes)) state.noteOpen = true;
  }
  $("noteBody").hidden = !state.noteOpen;
  bar.classList.toggle("open", state.noteOpen);
  $("noteToggle").textContent = state.noteOpen ? "▴" : "▾";
  syncNoteInput();
}

function syncNoteInput() {
  const tabs = $("noteTabs");
  const input = $("noteInput");
  if (!tabs || !input) return;

  tabs.querySelector('[data-scope="stock"]').hidden = !state.noteStock;
  // 上下文没了就退回「本日」，避免写到一个不存在的 key 上
  if (state.noteScope === "theme" && !state.activeTheme) state.noteScope = "day";
  if (state.noteScope === "stock" && !state.noteStock) state.noteScope = "day";

  state.noteKey = noteKeyNow();
  [...tabs.querySelectorAll(".seg-btn")]
    .forEach((b) => b.classList.toggle("on", b.dataset.scope === state.noteScope));

  input.value = noteValueOf(state.noteScope, state.noteKey);
  input.placeholder = state.noteScope === "day"
    ? "今天怎么想的…（失焦自动保存，清空即删除）"
    : state.noteScope === "theme"
      ? `关于「${state.activeTheme}」的看法…`
      : `关于 ${state.noteStock ? state.noteStock.name : ""} 的看法…`;

  const cnt = noteCountOf(state.notes);
  $("noteMeta").textContent = input.value.length
    ? `${input.value.length} 字`
    : (cnt ? `本日 ${cnt} 条` : "");
  loadNoteHistory();
}

async function loadNoteHistory() {
  const box = $("noteHistory");
  if (!box) return;
  if (state.noteScope === "day" || !state.noteKey || !state.date) {
    box.hidden = true;
    return;
  }
  const q = `date=${state.date}&scope=${state.noteScope}` +
            `&key=${encodeURIComponent(state.noteKey)}`;
  try {
    const r = await api(`/api/notes?${q}`);
    const hist = (r.history || []).filter((h) => h.trade_date !== state.date);
    if (!hist.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = `<div class="nh-head">历史 · ${hist.length} 条</div>` +
      hist.map((h) => `<div class="nh-item">
        <span class="nh-date">${h.trade_date.slice(5)}</span>
        <span class="nh-text">${esc(h.content)}</span></div>`).join("");
  } catch (e) {
    box.hidden = true;
  }
}

function setNoteScope(scope) {
  if (!["day", "theme", "stock"].includes(scope)) return;
  if (scope === "theme" && !state.activeTheme) return;
  if (scope === "stock" && !state.noteStock) return;
  flushNote();                    // 上一页签没存完的先发出去
  state.noteScope = scope;
  syncNoteInput();
}

/* 点表格行 = 选中个股（只记录，不打扰：不自动切页签、不自动展开） */
function selectStockForNote(code, name) {
  state.noteStock = { code, name };
  const tab = $("noteTabs").querySelector('[data-scope="stock"]');
  if (tab) tab.hidden = false;
  if (state.noteScope === "stock") syncNoteInput();
}

function applyNoteLocally(p, r) {
  const n = state.notes;
  if (!n) return;
  const text = (p.content || "").trim();
  if (p.scope === "day") {
    n.day = text;
  } else {
    const bucket = p.scope === "theme"
      ? (n.themes = n.themes || {}) : (n.stocks = n.stocks || {});
    if (text) bucket[p.key] = text;
    else delete bucket[p.key];
  }
  n.count = noteCountOf(n);
}

async function flushNote() {
  if (state.noteTimer) { clearTimeout(state.noteTimer); state.noteTimer = null; }
  const p = notePending;
  notePending = null;
  if (!p || !p.date) return;
  if (p.scope !== "day" && !p.key) return;
  try {
    const r = await post("/api/note", p);
    applyNoteLocally(p, r);
    const t = new Date().toLocaleTimeString("zh-CN", { hour12: false }).slice(0, 5);
    if ($("noteMeta")) $("noteMeta").textContent = r.deleted ? "已清空" : `已保存 ${t}`;
  } catch (e) {
    if ($("noteMeta")) $("noteMeta").textContent = "保存失败: " + e.message;
  }
}

/* ---------- 个股悬浮卡：悬停梯队看涨停原因 / 题材 / 同题材共性 ---------- */
const cardCache = new Map();
let cardTimer = null;

function ensureCard() {
  let el = $("stockCard");
  if (!el) {
    el = document.createElement("div");
    el.id = "stockCard";
    el.className = "stock-card";
    document.body.appendChild(el);
  }
  return el;
}

function hideCard() {
  const el = $("stockCard");
  if (el) el.classList.remove("show");
}

function placeCard(el, x, y) {
  const gap = 14;
  const r = el.getBoundingClientRect();
  let left = x + gap;
  let top = y + gap;
  if (left + r.width > window.innerWidth - 10) left = x - r.width - gap;
  if (top + r.height > window.innerHeight - 10) top = window.innerHeight - r.height - 10;
  el.style.left = Math.max(10, left) + "px";
  el.style.top = Math.max(10, top) + "px";
}

function bindRowCard(tr, code) {
  tr.dataset.code = code;
  /* 触屏设备没有 hover：点一下直接把详情卡推到屏幕底部 */
  if (isTouch()) {
    tr.addEventListener("click", () => showCard(code, 0, 0, true));
    return;
  }
  tr.addEventListener("mouseenter", (e) => {
    clearTimeout(cardTimer);
    const { clientX: x, clientY: y } = e;
    cardTimer = setTimeout(() => showCard(code, x, y), 320);
  });
  tr.addEventListener("mousemove", (e) => {
    const el = $("stockCard");
    if (el && el.classList.contains("show")) placeCard(el, e.clientX, e.clientY);
  });
  tr.addEventListener("mouseleave", () => {
    clearTimeout(cardTimer);
    hideCard();
  });
}

async function showCard(code, x, y, sheet) {
  const key = `${state.date}:${code}`;
  const el = ensureCard();
  let c = cardCache.get(key);
  if (!c) {
    el.innerHTML = `<div class="sc-loading">加载中…</div>`;
    el.classList.add("show");
    if (!sheet) placeCard(el, x, y);
    try {
      c = await api(`/api/stock/card?date=${state.date}&code=${code}`);
      cardCache.set(key, c);
    } catch (err) {
      el.innerHTML = `<div class="sc-loading">${esc(err.message)}</div>`;
      return;
    }
  }
  el.innerHTML = cardHtml(c);
  el.classList.add("show");
  if (!sheet) placeCard(el, x, y);
}

function cardHtml(c) {
  const pct = Number(c.high_pct) || 0;
  const kws = (c.keywords || []).map((k) => `<span class="sc-kw">${esc(k)}</span>`).join("");
  const themes = (c.themes || []).map((t) =>
    `<span class="sc-theme">${esc(t.name)}${t.label ? `<i>${esc(t.label)}</i>` : ""}</span>`).join("");
  const common = (c.common || []).map((x) =>
    `<span class="sc-common">${esc(x.kw)}<i>×${x.n}</i></span>`).join("");
  const peers = (c.peers || []).map((p) =>
    `<span class="sc-peer">${esc(p.name)}${p.lianban ? `<i>${p.lianban}板</i>` : ""}</span>`).join("");
  const cpts = (c.concepts || []).map((n) => `<span class="sc-cpt">${esc(n)}</span>`).join("");
  const recent = (c.recent || []).length
    ? `<div class="sc-sec"><div class="sc-lab">最近涨停 <span class="sc-note">近 ${c.recent.length} 次</span></div>
        <div class="sc-recent">${c.recent.map((x) => `<div class="sc-recent-row">
          <span class="sc-rd">${esc((x.date || "").slice(5))}</span>
          <span class="sc-rt">${esc(x.zt_time || "-")}</span>
          <span class="sc-rr">${esc(x.reason || "无原因")}</span></div>`).join("")}</div></div>`
    : "";
  const myNote = c.note
    ? `<div class="sc-sec"><div class="sc-lab">我的笔记</div>
        <div class="sc-mynote">${esc(c.note)}</div></div>`
    : "";
  const fmv = c.float_mv ? (c.float_mv / 1e8).toFixed(1) + "亿" : "-";
  return `
    <div class="sc-head">
      <span class="sc-name">${esc(c.name)}</span>
      <span class="sc-status s${c.sealed}">${esc(c.status)}</span>
    </div>
    <div class="sc-sub">
      <span class="sc-code">${esc(c.code)}</span>
      <span class="sc-pct">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</span>
      <span class="sc-lb">${c.lianban_height ? c.lianban_height + "板" : "首板"}</span>
    </div>
    ${kws ? `<div class="sc-sec"><div class="sc-lab">涨停原因</div><div class="sc-kws">${kws}</div></div>` : ""}
    ${recent}
    ${myNote}
    ${themes ? `<div class="sc-sec"><div class="sc-lab">所属题材</div><div class="sc-kws">${themes}</div></div>` : ""}
    ${common ? `<div class="sc-sec"><div class="sc-lab">同题材共性 <span class="sc-note">算法归纳 · 非新闻</span></div>
       <div class="sc-kws">${common}</div>${peers ? `<div class="sc-peers">${peers}</div>` : ""}</div>` : ""}
    ${cpts ? `<div class="sc-sec"><div class="sc-lab">同花顺概念 <span class="sc-note">共 ${c.concept_total} 个</span></div>
       <div class="sc-kws">${cpts}</div></div>` : ""}
    <div class="sc-foot">封单 ${c.fengdan_pct ? c.fengdan_pct.toFixed(2) + "%" : "-"}
      · 换手 ${c.turnover ? c.turnover.toFixed(1) + "%" : "-"}
      · 上板 ${esc(c.zt_time || "-")} · 流通 ${fmv}</div>`;
}

/* ---------- 历史回顾（自定义下拉，避免原生 option 在 Windows 上样式失效） ---------- */
async function loadDates() {
  try {
    const dates = await api("/api/dates");
    const menu = $("historyMenu");
    menu.innerHTML = "";
    dates.forEach((row) => {
      const d = typeof row === "string" ? row : row.date;
      const holiday = typeof row === "string" ? false : !row.is_trading_day;
      const item = document.createElement("div");
      item.className = "dropdown-item";
      item.textContent = holiday ? `${d}（休市中）` : d;
      item.onclick = () => {
        closeDropdown();
        applyDate(d).catch((e) => setStatus("加载失败: " + e.message));
      };
      menu.appendChild(item);
    });
  } catch (e) { /* 无历史数据时静默 */ }
}

function toggleDropdown() {
  $("historyDropdown").classList.toggle("open");
}
function closeDropdown() {
  $("historyDropdown").classList.remove("open");
}

/* ---------- 个股查询（验证归类准不准） ---------- */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function searchStock() {
  const q = $("stockSearch").value.trim();
  if (!q) return;
  setStatus(`查询「${q}」…`);
  try {
    const r = await api(`/api/stock/search?date=${state.date}&q=${encodeURIComponent(q)}`);
    renderSearch(r);
    setStatus(`查询「${q}」：${r.results.length} 条`);
  } catch (e) { setStatus("查询失败: " + e.message); }
}

function renderSearch(r) {
  $("searchTitle").textContent = `查询「${r.query}」 · ${r.date}`;
  $("searchBody").innerHTML = r.results.length
    ? r.results.map(stockCard).join("")
    : `<div class="sc-note">${esc(r.note || "未找到该股票")}</div>`;
  $("searchModal").classList.add("open");
}

function closeSearch() { $("searchModal").classList.remove("open"); }

/* ---------- 搜索实时建议（输入即搜，不用回车） ---------- */
async function suggestStock(q) {
  try {
    const r = await api(`/api/stock/search?date=${state.date}&q=${encodeURIComponent(q)}`);
    renderSuggest(r);
  } catch (e) { /* 静默失败，不打扰输入 */ }
}

function renderSuggest(r) {
  const box = $("searchSuggest");
  if (!r.results.length) {
    box.innerHTML = `<div class="suggest-empty">未找到「${esc(r.query)}」</div>`;
  } else {
    box.innerHTML = r.results.map((s) => `
      <div class="suggest-item" onclick="pickStock('${s.code}')">
        <span class="suggest-code">${s.code}</span>
        <span class="suggest-name">${s.name}</span>
        <span class="suggest-status">${s.status}</span>
      </div>`).join("");
  }
  box.classList.add("open");
}

function pickStock(code) {
  $("stockSearch").value = code;
  closeSuggest();
  searchStock();
}

function closeSuggest() { $("searchSuggest").classList.remove("open"); }

function stockCard(s) {
  const badgeCls = s.sealed === 1 ? "" : s.sealed === 2 ? "alt" : "none";
  const stats = [
    `连板 <b>${s.lianban_height ? s.lianban_height + "板" : "-"}</b>`,
    `涨幅 <b>${s.high_pct ? Number(s.high_pct).toFixed(2) + "%" : "-"}</b>`,
    `上板 <b>${esc(s.zt_time || "-")}</b>`,
  ];
  if (s.turnover) stats.push(`换手 <b>${Number(s.turnover).toFixed(1)}%</b>`);
  const themes = s.themes.length
    ? s.themes.map((t) => {
        const rank = t.leader_label ? " · " + esc(t.leader_label)
          : t.final_rank ? " · #" + t.final_rank : "";
        return `<span class="sc-tag hot">${esc(t.theme_name)} ${Number(t.zt_count || 0)}只涨停${rank}</span>`;
      }).join("")
    : `<span class="sc-tag">未归入任何题材</span>`;
  const concepts = s.concepts.length
    ? s.concepts.slice(0, 12).map((c) => `<span class="sc-tag">${esc(c)}</span>`).join("")
    : `<span class="sc-tag">无概念缓存</span>`;
  let note = "";
  if (!s.in_pool) {
    note = `<div class="sc-note">当日未涨停，也未进入创/科异动池（异动池口径：30/68 开头且涨幅 &gt;9.90%）</div>`;
  } else if (!s.themes.length) {
    note = `<div class="sc-note">在当日池中，但没被任何题材吸收：涨停原因未命中主线词，也不属于该主线核心概念。</div>`;
  }
  const reasonText = s.reason || (s.sealed === 2
    ? "（创/科异动无涨停原因，按同花顺概念归属）"
    : "（数据源未给原因）");
  const anomalyLine = s.anomaly_content
    ? `<div class="sc-reason"><b>异动解读：</b>${esc(s.anomaly_content)}</div>` : "";
  return `
    <div class="stock-result">
      <div class="sc-head">
        <span class="sc-name">${esc(s.name || "-")}</span>
        <span class="sc-code">${esc(s.code)}</span>
        ${s.anomaly_tag ? `<span class="sc-badge alt">${esc(s.anomaly_tag)}</span>` : ""}
        <span class="sc-badge ${badgeCls}">${esc(s.status)}</span>
      </div>
      <div class="sc-reason"><b>${s.sealed === 2 ? "异动原因" : "涨停原因"}：</b>${esc(reasonText)}</div>
      ${anomalyLine}
      <div class="sc-stats">${stats.join("")}</div>
      <div class="sc-section"><b>所属题材：</b>${themes}</div>
      <div class="sc-section"><b>同花顺概念：</b>${concepts}${s.concept_total > 12 ? `<span class="sc-note"> 共 ${s.concept_total} 个</span>` : ""}</div>
      ${note}
    </div>`;
}

/* ---------- 补全候选（板块外围概念里未纳入的池内个股） ---------- */
async function showCandidates() {
  if (!state.activeTheme) return setStatus("请先在左侧选择题材");
  setStatus(`查看「${state.activeTheme}」补全候选…`);
  try {
    const r = await api(`/api/theme/candidates?date=${state.date}&theme=${encodeURIComponent(state.activeTheme)}`);
    renderCandidates(r);
    setStatus(`「${r.theme}」补全候选：${r.total} 只`);
  } catch (e) { setStatus("候选查询失败: " + e.message); }
}

function renderCandidates(r) {
  $("searchTitle").textContent = `${r.theme} · 补全候选 ${r.total} 只 · ${r.date}`;
  if (!r.groups.length) {
    const core = (r.core_concepts || []).join("、") || "未配置";
    $("searchBody").innerHTML = `<div class="sc-note">没有外围概念候选。核心概念（${esc(core)}）已全量纳入本板块。</div>`;
  } else {
    const parts = r.groups.map((g) => `
      <div class="cand-group">
        <div class="cand-head">${esc(g.concept)} · ${g.count} 只</div>
        ${g.stocks.map((s) => `
          <div class="cand-row">
            <span class="sc-name">${esc(s.name)}</span>
            <span class="sc-code">${esc(s.code)}</span>
            <span class="sc-badge ${s.status === "涨停" ? "" : "alt"}">${esc(s.status)}</span>
            <span class="cand-reason">${esc(s.reason || "—")}</span>
          </div>`).join("")}
      </div>`).join("");
    $("searchBody").innerHTML = parts +
      `<div class="sc-note">这些股只命中「外围概念」，不在核心概念里，所以没有自动并入。
       判断确实属于主线的话，把对应概念名加进 config/default.yaml 的 theme_core_concepts 即可。</div>`;
  }
  $("searchModal").classList.add("open");
}

/* ---------- 事件绑定 ---------- */
$("tradeDate").onchange = (e) => {
  applyDate(e.target.value).catch((err) => setStatus("加载失败: " + err.message));
};

$("historyBtn").onclick = (e) => { e.stopPropagation(); toggleDropdown(); };
document.addEventListener("click", (e) => {
  if (!$("historyDropdown").contains(e.target)) closeDropdown();
});

// 搜索实时反馈：输入即搜（debounce 300ms），不用回车
let searchTimer = null;
$("stockSearch").addEventListener("input", (e) => {
  const q = e.target.value.trim();
  clearTimeout(searchTimer);
  if (!q) { closeSuggest(); return; }
  searchTimer = setTimeout(() => suggestStock(q), 300);
});
$("stockSearch").onkeydown = (e) => {
  if (e.key === "Enter") { e.preventDefault(); searchStock(); }
};
// 点击搜索框外部关闭下拉
document.addEventListener("click", (e) => {
  if (!e.target.closest(".search-field")) closeSuggest();
});
$("btnCloseSearch").onclick = closeSearch;
$("searchModal").onclick = (e) => {
  if (e.target === $("searchModal")) closeSearch();
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeSearch();
});

$("btnFetch").onclick = async () => {
  setStatus("采集中（涨停池+炸板池+连板更新）…");
  try {
    const r = await post("/api/fetch", { date: state.requestedDate || state.date });
    const head = r.is_holiday
      ? `${r.requested_date} 休市 · 已采集 ${r.trade_date}：`
      : "采集完成：";
    let msg = `${head}涨停 ${r.zt} 只，炸板 ${r.zhaban} 只`;
    if (r.is_intraday) msg += "（盘中快照，收盘后请重新采集）";
    if (r.auto_run && r.auto_run.skipped === false) {
      msg += `，已复盘 ${r.auto_run.themes_count || 0} 个题材`;
    } else if (r.auto_run && r.auto_run.reason) {
      msg += `，${r.auto_run.reason}`;
    }
    setStatus(msg);
    await applyDate(state.requestedDate || state.date, { silent: true });
    setStatus(msg);
  } catch (e) { setStatus("采集失败: " + e.message); }
};

$("btnBackfill").onclick = async () => {
  const raw = prompt(
    "回填最近多少个交易日？\n（4 月起约需 150；库里已有的日期会自动跳过，可反复跑）",
    "30");
  if (raw === null) return;
  const days = Math.max(1, Math.min(400, parseInt(raw, 10) || 30));
  if (!confirm(`将回填最近 ${days} 个交易日的涨停/题材数据（约需几分钟），确定？`)) return;
  setStatus(`历史回填中…（最近 ${days} 个交易日，请勿关闭页面）`);
  try {
    const r = await post("/api/backfill", { days });
    const fail = (r.failed || []).length;
    setStatus(`历史回填完成：新增 ${r.done.length} 个交易日` +
      (r.skipped ? `，跳过已有 ${r.skipped}` : "") +
      (fail ? `，失败 ${fail} 天（可再点一次补齐）` : ""));
    await loadDates();
    await applyDate(state.requestedDate || state.date, { silent: true });
  } catch (e) { setStatus("历史回填失败: " + e.message); }
};

$("btnRun").onclick = async () => {
  setStatus("复盘计算中…");
  try {
    const r = await post("/api/run", { date: state.requestedDate || state.date });
    const head = r.is_holiday
      ? `${r.requested_date} 休市 · 已用 ${r.trade_date} 复盘：`
      : "复盘完成：";
    let msg = `${head}${r.themes_count || 0} 个题材，${r.exported} 只成分股`;
    if (r.is_intraday) msg += "（盘中快照，收盘后请重新复盘）";
    await applyDate(state.requestedDate || state.date, { silent: true });
    setStatus(msg);
  } catch (e) { setStatus("复盘失败: " + e.message); }
};

/* ---------- 导入同花顺：粘贴 / 生成 TXT / 生成图片 ---------- */
// 粘贴：同花顺一次粘贴有 200 字上限，自动分组逐组复制
// TXT ：一个文件装整个板块，「添加股票 → 上传 txt」一次导入（主力路径）
// 图片：同花顺图片识别只认 6 位数字、一次 10~20 只，按 12 只一批出图（兜底路径）
let clipState = { sizes: [], index: 0, total: 0, count: 0, scope: "single" };
let thsState = { scope: "single", dir: "", files: [], images: [], skipped: [], total: 0, themes: 0 };
let imgOpt = { font: 40, line: 56, pad: 36, dpr: 2 };

function openThs() {
  if (!state.activeTheme) return setStatus("请先在左侧选择题材");
  // 页面缓存与脚本版本不一致时缺 #thsModal：退化成纯粘贴，绝不抛错
  // （顶层抛错会中断后续所有绑定，白天模式按钮就是这么失效的）
  if (!$("thsModal")) {
    doClip(0)
      .then(() => { const m = $("clipModal"); if (m) m.classList.add("open"); })
      .catch((e) => setStatus("复制失败: " + e.message));
    return;
  }
  thsState = { scope: "single", dir: "", files: [], images: [], skipped: [], total: 0, themes: 0 };
  const clear = (id) => { const el = $(id); if (el) el.innerHTML = ""; };
  clear("thsFiles");
  clear("thsHint");
  clear("thsProgress");
  const single = $("thsIncludeSingle");
  if (single) single.checked = false;
  renderThsScope();
  $("thsModal").classList.add("open");
  doClip(0).catch((e) => {
    const hint = $("clipHint");
    if (hint) hint.textContent = e.message;
    const box = $("clipGroups");
    if (box) box.innerHTML = "";
    const next = $("btnClipNext");
    if (next) {
      next.textContent = "剪贴板不可用，请用下面的「生成 TXT」";
      next.onclick = null;
    }
  });
}

function renderThsScope() {
  document.querySelectorAll("#thsScope .seg-btn").forEach((b) => {
    b.classList.toggle("on", b.dataset.scope === thsState.scope);
  });
  const sub = $("thsSub");
  if (sub) {
    sub.textContent = thsState.scope === "single"
      ? `当前题材：${state.activeTheme || "未选择"}`
      : "全部题材，每个题材一个板块";
  }
  const row = $("thsIncludeRow");
  if (row) row.hidden = thsState.scope !== "all";
}

async function doClip(idx) {
  const r = await post("/api/clipboard", {
    date: state.date,
    theme: thsState.scope === "single" ? state.activeTheme : null,
    group_index: idx,
  });
  clipState = {
    sizes: r.group_sizes, index: idx, total: r.total_groups,
    count: r.count, scope: thsState.scope,
  };
  renderClip();
}

function renderClip() {
  const label = clipState.scope === "single" ? `「${state.activeTheme}」` : "全部题材";
  const hint = $("clipHint");
  if (hint) {
    hint.textContent = clipState.total > 1
      ? `${label}共 ${clipState.count} 只代码，超过同花顺单次粘贴上限，分 ${clipState.total} 组逐组复制`
      : `${label}共 ${clipState.count} 只代码，一次粘贴即可`;
  }
  const box = $("clipGroups");
  if (!box) return;
  box.innerHTML = "";
  clipState.sizes.forEach((size, i) => {
    const done = i <= clipState.index;
    const div = document.createElement("div");
    div.className = "clip-group" + (done ? " done" : "") + (i === clipState.index ? " current" : "");
    div.innerHTML = `<span class="clip-no">第 ${i + 1} 组</span>
                     <span class="clip-size">${size} 只</span>
                     <span class="clip-status">${done ? "✓ 已复制" : "待复制"}</span>`;
    box.appendChild(div);
  });
  const next = clipState.index + 1;
  const btn = $("btnClipNext");
  if (next >= clipState.total) {
    btn.textContent = "全部复制完成";
    btn.onclick = closeClip;
  } else {
    btn.textContent = `复制第 ${next + 1} 组（${clipState.sizes[next]} 只）`;
    btn.onclick = async () => { await doClip(next); };
  }
}

function closeClip() {
  const m = $("thsModal") || $("clipModal");
  if (m) m.classList.remove("open");
}

/* 生成 TXT */
async function doThsTxt() {
  const btn = $("thsTxtBtn");
  btn.disabled = true;
  $("thsProgress").textContent = "正在生成…";
  try {
    const r = await post("/api/export/ths", {
      date: state.date,
      theme: thsState.scope === "single" ? state.activeTheme : null,
      scope: thsState.scope,
      include_single: $("thsIncludeSingle").checked,
    });
    thsState.dir = r.dir || "";
    thsState.files = r.files || [];
    thsState.skipped = r.skipped || [];
    thsState.total = r.total || 0;
    thsState.themes = r.themes || 0;
    renderThsFiles(r.hint);
    $("thsProgress").textContent = r.total
      ? `已生成 ${thsState.files.length} 个 txt，共 ${r.total} 只 / ${r.themes} 个题材`
      : "";
    if (r.dir && $("thsReveal").checked && thsState.files.length) {
      await revealPath(thsState.files[0].path);
    }
  } catch (e) {
    $("thsProgress").textContent = "生成失败: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

/* 生成图片：canvas 渲染 → base64 → 一次回传落盘 */
async function doThsImg() {
  if (thsState.scope !== "single") {
    return setStatus("图片请切到「当前题材」再生成：同花顺一次只识别 10~20 只");
  }
  const btn = $("thsImgBtn");
  btn.disabled = true;
  $("thsProgress").textContent = "正在渲染图片…";
  try {
    const r = await post("/api/export/ths", {
      date: state.date, theme: state.activeTheme, scope: "single", include_single: false,
    });
    if (r.img_opt) imgOpt = r.img_opt;
    if (!r.images || !r.images.length) {
      $("thsProgress").textContent = r.hint || "没有可出图的题材";
      return;
    }
    thsState.dir = r.dir || "";
    thsState.files = r.files || [];
    renderThsFiles(r.hint);

    const canvases = [];
    for (const b of r.images) canvases.push(await codeCanvas(b.codes));
    $("thsProgress").textContent = `已渲染 ${canvases.length} 张，正在保存…`;

    /* 手机没有「本地磁盘」这个概念：不往电脑落盘，直接交到相册/系统分享 */
    if (isNarrow()) {
      const res = await saveImagesToPhone(canvases);
      $("thsProgress").textContent = res.msg;
      return;
    }

    const items = canvases.map((c, i) => ({
      path: r.images[i].path,
      data_b64: c.toDataURL("image/png").split(",")[1],
    }));
    const up = await post("/api/export/ths/image", { items });
    let msg = `图片已保存 ${up.saved}/${items.length} 张`;
    if (up.failed && up.failed.length) {
      msg += `，${up.failed.length} 张失败（${up.failed.map((f) => f.name).join("、")}）`;
    }
    if (items.length > 3) msg += " · 张数较多，建议优先用 TXT";
    $("thsProgress").textContent = msg;
    if ($("thsReveal").checked) await revealPath(r.images[0].path);
  } catch (e) {
    $("thsProgress").textContent = "图片生成失败: " + e.message + "（TXT 已可用）";
  } finally {
    btn.disabled = false;
  }
}

/* 白底黑字、单列竖排、图内只有 6 位数字 —— 同花顺 OCR 只认代码，
   混进日期/题材名会被当成股票代码读走 */
async function codeCanvas(codes) {
  const { font, line, pad, dpr } = imgOpt;
  try { await document.fonts.ready; } catch (_) { /* 老浏览器忽略 */ }
  const spec = `600 ${font}px Consolas, "SF Mono", "DejaVu Sans Mono", monospace`;
  const probe = document.createElement("canvas").getContext("2d");
  probe.font = spec;
  const w = Math.ceil(probe.measureText("000000").width) + pad * 2;
  const h = pad * 2 + codes.length * line;

  const c = document.createElement("canvas");
  c.width = Math.round(w * dpr);
  c.height = Math.round(h * dpr);
  const g = c.getContext("2d");
  g.scale(dpr, dpr);
  g.fillStyle = "#ffffff";          // 必须实底：透明底会被 OCR 当黑底，直接废
  g.fillRect(0, 0, w, h);
  g.fillStyle = "#000000";
  g.font = spec;
  g.textAlign = "center";
  g.textBaseline = "middle";
  codes.forEach((code, i) => g.fillText(code, w / 2, pad + line * (i + 0.5)));
  return c;
}

/* ---------- 手机端图片落地 ----------
   桌面走「POST 给后端写磁盘」，手机没有可写的路径，改走两条路：
   1) 有 Web Share API（HTTPS 页面）→ 调系统分享面板，用户选「存储到照片」
   2) 纯 http（局域网直连）→ 页面内预览，长按图片存相册
   不再用 <a download>：多张连续下载会被浏览器拦，且 iOS 落在「文件」里不直观。 */
function canvasToFile(canvas, name) {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => {
      resolve(blob ? new File([blob], name, { type: "image/png" }) : null);
    }, "image/png");
  });
}

function showImagePreview(canvases) {
  const box = $("thsFiles");
  if (!box) return;
  box.innerHTML = "";
  canvases.forEach((c, i) => {
    const img = document.createElement("img");
    img.src = c.toDataURL("image/png");
    img.className = "ths-preview";
    img.alt = `第 ${i + 1} 张`;
    box.appendChild(img);
  });
}

async function saveImagesToPhone(canvases) {
  const safe = (state.activeTheme || "题材").replace(/[\\/:*?"<>|\s]/g, "");
  const files = [];
  for (let i = 0; i < canvases.length; i++) {
    const f = await canvasToFile(canvases[i], `${safe}_${state.date}_${i + 1}.png`);
    if (f) files.push(f);
  }
  if (!files.length) return { mode: "none", count: 0, msg: "图片生成失败，可改用 TXT" };

  if (navigator.canShare && navigator.canShare({ files })) {
    try {
      await navigator.share({ files, title: `复盘吧 · ${state.activeTheme || ""}` });
      return { mode: "share", count: files.length,
               msg: `已交给系统分享，选「存储到照片」；共 ${files.length} 张` };
    } catch (e) {
      if (e && e.name === "AbortError") return { mode: "abort", count: 0, msg: "已取消" };
      /* 其他异常继续走预览 */
    }
  }

  showImagePreview(canvases);
  return { mode: "preview", count: files.length,
           msg: `已生成 ${files.length} 张：长按图片 → 存储到照片，再去同花顺「添加股票 → 上传图片」` };
}

function renderThsFiles(hint) {
  const box = $("thsFiles");
  box.innerHTML = "";
  const notes = [];
  if (hint) notes.push(hint);
  if (thsState.skipped.length) {
    const names = thsState.skipped.slice(0, 4).map((s) => s.theme).join("、");
    notes.push(`另有 ${thsState.skipped.length} 个只含 1 只的题材未导出（${names}…）`);
  }
  $("thsHint").textContent = notes.join(" · ");
  /* 手机上没有可操作的本地路径，文件行和「定位」按钮都是干扰 */
  if (isNarrow()) return;
  thsState.files.forEach((f) => {
    const row = document.createElement("div");
    row.className = "ths-file";
    row.innerHTML = `<span class="ths-file-name">${esc(f.name)}</span>
                     <span class="ths-count">${f.count} 只</span>
                     <button class="ghost ths-loc" title="在资源管理器中定位">定位</button>`;
    row.querySelector(".ths-loc").onclick = () => revealPath(f.path);
    box.appendChild(row);
  });
}

async function revealPath(p) {
  const hint = $("thsHint");
  if (!p) {                              // 刷新后 state 清空，别静默失败
    if (hint) hint.textContent = "还没有可定位的文件，请先点「生成 TXT」";
    return;
  }
  try {
    const r = await post("/api/export/reveal", { path: p });
    if (!r.ok && hint) hint.textContent = r.hint || "";
  } catch (e) {
    let msg = `无法自动打开：${p}`;
    try { await navigator.clipboard.writeText(p); msg += "（路径已复制）"; } catch (_) {}
    if (hint) hint.textContent = msg;
  }
}

/* 打开当日导出目录：目录由后端按日期解析，不依赖前端 state（刷新后会是空的） */
async function openThsFolder() {
  const hint = $("thsHint");
  try {
    const r = await post("/api/export/open", { date: state.date });
    if (hint) hint.textContent = r.ok ? "" : (r.hint || "");
  } catch (e) {
    if (hint) hint.textContent = "打开文件夹失败: " + e.message;
  }
}

const _bind = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };
_bind("btnCandidates", showCandidates);
_bind("btnTheme", toggleTheme);
_bind("btnCloseAssign", closeAssign);

/* ---------- 手机端「更多」菜单（桌面端这些按钮是摊平的，按钮本身不显示） ---------- */
(function bindMore() {
  const btn = $("btnMore");
  const panel = $("morePanel");
  const header = document.querySelector("header");
  if (!btn || !panel || !header) return;

  const setOpen = (on) => {
    header.classList.toggle("more-open", on);
    btn.setAttribute("aria-expanded", on ? "true" : "false");
  };

  btn.onclick = (e) => {
    e.stopPropagation();
    setOpen(!header.classList.contains("more-open"));
  };

  panel.addEventListener("click", (e) => {
    if (e.target.closest("#historyDropdown")) return;   // 下拉里还有一层，别抢
    if (e.target.closest("button")) setOpen(false);
  });

  document.addEventListener("click", (e) => {
    if (!header.classList.contains("more-open")) return;
    if (e.target.closest("#morePanel") || e.target.closest("#btnMore")) return;
    setOpen(false);
  });

  window.addEventListener("resize", () => { if (!isNarrow()) setOpen(false); });
})();

/* 触屏：点空白处收掉个股详情卡（桌面靠 mouseleave，手机没有） */
document.addEventListener("click", (e) => {
  if (!isTouch()) return;
  const el = $("stockCard");
  if (!el || !el.classList.contains("show")) return;
  if (el.contains(e.target) || e.target.closest("#leaderTable tr, .ldr-row")) return;
  el.classList.remove("show");
});
{
  const f = $("assignFilter");
  if (f) f.oninput = (e) => renderAssignList(e.target.value);
  const m = $("assignModal");
  if (m) m.onclick = (e) => { if (e.target === m) closeAssign(); };
}

/* ---------- 应用内更新 ---------- */
let updateInfo = null;

async function checkUpdate() {
  try {
    const r = await api("/api/update/check");
    updateInfo = r;
    const btn = $("btnUpdate");
    if (r && r.has_update) {
      btn.hidden = false;
      btn.textContent = `更新 v${r.latest}`;
      btn.title = (r.notes || "") + `（当前 ${r.current}）`;
    } else {
      btn.hidden = true;
    }
  } catch (e) { /* 忽略网络错误 */ }
}

function waitForRestart() {
  const started = Date.now();
  const timer = setInterval(async () => {
    try {
      const r = await api("/api/version");
      if (r && r.version) { clearInterval(timer); location.reload(); }
    } catch (e) { /* 服务还没起来 */ }
    if (Date.now() - started > 90000) clearInterval(timer);
  }, 2000);
}

$("btnUpdate").onclick = async () => {
  if (!updateInfo || !updateInfo.has_update) return;
  const ok = confirm(`发现新版本 ${updateInfo.latest}（当前 ${updateInfo.current}）。\n更新会重启程序，data/ 与 .env 会保留，继续？`);
  if (!ok) return;
  setStatus("正在下载更新…");
  $("btnUpdate").disabled = true;
  try {
    const r = await post("/api/update/apply", {});
    if (!r.ok) {
      setStatus("更新失败：" + (r.message || "未知错误"));
      $("btnUpdate").disabled = false;
      return;
    }
    setStatus("更新中，程序将自动重启，请稍候…");
    waitForRestart();
  } catch (e) {
    setStatus("更新失败: " + e.message);
    $("btnUpdate").disabled = false;
  }
};

/* ---------- 版本自检：前端有更新时自动刷新，避免旧页面调新接口报错 ---------- */
const APP_VERSION = (document.querySelector('meta[name="app-version"]') || {}).content || "";
async function checkAppVersion() {
  try {
    const r = await api("/api/version");
    if (r && r.version && APP_VERSION && r.version !== APP_VERSION) {
      setStatus("检测到前端已更新，正在自动刷新…");
      setTimeout(() => location.reload(), 600);
    }
  } catch (e) { /* 网络抖动忽略 */ }
}
setInterval(checkAppVersion, 30000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") checkAppVersion();
});

/* ---------- 导入同花顺面板绑定（缺节点只跳过，不牵连其它按钮） ---------- */
function bindThs() {
  const btn = $("btnClipboard");
  if (btn) btn.onclick = openThs;
  if (!$("thsModal")) return;            // 旧页面：openThs 内部已退化为纯粘贴
  const scope = $("thsScope");
  if (scope) {
    scope.onclick = async (e) => {
      const b = e.target.closest(".seg-btn");
      if (!b || b.dataset.scope === thsState.scope) return;
      thsState.scope = b.dataset.scope;
      renderThsScope();
      try {
        await doClip(0);
      } catch (err) {
        const h = $("clipHint");
        if (h) h.textContent = err.message;
      }
    };
  }
  const close = $("btnCloseThs");
  if (close) close.onclick = closeClip;
  const modal = $("thsModal");
  if (modal) modal.onclick = (e) => { if (e.target === modal) closeClip(); };
  const txt = $("thsTxtBtn");
  if (txt) txt.onclick = doThsTxt;
  const img = $("thsImgBtn");
  if (img) img.onclick = doThsImg;
  const openBtn = $("thsOpenBtn");
  if (openBtn) openBtn.onclick = openThsFolder;
}
try { bindThs(); } catch (e) { console.error("绑定导入同花顺面板失败", e); }

/* ---------- 题材框架管理（开盘啦层级表） ---------- */
const fwState = { groups: [], query: "", onlyManual: false, timer: 0 };

async function loadFramework(q = "") {
  const r = await api(`/api/framework/themes?q=${encodeURIComponent(q)}`);
  fwState.groups = r.groups || [];
  const s = r.stats || {};
  $("fwStats").textContent =
    `${s.themes || 0} 个一级题材 · ${s.total || 0} 条` +
    (s.manual ? ` · 手工 ${s.manual}` : "") +
    (s.disabled ? ` · 停用 ${s.disabled}` : "");
  renderFramework();
}

function fwRow(theme, sub, item, isThemeRow) {
  const badge = item.source === "manual"
    ? `<span class="fw-badge manual">手工</span>`
    : `<span class="fw-badge">框架</span>`;
  const off = item.enabled === 0 ? " off" : "";
  const label = isThemeRow ? theme : sub;
  const note = item.note ? `<span class="fw-note">${esc(item.note)}</span>` : "";
  const toggleText = item.enabled === 0 ? "启用" : "停用";
  const delBtn = item.source === "manual"
    ? `<button data-act="del">删除</button>` : "";
  return `
    <div class="fw-row${off}" data-theme="${esc(theme)}" data-sub="${esc(sub)}">
      <span class="fw-label">${esc(label)}</span>
      ${badge}${note}
      <span class="fw-row-actions">
        <button data-act="rename">改名</button>
        <button data-act="toggle">${toggleText}</button>
        ${delBtn}
      </span>
    </div>`;
}

function renderFramework() {
  const box = $("fwList");
  let groups = fwState.groups;
  if (fwState.onlyManual) {
    groups = groups
      .map((g) => {
        const subs = (g.subs || []).filter((s) => s.source === "manual");
        if (g.source !== "manual" && !subs.length) return null;
        return { ...g, subs };
      })
      .filter(Boolean);
  }
  if (!groups.length) {
    box.innerHTML = `<div class="sc-note">没有匹配的题材</div>`;
    return;
  }
  box.innerHTML = groups.map((g) => `
    <div class="fw-group">
      ${fwRow(g.theme_name, "", g, true)}
      <div class="fw-subs">
        ${(g.subs || []).map((s) => fwRow(g.theme_name, s.sub_name, s, false)).join("")}
        <button class="fw-add-sub" data-act="add-sub" data-theme="${esc(g.theme_name)}">+ 添加细分词</button>
      </div>
    </div>`).join("");
}

async function loadLoose() {
  try {
    const r = await api("/api/framework/loose");
    $("fwLooseCount").textContent = r.count || 0;
    const box = $("fwLooseList");
    if (!r.items || !r.items.length) {
      box.innerHTML = `<div class="sc-note">没有未归类词，全部都能归到大板块</div>`;
      return;
    }
    box.innerHTML = r.items.map((it) => `
      <div class="fw-loose-row" data-name="${esc(it.name)}">
        <span class="fw-label">${esc(it.name)}</span>
        <span class="fw-note">${it.days}天 · ${it.total}只</span>
        <input list="fwThemeOptions" class="fw-loose-input" placeholder="归到哪个大板块">
        <button data-act="assign-loose">归入</button>
      </div>`).join("");
  } catch (e) {
    $("fwLooseList").innerHTML = `<div class="sc-note">未归类词加载失败: ${esc(e.message)}</div>`;
  }
}

function fillThemeOptions() {
  const dl = $("fwThemeOptions");
  if (!dl) return;
  const names = [...new Set(fwState.groups.map((g) => g.theme_name))].sort();
  dl.innerHTML = names.map((n) => `<option value="${esc(n)}"></option>`).join("");
}

async function fwRefresh(msg) {
  await loadFramework(fwState.query);
  try {
    const d = state.requestedDate || state.date;
    const r = await post("/api/framework/apply", { date: d });
    if (r.skipped) {
      setStatus(`${msg || ""} · ${r.reason}`);
    } else {
      setStatus(`${msg || ""} · 已按新框架重算 ${r.themes_count || 0} 个题材`);
      await applyDate(d, { silent: true });
    }
  } catch (e) {
    setStatus(`${msg || ""} · 重算失败: ${e.message}`);
  }
}

function bindFramework() {
  const btn = $("btnFramework");
  if (!btn) return;
  const modal = $("fwModal");

  btn.onclick = async () => {
    modal.classList.add("open");
    setStatus("加载题材框架…");
    try {
      await loadFramework("");
      fillThemeOptions();
      await loadLoose();
      setStatus("题材框架已加载");
    } catch (e) { setStatus("题材框架加载失败: " + e.message); }
    setTimeout(() => $("fwQuery").focus(), 120);
  };
  $("btnCloseFw").onclick = () => modal.classList.remove("open");
  modal.onclick = (e) => { if (e.target === modal) modal.classList.remove("open"); };

  /* 从发布方拉最新框架。应用更新包会保留 data/，所以框架只能走这条通道。
     注意：同步完只改了框架，当天的复盘结果要重算才生效。 */
  const syncBtn = $("btnFwSync");
  if (syncBtn) {
    syncBtn.onclick = async () => {
      syncBtn.disabled = true;
      syncBtn.textContent = "同步中…";
      setStatus("正在拉取最新框架…");
      try {
        const r = await post("/api/framework/sync", {});
        const ra = r.reanalyze || {};
        let msg = `框架已更新：${r.rows} 条 / ${r.themes} 个题材（版本 ${r.version}）`;
        if (ra.ok) msg += ` · 已按新框架重算 ${ra.date}（${ra.themes} 个题材）`;
        else if (ra.skipped) msg += ` · ${ra.reason}`;
        else if (ra.reason) msg += ` · 重算失败：${ra.reason}`;
        setStatus(msg);
        await loadFramework("");
        fillThemeOptions();
        if (ra.ok && state.date) await loadDay(state.date);   // 刷新当天数据
      } catch (e) {
        setStatus("框架同步失败: " + e.message);
      } finally {
        syncBtn.disabled = false;
        syncBtn.textContent = "同步框架";
      }
    };
  }

  // 搜索框：同时做归类预览（防抖 380ms）
  $("fwQuery").oninput = () => {
    clearTimeout(fwState.timer);
    const q = $("fwQuery").value.trim();
    fwState.timer = setTimeout(async () => {
      fwState.query = q;
      try {
        if (q) {
          const m = await api(`/api/framework/match?q=${encodeURIComponent(q)}`);
          const themes = (m.themes || []);
          const cls = m.exact ? "exact" : "";
          $("fwMatch").innerHTML = themes.length
            ? `归到：${themes.map((t) => `<b>${esc(t)}</b>`).join(" / ")}` +
              (m.exact ? `<span class="fw-badge ${cls}">精确命中</span>` : `<span class="fw-badge">包含匹配</span>`)
            : `<span class="sc-note">框架里没有匹配，会独立成题材</span>`;
        } else {
          $("fwMatch").innerHTML = "";
        }
        await loadFramework(q);
      } catch (e) { $("fwMatch").textContent = "查询失败: " + e.message; }
    }, 380);
  };

  // 新增（一级题材 + 可选细分）
  $("btnFwAdd").onclick = async () => {
    const theme = $("fwNewTheme").value.trim();
    const sub = $("fwNewSub").value.trim();
    if (!theme) return setStatus("请先填一级题材名");
    try {
      await post("/api/framework/themes", { theme_name: theme, sub_name: sub });
      $("fwNewTheme").value = ""; $("fwNewSub").value = "";
      await fwRefresh(`已添加：${theme}${sub ? " / " + sub : ""}`);
    } catch (e) { setStatus("添加失败: " + e.message); }
  };

  $("fwOnlyManual").onchange = (e) => {
    fwState.onlyManual = e.target.checked;
    renderFramework();
  };

  // 未归类词：展开/收起 + 归入大板块
  $("btnFwLooseToggle").onclick = () => {
    const box = $("fwLooseList");
    const open = box.hidden;
    box.hidden = !open;
    $("btnFwLooseToggle").querySelector(".fw-caret").textContent = open ? "▾" : "▸";
    if (open) loadLoose();
  };

  $("fwLooseList").onclick = async (ev) => {
    const el = ev.target.closest("[data-act='assign-loose']");
    if (!el) return;
    const row = el.closest(".fw-loose-row");
    const name = row.dataset.name;
    const input = row.querySelector(".fw-loose-input");
    const theme = (input.value || "").trim();
    if (!theme) { input.focus(); return setStatus("请先选一个要归入的大板块"); }
    try {
      await post("/api/framework/themes", { theme_name: theme, sub_name: name });
      await fwRefresh(`已把「${name}」归入「${theme}」，以后自动生效`);
      await loadLoose();
      fillThemeOptions();
    } catch (e) { setStatus("归入失败: " + e.message); }
  };

  // 行内操作（事件委托）
  $("fwList").onclick = async (ev) => {
    const el = ev.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    const theme = el.dataset.theme || el.closest(".fw-row")?.dataset.theme;
    const sub = el.closest(".fw-row")?.dataset.sub ?? "";
    if (act === "add-sub") {
      const name = prompt(`给「${theme}」添加细分词（如 冰雪产业）`);
      if (!name || !name.trim()) return;
      try {
        await post("/api/framework/themes", { theme_name: theme, sub_name: name.trim() });
        await fwRefresh(`已添加细分：${theme} / ${name.trim()}`);
      } catch (e) { setStatus("添加失败: " + e.message); }
      return;
    }
    if (act === "rename") {
      const cur = sub || theme;
      const name = prompt("改名（一级题材或细分词）", cur);
      if (!name || !name.trim() || name.trim() === cur) return;
      try {
        await api("/api/framework/themes", {
          method: "PUT",
          body: JSON.stringify(sub
            ? { theme_name: theme, sub_name: sub, new_sub: name.trim() }
            : { theme_name: theme, sub_name: "", new_theme: name.trim(), new_sub: "" }),
        });
        await fwRefresh("已改名");
      } catch (e) { setStatus("改名失败: " + e.message); }
      return;
    }
    if (act === "toggle") {
      const row = el.closest(".fw-row");
      const to = row.classList.contains("off") ? 1 : 0;
      try {
        await api("/api/framework/themes", {
          method: "PUT",
          body: JSON.stringify({ theme_name: theme, sub_name: sub, enabled: to }),
        });
        await fwRefresh(to ? "已启用" : "已停用");
      } catch (e) { setStatus("操作失败: " + e.message); }
      return;
    }
    if (act === "del") {
      if (!confirm(`删除「${sub || theme}」？`)) return;
      try {
        const r = await api("/api/framework/themes", {
          method: "DELETE",
          body: JSON.stringify({ theme_name: theme, sub_name: sub }),
        });
        await fwRefresh(r.message || "已删除");
      } catch (e) { setStatus("删除失败: " + e.message); }
    }
  };

  $("btnFwImport").onclick = async () => {
    if (!confirm("用框架文件重导？框架数据会整批替换，手工添加的题材保留。")) return;
    try {
      const r = await post("/api/framework/import", {});
      await fwRefresh(`重导完成：${r.imported} 条（手工数据已保留）`);
    } catch (e) { setStatus("重导失败: " + e.message); }
  };

  $("btnFwExport").onclick = async () => {
    try {
      const r = await api("/api/framework/export");
      const blob = new Blob([r.yaml], { type: "text/yaml;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `kpl_framework_${localToday()}.yaml`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      setStatus(`已导出 ${r.count} 条细分词`);
    } catch (e) { setStatus("导出失败: " + e.message); }
  };
}
try { bindFramework(); } catch (e) { console.error("绑定题材框架面板失败", e); }

/* ---------- 爆量题材：视图切换 / 倍数切换 / 回补 ---------- */
try {
  $("viewScope").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn");
    if (b && b.dataset.view !== state.view) setView(b.dataset.view).catch(() => {});
  });

  // 切倍数纯前端重渲染：左栏家数与右栏明细都来自同一份 payload，不会打架
  $("burstMult").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn");
    const m = b ? Number(b.dataset.mult) : 0;
    if (!m || m === state.burstMult) return;
    state.burstMult = m;
    [...$("burstMult").querySelectorAll(".seg-btn")]
      .forEach((x) => x.classList.toggle("on", Number(x.dataset.mult) === m));
    if (state.burst && state.burst.ready) renderBurstThemes();
  });

  $("btnVolBackfill").addEventListener("click", async () => {
    const btn = $("btnVolBackfill");
    if (!confirm("回补全市场近 20 个交易日的逐日成交量（约 6-10 分钟，只需跑一次），确定？")) return;
    btn.disabled = true;
    try {
      const r = await post("/api/volume/backfill", { days: 20 });
      if (r.already) setStatus("爆量回补已在运行中…");
      const timer = setInterval(async () => {
        let j;
        try { j = await api("/api/volume/job"); } catch (e) { return; }
        const pct = j.total ? Math.floor((j.done / j.total) * 100) : 0;
        setStatus(`爆量回补中… ${pct}%（${j.done}/${j.total}，失败 ${j.fail}）`);
        if (j.running) return;
        clearInterval(timer);
        btn.disabled = false;
        setStatus(j.message || "爆量回补完成");
        try { await post("/api/volume/scan", { date: state.date }); } catch (e) { /* 下次访问会自愈 */ }
        if (state.view === "burst") await loadBurst(state.date);
      }, 1000);
    } catch (e) {
      btn.disabled = false;
      setStatus("爆量回补启动失败: " + e.message);
    }
  });
} catch (e) { console.error("绑定爆量面板失败", e); }

/* ---------- 复盘笔记：展开/页签/输入 ---------- */
try {
  state.noteOpen = localStorage.getItem("noteOpen") === "1";
  $("noteBody").hidden = !state.noteOpen;

  $("noteToggle").addEventListener("click", () => {
    state.noteOpen = !state.noteOpen;
    localStorage.setItem("noteOpen", state.noteOpen ? "1" : "0");
    $("noteBody").hidden = !state.noteOpen;
    $("noteBar").classList.toggle("open", state.noteOpen);
    $("noteToggle").textContent = state.noteOpen ? "▴" : "▾";
    if (state.noteOpen) syncNoteInput();
  });

  $("noteTabs").addEventListener("click", (e) => {
    const b = e.target.closest(".seg-btn");
    if (b && !b.hidden) setNoteScope(b.dataset.scope);
  });

  const input = $("noteInput");
  input.addEventListener("input", () => {
    // 快照当前上下文：之后切日期/切板块，这份内容仍然写回它原本的日期
    notePending = {
      date: state.date,
      scope: state.noteScope,
      key: state.noteScope === "day" ? "" : state.noteKey,
      content: input.value,
    };
    $("noteMeta").textContent = input.value.length ? `${input.value.length} 字` : "未保存";
    if (state.noteTimer) clearTimeout(state.noteTimer);
    state.noteTimer = setTimeout(flushNote, 900);
  });
  input.addEventListener("blur", () => { flushNote(); });
} catch (e) { console.error("绑定笔记失败", e); }

/* ---------- 题材列表滚动提示 ---------- */
$("themeList").addEventListener("scroll", updateScrollHint, { passive: true });
window.addEventListener("resize", updateScrollHint);

/* ---------- 启动 ---------- */
initTheme();
const today = localToday();
setTimeout(checkUpdate, 1500);
$("tradeDate").max = today;      // 不允许选未来日期
loadDates();
applyDate(today).catch((e) => setStatus("加载失败: " + e.message));
