from __future__ import annotations

import logging
from datetime import datetime
from typing import cast

from celery.app.task import Task
from django.utils import timezone

from .models import (
    ChatMessage,
    GithubApp,
    GithubInstallation,
    GithubRepository,
    GithubUser,
    PullRequest,
    ReviewRun,
)

from .tasks import handle_chat_response, run_pr_review

logger = logging.getLogger(__name__)


def upsert_user(payload: dict | None) -> GithubUser | None:
    if not payload:
        return None
    return GithubUser.objects.update_or_create(
        github_id=payload.get("id"),
        defaults={
            "login": payload.get("login", ""),
            "avatar_url": payload.get("avatar_url", ""),
            "html_url": payload.get("html_url", ""),
            "name": payload.get("name", ""),
            "email": payload.get("email", ""),
        },
    )[0]


def upsert_installation(payload: dict) -> GithubInstallation:
    account = payload.get("account") or {}
    return GithubInstallation.objects.update_or_create(
        github_app=None,
        installation_id=payload["id"],
        defaults={
            "account_login": account.get("login", ""),
            "account_type": account.get("type", ""),
            "target_type": payload.get("target_type", ""),
            "permissions": payload.get("permissions", {}),
            "events": payload.get("events", []),
            "is_active": payload.get("suspended_at") is None,
        },
    )[0]


def upsert_installation_for_app(
    payload: dict, github_app: GithubApp
) -> GithubInstallation:
    account = payload.get("account") or {}
    return GithubInstallation.objects.update_or_create(
        github_app=github_app,
        installation_id=payload["id"],
        defaults={
            "account_login": account.get("login", ""),
            "account_type": account.get("type", ""),
            "target_type": payload.get("target_type", ""),
            "permissions": payload.get("permissions", {}),
            "events": payload.get("events", []),
            "is_active": payload.get("suspended_at") is None,
        },
    )[0]


def upsert_repository(
    installation: GithubInstallation, repo_payload: dict
) -> GithubRepository:
    return GithubRepository.objects.update_or_create(
        installation=installation,
        repo_id=repo_payload["id"],
        defaults={
            "full_name": repo_payload.get("full_name", ""),
            "html_url": repo_payload.get("html_url", ""),
            "private": repo_payload.get("private", False),
            "default_branch": repo_payload.get("default_branch", "main"),
            "is_active": True,
        },
    )[0]


def deactivate_repository(installation: GithubInstallation, repo_payload: dict) -> None:
    GithubRepository.objects.filter(
        installation=installation, repo_id=repo_payload["id"]
    ).update(is_active=False)


def upsert_pull_request(repository: GithubRepository, payload: dict) -> PullRequest:
    user = upsert_user(payload.get("user"))
    created_at = parse_github_datetime(payload.get("created_at"))
    updated_at = parse_github_datetime(payload.get("updated_at"))
    return PullRequest.objects.update_or_create(
        repository=repository,
        pr_number=payload["number"],
        defaults={
            "pr_id": payload.get("id"),
            "title": payload.get("title", ""),
            "state": payload.get("state", ""),
            "html_url": payload.get("html_url", ""),
            "opened_by": user,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )[0]


def parse_github_datetime(value: str | None) -> datetime:
    if not value:
        return timezone.now()
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def queue_review(pull_request: PullRequest, head_sha: str) -> ReviewRun:
    review_run = ReviewRun.objects.create(
        pull_request=pull_request,
        head_sha=head_sha,
        status=ReviewRun.STATUS_QUEUED,
    )
    cast(Task, run_pr_review).delay(review_run.id)
    logger.info(
        "review.queued review_run_id=%s repo=%s pr=%s sha=%s",
        review_run.id,
        pull_request.repository.full_name,
        pull_request.pr_number,
        head_sha,
    )
    return review_run


def record_chat_message(
    pull_request: PullRequest, payload: dict, *, respond: bool = True
) -> ChatMessage:
    message = ChatMessage.objects.create(
        pull_request=pull_request,
        author=payload.get("user", {}).get("login", "unknown"),
        body=payload.get("body", ""),
        github_comment_id=payload.get("id"),
    )
    if respond:
        cast(Task, handle_chat_response).delay(pull_request.id, message.body)
    return message
