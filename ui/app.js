"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = (name, ...a) => window.pywebview.api[name](...a);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = { lib: null, view: "library", filter: "all", sort: "recent", q: "", game: null, achTab: "all", achSort: "default", achQ: "",
            actDays: 30, task: null, live: {}, wasRunning: false };
try { Object.assign(S, JSON.parse(localStorage.getItem("gt.prefs") || "{}")); } catch (e) {}
const savePrefs = () => { try { localStorage.setItem("gt.prefs", JSON.stringify({ filter: S.filter, sort: S.sort, actDays: S.actDays, achSort: S.achSort })); } catch (e) {} };

// ---------------------------------------------------------------- formatting
function dur(s, short) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return s ? "<1m" : "0m";
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  if (!h) return `${m}m`;
  if (short && h >= 100) return `${h}h`;
  return `${h}h ${String(m).padStart(2, "0")}m`;
}
function clock(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}`;
}
function ago(ts) {
  if (!ts) return "Never";
  const d = Date.now() / 1000 - ts;
  if (d < 90) return "Just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  const then = new Date(ts * 1000), now = new Date();
  const days = Math.round((new Date(now.toDateString()) - new Date(then.toDateString())) / 864e5);
  if (days === 0) return `Today, ${then.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return dateStr(ts);
}
const dateStr = ts => new Date(ts * 1000).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
const dateTime = ts => new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
const pct = (a, b) => b ? Math.round(a / b * 100) : 0;
function hue(str) { let h = 0; for (const c of str) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function artStyle(g) {
  if (g.cover) return `style="background-image:url('${g.cover}')"`;
  const h = hue(g.name);
  return `style="background:linear-gradient(150deg,hsl(${h} 55% 32%),hsl(${(h + 50) % 360} 60% 16%))"`;
}
const ICON = {
  clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  trophy: '<svg viewBox="0 0 24 24"><path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0zM17 5h3v2a3 3 0 0 1-3 3M7 5H4v2a3 3 0 0 0 3 3"/></svg>',
  games: '<svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="4"/><path d="M6 12h4M8 10v4M15 11h.01M18 13h.01"/></svg>',
  star: '<svg viewBox="0 0 24 24"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z"/></svg>',
  cal: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/></svg>',
  back: '<svg viewBox="0 0 24 24"><path d="M15 18l-6-6 6-6"/></svg>',
  refresh: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></svg>',
  link: '<svg viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>',
  edit: '<svg viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  img: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  eye: '<svg viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  lock: '<svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 4v16l13-8z"/></svg>',
};

// ---------------------------------------------------------------- navigation
function show(view) {
  S.view = view;
  $$(".view").forEach(v => v.hidden = v.id !== "v-" + view);
  $$("nav button").forEach(b => b.classList.toggle("active", b.dataset.view === view || (view === "game" && b.dataset.view === "library")));
  $("#main").scrollTop = 0;
}
$$("nav button").forEach(b => b.onclick = () => {
  const v = b.dataset.view;
  show(v);
  if (v === "library") loadLibrary();
  if (v === "activity") loadActivity();
  if (v === "settings") loadSettings();
});

// ---------------------------------------------------------------- library
async function loadLibrary() {
  S.lib = await api("library");
  renderLibrary();
}
function libFiltered() {
  const q = S.q.trim().toLowerCase();
  let g = S.lib.games.filter(x => (S.filter === "hidden") === x.hidden);
  if (S.filter === "all") g = g.filter(x => x.installed);
  else if (S.filter === "notinst") g = g.filter(x => !x.installed);
  else if (S.filter !== "hidden") g = g.filter(x => x.installed || x.played > 0 || x.ach_unlocked > 0);
  if (q) g = g.filter(x => x.name.toLowerCase().includes(q));
  if (S.filter === "played") g = g.filter(x => x.played > 0);
  if (S.filter === "ach") g = g.filter(x => x.ach_total && x.ach_unlocked < x.ach_total && x.ach_unlocked > 0);
  if (S.filter === "perfect") g = g.filter(x => x.ach_total && x.ach_unlocked === x.ach_total);
  if (S.filter === "none") g = g.filter(x => !x.ach_total);
  const by = {
    recent: (a, b) => (b.playing - a.playing) || ((b.last_played || 0) - (a.last_played || 0)) || (b.played - a.played) || a.name.localeCompare(b.name),
    time: (a, b) => b.played - a.played,
    ach: (a, b) => (pct(b.ach_unlocked, b.ach_total) - pct(a.ach_unlocked, a.ach_total)) || (b.ach_total - a.ach_total),
    name: (a, b) => a.name.localeCompare(b.name),
    added: (a, b) => (b.added_at || 0) - (a.added_at || 0),
  }[S.sort];
  return g.sort(by);
}
function renderLibrary() {
  const all = S.lib.games.filter(x => !x.hidden && (x.installed || x.played > 0 || x.ach_unlocked > 0));
  const time = all.reduce((t, x) => t + x.played, 0);
  const tot = all.reduce((t, x) => t + x.ach_total, 0), un = all.reduce((t, x) => t + x.ach_unlocked, 0);
  const perfect = all.filter(x => x.ach_total && x.ach_unlocked === x.ach_total).length;
  $("#libStats").innerHTML = `
    <div class="stat"><div class="ic">${ICON.clock}</div><div><div class="v">${dur(time, true)}</div><div class="k">Total playtime (all games)</div></div></div>
    <div class="stat"><div class="ic violet">${ICON.games}</div><div><div class="v">${all.length}</div><div class="k">${all.filter(x => x.installed).length} installed · ${all.filter(x => !x.installed).length} not installed</div></div></div>
    <div class="stat"><div class="ic gold">${ICON.trophy}</div><div><div class="v">${un} <span class="muted" style="font-size:14px;font-weight:500">/ ${tot}</span></div><div class="k">Achievements · ${pct(un, tot)}%</div></div></div>
    <div class="stat"><div class="ic green">${ICON.star}</div><div><div class="v">${perfect}</div><div class="k">Perfect games (100%)</div></div></div>`;
  const list = libFiltered();
  $("#grid").innerHTML = list.length ? list.map(g => {
    const p = pct(g.ach_unlocked, g.ach_total), full = g.ach_total && g.ach_unlocked === g.ach_total;
    return `<div class="tile" data-id="${g.id}">
      <div class="art ${g.cover ? "" : "fallback"}" ${artStyle(g)}>${g.cover ? "" : esc(g.name)}
        ${g.playing ? `<span class="badge live">${ICON.play}Playing</span>` : !g.installed ? `<span class="badge gone">Not installed</span>` : ""}
        ${full ? `<span class="badge perfect">100%</span>` : ""}
        ${g.unlock_missing ? `<span class="badge warn" title="Played, but the file where this game records unlocks wasn't found">Achievements not found</span>` : ""}</div>
      <div class="info"><div class="nm" title="${esc(g.name)}">${esc(g.name)}</div>
        <div class="meta"><span>${g.played ? dur(g.played, true) : "Not played"}</span><span>${g.ach_total ? `${g.ach_unlocked}/${g.ach_total}` : ""}</span></div>
        ${g.ach_total ? `<div class="pbar ${full ? "full" : ""}"><i style="width:${p}%"></i></div>` : `<div class="pbar" style="opacity:.35"><i style="width:0"></i></div>`}
      </div></div>`;
  }).join("") : `<div class="empty" style="grid-column:1/-1">No games match.</div>`;
  $$("#grid .tile").forEach(t => t.onclick = () => openGame(t.dataset.id));
}
$("#q").oninput = e => { S.q = e.target.value; renderLibrary(); };
$("#sort").onchange = e => { S.sort = e.target.value; savePrefs(); renderLibrary(); };
$$("#filters button").forEach(b => b.onclick = () => { S.filter = b.dataset.f; savePrefs(); syncFilterUi(); renderLibrary(); });
function syncFilterUi() { $$("#filters button").forEach(x => x.classList.toggle("on", x.dataset.f === S.filter)); $("#sort").value = S.sort; }

// ---------------------------------------------------------------- game page
async function openGame(id, keepScroll) {
  const top = $("#main").scrollTop;
  const g = await api("game", id);
  if (!g) return;
  S.game = g;
  if (S.view !== "game") { S.achTab = "all"; S.achQ = ""; }
  show("game");
  renderGame();
  if (keepScroll) $("#main").scrollTop = top;
}
function renderGame() {
  const g = S.game, el = $("#v-game");
  const total = g.tracked + g.baseline;
  const un = g.achievements.filter(a => a.unlocked_at).length, n = g.achievements.length;
  const last = g.sessions[0];
  const avg = g.session_count ? g.tracked / g.session_count : 0;
  const heroBg = g.hero ? `background-image:url('${g.hero}')` : g.cover ? `background-image:url('${g.cover}');filter:blur(30px) brightness(.6);transform:scale(1.2)` :
    `background:linear-gradient(150deg,hsl(${hue(g.name)} 50% 22%),var(--bg))`;
  const tags = [
    g.playing ? `<span class="tag live">● Playing now</span>` : "",
    g.appid ? `<span class="tag" title="${esc(g.appid_source || "")}">Steam AppID ${g.appid} · ${esc(shortSrc(g.appid_source))}</span>` : `<span class="tag warn">No Steam match</span>`,
    g.schema_source ? `<span class="tag">Achievements: ${esc(g.schema_source)}</span>` : g.schema_error ? `<span class="tag warn" title="${esc(g.schema_error)}">${esc(g.schema_error)}</span>` : "",
    g.achievements.length && !g.unlock_found ? `<span class="tag warn">Unlock file not found</span>` : "",
    !g.installed ? `<span class="tag warn">Not installed</span>` : "",
    g.hidden ? `<span class="tag">Hidden</span>` : "",
  ].join("");
  el.innerHTML = `
    <div class="hero"><div style="position:absolute;inset:0;background-size:cover;background-position:center 25%;${heroBg}"></div>
      <button class="btn back" id="gBack">${ICON.back}Library</button></div>
    <div class="ghead">
      <div class="cover" ${artStyle(g)}></div>
      <div class="t">
        <h1>${esc(g.name)}</h1>
        <div class="tags">${tags}</div>
        <div class="actions">
          <button class="btn primary" id="aRefresh">${ICON.refresh}Refresh achievements</button>
          <button class="btn" id="aMatch">${ICON.link}${g.appid ? "Change Steam match" : "Find on Steam"}</button>
          <button class="btn" id="aRename">${ICON.edit}Rename</button>
          <button class="btn" id="aCover">${ICON.img}Cover</button>
          <button class="btn" id="aFolder">${ICON.folder}Open folder</button>
          <button class="btn" id="aHide">${ICON.eye}${g.hidden ? "Unhide" : "Hide"}</button>
        </div>
      </div>
    </div>
    <div class="gstats">
      <div class="stat"><div class="ic">${ICON.clock}</div><div><div class="v" id="gTotal">${dur(total)}</div><div class="k">Total playtime${g.baseline ? ` · ${dur(g.baseline)} before tracking (${esc(g.baseline_source)})` : ""}</div></div></div>
      <div class="stat"><div class="ic violet">${ICON.games}</div><div><div class="v">${g.session_count}</div><div class="k">Sessions · avg ${dur(avg)}</div></div></div>
      <div class="stat"><div class="ic green">${ICON.cal}</div><div><div class="v" style="font-size:17px">${last ? ago(last.end) : g.ext_last_played ? ago(g.ext_last_played) : "Never"}</div><div class="k">Last played${!last && g.ext_last_played ? " (Steam)" : ""}</div></div></div>
      <div class="stat"><div class="ring" style="--p:${pct(un, n)}"><span>${n ? pct(un, n) + "%" : "–"}</span></div><div><div class="v">${un} / ${n}</div><div class="k">Achievements</div></div></div>
      <div class="stat"><div class="ic gold">${ICON.trophy}</div><div><div class="v" style="font-size:17px">${rarest(g)}</div><div class="k">Rarest unlocked</div></div></div>
    </div>
    <div class="two">
      <div class="card"><h3>Playtime, last 30 days <small id="g30"></small></h3><div class="chart" id="gChart"></div></div>
      <div class="card"><h3>Sessions <span style="display:flex;gap:8px;align-items:center"><small>${g.session_count} recorded</small><button class="btn small ghost" id="aAddSess">${ICON.plus}Add</button></span></h3>
        <div class="sess">${g.sessions.length ? g.sessions.map(s => `
          <div class="srow"><div class="d">${dateTime(s.start)}<small>${s.live ? "in progress" : esc((s.exe || "").split("\\").pop())}</small></div>
          <b>${dur(s.end - s.start)}</b>${s.live ? "<span></span>" : `<button class="x" data-sid="${s.id}" title="Delete this session">${ICON.x}</button>`}</div>`).join("")
          : `<div class="muted" style="padding:6px">No sessions yet. Play the game and it's recorded automatically.</div>`}</div></div>
    </div>
    <h2>Achievements</h2>
    ${n ? `<div class="achbar">
      <div class="seg" id="achTabs">${["all", "unlocked", "locked"].map(t => `<button data-t="${t}" class="${S.achTab === t ? "on" : ""}">${t[0].toUpperCase() + t.slice(1)} ${t === "all" ? n : t === "unlocked" ? un : n - un}</button>`).join("")}</div>
      <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="achQ" placeholder="Search achievements" value="${esc(S.achQ)}"></div>
      <select id="achSort">
        <option value="default">Game order</option><option value="recent">Recently unlocked</option>
        <option value="rarest">Rarest first</option><option value="common">Most common first</option><option value="name">Name</option>
      </select></div>
      <div class="achs" id="achs"></div>`
    : `<div class="card muted">${g.schema_error ? esc(g.schema_error) + "." : "No achievement list yet."}
       ${g.appid ? "" : " Use <b>Find on Steam</b> to match this game, then the library downloads its achievements."}</div>`}
    ${g.orphans.length ? `<div class="card" style="margin-top:14px"><h3>Unlocks not in the list <small>${g.orphans.length}</small></h3><div class="muted">${g.orphans.map(o => esc(o.api)).join(", ")}</div></div>` : ""}
    ${g.emu_fix && g.emu_fix.length ? `<div class="card warnbox" style="margin-top:18px">
      <h3>This copy of the game throws its achievements away</h3>
      <p>It uses the GSE / Goldberg Steam emulator, but the repack shipped without the emulator's achievement list
      (<b>steam_settings\\achievements.json</b>). The emulator only saves unlocks for achievements on that list, so every unlock so far was discarded.
      the library can write the list it downloaded (${g.achievements.length} achievements) into the game folder. From your next session on, unlocks are saved and tracked.
      Unlocks from past sessions can't be recovered, though some games award them again when they check your progress.</p>
      <p class="muted" style="font-size:12.5px">${g.emu_fix.map(esc).join("<br>")}</p>
      <div class="actions"><button class="btn primary" id="aEmuFix">${ICON.trophy}Fix: let this copy record achievements</button></div></div>` : ""}
    ${g.achievements.length && !g.emu_fix.length && !g.source_is_steam && !g.achievements.some(a => a.unlocked_at) && (g.baseline + g.tracked) >= 3600 && g.unlock_found ? `<div class="card warnbox" style="margin-top:18px">
      <h3>Played ${dur(g.baseline + g.tracked)} but no achievements recorded</h3>
      <p>the library is reading this game's unlock file${g.sources.length > 1 ? "s" : ""} (below), but nothing in ${g.sources.length > 1 ? "them" : "it"} is unlocked.
      Either you haven't earned one yet, or this copy's crack doesn't save achievements: some (for example ones that emulate Epic Online Services
      instead of Steam) never write them anywhere. If you know the copy keeps them somewhere else, point the library to that file.</p>
      <div class="actions"><button class="btn" id="aLocate2">${ICON.folder}Locate unlock file…</button></div></div>` : ""}
    ${g.achievements.length && !g.unlock_found && !g.emu_fix.length ? `<div class="card warnbox" style="margin-top:18px">
      <h3>Achievements you earn in this game aren't being read yet</h3>
      <p>the library has the achievement list but can't find the file where this copy of the game records unlocks.
      ${g.baseline || g.tracked ? "You've played it, so the file probably exists somewhere unusual." : "It may not exist yet: it usually appears on the first unlock, and the library is watching for it."}
      Repacks normally keep it in a folder named after the AppID (${g.appid || "?"}) containing <b>achievements.json</b> or <b>achievements.ini</b>,
      often inside the game folder (look for "Saved Games", "_Saved Games", "SteamData") or in Documents / AppData.
      Some cracks never save achievements at all (for example ones that emulate Epic Online Services instead of Steam).
      Then there is nothing to read, and only playtime can be tracked for that copy.</p>
      ${g.expected.length ? `<p class="muted" style="font-size:12.5px">Already watching:<br>${g.expected.map(esc).join("<br>")}</p>` : ""}
      <div class="actions"><button class="btn primary" id="aLocate">${ICON.folder}Locate unlock file…</button>
        <button class="btn" id="aLocateDir">${ICON.folder}Pick a folder to search…</button></div></div>` : ""}
    <div class="card" style="margin-top:18px"><h3>Where the data comes from <button class="btn small ghost" id="aAddSrc">${ICON.plus}Add unlock file</button></h3><div class="srcs">
      <div><b>Install folder</b><span data-open="${esc(g.install_dir)}">${esc(g.install_dir)}</span></div>
      ${g.sources.map(s => `<div><b>${esc(s.label)}</b><span data-open="${esc(s.path)}">${esc(s.path)}</span>${s.manual ? `<button class="x" data-rm="${esc(s.path)}" title="Stop reading this file">${ICON.x}</button>` : ""}</div>`).join("") || `<div><b>Unlock files</b><span>none yet. They appear when the game unlocks its first achievement.</span></div>`}
    </div></div>`;
  const achH = $$("h2", el).find(h => h.textContent === "Achievements");
  $$(".warnbox", el).reverse().forEach(w => { w.style.marginTop = "0"; w.style.marginBottom = "14px"; achH.after(w); });
  $("#gBack").onclick = () => { show("library"); loadLibrary(); };
  $("#aRefresh").onclick = async () => { if (await api("refresh_game", g.id)) pollSoon(); else note("Another refresh is running"); };
  $("#aMatch").onclick = () => matchDialog(g);
  $("#aRename").onclick = () => renameDialog(g);
  $("#aCover").onclick = () => coverDialog(g);
  $("#aFolder").onclick = () => api("open_path", g.install_dir);
  $("#aHide").onclick = async () => { await api("set_hidden", g.id, !g.hidden); openGame(g.id, true); };
  $("#aAddSess").onclick = () => sessionDialog(g);
  $$(".srow .x").forEach(b => b.onclick = async () => {
    if (!confirm("Delete this session? Its time is removed from the total.")) return;
    await api("delete_session", +b.dataset.sid); openGame(g.id, true);
  });
  $$("[data-open]").forEach(s => s.onclick = () => api("open_path", s.dataset.open));
  const locate = async folder => {
    const r = await api("add_unlock_source", g.id, folder);
    if (r.error) note(r.error);
    else if (r.ok) { note(`Now reading ${r.file.split("\\").pop()}: ${r.new} achievement${r.new === 1 ? "" : "s"} found`); openGame(g.id, true); }
  };
  if ($("#aLocate")) $("#aLocate").onclick = () => locate(false);
  if ($("#aLocate2")) $("#aLocate2").onclick = () => locate(false);
  if ($("#aEmuFix")) $("#aEmuFix").onclick = async () => {
    const w = await api("fix_emu_list", g.id);
    note(w.length ? "Done. Achievements will be saved from your next session." : "Nothing to fix");
    openGame(g.id, true);
  };
  if ($("#aLocateDir")) $("#aLocateDir").onclick = () => locate(true);
  $("#aAddSrc").onclick = () => locate(false);
  $$("[data-rm]").forEach(b => b.onclick = async () => { await api("remove_unlock_source", g.id, b.dataset.rm); openGame(g.id, true); });
  gameChart(g);
  if (n) {
    $$("#achTabs button").forEach(b => b.onclick = () => { S.achTab = b.dataset.t; $$("#achTabs button").forEach(x => x.classList.toggle("on", x === b)); renderAchs(); });
    $("#achQ").oninput = e => { S.achQ = e.target.value; renderAchs(); };
    $("#achSort").value = S.achSort;
    $("#achSort").onchange = e => { S.achSort = e.target.value; savePrefs(); renderAchs(); };
    renderAchs();
  }
}
function shortSrc(s) { s = s || ""; return s.startsWith("Steam store search") ? "matched by name" : s.replace("file: ", "from "); }
function rarest(g) {
  const u = g.achievements.filter(a => a.unlocked_at && a.pct != null).sort((a, b) => a.pct - b.pct)[0];
  return u ? `<span title="${esc(u.name)}">${u.pct.toFixed(1)}%</span>` : "–";
}
function renderAchs() {
  const q = S.achQ.trim().toLowerCase();
  let a = S.game.achievements.slice();
  if (S.achTab === "unlocked") a = a.filter(x => x.unlocked_at);
  if (S.achTab === "locked") a = a.filter(x => !x.unlocked_at);
  if (q) a = a.filter(x => (x.name + " " + x.description).toLowerCase().includes(q));
  const pc = x => x.pct == null ? 101 : x.pct;
  const sorts = {
    default: (x, y) => (!!y.unlocked_at - !!x.unlocked_at) || x.sort - y.sort,
    recent: (x, y) => (y.unlocked_at || 0) - (x.unlocked_at || 0) || x.sort - y.sort,
    rarest: (x, y) => pc(x) - pc(y), common: (x, y) => pc(y) - pc(x),
    name: (x, y) => x.name.localeCompare(y.name),
  };
  a.sort(sorts[S.achSort] || sorts.default);
  $("#achs").innerHTML = a.length ? a.map(x => {
    const on = !!x.unlocked_at, secret = x.hidden && !on;
    const icon = !on && x.icon_gray ? x.icon_gray : x.icon;
    const rare = x.pct != null && x.pct < 10;
    const manual = x.source === "Marked by you";
    return `<div class="ach ${on ? "on" : ""}" data-api="${esc(x.api)}">
      <div class="ai ${!on && x.icon_gray ? "gray" : ""}" ${icon ? `style="background-image:url('${icon}')"` : ""}>${icon ? "" : on ? ICON.trophy : ICON.lock}</div>
      <div class="tx"><div class="an">${esc(x.name)}${secret ? `<span class="tag" style="font-size:10.5px;padding:1px 7px">Hidden</span>` : ""}</div>
        <div class="ad ${secret && x.description ? "secret" : ""}" title="${secret ? "Hidden achievement: hover to reveal" : ""}">${esc(x.description) || (secret ? "Hidden achievement" : "")}</div>
        <div class="af">${on ? `<span class="when">${ICON.trophy.replace("<svg", '<svg style="width:12px;height:12px;vertical-align:-2px"')} ${dateTime(x.unlocked_at)}</span>` : `<span>Locked</span>`}
          ${x.pct != null ? `<span class="rar ${rare ? "rare" : ""}" title="${x.pct.toFixed(1)}% of Steam players have this"><i><b style="width:${Math.min(100, x.pct)}%"></b></i>${x.pct.toFixed(1)}%${rare ? " · rare" : ""}</span>` : ""}
          ${on && x.source ? `<span title="Read from">${esc(x.source)}</span>` : ""}</div></div>
      ${!on ? `<button class="mark" data-mark="${esc(x.api)}" title="I earned this: mark it as unlocked">${ICON.plus}Mark earned</button>`
        : manual ? `<button class="mark" data-unmark="${esc(x.api)}" title="Remove your mark">${ICON.x}Unmark</button>` : ""}</div>`;
  }).join("") : `<div class="muted">Nothing here.</div>`;
  $$("#achs [data-mark]").forEach(b => b.onclick = () => markDialog(S.game, S.game.achievements.find(a => a.api === b.dataset.mark)));
  $$("#achs [data-unmark]").forEach(b => b.onclick = async () => { await api("mark_achievement", S.game.id, b.dataset.unmark, false); openGame(S.game.id, true); });
}
function markDialog(g, a) {
  const now = new Date(), iso = new Date(now - now.getTimezoneOffset() * 6e4).toISOString().slice(0, 16);
  const d = dialog(`<h3>Mark "${esc(a.name)}" as earned</h3>
    <p>Use this when you know you unlocked it but this copy of the game didn't record it. It is shown as <b>Marked by you</b>.
    If the game's own files report it later, that record takes over.</p>
    <div style="display:flex;gap:10px;align-items:center"><span class="muted">Unlocked on</span><input type="datetime-local" id="mwhen" value="${iso}"></div>
    <div class="foot"><button class="btn" id="mc">Cancel</button><button class="btn primary" id="mok">${ICON.trophy}Mark earned</button></div>`);
  $("#mc", d).onclick = closeDialog;
  $("#mok", d).onclick = async () => { await api("mark_achievement", g.id, a.api, true, $("#mwhen", d).value); closeDialog(); openGame(g.id, true); };
}

// ---------------------------------------------------------------- charts (single series bars, hours)
function barChart(el, bars, opts = {}) {
  const W = el.clientWidth || 600, H = el.clientHeight || 210, padL = 38, padB = 22, padT = 8;
  const max = Math.max(...bars.map(b => b.v), 0);
  const stepH = niceStep(max / 3600);
  const top = Math.max(stepH * Math.ceil(max / 3600 / stepH), stepH) * 3600;
  const iw = W - padL, ih = H - padB - padT, bw = iw / bars.length, gap = Math.min(6, bw * .28);
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  for (let y = 0; y <= top + 1; y += stepH * 3600) {
    const py = padT + ih - y / top * ih;
    s += `<line class="gl" x1="${padL}" x2="${W}" y1="${py}" y2="${py}"/><text class="ax" x="${padL - 8}" y="${py + 4}" text-anchor="end">${fmtH(y / 3600)}</text>`;
  }
  const every = Math.ceil(bars.length / Math.floor(iw / 44));
  bars.forEach((b, i) => {
    const x = padL + i * bw, h = b.v ? Math.max(3, b.v / top * ih) : 0, y = padT + ih - h, w = Math.max(1, bw - gap), r = Math.min(4, w / 2, h);
    s += `<g class="col" data-i="${i}"><rect class="hit" x="${x}" y="${padT}" width="${bw}" height="${ih}"/>`;
    if (h) s += `<path class="b" fill="var(--bar)" d="M${x + gap / 2},${y + ih - ih + h - h + r} q0,-${r} ${r},-${r} h${w - 2 * r} q${r},0 ${r},${r} v${h - r} h-${w} z" />`;
    s += `</g>`;
    if (i % every === 0 || i === bars.length - 1) s += `<text class="ax" x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(b.l)}</text>`;
  });
  if (!max) s += `<text class="ax" x="${padL + iw / 2}" y="${padT + ih / 2}" text-anchor="middle" style="font-size:13px">No playtime recorded in this period</text>`;
  el.innerHTML = s + "</svg>";
  const tip = $("#tip");
  $$("g.col", el).forEach(g => {
    const b = bars[+g.dataset.i];
    g.onmousemove = e => { tip.hidden = false; tip.innerHTML = `<b>${esc(b.t)}</b>${b.v ? dur(b.v) : "Not played"}${b.extra || ""}`; tip.style.left = e.clientX + 14 + "px"; tip.style.top = e.clientY - 10 + "px"; g.querySelector("path")?.setAttribute("fill", "#86a9ff"); };
    g.onmouseleave = () => { tip.hidden = true; g.querySelector("path")?.setAttribute("fill", "var(--bar)"); };
    if (opts.onClick) g.onclick = () => opts.onClick(b);
  });
}
function niceStep(maxH) { for (const s of [0.25, 0.5, 1, 2, 3, 4, 6, 8, 12, 24]) if (maxH / s <= 4) return s; return 24; }
const fmtH = h => h === 0 ? "0" : h < 1 ? `${Math.round(h * 60)}m` : `${+h.toFixed(1)}h`;
function lastDays(n) {
  const out = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(); d.setHours(12, 0, 0, 0); d.setDate(d.getDate() - i);
    const k = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    out.push({ k, d });
  }
  return out;
}
function gameChart(g) {
  const days = lastDays(30);
  const bars = days.map(({ k, d }) => ({ v: g.daily[k] || 0, l: d.toLocaleDateString([], { day: "numeric", month: "short" }), t: d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" }) }));
  $("#g30").textContent = dur(bars.reduce((t, b) => t + b.v, 0)) + " total";
  barChart($("#gChart"), bars);
}

// ---------------------------------------------------------------- activity
async function loadActivity() {
  const a = await api("activity", S.actDays);
  const el = $("#v-activity");
  const days = lastDays(Math.min(S.actDays, 365));
  const dayTotal = k => Object.values(a.daily[k] || {}).reduce((t, v) => t + v, 0);
  const total = a.top.reduce((t, x) => t + x[1], 0);
  const activeDays = days.filter(d => dayTotal(d.k) > 0).length;
  el.innerHTML = `
    <header class="top"><h1 style="margin-right:auto">Activity</h1>
      <div class="seg" id="actSeg">${[7, 30, 90, 365].map(d => `<button data-d="${d}" class="${S.actDays === d ? "on" : ""}">${d === 365 ? "1 year" : d + " days"}</button>`).join("")}</div></header>
    <div class="stats">
      <div class="stat"><div class="ic">${ICON.clock}</div><div><div class="v">${dur(total, true)}</div><div class="k">Played in this period</div></div></div>
      <div class="stat"><div class="ic violet">${ICON.cal}</div><div><div class="v">${activeDays}</div><div class="k">Days with gaming · avg ${dur(activeDays ? total / activeDays : 0)}</div></div></div>
      <div class="stat"><div class="ic green">${ICON.games}</div><div><div class="v">${a.sessions}</div><div class="k">Sessions · ${a.top.length} games</div></div></div>
      <div class="stat"><div class="ic gold">${ICON.trophy}</div><div><div class="v">${a.unlocks.filter(u => u.time >= Date.now() / 1000 - S.actDays * 86400).length}</div><div class="k">Achievements unlocked</div></div></div>
    </div>
    <div class="card"><h3>Playtime per ${S.actDays > 90 ? "week" : "day"}</h3><div class="chart" id="actChart" style="height:240px"></div></div>
    <div class="two" style="margin-top:16px">
      <div class="card"><h3>Recent achievements <small>latest ${a.unlocks.length}</small></h3><div class="feed">${a.unlocks.map(u => `
        <div class="fi" data-g="${u.game_id}"><div class="ai" ${u.icon ? `style="background-image:url('${u.icon}')"` : ""}></div>
        <div style="flex:1;min-width:0"><div class="n">${esc(u.name)}</div><div class="s">${esc(u.game)} · ${ago(u.time)}${u.pct != null ? ` · ${u.pct.toFixed(1)}% of players` : ""}</div></div></div>`).join("") || `<div class="muted" style="padding:6px">No achievements yet.</div>`}</div></div>
      <div style="display:flex;flex-direction:column;gap:16px">
        <div class="card"><h3>Most played</h3><div class="hbars">${a.top.slice(0, 8).map(([gid, s]) => `
          <div class="hb" data-g="${gid}"><div class="cv" ${a.covers[gid] ? `style="background-image:url('${a.covers[gid]}')"` : ""}></div>
          <div style="min-width:0"><div class="n">${esc(a.names[gid] || "?")}</div><div class="track" style="width:${a.top[0][1] ? s / a.top[0][1] * 100 : 0}%"></div></div>
          <div class="v">${dur(s, true)}</div></div>`).join("") || `<div class="muted">Nothing played in this period.</div>`}</div></div>
        <div class="card"><h3>Longest sessions</h3>${a.longest.map(l => `
          <div class="srow" style="cursor:pointer" data-g="${l.game_id}"><div class="d">${esc(l.game)}<small>${dateTime(l.start)}</small></div><b>${dur(l.dur)}</b><span></span></div>`).join("") || `<div class="muted">None yet.</div>`}</div>
      </div>
    </div>`;
  $$("#actSeg button").forEach(b => b.onclick = () => { S.actDays = +b.dataset.d; savePrefs(); loadActivity(); });
  $$("[data-g]", el).forEach(x => x.onclick = () => openGame(x.dataset.g));
  let bars;
  const games = k => Object.entries(a.daily[k] || {}).sort((x, y) => y[1] - x[1]).slice(0, 3).map(([g, s]) => `<br><span class="muted">${esc(a.names[g])}: ${dur(s)}</span>`).join("");
  if (S.actDays > 90) {
    bars = [];
    for (let i = 0; i < days.length; i += 7) {
      const wk = days.slice(i, i + 7);
      const v = wk.reduce((t, d) => t + dayTotal(d.k), 0);
      const f = wk[0].d, l = wk[wk.length - 1].d;
      bars.push({ v, l: f.toLocaleDateString([], { month: "short", day: "numeric" }), t: `${f.toLocaleDateString([], { day: "numeric", month: "short" })} – ${l.toLocaleDateString([], { day: "numeric", month: "short" })}` });
    }
  } else {
    bars = days.map(({ k, d }) => ({ v: dayTotal(k), l: S.actDays <= 7 ? d.toLocaleDateString([], { weekday: "short" }) : d.toLocaleDateString([], { day: "numeric", month: "short" }),
      t: d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" }), extra: games(k) }));
  }
  barChart($("#actChart"), bars);
}

// ---------------------------------------------------------------- settings
const OV_PINS = [["fps", "FPS"], ["frametime", "Frame time"], ["gpu_usage", "GPU usage"], ["gpu_temp", "GPU temp"],
  ["gpu_hotspot", "GPU hotspot"], ["gpu_clock", "GPU clock"], ["gpu_power", "Board power"], ["gpu_fan", "GPU fan"],
  ["vram", "VRAM"], ["cpu", "CPU"], ["ram", "RAM"], ["rec", "Recording"], ["ach", "Achievements"]];

async function loadSettings() {
  const s = await api("settings"), c = s.config, el = $("#v-settings");
  const sw = (id, on) => `<label class="switch"><input type="checkbox" id="${id}" ${on ? "checked" : ""}><span></span></label>`;
  el.innerHTML = `
    <header class="top"><h1>Settings</h1></header>
    <div class="set">
      <div class="card"><h3>Game folders</h3>
        <div class="row"><div class="l"><b>Drives to scan</b><small>Every game installed on these drives is tracked automatically.</small></div>
          <div class="drv">${s.drives.map(d => `<label><input type="checkbox" data-drive="${d}" ${c.scan_drives.map(x => x.toUpperCase()).includes(d) ? "checked" : ""}>${d}:</label>`).join("")}</div></div>
        <div class="row" style="display:block"><div class="l"><b>Games added by hand</b><small>For a game the library didn't find, or one on another drive.</small></div>
          <div class="plist" id="manualList">${c.manual_games.map((p, i) => `<div><span>${esc(p)}</span><button data-i="${i}" title="Remove">${ICON.x}</button></div>`).join("") || `<span class="muted" style="font-size:13px">None</span>`}</div>
          <button class="btn small" id="addGame" style="margin-top:10px">${ICON.plus}Add a game folder</button></div>
        <div class="row" style="display:block"><div class="l"><b>Skip these folders</b><small>One per line. Not games, never scanned.</small></div>
          <textarea id="excl" rows="4" style="width:100%;margin-top:8px">${esc(c.exclude_folders.join("\n"))}</textarea></div>
      </div>
      <div class="card"><h3>Tracking &amp; notifications</h3>
        <div class="row"><div class="l"><b>Run in the background at Windows start</b><small>Needed to record playtime when this window is closed.</small></div>${sw("autostart", s.autostart)}</div>
        <div class="row"><div class="l"><b>Background tracker</b><small>${s.tracker ? "Running" : "Not running"}</small></div><button class="btn small" id="restartTracker">${ICON.refresh}${s.tracker ? "Restart" : "Start"}</button></div>
        <div class="row"><div class="l"><b>Achievement unlocked pop-ups</b><small>A Windows notification for every new unlock.</small></div>${sw("notif", c.notifications)}</div>
        <div class="row"><div class="l"><b>Session summary pop-up</b><small>When a game closes: time played this session and in total.</small></div>${sw("summary", c.session_summary !== false)}</div>
        <div class="row"><div class="l"><b>Ignore sessions shorter than</b><small>Quick launches and crashes don't count.</small></div>
          <span><input type="number" id="minSess" min="0" max="3600" style="width:80px" value="${c.min_session_seconds}"> sec</span></div>
      </div>
      <div class="card"><h3>Game launch pop-up</h3>
        <div class="row"><div class="l"><b>Show a pop-up when a game starts</b><small>Same look as the achievement pop-up.</small></div>${sw("welcome", c.welcome_popup !== false)}</div>
        <div class="row"><div class="l"><b>Message</b><small><code>{game}</code> = the game's name, <code>{user}</code> = your Windows user name.</small></div>
          <input type="text" id="wMsg" maxlength="80" style="width:280px" value="${esc(c.welcome_message)}"></div>
        <div class="row"><div class="l"><b>Small line above the message</b><small>Leave empty to hide it.</small></div>
          <input type="text" id="wHead" maxlength="40" style="width:280px" value="${esc(c.welcome_header)}"></div>
        <div class="row"><div class="l"><b>Show the game's name below the message</b></div>${sw("wGame", c.welcome_show_game !== false)}</div>
        <div class="row"><div class="l"><b>Picture in the circle</b></div>
          <select id="wImg"><option value="cover">Game cover</option><option value="logo">App logo</option></select></div>
        <div class="row"><div class="l"><b>Colour</b><small>The ring and the small line.</small></div>
          <input type="color" id="wColor" value="${esc(c.welcome_color)}" style="width:52px;height:32px;padding:2px;cursor:pointer"></div>
        <div class="row"><div class="l"><b>Position on screen</b></div>
          <select id="wPos">${[["top-left", "Top left"], ["top-center", "Top middle"], ["top-right", "Top right"], ["bottom-left", "Bottom left"], ["bottom-center", "Bottom middle"], ["bottom-right", "Bottom right"]].map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select></div>
        <div class="row"><div class="l"><b>Size</b><small>100% is the achievement pop-up's size.</small></div>
          <span style="display:flex;align-items:center;gap:10px"><input type="range" id="wSize" min="0.7" max="2" step="0.05" value="${c.welcome_size}" style="width:180px"><b id="wSizeV" style="width:46px;text-align:right"></b></span></div>
        <div class="row"><div class="l"><b>Time fully on screen</b><small>Plus about 2 s to slide in and out.</small></div>
          <span><input type="number" id="wSecs" min="2" max="60" step="1" style="width:70px" value="${c.welcome_seconds}"> sec</span></div>
        <div class="row"><div class="l"><b>Sound</b><small id="wSndFile"></small></div>
          <span style="display:flex;gap:8px"><select id="wSnd"><option value="none">No sound</option><option value="achievement">Achievement sound</option><option value="achievement-360">Xbox 360 achievement</option><option value="custom">My own .wav file</option></select><button class="btn small" id="wSndPick">Choose…</button></span></div>
        <div class="row"><div class="l"><b>Don't show it again for the same game within</b><small>Covers quick relaunches and waking from sleep. 0 = every launch.</small></div>
          <span><input type="number" id="wCool" min="0" max="1440" style="width:70px" value="${c.welcome_cooldown_minutes}"> min</span></div>
        <div class="row"><div class="l"><b>Try it</b><small>Uses the settings above, even before you save.</small></div>
          <span style="display:flex;gap:8px"><button class="btn small" id="wReset">Reset to default</button><button class="btn small primary" id="testWelcome">Preview</button></span></div>
      </div>
      ${s.overlay_available ? `<div class="card"><h3>Game overlay</h3>
        <div class="row"><div class="l"><b>In-game overlay</b><small>A panel over the game with FPS, AMD GPU stats, recording and achievements. Only works while a game is running.</small></div>${sw("ovOn", c.overlay_enabled !== false)}</div>
        <div class="row"><div class="l"><b>Open with the controller's Guide / PS button</b></div>${sw("ovGuide", c.overlay_guide_button !== false)}</div>
        <div class="row"><div class="l"><b>Keyboard shortcut</b><small>For example Alt+G or F10. (Win+G is kept by Windows itself.) Leave empty for none.</small></div>
          <input type="text" id="ovKey" maxlength="30" style="width:160px" value="${esc(c.overlay_hotkey)}"></div>
        <div class="row" style="display:block"><div class="l"><b>Show in the panel</b></div>
          <div class="drv" style="margin-top:8px">${[["ovFps", "overlay_show_fps", "FPS + frame-time graph"], ["ovGpu", "overlay_show_gpu", "AMD GPU stats"], ["ovSys", "overlay_show_system", "CPU and RAM"], ["ovRec", "overlay_show_recording", "Recording status"], ["ovAch", "overlay_show_achievements", "Achievements"], ["ovAmd", "overlay_show_amd_settings", "AMD settings panel (saved per game)"]].map(([id, k, t]) => `<label><input type="checkbox" id="${id}" ${c[k] !== false ? "checked" : ""}>${t}</label>`).join("")}</div></div>
        <div class="row"><div class="l"><b>Position</b></div>
          <select id="ovPos"><option value="right">Right side</option><option value="center">Middle</option><option value="left">Left side</option></select></div>
        <div class="row"><div class="l"><b>Size</b></div>
          <span style="display:flex;align-items:center;gap:10px"><input type="range" id="ovSize" min="0.7" max="1.6" step="0.05" value="${c.overlay_size}" style="width:180px"><b id="ovSizeV" style="width:46px;text-align:right"></b></span></div>
        <div class="row"><div class="l"><b>Opacity</b></div>
          <span style="display:flex;align-items:center;gap:10px"><input type="range" id="ovAlpha" min="0.5" max="1" step="0.02" value="${c.overlay_opacity}" style="width:180px"><b id="ovAlphaV" style="width:46px;text-align:right"></b></span></div>
        <div class="row"><div class="l"><b>Haze over the game</b><small>Darkens the game behind the panels while they're open. 0% = off.</small></div>
          <span style="display:flex;align-items:center;gap:10px"><input type="range" id="ovHaze" min="0" max="0.8" step="0.05" value="${c.overlay_haze}" style="width:180px"><b id="ovHazeV" style="width:46px;text-align:right"></b></span></div>
        <div class="row"><div class="l"><b>Colour</b><small>Outline, bars and graph.</small></div>
          <input type="color" id="ovColor" value="${esc(c.overlay_color)}" style="width:52px;height:32px;padding:2px;cursor:pointer"></div>
        <div class="row"><div class="l"><b>Close by itself after</b><small>0 = stays until you press the button again.</small></div>
          <span><input type="number" id="ovAuto" min="0" max="120" style="width:70px" value="${c.overlay_auto_hide}"> sec</span></div>
        <div class="row" style="display:block"><div class="l"><b>Pinned on screen while playing</b><small>Stays visible after the overlay closes. You can also pin by clicking a stat in the overlay (or RB, then A).</small></div>
          <div class="drv" style="margin-top:8px">${OV_PINS.map(([k, t]) => `<label><input type="checkbox" data-pin="${k}" ${(c.overlay_pins || []).includes(k) || (k === "fps" && !(c.overlay_pins || []).length && c.overlay_fps_counter) ? "checked" : ""}>${t}</label>`).join("")}</div></div>
        <div class="row"><div class="l"><b>Where the pinned stats sit</b></div>
          <select id="ovCntPos"><option value="top-left">Top left</option><option value="top-right">Top right</option><option value="bottom-left">Bottom left</option><option value="bottom-right">Bottom right</option></select></div>
        <div class="row"><div class="l"><b>Try it</b><small>Saves these settings and shows the panel for 12 seconds.</small></div>
          <span style="display:flex;gap:8px"><button class="btn small" id="ovReset">Reset to default</button><button class="btn small primary" id="testOverlay">Preview</button></span></div>
      </div>` : ""}
      <div class="card"><h3>Downloads</h3>
        <div class="row"><div class="l"><b>Steam Web API key</b><small>Optional. Without it, achievement lists come from SteamHunters and the Steam Community.
          With one you also get locked icons and hidden flags. Free at <a href="#" id="keyLink" style="color:var(--accent)">steamcommunity.com/dev/apikey</a>.</small></div>
          <input type="password" id="apikey" style="width:230px" value="${esc(c.steam_api_key)}" placeholder="optional"></div>
        <div class="row"><div class="l"><b>Steam store region</b><small>Used to find games by name.</small></div>
          <select id="cc">${["us", "in", "gb", "de", "fr", "jp"].map(x => `<option ${c.store_country === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
        <div class="row"><div class="l"><b>Download everything again</b><small>Achievement lists, rarity, covers and icons for every game.</small></div>
          <button class="btn small" id="forceAll">${ICON.refresh}Re-download</button></div>
      </div>
      <div class="card"><h3>Data</h3>
        <div class="row"><div class="l"><b>Data folder</b><small>${esc(s.data_dir)}</small></div><button class="btn small" id="openData">${ICON.folder}Open</button></div>
        <div class="row"><div class="l"><b>Log</b><small>${esc(s.log)}</small></div><button class="btn small" id="openLog">${ICON.folder}Show</button></div>
        <div class="row"><div class="l"><b>Achievement sources read</b><small>Steam (local cache + your public profile), GSE / Goldberg, CODEX, RUNE, EMPRESS, OnlineFix, Tenoke, and achievement files inside the game folder.</small></div></div>
      </div>
    </div>
    <div style="margin-top:18px;display:flex;gap:10px;justify-content:flex-end"><button class="btn primary" id="saveSet">Save settings</button></div>`;
  const wKnown = ["none", "achievement", "achievement-360"];
  let wSndPath = wKnown.includes(c.welcome_sound) ? "" : c.welcome_sound;
  $("#wImg").value = c.welcome_image; $("#wPos").value = c.welcome_position;
  $("#wSnd").value = wSndPath ? "custom" : c.welcome_sound;
  const wSync = () => {
    $("#wSizeV").textContent = Math.round($("#wSize").value * 100) + "%";
    const custom = $("#wSnd").value === "custom";
    $("#wSndPick").style.display = custom ? "" : "none";
    $("#wSndFile").textContent = custom ? (wSndPath ? wSndPath.split("\\").pop() : "Pick a .wav file") : "Plays as the pop-up appears.";
  };
  wSync();
  $("#wSize").oninput = wSync;
  $("#wSnd").onchange = async () => {
    if ($("#wSnd").value === "custom" && !wSndPath) { wSndPath = (await api("pick_sound")) || ""; if (!wSndPath) $("#wSnd").value = "none"; }
    wSync();
  };
  $("#wSndPick").onclick = async () => { const p = await api("pick_sound"); if (p) wSndPath = p; wSync(); };
  const welcomeOpts = () => ({
    welcome_popup: $("#welcome").checked, welcome_message: $("#wMsg").value.trim() || s.defaults.welcome_message,
    welcome_header: $("#wHead").value.trim(), welcome_show_game: $("#wGame").checked, welcome_image: $("#wImg").value,
    welcome_color: $("#wColor").value, welcome_position: $("#wPos").value, welcome_size: +$("#wSize").value,
    welcome_seconds: Math.min(60, Math.max(2, +$("#wSecs").value || s.defaults.welcome_seconds)),
    welcome_sound: $("#wSnd").value === "custom" ? (wSndPath || "none") : $("#wSnd").value,
    welcome_cooldown_minutes: Math.max(0, +$("#wCool").value || 0),
  });
  $("#wReset").onclick = async () => {
    const d = s.defaults;
    await api("save_settings", Object.fromEntries(Object.keys(d).filter(k => k.startsWith("welcome_")).map(k => [k, d[k]])));
    note("Launch pop-up reset to default"); loadSettings();
  };
  const overlayOpts = () => !s.overlay_available ? {} : ({
    overlay_enabled: $("#ovOn").checked, overlay_guide_button: $("#ovGuide").checked,
    overlay_hotkey: $("#ovKey").value.trim(),
    overlay_show_fps: $("#ovFps").checked, overlay_show_gpu: $("#ovGpu").checked, overlay_show_system: $("#ovSys").checked,
    overlay_show_recording: $("#ovRec").checked, overlay_show_achievements: $("#ovAch").checked,
    overlay_show_amd_settings: $("#ovAmd").checked,
    overlay_position: $("#ovPos").value, overlay_size: +$("#ovSize").value, overlay_opacity: +$("#ovAlpha").value,
    overlay_haze: +$("#ovHaze").value,
    overlay_color: $("#ovColor").value, overlay_auto_hide: Math.min(120, Math.max(0, +$("#ovAuto").value || 0)),
    overlay_pins: $$("[data-pin]").filter(x => x.checked).map(x => x.dataset.pin), overlay_fps_counter: false,
    overlay_fps_counter_position: $("#ovCntPos").value,
  });
  if (s.overlay_available) {
    $("#ovPos").value = c.overlay_position; $("#ovCntPos").value = c.overlay_fps_counter_position;
    const ovSync = () => {
      $("#ovSizeV").textContent = Math.round($("#ovSize").value * 100) + "%";
      $("#ovAlphaV").textContent = Math.round($("#ovAlpha").value * 100) + "%";
      $("#ovHazeV").textContent = Math.round($("#ovHaze").value * 100) + "%";
    };
    ovSync(); $("#ovSize").oninput = ovSync; $("#ovAlpha").oninput = ovSync; $("#ovHaze").oninput = ovSync;
    $("#testOverlay").onclick = async () => { await api("test_overlay", overlayOpts()); note("Overlay preview for 12 seconds"); };
    $("#ovReset").onclick = async () => {
      const d = s.defaults;
      await api("save_settings", Object.fromEntries(Object.keys(d).filter(k => k.startsWith("overlay_")).map(k => [k, d[k]])));
      note("Game overlay reset to default"); loadSettings();
    };
  }
  const collect = () => ({
    scan_drives: $$("[data-drive]").filter(x => x.checked).map(x => x.dataset.drive),
    exclude_folders: $("#excl").value.split("\n").map(x => x.trim()).filter(Boolean),
    notifications: $("#notif").checked, session_summary: $("#summary").checked,
    ...welcomeOpts(), ...overlayOpts(),
    min_session_seconds: +$("#minSess").value || 0, steam_api_key: $("#apikey").value.trim(), store_country: $("#cc").value,
  });
  $("#saveSet").onclick = async () => { await api("save_settings", collect()); note("Settings saved. Rescanning…"); await api("refresh_all", false); pollSoon(); };
  $("#autostart").onchange = async e => { e.target.checked = await api("set_autostart", e.target.checked); };
  $("#restartTracker").onclick = async () => { await api("restart_tracker"); note("Tracker restarted"); setTimeout(loadSettings, 2500); };
  $("#addGame").onclick = async () => { if (await api("add_game_folder")) { note("Game added. Scanning…"); pollSoon(); setTimeout(loadSettings, 800); } };
  $$("#manualList button").forEach(b => b.onclick = async () => {
    const cfg = collect(); cfg.manual_games = c.manual_games.filter((_, i) => i !== +b.dataset.i);
    await api("save_settings", cfg); loadSettings();
  });
  $("#testWelcome").onclick = () => api("test_welcome", welcomeOpts());
  $("#forceAll").onclick = async () => { if (await api("refresh_all", true)) pollSoon(); };
  $("#openData").onclick = () => api("open_path", s.data_dir);
  $("#openLog").onclick = () => api("open_path", s.log);
  $("#keyLink").onclick = e => { e.preventDefault(); api("open_url", "https://steamcommunity.com/dev/apikey"); };
}

// ---------------------------------------------------------------- dialogs
function dialog(html) { $("#dialog").innerHTML = html; $("#modal").hidden = false; return $("#dialog"); }
function closeDialog() { $("#modal").hidden = true; }
$("#modal").onclick = e => { if (e.target.id === "modal") closeDialog(); };
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { if (!$("#modal").hidden) closeDialog(); else if (S.view === "game") { show("library"); loadLibrary(); } }
});

function matchDialog(g) {
  const d = dialog(`<h3>Match with a Steam game</h3>
    <p>the library uses the Steam AppID to download the achievement list, rarity and artwork. Search by name or type an AppID.</p>
    <div style="display:flex;gap:8px"><input id="mq" style="flex:1" value="${esc(g.original_name)}"><button class="btn primary" id="mgo">Search</button></div>
    <div class="results" id="mres"></div>
    <div class="foot">${g.appid_locked ? `<button class="btn" id="mauto">Go back to automatic</button>` : ""}
      ${g.appid ? `<button class="btn danger" id="mnone">Not on Steam</button>` : ""}<button class="btn" id="mclose">Cancel</button></div>`);
  const go = async () => {
    $("#mres", d).innerHTML = `<div class="muted">Searching…</div>`;
    const r = await api("steam_search", $("#mq", d).value);
    $("#mres", d).innerHTML = r.length ? r.map(x => `<div class="res" data-a="${x.appid}"><img src="${esc(x.image || "")}" onerror="this.style.visibility='hidden'"><div><b>${esc(x.name)}</b><small>AppID ${x.appid}${x.appid === g.appid ? " · current" : ""}</small></div></div>`).join("")
      : `<div class="muted">No results. Try a shorter name, or type the AppID from the game's Steam store URL.</div>`;
    $$(".res", d).forEach(el => el.onclick = async () => { closeDialog(); await api("set_appid", g.id, +el.dataset.a); note("Downloading achievements…"); pollSoon(); });
  };
  $("#mgo", d).onclick = go;
  $("#mq", d).onkeydown = e => { if (e.key === "Enter") go(); };
  $("#mclose", d).onclick = closeDialog;
  if ($("#mauto", d)) $("#mauto", d).onclick = async () => { closeDialog(); await api("auto_appid", g.id); pollSoon(); };
  if ($("#mnone", d)) $("#mnone", d).onclick = async () => { closeDialog(); await api("set_appid", g.id, 0); pollSoon(); };
  go();
}
function renameDialog(g) {
  const d = dialog(`<h3>Rename</h3><p>Folder name: ${esc(g.original_name)}</p><input id="rn" style="width:100%" value="${esc(g.name)}">
    <div class="foot"><button class="btn" id="rreset">Use folder name</button><button class="btn" id="rc">Cancel</button><button class="btn primary" id="rok">Save</button></div>`);
  const save = async v => { await api("rename", g.id, v); closeDialog(); openGame(g.id, true); };
  $("#rn", d).focus(); $("#rn", d).select();
  $("#rn", d).onkeydown = e => { if (e.key === "Enter") save($("#rn", d).value); };
  $("#rok", d).onclick = () => save($("#rn", d).value);
  $("#rreset", d).onclick = () => save("");
  $("#rc", d).onclick = closeDialog;
}
function coverDialog(g) {
  const d = dialog(`<h3>Cover art</h3><p>Pick an image from your PC (portrait 2:3 looks best), or go back to the downloaded Steam cover.</p>
    <div class="foot"><button class="btn" id="cdef">Use Steam cover</button><button class="btn" id="cc2">Cancel</button><button class="btn primary" id="cpick">${ICON.img}Choose image…</button></div>`);
  $("#cpick", d).onclick = async () => { const r = await api("choose_cover", g.id); closeDialog(); if (r) openGame(g.id, true); };
  $("#cdef", d).onclick = async () => { await api("reset_cover", g.id); closeDialog(); openGame(g.id, true); };
  $("#cc2", d).onclick = closeDialog;
}
function sessionDialog(g) {
  const now = new Date(Date.now() - 2 * 3600e3), iso = new Date(now - now.getTimezoneOffset() * 6e4).toISOString().slice(0, 16);
  const d = dialog(`<h3>Add a session</h3><p>For time played while tracking was off.</p>
    <div style="display:flex;gap:10px;align-items:center"><input type="datetime-local" id="ss" value="${iso}"><input type="number" id="sm" value="60" min="1" style="width:90px"> minutes</div>
    <div class="foot"><button class="btn" id="sc">Cancel</button><button class="btn primary" id="sok">Add</button></div>`);
  $("#sc", d).onclick = closeDialog;
  $("#sok", d).onclick = async () => { await api("add_session", g.id, $("#ss", d).value, +$("#sm", d).value); closeDialog(); openGame(g.id, true); };
}

// ---------------------------------------------------------------- status polling
let noteTimer;
function note(msg, spin) {
  const t = $("#toast");
  t.innerHTML = (spin ? `<span class="sp"></span>` : "") + `<span>${esc(msg)}</span>`;
  t.hidden = false;
  clearTimeout(noteTimer);
  if (!spin) noteTimer = setTimeout(() => t.hidden = true, 3500);
}
let pollTimer;
function pollSoon() { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 300); }
async function poll() {
  clearTimeout(pollTimer);
  try {
    const st = await api("status");
    const prevPlaying = JSON.stringify((S.live.playing || []).map(p => p.id));
    S.live = st.live || {};
    renderNow();
    $("#trackerState").className = "tstate" + (st.tracker ? "" : " off");
    $("#trackerState").innerHTML = st.tracker ? `<i></i>Tracking ${S.lib ? S.lib.games.filter(g => g.installed && !g.hidden).length : ""} games`
      : `<i></i>Tracker stopped · <a id="startTr">start</a>`;
    if ($("#startTr")) $("#startTr").onclick = async () => { await api("start_tracker"); setTimeout(poll, 2000); };
    if (st.task.running) note(st.task.message, true);
    else if (S.wasRunning) {
      note(st.task.error ? "Refresh failed: " + st.task.error : "Up to date");
      reloadView();
    }
    S.wasRunning = st.task.running;
    if (prevPlaying !== JSON.stringify((S.live.playing || []).map(p => p.id))) reloadView();
  } catch (e) { console.error(e); }
  pollTimer = setTimeout(poll, S.wasRunning ? 700 : 2500);
}
function reloadView() {
  if (S.view === "library") loadLibrary();
  else if (S.view === "game" && S.game) openGame(S.game.id, true);
  else if (S.view === "activity") loadActivity();
}
function renderNow() {
  const p = (S.live.playing || [])[0], el = $("#now");
  if (!p) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `<div class="lbl"><i></i>Now playing</div><div class="nm">${esc(p.name)}</div><div class="tm">${clock(Date.now() / 1000 - p.since)}</div>`;
  el.onclick = () => openGame(p.id);
}
setInterval(() => {
  renderNow();
  if (S.view === "game" && S.game && S.game.playing) {
    const p = (S.live.playing || []).find(x => x.id === S.game.id);
    const base = S.game.baseline + S.game.tracked - (S.game.sessions[0]?.live ? S.game.sessions[0].end - S.game.sessions[0].start : 0);
    if (p && $("#gTotal")) $("#gTotal").textContent = dur(base + Date.now() / 1000 - p.since);
  }
}, 1000);
$("#btnRefresh").onclick = async () => { if (await api("refresh_all", false)) pollSoon(); else note("A refresh is already running"); };
window.addEventListener("resize", () => {
  clearTimeout(window._rz);
  window._rz = setTimeout(() => { if (S.view === "game" && S.game) gameChart(S.game); if (S.view === "activity") loadActivity(); }, 200);
});

window.addEventListener("pywebviewready", async () => {
  syncFilterUi();
  await loadLibrary();
  const [h, y] = location.hash.slice(1).split("@");
  if (h.startsWith("game=")) await openGame(h.slice(5));
  else if (["activity", "settings"].includes(h)) $(`nav button[data-view=${h}]`).click();
  else if (["notinst", "all", "played", "ach", "perfect", "none", "hidden"].includes(h)) { S.filter = h; syncFilterUi(); renderLibrary(); }
  if (y) setTimeout(() => $("#main").scrollTop = +y, 400);
  poll();
});
