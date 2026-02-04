from __future__ import annotations

from uuid import uuid4

from django.conf import settings
from django.db import models


class GithubApp(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_READY = "ready"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_READY, "Ready"),
    ]

    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="github_apps",
    )
    desired_name = models.CharField[str, str](max_length=255)
    status = models.CharField[str, str](
        max_length=16, choices=STATUS_CHOICES, default=STATUS_DRAFT
    )

    app_id = models.BigIntegerField[int, int](null=True, blank=True)
    slug = models.CharField[str, str](max_length=255, blank=True)
    client_id = models.CharField[str, str](max_length=255, blank=True)
    client_secret = models.CharField[str, str](max_length=255, blank=True)
    webhook_secret = models.CharField[str, str](max_length=255, blank=True)
    private_key_pem = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.slug or self.desired_name


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
    github_app = models.ForeignKey["GithubApp", "GithubApp"](
        GithubApp,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="installations",
    )
    installation_id = models.BigIntegerField[int, int]()
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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["github_app", "installation_id"],
                name="github_installation_per_app",
            )
        ]

    def __str__(self) -> str:
        return f"{self.account_login} ({self.installation_id})"


class GithubRepository(models.Model):
    installation = models.ForeignKey["GithubInstallation", "GithubInstallation"](
        GithubInstallation, on_delete=models.CASCADE, related_name="repositories"
    )
    full_name = models.CharField[str, str](max_length=255)
    repo_id = models.BigIntegerField[int, int]()
    html_url = models.URLField[str, str](blank=True)
    private = models.BooleanField[bool, bool](default=False)
    default_branch = models.CharField[str, str](max_length=255, default="main")
    is_active = models.BooleanField[bool, bool](default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["installation", "repo_id"],
                name="github_repo_per_installation",
            )
        ]

    def __str__(self) -> str:
        return self.full_name


class RuleSet(models.Model):
    SCOPE_GLOBAL = "global"
    SCOPE_REPO = "repo"
    SCOPE_CHOICES = [
        (SCOPE_GLOBAL, "Global"),
        (SCOPE_REPO, "Repository"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="rule_sets",
    )
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
    status = models.CharField[str, str](
        max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED
    )
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
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    github_login = models.CharField[str, str](max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.user.username


class UserApiKey(models.Model):
    PROVIDER_ZAI = "zai"
    PROVIDER_CHOICES = [
        (PROVIDER_ZAI, "ZAI / GLM"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_keys",
    )
    provider = models.CharField[str, str](max_length=32, choices=PROVIDER_CHOICES)
    api_key = models.TextField()
    is_active = models.BooleanField[bool, bool](default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"], name="api_key_per_user_provider"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.provider}"
