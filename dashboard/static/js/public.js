const H = window.__HUB__ || {};

const $ = (sel) => document.querySelector(sel);

function initials(name) {
  return (name || "?").slice(0, 2).toUpperCase();
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
      if (d.stream) c.classList.add(d.count >= 4 ? "hot" : "stream");
      if (d.today) c.classList.add("today");
      if (d.future) c.classList.add("future");
      c.title = `${d.date} · ${d.count} Mods`;
      col.appendChild(c);
    });
    root.appendChild(col);
  });
}

function renderChart(id, series, key, cls) {
  const root = document.getElementById(id);
  if (!root) return;
  const max = Math.max(1, ...series.map((x) => x[key] || 0));
  root.innerHTML = series.map((x) => {
    const h = Math.max(6, Math.round(((x[key] || 0) / max) * 140));
    return `<div class="bar ${cls || ""}" style="height:${h}px"><span>${x.date}</span></div>`;
  }).join("");
}

function heatDots(hm) {
  return (hm || []).filter((c) => !c.empty).map((c) =>
    `<b class="${c.state}${c.today ? " today" : ""}"></b>`
  ).join("");
}

function renderRoster() {
  const body = $("#roster-body");
  if (!body) return;
  body.innerHTML = (H.roster || []).map((r) => `
    <tr data-uid="${r.uid}">
      <td>${r.rank}</td>
      <td>
        <div class="who">
          <div class="av">${initials(r.display_name)}</div>
          <div>
            ${r.display_name}
            <small>${r.twitch_name ? "twitch.tv/" + r.twitch_name : "kein Twitch"}</small>
          </div>
        </div>
      </td>
      <td><span class="grade ${r.grade[0].toLowerCase()}">${r.grade_emoji} ${r.grade}</span></td>
      <td>${r.present}/${r.present + r.absent} · ${r.pct}%</td>
      <td>${r.streak}🔥 <small style="color:var(--dim)">max ${r.longest_streak}</small></td>
      <td>${r.twitch_messages}</td>
      <td>${r.voice_label}</td>
      <td><div class="heatmini">${heatDots(r.heatmap)}</div></td>
    </tr>
  `).join("");
  body.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", () => openModal(tr.dataset.uid));
  });
}

function renderLB(id, items, labelFn, valueFn) {
  const root = document.getElementById(id);
  if (!root) return;
  const max = Math.max(1, ...items.map(valueFn));
  root.innerHTML = items.map((it, i) => `
    <div class="lb-row">
      <div class="n">${String(i + 1).padStart(2, "0")}</div>
      <div>
        ${it.username || it.name || it.display_name}
        <div class="meter"><i style="width:${Math.round(valueFn(it) / max * 100)}%"></i></div>
      </div>
      <strong>${labelFn(it)}</strong>
    </div>
  `).join("") || `<div class="hint" style="color:var(--dim)">Noch keine Daten.</div>`;
}

function renderMonths() {
  const root = $("#months");
  if (!root) return;
  root.innerHTML = (H.monthly || []).map((m) => `
    <div class="mcell">
      <div class="mn">${m.name}</div>
      <div class="mv">${m.streams}</div>
    </div>
  `).join("");
}

function openModal(uid) {
  const r = (H.roster || []).find((x) => x.uid === uid);
  if (!r) return;
  const inner = $("#modal-inner");
  const days = (r.heatmap || []).map((c) => {
    if (c.empty) return `<div class="d empty"></div>`;
    return `<div class="d ${c.state}${c.today ? " today" : ""}">${c.day}</div>`;
  }).join("");
  inner.innerHTML = `
    <h3>${r.grade_emoji} ${r.display_name}</h3>
    <p style="color:var(--muted)">Rang #${r.rank} · ${r.twitch_name ? "twitch.tv/" + r.twitch_name : "kein Twitch"}</p>
    <div class="stats-grid">
      <div class="statp"><span>Quote</span><strong>${r.pct}%</strong></div>
      <div class="statp"><span>Streak</span><strong>${r.streak} / ${r.longest_streak}</strong></div>
      <div class="statp"><span>Twitch-Msgs</span><strong>${r.twitch_messages}</strong></div>
      <div class="statp"><span>Discord</span><strong>${r.discord_messages}</strong></div>
      <div class="statp"><span>Voice</span><strong>${r.voice_label}</strong></div>
      <div class="statp"><span>Peak-Stunde</span><strong>${r.peak_hour != null ? r.peak_hour + ":00" : "–"}</strong></div>
    </div>
    <div class="cal">${days}</div>
  `;
  $("#modal").classList.add("open");
}

function boot() {
  renderWeeks();
  renderChart("msg-chart", H.daily_messages || [], "count");
  renderChart("voice-chart", H.daily_voice || [], "seconds", "voice");
  renderRoster();
  renderMonths();
  renderLB("voice-lb", H.voice_leaderboard || [], (x) => x.label, (x) => x.seconds);
  renderLB("msg-lb", H.msg_leaderboard || [], (x) => x.total, (x) => x.total);
  renderLB("joins", (H.recent_joins || []).map((j) => ({
    username: j.name, total: j.age_days, seconds: j.age_days
  })), (x) => `${x.total}d`, (x) => x.total || 1);

  $("#modal").addEventListener("click", (e) => {
    if (e.target.id === "modal") e.currentTarget.classList.remove("open");
  });
}

boot();

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
