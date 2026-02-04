from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from celery import shared_task
from django.utils import timezone

from . import github
from .models import RuleSet, UserApiKey
from .models import PullRequest, ReviewComment, ReviewRun
from .opencode_client import run_opencode

logger = logging.getLogger(__name__)


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
        if not api_key:
            raise RuntimeError(
                "Missing ZAI API key for this user. Go to Account → API Keys and set it."
            )

        diff_text = github.fetch_pull_request_diff(
            installation_id=installation.installation_id,
            auth=auth,
            repo_full_name=repository.full_name,
            pull_number=pull_request.pr_number,
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
            "Project rules / preferences:\n"
            f"{rules_text}\n\n"
            "Task:\n"
            "- Review the attached PR diff.\n"
            "- Call out correctness, security, performance, and maintainability issues.\n"
            "- If something is uncertain, ask a question instead of guessing.\n"
            "- Output Markdown suitable for a single GitHub comment.\n"
            f"{diff_note}"
        )

        with tempfile.TemporaryDirectory(prefix="codereview-ai-") as tmpdir:
            diff_path = Path(tmpdir) / "pull_request.diff"
            diff_path.write_text(diff_text, encoding="utf-8")
            result = run_opencode(
                message=prompt,
                files=[diff_path],
                env={"ZAI_API_KEY": api_key},
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
        logger.exception(
            "review.failed review_run_id=%s error=%s", review_run_id, error_text
        )
        body = (
            "❌ Review failed.\n\n"
            f"Error: `{error_text}`\n\n"
            "If this is an API key issue, set it at Account → API Keys."
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
    pull_request = PullRequest.objects.select_related("repository__installation").get(
        id=pull_request_id
    )
    repository = pull_request.repository
    installation = repository.installation
    auth = github.auth_for_installation(installation)

    # Placeholder response, replace with AI assistant.
    response = "Thanks for the message. I have saved your feedback and will adjust future reviews accordingly."
    github.post_issue_comment(
        installation_id=installation.installation_id,
        auth=auth,
        repo_full_name=repository.full_name,
        issue_number=pull_request.pr_number,
        body=response,
    )
