from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="GithubUser",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("login", models.CharField(max_length=255, unique=True)),
                ("github_id", models.BigIntegerField(unique=True)),
                ("avatar_url", models.URLField(blank=True)),
                ("html_url", models.URLField(blank=True)),
                ("name", models.CharField(blank=True, max_length=255)),
                ("email", models.EmailField(blank=True, max_length=254)),
            ],
        ),
        migrations.CreateModel(
            name="GithubInstallation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("installation_id", models.BigIntegerField(unique=True)),
                ("account_login", models.CharField(max_length=255)),
                ("account_type", models.CharField(max_length=32)),
                ("target_type", models.CharField(max_length=32)),
                ("permissions", models.JSONField(blank=True, default=dict)),
                ("events", models.JSONField(blank=True, default=list)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "installed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="installations",
                        to="web.githubuser",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="GithubRepository",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("full_name", models.CharField(max_length=255, unique=True)),
                ("repo_id", models.BigIntegerField(unique=True)),
                ("html_url", models.URLField(blank=True)),
                ("private", models.BooleanField(default=False)),
                ("default_branch", models.CharField(default="main", max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                (
                    "installation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="repositories",
                        to="web.githubinstallation",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RuleSet",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("scope", models.CharField(choices=[("global", "Global"), ("repo", "Repository")], max_length=16)),
                ("instructions", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rule_sets",
                        to="web.githubuser",
                    ),
                ),
                (
                    "repository",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rule_sets",
                        to="web.githubrepository",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Rule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=255)),
                ("description", models.TextField()),
                ("severity", models.CharField(default="info", max_length=32)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "rule_set",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rules",
                        to="web.ruleset",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PullRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pr_number", models.IntegerField()),
                ("pr_id", models.BigIntegerField()),
                ("title", models.CharField(max_length=512)),
                ("state", models.CharField(max_length=32)),
                ("html_url", models.URLField(blank=True)),
                ("last_reviewed_sha", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField()),
                (
                    "opened_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pull_requests",
                        to="web.githubuser",
                    ),
                ),
                (
                    "repository",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pull_requests",
                        to="web.githubrepository",
                    ),
                ),
            ],
            options={"unique_together": {("repository", "pr_number")}},
        ),
        migrations.CreateModel(
            name="ReviewRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("head_sha", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                        ],
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("summary", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True)),
                (
                    "pull_request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="review_runs",
                        to="web.pullrequest",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ReviewComment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("body", models.TextField()),
                ("github_comment_id", models.BigIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "review_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="web.reviewrun",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="FeedbackSignal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "signal",
                    models.CharField(
                        choices=[("like", "Like"), ("ignore", "Ignore"), ("dislike", "Dislike")],
                        max_length=16,
                    ),
                ),
                ("source", models.CharField(default="comment", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "review_comment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feedback",
                        to="web.reviewcomment",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ChatMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("author", models.CharField(max_length=255)),
                ("body", models.TextField()),
                ("github_comment_id", models.BigIntegerField(unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "pull_request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="chat_messages",
                        to="web.pullrequest",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="AppSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=255, unique=True)),
                ("value", models.TextField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="UserProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("github_login", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
