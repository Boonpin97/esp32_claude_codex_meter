#!/usr/bin/env python3
"""Poll Claude Code usage headers and publish them to Firestore."""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import firebase_admin
from firebase_admin import credentials, firestore


CONFIG_PATH = Path(__file__).with_name("config.json")
EXAMPLE_CONFIG_PATH = Path(__file__).with_name("config.example.json")
DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

API_URL = "https://api.anthropic.com/v1/messages"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
DEFAULT_MODEL = "claude-3-5-haiku-20241022"
CLAUDE_CODE_USER_AGENT = "claude-code/2.1.150"
AUTH_REFRESH_MIN_BACKOFF_SECS = 30
AUTH_REFRESH_MAX_BACKOFF_SECS = 900
API_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": CLAUDE_CODE_USER_AGENT,
}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    else:
        raw = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
        log("Using config.example.json values. Copy it to config.json and edit it for your Pi.")

    config = json.loads(raw)
    config["project_id"] = os.environ.get("FIREBASE_PROJECT_ID", config["project_id"])
    config["document_path"] = os.environ.get("FIRESTORE_DOCUMENT_PATH", config["document_path"])
    config["poll_interval_secs"] = int(
        os.environ.get("CLAUDE_POLL_INTERVAL", config["poll_interval_secs"])
    )
    config["request_timeout_secs"] = int(
        os.environ.get("CLAUDE_REQUEST_TIMEOUT", config["request_timeout_secs"])
    )
    configured_model = config.get("model", DEFAULT_MODEL)
    config["model"] = os.environ.get("CLAUDE_USAGE_MODEL", configured_model) or DEFAULT_MODEL
    config["service_account_json"] = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS", config["service_account_json"]
    )
    return config


def extract_access_token(blob: str) -> str | None:
    blob = blob.strip()
    if not blob:
        return None

    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        direct = data.get("accessToken")
        if isinstance(direct, str):
            return direct

        for value in data.values():
            if isinstance(value, dict):
                nested = value.get("accessToken")
                if isinstance(nested, str):
                    return nested

    match = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if match:
        return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob

    return None


def read_credentials_blob() -> tuple[dict[str, Any] | None, Path | None]:
    path = Path(os.environ.get("CLAUDE_CREDENTIALS_PATH", str(DEFAULT_CREDENTIALS_PATH)))
    if not path.exists():
        return None, None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, path

    if isinstance(data, dict):
        return data, path
    return None, path


def extract_oauth_credentials(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    oauth = data.get("claudeAiOauth")
    if isinstance(oauth, dict):
        return oauth

    return None


def oauth_token_expired(oauth: dict[str, Any], skew_secs: int = 60) -> bool:
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        return False
    return (expires_at / 1000.0) <= (time.time() + skew_secs)


def save_oauth_credentials(path: Path, root: dict[str, Any], oauth: dict[str, Any]) -> None:
    root = dict(root)
    root["claudeAiOauth"] = oauth
    path.write_text(json.dumps(root, separators=(",", ":")), encoding="utf-8")


def refresh_access_token(
    refresh_token: str, timeout_secs: int
) -> tuple[str, dict[str, Any]]:
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
    ).encode("utf-8")

    req = Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": CLAUDE_CODE_USER_AGENT,
        },
        method="POST",
    )

    with urlopen(req, timeout=timeout_secs) as response:
        payload = json.loads(response.read().decode("utf-8"))

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("OAuth refresh response missing access_token")

    return access_token, payload


def get_access_token(timeout_secs: int) -> str | None:
    for env_name in ("CLAUDE_ACCESS_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
        value = os.environ.get(env_name)
        if value:
            return value

    root, path = read_credentials_blob()
    oauth = extract_oauth_credentials(root)
    if not oauth:
        if path and path.exists():
            return extract_access_token(path.read_text(encoding="utf-8"))
        return None

    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    if isinstance(access_token, str) and access_token and not oauth_token_expired(oauth):
        return access_token

    if not isinstance(refresh_token, str) or not refresh_token:
        return access_token if isinstance(access_token, str) else None

    refreshed_access_token, refresh_payload = refresh_access_token(refresh_token, timeout_secs)
    oauth["accessToken"] = refreshed_access_token

    refreshed_refresh_token = refresh_payload.get("refresh_token")
    if isinstance(refreshed_refresh_token, str) and refreshed_refresh_token:
        oauth["refreshToken"] = refreshed_refresh_token

    expires_in = refresh_payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        oauth["expiresAt"] = int((time.time() + float(expires_in)) * 1000)

    if path and root is not None:
        save_oauth_credentials(path, root, oauth)

    return refreshed_access_token


def force_refresh_access_token(timeout_secs: int) -> str | None:
    root, path = read_credentials_blob()
    oauth = extract_oauth_credentials(root)
    if not oauth:
        return None

    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None

    refreshed_access_token, refresh_payload = refresh_access_token(refresh_token, timeout_secs)
    oauth["accessToken"] = refreshed_access_token

    refreshed_refresh_token = refresh_payload.get("refresh_token")
    if isinstance(refreshed_refresh_token, str) and refreshed_refresh_token:
        oauth["refreshToken"] = refreshed_refresh_token

    expires_in = refresh_payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        oauth["expiresAt"] = int((time.time() + float(expires_in)) * 1000)

    if path and root is not None:
        save_oauth_credentials(path, root, oauth)

    return refreshed_access_token


def request_usage_headers(token: str, model: str, timeout_secs: int) -> dict[str, str]:
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }

    req = Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={**API_HEADERS, "Authorization": f"Bearer {token}"},
        method="POST",
    )

    with urlopen(req, timeout=timeout_secs) as response:
        return {key.lower(): value for key, value in response.headers.items()}


def parse_reset_minutes(raw: str | None) -> int:
    if not raw:
        return -1

    raw = raw.strip()

    try:
        value = float(raw)
    except ValueError:
        value = None

    if value is not None:
        if value > 10_000_000_000:
            value /= 1000.0
        mins = int(round((value - time.time()) / 60.0))
        return max(mins, 0)

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return -1

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = int(round((dt.timestamp() - time.time()) / 60.0))
    return max(mins, 0)


def normalize_reset_timestamp(raw: str | None) -> str:
    if not raw:
        return ""

    raw = raw.strip()

    try:
        value = float(raw)
    except ValueError:
        value = None

    if value is not None:
        if value > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value).astimezone().isoformat()

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat()


def parse_percent(raw: str | None) -> int:
    if not raw:
        return 0

    try:
        return max(0, min(100, int(round(float(raw) * 100.0))))
    except ValueError:
        return 0


def build_payload(headers: dict[str, str]) -> dict[str, Any]:
    return {
        "sessionPct": parse_percent(headers.get("anthropic-ratelimit-unified-5h-utilization")),
        "sessionResetAt": normalize_reset_timestamp(headers.get("anthropic-ratelimit-unified-5h-reset")),
        "weeklyPct": parse_percent(headers.get("anthropic-ratelimit-unified-7d-utilization")),
        "weeklyResetAt": normalize_reset_timestamp(headers.get("anthropic-ratelimit-unified-7d-reset")),
        "status": headers.get("anthropic-ratelimit-unified-5h-status", "unknown"),
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


def compute_auth_refresh_backoff_secs(failures: int) -> int:
    if failures <= 0:
        return AUTH_REFRESH_MIN_BACKOFF_SECS

    backoff = AUTH_REFRESH_MIN_BACKOFF_SECS * (2 ** (failures - 1))
    return min(backoff, AUTH_REFRESH_MAX_BACKOFF_SECS)


def init_firestore(config: dict[str, Any]) -> firestore.Client:
    cred_path = Path(config["service_account_json"])
    cred = credentials.Certificate(str(cred_path))
    firebase_admin.initialize_app(cred, {"projectId": config["project_id"]})
    return firestore.client()


def document_ref(db: firestore.Client, document_path: str):
    parts = [part for part in document_path.split("/") if part]
    if len(parts) % 2 != 0:
      raise ValueError("document_path must be collection/document, optionally nested")

    ref = db.collection(parts[0]).document(parts[1])
    for idx in range(2, len(parts), 2):
        ref = ref.collection(parts[idx]).document(parts[idx + 1])
    return ref


def write_payload(ref, payload: dict[str, Any]) -> None:
    ref.set(payload)


def main() -> int:
    config = load_config()
    db = init_firestore(config)
    ref = document_ref(db, config["document_path"])
    auth_refresh_failures = 0
    auth_refresh_blocked_until = 0.0
    log(
        f"Publishing Claude usage to Firestore project={config['project_id']} "
        f"document={config['document_path']}"
    )

    while True:
        now = time.time()
        if now < auth_refresh_blocked_until:
            remaining = max(1, int(math.ceil(auth_refresh_blocked_until - now)))
            log(f"OAuth refresh backoff active; skipping refresh for {remaining}s")
            write_payload(ref, build_error_payload("auth_refresh_rate_limited"))
            time.sleep(min(config["poll_interval_secs"], remaining))
            continue

        try:
            token = get_access_token(config["request_timeout_secs"])
            auth_refresh_failures = 0
            auth_refresh_blocked_until = 0.0
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                auth_refresh_failures += 1
                backoff_secs = compute_auth_refresh_backoff_secs(auth_refresh_failures)
                auth_refresh_blocked_until = time.time() + backoff_secs
                log(
                    f"OAuth refresh HTTP 429; backing off for {backoff_secs}s: "
                    f"{body[:200]}"
                )
                write_payload(ref, build_error_payload("auth_refresh_rate_limited"))
                time.sleep(config["poll_interval_secs"])
                continue
            log(f"OAuth refresh HTTP {exc.code}: {body[:200]}")
            write_payload(ref, build_error_payload("auth_refresh_http_error"))
            time.sleep(config["poll_interval_secs"])
            continue
        except URLError as exc:
            log(f"OAuth refresh network error: {exc}")
            write_payload(ref, build_error_payload("auth_refresh_network_error"))
            time.sleep(config["poll_interval_secs"])
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"OAuth refresh failed: {exc}")
            write_payload(ref, build_error_payload("auth_refresh_error"))
            time.sleep(config["poll_interval_secs"])
            continue

        if not token:
            log(
                "No Claude access token found. Set CLAUDE_ACCESS_TOKEN or ensure "
                "~/.claude/.credentials.json exists."
            )
            write_payload(ref, build_error_payload("auth_missing"))
            time.sleep(config["poll_interval_secs"])
            continue

        try:
            log(f"Requesting usage headers with model={config['model']}")
            headers = request_usage_headers(token, config["model"], config["request_timeout_secs"])
            payload = build_payload(headers)
            write_payload(ref, payload)
            log(
                "Published session={session}% weekly={weekly}% status={status}".format(
                    session=payload["sessionPct"],
                    weekly=payload["weeklyPct"],
                    status=payload["status"],
                )
            )
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401:
                try:
                    log("Anthropic HTTP 401; refreshing OAuth token and retrying once")
                    refreshed_token = force_refresh_access_token(config["request_timeout_secs"])
                    auth_refresh_failures = 0
                    auth_refresh_blocked_until = 0.0
                    if refreshed_token:
                        headers = request_usage_headers(
                            refreshed_token,
                            config["model"],
                            config["request_timeout_secs"],
                        )
                        payload = build_payload(headers)
                        write_payload(ref, payload)
                        log(
                            "Published session={session}% weekly={weekly}% status={status}".format(
                                session=payload["sessionPct"],
                                weekly=payload["weeklyPct"],
                                status=payload["status"],
                            )
                        )
                        time.sleep(config["poll_interval_secs"])
                        continue
                except HTTPError as refresh_exc:
                    refresh_body = refresh_exc.read().decode("utf-8", errors="replace")
                    if refresh_exc.code == 429:
                        auth_refresh_failures += 1
                        backoff_secs = compute_auth_refresh_backoff_secs(auth_refresh_failures)
                        auth_refresh_blocked_until = time.time() + backoff_secs
                        log(
                            f"OAuth retry HTTP 429; backing off for {backoff_secs}s: "
                            f"{refresh_body[:200]}"
                        )
                        write_payload(ref, build_error_payload("auth_refresh_rate_limited"))
                        time.sleep(config["poll_interval_secs"])
                        continue
                    log(f"OAuth retry HTTP {refresh_exc.code}: {refresh_body[:200]}")
                    write_payload(ref, build_error_payload("auth_refresh_http_error"))
                    time.sleep(config["poll_interval_secs"])
                    continue
                except URLError as refresh_exc:
                    log(f"OAuth retry network error: {refresh_exc}")
                    write_payload(ref, build_error_payload("auth_refresh_network_error"))
                    time.sleep(config["poll_interval_secs"])
                    continue
                except Exception as refresh_exc:  # noqa: BLE001
                    log(f"OAuth retry failed: {refresh_exc}")
                    write_payload(ref, build_error_payload("auth_refresh_error"))
                    time.sleep(config["poll_interval_secs"])
                    continue

            log(f"Anthropic HTTP {exc.code}: {body[:200]}")
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
