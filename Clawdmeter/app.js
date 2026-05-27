import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js";
import {
  getFirestore,
  doc,
  onSnapshot,
  collection,
  query,
  orderBy,
  limit,
  getDocs,
} from "https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js";

// Fill in your Firebase project's web app config.
// Get these values from: Firebase console → Project settings → Your apps → SDK setup.
const firebaseConfig = {
  apiKey: "YOUR_API_KEY",
  authDomain: "YOUR_PROJECT_ID.firebaseapp.com",
  projectId: "YOUR_PROJECT_ID",
  storageBucket: "YOUR_PROJECT_ID.firebasestorage.app",
  messagingSenderId: "YOUR_MESSAGING_SENDER_ID",
  appId: "YOUR_APP_ID",
};

const db = getFirestore(initializeApp(firebaseConfig));

const PROVIDERS = {
  claude: { docPath: ["usage", "current"], color: "#ff8c00", weeklyColor: "#ffb86b" },
  codex: { docPath: ["usage", "codex"], color: "#00b4b0", weeklyColor: "#6fe3df" },
};

// Visible window → max points to fetch (history is written ~every 5 min).
const RANGES = { "1h": 12, "24h": 288, "7d": 2016 };
let currentRange = "24h";

const charts = {};

// ── Current stats (live) ────────────────────────────────────────────────────

function fmtReset(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtAgo(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const secs = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

function colorForPct(pct) {
  if (pct >= 90) return "var(--red)";
  if (pct >= 70) return "var(--amber)";
  return "var(--green)";
}

function renderCurrent(provider, data) {
  const card = document.getElementById(`card-${provider}`);
  const set = (field, value) => {
    const el = card.querySelector(`[data-field="${field}"]`);
    if (el) el.textContent = value;
  };

  const sessionPct = Number(data.sessionPct ?? 0);
  const weeklyPct = Number(data.weeklyPct ?? 0);
  const ok = data.ok !== false;

  set("status", ok ? String(data.status ?? "—") : String(data.status ?? "error"));
  card.querySelector('[data-field="status"]').classList.toggle("bad", !ok);

  set("sessionPct", sessionPct);
  set("weeklyPct", weeklyPct);
  set("sessionReset", fmtReset(data.sessionResetAt));
  set("weeklyReset", fmtReset(data.weeklyResetAt));
  set("updated", fmtAgo(data.updatedAt));

  const sessionFill = card.querySelector('[data-field="sessionFill"]');
  sessionFill.style.width = `${Math.min(100, sessionPct)}%`;
  sessionFill.style.background = colorForPct(sessionPct);

  const weeklyFill = card.querySelector('[data-field="weeklyFill"]');
  const budget = Number(data.weeklyBudgetPct ?? computeWeeklyBudget(data));
  weeklyFill.style.width = `${Math.min(100, weeklyPct)}%`;
  weeklyFill.style.background = budget > 0 && weeklyPct > budget ? "var(--red)" : "var(--green)";

  const budgetEl = card.querySelector('[data-field="weeklyBudget"]');
  budgetEl.style.width = `${Math.min(100, budget)}%`;
}

// Mirror of the ESP32 computeWeeklyBudgetPct: % of the 7-day window elapsed.
function computeWeeklyBudget(data) {
  const reset = data.weeklyResetAt ? new Date(data.weeklyResetAt).getTime() : NaN;
  const updated = data.updatedAt ? new Date(data.updatedAt).getTime() : NaN;
  if (Number.isNaN(reset) || Number.isNaN(updated)) return 0;
  const weekMs = 604800 * 1000;
  const remaining = reset - updated;
  if (remaining <= 0) return 100;
  return Math.max(0, Math.min(100, Math.round(((weekMs - remaining) / weekMs) * 100)));
}

// ── History graph ─────────────────────────────────────────────────────────────

function tsToDate(ts) {
  if (!ts) return null;
  if (typeof ts.toDate === "function") return ts.toDate();
  if (typeof ts.seconds === "number") return new Date(ts.seconds * 1000);
  return null;
}

function fmtLabel(date) {
  if (!date) return "";
  return currentRange === "1h"
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

async function loadHistory(provider) {
  const { docPath } = PROVIDERS[provider];
  const histCol = collection(db, docPath[0], docPath[1], "history");
  const q = query(histCol, orderBy("ts", "desc"), limit(RANGES[currentRange]));
  const snap = await getDocs(q);
  const rows = [];
  snap.forEach((d) => rows.push(d.data()));
  rows.reverse();
  return rows;
}

function buildChart(provider, rows) {
  const cfg = PROVIDERS[provider];
  const labels = rows.map((r) => fmtLabel(tsToDate(r.ts)));
  const session = rows.map((r) => Number(r.sessionPct ?? 0));
  const weekly = rows.map((r) => Number(r.weeklyPct ?? 0));

  const ctx = document.getElementById(`chart-${provider}`);
  if (charts[provider]) charts[provider].destroy();

  charts[provider] = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Session (5h) %",
          data: session,
          borderColor: cfg.color,
          backgroundColor: cfg.color + "22",
          fill: true,
          tension: 0.25,
          pointRadius: 0,
          borderWidth: 2,
        },
        {
          label: "Weekly (7d) %",
          data: weekly,
          borderColor: cfg.weeklyColor,
          borderDash: [5, 4],
          fill: false,
          tension: 0.25,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: { min: 0, max: 100, ticks: { color: "#8aa0b4" }, grid: { color: "#1e2c3c" } },
        x: { ticks: { color: "#8aa0b4", maxTicksLimit: 8, autoSkip: true }, grid: { display: false } },
      },
      plugins: {
        legend: { labels: { color: "#e8eef5", boxWidth: 14 } },
      },
    },
  });
}

async function refreshCharts() {
  for (const provider of Object.keys(PROVIDERS)) {
    try {
      const rows = await loadHistory(provider);
      buildChart(provider, rows);
    } catch (err) {
      console.error(`history load failed for ${provider}`, err);
    }
  }
}

// ── Wiring ──────────────────────────────────────────────────────────────────

for (const [provider, cfg] of Object.entries(PROVIDERS)) {
  const ref = doc(db, cfg.docPath[0], cfg.docPath[1]);
  onSnapshot(ref, (snap) => {
    if (snap.exists()) renderCurrent(provider, snap.data());
  });
}

document.getElementById("range").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-range]");
  if (!btn) return;
  currentRange = btn.dataset.range;
  document.querySelectorAll("#range button").forEach((b) => b.classList.toggle("active", b === btn));
  refreshCharts();
});

refreshCharts();
setInterval(refreshCharts, 5 * 60 * 1000);
