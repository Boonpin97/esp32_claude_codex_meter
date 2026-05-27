# ESP32 Claude + Codex Usage Meter

A desk gadget that shows your **Claude Code** and **Codex** rate-limit usage at a glance on a
3.5" ESP32 touch display, plus a companion web dashboard with history graphs.

```
Claude / Codex APIs
        │  poll every 60s (host_companion/usage_sender.py, on the Pi)
        ▼
   Firestore  ──  usage/current (Claude snapshot)
                  usage/codex   (Codex snapshot)
                  usage/current/history/{autoId}  (time-series, every 5 min)
                  usage/codex/history/{autoId}
        │
        ├── read every 30s ──> ESP32 firmware → 3.5" ST7796 display
        └── read (live)    ──> Clawdmeter web app (Firebase Hosting)
```

---

## Quick setup

### 1 — Raspberry Pi (usage poller)

```bash
cd host_companion
cp usage_config.example.json usage_config.json   # edit project_id, service_account_json
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

Credentials needed (at least one pair):
- **Claude:** sign in to Claude Code — it writes `~/.claude/.credentials.json` automatically.
  OAuth tokens are refreshed automatically when they expire.
- **Codex:** run `codex --login` — it writes `~/.codex/auth.json`.

Run once to verify:
```bash
python3 usage_sender.py
```

To run as a background service:
```bash
sudo cp usage-sender.service /etc/systemd/system/
sudo systemctl enable --now usage-sender
journalctl -u usage-sender -f
```

### 2 — ESP32 firmware

```bash
cp include/monitor_config.example.local.h include/monitor_config.local.h
# edit: WiFi credentials, Firestore project ID, optional API key
pio run -t upload
```

### 3 — Web app (optional)

To deploy after changes:

```bash
npx -y firebase-tools@latest deploy --only hosting
npx -y firebase-tools@latest deploy --only firestore:rules   # after editing firestore.rules
```

---

## Hardware

| Part | Details |
| --- | --- |
| Board | ESP32-3248S035 / JC3248A035N (see `Datasheets/`) |
| Display | 3.5" 480×320 ST7796, SPI via HSPI bus |
| Touch | Resistive, CS on GPIO 33, IRQ on GPIO 36 |
| Backlight | GPIO 27 |
| SPI pins | SCK 14 · MOSI 13 · MISO 12 · DC 2 · CS 15 |

The firmware supports **two saved Wi-Fi profiles** (HOME / OFFICE). Tap the Wi-Fi button on
the display to toggle between them.

---

## Display layout

The screen is divided into four panels in a 2×2 landscape grid (stacks vertically in portrait):

```
┌────────────────────────┬────────────────────────┐
│  Claude Session (5h)   │  Claude Weekly (7d)    │
│  33%  Reset: 3:00pm    │  21%  Reset: Mon 0700  │
│  ████░░░░░░░░░░░░░░░░  │  ██░░░░░░░░░|░░░░░░░░  │
├────────────────────────┼────────────────────────┤
│  Codex Session (5h)    │  Codex Weekly (7d)     │
│   1%  Reset: 7:00pm    │  20%  Reset: Fri 0700  │
│  ░░░░░░░░░░░░░░░░░░░░  │  ██░░░░░░░░░|░░░░░░░░  │
└────────────────────────┴────────────────────────┘
```

Each panel shows:
- **Percentage** — current rate-limit consumption (0–100 %)
- **Reset time** — when the window rolls over (session: 12h clock; weekly: weekday + time)
- **Usage bar** — colour-coded: green → amber (≥50%) → red (≥80%)
- **Weekly headroom marker** — a faint underlay on the weekly bar shows how far through
  the 7-day window you are. If the usage bar grows past the marker, the bar turns red,
  meaning you're burning quota faster than the week is passing.

The header row shows a **status pill** (LIVE / POLLING / STALE / ERROR) and a **Wi-Fi
button** that switches between HOME and OFFICE profiles.

### Weekly headroom formula

```
budgetPct = (604800 − (weeklyResetAt − updatedAt)) / 604800 × 100
```

Both timestamps come from Firestore with the same timezone offset, so the offset cancels and
the math is done in plain seconds. Precision is to the second; the bar has 100 discrete steps
(~1–2 h of visual resolution across a 7-day bar).

---

## Firmware config (`include/monitor_config.local.h`)

Copy `monitor_config.example.local.h` and set these macros (all others have sane defaults):

| Macro | Default | Notes |
| --- | --- | --- |
| `MONITOR_WIFI_SSID` | `""` | Home Wi-Fi SSID |
| `MONITOR_WIFI_PASSWORD` | `""` | Home Wi-Fi password |
| `MONITOR_FIRESTORE_PROJECT_ID` | `"your-project-id"` | Firebase project ID |
| `MONITOR_FIRESTORE_DOCUMENT_PATH` | `"usage/current"` | Claude doc path |
| `MONITOR_CODEX_DOCUMENT_PATH` | `"usage/codex"` | Codex doc path |
| `MONITOR_FIRESTORE_API_KEY` | `""` | Web API key (optional, for restricted rules) |
| `MONITOR_DISPLAY_ROTATION` | `1` | 0–3 (1 = landscape) |
| `MONITOR_DASHBOARD_TITLE` | `"Claude Usage"` | Header text |
| `MONITOR_FIRESTORE_POLL_INTERVAL_MS` | `30000` | How often the ESP32 polls Firestore |
| `MONITOR_DATA_STALE_AFTER_MS` | `180000` | Age before "STALE" status appears |

Optional second profile: define `OFFICE_WIFI_SSID` and `OFFICE_WIFI_PASSWORD` to enable the
Wi-Fi toggle button on the display.

This file is git-ignored (`include/monitor_config.h` contains only defaults and is safe to
commit).

---

## Pi poller config (`host_companion/usage_config.json`)

| Key | Default | Notes |
| --- | --- | --- |
| `poll_interval_secs` | `60` | How often each source is polled |
| `request_timeout_secs` | `20` | HTTP timeout for API + OAuth calls |
| `history_interval_secs` | `300` | Minimum gap between history points (5 min) |
| `history_retention_hours` | `168` | Written into `expireAt` for optional TTL policy |
| `claude.model` | `claude-haiku-4-5-20251001` | Model used to trigger the usage headers |
| `claude.document_path` | `usage/current` | Firestore doc for Claude snapshot |
| `codex.document_path` | `usage/codex` | Firestore doc for Codex snapshot |

All keys can be overridden by environment variable. See
[host_companion/README.md](host_companion/README.md) for the full list and Firestore field
shapes.

---

## Firestore data model

### Snapshot docs (overwritten every poll)

`usage/current` and `usage/codex`:

```json
{
  "sessionPct": 33,
  "sessionResetAt": "2026-05-28T07:00:00+08:00",
  "weeklyPct": 21,
  "weeklyResetAt": "2026-06-02T07:00:00+08:00",
  "status": "allowed",
  "ok": true,
  "updatedAt": "2026-05-28T03:02:25+08:00"
}
```

On error `ok` is `false` and `status` is one of: `auth_missing`, `auth_expired`,
`rate_limited`, `api_http_error`, `network_error`, `unexpected_error`.

### History subcollections (appended every 5 min)

`usage/current/history/{autoId}` and `usage/codex/history/{autoId}`:

```json
{ "ts": "<serverTimestamp>", "sessionPct": 33, "weeklyPct": 21, "expireAt": "<Timestamp +7d>" }
```

The web app queries `orderBy("ts","desc").limit(N)` — no composite index needed.
`expireAt` supports an optional Firestore TTL policy for automatic cleanup; without it the
collection grows indefinitely but the web app only ever reads the most recent N points.

---

## Web app (`Clawdmeter/`)

Static page served from Firebase Hosting — no build step, no server.

- **Live cards** — `onSnapshot` on each snapshot doc; updates in real time.
- **History graphs** — Chart.js line charts for session % and weekly % over time, with a
  1h / 24h / 7d range toggle.
- **Headroom marker** — the weekly bar underlay mirrors the ESP32 logic.
- **Colour identity** — Claude warm orange (`#ff8c00`), Codex teal (`#00b4b0`), matching the
  display firmware.

Once deployed, accessible at `https://your-project-id.web.app`.

---

## Repo layout

```
src/main.cpp                    ESP32 firmware (WiFi, Firestore REST, ST7796 display, touch)
include/monitor_config.h        Firmware config defaults
include/monitor_config.local.h  Your local overrides — git-ignored
host_companion/
  usage_sender.py               Pi poller: Claude + Codex → Firestore
  usage_config.json             Local config — git-ignored
  usage_config.example.json     Config template
  usage-sender.service          systemd unit for the Pi
  README.md                     Detailed host companion docs
Clawdmeter/
  index.html                    Web app entry point
  app.js                        Firebase SDK + Chart.js logic
  style.css                     Dark dashboard styles
firestore.rules                 Security rules (public read, write via admin SDK only)
firebase.json / .firebaserc     Firebase project config
platformio.ini                  PlatformIO build config
Datasheets/                     Hardware reference PDFs
```
