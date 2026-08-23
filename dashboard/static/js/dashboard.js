(function () {
  "use strict";

  // ── Server-Sent Events (SSE) ─────────────────────────
  // SSE ist einfacher als WebSocket, funktioniert mit jedem
  // normalen HTTP-Server und reconnected automatisch.
  let es = null;
  let reconnect = null;

  function connect() {
    // Vorherige Verbindung schließen
    if (es) { try { es.close(); } catch (_) {} }

    dot("connecting");
    es = new EventSource("/ws");

    es.onopen = () => {
      dot("on");
      clearTimeout(reconnect);
    };

    es.onmessage = (e) => {
      let p;
      try { p = JSON.parse(e.data); } catch { return; }
      if (p.type === "ping") return;
      if (p.type === "initial_state") {
        if (p.data && p.data.now_playing !== undefined) {
          updateNowPlaying(p.data.now_playing);
        }
        return;
      }
      route(p);
    };

    es.onerror = () => {
      dot("off");
      es.close();
      clearTimeout(reconnect);
      reconnect = setTimeout(connect, 5000);
    };
  }

  function dot(s) {
    const d = document.getElementById("ws-dot");
    const l = document.getElementById("ws-label");
    if (d) d.className = "ws-dot " + s;
    if (l) l.textContent = { on: "Live", off: "Getrennt", connecting: "Verbinde..." }[s] || s;
  }

  // ── Event Router ─────────────────────────────────────
  function route(p) {
    const { type, data } = p;
    switch (type) {
      case "now_playing":
        updateNowPlaying(data && data.title ? data : null);
        break;
      case "member_join":
        prepend("join-list", data, data.suspicious ? "fd-red" : "fd-green");
        bump("sv-joins");
        toast(esc(data.name || "?") + " ist beigetreten", data.suspicious ? "red" : "green");
        break;
      case "twitch_mod":
        prepend("mod-list", data, "fd-amber");
        bump("sv-mods");
        toast(esc(data.action || "?") + " → " + esc(data.target || "?"), "amber");
        break;
      case "message_count":
        bump("sv-msgs");
        break;
      case "vc_join":
        toast(esc(data.username || "?") + " → " + esc(data.channel_name || "Voice"), "blue");
        break;
      case "vc_leave":
        toast(esc(data.username || "?") + " hat Voice verlassen", "blue");
        break;
      case "vc_owner_change":
        toast("Owner: " + esc(data.old_owner) + " → " + esc(data.new_owner), "amber");
        break;
      case "vc_owner_recovery":
        toast(esc(data.username) + " hat Owner-Rang zurück", "blue");
        break;
      case "settings_changed":
        toast("Einstellungen gespeichert", "green");
        break;
    }
  }

  // ── Now Playing ──────────────────────────────────────
  function updateNowPlaying(song) {
    const box = document.getElementById("np-box");
    if (!box) return;
    if (!song) {
      box.innerHTML = `<div class="np-idle"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>Kein Song spielt gerade</div>`;
      return;
    }
    const art = song.thumbnail
      ? `<img class="np-artwork" src="${esc(song.thumbnail)}" alt="">`
      : `<div class="np-artwork-ph"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>`;
    const yt = song.webpage_url ? ` · <a href="${esc(song.webpage_url)}" target="_blank">YouTube</a>` : "";
    box.innerHTML = `
      <div class="now-playing">
        ${art}
        <div class="np-info">
          <div class="np-title">${esc(song.title)}</div>
          <div class="np-meta">von <strong>${esc(song.requester || "?")}</strong> · ${esc(song.duration_str || "")}${yt}</div>
        </div>
        <div class="np-wave">
          <div class="np-bar" style="height:8px"></div>
          <div class="np-bar" style="height:14px"></div>
          <div class="np-bar" style="height:6px"></div>
        </div>
      </div>`;
  }

  // ── Feed prepend ─────────────────────────────────────
  function prepend(id, data, dotClass) {
    const list = document.getElementById(id);
    if (!list) return;
    const es2 = list.querySelector(".empty-state");
    if (es2) es2.remove();
    const el = document.createElement("div");
    el.className = "feed-item";
    el.innerHTML = `<div class="feed-dot ${dotClass}"></div><div class="feed-body"><div class="feed-text">${buildText(data)}</div><div class="feed-time">Gerade eben</div></div>`;
    list.insertBefore(el, list.firstChild);
    while (list.children.length > 30) list.removeChild(list.lastChild);
  }

  function buildText(d) {
    if (d.name)   return `<strong>${esc(d.name)}</strong>${d.suspicious ? ' <span class="card-badge badge-red" style="margin-left:5px">Verdächtig</span>' : ""}`;
    if (d.target) return `<strong>${esc(d.action || "?")}</strong> → ${esc(d.target)}${d.moderator ? ` <span style="color:var(--t2)">· ${esc(d.moderator)}</span>` : ""}`;
    return JSON.stringify(d).slice(0, 80);
  }

  // ── Stat bump ────────────────────────────────────────
  function bump(id) {
    const el = document.getElementById(id);
    if (!el) return;
    const n = parseInt(el.textContent.replace(/\D/g, "")) || 0;
    el.textContent = (n + 1).toLocaleString("de");
  }

  // ── Toasts ───────────────────────────────────────────
  const ICONS = {
    green: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>`,
    red:   `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
    amber: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
    blue:  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
  };

  function toast(msg, type) {
    const c = document.getElementById("toasts");
    if (!c) return;
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.innerHTML = (ICONS[type] || "") + `<span>${msg}</span>`;
    c.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateX(8px)";
      setTimeout(() => el.remove(), 300);
    }, 3500);
  }

  // ── Settings toggles ─────────────────────────────────
  document.querySelectorAll(".toggle input").forEach(cb => {
    cb.addEventListener("change", function () {
      const lbl = this.closest(".toggle-row")?.querySelector(".toggle-label");
      if (lbl) lbl.textContent = this.checked ? "Aktiviert" : "Deaktiviert";
    });
  });

  // ── Util ─────────────────────────────────────────────
  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // ── Init ─────────────────────────────────────────────
  connect();
})();
