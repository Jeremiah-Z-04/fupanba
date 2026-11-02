/* 复盘吧 · 静态只读版（GitHub Pages）
   数据来自 ./data/ 下预生成的 JSON，不依赖任何后端。
   对应桌面版的 app.js，但去掉了采集、人工修正、题材框架这些需要写库的功能。 */
const $ = (id) => document.getElementById(id);
const state = {
  dates: [], date: "", day: null, activeTheme: null,
  view: "zt",          // zt=涨停题材 / burst=爆量题材
  burstMult: 2,        // 2 或 3，纯前端切换
  burstTheme: null,
};

/* 与 config/default.yaml 的 export.ths 保持一致 */
const IMG = { font: 40, line: 56, pad: 36, dpr: 2, chunk: 12 };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const safeName = (s) => String(s || "题材").replace(/[\\/:*?"<>|\s]/g, "");

function setStatus(msg) { const el = $("statusBar"); if (el) el.textContent = msg || ""; }
function setProg(msg) { const el = $("thsProgress"); if (el) el.textContent = msg || ""; }

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

const isNarrow = () => window.matchMedia("(max-width: 720px)").matches;

/* ---------------- 启动 ---------------- */
async function boot() {
  try {
    const idx = await getJSON("./data/index.json");
    state.dates = idx.dates || [];
    if (!state.dates.length) return setStatus("还没有导出的数据");
    $("dateSel").innerHTML = state.dates
      .map((d) => `<option value="${d}">${d}</option>`).join("");
    $("dateSel").onchange = (e) => loadDay(e.target.value);
    $("btnPrev").onclick = () => step(1);   // 更早
    $("btnNext").onclick = () => step(-1);  // 更新
    bindThs();
    bindBurst();
    await loadDay(idx.latest);
    setStatus(`数据更新于 ${idx.generated_at}`);
  } catch (e) {
    setStatus("加载失败：" + e.message);
  }
}

function step(dir) {
  const i = state.dates.indexOf(state.date);
  if (i < 0) return;
  const j = Math.max(0, Math.min(state.dates.length - 1, i + dir));
  if (j !== i) loadDay(state.dates[j]);
}

function updateNav() {
  const i = state.dates.indexOf(state.date);
  $("btnPrev").disabled = i >= state.dates.length - 1;
  $("btnNext").disabled = i <= 0;
}

async function loadDay(ds) {
  setStatus(`加载 ${ds} …`);
  try {
    const day = await getJSON(`./data/${ds}.json`);
    state.date = ds;
    state.day = day;
    $("dateSel").value = ds;
    renderBreadth(day.breadth || {});
    if (state.view === "burst") {
      renderBurst();
    } else {
      state.activeTheme = (day.themes && day.themes[0]) ? day.themes[0].theme_name : null;
      renderThemes(day.themes || []);
      renderUnassigned(day.unassigned);
      renderLeaders(state.activeTheme);
    }
    updateNav();
    setStatus(day.trading_day === false ? `${ds}（非交易日）` : "");
  } catch (e) {
    setStatus("加载失败：" + e.message);
  }
}

/* ---------------- 渲染 ---------------- */
const fmt = (v) => (v === null || v === undefined || v === "" ? "—" : v);

function renderBreadth(b) {
  $("bdUp").textContent = fmt(b.up_99);
  $("bdZt").textContent = fmt(b.limit_up);
  $("bdTech").textContent = fmt(b.tech_10);
  ["bdUp", "bdZt", "bdTech"].forEach((id) => {
    $(id).classList.toggle("na", $(id).textContent === "—");
  });
  $("bdHint").textContent = b.hint || "";
}

/* 题材列表：手机上是「点击就地展开龙头」，桌面是「点击右侧联动」。
   两边共用同一套 DOM，靠 CSS 控制展开区只在窄屏显示。 */
function renderThemes(themes) {
  $("themeCount").textContent = `${themes.length} 个题材 · 按涨停+异动`;
  const box = $("themeList");
  box.innerHTML = "";
  themes.forEach((t) => box.appendChild(makeThemeBlock(t)));
}

function makeThemeBlock(t) {
  const wrap = document.createElement("div");
  wrap.className = "theme-block" + (t.theme_name === state.activeTheme ? " active" : "");
  wrap.dataset.theme = t.theme_name;

  const zt = t.zt_count || 0;
  const strong = t.strong_count || 0;
  const total = zt + strong;

  const item = document.createElement("div");
  item.className = "theme-item";
  item.title = `${t.theme_name}：涨停 ${zt} 只` +
    (strong ? ` + 创/科异动 ${strong} 只 = ${total} 只` : "");
  item.innerHTML = `<span class="t-name">${esc(t.theme_name)}
    <span class="t-count">${total} 只${strong ? `<i class="t-split">${zt}+${strong}</i>` : ""}</span></span>`;
  item.onclick = () => onThemeClick(wrap);

  const detail = document.createElement("div");
  detail.className = "theme-detail";

  wrap.append(item, detail);
  return wrap;
}

function onThemeClick(wrap) {
  const theme = wrap.dataset.theme;
  const wasOpen = wrap.classList.contains("open");

  // 手风琴：先全部收起，同一时间只展开一个，页面不会越拉越长
  $("themeList").querySelectorAll(".theme-block.open")
    .forEach((b) => b.classList.remove("open"));
  $("themeList").querySelectorAll(".theme-block.active")
    .forEach((b) => b.classList.remove("active"));

  if (wasOpen) {                 // 再点一次 = 收起
    state.activeTheme = null;
    renderLeaders(null);
    return;
  }

  wrap.classList.add("open", "active");
  state.activeTheme = theme;
  renderDetail(wrap);
  renderLeaders(theme);          // 桌面端右侧同步；手机上这段区域是隐藏的

  /* 只保证展开后的第一行龙头露出来，不做整页跳转。
     目标位置放在视口下半部，用户视线不用重新找。 */
  if (isNarrow()) {
    const first = wrap.querySelector(".theme-detail").firstElementChild;
    if (first) {
      const fr = first.getBoundingClientRect();
      if (fr.bottom > window.innerHeight - 8) {
        window.scrollBy({ top: fr.top - window.innerHeight * 0.52, behavior: "smooth" });
      }
    }
  }
}

/* 展开区里的龙头：结构与手机版表格一样的两行卡片 */
function rowHtml(l) {
  const isLeader = !!l.leader_label;
  const badge = "rank-badge" + (isLeader ? "" : " dim") + (l.is_override ? " overridden" : "");
  const label = isLeader ? l.leader_label : "#" + l.final_rank;
  const fd = l.float_mv > 0 ? (l.fengdan_amt / l.float_mv * 100).toFixed(2) + "%" : "-";
  const pct = Number(l.high_pct) || 0;
  return `<div class="ldr-row">
    <span class="ldr-rank"><span class="${badge}">${esc(label)}</span></span>
    <span class="ldr-main">
      <span class="stock-code">${esc(l.code)}</span>
      <span class="stock-name">${esc(l.name)}</span>
    </span>
    <span class="ldr-lb">${l.lianban_height ? l.lianban_height + "板" : "-"}</span>
    <span class="ldr-pct">${pct ? pct.toFixed(2) + "%" : "-"}</span>
    <span class="ldr-sub">封单 ${fd} · 上板 ${esc(l.zt_time || "-")}</span>
  </div>`;
}

function renderDetail(wrap) {
  const theme = wrap.dataset.theme;
  const detail = wrap.querySelector(".theme-detail");
  const rows = (state.day.leaders || [])
    .filter((l) => l.theme_name === theme)
    .sort((a, b) => a.final_rank - b.final_rank);
  detail.innerHTML = rows.length
    ? rows.map(rowHtml).join("")
    : `<div class="ldr-empty">这个题材暂无龙头</div>`;
}

function renderLeaders(theme) {
  const info = (state.day.themes || []).find((t) => t.theme_name === theme);
  const hint = info
    ? `${info.zt_count} 只涨停` + (info.strong_count ? ` + ${info.strong_count} 异动` : "")
    : "";
  $("leaderTitle").innerHTML =
    `龙头梯队 · ${esc(theme || "—")} <span class="hint">${hint}</span>`;

  const rows = (state.day.leaders || [])
    .filter((l) => l.theme_name === theme)
    .sort((a, b) => a.final_rank - b.final_rank);

  const tbody = $("leaderTable").querySelector("tbody");
  tbody.innerHTML = "";
  rows.forEach((l) => {
    const tr = document.createElement("tr");
    const isLeader = !!l.leader_label;
    const badge = "rank-badge" + (isLeader ? "" : " dim") + (l.is_override ? " overridden" : "");
    const label = isLeader ? l.leader_label : "#" + l.final_rank;
    const fd = l.float_mv > 0
      ? (l.fengdan_amt / l.float_mv * 100).toFixed(2) + "%" : "-";
    const pct = Number(l.high_pct) || 0;
    tr.innerHTML = `
      <td><span class="${badge}">${esc(label)}</span></td>
      <td><span class="stock-code">${esc(l.code)}</span><span class="stock-name">${esc(l.name)}</span></td>
      <td>${l.lianban_height ? l.lianban_height + "板" : "-"}</td>
      <td class="pct">${pct ? pct.toFixed(2) + "%" : "-"}</td>
      <td>${fd}</td>
      <td>${esc(l.zt_time || "-")}</td>`;
    tbody.appendChild(tr);
  });
}

function renderUnassigned(u) {
  const box = $("unassignedBox");
  if (!u || !u.count) { box.hidden = true; return; }
  box.hidden = false;
  $("unassignedCount").textContent = u.count;
  $("unassignedList").innerHTML = u.stocks.map((s) => `
    <div class="unassigned-item">
      <span class="unassigned-code">${esc(s.code)}</span>
      <span class="unassigned-name">${esc(s.name)}</span>
      <span class="unassigned-status">${esc(s.status)}</span>
      <span class="unassigned-reason">${esc(s.reason || (s.concepts || []).join("·"))}</span>
    </div>`).join("");
}

/* ---------------- 爆量题材（只读版：数据已在 JSON 里预聚合） ---------------- */
function fmtVol(v) {
  const n = Number(v) || 0;
  if (!n) return "-";
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return String(Math.round(n));
}

function burstData() {
  const b = state.day && state.day.burst;
  return b && b.ready ? b : null;
}

function burstSorted() {
  const b = burstData();
  const m = state.burstMult;
  return ((b && b.themes) || [])
    .filter((t) => (t["count_" + m] || 0) > 0)
    .sort((x, y) => ((y["count_" + m] || 0) - (x["count_" + m] || 0)) ||
                    ((y["max_" + m] || 0) - (x["max_" + m] || 0)));
}

function burstRows(theme) {
  const b = burstData();
  const m = state.burstMult;
  return ((b && b.stocks) || [])
    .filter((s) => s.theme === theme && s.ratio >= m)
    .sort((x, y) => y.ratio - x.ratio);
}

function renderBurst() {
  const list = $("themeList");
  list.innerHTML = "";
  const b = burstData();
  const thread = $("burstHint");
  if (!b) {
    $("themeCount").textContent = "";
    list.innerHTML = `<div class="ldr-empty">该日没有爆量数据（需在本地回补成交量后重新导出）</div>`;
    $("burstTable").querySelector("tbody").innerHTML = "";
    $("leaderTitle").innerHTML = `爆量明细 <span class="hint">无数据</span>`;
    if (thread) thread.textContent = "";
    return;
  }
  const bw = (b.base_window || []).map((x) => x.slice(5)).join(" / ");
  if (thread) thread.textContent = (bw ? `${b.base_desc}（${bw}）` : (b.base_desc || ""));
  const m = state.burstMult;
  const th = burstSorted();
  const tot = b.totals || {};
  $("themeCount").textContent = `${th.length} 个板块 · ≥${m}倍 · 去重 ${tot["surge_" + m] || 0} 只` +
    (tot["unmapped_" + m] ? ` · 未归类 ${tot["unmapped_" + m]}` : "");
  th.forEach((t) => list.appendChild(makeBurstBlock(t)));
  if (th.length) selectBurst(th[0].theme_name);
  else {
    $("burstTable").querySelector("tbody").innerHTML = "";
    $("leaderTitle").innerHTML = `爆量明细 <span class="hint">该倍数下没有板块</span>`;
  }
}

function makeBurstBlock(t) {
  const m = state.burstMult;
  const n = t["count_" + m] || 0;
  const mx = t["max_" + m] || 0;
  const zt = t.zt_2 || 0;
  const wrap = document.createElement("div");
  wrap.className = "theme-block" + (t.theme_name === state.burstTheme ? " active" : "");
  wrap.dataset.theme = t.theme_name;

  const item = document.createElement("div");
  item.className = "theme-item";
  item.title = `${t.theme_name}：≥${m}倍爆量 ${n} 只` +
    (zt ? `（其中涨停 ${zt} 只）` : "") + `，板块内最高 ${mx.toFixed(2)} 倍`;
  item.innerHTML = `<span class="t-name">${esc(t.theme_name)}
    <span class="t-count">${n} 只${zt ? `<i class="t-split">涨停${zt}</i>` : ""}\
<i class="t-ratio">${mx.toFixed(1)}×</i></span></span>`;
  item.onclick = () => onBurstClick(wrap);

  const detail = document.createElement("div");
  detail.className = "theme-detail";
  wrap.append(item, detail);
  return wrap;
}

function onBurstClick(wrap) {
  const wasOpen = wrap.classList.contains("open");
  $("themeList").querySelectorAll(".theme-block.open")
    .forEach((b) => b.classList.remove("open"));
  $("themeList").querySelectorAll(".theme-block.active")
    .forEach((b) => b.classList.remove("active"));
  if (wasOpen) {
    state.burstTheme = null;
    $("burstTable").querySelector("tbody").innerHTML = "";
    $("leaderTitle").innerHTML = `爆量明细 <span class="hint">先选板块</span>`;
    return;
  }
  wrap.classList.add("open", "active");
  state.burstTheme = wrap.dataset.theme;
  renderBurstDetail(wrap);
  renderBurstTable(wrap.dataset.theme);
  if (isNarrow()) {
    const first = wrap.querySelector(".theme-detail").firstElementChild;
    if (first) {
      const fr = first.getBoundingClientRect();
      if (fr.bottom > window.innerHeight - 8) {
        window.scrollBy({ top: fr.top - window.innerHeight * 0.52, behavior: "smooth" });
      }
    }
  }
}

function renderBurstDetail(wrap) {
  const detail = wrap.querySelector(".theme-detail");
  const rows = burstRows(wrap.dataset.theme);
  detail.innerHTML = rows.length ? rows.map((s) => `<div class="ldr-row">
    <span class="ldr-rank"><span class="burst-ratio">${s.ratio.toFixed(1)}×</span></span>
    <span class="ldr-main"><span class="stock-code">${esc(s.code)}</span>
      <span class="stock-name">${esc(s.name)}</span></span>
    <span class="ldr-lb">${fmtVol(s.volume)}</span>
    <span class="ldr-pct">${Number(s.high_pct).toFixed(1)}%</span>
    <span class="ldr-sub">基准 ${fmtVol(s.base_volume)}（${esc((s.base_date || "").slice(5))}）</span>
  </div>`).join("") : `<div class="ldr-empty">该倍数下没有个股</div>`;
}

function selectBurst(theme) {
  state.burstTheme = theme;
  $("themeList").querySelectorAll(".theme-block.active")
    .forEach((b) => b.classList.remove("active"));
  const cur = [...$("themeList").querySelectorAll(".theme-block")]
    .find((b) => b.dataset.theme === theme);
  if (cur) cur.classList.add("active");
  renderBurstTable(theme);
}

function renderBurstTable(theme) {
  const rows = burstRows(theme);
  const m = state.burstMult;
  const info = (burstSorted() || []).find((t) => t.theme_name === theme) ||
    (((burstData() || {}).themes || []).find((t) => t.theme_name === theme));
  const mx = info ? (info["max_" + m] || 0) : 0;
  $("leaderTitle").innerHTML = `爆量明细 · ${esc(theme || "—")} ` +
    `<span class="hint">${rows.length} 只 · 最高 ${mx.toFixed(2)} 倍</span>`;
  const tbody = $("burstTable").querySelector("tbody");
  tbody.innerHTML = rows.map((s) => {
    const srcTag = s.src === "reason" ? "原因"
      : s.src === "zt_today" ? "涨停" : (s.src === "concept" ? "概念" : "—");
    return `<tr title="${esc(s.reason ? `涨停原因（${s.reason_date}）：${s.reason}` : "无涨停原因，按概念归类")}">
      <td><span class="stock-code">${esc(s.code)}</span><span class="stock-name">${esc(s.name)}</span>
          <span class="burst-src">${srcTag}</span></td>
      <td class="pct">${Number(s.high_pct).toFixed(2)}%</td>
      <td class="burst-vol">${fmtVol(s.volume)}</td>
      <td class="burst-vol">${fmtVol(s.base_volume)}</td>
      <td class="burst-ratio">${s.ratio.toFixed(2)}×</td>
      <td class="burst-date">${esc((s.base_date || "").slice(5))}</td>
    </tr>`;
  }).join("");
}

function setView(v) {
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
  // 两张表共用 right 容器，不归零会继承上一张表的滚动位置
  const right = document.querySelector("main .right");
  if (right) right.scrollTop = 0;
  if (v === "burst") {
    state.activeTheme = null;
    $("leaderTitle").innerHTML = `爆量明细 <span class="hint">← 点击左侧板块查看</span>`;
    renderBurst();
  } else {
    state.burstTheme = null;
    const themes = (state.day && state.day.themes) || [];
    state.activeTheme = themes.length ? themes[0].theme_name : null;
    renderThemes(themes);
    renderUnassigned(state.day && state.day.unassigned);
    renderLeaders(state.activeTheme);
  }
}

function bindBurst() {
  $("viewScope").onclick = (e) => {
    const b = e.target.closest(".seg-btn");
    if (b && b.dataset.view !== state.view) setView(b.dataset.view);
  };
  $("burstMult").onclick = (e) => {
    const b = e.target.closest(".seg-btn");
    const m = b ? Number(b.dataset.mult) : 0;
    if (!m || m === state.burstMult) return;
    state.burstMult = m;
    [...$("burstMult").querySelectorAll(".seg-btn")]
      .forEach((x) => x.classList.toggle("on", Number(x.dataset.mult) === m));
    if (state.view === "burst") renderBurst();
  };
}

/* ---------------- 导入同花顺 ---------------- */
function currentCodes() {
  return ((state.day && state.day.exports) || {})[state.activeTheme] || [];
}

function bindThs() {
  $("btnThs").onclick = openThs;
  const close = () => $("thsModal").classList.remove("open");
  $("btnCloseThs").onclick = close;
  $("thsModal").onclick = (e) => { if (e.target === $("thsModal")) close(); };
  $("thsImgBtn").onclick = doImage;
  $("thsTxtBtn").onclick = doTxt;
  $("thsCopyBtn").onclick = doCopy;
}

function openThs() {
  const codes = currentCodes();
  $("thsSub").textContent = state.activeTheme ? `当前题材：${state.activeTheme}` : "";
  $("thsSummary").textContent = codes.length
    ? `${state.activeTheme}：${codes.length} 只成分股，按龙头顺序排列`
    : "这个题材没有成分股";
  setProg("");
  $("thsPreview").innerHTML = "";
  $("thsModal").classList.add("open");
}

/* 白底黑字、单列竖排、只放 6 位数字 —— 同花顺 OCR 只认代码，
   混进日期/题材名会被当成股票代码读走 */
function codeCanvas(codes) {
  const { font, line, pad, dpr } = IMG;
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
  g.fillStyle = "#ffffff";     // 必须实底：透明底会被 OCR 当黑底
  g.fillRect(0, 0, w, h);
  g.fillStyle = "#000000";
  g.font = spec;
  g.textAlign = "center";
  g.textBaseline = "middle";
  codes.forEach((code, i) => g.fillText(code, w / 2, pad + line * (i + 0.5)));
  return c;
}

function toFile(canvas, name) {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => {
      resolve(blob ? new File([blob], name, { type: "image/png" }) : null);
    }, "image/png");
  });
}

async function doImage() {
  const codes = currentCodes();
  if (!codes.length) return setProg("没有成分股");
  const chunks = [];
  for (let i = 0; i < codes.length; i += IMG.chunk) chunks.push(codes.slice(i, i + IMG.chunk));
  setProg(`正在渲染 ${chunks.length} 张…`);
  const canvases = chunks.map(codeCanvas);

  const base = safeName(state.activeTheme);
  const files = [];
  for (let i = 0; i < canvases.length; i++) {
    const f = await toFile(canvases[i], `${base}_${state.date}_${i + 1}.png`);
    if (f) files.push(f);
  }

  if (navigator.canShare && navigator.canShare({ files })) {
    try {
      await navigator.share({ files, title: `复盘吧 · ${state.activeTheme}` });
      return setProg(`已交给系统分享（${files.length} 张），选「存储到照片」即可`);
    } catch (e) {
      if (e && e.name === "AbortError") return setProg("已取消");
    }
  }

  const box = $("thsPreview");
  box.innerHTML = "";
  canvases.forEach((c, i) => {
    const img = document.createElement("img");
    img.src = c.toDataURL("image/png");
    img.className = "ths-preview";
    img.alt = `第 ${i + 1} 张`;
    box.appendChild(img);
  });
  setProg(`已生成 ${canvases.length} 张：长按图片 → 存储到照片`);
}

function doTxt() {
  const codes = currentCodes();
  if (!codes.length) return setProg("没有成分股");
  const blob = new Blob([codes.join("\n")], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${safeName(state.activeTheme)}_${state.date}.txt`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  setProg(`已生成 TXT（${codes.length} 只），去同花顺「添加股票 → 上传 txt」`);
}

async function doCopy() {
  const codes = currentCodes();
  if (!codes.length) return setProg("没有成分股");
  try {
    await navigator.clipboard.writeText(codes.join("\n"));
    setProg(`已复制 ${codes.length} 个代码，去同花顺粘贴`);
  } catch (e) {
    setProg("复制失败（需要 HTTPS），请改用「生成 TXT」");
  }
}

boot();
