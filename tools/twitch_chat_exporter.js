// ==UserScript==
// @name         Twitch Viewer Card Chat-Verlauf Exporter (.txt)
// @namespace    https://github.com/chaostobimc/Modcheck
// @version      1.1.0
// @description  Scrollt im Twitch-Nachrichtenverlauf (Viewer Card / Zuschauerkarte) automatisch ganz nach oben und speichert alle Chatnachrichten mit Datum & Uhrzeit als .txt-Datei.
// @match        https://www.twitch.tv/popout/*/viewercard/*
// @match        https://www.twitch.tv/*
// @grant        none
// ==/UserScript==

/**
 * SCHNELL-ANLEITUNG (Ohne Erweiterung – direkt in der Browser-Konsole):
 * 1. Öffne auf Twitch die Zuschauerkarte der Person (am besten direkt als eigenes Tab):
 *      https://www.twitch.tv/popout/DEIN_KANAL/viewercard/TWITCH_USERNAME
 *    (Alternativ im Chat / Mod-View auf den Namen klicken, sodass der Nachrichtenverlauf offen ist.)
 * 2. Drücke F12 -> Reiter "Konsole" (Console).
 *    (Falls der Browser beim ersten Einfügen fragt: kurz "allow pasting" bzw. "Einfügen erlauben" eingeben.)
 * 3. Füge dieses komplette Skript ein und drücke Enter.
 * 4. Oben rechts erscheint eine Live-Box: Das Skript scrollt von alleine bis zur allerersten
 *    Nachricht ganz oben und speichert am Ende automatisch die fertige .txt-Datei!
 */

(function () {
  "use strict";

  if (window.__modcheckExporterRunning) {
    alert("Der Twitch Chat-Exporter läuft bereits! Nutze das Panel oben rechts.");
    return;
  }
  window.__modcheckExporterRunning = true;

  // ── Ziel-User & Kanal aus URL erkennen ───────────────────────────────────
  const urlMatch = location.pathname.match(/\/(?:popout\/)?([^/]+)\/viewercard\/([^/?#]+)/i);
  let detectedChannel = urlMatch ? decodeURIComponent(urlMatch[1]).toLowerCase() : "";
  let detectedUser = urlMatch ? decodeURIComponent(urlMatch[2]).toLowerCase() : "";

  const collected = new Map();
  let gqlCount = 0;
  let domCount = 0;
  let isPaused = false;
  let stopRequested = false;

  // ── Datums- & Zeit-Helfer (Deutsch & Englisch, Europe/Berlin) ────────────
  const MONTHS_MAP = {
    jan: 1, januar: 1, january: 1,
    feb: 2, februar: 2, february: 2,
    mär: 3, maerz: 3, märz: 3, mar: 3, march: 3,
    apr: 4, april: 4,
    mai: 5, may: 5,
    jun: 6, juni: 6, june: 6,
    jul: 7, juli: 7, july: 7,
    aug: 8, august: 8,
    sep: 9, sept: 9, september: 9,
    okt: 10, oktober: 10, oct: 10, october: 10,
    nov: 11, november: 11,
    dez: 12, dezember: 12, dec: 12, december: 12,
  };

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function normText(s) {
    return String(s || "").replace(/\s+/g, " ").trim();
  }

  function toBerlinParts(dateObj) {
    try {
      const fmt = new Intl.DateTimeFormat("de-DE", {
        timeZone: "Europe/Berlin",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      });
      const parts = {};
      for (const p of fmt.formatToParts(dateObj)) {
        parts[p.type] = p.value;
      }
      const dateIso = `${parts.year}-${parts.month}-${parts.day}`;
      const dateDe = `${parts.day}.${parts.month}.${parts.year}`;
      const h = parts.hour === "24" ? "00" : parts.hour;
      const timeStr = `${h}:${parts.minute}:${parts.second}`;
      return { dateIso, dateDe, timeStr };
    } catch (_) {
      const y = dateObj.getFullYear();
      const m = pad2(dateObj.getMonth() + 1);
      const d = pad2(dateObj.getDate());
      const timeStr = `${pad2(dateObj.getHours())}:${pad2(dateObj.getMinutes())}:${pad2(dateObj.getSeconds())}`;
      return { dateIso: `${y}-${m}-${d}`, dateDe: `${d}.${m}.${y}`, timeStr };
    }
  }

  function formatIsoToDe(dateIso) {
    if (!dateIso || dateIso.length < 10) return dateIso || "Unbekanntes Datum";
    const [y, m, d] = dateIso.slice(0, 10).split("-");
    return `${d}.${m}.${y}`;
  }

  function parseDateHeader(rawText) {
    if (!rawText) return null;
    const s = normText(rawText);
    const low = s.toLowerCase();
    const now = new Date();

    if (low === "heute" || low === "today") {
      return toBerlinParts(now).dateIso;
    }
    if (low === "gestern" || low === "yesterday") {
      return toBerlinParts(new Date(now.getTime() - 86400000)).dateIso;
    }

    // Format: DD.MM.YYYY oder DD.MM.YY
    let m = s.match(/\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b/);
    if (m) {
      const dayNum = parseInt(m[1], 10);
      const monNum = parseInt(m[2], 10);
      let yr = parseInt(m[3], 10);
      if (yr < 100) yr += 2000;
      if (dayNum >= 1 && dayNum <= 31 && monNum >= 1 && monNum <= 12 && yr >= 2005 && yr <= 2100) {
        return `${yr}-${pad2(monNum)}-${pad2(dayNum)}`;
      }
    }

    // Format Deutsch: "(Montag, )3. Nov. 2026" oder "3. November"
    m = low.match(/(\d{1,2})\.?\s+([a-zäöü]+)\.?(?:\s+(\d{4}))?/i);
    if (m) {
      const monNum = MONTHS_MAP[m[2].replace(/\.$/, "")];
      const dayNum = parseInt(m[1], 10);
      if (monNum && dayNum >= 1 && dayNum <= 31) {
        const yr = m[3] ? parseInt(m[3], 10) : now.getFullYear();
        return `${yr}-${pad2(monNum)}-${pad2(dayNum)}`;
      }
    }

    // Format Englisch: "Nov 3, 2026" oder "November 3 2026"
    m = low.match(/([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(\d{4}))?/i);
    if (m) {
      const monNum = MONTHS_MAP[m[1].replace(/\.$/, "")];
      const dayNum = parseInt(m[2], 10);
      if (monNum && dayNum >= 1 && dayNum <= 31) {
        const yr = m[3] ? parseInt(m[3], 10) : now.getFullYear();
        return `${yr}-${pad2(monNum)}-${pad2(dayNum)}`;
      }
    }

    // Format US: MM/DD/YYYY
    m = s.match(/\b(\d{1,2})\/(\d{1,2})\/(\d{2,4})\b/);
    if (m) {
      const monNum = parseInt(m[1], 10);
      const dayNum = parseInt(m[2], 10);
      let yr = parseInt(m[3], 10);
      if (yr < 100) yr += 2000;
      if (dayNum >= 1 && dayNum <= 31 && monNum >= 1 && monNum <= 12) {
        return `${yr}-${pad2(monNum)}-${pad2(dayNum)}`;
      }
    }

    return null;
  }

  function parseTimeString(rawTime) {
    if (!rawTime) return null;
    const m = rawTime.trim().match(/\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?\b/i);
    if (!m) return null;
    let hh = parseInt(m[1], 10);
    const mmNum = parseInt(m[2], 10);
    if (hh > 23 || mmNum > 59) return null;
    const mm = pad2(mmNum);
    const ss = m[3] ? pad2(parseInt(m[3], 10)) : "00";
    const ampm = (m[4] || "").toUpperCase();
    if (ampm === "PM" && hh < 12) hh += 12;
    if (ampm === "AM" && hh === 12) hh = 0;
    return {
      timeStr: `${pad2(hh)}:${mm}:${ss}`,
      minuteKey: `${pad2(hh)}:${mm}`,
      hasSeconds: Boolean(m[3]),
    };
  }

  // ── 1) GraphQL-Interceptor (fetch + XHR, kompatibel mit FFZ / 7TV / BTTV) ─
  function extractTextFromGqlContent(content) {
    if (!content) return "";
    if (typeof content === "string") return normText(content);
    if (typeof content.text === "string" && content.text.length > 0) return normText(content.text);
    if (Array.isArray(content.fragments)) {
      return normText(
        content.fragments
          .map((f) => f?.text || (f?.emote?.token ? ` ${f.emote.token} ` : ""))
          .join("")
      );
    }
    return "";
  }

  function walkGraphqlPayload(node) {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const item of node) walkGraphqlPayload(item);
      return;
    }

    if (typeof node.sentAt === "string" && (node.content !== undefined || typeof node.messageBody === "string")) {
      const text = extractTextFromGqlContent(node.content) || normText(node.messageBody);
      const dt = new Date(node.sentAt);
      if (text && !isNaN(dt.getTime())) {
        const senderLogin = (node.sender?.login || "").toLowerCase();
        const senderDisp = node.sender?.displayName || node.sender?.login || detectedUser || "User";
        if (!detectedUser && senderLogin) {
          detectedUser = senderLogin;
        }
        if (!detectedUser || !senderLogin || senderLogin === detectedUser) {
          const { dateIso, timeStr } = toBerlinParts(dt);
          const key = node.id ? `gql_${node.id}` : `gql_${dt.getTime()}_${text}`;
          if (!collected.has(key)) {
            collected.set(key, {
              id: key,
              tsMs: dt.getTime(),
              dateIso,
              timeStr,
              minuteKey: timeStr.slice(0, 5),
              hasSeconds: true,
              user: senderDisp,
              text,
              source: "gql",
            });
            gqlCount++;
          }
        }
      }
    }

    for (const k of Object.keys(node)) {
      const val = node[k];
      if (val && typeof val === "object") {
        walkGraphqlPayload(val);
      }
    }
  }

  if (!window.__modcheckOrigFetch) {
    window.__modcheckOrigFetch = window.fetch;
    window.fetch = async function (...args) {
      const resp = await window.__modcheckOrigFetch.apply(this, args);
      try {
        const reqUrl = typeof args[0] === "string" ? args[0] : (args[0]?.url || "");
        if (reqUrl.includes("gql.twitch.tv/gql")) {
          resp.clone().json().then((json) => {
            walkGraphqlPayload(json);
            updateHud();
          }).catch(() => {});
        }
      } catch (_) {}
      return resp;
    };
  }

  if (!window.__modcheckOrigXhrOpen) {
    window.__modcheckOrigXhrOpen = XMLHttpRequest.prototype.open;
    window.__modcheckOrigXhrSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.__mcUrl = typeof url === "string" ? url : "";
      return window.__modcheckOrigXhrOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function (...args) {
      if (this.__mcUrl && this.__mcUrl.includes("gql.twitch.tv/gql")) {
        this.addEventListener("load", function () {
          try {
            const json = JSON.parse(this.responseText);
            walkGraphqlPayload(json);
            updateHud();
          } catch (_) {}
        });
      }
      return window.__modcheckOrigXhrSend.apply(this, args);
    };
  }

  // ── 2) DOM-Scraper (Native Twitch + SimpleBar + FFZ / 7TV / BTTV) ────────
  const MSG_SELECTOR = [
    '[data-a-target="chat-line-message"]',
    ".chat-line__message",
    ".vcml-message",
    '[data-test-selector*="message"]',
    ".seventv-chat-message-container",
    ".ffz--chat-line",
  ].join(", ");

  function extractMessageTextWithEmotes(container) {
    let out = "";
    const walker = document.createTreeWalker(
      container,
      NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT,
      null
    );
    let curr = walker.nextNode();
    while (curr) {
      if (curr.nodeType === Node.TEXT_NODE) {
        out += curr.nodeValue;
      } else if (curr.nodeType === Node.ELEMENT_NODE && curr.tagName === "IMG") {
        const alt = curr.getAttribute("alt") || curr.getAttribute("data-emote-name") || "";
        if (alt) out += ` ${alt} `;
      }
      curr = walker.nextNode();
    }
    return normText(out);
  }

  function findViewerCardRoot() {
    return (
      document.querySelector('[data-a-target="viewer-card-mod-logs"]') ||
      document.querySelector('[data-a-target="viewer-card-message-log"]') ||
      document.querySelector(".viewer-card-mod-logs") ||
      document.querySelector(".viewer-card") ||
      document.querySelector('[class*="viewer-card"]') ||
      document.body
    );
  }

  function findScrollableContainer() {
    const root = findViewerCardRoot();
    const candidates = [];

    const all = [root, ...root.querySelectorAll("*")];
    for (const el of all) {
      if (!(el instanceof HTMLElement)) continue;
      const style = window.getComputedStyle(el);
      const oy = style.overflowY;
      const isScrollableCss = oy === "auto" || oy === "scroll" || oy === "overlay";
      const isSimpleBar = el.classList.contains("simplebar-content-wrapper") || el.getAttribute("data-a-target") === "root-scroller";
      if ((isScrollableCss || isSimpleBar) && el.scrollHeight > el.clientHeight + 10) {
        let score = el.scrollHeight - el.clientHeight;
        if (el.closest(".viewer-card") || location.pathname.includes("/viewercard/")) score += 50000;
        if (el.querySelector(MSG_SELECTOR) || el.querySelector('[data-a-target="chat-timestamp"]')) {
          score += 100000;
        }
        candidates.push({ el, score });
      }
    }

    candidates.sort((a, b) => b.score - a.score);
    return candidates.length > 0 ? candidates[0].el : null;
  }

  function scrapeVisibleDom(scrollRoot) {
    const scope = scrollRoot || findViewerCardRoot();
    if (!scope) return 0;

    if (!detectedUser) {
      const headerLink = document.querySelector(
        '[data-a-target="viewer-card-display-name"], .viewer-card a[href^="https://www.twitch.tv/"]'
      );
      if (headerLink && headerLink.textContent.trim()) {
        detectedUser = headerLink.textContent.trim().toLowerCase();
      }
    }

    let currentHeaderDate = null;
    let newlyAdded = 0;

    const nodes = scope.querySelectorAll(
      `h1, h2, h3, h4, p, div[class*="header"], div[class*="date"], span[class*="date"], ${MSG_SELECTOR}`
    );

    nodes.forEach((el, domIdx) => {
      const isMsg = el.matches(MSG_SELECTOR);

      if (!isMsg) {
        if (el.children.length <= 2 && !el.closest(MSG_SELECTOR)) {
          const txt = normText(el.textContent);
          if (txt.length >= 4 && txt.length <= 45) {
            const parsedHeader = parseDateHeader(txt);
            if (parsedHeader) {
              currentHeaderDate = parsedHeader;
            }
          }
        }
        return;
      }

      const tsEl =
        el.querySelector(
          '[data-a-target="chat-timestamp"], .chat-line__timestamp, .vcml-message__timestamp, .seventv-chat-message-timestamp, time'
        ) || null;
      const authorEl =
        el.querySelector(
          '[data-a-target="chat-message-username"], .chat-author__display-name, .seventv-chat-user-username'
        ) || null;
      const bodyEl =
        el.querySelector(
          '[data-a-target="chat-message-text"], .message, .seventv-chat-message-body'
        ) || null;

      let rawTs = tsEl ? normText(tsEl.textContent) : "";
      const isoAttr = tsEl?.getAttribute?.("datetime") || el.getAttribute("data-timestamp") || "";

      let dateIso = currentHeaderDate;
      let timeObj = parseTimeString(rawTs);

      if (isoAttr) {
        const dt = new Date(isoAttr);
        if (!isNaN(dt.getTime())) {
          const bp = toBerlinParts(dt);
          dateIso = bp.dateIso;
          timeObj = { timeStr: bp.timeStr, minuteKey: bp.timeStr.slice(0, 5), hasSeconds: true };
        }
      } else if (rawTs) {
        const inlineDate = parseDateHeader(rawTs);
        if (inlineDate) dateIso = inlineDate;
      }

      const fullLineText = extractMessageTextWithEmotes(el);
      const linePrefix = fullLineText.slice(0, 35);
      if (!timeObj) {
        timeObj = parseTimeString(linePrefix);
      }
      if (!dateIso) {
        dateIso = parseDateHeader(linePrefix) || toBerlinParts(new Date()).dateIso;
      }

      let user = authorEl ? normText(authorEl.textContent) : (detectedUser || "User");
      let msgText = "";
      if (bodyEl) {
        msgText = extractMessageTextWithEmotes(bodyEl);
      } else {
        msgText = fullLineText
          .replace(/^\d{1,2}\.\d{1,2}\.\d{2,4}[,\s-]*/, "")
          .replace(/^\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?\s*/i, "");
        const colonIdx = msgText.indexOf(":");
        if (colonIdx > 0 && colonIdx < 30) {
          user = normText(msgText.slice(0, colonIdx)) || user;
          msgText = normText(msgText.slice(colonIdx + 1));
        } else {
          msgText = normText(msgText);
        }
      }

      if (!msgText) return;
      const timeStr = timeObj ? timeObj.timeStr : "00:00:00";
      const minuteKey = timeObj ? timeObj.minuteKey : "00:00";
      const approxMs = Date.parse(`${dateIso}T${timeStr}`) || (Date.now() + domIdx);

      const domKey = `dom_${dateIso}_${minuteKey}_${user.toLowerCase()}_${msgText}`;
      if (!collected.has(domKey)) {
        collected.set(domKey, {
          id: domKey,
          tsMs: approxMs,
          dateIso,
          timeStr,
          minuteKey,
          hasSeconds: Boolean(timeObj?.hasSeconds),
          user,
          text: msgText,
          source: "dom",
        });
        domCount++;
        newlyAdded++;
      }
    });

    return newlyAdded;
  }

  // ── 3) Deduplizierung, Sortierung & .txt-Download ────────────────────────
  function buildCleanSortedList() {
    const all = Array.from(collected.values());

    const gqlFingerprints = new Set();
    for (const m of all) {
      if (m.source === "gql") {
        gqlFingerprints.add(`${m.dateIso}|${m.minuteKey}|${normText(m.text)}`);
      }
    }

    const filtered = all.filter((m) => {
      if (m.source === "dom" && gqlFingerprints.has(`${m.dateIso}|${m.minuteKey}|${normText(m.text)}`)) {
        return false;
      }
      return true;
    });

    filtered.sort((a, b) => {
      if (a.dateIso !== b.dateIso) return a.dateIso.localeCompare(b.dateIso);
      if (a.timeStr !== b.timeStr) return a.timeStr.localeCompare(b.timeStr);
      return a.tsMs - b.tsMs;
    });

    return filtered;
  }

  function downloadTxtFile() {
    const messages = buildCleanSortedList();
    if (messages.length === 0) {
      alert("Noch keine Chatnachrichten gefunden! Bitte öffne zuerst den Twitch-Nachrichtenverlauf (Viewer Card) der Person.");
      return;
    }

    const uniqueDays = new Set(messages.map((m) => m.dateIso));
    const userLabel = detectedUser || messages[0]?.user || "twitch_user";
    const channelLabel = detectedChannel || "twitch_channel";
    const firstDate = formatIsoToDe(messages[0].dateIso);
    const lastDate = formatIsoToDe(messages[messages.length - 1].dateIso);

    const headerLines = [
      "====================================================================",
      "TWITCH CHAT-VERLAUF EXPORT",
      `User:               ${userLabel}`,
      `Kanal:              ${channelLabel}`,
      `Zeitraum:           ${firstDate} bis ${lastDate}`,
      `Aktive Chat-Tage:   ${uniqueDays.size}`,
      `Nachrichten gesamt: ${messages.length}`,
      `Exportiert am:      ${new Date().toLocaleString("de-DE")}`,
      "====================================================================",
      "",
    ];

    let currentDay = "";
    const bodyLines = [];
    for (const m of messages) {
      if (m.dateIso !== currentDay) {
        currentDay = m.dateIso;
        bodyLines.push(`--- ${formatIsoToDe(currentDay)} ---`);
      }
      const dispTime = m.hasSeconds ? m.timeStr : m.minuteKey;
      bodyLines.push(`[${formatIsoToDe(m.dateIso)} ${dispTime}] ${m.user}: ${m.text}`);
    }

    const fullContent = headerLines.concat(bodyLines).join("\r\n") + "\r\n";
    const blob = new Blob([fullContent], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);

    const safeUser = userLabel.replace(/[^a-z0-9_-]/gi, "_");
    const todayIso = toBerlinParts(new Date()).dateIso;
    const filename = `twitch_chat_${safeUser}_${todayIso}.txt`;

    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 1500);
  }

  // ── 4) Schwebendes Live-HUD im Browser ───────────────────────────────────
  const oldHud = document.getElementById("modcheck-exporter-hud");
  if (oldHud) oldHud.remove();

  const hud = document.createElement("div");
  hud.id = "modcheck-exporter-hud";
  hud.style.cssText = [
    "position:fixed",
    "top:16px",
    "right:16px",
    "z-index:2147483647",
    "width:335px",
    "background:#18181b",
    "color:#efeff1",
    "border:2px solid #9147ff",
    "border-radius:12px",
    "padding:14px 16px",
    "font-family:Inter,Roobert,Helvetica,Arial,sans-serif",
    "font-size:13px",
    "box-shadow:0 12px 32px rgba(0,0,0,0.65)",
  ].join(";");

  hud.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <strong style="color:#bf94ff;font-size:14px">🟣 Modcheck Chat-Exporter</strong>
      <button id="mc-exp-close" title="Schließen" style="background:none;border:none;color:#adadb8;cursor:pointer;font-size:16px">✕</button>
    </div>
    <div id="mc-exp-status" style="color:#00f593;font-weight:600;margin-bottom:6px">🔍 Suche Nachrichtenverlauf...</div>
    <div id="mc-exp-stats" style="color:#adadb8;line-height:1.5;margin-bottom:12px">
      Gesammelt: <b>0</b> Nachrichten
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button id="mc-exp-pause" style="flex:1;background:#3a3a3d;color:#fff;border:none;border-radius:6px;padding:7px 10px;cursor:pointer;font-weight:600">⏸️ Pause</button>
      <button id="mc-exp-save" style="flex:2;background:#9147ff;color:#fff;border:none;border-radius:6px;padding:7px 10px;cursor:pointer;font-weight:600">💾 Jetzt .txt speichern</button>
    </div>
  `;
  document.body.appendChild(hud);

  const statusEl = hud.querySelector("#mc-exp-status");
  const statsEl = hud.querySelector("#mc-exp-stats");
  const pauseBtn = hud.querySelector("#mc-exp-pause");
  const saveBtn = hud.querySelector("#mc-exp-save");
  const closeBtn = hud.querySelector("#mc-exp-close");

  function updateHud(statusText) {
    if (statusText) statusEl.textContent = statusText;
    const list = buildCleanSortedList();
    const oldest = list.length > 0 ? formatIsoToDe(list[0].dateIso) : "–";
    const newest = list.length > 0 ? formatIsoToDe(list[list.length - 1].dateIso) : "–";
    const days = new Set(list.map((m) => m.dateIso)).size;
    statsEl.innerHTML =
      `Gesammelt: <b style="color:#fff">${list.length.toLocaleString("de-DE")}</b> Nachrichten (${days} Tage)<br>` +
      `Zeitraum: <b style="color:#fff">${oldest}</b> – <b style="color:#fff">${newest}</b><br>` +
      `<span style="font-size:11px;opacity:.8">Quelle: ${gqlCount} exakt (GQL) • ${domCount} sichtbar (DOM)</span>`;
  }

  pauseBtn.addEventListener("click", () => {
    isPaused = !isPaused;
    pauseBtn.textContent = isPaused ? "▶️ Weiter" : "⏸️ Pause";
    updateHud(isPaused ? "⏸️ Pausiert" : "⏫ Scrolle weiter nach oben...");
  });

  saveBtn.addEventListener("click", () => {
    stopRequested = true;
    window.__modcheckExporterRunning = false;
    downloadTxtFile();
    updateHud("✅ .txt-Datei heruntergeladen!");
  });

  closeBtn.addEventListener("click", () => {
    stopRequested = true;
    window.__modcheckExporterRunning = false;
    hud.remove();
  });

  // ── 5) Automatischer Hochscroll-Loop bis zum allerersten Chat-Eintrag ────
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function runAutoScroll() {
    let scroller = findScrollableContainer();
    scrapeVisibleDom(scroller);
    updateHud();

    if (!scroller) {
      updateHud("⚠️ Bitte Zuschauerkarte (Nachrichtenverlauf) öffnen!");
      while (!scroller && !stopRequested) {
        await sleep(1000);
        scroller = findScrollableContainer();
      }
    }
    if (stopRequested || !scroller) return;

    let idleRounds = 0;
    let lastTotal = -1;
    let lastScrollHeight = scroller.scrollHeight;
    let step = 0;

    updateHud("⏫ Scrolle automatisch bis ganz nach oben...");

    while (!stopRequested) {
      if (isPaused) {
        await sleep(400);
        continue;
      }

      if (!scroller.isConnected) {
        scroller = findScrollableContainer();
        if (!scroller) {
          await sleep(600);
          continue;
        }
      }

      scrapeVisibleDom(scroller);

      // Falls schon auf 0: kurz runterwippen, damit Twitchs IntersectionObserver am oberen Rand neu auslöst
      if (scroller.scrollTop <= 5) {
        scroller.scrollTop = Math.min(scroller.clientHeight + 250, scroller.scrollHeight);
        scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
        await sleep(160);
      }

      scroller.scrollTop = 0;
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
      scroller.dispatchEvent(
        new WheelEvent("wheel", { deltaY: -1200, bubbles: true, cancelable: true })
      );

      step++;
      await sleep(750);
      scrapeVisibleDom(scroller);

      const currentTotal = buildCleanSortedList().length;
      const currentHeight = scroller.scrollHeight;
      updateHud(`⏫ Scrolle nach oben... (Schritt ${step})`);

      if (currentTotal === lastTotal && Math.abs(currentHeight - lastScrollHeight) < 5) {
        idleRounds++;
        if (idleRounds >= 10) {
          break;
        }
      } else {
        idleRounds = 0;
        lastTotal = currentTotal;
        lastScrollHeight = currentHeight;
      }
    }

    if (!stopRequested) {
      scrapeVisibleDom(scroller);
      updateHud("✅ Ganz oben angekommen! Speichere .txt...");
      window.__modcheckExporterRunning = false;
      downloadTxtFile();
    }
  }

  runAutoScroll();
})();
