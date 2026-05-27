#!/usr/bin/env python3
"""Poll Codex usage via ChatGPT backend API and publish to Firestore.

Auth is read from ~/.codex/auth.json, mirroring how the Claude companion
reads ~/.claude/.credentials.json.  No OpenAI API key is required.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import firebase_admin
from firebase_admin import credentials, firestore


CONFIG_PATH = Path(__file__).with_name("codex_config.json")
EXAMPLE_CONFIG_PATH = Path(__file__).with_name("codex_config.example.json")
DEFAULT_AUTH_PATH = Path.home() / ".codex" / "auth.json"

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# Duration constants used to identify which window is which
_FIVE_HOUR_SECS = 18_000
_WEEKLY_SECS = 604_800


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    else:
        raw = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
        log("Using codex_config.example.json. Copy it to codex_config.json and edit it.")

    config = json.loads(raw)
    config["project_id"] = os.environ.get("FIREBASE_PROJECT_ID", config["project_id"])
    config["document_path"] = os.environ.get(
        "CODEX_FIRESTORE_DOCUMENT_PATH", config["document_path"]
    )
    config["poll_interval_secs"] = int(
        os.environ.get("CODEX_POLL_INTERVAL", config["poll_interval_secs"])
    )
    config["request_timeout_secs"] = int(
        os.environ.get("CODEX_REQUEST_TIMEOUT", config["request_timeout_secs"])
    )
    config["service_account_json"] = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS", config["service_account_json"]
    )
    return config


def read_auth() -> tuple[str, str]:
    """Return (access_token, account_id) from ~/.codex/auth.json or env vars."""
    token = os.environ.get("CODEX_ACCESS_TOKEN", "")
    account_id = os.environ.get("CODEX_ACCOUNT_ID", "")
    if token:
        return token, account_id

    auth_path = Path(os.environ.get("CODEX_AUTH_PATH", str(DEFAULT_AUTH_PATH)))
    if not auth_path.exists():
        return "", ""

    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        # Codex CLI stores credentials under data["tokens"]
        tokens = data.get("tokens", data)
        token = tokens.get("access_token", "")
        account_id = tokens.get("account_id", "")
        return token, account_id
    except Exception:  # noqa: BLE001
        return "", ""


def fetch_usage(access_token: str, account_id: str, timeout_secs: int) -> dict[str, Any]:
    """Call the ChatGPT wham/usage endpoint and return the parsed JSON."""
    headers: dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    req = Request(USAGE_URL, headers=headers)
    with urlopen(req, timeout=timeout_secs) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_window(window: dict[str, Any]) -> tuple[int, str]:
    """Return (used_pct, reset_at_iso) from a rate_limit window object."""
    used_pct = max(0, min(100, round(float(window.get("used_percent", 0)))))
    reset_at_raw = window.get("reset_at", 0)
    try:
        reset_ts = float(reset_at_raw)
        reset_iso = datetime.fromtimestamp(reset_ts).astimezone().isoformat()
    except (ValueError, TypeError, OSError):
        reset_iso = ""
    return used_pct, reset_iso


def build_payload(data: dict[str, Any]) -> dict[str, Any]:
    rate_limit = data.get("rate_limit", {})
    session_pct = 0
    session_reset_at = ""
    weekly_pct = 0
    weekly_reset_at = ""

    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not window:
            continue
        duration = window.get("limit_window_seconds", 0)
        pct, reset_iso = parse_window(window)
        if duration == _FIVE_HOUR_SECS:
            session_pct, session_reset_at = pct, reset_iso
        elif duration == _WEEKLY_SECS:
            weekly_pct, weekly_reset_at = pct, reset_iso

    return {
        "sessionPct": session_pct,
        "sessionResetAt": session_reset_at,
        "weeklyPct": weekly_pct,
        "weeklyResetAt": weekly_reset_at,
        "status": "ok",
        "ok": True,
        "updatedAt": datetime.now().astimezone().isoformat(),
    }


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


def init_firestore(config: dict[str, Any]) -> firestore.Client:
    cred_path = Path(config["service_account_json"])
    cred = credentials.Certificate(str(cred_path))
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


def main() -> int:
    config = load_config()
    db = init_firestore(config)
    ref = document_ref(db, config["document_path"])
    log(
        f"Publishing Codex usage to Firestore project={config['project_id']} "
        f"document={config['document_path']}"
    )

    while True:
        access_token, account_id = read_auth()
        if not access_token:
            log(
                "No Codex access token found. Set CODEX_ACCESS_TOKEN or ensure "
                "~/.codex/auth.json exists."
            )
            write_payload(ref, build_error_payload("auth_missing"))
            time.sleep(config["poll_interval_secs"])
            continue

        try:
            data = fetch_usage(access_token, account_id, config["request_timeout_secs"])
            payload = build_payload(data)
            write_payload(ref, payload)
            log(
                "Published session={session}% weekly={weekly}%".format(
                    session=payload["sessionPct"],
                    weekly=payload["weeklyPct"],
                )
            )
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            log(f"ChatGPT API HTTP {exc.code}: {body[:200]}")
            write_payload(ref, build_error_payload("api_http_error"))
        except URLError as exc:
            log(f"Network error: {exc}")
            write_payload(ref, build_error_payload("network_error"))
        except Exception as exc:  # noqa: BLE001
            log(f"Unexpected error: {exc}")
            write_payload(ref, build_error_payload("unexpected_error"))

        time.sleep(config["poll_interval_secs"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        log("Stopped by user")
