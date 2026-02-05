from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from celery import shared_task
from django.utils import timezone

from . import github
from .models import (
    ChatMessage,
    GithubRepository,
    PullRequest,
    ReviewComment,
    ReviewRun,
    RuleSet,
    UserApiKey,
)
from .opencode_client import run_opencode

logger = logging.getLogger(__name__)

_ZAI_TOKEN_ERROR_SUBSTRINGS = {
    "token expired or incorrect",
    "invalid api key",
    "invalid apikey",
}


@shared_task
def run_pr_review(review_run_id: int) -> None:
    review_run = ReviewRun.objects.select_related(
        "pull_request__repository__installation__github_app__owner"
    ).get(id=review_run_id)
    logger.info("review.start review_run_id=%s", review_run_id)
    review_run.status = ReviewRun.STATUS_RUNNING
    review_run.started_at = timezone.now()
    review_run.save(update_fields=["status", "started_at"])

    pull_request = review_run.pull_request
    repository = pull_request.repository
    installation = repository.installation
    auth = github.auth_for_installation(installation)

    placeholder_body = "👁 Reviewing this PR now. I will post a full review shortly."
    placeholder_comment_id = github.post_issue_comment(
        installation_id=installation.installation_id,
        auth=auth,
        repo_full_name=repository.full_name,
        issue_number=pull_request.pr_number,
        body=placeholder_body,
    )
    logger.info(
        "review.placeholder_posted review_run_id=%s comment_id=%s repo=%s pr=%s",
        review_run_id,
        placeholder_comment_id,
        repository.full_name,
        pull_request.pr_number,
    )

    review_comment = ReviewComment.objects.create(
        review_run=review_run,
        body=placeholder_body,
        github_comment_id=placeholder_comment_id,
    )

    try:
        owner = getattr(installation.github_app, "owner", None)
        if not owner:
            raise RuntimeError(
                "This installation is not associated with a user-owned GitHub App."
            )
        api_key = (
            UserApiKey.objects.filter(
                user=owner,
                provider=UserApiKey.PROVIDER_ZAI,
                is_active=True,
            )
            .order_by("-updated_at")
            .values_list("api_key", flat=True)
            .first()
        )
        api_key = (api_key or "").strip()
        if not api_key:
            raise RuntimeError(
                "Missing ZAI API key for this user. Go to Account → API Keys and set it."
            )

        token = github.get_installation_token(installation.installation_id, auth)
        pr_json = github.fetch_pull_request_json(
            installation_id=installation.installation_id,
            auth=auth,
            repo_full_name=repository.full_name,
            pull_number=pull_request.pr_number,
            token=token,
        )
        head_sha = str(((pr_json.get("head") or {}).get("sha")) or "").strip()

        diff_text = github.fetch_pull_request_diff(
            installation_id=installation.installation_id,
            auth=auth,
            repo_full_name=repository.full_name,
            pull_number=pull_request.pr_number,
            token=token,
        )
        logger.info(
            "review.diff_fetched review_run_id=%s chars=%s",
            review_run_id,
            len(diff_text),
        )

        max_diff_chars = 160_000
        diff_note = ""
        if len(diff_text) > max_diff_chars:
            diff_note = (
                f"\n\nNOTE: Diff truncated to {max_diff_chars} characters for review."
            )
            diff_text = diff_text[:max_diff_chars]

        global_rule_sets = RuleSet.objects.prefetch_related("rules").filter(
            owner=owner,
            scope=RuleSet.SCOPE_GLOBAL,
            is_active=True,
        )
        repo_rule_sets = RuleSet.objects.prefetch_related("rules").filter(
            owner=owner,
            scope=RuleSet.SCOPE_REPO,
            repository=repository,
            is_active=True,
        )

        instruction_blocks: list[str] = []
        for rule_set in [*global_rule_sets, *repo_rule_sets]:
            instructions = rule_set.instructions.strip()
            if instructions:
                instruction_blocks.append(f"- {rule_set.name}: {instructions}")
            for rule in rule_set.rules.filter(is_active=True).all():
                instruction_blocks.append(
                    f"- [{rule.severity}] {rule.title}: {rule.description.strip()}"
                )

        rules_text = (
            "\n".join(instruction_blocks)
            if instruction_blocks
            else "- (no rules configured)"
        )

        prompt = (
            "You are an AI code reviewer responding as a GitHub PR review comment.\n"
            "Be crisp and actionable. Prefer pointing to specific files/lines.\n\n"
            "Context files:\n"
            "- `pull_request.diff` (the PR diff)\n"
            "- `repo_snapshot.md` (repo snapshot metadata)\n"
            "- `repo_index.md` (full file listing)\n"
            "You can read any file in the repository via the OpenCode project workspace.\n\n"
            "Project rules / preferences:\n"
            f"{rules_text}\n\n"
            "Task:\n"
            "- Review the attached PR diff.\n"
            "- Use the attached repository snapshot to confirm context when needed.\n"
            "- Call out correctness, security, performance, and maintainability issues.\n"
            "- If something is uncertain, ask a question instead of guessing.\n"
            "- Output Markdown suitable for a single GitHub comment.\n"
            f"{diff_note}"
        )

        with tempfile.TemporaryDirectory(prefix="codereview-ai-") as tmpdir:
            tmp_path = Path(tmpdir)
            diff_path = tmp_path / "pull_request.diff"
            diff_path.write_text(diff_text, encoding="utf-8")
            repo_dir, repo_snapshot_md = _prepare_repo_snapshot(
                tmp_path=tmp_path,
                repo_full_name=repository.full_name,
                head_sha=head_sha,
                token=token,
            )
            repo_snapshot_path = tmp_path / "repo_snapshot.md"
            repo_snapshot_path.write_text(repo_snapshot_md, encoding="utf-8")

            context_files: list[Path] = [diff_path, repo_snapshot_path]
            repo_index_path = tmp_path / "repo_index.md"
            if repo_dir is not None:
                repo_index_path.write_text(
                    _render_repo_index_markdown(repo_dir=repo_dir),
                    encoding="utf-8",
                )
                context_files.append(repo_index_path)
            result = run_opencode(
                message=prompt,
                files=context_files,
                env={"ZAI_API_KEY": api_key},
                cwd=repo_dir,
            )
        logger.info("review.opencode_done review_run_id=%s", review_run_id)

        summary = result.text.strip()
        github.update_issue_comment(
            installation_id=installation.installation_id,
            auth=auth,
            repo_full_name=repository.full_name,
            comment_id=placeholder_comment_id,
            body=summary,
        )
        logger.info("review.posted review_run_id=%s", review_run_id)

        review_comment.body = summary
        review_comment.save(update_fields=["body"])

        review_run.status = ReviewRun.STATUS_DONE
        review_run.finished_at = timezone.now()
        review_run.summary = summary
        review_run.save(update_fields=["status", "finished_at", "summary"])
    except Exception as e:
        error_text = str(e).strip() or "Unknown error"
        if _looks_like_zai_auth_error(error_text):
            error_text = (
                "ZAI API key rejected (token expired or incorrect). "
                "Go to Account → API Keys and update your ZAI key."
            )
        logger.exception(
            "review.failed review_run_id=%s error=%s", review_run_id, error_text
        )
        body = (
            "❌ Review failed.\n\n"
            f"Error: `{error_text}`\n\n"
            "Troubleshooting:\n"
            "- If this is an API key issue, set it at Account → API Keys.\n"
            "- If this is an OpenCode install/runtime issue, ensure `opencode` is "
            "present and runnable in the worker image."
        )
        try:
            github.update_issue_comment(
                installation_id=installation.installation_id,
                auth=auth,
                repo_full_name=repository.full_name,
                comment_id=placeholder_comment_id,
                body=body,
            )
        except Exception:
            pass

        review_comment.body = body
        review_comment.save(update_fields=["body"])

        review_run.status = ReviewRun.STATUS_FAILED
        review_run.finished_at = timezone.now()
        review_run.error_message = error_text
        review_run.save(update_fields=["status", "finished_at", "error_message"])


@shared_task
def handle_chat_response(pull_request_id: int, comment_body: str) -> None:
    """Backward-compatible chat task signature.

    Prefer calling `handle_chat_response_v2(pull_request_id, chat_message_id)`.
    """
    pull_request = PullRequest.objects.get(id=pull_request_id)
    message = (
        pull_request.chat_messages.filter(body=comment_body)
        .order_by("-created_at")
        .first()
    )
    if not message:
        message = ChatMessage.objects.create(
            pull_request=pull_request,
            author="unknown",
            body=comment_body,
            github_comment_id=int(timezone.now().timestamp() * 1_000_000),
        )
    handle_chat_response_v2(pull_request_id=pull_request_id, chat_message_id=message.id)


@shared_task
def handle_chat_response_v2(pull_request_id: int, chat_message_id: int) -> None:
    pull_request = PullRequest.objects.select_related(
        "repository__installation__github_app__owner"
    ).get(id=pull_request_id)
    repository = pull_request.repository
    installation = repository.installation
    auth = github.auth_for_installation(installation)

    chat_message = ChatMessage.objects.get(
        id=chat_message_id, pull_request=pull_request
    )
    user_query = _extract_user_query(chat_message.body)

    try:
        owner = getattr(installation.github_app, "owner", None)
        if not owner:
            raise RuntimeError(
                "This installation is not associated with a user-owned GitHub App."
            )
        api_key = (
            UserApiKey.objects.filter(
                user=owner,
                provider=UserApiKey.PROVIDER_ZAI,
                is_active=True,
            )
            .order_by("-updated_at")
            .values_list("api_key", flat=True)
            .first()
        )
        api_key = (api_key or "").strip()
        if not api_key:
            raise RuntimeError(
                "Missing ZAI API key for this user. Go to Account → API Keys and set it."
            )

        rules_text = _build_rules_text(owner=owner, repository=repository)
        conversation_md = _render_conversation_markdown(
            pull_request=pull_request,
            upto=chat_message,
        )
        latest_review_summary = _latest_review_summary(pull_request=pull_request)

        token = github.get_installation_token(installation.installation_id, auth)
        pr_json = github.fetch_pull_request_json(
            installation_id=installation.installation_id,
            auth=auth,
            repo_full_name=repository.full_name,
            pull_number=pull_request.pr_number,
            token=token,
        )
        head_sha = str(((pr_json.get("head") or {}).get("sha")) or "").strip()

        diff_text = github.fetch_pull_request_diff(
            installation_id=installation.installation_id,
            auth=auth,
            repo_full_name=repository.full_name,
            pull_number=pull_request.pr_number,
            token=token,
        )

        max_diff_chars = 120_000
        diff_note = ""
        if len(diff_text) > max_diff_chars:
            diff_note = (
                f"\n\nNOTE: Diff truncated to {max_diff_chars} characters for chat."
            )
            diff_text = diff_text[:max_diff_chars]

        prompt = (
            "You are an AI assistant replying as a GitHub PR issue comment.\n"
            "Use the attached PR context files (conversation, latest review summary, "
            "PR diff, and a repository snapshot) to answer the user's request.\n"
            "Be crisp and actionable. Prefer pointing to specific files/lines.\n"
            "If something is uncertain or missing, ask a clarifying question instead of guessing.\n\n"
            "Repository access:\n"
            "- `repo_snapshot.md` (repo snapshot metadata)\n"
            "- `repo_index.md` (full file listing)\n"
            "You can read any file in the repository via the OpenCode project workspace.\n\n"
            "Project rules / preferences:\n"
            f"{rules_text}\n\n"
            "PR:\n"
            f"- Repo: {repository.full_name}\n"
            f"- PR: #{pull_request.pr_number} — {pull_request.title}\n"
            f"- URL: {pull_request.html_url}\n"
            f"- Head SHA: {head_sha or '(unknown)'}\n\n"
            "User request (most recent message that mentioned @codereview):\n"
            f"{user_query or '(no explicit question provided)'}\n\n"
            "Task:\n"
            "- Reply in Markdown suitable for a single GitHub comment.\n"
            "- Use the conversation context to stay consistent.\n"
            "- If the user asks for a re-review or deeper check, focus on the requested areas.\n"
            f"{diff_note}"
        )

        with tempfile.TemporaryDirectory(prefix="codereview-ai-chat-") as tmpdir:
            tmp_path = Path(tmpdir)
            context_files: list[Path] = []

            pr_path = tmp_path / "pull_request.md"
            pr_path.write_text(
                _render_pr_context_markdown(
                    pull_request=pull_request,
                    pr_json=pr_json,
                    head_sha=head_sha,
                ),
                encoding="utf-8",
            )
            context_files.append(pr_path)

            conversation_path = tmp_path / "conversation.md"
            conversation_path.write_text(conversation_md, encoding="utf-8")
            context_files.append(conversation_path)

            review_path = tmp_path / "latest_review_summary.md"
            review_path.write_text(latest_review_summary, encoding="utf-8")
            context_files.append(review_path)

            diff_path = tmp_path / "pull_request.diff"
            diff_path.write_text(diff_text, encoding="utf-8")
            context_files.append(diff_path)

            repo_dir, repo_snapshot_md = _prepare_repo_snapshot(
                tmp_path=tmp_path,
                repo_full_name=repository.full_name,
                head_sha=head_sha,
                token=token,
            )
            repo_snapshot_path = tmp_path / "repo_snapshot.md"
            repo_snapshot_path.write_text(repo_snapshot_md, encoding="utf-8")
            context_files.append(repo_snapshot_path)
            repo_index_path = tmp_path / "repo_index.md"
            if repo_dir is not None:
                repo_index_path.write_text(
                    _render_repo_index_markdown(repo_dir=repo_dir),
                    encoding="utf-8",
                )
                context_files.append(repo_index_path)

            result = run_opencode(
                message=prompt,
                files=context_files,
                env={"ZAI_API_KEY": api_key},
                cwd=repo_dir,
            )

        response = result.text.strip()
        if not response:
            response = (
                "I couldn't generate a response from the model output. "
                "Can you rephrase your question or point me at specific files/areas?"
            )

        max_comment_chars = 60_000
        if len(response) > max_comment_chars:
            response = response[:max_comment_chars].rstrip() + "\n\n_(truncated)_"

        reply_comment_id = github.post_issue_comment(
            installation_id=installation.installation_id,
            auth=auth,
            repo_full_name=repository.full_name,
            issue_number=pull_request.pr_number,
            body=response,
        )

        ChatMessage.objects.update_or_create(
            github_comment_id=reply_comment_id,
            defaults={
                "pull_request": pull_request,
                "author": "codereview",
                "body": response,
                "is_hidden": False,
            },
        )
    except Exception as e:
        error_text = str(e).strip() or "Unknown error"
        if _looks_like_zai_auth_error(error_text):
            error_text = (
                "ZAI API key rejected (token expired or incorrect). "
                "Go to Account → API Keys and update your ZAI key."
            )
        logger.exception(
            "chat.failed pull_request_id=%s chat_message_id=%s error=%s",
            pull_request_id,
            chat_message_id,
            error_text,
        )
        body = (
            "❌ Reply failed.\n\n"
            f"Error: `{error_text}`\n\n"
            "Troubleshooting:\n"
            "- If this is an API key issue, set it at Account → API Keys.\n"
            "- If this is an OpenCode install/runtime issue, ensure `opencode` is "
            "present and runnable in the worker image."
        )
        try:
            github.post_issue_comment(
                installation_id=installation.installation_id,
                auth=auth,
                repo_full_name=repository.full_name,
                issue_number=pull_request.pr_number,
                body=body,
            )
        except Exception:
            pass


def _looks_like_zai_auth_error(message: str) -> bool:
    normalized = message.strip().lower()
    return any(substr in normalized for substr in _ZAI_TOKEN_ERROR_SUBSTRINGS)


def _extract_user_query(body: str) -> str:
    normalized = body.strip()
    normalized = re.sub(r"(?i)@codereview\b", "", normalized).strip()
    normalized = re.sub(r"^[\s,:;-]+", "", normalized).strip()
    return normalized


def _build_rules_text(*, owner: object, repository: GithubRepository) -> str:
    global_rule_sets = RuleSet.objects.prefetch_related("rules").filter(
        owner=owner,
        scope=RuleSet.SCOPE_GLOBAL,
        is_active=True,
    )
    repo_rule_sets = RuleSet.objects.prefetch_related("rules").filter(
        owner=owner,
        scope=RuleSet.SCOPE_REPO,
        repository=repository,
        is_active=True,
    )

    instruction_blocks: list[str] = []
    for rule_set in [*global_rule_sets, *repo_rule_sets]:
        instructions = rule_set.instructions.strip()
        if instructions:
            instruction_blocks.append(f"- {rule_set.name}: {instructions}")
        for rule in rule_set.rules.filter(is_active=True).all():
            instruction_blocks.append(
                f"- [{rule.severity}] {rule.title}: {rule.description.strip()}"
            )

    return (
        "\n".join(instruction_blocks)
        if instruction_blocks
        else "- (no rules configured)"
    )


def _latest_review_summary(*, pull_request: PullRequest) -> str:
    latest = (
        pull_request.review_runs.filter(status=ReviewRun.STATUS_DONE)
        .order_by("-id")
        .first()
    )
    if not latest:
        return "No completed automated review found yet."
    comment = latest.comments.order_by("-id").first()
    if comment and comment.body.strip():
        return comment.body.strip()
    if latest.summary.strip():
        return latest.summary.strip()
    return "Latest review run exists, but no summary/comment body was stored."


def _render_conversation_markdown(
    *, pull_request: PullRequest, upto: ChatMessage, limit: int = 30
) -> str:
    messages = (
        pull_request.chat_messages.filter(
            is_hidden=False,
            created_at__lte=upto.created_at,
        )
        .order_by("-created_at")
        .all()[:limit]
    )
    ordered = list(reversed(list(messages)))

    lines: list[str] = [
        f"# Conversation (last {len(ordered)} messages)",
        "",
        f"PR: {pull_request.repository.full_name}#{pull_request.pr_number}",
        "",
    ]
    for msg in ordered:
        lines.append(f"## {msg.author} — {msg.created_at.isoformat()}")
        lines.append("")
        lines.append(msg.body.strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _render_pr_context_markdown(
    *, pull_request: PullRequest, pr_json: dict, head_sha: str
) -> str:
    base = pr_json.get("base") or {}
    head = pr_json.get("head") or {}
    base_ref = str(base.get("ref") or "").strip()
    head_ref = str(head.get("ref") or "").strip()
    body = str(pr_json.get("body") or "").strip()
    if body:
        body = body[:20_000].rstrip()
    else:
        body = "(empty)"
    return (
        "# Pull Request Context\n\n"
        f"- Repo: {pull_request.repository.full_name}\n"
        f"- PR: #{pull_request.pr_number}\n"
        f"- Title: {pull_request.title}\n"
        f"- URL: {pull_request.html_url}\n"
        f"- Base: {base_ref or '(unknown)'}\n"
        f"- Head: {head_ref or '(unknown)'}\n"
        f"- Head SHA: {head_sha or '(unknown)'}\n\n"
        "## PR description\n\n"
        f"{body}\n"
    )


def _fetch_and_write_pr_files(
    *,
    repo_root: Path,
    installation_id: int,
    auth: github.GithubAppAuth,
    repo_full_name: str,
    pull_number: int,
    head_sha: str,
    token: str | None,
    max_files: int = 20,
    max_total_chars: int = 120_000,
    max_file_chars: int = 20_000,
) -> tuple[list[Path], str]:
    if not head_sha:
        return ([], "# Attached files\n\n- Skipped: could not determine PR head SHA.\n")

    repo_root.mkdir(parents=True, exist_ok=True)
    repo_root_resolved = repo_root.resolve()

    files = github.list_pull_request_files(
        installation_id=installation_id,
        auth=auth,
        repo_full_name=repo_full_name,
        pull_number=pull_number,
        limit=200,
        token=token,
    )

    attached: list[Path] = []
    skipped: list[str] = []
    remaining = max_total_chars

    for item in files:
        if len(attached) >= max_files or remaining <= 0:
            break
        filename = str(item.get("filename") or "").strip()
        status = str(item.get("status") or "").strip()
        if not filename:
            continue
        if status in {"removed"}:
            skipped.append(f"- `{filename}` (removed)")
            continue
        if filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip")):
            skipped.append(f"- `{filename}` (binary/non-text)")
            continue

        content = github.fetch_repository_file_text(
            installation_id=installation_id,
            auth=auth,
            repo_full_name=repo_full_name,
            path=filename,
            ref=head_sha,
            max_bytes=200_000,
            token=token,
        )
        if content is None:
            skipped.append(f"- `{filename}` (unavailable or non-text)")
            continue

        content = content.strip("\n")
        truncated = False
        if len(content) > max_file_chars:
            content = content[:max_file_chars].rstrip()
            truncated = True

        if len(content) > remaining:
            skipped.append(f"- `{filename}` (budget exceeded)")
            continue

        target = (repo_root / filename).resolve()
        if not target.is_relative_to(repo_root_resolved):
            skipped.append(f"- `{filename}` (invalid path)")
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        if truncated:
            content = content + "\n\n# NOTE: truncated\n"
        target.write_text(content + "\n", encoding="utf-8")
        attached.append(target)
        remaining -= len(content)

    lines: list[str] = ["# Attached files", ""]
    if attached:
        lines.append("## Included")
        lines.append("")
        for path in attached:
            rel = path.relative_to(repo_root_resolved)
            lines.append(f"- `{rel.as_posix()}`")
        lines.append("")
    if skipped:
        lines.append("## Skipped")
        lines.append("")
        lines.extend(skipped)
        lines.append("")
    if not attached and not skipped:
        lines.append("- No files were listed for this PR.")
        lines.append("")
    return attached, "\n".join(lines).strip() + "\n"


def _prepare_repo_snapshot(
    *,
    tmp_path: Path,
    repo_full_name: str,
    head_sha: str,
    token: str,
) -> tuple[Path | None, str]:
    if not head_sha:
        return (
            None,
            "# Repository snapshot\n\n- Skipped: could not determine PR head SHA.\n",
        )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    method = ""
    error: str | None = None
    try:
        _git_checkout_repo_at_sha(
            repo_dir=repo_dir,
            repo_full_name=repo_full_name,
            head_sha=head_sha,
            token=token,
        )
        method = "git (shallow fetch depth=1)"
    except Exception as e:
        error = str(e).strip() or "unknown error"
        try:
            shutil.rmtree(repo_dir, ignore_errors=True)
            repo_dir.mkdir(parents=True, exist_ok=True)
            _download_and_extract_zipball(
                repo_dir=repo_dir,
                repo_full_name=repo_full_name,
                head_sha=head_sha,
                token=token,
            )
            method = "zipball (GitHub API)"
            error = None
        except Exception as e2:
            error = str(e2).strip() or error
            shutil.rmtree(repo_dir, ignore_errors=True)
            return (
                None,
                "# Repository snapshot\n\n"
                f"- Repo: `{repo_full_name}`\n"
                f"- Ref: `{head_sha}`\n"
                "- Failed to prepare a full codebase snapshot.\n"
                f"- Error: `{error}`\n",
            )

    stats = _repo_stats(repo_dir=repo_dir)
    lines: list[str] = [
        "# Repository snapshot",
        "",
        f"- Repo: `{repo_full_name}`",
        f"- Ref: `{head_sha}`",
        f"- Method: {method}",
        "- OpenCode working dir: `repo/` (full working tree; `.git` metadata excluded when using git)",
        "- Attached: `repo_index.md` (file listing)",
    ]
    if stats:
        file_count, total_bytes = stats
        lines.append(f"- Files: {file_count:,}")
        lines.append(f"- Size: {total_bytes / (1024 * 1024):.1f} MiB")
    return repo_dir, "\n".join(lines).strip() + "\n"


def _git_checkout_repo_at_sha(
    *,
    repo_dir: Path,
    repo_full_name: str,
    head_sha: str,
    token: str,
    timeout_seconds: float = 120,
) -> None:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is not installed or not on PATH")

    remote_url = f"https://x-access-token@github.com/{repo_full_name}.git"
    askpass_path = repo_dir.parent / "git_askpass.sh"
    askpass_path.write_text(
        "#!/bin/sh\n"
        'exec printf "%s" "${GITHUB_TOKEN:-}"\n',
        encoding="utf-8",
    )
    askpass_path.chmod(0o700)

    env = os.environ.copy()
    env.update(
        {
            "GIT_ASKPASS": str(askpass_path),
            "GIT_TERMINAL_PROMPT": "0",
            "GITHUB_TOKEN": token,
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )

    def run(args: list[str]) -> None:
        proc = subprocess.run(
            args,
            cwd=repo_dir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(stderr or f"git failed: {' '.join(args[:2])}")

    run([git, "init", "-q"])
    run([git, "remote", "add", "origin", remote_url])
    run([git, "fetch", "--depth", "1", "--no-tags", "origin", head_sha])
    run([git, "checkout", "--detach", "FETCH_HEAD"])

    shutil.rmtree(repo_dir / ".git", ignore_errors=True)
    try:
        askpass_path.unlink(missing_ok=True)
    except Exception:
        pass


def _download_and_extract_zipball(
    *,
    repo_dir: Path,
    repo_full_name: str,
    head_sha: str,
    token: str,
    max_total_bytes: int = 512 * 1024 * 1024,
) -> None:
    zip_path = repo_dir.parent / "repo.zip"
    github.download_repository_zipball(
        repo_full_name=repo_full_name,
        ref=head_sha,
        token=token,
        dest_path=zip_path,
        timeout_seconds=120,
    )
    _extract_zipball_to_repo_dir(
        zip_path=zip_path,
        repo_dir=repo_dir,
        max_total_bytes=max_total_bytes,
    )
    try:
        zip_path.unlink(missing_ok=True)
    except Exception:
        pass


def _extract_zipball_to_repo_dir(
    *,
    zip_path: Path,
    repo_dir: Path,
    max_total_bytes: int,
) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    repo_dir_resolved = repo_dir.resolve()

    extracted_bytes = 0
    with zipfile.ZipFile(zip_path) as zf:
        names = [name for name in zf.namelist() if name and not name.endswith("/")]
        prefix = ""
        if names:
            first = names[0].split("/", 1)[0]
            if first and all(name.startswith(first + "/") for name in names):
                prefix = first + "/"

        for info in zf.infolist():
            name = (info.filename or "").replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            if prefix and name.startswith(prefix):
                rel_name = name[len(prefix) :]
            else:
                rel_name = name
            rel_name = rel_name.lstrip("/")
            if not rel_name:
                continue

            extracted_bytes += int(getattr(info, "file_size", 0) or 0)
            if extracted_bytes > max_total_bytes:
                break

            target = (repo_dir / rel_name).resolve()
            if not target.is_relative_to(repo_dir_resolved):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _repo_stats(*, repo_dir: Path) -> tuple[int, int] | None:
    try:
        file_count = 0
        total_bytes = 0
        for root, _dirs, files in os.walk(repo_dir):
            root_path = Path(root)
            for name in files:
                path = root_path / name
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                if not path.is_file():
                    continue
                file_count += 1
                total_bytes += int(st.st_size or 0)
        return file_count, total_bytes
    except Exception:
        return None


def _render_repo_index_markdown(*, repo_dir: Path, max_paths: int = 8_000) -> str:
    repo_dir_resolved = repo_dir.resolve()
    paths: list[str] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [
            d
            for d in dirs
            if d
            not in {
                ".git",
                ".hg",
                ".svn",
                "node_modules",
                ".venv",
                "venv",
                "__pycache__",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "dist",
                "build",
                ".tox",
            }
        ]
        root_path = Path(root)
        for name in files:
            if not name:
                continue
            path = (root_path / name).resolve()
            if not path.is_relative_to(repo_dir_resolved):
                continue
            rel = path.relative_to(repo_dir_resolved).as_posix()
            paths.append(rel)
            if len(paths) >= max_paths:
                break
        if len(paths) >= max_paths:
            break

    paths.sort()
    lines: list[str] = [
        "# Repository file index",
        "",
        f"- Root: `{repo_dir_resolved.as_posix()}`",
        f"- Listed: {len(paths):,} paths",
        "",
        "## Paths",
        "",
    ]
    if not paths:
        lines.append("- (no files found)")
    else:
        lines.extend([f"- `{p}`" for p in paths])
        if len(paths) >= max_paths:
            lines.append("")
            lines.append(f"_Index truncated to {max_paths:,} paths._")
    return "\n".join(lines).strip() + "\n"
