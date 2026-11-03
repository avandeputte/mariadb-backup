/* MariaDB Backup Manager -- the whole UI. No framework, no build step. */
"use strict";

let S = null;            // last /api/state
let pollTimer = null;    // live log / list refresh
const $ = (sel, root) => (root || document).querySelector(sel);

/* ---------------------------------------------------------------- helpers */

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(method, path, body) {
  const opt = {method, headers: {"X-MBM": "1"}, credentials: "same-origin"};
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* not json */ }
  if (r.status === 401 && path !== "/api/login") { showLogin(false); throw new Error("sign in first"); }
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg; t.className = "show" + (bad ? " bad" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.className = "", bad ? 5000 : 2500);
}

function size(n) {
  if (n === null || n === undefined) return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; n = +n;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
function when(iso) {
  if (!iso) return "";
  const d = new Date(iso); if (isNaN(d)) return iso;
  const now = new Date(), sameDay = d.toDateString() === now.toDateString();
  const t = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  return sameDay ? "today " + t : d.toLocaleDateString([], {month: "short", day: "numeric", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric"}) + " " + t;
}
function dur(a, b) {
  if (!a || !b) return "";
  const s = Math.round((new Date(b) - new Date(a)) / 1000);
  return s >= 60 ? Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s" : s + "s";
}
const pill = st => h("span", {class: "pill " + st}, st === "ok" ? "succeeded" : st);
const byId = (list, id) => (list || []).find(x => x.id === id);
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function scheduleText(s) {
  s = s || {mode: "manual"};
  const t = s.time || "02:00";
  switch (s.mode) {
    case "hourly": return (s.every > 1 ? "every " + s.every + " hours" : "hourly") + " at :" + String(s.minute || 0).padStart(2, "0");
    case "daily": return "daily at " + t;
    case "weekly": return (s.days || []).map(d => DAYS[d % 7]).join(", ") + " at " + t;
    case "monthly": return "monthly on day " + (s.day || 1) + " at " + t;
    case "cron": return "cron " + s.cron;
    default: return "on demand only";
  }
}

/* ---------------------------------------------------------------- dialog */

function dialog(title, body, buttons) {
  const dlg = $("#dlg"), form = $("#dlgform");
  form.replaceChildren(h("h2", {}, title), body,
    h("div", {class: "err", id: "dlgerr"}),
    h("div", {class: "dlgbtns"}, buttons.map(b => h("button", {
      class: "btn " + (b.cls || ""), type: "button",
      onclick: async ev => {
        if (!b.run) { dlg.close(); return; }
        ev.target.disabled = true; $("#dlgerr").textContent = "";
        try { const keep = await b.run(); if (!keep) dlg.close(); }
        catch (e) { $("#dlgerr").textContent = e.message; }
        finally { ev.target.disabled = false; }
      }}, b.label))));
  dlg.showModal();
  return dlg;
}
const closeDialog = () => $("#dlg").open && $("#dlg").close();
function confirmDlg(title, text, label, fn) {
  dialog(title, h("p", {}, text), [{label: "Cancel"}, {label, cls: "danger solid", run: fn}]);
}

/* ---------------------------------------------------------------- login */

async function showLogin(setup) {
  $("#login").hidden = false;
  $("#logintitle").textContent = setup ? "Create the administrator" : "Sign in";
  $("#loginintro").textContent = setup ? "This is the only login. Pick a password of at least 8 characters." : "MariaDB Backup Manager";
  $("#lp2wrap").hidden = !setup;
  $("#loginbtn").textContent = setup ? "Create and sign in" : "Sign in";
  $("#loginbox").onsubmit = async ev => {
    ev.preventDefault(); $("#loginerr").textContent = "";
    const username = $("#lu").value.trim(), password = $("#lp").value;
    if (setup && password !== $("#lp2").value) { $("#loginerr").textContent = "The passwords differ"; return; }
    try {
      await api("POST", setup ? "/api/setup" : "/api/login", {username, password});
      $("#login").hidden = true; $("#lp").value = ""; boot();
    } catch (e) { $("#loginerr").textContent = e.message; }
  };
}

/* ---------------------------------------------------------------- shell */

const PAGES = {overview: "Overview", jobs: "Backup jobs", backups: "Backups & restore", activity: "Activity",
  servers: "Servers", destinations: "Destinations", settings: "Settings", restore: "Restore", run: "Run"};

async function refresh() { S = await api("GET", "/api/state"); $("#busy").hidden = !S.busy; return S; }

async function route() {
  clearTimeout(pollTimer); pollTimer = null;
  document.body.classList.remove("navopen");
  const [page, ...args] = (location.hash.slice(1) || "overview").split("/").map(decodeURIComponent);
  const fn = RENDER[page] || RENDER.overview;
  document.querySelectorAll("#navlinks a").forEach(a => a.classList.toggle("on",
    a.dataset.page === page || (page === "restore" && a.dataset.page === "backups") || (page === "run" && a.dataset.page === "activity")));
  $("#pagetitle").textContent = PAGES[page] || "Overview";
  const c = $("#content");
  try { await refresh(); c.replaceChildren(await fn(...args)); }
  catch (e) { if (e.message !== "sign in first") c.replaceChildren(h("div", {class: "card err"}, e.message)); }
}

async function boot() {
  const who = await api("GET", "/api/whoami");
  if (!who.user) { showLogin(who.setup); return; }
  $("#whofoot").replaceChildren(who.user, " · ", h("a", {href: "#", onclick: async e => {
    e.preventDefault(); await api("POST", "/api/logout"); location.reload(); }}, "sign out"));
  $("#verfoot").textContent = "version " + who.version;
  route();
}

window.addEventListener("hashchange", route);
$("#navtoggle").onclick = () => document.body.classList.toggle("navopen");
$("#scrim").onclick = () => document.body.classList.remove("navopen");

/* ---------------------------------------------------------------- overview */

const RENDER = {};

RENDER.overview = async () => {
  const {runs} = await api("GET", "/api/runs?limit=8");
  const steps = [];
  if (!S.servers.length) steps.push(["Add the MariaDB server to back up", "#servers"]);
  if (!S.destinations.length) steps.push(["Add a destination: a local folder or Google Drive", "#destinations"]);
  if (!S.jobs.length) steps.push(["Create a backup job: what to back up, where, how often, how many to keep", "#jobs"]);
  const ok = runs.filter(r => r.status === "ok").length, failed = runs.filter(r => r.status === "failed").length;
  const next = S.jobs.filter(j => j.next_run).sort((a, b) => a.next_run.localeCompare(b.next_run))[0];
  return h("div", {},
    steps.length ? h("div", {class: "card"}, h("h2", {}, "Getting started"),
      h("ol", {style: "margin-left:18px"}, steps.map(([t, l]) => h("li", {}, h("a", {href: l}, t))))) : null,
    h("div", {class: "grid", style: "margin-bottom:18px"},
      h("div", {class: "card"}, h("div", {class: "hint"}, "Backup jobs"), h("div", {class: "stat"}, S.jobs.length),
        h("div", {class: "hint"}, S.jobs.filter(j => j.cron && j.enabled).length + " scheduled")),
      h("div", {class: "card"}, h("div", {class: "hint"}, "Next scheduled backup"),
        h("div", {class: "stat", style: "font-size:18px;padding:5px 0"}, next ? when(next.next_run) : "none"),
        h("div", {class: "hint"}, next ? next.name : "")),
      h("div", {class: "card"}, h("div", {class: "hint"}, "Last 8 runs"),
        h("div", {class: "stat"}, ok, h("span", {class: "hint"}, " succeeded")),
        h("div", {class: "hint", style: failed ? "color:var(--down)" : ""}, failed + " failed"))),
    S.jobs.length ? h("div", {class: "card"}, h("h2", {}, "Jobs"), jobsTable(true)) : null,
    h("div", {class: "card"}, h("div", {class: "spread"}, h("h2", {}, "Recent activity"), h("a", {href: "#activity"}, "all activity")),
      runsTable(runs)));
};

/* ---------------------------------------------------------------- jobs */

function jobsTable(compact) {
  if (!S.jobs.length) return h("div", {class: "empty"}, "No backup jobs yet.");
  return h("div", {class: "tablewrap"}, h("table", {},
    h("tr", {}, h("th", {}, "Job"), compact ? null : h("th", {}, "What"), h("th", {}, "Where"),
      h("th", {}, "When"), h("th", {}, "Last run"), h("th", {}, "")),
    S.jobs.map(j => {
      const srv = byId(S.servers, j.server_id), dst = byId(S.destinations, j.destination_id);
      const what = (j.databases === "all" ? "all databases" : j.databases.join(", ") || "no databases") + (j.include_users ? " + users" : "");
      const lr = j.last_run;
      return h("tr", {},
        h("td", {}, h("b", {}, j.name), h("div", {class: "hint"}, srv ? srv.name : "?")),
        compact ? null : h("td", {}, what),
        h("td", {}, dst ? dst.name : "?", h("div", {class: "hint"}, j.keep ? "keeps newest " + j.keep : "keeps all")),
        h("td", {}, j.enabled ? scheduleText(j.schedule) : h("span", {class: "muted"}, "paused"),
          j.next_run ? h("div", {class: "hint"}, "next " + when(j.next_run)) : null),
        h("td", {}, j.pending ? pill("running") : lr ? h("a", {href: "#run/" + lr.id}, pill(lr.status)) : h("span", {class: "muted"}, "never"),
          lr ? h("div", {class: "hint"}, when(lr.finished), lr.result && lr.result.size ? " · " + size(lr.result.size) : "") : null),
        h("td", {class: "act"},
          h("button", {class: "btn sm pri", disabled: j.pending, onclick: () => runJob(j)}, "Run now"),
          compact ? null : h("button", {class: "btn sm", onclick: () => editJob(j)}, "Edit"),
          compact ? null : h("button", {class: "btn sm danger", onclick: () => confirmDlg("Delete job " + j.name + "?",
            "The job and its schedule are removed. Backups it already made stay where they are.", "Delete",
            async () => { await api("DELETE", "/api/jobs/" + j.id); route(); })}, "Delete")));
    })));
}

async function runJob(j) {
  try { const r = await api("POST", "/api/jobs/" + j.id + "/run"); location.hash = "#run/" + r.run_id; }
  catch (e) { toast(e.message, true); }
}

RENDER.jobs = async () => h("div", {},
  h("div", {class: "spread"}, h("p", {class: "hint"}, "A job says which server and databases to back up, where to put the backup, when to run, and how many earlier backups to keep."),
    h("button", {class: "btn pri", onclick: () => editJob(null), disabled: !S.servers.length || !S.destinations.length}, "New job")),
  (!S.servers.length || !S.destinations.length) ? h("div", {class: "note"}, "Add a ", h("a", {href: "#servers"}, "server"), " and a ",
    h("a", {href: "#destinations"}, "destination"), " first.") : null,
  h("div", {class: "card"}, jobsTable(false)));

function editJob(j) {
  const isNew = !j;
  j = j || {name: "", server_id: (S.servers[0] || {}).id, destination_id: (S.destinations[0] || {}).id,
    databases: "all", include_users: true, keep: 7, enabled: true, desync: false, lock_all: false, compress_level: 6,
    schedule: {mode: "daily", time: "02:00"}};
  const sch = Object.assign({mode: "manual", time: "02:00", minute: 0, every: 1, days: [1], day: 1, cron: "0 2 * * *"}, j.schedule);
  const f = {};
  f.name = h("input", {value: j.name, placeholder: "nightly"});
  f.server = h("select", {onchange: () => loadDbs()}, S.servers.map(s => h("option", {value: s.id, selected: s.id === j.server_id}, s.name)));
  f.dest = h("select", {}, S.destinations.map(d => h("option", {value: d.id, selected: d.id === j.destination_id}, d.name)));
  f.all = h("input", {type: "radio", name: "dbm", checked: j.databases === "all", onchange: () => dbBox.hidden = true});
  f.some = h("input", {type: "radio", name: "dbm", checked: j.databases !== "all", onchange: () => { dbBox.hidden = false; loadDbs(); }});
  const dbBox = h("div", {class: "chips", hidden: j.databases === "all", style: "margin:4px 0 0 24px"});
  const galeraNote = h("div", {class: "hint"});
  f.users = h("input", {type: "checkbox", checked: j.include_users});
  f.keep = h("input", {type: "number", min: 0, value: j.keep, style: "width:110px"});
  f.enabled = h("input", {type: "checkbox", checked: j.enabled});
  f.desync = h("input", {type: "checkbox", checked: j.desync});
  f.lock = h("input", {type: "checkbox", checked: j.lock_all});
  f.level = h("select", {}, [1, 3, 6, 9].map(n => h("option", {value: n, selected: +j.compress_level === n}, n === 1 ? "1 (fastest)" : n === 9 ? "9 (smallest)" : String(n))));
  f.mode = h("select", {style: "width:auto", onchange: () => showSched()}, [["manual", "On demand only"], ["hourly", "Every hour(s)"], ["daily", "Daily"],
    ["weekly", "Weekly"], ["monthly", "Monthly"], ["cron", "Cron expression"]].map(([v, t]) => h("option", {value: v, selected: v === sch.mode}, t)));
  f.time = h("input", {type: "time", value: sch.time, style: "width:130px"});
  f.minute = h("input", {type: "number", min: 0, max: 59, value: sch.minute, style: "width:90px"});
  f.every = h("input", {type: "number", min: 1, max: 23, value: sch.every || 1, style: "width:90px"});
  f.day = h("input", {type: "number", min: 1, max: 31, value: sch.day, style: "width:90px"});
  f.cron = h("input", {value: sch.cron, class: "mono", placeholder: "min hour day month weekday"});
  f.days = DAYS.map((d, i) => h("input", {type: "checkbox", value: i, checked: (sch.days || []).includes(i)}));
  const schedBox = h("div", {});
  function showSched() {
    const m = f.mode.value, parts = [];
    if (m === "hourly") parts.push(h("div", {class: "row"}, "every", f.every, "hour(s), at minute", f.minute));
    if (["daily", "weekly", "monthly"].includes(m)) parts.push(h("div", {class: "row"}, "at", f.time, m === "monthly" ? ["on day", f.day] : null));
    if (m === "weekly") parts.push(h("div", {class: "chips", style: "margin-top:6px"}, f.days.map((c, i) => h("label", {class: "inline"}, c, DAYS[i]))));
    if (m === "cron") parts.push(f.cron, h("div", {class: "hint"}, "Five fields: minute hour day-of-month month day-of-week. Server time: " + when(S.now)));
    schedBox.replaceChildren(...parts);
  }
  showSched();
  const picked = new Set(j.databases === "all" ? [] : j.databases);
  async function loadDbs() {
    if (f.some.checked) dbBox.replaceChildren(h("span", {class: "hint"}, "Reading databases from the server..."));
    try {
      const info = await api("GET", "/api/servers/" + f.server.value + "/info");
      galeraNote.textContent = info.galera ? "This server is a Galera node (" + info.galera.state + ", cluster of " + info.galera.cluster_size + "). Desync keeps a long dump from slowing the cluster." : "";
      if (!f.some.checked) return;
      dbBox.replaceChildren(...info.databases.filter(d => !d.system).map(d => h("label", {class: "inline"},
        h("input", {type: "checkbox", value: d.name, checked: picked.has(d.name), onchange: e => e.target.checked ? picked.add(d.name) : picked.delete(d.name)}),
        d.name, h("span", {class: "hint"}, size(d.size)))));
      if (!dbBox.children.length) dbBox.replaceChildren(h("span", {class: "hint"}, "No user databases on this server."));
    } catch (e) { if (f.some.checked) dbBox.replaceChildren(h("span", {class: "err"}, e.message)); }
  }
  loadDbs();
  dialog(isNew ? "New backup job" : "Edit " + j.name, h("div", {},
    h("label", {}, "Name"), f.name,
    h("div", {class: "frm2"}, h("div", {}, h("label", {}, "Server"), f.server), h("div", {}, h("label", {}, "Destination"), f.dest)),
    h("label", {}, "What to back up"),
    h("label", {class: "inline"}, f.all, "All databases (new ones are picked up automatically)"),
    h("label", {class: "inline"}, f.some, "Only these databases"), dbBox,
    h("label", {class: "inline"}, f.users, "Users, roles and grants"),
    h("label", {}, "When"), h("div", {class: "row"}, f.mode, h("label", {class: "inline"}, f.enabled, "enabled")), schedBox,
    h("label", {}, "How many backups to keep"), h("div", {class: "row"}, f.keep,
      h("span", {class: "hint"}, "The newest this many backups of this job are kept in the destination; older ones are deleted after each successful backup. 0 keeps everything.")),
    h("details", {style: "margin-top:12px"}, h("summary", {class: "hint"}, "Galera and advanced options"),
      galeraNote,
      h("label", {class: "inline"}, f.desync, "Galera: desync this node during the backup (wsrep_desync)"),
      h("label", {class: "inline"}, f.lock, "Lock all tables instead of a transaction snapshot (only for MyISAM/Aria data; blocks writes)"),
      h("label", {}, "Compression"), f.level)),
    [{label: "Cancel"}, {label: isNew ? "Create job" : "Save", cls: "pri", run: async () => {
      const schedule = {mode: f.mode.value, time: f.time.value || "02:00", minute: +f.minute.value || 0, every: +f.every.value || 1,
        day: +f.day.value || 1, cron: f.cron.value, days: f.days.filter(c => c.checked).map(c => +c.value)};
      if (schedule.mode === "weekly" && !schedule.days.length) throw new Error("Pick at least one weekday");
      await api("POST", "/api/jobs", {id: isNew ? undefined : j.id, name: f.name.value, server_id: f.server.value,
        destination_id: f.dest.value, databases: f.all.checked ? "all" : [...picked], include_users: f.users.checked,
        keep: +f.keep.value || 0, enabled: f.enabled.checked, desync: f.desync.checked, lock_all: f.lock.checked,
        compress_level: +f.level.value, schedule});
      toast("Saved"); route();
    }}]);
}

/* ---------------------------------------------------------------- servers */

RENDER.servers = async () => h("div", {},
  h("div", {class: "spread"}, h("p", {class: "hint"}, "The MariaDB servers to back up and restore onto. For a Galera cluster, add one node (or each node, to restore onto a specific one)."),
    h("button", {class: "btn pri", onclick: () => editServer(null)}, "Add server")),
  h("div", {class: "card"}, !S.servers.length ? h("div", {class: "empty"}, "No servers yet.") :
    h("table", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Connects to"), h("th", {}, "User"), h("th", {})),
      S.servers.map(s => h("tr", {},
        h("td", {}, h("b", {}, s.name)),
        h("td", {class: "mono"}, s.host ? s.host + (s.port ? ":" + s.port : "") : (s.socket || "local socket")),
        h("td", {}, s.user || h("span", {class: "muted"}, "default")),
        h("td", {class: "act"}, h("button", {class: "btn sm", onclick: () => testServer(s)}, "Test"),
          h("button", {class: "btn sm", onclick: () => editServer(s)}, "Edit"),
          h("button", {class: "btn sm danger", onclick: () => confirmDlg("Remove " + s.name + "?", "Only the connection settings are removed.", "Remove",
            async () => { await api("DELETE", "/api/servers/" + s.id); route(); })}, "Remove")))))));

function serverInfoView(info) {
  const dbs = info.databases.filter(d => !d.system);
  return h("div", {},
    h("p", {class: "ok-msg"}, "Connected: MariaDB " + info.version),
    info.galera ? h("p", {}, "Galera node: ", h("b", {}, info.galera.state), ", cluster of " + info.galera.cluster_size + " (" + info.galera.cluster_status + ")") : h("p", {class: "hint"}, "Not a Galera node."),
    h("p", {class: "hint"}, dbs.length + " database(s): " + dbs.map(d => d.name + " (" + size(d.size) + ")").join(", ")),
    dbs.some(d => d.non_innodb) ? h("div", {class: "note"}, "Some tables are not InnoDB (" + dbs.filter(d => d.non_innodb).map(d => d.name).join(", ") +
      "). They are not covered by the transaction snapshot" + (info.galera ? ", and Galera does not replicate them." : ".")) : null);
}

async function testServer(s) {
  const out = h("div", {class: "hint"}, "Connecting...");
  dialog("Test " + s.name, out, [{label: "Close"}]);
  try { out.replaceChildren(serverInfoView(await api("GET", "/api/servers/" + s.id + "/info"))); }
  catch (e) { out.replaceChildren(h("div", {class: "err"}, e.message)); }
}

function editServer(s) {
  const isNew = !s; s = s || {};
  const f = {name: h("input", {value: s.name || ""}), host: h("input", {value: s.host || "", placeholder: "db1.lan (empty = local socket)"}),
    port: h("input", {value: s.port || "", placeholder: "3306"}), socket: h("input", {value: s.socket || "", placeholder: "/run/mysqld/mysqld.sock"}),
    user: h("input", {value: s.user || "", placeholder: "backup", autocomplete: "off"}),
    password: h("input", {type: "password", autocomplete: "new-password", placeholder: s.has_password ? "unchanged" : ""}),
    ssl: h("input", {type: "checkbox", checked: !!s.ssl})};
  const result = h("div", {});
  const body = () => ({id: s.id, name: f.name.value, host: f.host.value, port: f.port.value, socket: f.socket.value, user: f.user.value,
    password: f.password.value === "" && s.has_password ? null : f.password.value, ssl: f.ssl.checked});
  dialog(isNew ? "Add server" : "Edit " + s.name, h("div", {},
    h("label", {}, "Name"), f.name,
    h("div", {class: "frm2"}, h("div", {}, h("label", {}, "Host"), f.host), h("div", {}, h("label", {}, "Port"), f.port)),
    h("label", {}, "Socket (instead of host)"), f.socket,
    h("div", {class: "frm2"}, h("div", {}, h("label", {}, "User"), f.user), h("div", {}, h("label", {}, "Password"), f.password)),
    h("label", {class: "inline"}, f.ssl, "Require TLS"),
    h("details", {style: "margin-top:8px"}, h("summary", {class: "hint"}, "Privileges the user needs"),
      h("pre", {class: "mono", style: "white-space:pre-wrap;margin-top:6px"},
        "CREATE USER 'backup'@'%' IDENTIFIED BY '...';\n" +
        "GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES,\n  SHOW DATABASES ON *.* TO 'backup'@'%';\n" +
        "-- Galera desync (SET GLOBAL wsrep_desync) also needs SUPER.\n" +
        "-- Restores create databases, users and grants:\n-- use an administrative account (e.g. root) for the server you restore onto.")),
    result),
    [{label: "Cancel"}, {label: "Test", run: async () => {
      result.replaceChildren(h("div", {class: "hint"}, "Connecting..."));
      try { result.replaceChildren(serverInfoView(await api("POST", "/api/servers/test", body()))); }
      catch (e) { result.replaceChildren(h("div", {class: "err"}, e.message)); }
      return true;
    }}, {label: "Save", cls: "pri", run: async () => { await api("POST", "/api/servers", body()); toast("Saved"); route(); }}]);
}

/* ---------------------------------------------------------------- destinations */

RENDER.destinations = async () => h("div", {},
  h("div", {class: "spread"}, h("p", {class: "hint"}, "Where backups are stored. Each backup is a single .mdbk.tar file."),
    h("button", {class: "btn pri", onclick: () => editDest(null)}, "Add destination")),
  h("div", {class: "card"}, !S.destinations.length ? h("div", {class: "empty"}, "No destinations yet.") :
    h("table", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Type"), h("th", {}, "Location"), h("th", {})),
      S.destinations.map(d => h("tr", {},
        h("td", {}, h("b", {}, d.name)),
        h("td", {}, d.type === "local" ? "Local folder" : "Google Drive"),
        h("td", {}, d.type === "local" ? h("span", {class: "mono"}, d.path) : [
          d.folder_id ? h("span", {class: "mono"}, d.folder_id) : "folder “" + d.folder_name + "”",
          h("div", {class: "hint"}, d.signed_in ? (d.auth === "service_account" ? "service account" : "signed in" + (d.account ? " as " + d.account : "")) :
            h("span", {style: "color:var(--warn)"}, "not signed in"))]),
        h("td", {class: "act"},
          d.type === "gdrive" && d.auth !== "service_account" ? h("button", {class: "btn sm" + (d.signed_in ? "" : " pri"), onclick: () => googleSignIn(d)}, d.signed_in ? "Sign in again" : "Sign in with Google") : null,
          h("button", {class: "btn sm", onclick: () => testDest(d)}, "Test"),
          h("button", {class: "btn sm", onclick: () => editDest(d)}, "Edit"),
          h("button", {class: "btn sm danger", onclick: () => confirmDlg("Remove " + d.name + "?", "Only the settings are removed; the backup files are left where they are.", "Remove",
            async () => { await api("DELETE", "/api/destinations/" + d.id); route(); })}, "Remove")))))));

async function testDest(d) {
  try { toast((await api("POST", "/api/destinations/" + d.id + "/test")).message); }
  catch (e) { toast(e.message, true); }
}

function editDest(d) {
  const isNew = !d; d = d || {type: "local", path: S.default_local_dir, auth: "oauth", folder_name: "MariaDB Backups"};
  const f = {name: h("input", {value: d.name || ""}),
    type: h("select", {onchange: () => show()}, h("option", {value: "local", selected: d.type === "local"}, "Local folder"),
      h("option", {value: "gdrive", selected: d.type === "gdrive"}, "Google Drive")),
    path: h("input", {value: d.path || "", class: "mono"}),
    auth: h("select", {onchange: () => show()}, h("option", {value: "oauth", selected: d.auth !== "service_account"}, "My Google account (device sign-in)"),
      h("option", {value: "service_account", selected: d.auth === "service_account"}, "Service account (Shared Drive)")),
    client_id: h("input", {value: d.client_id || "", class: "mono"}),
    client_secret: h("input", {type: "password", placeholder: d.has_client_secret ? "unchanged" : "", autocomplete: "off"}),
    folder_name: h("input", {value: d.folder_name || "MariaDB Backups"}),
    folder_id: h("input", {value: d.folder_id || "", class: "mono", placeholder: "from the folder's URL"}),
    sa_json: h("textarea", {placeholder: d.has_sa_json ? "(key stored; paste a new one to replace it)" : "{ \"type\": \"service_account\", ... }"})};
  const box = h("div", {});
  function show() {
    if (f.type.value === "local") {
      box.replaceChildren(h("label", {}, "Folder on this machine"), f.path,
        h("p", {class: "hint"}, "Created if missing. In Docker, mount a volume here (the image uses /backups)."));
      return;
    }
    const oauth = f.auth.value !== "service_account";
    box.replaceChildren(...[h("label", {}, "Sign in with"), f.auth,
      oauth ? [
        h("div", {class: "note"}, "One-time setup in your own Google Cloud project: enable the Google Drive API, create an OAuth client ID of type ",
          h("b", {}, "TVs and Limited Input devices"), ", and paste its ID and secret here. After saving, press ", h("b", {}, "Sign in with Google"),
          ". The app only gets access to the files it creates (drive.file)."),
        h("label", {}, "OAuth client ID"), f.client_id, h("label", {}, "OAuth client secret"), f.client_secret,
        h("label", {}, "Folder name in your Drive"), f.folder_name,
        h("p", {class: "hint"}, "The folder is created on first use. Google only lets this app see folders it made itself, so use a name rather than an existing folder.")] : [
        h("div", {class: "note"}, "Create a service account and a JSON key, add the service account as a Content manager of a Shared Drive, and give the ID of a folder in it. Service accounts have no storage of their own, so a My Drive folder does not work."),
        h("label", {}, "Service account JSON key"), f.sa_json, h("label", {}, "Folder ID"), f.folder_id]].flat());
  }
  show();
  dialog(isNew ? "Add destination" : "Edit " + d.name, h("div", {}, h("label", {}, "Name"), f.name, h("label", {}, "Type"), f.type, box),
    [{label: "Cancel"}, {label: "Save", cls: "pri", run: async () => {
      const r = await api("POST", "/api/destinations", {id: d.id, name: f.name.value, type: f.type.value, path: f.path.value, auth: f.auth.value,
        client_id: f.client_id.value, client_secret: f.client_secret.value || (d.has_client_secret ? null : ""),
        folder_name: f.folder_name.value, folder_id: f.folder_id.value, sa_json: f.sa_json.value || (d.has_sa_json ? null : "")});
      toast("Saved"); await route();
      const saved = byId(S.destinations, r.id);
      if (saved && saved.type === "gdrive" && saved.auth !== "service_account" && !saved.signed_in) googleSignIn(saved);
    }}]);
}

async function googleSignIn(d) {
  const out = h("div", {class: "hint"}, "Asking Google for a code...");
  dialog("Sign in with Google", out, [{label: "Close"}]);
  let flow;
  try { flow = await api("POST", "/api/destinations/" + d.id + "/google/start"); }
  catch (e) { out.replaceChildren(h("div", {class: "err"}, e.message)); return; }
  const status = h("p", {class: "hint"}, "Waiting for you to approve...");
  out.replaceChildren(h("p", {}, "Open ", h("a", {href: flow.url, target: "_blank", rel: "noopener"}, flow.url), " on any device and enter:"),
    h("div", {class: "code"}, flow.user_code), status);
  const tick = async () => {
    if (!$("#dlg").open) return;
    try {
      const st = await api("GET", "/api/google/flows/" + flow.id);
      if (st.status === "ok") { status.className = "ok-msg"; status.textContent = "Signed in. Backups can now go to Google Drive."; route(); return; }
      if (st.status !== "pending") { status.className = "err"; status.textContent = st.error || ("Sign-in " + st.status + "."); return; }
    } catch (e) { status.textContent = e.message; }
    setTimeout(tick, 2500);
  };
  setTimeout(tick, 2500);
}

/* ---------------------------------------------------------------- backups */

RENDER.backups = async (did) => {
  if (!S.destinations.length) return h("div", {class: "card empty"}, "Add a ", h("a", {href: "#destinations"}, "destination"), " first.");
  did = did || sessionStorage.getItem("mbm_dest") || S.destinations[0].id;
  if (!byId(S.destinations, did)) did = S.destinations[0].id;
  sessionStorage.setItem("mbm_dest", did);
  const d = byId(S.destinations, did);
  const pick = h("select", {style: "width:auto", onchange: e => location.hash = "#backups/" + e.target.value},
    S.destinations.map(x => h("option", {value: x.id, selected: x.id === did}, x.name)));
  const list = h("div", {class: "card"}, h("div", {class: "hint"}, d.type === "gdrive" ? "Listing the Google Drive folder..." : "Listing..."));
  (async () => {
    try {
      const {backups, free} = await api("GET", "/api/backups/" + did);
      const total = backups.reduce((a, b) => a + b.size, 0);
      list.replaceChildren(
        h("div", {class: "hint", style: "margin-bottom:8px"}, backups.length + " backup(s), " + size(total) + (free !== null ? " · " + size(free) + " free" : "")),
        !backups.length ? h("div", {class: "empty"}, "No backups here yet.") :
          h("div", {class: "tablewrap"}, h("table", {}, h("tr", {}, h("th", {}, "Backup"), h("th", {}, "Taken"), h("th", {}, "Size"), h("th", {})),
            backups.map(b => h("tr", {},
              h("td", {}, h("b", {}, b.label || "?"), h("div", {class: "mono hint"}, b.name)),
              h("td", {}, when(b.created)), h("td", {}, size(b.size)),
              h("td", {class: "act"},
                h("a", {class: "btn sm pri", href: "#restore/" + did + "/" + encodeURIComponent(b.name)}, "Restore..."),
                h("a", {class: "btn sm", href: "/api/backups/" + did + "/" + encodeURIComponent(b.name) + "/download"}, "Download"),
                h("button", {class: "btn sm", onclick: async ev => {
                  ev.target.disabled = true; ev.target.textContent = "Checking...";
                  try { const r = await api("POST", "/api/backups/" + did + "/" + encodeURIComponent(b.name) + "/verify");
                    toast(r.problems.length ? r.problems.length + " problem(s): " + r.problems[0] : r.files + " files, all checksums match", r.problems.length > 0); }
                  catch (e) { toast(e.message, true); }
                  ev.target.disabled = false; ev.target.textContent = "Verify";
                }}, "Verify"),
                h("button", {class: "btn sm danger", onclick: () => confirmDlg("Delete " + b.name + "?", "The backup file is deleted from " + d.name + ". This cannot be undone.", "Delete",
                  async () => { await api("DELETE", "/api/backups/" + did + "/" + encodeURIComponent(b.name)); route(); })}, "Delete")))))));
    } catch (e) { list.replaceChildren(h("div", {class: "err"}, e.message)); }
  })();
  return h("div", {}, h("div", {class: "spread"}, h("div", {class: "row"}, "Destination", pick), h("button", {class: "btn", onclick: route}, "Refresh")), list);
};

/* ---------------------------------------------------------------- restore */

RENDER.restore = async (did, name) => {
  const d = byId(S.destinations, did);
  const wrap = h("div", {}, h("div", {class: "card hint"}, d && d.type === "gdrive" ? "Downloading the backup from Google Drive and reading it... (large backups take a while)" : "Reading the backup..."));
  (async () => {
    let c;
    try { c = await api("GET", "/api/backups/" + did + "/" + encodeURIComponent(name) + "/contents"); }
    catch (e) { wrap.replaceChildren(h("div", {class: "card err"}, e.message)); return; }
    wrap.replaceChildren(restoreForm(did, name, c));
  })();
  return wrap;
};

function restoreForm(did, name, c) {
  const dbState = c.databases.map(db => {
    const objs = [...db.tables.map(t => ["table", t]), ...db.views.map(v => ["view", v]),
      ...(db.routines ? [["routines", null]] : []), ...(db.events ? [["events", null]] : [])];
    const st = {db, objs, on: h("input", {type: "checkbox"}), mode: h("select", {style: "width:auto;padding:3px 6px"},
      h("option", {value: "all"}, "everything"), h("option", {value: "some"}, "pick tables...")),
      target: h("input", {class: "target", value: db.name, title: "Restore under this database name"}),
      checks: objs.map(o => h("input", {type: "checkbox", checked: true}))};
    st.box = h("div", {class: "objs", hidden: true}, objs.map((o, i) => h("label", {class: "inline"}, st.checks[i],
      o[1] || (o[0] === "routines" ? "procedures & functions" : "events"), o[1] ? h("span", {class: "kind"}, o[0]) : null)));
    st.mode.onchange = () => st.box.hidden = st.mode.value !== "some";
    st.on.onchange = () => { st.mode.disabled = st.target.disabled = !st.on.checked; if (!st.on.checked) st.box.hidden = true; else st.mode.onchange(); };
    st.on.onchange();
    return st;
  });
  const roles = c.accounts.roles.map(r => ({kind: "role", name: r, system: false, cb: h("input", {type: "checkbox"})}));
  const users = c.accounts.users.map(u => ({kind: "user", name: u.name, system: u.system, cb: h("input", {type: "checkbox"})}));
  const accts = [...roles, ...users.filter(u => !u.system), ...users.filter(u => u.system)];
  const server = h("select", {}, S.servers.map(s => h("option", {value: s.id}, s.name)));
  const opt = {schema: h("input", {type: "checkbox"}), replace: h("input", {type: "checkbox"}), force: h("input", {type: "checkbox"})};
  const allDbs = h("button", {class: "btn sm", type: "button", onclick: () => dbState.forEach(s => { s.on.checked = true; s.on.onchange(); })}, "select all");
  const allUsers = h("button", {class: "btn sm", type: "button", onclick: () => accts.forEach(a => a.cb.checked = !a.system)}, "all except system accounts");

  function plan() {
    const p = {databases: [], roles: [], users: [], schema_only: opt.schema.checked, replace_users: opt.replace.checked, force: opt.force.checked};
    for (const s of dbState) {
      if (!s.on.checked) continue;
      const objects = s.mode.value === "all" ? null : s.objs.filter((o, i) => s.checks[i].checked);
      if (objects && !objects.length) continue;
      p.databases.push({name: s.db.name, target: s.target.value.trim() || s.db.name, objects});
    }
    for (const a of accts) if (a.cb.checked) (a.kind === "role" ? p.roles : p.users).push(a.name);
    return p;
  }

  async function review() {
    const p = plan();
    if (!p.databases.length && !p.roles.length && !p.users.length) { toast("Pick something to restore", true); return; }
    const out = h("div", {class: "hint"}, "Checking the target server...");
    const dlg = dialog("Restore onto " + server.selectedOptions[0].text + "?", out, [{label: "Cancel"}]);
    let pv;
    try { pv = await api("POST", "/api/backups/" + did + "/" + encodeURIComponent(name) + "/preview", {server_id: server.value, plan: p}); }
    catch (e) { out.replaceChildren(h("div", {class: "err"}, e.message)); return; }
    dialog("Restore onto " + server.selectedOptions[0].text + "?", h("div", {},
      h("p", {class: "hint"}, "MariaDB " + pv.version + (pv.galera ? " · Galera node (" + pv.galera.state + "); everything restored replicates to the whole cluster" : "")),
      h("ul", {style: "margin:10px 0 0 18px"}, pv.lines.map(l => h("li", {}, l))),
      pv.warnings.length ? h("div", {class: "note"}, h("b", {}, "On the target server:"), h("ul", {style: "margin:4px 0 0 18px"}, pv.warnings.map(w => h("li", {}, w)))) : null),
      [{label: "Cancel"}, {label: "Restore now", cls: "danger solid", run: async () => {
        const r = await api("POST", "/api/backups/" + did + "/" + encodeURIComponent(name) + "/restore", {server_id: server.value, plan: p});
        location.hash = "#run/" + r.run_id;
      }}]);
  }

  return h("div", {},
    h("div", {class: "card"}, h("h2", {}, c.label || c.name),
      h("p", {class: "hint"}, "Taken " + when(c.created) + " from " + c.source + " · MariaDB " + c.server_version + (c.galera ? " · Galera" : "") + " · " + c.consistency),
      h("p", {class: "mono hint"}, name)),
    h("div", {class: "card"}, h("div", {class: "spread"}, h("h2", {}, "Databases"), c.databases.length ? allDbs : null),
      !c.databases.length ? h("div", {class: "hint"}, "This backup holds no databases.") :
        h("div", {class: "tree"}, dbState.map(s => h("div", {class: "db"},
          h("div", {class: "dbhead"}, h("label", {class: "inline"}, s.on, h("b", {}, s.db.name)),
            h("span", {class: "hint"}, s.db.tables.length + " tables, " + s.db.views.length + " views · " + size(s.db.bytes)),
            s.mode, h("span", {class: "hint"}, "as"), s.target), s.box)))),
    h("div", {class: "card"}, h("div", {class: "spread"}, h("h2", {}, "Users, roles and grants"), accts.length ? allUsers : null),
      !c.has_accounts ? h("div", {class: "hint"}, "This backup was taken without users.") :
        h("div", {class: "chips"}, accts.map(a => h("label", {class: "inline"}, a.cb, a.name, h("span", {class: "kind"}, a.kind),
          a.system ? h("span", {class: "kind", style: "color:var(--warn)"}, "system") : null))),
      c.has_accounts ? h("p", {class: "hint", style: "margin-top:8px"}, "Grants are restored after the databases, so grants on restored tables and procedures apply cleanly. Passwords are restored as their stored hashes.") : null),
    h("div", {class: "card"}, h("h2", {}, "Restore onto"),
      S.servers.length ? server : h("div", {class: "err"}, "Add a server first."),
      h("label", {class: "inline"}, opt.schema, "Structure only (no table rows)"),
      h("label", {class: "inline"}, opt.replace, "Replace users that already exist (password and settings); otherwise they are kept and only grants are added"),
      h("label", {class: "inline"}, opt.force, "Continue past SQL errors"),
      h("div", {class: "note"}, "Restored tables and views replace existing ones of the same name in the target database. Nothing else in it is touched."),
      h("div", {class: "row", style: "margin-top:12px"}, h("button", {class: "btn pri", onclick: review, disabled: !S.servers.length}, "Review restore..."),
        h("a", {href: "#backups/" + did}, "back to backups"))));
}

/* ---------------------------------------------------------------- activity */

function runsTable(runs) {
  if (!runs.length) return h("div", {class: "empty"}, "Nothing has run yet.");
  return h("div", {class: "tablewrap"}, h("table", {}, h("tr", {}, h("th", {}, "What"), h("th", {}, "Status"), h("th", {}, "Started"), h("th", {}, "Took"), h("th", {}, "Result")),
    runs.map(r => h("tr", {style: "cursor:pointer", onclick: () => location.hash = "#run/" + r.id},
      h("td", {}, h("a", {href: "#run/" + r.id}, r.title), h("div", {class: "hint"}, r.trigger === "schedule" ? "scheduled" : "manual")),
      h("td", {}, pill(r.status)), h("td", {}, when(r.started || r.queued)), h("td", {}, dur(r.started, r.finished)),
      h("td", {class: "hint"}, r.status === "ok" && r.result && r.result.size ? size(r.result.size) + (r.result.pruned && r.result.pruned.length ? ", " + r.result.pruned.length + " old removed" : "") :
        (r.status === "failed" ? r.last.replace(/^\S+ /, "") : ""))))));
}

RENDER.activity = async () => {
  const {runs} = await api("GET", "/api/runs?limit=200");
  if (runs.some(r => r.status === "running" || r.status === "queued")) pollTimer = setTimeout(route, 4000);
  return h("div", {class: "card"}, runsTable(runs));
};

RENDER.run = async (rid) => {
  const box = h("div", {});
  async function draw() {
    const r = await api("GET", "/api/runs/" + rid);
    const logEl = h("div", {class: "log"}, r.log.map(l => h("div", {class: /FAILED|error:|ERROR/.test(l) ? "e" : /warning|note:/.test(l) ? "w" : ""}, l)));
    box.replaceChildren(h("div", {class: "card"},
      h("div", {class: "spread"}, h("div", {}, h("h2", {}, r.title), h("div", {class: "hint"}, (r.trigger === "schedule" ? "Scheduled" : "Started by hand") + " · queued " + when(r.queued) + (r.finished ? " · took " + dur(r.started, r.finished) : ""))),
        h("div", {class: "row"}, pill(r.status),
          ["queued", "running"].includes(r.status) ? h("button", {class: "btn sm danger", onclick: async () => { await api("POST", "/api/runs/" + rid + "/cancel"); draw(); }}, "Cancel") : null)),
      r.status === "ok" && r.kind === "backup" && r.result ? h("p", {class: "ok-msg"}, "Backup " + r.result.file + " (" + size(r.result.size) + ") stored. ",
        h("a", {href: "#backups/" + r.result.destination_id}, "See backups")) : null,
      logEl.childNodes.length ? logEl : h("div", {class: "hint"}, "Waiting to start...")));
    logEl.scrollTop = logEl.scrollHeight;
    if (["queued", "running"].includes(r.status) && location.hash === "#run/" + rid) pollTimer = setTimeout(draw, 1500);
    else refresh().catch(() => {});
  }
  await draw();
  return box;
};

/* ---------------------------------------------------------------- settings */

RENDER.settings = async () => {
  const f = {user: h("input", {value: S.user || "", autocomplete: "username"}), cur: h("input", {type: "password", autocomplete: "current-password"}),
    pw: h("input", {type: "password", autocomplete: "new-password"}), pw2: h("input", {type: "password", autocomplete: "new-password"})};
  const msg = h("div", {});
  let theme = "system"; try { theme = localStorage.getItem("mbm_theme") || "system"; } catch (e) {}
  const themeSel = h("select", {style: "width:auto", onchange: e => {
    try { localStorage.setItem("mbm_theme", e.target.value); } catch (x) {}
    const dark = e.target.value === "dark" || (e.target.value === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }}, ["system", "light", "dark"].map(t => h("option", {value: t, selected: t === theme}, t)));
  return h("div", {},
    h("div", {class: "card"}, h("h2", {}, "Administrator login"),
      h("div", {class: "frm2"}, h("div", {}, h("label", {}, "Username"), f.user), h("div", {}, h("label", {}, "Current password"), f.cur),
        h("div", {}, h("label", {}, "New password"), f.pw), h("div", {}, h("label", {}, "Repeat new password"), f.pw2)), msg,
      h("button", {class: "btn pri", style: "margin-top:12px", onclick: async () => {
        msg.replaceChildren();
        if (f.pw.value !== f.pw2.value) { msg.replaceChildren(h("div", {class: "err"}, "The new passwords differ")); return; }
        try { await api("POST", "/api/password", {username: f.user.value, current: f.cur.value, password: f.pw.value});
          msg.replaceChildren(h("div", {class: "ok-msg"}, "Changed. Other signed-in browsers have been signed out.")); }
        catch (e) { msg.replaceChildren(h("div", {class: "err"}, e.message)); }
      }}, "Change")),
    h("div", {class: "card"}, h("h2", {}, "Appearance"), h("div", {class: "row"}, "Theme", themeSel)),
    h("div", {class: "card"}, h("h2", {}, "About"), h("p", {}, "MariaDB Backup Manager " + S.version),
      h("p", {class: "hint"}, "Server time " + when(S.now) + ". Schedules run in this time zone."),
      h("p", {class: "hint"}, "Command-line restore without the UI: ", h("span", {class: "mono"}, "python3 /opt/mariadb-backup-manager/app.py cli --help"))));
};

boot();
