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

from .models import GithubInstallation


@dataclass
class GithubAppAuth:
    app_id: str
    private_key_pem: str
    webhook_secret: str


def _load_legacy_private_key() -> str:
    if not settings.GITHUB_APP_PRIVATE_KEY_PATH:
        raise RuntimeError(
            "GITHUB_APP_PRIVATE_KEY_PATH is required for legacy GitHub App mode."
        )
    key_path = Path(settings.GITHUB_APP_PRIVATE_KEY_PATH)
    return key_path.read_text()


def legacy_app_auth() -> GithubAppAuth:
    if not settings.GITHUB_APP_ID:
        raise RuntimeError("GITHUB_APP_ID is required for legacy GitHub App mode.")
    return GithubAppAuth(
        app_id=settings.GITHUB_APP_ID,
        private_key_pem=_load_legacy_private_key(),
        webhook_secret=settings.GITHUB_WEBHOOK_SECRET,
    )


def auth_for_installation(installation: GithubInstallation) -> GithubAppAuth:
    github_app = installation.github_app
    if (
        github_app
        and github_app.app_id
        and github_app.private_key_pem
        and github_app.webhook_secret
    ):
        return GithubAppAuth(
            app_id=str(github_app.app_id),
            private_key_pem=github_app.private_key_pem,
            webhook_secret=github_app.webhook_secret,
        )
    return legacy_app_auth()


def build_jwt(auth: GithubAppAuth) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 10 * 60,
        "iss": auth.app_id,
    }
    return jwt.encode(payload, auth.private_key_pem, algorithm="RS256")


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    provided = signature.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


def get_installation_token(installation_id: int, auth: GithubAppAuth) -> str:
    jwt_token = build_jwt(auth)
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(url, headers=headers)
        response.raise_for_status()
        return response.json()["token"]


def post_issue_comment(
    installation_id: int,
    auth: GithubAppAuth,
    repo_full_name: str,
    issue_number: int,
    body: str,
) -> int:
    token = get_installation_token(installation_id, auth)
    url = (
        f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments"
    )
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(url, headers=headers, json={"body": body})
        response.raise_for_status()
        return response.json()["id"]


def update_issue_comment(
    installation_id: int,
    auth: GithubAppAuth,
    repo_full_name: str,
    comment_id: int,
    body: str,
) -> None:
    token = get_installation_token(installation_id, auth)
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
    auth: GithubAppAuth,
    repo_full_name: str,
    head_sha: str,
    name: str,
    status: str,
    conclusion: str | None = None,
    output: dict | None = None,
) -> None:
    token = get_installation_token(installation_id, auth)
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


def convert_manifest_code(
    code: str, *, api_url: str = "https://api.github.com"
) -> dict:
    url = f"{api_url}/app-manifests/{code}/conversions"
    headers = {"Accept": "application/vnd.github+json"}
    with httpx.Client(timeout=20) as client:
        response = client.post(url, headers=headers)
        response.raise_for_status()
        return response.json()


def fetch_pull_request_diff(
    *,
    installation_id: int,
    auth: GithubAppAuth,
    repo_full_name: str,
    pull_number: int,
    token: str | None = None,
) -> str:
    token = token or get_installation_token(installation_id, auth)
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pull_number}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff",
    }
    with httpx.Client(timeout=40) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.text


def fetch_pull_request_json(
    *,
    installation_id: int,
    auth: GithubAppAuth,
    repo_full_name: str,
    pull_number: int,
    token: str | None = None,
) -> dict:
    """Fetch pull request metadata as JSON."""
    token = token or get_installation_token(installation_id, auth)
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pull_number}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client(timeout=40) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}


def list_pull_request_files(
    *,
    installation_id: int,
    auth: GithubAppAuth,
    repo_full_name: str,
    pull_number: int,
    limit: int = 200,
    token: str | None = None,
) -> list[dict]:
    """List files changed in a pull request."""
    token = token or get_installation_token(installation_id, auth)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    files: list[dict] = []
    page = 1
    per_page = 100
    with httpx.Client(timeout=40) as client:
        while len(files) < limit:
            url = (
                f"https://api.github.com/repos/{repo_full_name}/pulls/{pull_number}/files"
                f"?per_page={per_page}&page={page}"
            )
            response = client.get(url, headers=headers)
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            for item in batch:
                if isinstance(item, dict):
                    files.append(item)
                    if len(files) >= limit:
                        break
            if len(batch) < per_page:
                break
            page += 1
    return files


def fetch_repository_file_text(
    *,
    installation_id: int,
    auth: GithubAppAuth,
    repo_full_name: str,
    path: str,
    ref: str,
    max_bytes: int = 200_000,
    token: str | None = None,
) -> str | None:
    """Fetch a repository file at a specific ref and decode it as UTF-8 text.

    Returns None if the path is not a regular file or is too large / not decodable.
    """
    token = token or get_installation_token(installation_id, auth)
    url = f"https://api.github.com/repos/{repo_full_name}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    params = {"ref": ref}
    with httpx.Client(timeout=40) as client:
        response = client.get(url, headers=headers, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None
        if data.get("type") != "file":
            return None

        size = data.get("size")
        if isinstance(size, int) and size > max_bytes:
            return None

        encoded = data.get("content")
        encoding = data.get("encoding")
        if not isinstance(encoded, str) or encoding != "base64":
            return None

        try:
            raw = base64.b64decode(encoded, validate=False)
        except Exception:
            return None
        if len(raw) > max_bytes:
            return None
        if b"\x00" in raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None


def list_installation_repositories(
    *,
    installation_id: int,
    auth: GithubAppAuth,
) -> list[dict]:
    """List repositories accessible to an installation.

    Uses the installation access token and `GET /installation/repositories`.
    """
    token = get_installation_token(installation_id, auth)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    repos: list[dict] = []
    page = 1
    per_page = 100
    with httpx.Client(timeout=40) as client:
        while True:
            url = f"https://api.github.com/installation/repositories?per_page={per_page}&page={page}"
            response = client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            batch = data.get("repositories", [])
            if not isinstance(batch, list) or not batch:
                break
            repos.extend([repo for repo in batch if isinstance(repo, dict)])
            if len(batch) < per_page:
                break
            page += 1
    return repos
