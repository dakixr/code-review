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


_GITHUB_API_VERSION = "2022-11-28"
_GITHUB_USER_AGENT = "codereview"


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


def _github_timeout(total_seconds: float) -> httpx.Timeout:
    """Return a GitHub-friendly timeout.

    In some containerized environments, IPv6 can be a blackhole. A shorter
    connect timeout helps fail over to IPv4 quickly and prevents jobs from
    stalling for long periods.
    """
    connect_seconds = min(5.0, total_seconds)
    pool_seconds = min(5.0, total_seconds)
    return httpx.Timeout(
        total_seconds,
        connect=connect_seconds,
        pool=pool_seconds,
    )


def get_installation_token(installation_id: int, auth: GithubAppAuth) -> str:
    jwt_token = build_jwt(auth)
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }

    max_attempts = 3
    backoff_seconds = 0.5
    timeout = _github_timeout(20.0)
    last_exception: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, headers=headers)

            response.raise_for_status()
            data = response.json()
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token.strip():
                raise RuntimeError(
                    "GitHub installation token response did not include a token."
                )
            return token
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in {502, 503, 504} and attempt < max_attempts:
                last_exception = e
            else:
                raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exception = e
        except Exception as e:
            raise RuntimeError("Failed to create a GitHub installation token.") from e

        if attempt < max_attempts:
            time.sleep(backoff_seconds * (2 ** (attempt - 1)))

    if isinstance(last_exception, httpx.TimeoutException):
        raise RuntimeError(
            "GitHub API request timed out while creating an installation token. "
            "Ensure the worker has outbound HTTPS access to api.github.com:443 "
            "(DNS/egress/proxy)."
        ) from last_exception
    if isinstance(last_exception, httpx.NetworkError):
        raise RuntimeError(
            "GitHub API request failed while creating an installation token. "
            "Ensure the worker has outbound HTTPS access to api.github.com:443 "
            "(DNS/egress/proxy)."
        ) from last_exception
    raise RuntimeError(
        "GitHub API request failed while creating an installation token."
    ) from last_exception


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
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }
    with httpx.Client(timeout=_github_timeout(20.0)) as client:
        response = client.post(url, headers=headers, json={"body": body})
        response.raise_for_status()
        return response.json()["id"]


def add_reaction_to_issue_comment(
    *,
    installation_id: int,
    auth: GithubAppAuth,
    repo_full_name: str,
    comment_id: int,
    content: str,
) -> None:
    """Add a reaction to an issue comment (e.g. content='eyes')."""
    token = get_installation_token(installation_id, auth)
    url = f"https://api.github.com/repos/{repo_full_name}/issues/comments/{comment_id}/reactions"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }
    with httpx.Client(timeout=_github_timeout(20.0)) as client:
        response = client.post(url, headers=headers, json={"content": content})
        # GitHub returns 422 if the same user already reacted with this content.
        if response.status_code == 422:
            return
        response.raise_for_status()


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
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }
    with httpx.Client(timeout=_github_timeout(20.0)) as client:
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
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
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
    with httpx.Client(timeout=_github_timeout(20.0)) as client:
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
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }
    with httpx.Client(timeout=_github_timeout(20.0)) as client:
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
    last_response: httpx.Response | None = None
    with httpx.Client(timeout=_github_timeout(40.0)) as client:
        for accept in [
            "application/vnd.github.v3.diff",
            "application/vnd.github.v3.patch",
        ]:
            headers = {
                "Authorization": f"token {token}",
                "Accept": accept,
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                "User-Agent": _GITHUB_USER_AGENT,
            }
            response = client.get(url, headers=headers)
            last_response = response
            if response.status_code in {406, 415, 501}:
                continue
            response.raise_for_status()

            content_type = (response.headers.get("content-type") or "").lower()
            if "json" in content_type or response.text.lstrip().startswith("{"):
                continue
            return response.text

    files = list_pull_request_files(
        installation_id=installation_id,
        auth=auth,
        repo_full_name=repo_full_name,
        pull_number=pull_number,
        limit=500,
        token=token,
    )
    diff_text = _render_pull_request_files_as_diff(files)
    if diff_text.strip():
        status_note = (
            f"{last_response.status_code} {last_response.reason_phrase}"
            if last_response is not None
            else "unknown error"
        )
        return (
            "NOTE: GitHub did not return a unified PR diff; "
            f"falling back to per-file patches from `/pulls/{{pull_number}}/files` "
            f"(last status: {status_note}).\n\n"
            f"{diff_text}"
        )

    if last_response is not None:
        last_response.raise_for_status()
    raise RuntimeError("Failed to fetch pull request diff.")


def _render_pull_request_files_as_diff(files: list[dict]) -> str:
    parts: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue

        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            continue

        status = item.get("status")
        status = status if isinstance(status, str) else "modified"

        previous_filename = item.get("previous_filename")
        previous_filename = (
            previous_filename if isinstance(previous_filename, str) else ""
        )

        old_path = (
            previous_filename if status == "renamed" and previous_filename else filename
        )
        new_path = filename

        parts.append(f"diff --git a/{old_path} b/{new_path}")

        if status == "renamed" and previous_filename:
            parts.append(f"rename from {previous_filename}")
            parts.append(f"rename to {filename}")

        if status == "added":
            parts.append("--- /dev/null")
            parts.append(f"+++ b/{new_path}")
        elif status == "removed":
            parts.append(f"--- a/{old_path}")
            parts.append("+++ /dev/null")
        else:
            parts.append(f"--- a/{old_path}")
            parts.append(f"+++ b/{new_path}")

        patch = item.get("patch")
        if isinstance(patch, str) and patch.strip():
            parts.append(patch.rstrip("\n"))
        else:
            parts.append(
                "(no patch available for this file — possibly binary, renamed without changes, or too large)"
            )

        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


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
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }
    with httpx.Client(timeout=_github_timeout(40.0)) as client:
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
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }

    files: list[dict] = []
    page = 1
    per_page = 100
    with httpx.Client(timeout=_github_timeout(40.0)) as client:
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
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }
    params = {"ref": ref}
    with httpx.Client(timeout=_github_timeout(40.0)) as client:
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
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }

    repos: list[dict] = []
    page = 1
    per_page = 100
    with httpx.Client(timeout=_github_timeout(40.0)) as client:
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


def download_repository_zipball(
    *,
    repo_full_name: str,
    ref: str,
    token: str,
    dest_path: Path,
    timeout_seconds: float = 120,
) -> None:
    """Download a repository zipball at a given ref to `dest_path`.

    Args:
        repo_full_name: "owner/name".
        ref: A commit SHA, tag, or branch name.
        token: Installation access token.
        dest_path: Where to write the zip file.
        timeout_seconds: Total request timeout.
    """
    url = f"https://api.github.com/repos/{repo_full_name}/zipball/{ref}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _GITHUB_USER_AGENT,
    }
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        timeout=_github_timeout(timeout_seconds),
        follow_redirects=True,
    ) as client:
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            with dest_path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
