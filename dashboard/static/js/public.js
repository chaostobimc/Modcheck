const H = window.__HUB__ || {};
const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function initials(name) {
  return (name || "?").replace(/[^A-Za-z0-9ÄÖÜäöüß]/g, "").slice(0, 2).toUpperCase() || "?";
}

function heatDots(hm) {
  return (hm || []).filter((c) => !c.empty).map((c) =>
    `<i class="${c.state}${c.today ? " today" : ""}"></i>`
  ).join("");
}

function renderStreams() {
  const body = $("#streams-body");
  if (!body) return;
  body.innerHTML = (H.recent_streams || []).map((s) => `
    <tr>
      <td>${esc(s.date_fmt)}</td>
      <td>${esc(s.weekday)}</td>
      <td>${s.count}</td>
      <td>${esc((s.names || []).join(", ") || "—")}</td>
    </tr>
  `).join("") || `<tr><td colspan="4">Keine Stream-Tage.</td></tr>`;
}

function renderWeekdays() {
  const root = $("#weekday-bars");
  if (!root) return;
  const counts = H.weekday_counts || [];
  const labels = H.weekdays || ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
  const max = Math.max(1, ...counts);
  root.innerHTML = labels.map((lab, i) => {
    const n = counts[i] || 0;
    const h = Math.max(4, Math.round((n / max) * 96));
    return `<div><b style="height:${h}px" title="${n}"></b><span>${lab}</span></div>`;
  }).join("");
}

function renderHours() {
  const root = $("#hour-bars");
  if (!root) return;
  const hours = H.hours || [];
  const max = Math.max(1, ...hours);
  root.innerHTML = hours.map((n, i) => {
    const h = Math.max(2, Math.round((n / max) * 80));
    return `<i style="height:${h}px" title="${i}:00 · ${n}"></i>`;
  }).join("");
}

function renderRoster() {
  const body = $("#roster-body");
  if (!body) return;
  body.innerHTML = (H.roster || []).map((r) => `
    <tr class="click" data-uid="${esc(r.uid)}">
      <td>${r.rank}</td>
      <td>
        <div class="who">
          <div class="av">${esc(initials(r.display_name))}</div>
          <div>${esc(r.display_name)}<small>${r.twitch_name ? "twitch.tv/" + esc(r.twitch_name) : "kein Twitch"}</small></div>
        </div>
      </td>
      <td><span class="grade ${esc(r.grade[0].toLowerCase())}">${esc(r.grade)}</span></td>
      <td>${r.present}/${r.present + r.absent} · ${r.pct}%</td>
      <td>${r.streak} <small style="color:var(--faint)">max ${r.longest_streak}</small></td>
      <td>${r.month_present}/${r.month_total}${r.month_excused ? ` · ${r.month_excused} abgem.` : ""}</td>
      <td>${r.twitch_messages}</td>
      <td>${r.avg_twitch}</td>
      <td>${r.discord_messages}</td>
      <td>${esc(r.voice_label)}</td>
      <td>${esc(r.last_seen_fmt)}</td>
      <td><div class="dots">${heatDots(r.heatmap)}</div></td>
    </tr>
  `).join("");
  body.querySelectorAll("tr.click").forEach((tr) => {
    tr.addEventListener("click", () => openModal(tr.dataset.uid));
  });
}

function renderMonths() {
  const root = $("#months");
  if (!root) return;
  root.innerHTML = (H.monthly || []).map((m) => `
    <div><span>${esc(m.name)}</span><b>${m.streams}</b></div>
  `).join("");
}

function renderWeeks() {
  const root = $("#weeks");
  if (!root) return;
  root.innerHTML = "";
  (H.weeks || []).forEach((week) => {
    const col = document.createElement("div");
    col.className = "week";
    week.forEach((d) => {
      const c = document.createElement("div");
      c.className = "cell";
      if (d.stream) c.classList.add(d.count >= 3 ? "hot" : "stream");
      if (d.today) c.classList.add("today");
      if (d.future) c.classList.add("future");
      c.title = `${d.date} · ${d.count} Mods`;
      col.appendChild(c);
    });
    root.appendChild(col);
  });
}

function renderChart(id, series, key) {
  const root = document.getElementById(id);
  if (!root) return;
  const max = Math.max(1, ...series.map((x) => x[key] || 0));
  root.innerHTML = series.map((x) => {
    const h = Math.max(2, Math.round(((x[key] || 0) / max) * 130));
    const label = x.full || x.date;
    const extra = key === "seconds" ? (x.label || "") : (x.count ?? x[key]);
    return `<div class="col" style="height:${h}px" title="${esc(label)} · ${esc(extra)}"></div>`;
  }).join("");
}

function renderLB(id, items, labelFn, valueFn) {
  const root = document.getElementById(id);
  if (!root) return;
  if (!items.length) {
    root.innerHTML = `<li style="color:var(--muted)">Keine Daten.</li>`;
    return;
  }
  const max = Math.max(1, ...items.map(valueFn));
  root.innerHTML = items.map((it, i) => `
    <li>
      <span class="n">${String(i + 1).padStart(2, "0")}</span>
      <div>
        ${esc(it.username || it.name)}
        <div class="meter"><i style="width:${Math.round(valueFn(it) / max * 100)}%"></i></div>
      </div>
      <strong>${esc(labelFn(it))}</strong>
    </li>
  `).join("");
}

function renderJoins() {
  const root = $("#joins");
  if (!root) return;
  const items = H.recent_joins || [];
  if (!items.length) {
    root.innerHTML = `<li style="color:var(--muted)">Keine Einträge.</li>`;
    return;
  }
  root.innerHTML = items.map((j) => `
    <li>
      <span class="n">${j.avatar ? `<img src="${esc(j.avatar)}" alt="">` : ""}</span>
      <div>${esc(j.name)}<div class="meter"></div></div>
      <span>${esc(j.ts_fmt)} · ${j.age_days}d Account</span>
    </li>
  `).join("");
}

function openModal(uid) {
  const r = (H.roster || []).find((x) => x.uid === uid);
  if (!r) return;
  const days = (r.heatmap || []).map((c) => {
    if (c.empty) return `<div class="d empty"></div>`;
    return `<div class="d ${c.state}${c.today ? " today" : ""}">${c.day}</div>`;
  }).join("");
  $("#modal-inner").innerHTML = `
    <h3>${esc(r.display_name)}</h3>
    <p class="sub">Rang ${r.rank} · Note ${esc(r.grade)} · ${r.twitch_name ? "twitch.tv/" + esc(r.twitch_name) : "kein Twitch"}</p>
    <div class="stats-grid">
      <div class="statp"><span>Quote</span><strong>${r.pct}%</strong></div>
      <div class="statp"><span>Anwesend</span><strong>${r.present} / ${r.present + r.absent}</strong></div>
      <div class="statp"><span>Streak</span><strong>${r.streak} / ${r.longest_streak}</strong></div>
      <div class="statp"><span>Twitch</span><strong>${r.twitch_messages} (Ø ${r.avg_twitch})</strong></div>
      <div class="statp"><span>Discord</span><strong>${r.discord_messages}</strong></div>
      <div class="statp"><span>Voice</span><strong>${esc(r.voice_label)}</strong></div>
      <div class="statp"><span>Peak</span><strong>${r.peak_hour != null ? r.peak_hour + ":00" : "—"}</strong></div>
      <div class="statp"><span>Seit</span><strong>${esc(r.first_seen_fmt)}</strong></div>
      <div class="statp"><span>Zuletzt</span><strong>${esc(r.last_seen_fmt)}</strong></div>
    </div>
    <div class="cal">${days}</div>
  `;
  const bg = $("#modal");
  bg.hidden = false;
}

function boot() {
  renderStreams();
  renderWeekdays();
  renderHours();
  renderRoster();
  renderMonths();
  renderWeeks();
  renderChart("msg-chart", H.daily_messages || [], "count");
  renderChart("voice-chart", H.daily_voice || [], "seconds");
  renderLB("msg-lb", H.msg_leaderboard || [], (x) => x.total, (x) => x.total);
  renderLB("voice-lb", H.voice_leaderboard || [], (x) => x.label, (x) => x.seconds);
  renderJoins();
}

boot();

const modal = $("#modal");
if (modal && !modal.dataset.bound) {
  modal.dataset.bound = "1";
  modal.addEventListener("click", (e) => {
    if (e.target.id === "modal") modal.hidden = true;
  });
}

setInterval(async () => {
  try {
    const res = await fetch("/api/public/hub");
    if (!res.ok) return;
    const next = await res.json();
    Object.assign(H, next);
    boot();
    const s = document.getElementById("stamp");
    if (s) s.textContent = next.generated_at;
  } catch (_) {}
}, 30000);
