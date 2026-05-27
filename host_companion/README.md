# Host Companion

Runs on your Raspberry Pi. A single process (`usage_sender.py`) polls **both** Claude Code
and Codex usage limits and publishes them to Firestore, where the ESP32 display and the
`Clawdmeter/` web app read them.

```
Claude / Codex APIs ──poll──> usage_sender.py ──write──> Firestore ──read──> ESP32 + web app
```

## Setup

1. Copy `usage_config.example.json` to `usage_config.json` and edit it for your Pi.
2. Put your Firebase service account JSON in this folder as `service-account.json`.
3. Install dependencies:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Make sure credentials exist:
   - **Claude:** `~/.claude/.credentials.json` (signed in via Claude Code), or set
     `CLAUDE_ACCESS_TOKEN`. OAuth tokens are auto-refreshed when they expire.
   - **Codex:** `~/.codex/auth.json` (run `codex --login`), or set `CODEX_ACCESS_TOKEN`.
5. Run:

   ```bash
   python3 usage_sender.py
   ```

   On the Pi this runs as the `usage-sender.service` systemd unit
   (`sudo systemctl restart usage-sender`, `journalctl -u usage-sender -f`).

## Configuration (`usage_config.json`)

```json
{
  "project_id": "your-project-id",
  "poll_interval_secs": 60,
  "request_timeout_secs": 20,
  "history_interval_secs": 300,
  "history_retention_hours": 168,
  "service_account_json": "service-account.json",
  "claude": { "document_path": "usage/current", "model": "claude-haiku-4-5-20251001" },
  "codex":  { "document_path": "usage/codex" }
}
```

| Key | Purpose |
| --- | --- |
| `poll_interval_secs` | How often each source is polled and the snapshot doc rewritten. |
| `history_interval_secs` | Minimum spacing between history points (default 300s = 5 min). |
| `history_retention_hours` | Written into each point's `expireAt` for the optional TTL policy. |

Any value can be overridden by env var: `FIREBASE_PROJECT_ID`, `USAGE_POLL_INTERVAL`,
`USAGE_REQUEST_TIMEOUT`, `USAGE_HISTORY_INTERVAL`, `USAGE_HISTORY_RETENTION_HOURS`,
`GOOGLE_APPLICATION_CREDENTIALS`, `CLAUDE_USAGE_MODEL`, `CLAUDE_FIRESTORE_DOCUMENT_PATH`,
`CODEX_FIRESTORE_DOCUMENT_PATH`.

## Firestore data

### Latest snapshot — `usage/current` (Claude) and `usage/codex` (Codex)

Overwritten every poll:

```json
{
  "sessionPct": 33,
  "sessionResetAt": "2026-05-28T07:00:00+08:00",
  "weeklyPct": 21,
  "weeklyResetAt": "2026-06-02T07:00:00+08:00",
  "status": "allowed",
  "ok": true,
  "updatedAt": "2026-05-28T02:05:24+08:00"
}
```

On errors the same shape is written with `ok: false` and `status` set to one of
`auth_missing`, `auth_expired`, `rate_limited`, `api_http_error`, `network_error`,
`unexpected_error`.

### History — `usage/current/history/{autoId}` and `usage/codex/history/{autoId}`

Appended at most once per `history_interval_secs`, only on successful polls. Minimal shape
for graphing:

```json
{ "ts": <serverTimestamp>, "sessionPct": 33, "weeklyPct": 21, "expireAt": <Timestamp> }
```

**Optional cleanup:** history grows unbounded. To auto-delete old points, enable a Firestore
[TTL policy](https://firebase.google.com/docs/firestore/ttl) on the `expireAt` field for the
`history` collection group (one-time setup in the console or via `gcloud`). Without it the
collection just keeps growing; the web app only ever reads the most recent N points.

## Weekly headroom / budget

The ESP32 (`computeWeeklyBudgetPct` in `src/main.cpp`) and the web app draw a faint "budget"
marker on the weekly bar — the share of the 7-day window that has already elapsed:

```
budgetPct = (604800 - (weeklyResetAt - updatedAt)) / 604800 * 100
```

If weekly usage runs ahead of that marker (bar longer than the budget), you're burning quota
faster than the week is passing, so the bar turns red.

## Web app (`Clawdmeter/`)

Static page (Firebase JS SDK + Chart.js, no build step) that reads the docs above directly
and shows live numbers plus a history graph for both providers. Deploy from the repo root:

```bash
npx -y firebase-tools@latest deploy --only hosting       # web app
npx -y firebase-tools@latest deploy --only firestore:rules  # after editing firestore.rules
```

`firestore.rules` grants public read on `usage/current`, `usage/codex`, and their `history`
subcollections; all writes come from this companion via the admin SDK (which bypasses rules).
