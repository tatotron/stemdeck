// Sheet panel: the current lyric line large, the next one smaller, with a
// gutter above each line for the chords that come later. Follows the same
// playback clock as the Lyrics tab.

import { getCurrentTrackInfo } from "./catalog.js";
import { t } from "./i18n.js";
import { currentLineIndex, fromServerLyrics, wordTimings } from "./lyricsLookup.js";
import { timingOf } from "./lyricsSync.js";
import { setPlayheadTime, transport } from "./transport.js";

const panel = () => document.getElementById("sheetPanel");
const toggle = () => document.getElementById("sheetToggle");
const statusEl = () => document.getElementById("sheetStatus");
const languageEl = () => document.getElementById("sheetLanguage");
const importEl = () => document.getElementById("sheetImportToggle");
const transcribeEl = () => document.getElementById("sheetTranscribe");

let lines = [];
let lineIndex = -2;
let words = [];
let nextWords = [];
let clockOn = false;
let busy = false;
let loadedFor = null;

function isOpen() {
  const el = panel();
  return !!el && !el.hidden;
}

function setStatus(text) {
  const el = statusEl();
  if (el) el.textContent = text || "";
}

function paintWords(container, timed, now, wipe) {
  container.replaceChildren();
  if (!timed.length) return;
  for (const word of timed) {
    const span = document.createElement("span");
    span.className = "sheet-word";
    span.textContent = word.text;
    if (wipe && now >= word.end) span.classList.add("sung");
    else if (wipe && now >= word.start) {
      span.classList.add("singing");
      const spanDur = Math.max(0.05, word.end - word.start);
      const fill = Math.max(0, Math.min(1, (now - word.start) / spanDur));
      span.style.setProperty("--fill", `${(fill * 100).toFixed(1)}%`);
    }
    container.append(span);
  }
}

function showLines(index, now) {
  const current = document.getElementById("sheetCurrent");
  const next = document.getElementById("sheetNext");
  if (!current || !next) return;
  const line = index >= 0 ? lines[index] : null;
  const following = index >= 0 ? lines[index + 1] : lines[0] || null;
  if (index !== lineIndex) {
    lineIndex = index;
    words = line ? wordTimings(line, following?.time ?? null) : [];
    const after = index >= 0 ? lines[index + 2] : lines[1];
    nextWords = following ? wordTimings(following, after?.time ?? null) : [];
    if (!line) current.textContent = "";
    if (!following) next.textContent = "";
  }
  if (line) paintWords(current, words, now, true);
  else current.textContent = "";
  if (following) paintWords(next, nextWords, now, false);
  else next.textContent = "";
}

function indexAt(now) {
  if (!lines.length) return -1;
  const index = currentLineIndex(lines, now);
  // Before the first line, show that line large so it can be read coming in.
  return index >= 0 ? index : 0;
}

function tick() {
  if (!clockOn) return;
  const now = transport()?.getCurrentTime?.() ?? 0;
  showLines(indexAt(now), now);
  requestAnimationFrame(tick);
}

function startClock() {
  if (clockOn) return;
  clockOn = true;
  requestAnimationFrame(tick);
}

function stopClock() {
  clockOn = false;
}

function setOpen(open) {
  const el = panel();
  const btn = toggle();
  if (!el) return;
  el.hidden = !open;
  btn?.setAttribute("aria-pressed", String(open));
  if (open) {
    load();
    startClock();
  } else {
    stopClock();
  }
}

async function saveSetting(body) {
  const r = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

async function loadLyrics(id) {
  const r = await fetch(`/api/jobs/${id}/lyrics`, { cache: "no-store" });
  if (r.status === 404) return [];
  if (!r.ok) throw new Error(String(r.status));
  const found = fromServerLyrics(await r.json());
  const timed = timingOf(found?.entry);
  return timed?.lines || [];
}

async function load() {
  const id = getCurrentTrackInfo()?.id || "";
  if (id === loadedFor) return;
  lines = [];
  lineIndex = -2;
  if (!id) {
    loadedFor = "";
    setStatus(t("sheet.noTrack"));
    showLines(-1, 0);
    return;
  }
  try {
    lines = await loadLyrics(id);
    loadedFor = id;
    lineIndex = -2;
    setStatus(lines.length ? (busy ? t("sheet.working") : "") : t("sheet.noLyrics"));
  } catch (e) {
    console.warn("[sheet] could not read lyrics:", e);
    loadedFor = null;
    setStatus(t("sheet.loadFailed"));
  }
  showLines(indexAt(transport()?.getCurrentTime?.() ?? 0), transport()?.getCurrentTime?.() ?? 0);
}

async function transcribe() {
  const info = getCurrentTrackInfo();
  if (!info?.id || busy) return;
  busy = true;
  if (transcribeEl()) transcribeEl().disabled = true;
  setStatus(t("sheet.working"));
  try {
    const r = await fetch(`/api/jobs/${info.id}/playalong/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: languageEl()?.value || "auto" }),
    });
    const body = r.ok ? await r.json() : null;
    loadedFor = null;
    await load();
    if (r.status === 409) setStatus(t("sheet.busy"));
    else if (!r.ok) setStatus(t("sheet.failed"));
    else if (body?.playalong_status === "error") setStatus(t("sheet.rejected"));
    else if (body?.ok) setStatus(t("sheet.done"));
  } catch (e) {
    console.warn("[sheet] transcription failed:", e);
    setStatus(t("sheet.failed"));
  } finally {
    busy = false;
    if (transcribeEl()) transcribeEl().disabled = false;
  }
}

function seek(which) {
  const index = indexAt(transport()?.getCurrentTime?.() ?? 0);
  const line = which === "next" ? lines[index + 1] : lines[index];
  if (line) setPlayheadTime(Math.max(0, line.time));
}

export function initPlayalong() {
  const btn = toggle();
  const el = panel();
  if (!btn || !el) return;
  btn.addEventListener("click", () => setOpen(!isOpen()));
  document.getElementById("sheetClose")?.addEventListener("click", () => setOpen(false));
  document.getElementById("sheetCurrent")?.addEventListener("click", () => seek("current"));
  document.getElementById("sheetNext")?.addEventListener("click", () => seek("next"));
  transcribeEl()?.addEventListener("click", () => void transcribe());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen()) setOpen(false);
  });
  document.addEventListener("trackopen", () => {
    loadedFor = null;
    if (isOpen()) void load();
  });

  languageEl()?.addEventListener("change", async () => {
    const select = languageEl();
    if (!select) return;
    const next = select.value;
    select.disabled = true;
    try {
      const saved = await saveSetting({ playalong_language: next });
      select.value = saved.playalong_language || "auto";
    } catch (e) {
      console.warn("[sheet] could not save the language:", e);
    } finally {
      select.disabled = false;
    }
  });

  const paintImport = (on) => importEl()?.setAttribute("aria-pressed", String(!!on));
  importEl()?.addEventListener("click", async () => {
    const button = importEl();
    if (!button) return;
    const next = button.getAttribute("aria-pressed") !== "true";
    paintImport(next);
    button.disabled = true;
    try {
      paintImport((await saveSetting({ playalong: next })).playalong);
    } catch (e) {
      console.warn("[sheet] could not save the import toggle:", e);
      paintImport(!next);
    } finally {
      button.disabled = false;
    }
  });

  // Off at the start of a session, like Detect structure: it is a choice about
  // the next import, not a preference that should survive a reload. The
  // language is a preference and stays.
  fetch("/api/settings", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then(async (d) => {
      if (!d) return;
      const select = languageEl();
      if (select && d.playalong_language) select.value = d.playalong_language;
      paintImport(d.playalong);
      if (d.playalong) {
        paintImport(false);
        try {
          await saveSetting({ playalong: false });
        } catch (e) {
          console.warn("[sheet] could not clear the import toggle:", e);
        }
      }
    })
    .catch((e) => console.warn("[sheet] could not read settings:", e));
}
