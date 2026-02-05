from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from .models import (
    GithubApp,
    GithubInstallation,
    GithubRepository,
    PullRequest,
    ReviewRun,
)
from .views import _flash_messages


class FlashMessagesTest(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    def _request_with_messages(self):
        request = self.factory.get("/")
        SessionMiddleware(lambda _request: None).process_request(request)
        MessageMiddleware(lambda _request: None).process_request(request)
        return request

    def test_flash_messages_renders_each_message_once(self) -> None:
        request = self._request_with_messages()
        messages.success(request, "Repo filtered.")
        html = str(_flash_messages(request))
        assert html.count("Repo filtered.") == 1


class ReviewRunVisibilityTest(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="alice", password="pw")
        self.other_user = User.objects.create_user(username="bob", password="pw")

        self.github_app = GithubApp.objects.create(
            owner=self.user,
            desired_name="Alice App",
            status=GithubApp.STATUS_READY,
            slug="alice-app",
        )
        self.installation = GithubInstallation.objects.create(
            github_app=self.github_app,
            installation_id=123,
            account_login="alice-org",
            account_type="Organization",
            target_type="Organization",
            permissions={},
            events=[],
            is_active=True,
        )
        self.repo = GithubRepository.objects.create(
            installation=self.installation,
            full_name="alice-org/repo",
            repo_id=99,
            html_url="https://github.com/alice-org/repo",
            private=False,
            default_branch="main",
            is_active=True,
        )
        now = timezone.now()
        self.pull_request = PullRequest.objects.create(
            repository=self.repo,
            pr_number=1,
            pr_id=111,
            title="Test PR",
            state="open",
            html_url="https://github.com/alice-org/repo/pull/1",
            last_reviewed_sha="",
            created_at=now,
            updated_at=now,
        )
        self.review_run = ReviewRun.objects.create(
            pull_request=self.pull_request,
            head_sha="abcdef1234567890",
            status=ReviewRun.STATUS_FAILED,
            error_message="boom",
            summary="summary text",
        )

    def test_dashboard_shows_operational_visibility_section(self) -> None:
        self.client.force_login(self.user)
        resp = self.client.get("/app")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Operational visibility" in body
        assert "alice-org/repo" in body
        assert "Failures" in body

    def test_review_run_detail_requires_auth(self) -> None:
        resp = self.client.get(f"/app/review-runs/{self.review_run.id}")
        assert resp.status_code == 200
        assert "Sign in required" in resp.content.decode()

    def test_review_run_detail_shows_metadata_for_owner(self) -> None:
        self.client.force_login(self.user)
        resp = self.client.get(f"/app/review-runs/{self.review_run.id}")
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Run metadata" in body
        assert "abcdef1234567890" in body
        assert "boom" in body

    def test_review_run_detail_404_for_other_user(self) -> None:
        self.client.force_login(self.other_user)
        resp = self.client.get(f"/app/review-runs/{self.review_run.id}")
        assert resp.status_code == 404
