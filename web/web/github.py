from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from django.conf import settings


@dataclass
class GithubAppConfig:
    app_id: str
    private_key_path: str
    webhook_secret: str


def get_app_config() -> GithubAppConfig:
    return GithubAppConfig(
        app_id=settings.GITHUB_APP_ID,
        private_key_path=settings.GITHUB_APP_PRIVATE_KEY_PATH,
        webhook_secret=settings.GITHUB_WEBHOOK_SECRET,
    )


def load_private_key() -> str:
    key_path = Path(settings.GITHUB_APP_PRIVATE_KEY_PATH)
    return key_path.read_text()


def build_jwt() -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 10 * 60,
        "iss": settings.GITHUB_APP_ID,
    }
    return jwt.encode(payload, load_private_key(), algorithm="RS256")


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = signature.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


def get_installation_token(installation_id: int) -> str:
    jwt_token = build_jwt()
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(url, headers=headers)
        response.raise_for_status()
        return response.json()["token"]


def post_issue_comment(installation_id: int, repo_full_name: str, issue_number: int, body: str) -> int:
    token = get_installation_token(installation_id)
    url = f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(url, headers=headers, json={"body": body})
        response.raise_for_status()
        return response.json()["id"]


def update_issue_comment(installation_id: int, repo_full_name: str, comment_id: int, body: str) -> None:
    token = get_installation_token(installation_id)
    url = f"https://api.github.com/repos/{repo_full_name}/issues/comments/{comment_id}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=20) as client:
        response = client.patch(url, headers=headers, json={"body": body})
        response.raise_for_status()


def create_check_run(
    installation_id: int,
    repo_full_name: str,
    head_sha: str,
    name: str,
    status: str,
    conclusion: str | None = None,
    output: dict | None = None,
) -> None:
    token = get_installation_token(installation_id)
    url = f"https://api.github.com/repos/{repo_full_name}/check-runs"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    payload: dict[str, object] = {
        "name": name,
        "head_sha": head_sha,
        "status": status,
    }
    if conclusion:
        payload["conclusion"] = conclusion
    if output:
        payload["output"] = output
    with httpx.Client(timeout=20) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()


def parse_webhook_body(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def basic_auth_header(client_id: str, client_secret: str) -> str:
    token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {token}"
