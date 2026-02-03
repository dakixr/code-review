from __future__ import annotations

from django.conf import settings
from django.db import models


class GithubUser(models.Model):
    login = models.CharField[str, str](max_length=255, unique=True)
    github_id = models.BigIntegerField[int, int](unique=True)
    avatar_url = models.URLField[str, str](blank=True)
    html_url = models.URLField[str, str](blank=True)
    name = models.CharField[str, str](max_length=255, blank=True)
    email = models.EmailField[str, str](blank=True)

    def __str__(self) -> str:
        return self.login


class GithubInstallation(models.Model):
    installation_id = models.BigIntegerField[int, int](unique=True)
    account_login = models.CharField[str, str](max_length=255)
    account_type = models.CharField[str, str](max_length=32)
    target_type = models.CharField[str, str](max_length=32)
    permissions = models.JSONField[dict, dict](default=dict, blank=True)
    events = models.JSONField[list, list](default=list, blank=True)
    is_active = models.BooleanField[bool, bool](default=True)
    installed_by = models.ForeignKey["GithubUser", "GithubUser"](
        GithubUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="installations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.account_login} ({self.installation_id})"


class GithubRepository(models.Model):
    installation = models.ForeignKey["GithubInstallation", "GithubInstallation"](
        GithubInstallation, on_delete=models.CASCADE, related_name="repositories"
    )
    full_name = models.CharField[str, str](max_length=255, unique=True)
    repo_id = models.BigIntegerField[int, int](unique=True)
    html_url = models.URLField[str, str](blank=True)
    private = models.BooleanField[bool, bool](default=False)
    default_branch = models.CharField[str, str](max_length=255, default="main")
    is_active = models.BooleanField[bool, bool](default=True)

    def __str__(self) -> str:
        return self.full_name


class RuleSet(models.Model):
    SCOPE_GLOBAL = "global"
    SCOPE_REPO = "repo"
    SCOPE_CHOICES = [
        (SCOPE_GLOBAL, "Global"),
        (SCOPE_REPO, "Repository"),
    ]

    name = models.CharField[str, str](max_length=255)
    scope = models.CharField[str, str](max_length=16, choices=SCOPE_CHOICES)
    repository = models.ForeignKey["GithubRepository", "GithubRepository"](
        GithubRepository,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="rule_sets",
    )
    instructions = models.TextField(blank=True)
    is_active = models.BooleanField[bool, bool](default=True)
    created_by = models.ForeignKey["GithubUser", "GithubUser"](
        GithubUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rule_sets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Rule(models.Model):
    rule_set = models.ForeignKey["RuleSet", "RuleSet"](
        RuleSet, on_delete=models.CASCADE, related_name="rules"
    )
    title = models.CharField[str, str](max_length=255)
    description = models.TextField()
    severity = models.CharField[str, str](max_length=32, default="info")
    is_active = models.BooleanField[bool, bool](default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.title


class PullRequest(models.Model):
    repository = models.ForeignKey["GithubRepository", "GithubRepository"](
        GithubRepository, on_delete=models.CASCADE, related_name="pull_requests"
    )
    pr_number = models.IntegerField[int, int]()
    pr_id = models.BigIntegerField[int, int]()
    title = models.CharField[str, str](max_length=512)
    state = models.CharField[str, str](max_length=32)
    html_url = models.URLField[str, str](blank=True)
    last_reviewed_sha = models.CharField[str, str](max_length=64, blank=True)
    opened_by = models.ForeignKey["GithubUser", "GithubUser"](
        GithubUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pull_requests",
    )
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        unique_together = ("repository", "pr_number")

    def __str__(self) -> str:
        return f"{self.repository.full_name}#{self.pr_number}"


class ReviewRun(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_DONE, "Done"),
        (STATUS_FAILED, "Failed"),
    ]

    pull_request = models.ForeignKey["PullRequest", "PullRequest"](
        PullRequest, on_delete=models.CASCADE, related_name="review_runs"
    )
    head_sha = models.CharField[str, str](max_length=64)
    status = models.CharField[str, str](max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    summary = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"{self.pull_request} @ {self.head_sha}"


class ReviewComment(models.Model):
    review_run = models.ForeignKey["ReviewRun", "ReviewRun"](
        ReviewRun, on_delete=models.CASCADE, related_name="comments"
    )
    body = models.TextField()
    github_comment_id = models.BigIntegerField[int, int](null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class FeedbackSignal(models.Model):
    SIGNAL_LIKE = "like"
    SIGNAL_IGNORE = "ignore"
    SIGNAL_DISLIKE = "dislike"
    SIGNAL_CHOICES = [
        (SIGNAL_LIKE, "Like"),
        (SIGNAL_IGNORE, "Ignore"),
        (SIGNAL_DISLIKE, "Dislike"),
    ]

    review_comment = models.ForeignKey["ReviewComment", "ReviewComment"](
        ReviewComment, on_delete=models.CASCADE, related_name="feedback"
    )
    signal = models.CharField[str, str](max_length=16, choices=SIGNAL_CHOICES)
    source = models.CharField[str, str](max_length=64, default="comment")
    created_at = models.DateTimeField(auto_now_add=True)


class ChatMessage(models.Model):
    pull_request = models.ForeignKey["PullRequest", "PullRequest"](
        PullRequest, on_delete=models.CASCADE, related_name="chat_messages"
    )
    author = models.CharField[str, str](max_length=255)
    body = models.TextField()
    github_comment_id = models.BigIntegerField[int, int](unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AppSetting(models.Model):
    key = models.CharField[str, str](max_length=255, unique=True)
    value = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.key


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    github_login = models.CharField[str, str](max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.user.username
