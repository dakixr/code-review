from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from . import github
from .models import GithubRepository, PullRequest, ReviewComment, ReviewRun


@shared_task
def run_pr_review(review_run_id: int) -> None:
    review_run = ReviewRun.objects.select_related("pull_request__repository__installation").get(id=review_run_id)
    review_run.status = ReviewRun.STATUS_RUNNING
    review_run.started_at = timezone.now()
    review_run.save(update_fields=["status", "started_at"])

    pull_request = review_run.pull_request
    repository = pull_request.repository
    installation = repository.installation

    placeholder_comment_id = github.post_issue_comment(
        installation_id=installation.installation_id,
        repo_full_name=repository.full_name,
        issue_number=pull_request.pr_number,
        body="👁 Reviewing this PR now. I will post a full review shortly.",
    )

    ReviewComment.objects.create(
        review_run=review_run,
        body="👁 Reviewing this PR now. I will post a full review shortly.",
        github_comment_id=placeholder_comment_id,
    )

    # TODO: Replace with real AI review pipeline.
    summary = (
        "Initial automated review completed.\n\n"
        "- No blocking issues detected in this placeholder run.\n"
        "- Configure rules to make the reviewer stricter."
    )
    github.update_issue_comment(
        installation_id=installation.installation_id,
        repo_full_name=repository.full_name,
        comment_id=placeholder_comment_id,
        body=summary,
    )

    review_run.status = ReviewRun.STATUS_DONE
    review_run.finished_at = timezone.now()
    review_run.summary = summary
    review_run.save(update_fields=["status", "finished_at", "summary"])


@shared_task
def handle_chat_response(pull_request_id: int, comment_body: str) -> None:
    pull_request = PullRequest.objects.select_related("repository__installation").get(id=pull_request_id)
    repository = pull_request.repository
    installation = repository.installation

    # Placeholder response, replace with AI assistant.
    response = (
        "Thanks for the message. I have saved your feedback and will adjust future reviews accordingly."
    )
    github.post_issue_comment(
        installation_id=installation.installation_id,
        repo_full_name=repository.full_name,
        issue_number=pull_request.pr_number,
        body=response,
    )
