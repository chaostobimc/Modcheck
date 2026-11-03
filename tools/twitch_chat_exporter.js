// ==UserScript==
// @name         Twitch Viewer Card Chat-Verlauf Exporter (.txt) — TURBO v2.0
// @namespace    https://github.com/chaostobimc/Modcheck
// @version      2.0.0
// @description  Ultra-schneller Twitch Chat-Verlauf Exporter: Entfernt allen unnötigen Website-Ballast (ungestylter Roh-Modus) und lädt ältere Nachrichten per direkter Twitch-GQL-Turbo-Pagination (10x–50x schneller als normales Scrollen) als .txt herunter.
// @match        https://www.twitch.tv/popout/*/viewercard/*
// @match        https://www.twitch.tv/*
// @grant        none
// ==/UserScript==

/**
 * TURBO-ANLEITUNG (v2.0):
 * 1. Öffne die Zuschauerkarte der Person (am schnellsten direkt als eigenes Popout-Tab):
 *      https://www.twitch.tv/popout/DEIN_KANAL/viewercard/TWITCH_USERNAME
 * 2. Drücke F12 -> Reiter "Konsole" (Console), füge dieses Skript ein und drücke Enter.
 * 3. Was v2.0 automatisch für maximale Geschwindigkeit macht:
 *    - ⚡ UNGESTYLTER ROH-MODUS: Stoppt & entfernt sofort Video-Player, Live-Chat, Sidebars,
 *      Bilder, Emotes, SVGs, Animationen und schaltet das komplette Twitch-CSS ab.
 *    - 🧹 DOM-BREMSE GELÖST: Alte DOM-Zeilen werden nach dem Auslesen ausgeblendet, damit
 *      der Browser bei tausenden Nachrichten nicht mehr einfriert.
 *    - 🚀 DIREKTER GQL-TURBO: Sobald die erste Nachlade-Anfrage erkannt wird, fragt das Skript
 *      die Twitch-GraphQL-API direkt per Cursor im Hintergrund ab (versucht automatisch 100er-Pakete
 *      statt 25er-Schritte, komplett ohne langsames UI-Scrollen!).
 */

(function () {
  "use strict";

  if (window.__modcheckExporterRunning) {
    alert("Der Twitch Turbo-Exporter läuft bereits! Nutze das Panel oben rechts.");
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
  let liteModeActive = false;
  let turboActive = false;
  const startTimeMs = Date.now();

  let capturedGqlTemplate = null;
  let latestCapturedConn = null;
  let gqlWaiters = [];

  function notifyGqlWaiters() {
    const list = gqlWaiters;
    gqlWaiters = [];
    for (const resolve of list) resolve();
  }

  function waitForGqlOrTimeout(ms) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (!done) {
          done = true;
          resolve();
        }
      };
      gqlWaiters.push(finish);
      setTimeout(finish, ms);
    });
  }

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

  const pad2 = (n) => String(n).padStart(2, "0");
  const normText = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  let berlinFmt = null;
  try {
    berlinFmt = new Intl.DateTimeFormat("de-DE", {
      timeZone: "Europe/Berlin",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch (_) {}

  function toBerlinParts(dateObj) {
    if (berlinFmt) {
      const parts = {};
      for (const p of berlinFmt.formatToParts(dateObj)) {
        parts[p.type] = p.value;
      }
      const dateIso = `${parts.year}-${parts.month}-${parts.day}`;
      const h = parts.hour === "24" ? "00" : parts.hour;
      return { dateIso, timeStr: `${h}:${parts.minute}:${parts.second}` };
    }
    const y = dateObj.getFullYear();
    const m = pad2(dateObj.getMonth() + 1);
    const d = pad2(dateObj.getDate());
    return {
      dateIso: `${y}-${m}-${d}`,
      timeStr: `${pad2(dateObj.getHours())}:${pad2(dateObj.getMinutes())}:${pad2(dateObj.getSeconds())}`,
    };
  }

  function formatIsoToDe(dateIso) {
    if (!dateIso || dateIso.length < 10) return dateIso || "–";
    const [y, m, d] = dateIso.slice(0, 10).split("-");
    return `${d}.${m}.${y}`;
  }

  function parseDateHeader(rawText) {
    if (!rawText) return null;
    const s = normText(rawText);
    const low = s.toLowerCase();
    const now = new Date();

    if (low === "heute" || low === "today") return toBerlinParts(now).dateIso;
    if (low === "gestern" || low === "yesterday") return toBerlinParts(new Date(now.getTime() - 86400000)).dateIso;

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

    m = low.match(/(\d{1,2})\.?\s+([a-zäöü]+)\.?(?:\s+(\d{4}))?/i);
    if (m) {
      const monNum = MONTHS_MAP[m[2].replace(/\.$/, "")];
      const dayNum = parseInt(m[1], 10);
      if (monNum && dayNum >= 1 && dayNum <= 31) {
        const yr = m[3] ? parseInt(m[3], 10) : now.getFullYear();
        return `${yr}-${pad2(monNum)}-${pad2(dayNum)}`;
      }
    }

    m = low.match(/([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(\d{4}))?/i);
    if (m) {
      const monNum = MONTHS_MAP[m[1].replace(/\.$/, "")];
      const dayNum = parseInt(m[2], 10);
      if (monNum && dayNum >= 1 && dayNum <= 31) {
        const yr = m[3] ? parseInt(m[3], 10) : now.getFullYear();
        return `${yr}-${pad2(monNum)}-${pad2(dayNum)}`;
      }
    }

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

  // ── 1) GraphQL-Interceptor + Vorlagen-Extraktor für Direkt-API-Turbo ─────
  function normalizeHeaders(rawHeaders) {
    const out = {};
    if (!rawHeaders) return out;
    if (typeof Headers !== "undefined" && rawHeaders instanceof Headers) {
      rawHeaders.forEach((v, k) => {
        out[k] = v;
      });
    } else if (Array.isArray(rawHeaders)) {
      for (const pair of rawHeaders) {
        if (pair && pair.length >= 2) out[pair[0]] = pair[1];
      }
    } else if (typeof rawHeaders === "object") {
      for (const k of Object.keys(rawHeaders)) {
        if (rawHeaders[k] !== undefined) out[k] = String(rawHeaders[k]);
      }
    }
    return out;
  }

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

  function recordGqlMessageNode(msgNode) {
    if (!msgNode || typeof msgNode !== "object") return false;
    if (typeof msgNode.sentAt !== "string") return false;
    if (msgNode.content === undefined && typeof msgNode.messageBody !== "string") return false;

    const text = extractTextFromGqlContent(msgNode.content) || normText(msgNode.messageBody);
    const dt = new Date(msgNode.sentAt);
    if (!text || isNaN(dt.getTime())) return false;

    const senderLogin = (msgNode.sender?.login || "").toLowerCase();
    const senderDisp = msgNode.sender?.displayName || msgNode.sender?.login || detectedUser || "User";
    if (!detectedUser && senderLogin) {
      detectedUser = senderLogin;
    }
    if (detectedUser && senderLogin && senderLogin !== detectedUser) {
      return false;
    }

    const { dateIso, timeStr } = toBerlinParts(dt);
    const key = msgNode.id ? `gql_${msgNode.id}` : `gql_${dt.getTime()}_${text}`;
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
      return true;
    }
    return false;
  }

  function walkGraphqlPayload(node) {
    let added = 0;
    const stack = [node];
    while (stack.length > 0) {
      const curr = stack.pop();
      if (!curr || typeof curr !== "object") continue;
      if (Array.isArray(curr)) {
        for (let i = curr.length - 1; i >= 0; i--) stack.push(curr[i]);
        continue;
      }
      if (recordGqlMessageNode(curr)) {
        added++;
      }
      for (const k of Object.keys(curr)) {
        const val = curr[k];
        if (val && typeof val === "object") stack.push(val);
      }
    }
    return added;
  }

  function findMessageConnection(obj, opNameHint) {
    if (!obj || typeof obj !== "object") return null;
    const isLikelyModLogsOp = /MessagesBySender|ModLogsMessages|UserModLogs|ViewerCardModLogs/i.test(opNameHint || "");
    const stack = [obj];
    let fallbackConn = null;

    while (stack.length > 0) {
      const curr = stack.pop();
      if (!curr || typeof curr !== "object") continue;
      if (Array.isArray(curr)) {
        for (const item of curr) stack.push(item);
        continue;
      }
      if (Array.isArray(curr.edges)) {
        const hasMsgEdge = curr.edges.some((e) => {
          const n = e?.node?.message || e?.node;
          return n && (typeof n.sentAt === "string" || n.content !== undefined || typeof n.messageBody === "string");
        });
        if (hasMsgEdge) return curr;
        if (isLikelyModLogsOp && (curr.pageInfo || curr.__typename?.includes("Message"))) {
          fallbackConn = curr;
        }
      }
      for (const k of Object.keys(curr)) {
        if (curr[k] && typeof curr[k] === "object") stack.push(curr[k]);
      }
    }
    return fallbackConn;
  }

  function inspectGqlExchange(url, headersObj, credentialsMode, reqBodyStr, resJson) {
    walkGraphqlPayload(resJson);

    if (reqBodyStr) {
      try {
        const reqParsed = JSON.parse(reqBodyStr);
        if (Array.isArray(reqParsed) && Array.isArray(resJson)) {
          for (let i = 0; i < reqParsed.length; i++) {
            const reqOp = reqParsed[i];
            const resOp = resJson[i];
            const opName = reqOp?.operationName || resOp?.extensions?.operationName || "";
            const conn = findMessageConnection(resOp?.data, opName);
            if (conn && reqOp?.variables) {
              capturedGqlTemplate = {
                url,
                headers: headersObj,
                credentials: credentialsMode,
                isBatchArray: true,
                opTemplate: JSON.parse(JSON.stringify(reqOp)),
              };
              latestCapturedConn = conn;
              break;
            }
          }
        } else if (reqParsed && typeof reqParsed === "object" && !Array.isArray(reqParsed)) {
          const opName = reqParsed.operationName || resJson?.extensions?.operationName || "";
          const conn = findMessageConnection(resJson?.data, opName);
          if (conn && reqParsed.variables) {
            capturedGqlTemplate = {
              url,
              headers: headersObj,
              credentials: credentialsMode,
              isBatchArray: false,
              opTemplate: JSON.parse(JSON.stringify(reqParsed)),
            };
            latestCapturedConn = conn;
          }
        }
      } catch (_) {}
    }

    notifyGqlWaiters();
    updateHud();
  }

  // Hook window.fetch
  if (!window.__modcheckOrigFetch) {
    window.__modcheckOrigFetch = window.fetch.bind(window);
  }
  window.fetch = async function (...args) {
    let reqUrl = "";
    let headersObj = {};
    let credentialsMode = undefined;
    let reqBodyPromise = Promise.resolve(null);

    try {
      const input = args[0];
      const init = args[1];
      if (typeof input === "string") {
        reqUrl = input;
        headersObj = normalizeHeaders(init?.headers);
        credentialsMode = init?.credentials;
        if (typeof init?.body === "string") reqBodyPromise = Promise.resolve(init.body);
      } else if (input && typeof input === "object") {
        reqUrl = input.url || "";
        headersObj = Object.assign(normalizeHeaders(input.headers), normalizeHeaders(init?.headers));
        credentialsMode = init?.credentials || input.credentials;
        if (typeof init?.body === "string") {
          reqBodyPromise = Promise.resolve(init.body);
        } else if (typeof input.clone === "function") {
          reqBodyPromise = input.clone().text().catch(() => null);
        }
      }
    } catch (_) {}

    const resp = await window.__modcheckOrigFetch(...args);
    if (reqUrl.includes("gql.twitch.tv/gql")) {
      Promise.all([reqBodyPromise, resp.clone().json().catch(() => null)]).then(([bodyStr, json]) => {
        if (json) inspectGqlExchange(reqUrl, headersObj, credentialsMode, bodyStr, json);
      });
    }
    return resp;
  };

  // Hook XMLHttpRequest (für FFZ / 7TV / BTTV)
  if (!window.__modcheckOrigXhrOpen) {
    window.__modcheckOrigXhrOpen = XMLHttpRequest.prototype.open;
    window.__modcheckOrigXhrSend = XMLHttpRequest.prototype.send;
    window.__modcheckOrigXhrSetHeader = XMLHttpRequest.prototype.setRequestHeader;

    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.__mcUrl = typeof url === "string" ? url : "";
      this.__mcHeaders = {};
      return window.__modcheckOrigXhrOpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
      if (this.__mcHeaders) this.__mcHeaders[k] = v;
      return window.__modcheckOrigXhrSetHeader.call(this, k, v);
    };
    XMLHttpRequest.prototype.send = function (body, ...rest) {
      if (this.__mcUrl && this.__mcUrl.includes("gql.twitch.tv/gql")) {
        const bodyStr = typeof body === "string" ? body : null;
        const hdrs = Object.assign({}, this.__mcHeaders || {});
        const creds = this.withCredentials ? "include" : undefined;
        this.addEventListener("load", function () {
          try {
            const json = JSON.parse(this.responseText);
            inspectGqlExchange(this.__mcUrl, hdrs, creds, bodyStr, json);
          } catch (_) {}
        });
      }
      return window.__modcheckOrigXhrSend.call(this, body, ...rest);
    };
  }

  // ── 2) ULTRA-LITE ROH-MODUS: Entfernt Video, Chat, Bilder & Twitch-CSS ───
  const MSG_SELECTOR = [
    '[data-a-target="chat-line-message"]',
    ".chat-line__message",
    ".vcml-message",
    '[data-test-selector*="message"]',
    ".seventv-chat-message-container",
    ".ffz--chat-line",
  ].join(", ");

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
      if (el.id === "modcheck-exporter-hud") continue;
      const style = window.getComputedStyle(el);
      const oy = style.overflowY;
      const isScrollableCss = oy === "auto" || oy === "scroll" || oy === "overlay";
      const isSimpleBar =
        el.classList.contains("simplebar-content-wrapper") ||
        el.getAttribute("data-a-target") === "root-scroller";
      if ((isScrollableCss || isSimpleBar) && el.scrollHeight > el.clientHeight + 5) {
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

  function killHeavyMediaAndBloat(scroller) {
    const keepRoot = scroller || findViewerCardRoot();

    // 1. Alle Videos & Audio-Streams sofort stoppen & entladen
    document.querySelectorAll("video, audio").forEach((media) => {
      try {
        media.pause();
        media.removeAttribute("src");
        media.load();
        media.remove();
      } catch (_) {}
    });

    // 2. Unnötige Groß-Container (Player, Sidebars, Top-Nav, Extension-Iframes, Live-Chat) entfernen,
    //    sofern sie NICHT die geöffnete Viewer-Card enthalten!
    const bloatSelectors = [
      '[data-a-target="video-player"]',
      ".video-player",
      ".persistent-player",
      '[data-a-target="top-nav-container"]',
      ".top-nav",
      '[data-a-target="side-nav-bar"]',
      ".side-nav",
      "iframe",
      "canvas",
      ".channel-info-content",
      '[data-a-target="chat-scroller"]',
    ];
    document.querySelectorAll(bloatSelectors.join(", ")).forEach((el) => {
      if (keepRoot && (el === keepRoot || el.contains(keepRoot))) return;
      if (el.closest("#modcheck-exporter-hud")) return;
      el.remove();
    });
  }

  function enableUltraLiteMode(scroller) {
    if (liteModeActive) return;
    liteModeActive = true;

    // Scroller-Höhe per Inline-Style fixieren, damit er auch ohne Twitch-CSS scrollbar bleibt!
    if (scroller instanceof HTMLElement) {
      scroller.style.setProperty("height", "45vh", "important");
      scroller.style.setProperty("max-height", "45vh", "important");
      scroller.style.setProperty("overflow-y", "scroll", "important");
      scroller.style.setProperty("display", "block", "important");
      scroller.style.setProperty("border", "1px solid #333", "important");
      scroller.style.setProperty("padding", "8px", "important");
    }

    killHeavyMediaAndBloat(scroller);

    // Alle Twitch-Stylesheets deaktivieren -> komplett ungestylte, pfeilschnelle Roh-Seite!
    document.querySelectorAll('link[rel="stylesheet"], style:not(#mc-lite-style)').forEach((node) => {
      try {
        node.disabled = true;
        node.dataset.mcDisabled = "1";
      } catch (_) {}
    });

    let liteStyle = document.getElementById("mc-lite-style");
    if (!liteStyle) {
      liteStyle = document.createElement("style");
      liteStyle.id = "mc-lite-style";
      liteStyle.textContent = `
        * {
          animation: none !important;
          transition: none !important;
          box-shadow: none !important;
          text-shadow: none !important;
          filter: none !important;
          backdrop-filter: none !important;
          background-image: none !important;
        }
        html, body {
          background: #0e0e10 !important;
          color: #d8d8df !important;
          font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace !important;
          font-size: 12px !important;
          margin: 0 !important;
          padding: 6px !important;
        }
        img, svg, picture, video, audio, iframe, canvas {
          display: none !important;
        }
        a { color: #a970ff !important; text-decoration: none !important; }
        button { background: #26262c; color: #fff; border: 1px solid #444; padding: 3px 6px; }
        #modcheck-exporter-hud, #modcheck-exporter-hud * {
          font-family: Inter, system-ui, -apple-system, Segoe UI, Arial, sans-serif !important;
        }
      `;
      document.head.appendChild(liteStyle);
    }
  }

  function disableUltraLiteMode() {
    if (!liteModeActive) return;
    liteModeActive = false;
    document.querySelectorAll('[data-mc-disabled="1"]').forEach((node) => {
      try {
        node.disabled = false;
      } catch (_) {}
    });
    const liteStyle = document.getElementById("mc-lite-style");
    if (liteStyle) liteStyle.remove();
  }

  // ── 3) Inkrementeller DOM-Scraper + DOM-Pruning (verhindert Scroll-Stau) ─
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

  function scrapeVisibleDom(scrollRoot, pruneOldDomRows = false) {
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
    let msgRowOrdinal = 0;

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
            if (parsedHeader) currentHeaderDate = parsedHeader;
          }
        }
        return;
      }

      msgRowOrdinal++;
      // Behalte nur die obersten ~35 Zeilen sichtbar für den Scroll-Balken;
      // blende alle darunter liegenden aus -> verhindert das Einfrieren bei tausenden Zeilen!
      const shouldHideThisRow = pruneOldDomRows && msgRowOrdinal > 35;

      if (el.__mcScraped) {
        if (shouldHideThisRow && el.style.display !== "none") {
          el.style.display = "none";
        }
        return;
      }
      el.__mcScraped = true;

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

      const rawTs = tsEl ? normText(tsEl.textContent) : "";
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
      if (!timeObj) timeObj = parseTimeString(linePrefix);
      if (!dateIso) dateIso = parseDateHeader(linePrefix) || toBerlinParts(new Date()).dateIso;

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

      // Emote-Bilder entladen, damit kein Netzwerk/RAM verschwendet wird
      el.querySelectorAll("img").forEach((img) => img.removeAttribute("src"));
      if (shouldHideThisRow) {
        el.style.display = "none";
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

  // ── 4) DIREKTE GQL-TURBO-ENGINE (Lädt Seiten direkt ohne UI-Scrollen!) ───
  function extractConnMeta(conn) {
    const edges = Array.isArray(conn?.edges) ? conn.edges : [];
    const firstCursor = edges[0]?.cursor || conn?.pageInfo?.startCursor || null;
    const lastCursor = edges[edges.length - 1]?.cursor || conn?.pageInfo?.endCursor || null;

    let firstSentMs = null;
    let lastSentMs = null;
    for (let i = 0; i < edges.length; i++) {
      const s = (edges[i]?.node?.message || edges[i]?.node)?.sentAt;
      if (s) {
        const ms = Date.parse(s);
        if (!isNaN(ms)) {
          if (firstSentMs === null) firstSentMs = ms;
          lastSentMs = ms;
        }
      }
    }

    const pi = conn?.pageInfo;
    const explicitlyDone =
      edges.length === 0 ||
      Boolean(pi && pi.hasNextPage === false && pi.hasPreviousPage !== true);

    return { edges, firstCursor, lastCursor, firstSentMs, lastSentMs, explicitlyDone };
  }

  async function sendDirectGqlRequest(template, customVars) {
    const op = JSON.parse(JSON.stringify(template.opTemplate));
    op.variables = customVars;
    const payload = template.isBatchArray ? [op] : op;

    const fetchHeaders = Object.assign({}, template.headers, {
      "Content-Type": "application/json",
    });

    const fetchOpts = {
      method: "POST",
      headers: fetchHeaders,
      body: JSON.stringify(payload),
    };
    if (template.credentials) {
      fetchOpts.credentials = template.credentials;
    }

    const resp = await window.__modcheckOrigFetch(template.url, fetchOpts);

    if (resp.status === 429) {
      return { rateLimited: true };
    }
    if (!resp.ok) {
      return { httpError: resp.status };
    }

    const json = await resp.json();
    const singleRes = Array.isArray(json) ? json[0] : json;
    if (singleRes?.errors && !singleRes?.data) {
      return { gqlError: singleRes.errors };
    }

    const added = walkGraphqlPayload(singleRes);
    const conn = findMessageConnection(singleRes?.data, op.operationName || "");
    if (!conn && singleRes?.errors) {
      return { gqlError: singleRes.errors };
    }
    return { conn, added, raw: singleRes };
  }

  async function runDirectGqlTurbo(template, initialConn) {
    turboActive = true;
    const baseVars = JSON.parse(JSON.stringify(template.opTemplate.variables || {}));

    let cursorKey = "cursor";
    if ("after" in baseVars) cursorKey = "after";
    else if ("before" in baseVars) cursorKey = "before";
    else if ("cursor" in baseVars) cursorKey = "cursor";

    // Teste automatisch 100er-Batchgröße (4x weniger Requests als Twitchs Standard 25!),
    // mit sofortigem Fallback auf Original-Variablen falls die Query kein 100er-Limit unterstützt.
    const sizeKey = "limit" in baseVars ? "limit" : "first";
    let tryBoost100 = !(sizeKey in baseVars) || (typeof baseVars[sizeKey] === "number" && baseVars[sizeKey] < 100);

    let currentConn = initialConn;
    const visitedCursors = new Set();

    // 1. Falls wir erst ab Seite 2+ mitgeschnitten haben: Seite 1 per GQL holen und lückenlos ab Seite 1 durchlaufen!
    if (baseVars[cursorKey]) {
      try {
        const page1Vars = Object.assign({}, baseVars);
        delete page1Vars[cursorKey];
        if (tryBoost100) page1Vars[sizeKey] = 100;

        let p1 = await sendDirectGqlRequest(template, page1Vars);
        if ((p1.gqlError || !p1.conn) && tryBoost100) {
          tryBoost100 = false;
          const retryVars = Object.assign({}, baseVars);
          delete retryVars[cursorKey];
          p1 = await sendDirectGqlRequest(template, retryVars);
        }
        if (p1?.conn && Array.isArray(p1.conn.edges) && p1.conn.edges.length > 0) {
          currentConn = p1.conn;
        } else {
          visitedCursors.add(baseVars[cursorKey]);
        }
      } catch (_) {
        visitedCursors.add(baseVars[cursorKey]);
      }
    }

    let pageNum = 1;
    let consecutiveErrors = 0;

    while (!stopRequested && currentConn) {
      while (isPaused && !stopRequested) {
        await sleep(250);
      }
      if (stopRequested) break;

      const meta = extractConnMeta(currentConn);
      if (meta.explicitlyDone) {
        turboActive = false;
        return { completedAll: true };
      }

      const preferFirst =
        meta.firstSentMs !== null &&
        meta.lastSentMs !== null &&
        meta.firstSentMs < meta.lastSentMs;

      const primaryCursor = preferFirst ? meta.firstCursor : meta.lastCursor;
      const secondaryCursor = preferFirst ? meta.lastCursor : meta.firstCursor;

      let nextCursor = primaryCursor && !visitedCursors.has(primaryCursor) ? primaryCursor : null;
      if (!nextCursor && secondaryCursor && !visitedCursors.has(secondaryCursor)) {
        nextCursor = secondaryCursor;
      }
      if (!nextCursor) {
        turboActive = false;
        return { completedAll: true };
      }

      visitedCursors.add(nextCursor);
      const nextVars = Object.assign({}, baseVars, { [cursorKey]: nextCursor });
      if (tryBoost100) nextVars[sizeKey] = 100;

      let res;
      try {
        res = await sendDirectGqlRequest(template, nextVars);
      } catch (_) {
        res = { httpError: 0 };
      }

      // Falls Twitch 100er-Batchgröße ablehnt -> sofort mit Original-Größe weitermachen
      if ((res.gqlError || (!res.conn && !res.rateLimited && !res.httpError)) && tryBoost100) {
        tryBoost100 = false;
        visitedCursors.delete(nextCursor);
        continue;
      }

      if (res.rateLimited) {
        updateHud("⏳ Kurze Twitch-Pause (Rate-Limit), geht gleich weiter...", true);
        await sleep(800);
        visitedCursors.delete(nextCursor);
        continue;
      }

      if (res.httpError || res.gqlError || !res.conn) {
        consecutiveErrors++;
        if (consecutiveErrors >= 3) {
          // Z. B. Client-Integrity abgelaufen -> kurz per UI-Scroll neuen Token triggern!
          turboActive = false;
          capturedGqlTemplate = null;
          return { completedAll: false };
        }
        await sleep(400);
        visitedCursors.delete(nextCursor);
        continue;
      }

      consecutiveErrors = 0;
      pageNum++;
      currentConn = res.conn;
      updateHud(`🚀 TURBO-API aktiv (Seite ${pageNum}${tryBoost100 ? " • 100er-Boost" : ""})`);

      // Nur 10ms Pause – nutzt HTTP/2 Keep-Alive mit maximaler Geschwindigkeit!
      await sleep(10);
    }

    turboActive = false;
    return { completedAll: !stopRequested };
  }

  // ── 5) Deduplizierung, Sortierung & .txt-Download ────────────────────────
  function buildCleanSortedList() {
    const all = Array.from(collected.values());
    const gqlDays = new Set();
    const gqlFingerprints = new Set();
    for (const m of all) {
      if (m.source === "gql") {
        gqlDays.add(m.dateIso);
        gqlFingerprints.add(`${m.dateIso}|${m.minuteKey}|${normText(m.text)}`);
      }
    }

    const filtered = all.filter((m) => {
      if (m.source === "dom") {
        // Header-Artefakte ("[Datum 00:00] user: user") immer verwerfen
        if (!m.hasSeconds && m.minuteKey === "00:00" && normText(m.text).toLowerCase() === normText(m.user).toLowerCase()) {
          return false;
        }
        // Wenn der Tag bereits vollständig per GQL (inkl. Seite-1-Backfill) geladen wurde, DOM-Duplikate ignorieren
        if (gqlDays.has(m.dateIso) || gqlFingerprints.has(`${m.dateIso}|${m.minuteKey}|${normText(m.text)}`)) {
          return false;
        }
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
      alert("Noch keine Chatnachrichten gefunden! Bitte öffne die Zuschauerkarte (Nachrichtenverlauf) der Person.");
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

  // ── 6) Schwebendes Turbo-HUD im Browser ──────────────────────────────────
  const oldHud = document.getElementById("modcheck-exporter-hud");
  if (oldHud) oldHud.remove();

  const hud = document.createElement("div");
  hud.id = "modcheck-exporter-hud";
  hud.style.cssText = [
    "position:fixed",
    "top:14px",
    "right:14px",
    "z-index:2147483647",
    "width:355px",
    "background:#18181b",
    "color:#efeff1",
    "border:2px solid #9147ff",
    "border-radius:12px",
    "padding:14px 16px",
    "font-size:13px",
    "box-shadow:0 10px 28px rgba(0,0,0,0.8)",
  ].join(";");

  hud.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <strong style="color:#bf94ff;font-size:14px">⚡ Modcheck TURBO-Exporter v2.0</strong>
      <button id="mc-exp-close" title="Schließen & CSS wiederherstellen" style="background:none;border:none;color:#adadb8;cursor:pointer;font-size:16px">✕</button>
    </div>
    <div id="mc-exp-status" style="color:#00f593;font-weight:700;margin-bottom:6px">⚡ Initialisiere Roh-Modus & Turbo...</div>
    <div id="mc-exp-stats" style="color:#adadb8;line-height:1.55;margin-bottom:12px">
      Gesammelt: <b>0</b> Nachrichten
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">
      <button id="mc-exp-pause" style="flex:1;background:#3a3a3d;color:#fff;border:none;border-radius:6px;padding:7px 8px;cursor:pointer;font-weight:600">⏸️ Pause</button>
      <button id="mc-exp-lite" style="flex:1;background:#265c42;color:#fff;border:none;border-radius:6px;padding:7px 8px;cursor:pointer;font-weight:600">⚡ Roh-UI: AN</button>
    </div>
    <button id="mc-exp-save" style="width:100%;background:#9147ff;color:#fff;border:none;border-radius:6px;padding:8px 10px;cursor:pointer;font-weight:700">💾 Jetzt .txt speichern</button>
  `;
  document.body.appendChild(hud);

  const statusEl = hud.querySelector("#mc-exp-status");
  const statsEl = hud.querySelector("#mc-exp-stats");
  const pauseBtn = hud.querySelector("#mc-exp-pause");
  const liteBtn = hud.querySelector("#mc-exp-lite");
  const saveBtn = hud.querySelector("#mc-exp-save");
  const closeBtn = hud.querySelector("#mc-exp-close");

  let lastHudRenderMs = 0;
  function updateHud(statusText, force = false) {
    if (statusText) statusEl.textContent = statusText;
    const now = Date.now();
    if (!force && now - lastHudRenderMs < 150) return;
    lastHudRenderMs = now;

    const elapsedSec = Math.max(0.5, (now - startTimeMs) / 1000);
    const speed = Math.round(collected.size / elapsedSec);

    let oldestIso = null;
    let newestIso = null;
    for (const m of collected.values()) {
      if (!oldestIso || m.dateIso < oldestIso) oldestIso = m.dateIso;
      if (!newestIso || m.dateIso > newestIso) newestIso = m.dateIso;
    }

    statsEl.innerHTML =
      `Gesammelt: <b style="color:#fff">${collected.size.toLocaleString("de-DE")}</b> Nachrichten ` +
      `<span style="color:#00f593;font-weight:600">(~${speed}/Sek.)</span><br>` +
      `Zeitraum: <b style="color:#fff">${oldestIso ? formatIsoToDe(oldestIso) : "–"}</b> ` +
      `bis <b style="color:#fff">${newestIso ? formatIsoToDe(newestIso) : "–"}</b><br>` +
      `<span style="font-size:11px;opacity:.85">Engine: ${turboActive ? "🚀 Direkt-GQL-Turbo (kein UI-Lag)" : "⚡ Roh-UI Fast-Scroll"}</span>`;
  }

  pauseBtn.addEventListener("click", () => {
    isPaused = !isPaused;
    pauseBtn.textContent = isPaused ? "▶️ Weiter" : "⏸️ Pause";
    updateHud(isPaused ? "⏸️ Pausiert" : "🚀 Weiter geht's...", true);
  });

  liteBtn.addEventListener("click", () => {
    if (liteModeActive) {
      disableUltraLiteMode();
      liteBtn.textContent = "⚡ Roh-UI: AUS";
      liteBtn.style.background = "#3a3a3d";
    } else {
      enableUltraLiteMode(findScrollableContainer());
      liteBtn.textContent = "⚡ Roh-UI: AN";
      liteBtn.style.background = "#265c42";
    }
  });

  saveBtn.addEventListener("click", () => {
    stopRequested = true;
    window.__modcheckExporterRunning = false;
    downloadTxtFile();
    updateHud("✅ .txt-Datei heruntergeladen!", true);
  });

  closeBtn.addEventListener("click", () => {
    stopRequested = true;
    window.__modcheckExporterRunning = false;
    disableUltraLiteMode();
    hud.remove();
  });

  // ── 7) Haupt-Ablauf: Roh-Modus -> 1x Scroll-Trigger -> GQL-Turbo! ────────
  async function runTurboExporter() {
    let scroller = findScrollableContainer();

    // 1. ZUERST bereits geladene DOM-Zeilen sichern, bevor das Layout abgespeckt wird
    scrapeVisibleDom(scroller, false);
    updateHud(undefined, true);

    if (!scroller && !capturedGqlTemplate) {
      updateHud("⚠️ Bitte Zuschauerkarte (Nachrichtenverlauf) öffnen!", true);
      while (!scroller && !capturedGqlTemplate && !stopRequested) {
        await sleep(500);
        scroller = findScrollableContainer();
      }
    }
    if (stopRequested) return;

    // 2. Sofort Website-Ballast & CSS entfernen (Ungestylter Roh-Modus)
    enableUltraLiteMode(scroller);
    scrapeVisibleDom(scroller, true);
    updateHud("⚡ Roh-Modus aktiv! Starte Turbo-Erfassung...", true);

    let idleScrollSteps = 0;
    let lastCount = collected.size;

    while (!stopRequested) {
      if (isPaused) {
        await sleep(250);
        continue;
      }

      // Sobald der erste GQL-Request abgefangen wurde -> DIREKTER API-TURBO (ohne UI-Scrollen!)
      if (capturedGqlTemplate && latestCapturedConn) {
        const res = await runDirectGqlTurbo(capturedGqlTemplate, latestCapturedConn);
        if (res.completedAll) {
          break;
        }
      }

      if (!scroller || !scroller.isConnected) {
        scroller = findScrollableContainer();
        if (scroller && liteModeActive) {
          scroller.style.setProperty("height", "45vh", "important");
          scroller.style.setProperty("max-height", "45vh", "important");
          scroller.style.setProperty("overflow-y", "scroll", "important");
        }
      }

      if (scroller) {
        if (scroller.scrollTop <= 5) {
          scroller.scrollTop = Math.min(scroller.clientHeight + 250, scroller.scrollHeight);
          scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
          await sleep(70);
        }
        scroller.scrollTop = 0;
        scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
        scroller.dispatchEvent(
          new WheelEvent("wheel", { deltaY: -1500, bubbles: true, cancelable: true })
        );
      }

      // Springt SOFORT weiter, sobald eine GQL-Antwort eintrifft!
      await waitForGqlOrTimeout(340);
      if (capturedGqlTemplate && latestCapturedConn) {
        continue;
      }

      scrapeVisibleDom(scroller, liteModeActive);
      updateHud("⚡ Roh-UI Fast-Scroll (suche ältere Seiten)...");

      if (collected.size === lastCount) {
        idleScrollSteps++;
        if (idleScrollSteps >= 12) {
          break;
        }
      } else {
        idleScrollSteps = 0;
        lastCount = collected.size;
      }
    }

    if (!stopRequested) {
      scrapeVisibleDom(scroller, false);
      updateHud("✅ Komplett fertig! .txt wird gespeichert...", true);
      window.__modcheckExporterRunning = false;
      downloadTxtFile();
    }
  }

  runTurboExporter();
})();
