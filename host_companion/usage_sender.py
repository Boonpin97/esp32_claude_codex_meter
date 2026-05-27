#!/usr/bin/env python3
"""Poll Claude Code and Codex usage limits, publish both to Firestore.

Claude credentials: ~/.claude/.credentials.json
Codex credentials:  ~/.codex/auth.json

Both are written on the same poll interval to their respective Firestore
documents (default: usage/current and usage/codex).
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import firebase_admin
from firebase_admin import credentials, firestore


CONFIG_PATH = Path(__file__).with_name("usage_config.json")
EXAMPLE_CONFIG_PATH = Path(__file__).with_name("usage_config.example.json")

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_USER_AGENT = "claude-code/2.1.150"
CLAUDE_DEFAULT_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_DEFAULT_AUTH = Path.home() / ".codex" / "auth.json"
_FIVE_HOUR_SECS = 18_000
_WEEKLY_SECS = 604_800


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    else:
        raw = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
        log("Using usage_config.example.json. Copy it to usage_config.json and edit it.")

    config = json.loads(raw)
    config["project_id"] = os.environ.get("FIREBASE_PROJECT_ID", config["project_id"])
    config["poll_interval_secs"] = int(
        os.environ.get("USAGE_POLL_INTERVAL", config["poll_interval_secs"])
    )
    config["request_timeout_secs"] = int(
        os.environ.get("USAGE_REQUEST_TIMEOUT", config["request_timeout_secs"])
    )
    config["history_interval_secs"] = int(
        os.environ.get("USAGE_HISTORY_INTERVAL", config.get("history_interval_secs", 300))
    )
    config["history_retention_hours"] = int(
        os.environ.get(
            "USAGE_HISTORY_RETENTION_HOURS", config.get("history_retention_hours", 168)
        )
    )
    config["service_account_json"] = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS", config["service_account_json"]
    )
    config["claude"]["document_path"] = os.environ.get(
        "CLAUDE_FIRESTORE_DOCUMENT_PATH", config["claude"]["document_path"]
    )
    config["claude"]["model"] = os.environ.get(
        "CLAUDE_USAGE_MODEL", config["claude"]["model"]
    )
    config["codex"]["document_path"] = os.environ.get(
        "CODEX_FIRESTORE_DOCUMENT_PATH", config["codex"]["document_path"]
    )
    return config


# ── Firestore ─────────────────────────────────────────────────────────────────

def init_firestore(config: dict[str, Any]) -> firestore.Client:
    cred = credentials.Certificate(str(Path(config["service_account_json"])))
    firebase_admin.initialize_app(cred, {"projectId": config["project_id"]})
    return firestore.client()


def document_ref(db: firestore.Client, document_path: str):
    parts = [p for p in document_path.split("/") if p]
    if len(parts) % 2 != 0:
        raise ValueError("document_path must be collection/document pairs")
    ref = db.collection(parts[0]).document(parts[1])
    for i in range(2, len(parts), 2):
        ref = ref.collection(parts[i]).document(parts[i + 1])
    return ref


def write_payload(ref, payload: dict[str, Any]) -> None:
    ref.set(payload)


def write_history(history_ref, payload: dict[str, Any], retention_hours: int) -> None:
    history_ref.add({
        "ts": firestore.SERVER_TIMESTAMP,
        "sessionPct": payload["sessionPct"],
        "weeklyPct": payload["weeklyPct"],
        "expireAt": datetime.now(timezone.utc) + timedelta(hours=retention_hours),
    })


def build_error_payload(status: str) -> dict[str, Any]:
    return {
        "sessionPct": 0,
        "sessionResetAt": "",
        "weeklyPct": 0,
        "weeklyResetAt": "",
        "status": status,
        "ok": False,
        "updatedAt": datetime.now().astimezone().isoformat(),
    }


# ── Claude ────────────────────────────────────────────────────────────────────

def _extract_claude_token(blob: str) -> str | None:
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        for value in data.values():
            if isinstance(value, dict) and isinstance(value.get("accessToken"), str):
                return value["accessToken"]

    match = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_credentials_blob() -> tuple[dict[str, Any] | None, Path | None]:
    path = Path(os.environ.get("CLAUDE_CREDENTIALS_PATH", str(CLAUDE_DEFAULT_CREDENTIALS)))
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, path
    return (data if isinstance(data, dict) else None), path


def _oauth_token_expired(oauth: dict[str, Any], skew_secs: int = 60) -> bool:
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        return False
    return (expires_at / 1000.0) <= (time.time() + skew_secs)


def _save_oauth_credentials(path: Path, root: dict[str, Any], oauth: dict[str, Any]) -> None:
    updated = dict(root)
    updated["claudeAiOauth"] = oauth
    path.write_text(json.dumps(updated, separators=(",", ":")), encoding="utf-8")


def _do_refresh_token(refresh_token: str, timeout_secs: int) -> tuple[str, dict[str, Any]]:
    body = urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = Request(
        CLAUDE_OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": CLAUDE_USER_AGENT,
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_secs) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("OAuth refresh response missing access_token")
    return access_token, payload


def _apply_refresh_payload(oauth: dict[str, Any], access_token: str, payload: dict[str, Any]) -> None:
    oauth["accessToken"] = access_token
    refreshed_rt = payload.get("refresh_token")
    if isinstance(refreshed_rt, str) and refreshed_rt:
        oauth["refreshToken"] = refreshed_rt
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        oauth["expiresAt"] = int((time.time() + float(expires_in)) * 1000)


def read_claude_token(timeout_secs: int = 20) -> str | None:
    for env_name in ("CLAUDE_ACCESS_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        value = os.environ.get(env_name)
        if value:
            return value

    root, path = _read_credentials_blob()
    oauth = root.get("claudeAiOauth") if isinstance(root, dict) else None

    if not isinstance(oauth, dict):
        if path and path.exists():
            return _extract_claude_token(path.read_text(encoding="utf-8"))
        return None

    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")

    if isinstance(access_token, str) and access_token and not _oauth_token_expired(oauth):
        return access_token

    if not isinstance(refresh_token, str) or not refresh_token:
        return access_token if isinstance(access_token, str) else None

    new_token, refresh_payload = _do_refresh_token(refresh_token, timeout_secs)
    _apply_refresh_payload(oauth, new_token, refresh_payload)
    if path and root is not None:
        _save_oauth_credentials(path, root, oauth)
    return new_token


def force_refresh_claude_token(timeout_secs: int) -> str | None:
    root, path = _read_credentials_blob()
    oauth = root.get("claudeAiOauth") if isinstance(root, dict) else None
    if not isinstance(oauth, dict):
        return None
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    new_token, refresh_payload = _do_refresh_token(refresh_token, timeout_secs)
    _apply_refresh_payload(oauth, new_token, refresh_payload)
    if path and root is not None:
        _save_oauth_credentials(path, root, oauth)
    return new_token


def _normalize_reset_ts(raw: str | None) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    try:
        value = float(raw)
        if value > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value).astimezone().isoformat()
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().isoformat()
    except ValueError:
        return ""


def _parse_utilization(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return max(0, min(100, int(round(float(raw) * 100.0))))
    except ValueError:
        return 0


def _make_claude_request(token: str, model: str, timeout_secs: int) -> dict[str, str]:
    req = Request(
        CLAUDE_API_URL,
        data=json.dumps({
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8"),
        headers={
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": CLAUDE_USER_AGENT,
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout_secs) as response:
        return {k.lower(): v for k, v in response.headers.items()}


def _build_claude_payload(h: dict[str, str]) -> dict[str, Any]:
    return {
        "sessionPct": _parse_utilization(h.get("anthropic-ratelimit-unified-5h-utilization")),
        "sessionResetAt": _normalize_reset_ts(h.get("anthropic-ratelimit-unified-5h-reset")),
        "weeklyPct": _parse_utilization(h.get("anthropic-ratelimit-unified-7d-utilization")),
        "weeklyResetAt": _normalize_reset_ts(h.get("anthropic-ratelimit-unified-7d-reset")),
        "status": h.get("anthropic-ratelimit-unified-5h-status", "unknown"),
        "ok": True,
        "updatedAt": datetime.now().astimezone().isoformat(),
    }


def poll_claude(ref, config: dict[str, Any]) -> dict[str, Any] | None:
    timeout = config["request_timeout_secs"]
    model = config["claude"]["model"]

    token = read_claude_token(timeout)
    if not token:
        log("Claude: no access token — set CLAUDE_ACCESS_TOKEN or sign in via Claude Code")
        write_payload(ref, build_error_payload("auth_missing"))
        return None

    try:
        h = _make_claude_request(token, model, timeout)
    except HTTPError as exc:
        if exc.code != 401:
            raise
        body = exc.read().decode("utf-8", errors="replace")
        log(f"Claude: HTTP 401 — refreshing token and retrying: {body[:120]}")
        refreshed = force_refresh_claude_token(timeout)
        if not refreshed:
            write_payload(ref, build_error_payload("auth_expired"))
            return None
        h = _make_claude_request(refreshed, model, timeout)

    payload = _build_claude_payload(h)
    write_payload(ref, payload)
    log(
        f"Claude: session={payload['sessionPct']}% weekly={payload['weeklyPct']}%"
        f" status={payload['status']}"
    )
    return payload


# ── Codex ─────────────────────────────────────────────────────────────────────

def read_codex_auth() -> tuple[str, str]:
    """Return (access_token, account_id) from ~/.codex/auth.json or env vars."""
    token = os.environ.get("CODEX_ACCESS_TOKEN", "")
    account_id = os.environ.get("CODEX_ACCOUNT_ID", "")
    if token:
        return token, account_id

    path = Path(os.environ.get("CODEX_AUTH_PATH", str(CODEX_DEFAULT_AUTH)))
    if not path.exists():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tokens = data.get("tokens", data)
        return tokens.get("access_token", ""), tokens.get("account_id", "")
    except Exception:  # noqa: BLE001
        return "", ""


def poll_codex(ref, config: dict[str, Any]) -> dict[str, Any] | None:
    token, account_id = read_codex_auth()
    if not token:
        log("Codex:  no access token — sign in via: codex --login")
        write_payload(ref, build_error_payload("auth_missing"))
        return None

    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    req = Request(CODEX_USAGE_URL, headers=headers)
    with urlopen(req, timeout=config["request_timeout_secs"]) as response:
        data = json.loads(response.read().decode("utf-8"))

    rate_limit = data.get("rate_limit", {})
    session_pct, session_reset_at = 0, ""
    weekly_pct, weekly_reset_at = 0, ""

    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not window:
            continue
        duration = window.get("limit_window_seconds", 0)
        pct = max(0, min(100, round(float(window.get("used_percent", 0)))))
        try:
            reset_iso = datetime.fromtimestamp(
                float(window.get("reset_at", 0))
            ).astimezone().isoformat()
        except (ValueError, TypeError, OSError):
            reset_iso = ""

        if duration == _FIVE_HOUR_SECS:
            session_pct, session_reset_at = pct, reset_iso
        elif duration == _WEEKLY_SECS:
            weekly_pct, weekly_reset_at = pct, reset_iso

    payload = {
        "sessionPct": session_pct,
        "sessionResetAt": session_reset_at,
        "weeklyPct": weekly_pct,
        "weeklyResetAt": weekly_reset_at,
        "status": "ok",
        "ok": True,
        "updatedAt": datetime.now().astimezone().isoformat(),
    }
    write_payload(ref, payload)
    log(f"Codex:  session={payload['sessionPct']}% weekly={payload['weeklyPct']}%")
    return payload


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    config = load_config()
    db = init_firestore(config)
    claude_ref = document_ref(db, config["claude"]["document_path"])
    codex_ref = document_ref(db, config["codex"]["document_path"])

    log(f"Polling Claude → {config['claude']['document_path']}")
    log(f"Polling Codex  → {config['codex']['document_path']}")

    history_interval = config["history_interval_secs"]
    retention_hours = config["history_retention_hours"]
    log(f"History every {history_interval}s, retained {retention_hours}h")

    sources = [
        ("Claude", poll_claude, claude_ref, claude_ref.collection("history")),
        ("Codex", poll_codex, codex_ref, codex_ref.collection("history")),
    ]
    last_history_write: dict[str, float] = {name: 0.0 for name, *_ in sources}

    while True:
        for name, poll_fn, ref, history_ref in sources:
            try:
                payload = poll_fn(ref, config)
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                log(f"{name} HTTP {exc.code}: {body[:200]}")
                if exc.code == 401:
                    write_payload(ref, build_error_payload("auth_expired"))
                elif exc.code == 429:
                    write_payload(ref, build_error_payload("rate_limited"))
                else:
                    write_payload(ref, build_error_payload("api_http_error"))
                continue
            except URLError as exc:
                log(f"{name} network error: {exc}")
                write_payload(ref, build_error_payload("network_error"))
                continue
            except Exception as exc:  # noqa: BLE001
                log(f"{name} unexpected error: {exc}\n{traceback.format_exc()}")
                write_payload(ref, build_error_payload("unexpected_error"))
                continue

            now = time.time()
            if payload and payload.get("ok") and (now - last_history_write[name]) >= history_interval:
                try:
                    write_history(history_ref, payload, retention_hours)
                    last_history_write[name] = now
                except Exception as exc:  # noqa: BLE001
                    log(f"{name} history write failed: {exc}")

        time.sleep(config["poll_interval_secs"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        log("Stopped by user")
