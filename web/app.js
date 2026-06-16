/* Pokémon Vortex Agent — dashboard client */
"use strict";

const $ = id => document.getElementById(id);
let ws = null;
let lastChats = [];

/* ── WebSocket ───────────────────────────────────────── */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = e => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "snapshot") render(msg.data);
    } catch (_) {}
  };
}

function setConn(on) {
  const el = $("connStatus");
  el.textContent = on ? "● đã kết nối" : "● mất kết nối — đang thử lại…";
  el.className = "conn-status " + (on ? "on" : "off");
}

function sendCommand(cmd) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "command", cmd }));
}

/* ── Chat ────────────────────────────────────────────── */
$("chatForm").addEventListener("submit", e => {
  e.preventDefault();
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text || !ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "chat", text }));
  input.value = "";
});

/* ── Render ──────────────────────────────────────────── */
function render(snap) {
  const s = snap.state || {};
  renderHeader(s);
  renderArena(s);
  renderStats(s, snap.summary || {});
  renderTeam(snap.team || []);
  renderOpt(s);
  renderLogs(snap.logs || []);
  renderChats(snap.chats || []);
}

function renderHeader(s) {
  $("zoneInfo").textContent = `zone: ${s.zone || "?"} · tile: ${s.tile ? s.tile.join(",") : "?"} · ${s.action || ""} ${s.reason ? "— " + s.reason : ""}`;
  $("screenPill").textContent = s.screen || "?";
  $("modePill").textContent = "mode: " + (s.mode || "?");

  const up = s.uptime || 0;
  const hh = String(Math.floor(up / 3600)).padStart(2, "0");
  const mm = String(Math.floor((up % 3600) / 60)).padStart(2, "0");
  const ss = String(up % 60).padStart(2, "0");
  $("uptimePill").textContent = `⏱ ${hh}:${mm}:${ss}`;

  const pill = $("statusPill");
  if (s.paused) {
    pill.className = "status-pill paused";
    pill.innerHTML = '<span class="dot"></span>PAUSED' + (s.pause_reason === "rare" ? " (RARE)" : "");
  } else {
    pill.className = "status-pill running";
    pill.innerHTML = '<span class="dot"></span>RUNNING';
  }

  // Rare banner
  const banner = $("rareBanner");
  if (s.is_rare && s.pokemon) {
    banner.classList.remove("hidden");
    $("rareName").textContent = displayName(s.pokemon, s.variant, true);
  } else banner.classList.add("hidden");
}

/* Tên đã chứa variant prefix (vd "Dark Lunatone") → tránh lặp khi gắn tag */
function displayName(name, variant, withVariant) {
  if (!name) return "?";
  let base = name;
  if (variant && name.toLowerCase().startsWith(variant.toLowerCase() + " ")) {
    base = name.slice(variant.length + 1);
  }
  return withVariant && variant ? `${variant.toUpperCase()} ${base}` : base;
}

function hpClass(pct) { return pct == null ? "" : pct < 25 ? "crit" : pct < 55 ? "warn" : ""; }

function renderArena(s) {
  const inBattle = !!s.pokemon;
  $("arenaEmpty").classList.toggle("hidden", inBattle);
  $("arenaField").classList.toggle("hidden", !inBattle);
  $("phaseBadge").textContent = inBattle
    ? (s.battle_phase === "select" ? "chọn chiêu thức…" :
       s.battle_phase === "result" ? "kết quả lượt đánh" :
       s.battle_phase === "popup"  ? "pokémon xuất hiện!" : "đang chiến đấu")
    : (s.screen === "map" ? `đang quét ${s.zone || "?"}…` : s.screen || "…");

  if (!inBattle) { $("movesRow").innerHTML = ""; $("battleOutput").textContent = ""; return; }

  // enemy
  $("enemyName").textContent = displayName(s.pokemon, s.variant, false);
  $("enemyLv").textContent = s.poke_level ? "Lv." + s.poke_level : "";
  const vt = $("enemyVariant");
  if (s.variant) { vt.textContent = s.variant.toUpperCase(); vt.classList.remove("hidden"); }
  else vt.classList.add("hidden");
  const ehp = s.enemy_hp_pct;
  const ebar = $("enemyHpBar");
  ebar.style.width = (ehp != null ? ehp : 100) + "%";
  ebar.className = "hpbar-fill " + hpClass(ehp);
  $("enemyHpText").textContent = s.enemy_hp != null ? `HP ${s.enemy_hp}${ehp != null ? " (" + ehp + "%)" : ""}` : "";
  setSprite($("enemySprite"), s.enemy_img, s.pokemon, "front");

  // mine
  $("myName").textContent = s.my_name || "…";
  $("myLv").textContent = s.my_level ? "Lv." + s.my_level : "";
  const mhp = s.my_hp_pct;
  const mbar = $("myHpBar");
  mbar.style.width = (mhp != null ? mhp : 100) + "%";
  mbar.className = "hpbar-fill mine-fill " + hpClass(mhp);
  $("myHpText").textContent = s.my_hp != null ? `HP ${s.my_hp}${mhp != null ? " (" + mhp + "%)" : ""}` : "";
  setSprite($("mySprite"), s.my_img, s.my_name, "back");

  // moves
  const md = s.moves_detail && s.moves_detail.length ? s.moves_detail : (s.moves || []).map(m => ({ name: m }));
  $("movesRow").innerHTML = md.map(m =>
    `<div class="move-chip"><span class="type-dot t-${m.type || "Normal"}"></span>${esc(m.name || "?")}${m.type ? ` <span style="opacity:.55;font-size:10px">${m.type}</span>` : ""}</div>`
  ).join("");
}

function setSprite(img, src, name, side) {
  let url = src;
  if (!url && name) {
    url = `https://static.pokemon-vortex.com/v6/images/pokemon_${side === "back" ? "back" : "front"}/${encodeURIComponent(name)}.png`;
  }
  if (url && img.src !== url) img.src = url;
  img.style.visibility = url ? "visible" : "hidden";
}

function renderStats(s, sum) {
  // Lifetime (DB); nếu DB trống thì hiển thị số liệu phiên hiện tại
  const useSession = !(sum.battles > 0) && (s.battles > 0);
  $("statBattles").textContent = useSession ? s.battles : (sum.battles ?? 0);
  $("statWins").textContent    = useSession ? s.wins    : (sum.wins ?? 0);
  $("statLosses").textContent  = useSession ? s.losses  : (sum.losses ?? 0);
  $("statCatches").textContent = useSession ? s.catches : (sum.catches ?? 0);
  $("sessionLine").textContent =
    `Phiên này: ${s.battles || 0} trận · ${s.wins || 0} thắng · ${s.losses || 0} thua · ${s.catches || 0} bắt · LLM ${s.llm_calls || 0} calls`;

  let wr = sum.winrate;
  if (wr == null && useSession) {
    const decided = (s.wins || 0) + (s.losses || 0);
    if (decided > 0) wr = Math.round((s.wins / decided) * 1000) / 10;
  }
  $("winrateNum").textContent = wr != null ? wr + "%" : "–";
  const circ = 2 * Math.PI * 50;
  $("donutFill").setAttribute("stroke-dasharray", `${wr != null ? (wr / 100) * circ : 0} ${circ}`);

  const hl = $("hardestList");
  const hardest = sum.hardest || [];
  hl.innerHTML = hardest.length
    ? hardest.map(h => `<div class="hardest-item"><span>${esc(h.pokemon)}</span><b>${h.losses} thua / ${h.total} trận</b></div>`).join("")
    : '<span class="dim">chưa thua trận nào 🎉</span>';
}

function renderTeam(team) {
  if (!team || !team.length) return;
  $("teamGrid").innerHTML = team.slice(0, 6).map(p => {
    const img = p.imgSrc || `https://static.pokemon-vortex.com/v6/images/pokemon_front/${encodeURIComponent(p.name || "")}.png`;
    const moves = (p.moves || []).slice(0, 4).join(" · ");
    return `<div class="poke-card">
      <img src="${esc(img)}" loading="lazy" onerror="this.style.opacity=.2">
      <div class="pname" title="${esc(p.name || "?")}">${esc(p.name || "?")}</div>
      <div class="plv">Lv.${p.level ?? "?"}</div>
      <div class="pmoves">${esc(moves || "—")}</div>
    </div>`;
  }).join("");
}

let optLoading = false;
function renderOpt(s) {
  const body = $("optBody");
  if (s.recommendation) {
    optLoading = false;
    body.classList.remove("loading");
    body.textContent = s.recommendation;
  } else if (optLoading) {
    body.classList.add("loading");
    body.textContent = "🧠 LLM đang phân tích đội hình + lịch sử thắng thua…";
  }
}
document.querySelector(".opt-card .btn-accent").addEventListener("click", () => { optLoading = true; });

function logClass(line) {
  if (/ERROR|CRITICAL|LOSE|THUA/i.test(line)) return "ln-err";
  if (/WARN|RARE|LEARN/i.test(line)) return "ln-warn";
  if (/SUCCESS|WIN|CAUGHT|THẮNG/i.test(line)) return "ln-ok";
  if (/INFO/.test(line)) return "ln-info";
  return "";
}

function renderLogs(logs) {
  const feed = $("logFeed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  feed.innerHTML = logs.map(l => `<div class="${logClass(l)}">${esc(l)}</div>`).join("");
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

function renderChats(chats) {
  if (JSON.stringify(chats) === JSON.stringify(lastChats)) return;
  lastChats = chats;
  const feed = $("chatFeed");
  feed.innerHTML = chats.map(c => {
    // c dạng "HH:MM:SS <nội dung đã strip markup>"
    const body = c.replace(/^\d\d:\d\d:\d\d\s*/, "");
    let cls = "sys";
    let text = body;
    if (/^Bạn:|^>>\s/.test(body)) { cls = "user"; text = body.replace(/^Bạn:\s*|^>>\s*/, ""); }
    else if (/^Agent:/.test(body)) { cls = "agent"; text = body.replace(/^Agent:\s*/, ""); }
    return `<div class="chat-msg ${cls}">${esc(text)}</div>`;
  }).join("");
  feed.scrollTop = feed.scrollHeight;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

connect();
