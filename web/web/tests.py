from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone
from django.core.management import call_command

from .models import (
    ChatMessage,
    GithubApp,
    GithubInstallation,
    GithubRepository,
    PullRequest,
    ReviewComment,
    ReviewRun,
    UserApiKey,
)
from . import github
from .opencode_client import _format_opencode_start_error, run_opencode
from .tasks import handle_chat_response_v2
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


class OpenCodeClientTest(SimpleTestCase):
    def test_missing_binary_raises_actionable_error(self) -> None:
        try:
            run_opencode(
                message="hello",
                env={
                    "OPENCODE_BIN": "/definitely-not-a-real-path/opencode",
                    "PATH": "",
                },
            )
        except RuntimeError as e:
            message = str(e)
            assert "OpenCode binary not found" in message
            assert "OPENCODE_BIN" in message
        else:
            raise AssertionError("Expected RuntimeError")

    def test_script_missing_interpreter_includes_hint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codereview-test-") as tmpdir:
            script_path = Path(tmpdir) / "opencode"
            script_path.write_text("#!/does/not/exist\n", encoding="utf-8")
            script_path.chmod(0o755)

            try:
                run_opencode(
                    message="hello",
                    env={"OPENCODE_BIN": str(script_path), "PATH": ""},
                )
            except RuntimeError as e:
                assert "interpreter may be missing" in str(e)
            else:
                raise AssertionError("Expected RuntimeError")

    def test_musl_linked_binary_message_hint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codereview-test-") as tmpdir:
            fake_bin = Path(tmpdir) / "opencode"
            fake_bin.write_bytes(b"\x7fELF.... /lib/ld-musl-x86_64.so.1 ...")
            message = _format_opencode_start_error(
                opencode_bin=str(fake_bin),
                merged_env={"PATH": ""},
            )
            assert "musl-linked" in message

    def test_invocation_separates_files_from_message(self) -> None:
        captured_args: list[str] = []

        def fake_run(
            args: list[str],
            *,
            env: dict[str, str],
            cwd: str | None,
            check: bool,
            stdin: object,
            capture_output: bool,
            text: bool,
            timeout: float,
        ):
            del env, cwd, check, stdin, capture_output, text, timeout
            captured_args.extend(args)

            class Result:
                returncode = 0
                stdout = (
                    '{"type":"message","message":{"role":"assistant","content":"ok"}}\n'
                )
                stderr = ""

            return Result()

        with tempfile.TemporaryDirectory(prefix="codereview-test-") as tmpdir:
            file_path = Path(tmpdir) / "pull_request.diff"
            file_path.write_text("diff --git a/a b/a\n", encoding="utf-8")

            from . import opencode_client as client

            original_run = client.subprocess.run
            client.subprocess.run = fake_run  # type: ignore[assignment]
            try:
                result = run_opencode(
                    message="hello world",
                    files=[file_path],
                    env={"OPENCODE_BIN": "/bin/echo"},
                )
            finally:
                client.subprocess.run = original_run  # type: ignore[assignment]

        assert result.text == "ok"
        assert "--file" in captured_args
        assert "--" in captured_args
        assert captured_args.index("--") < captured_args.index("hello world")

    def test_extracts_text_part_events(self) -> None:
        def fake_run(
            args: list[str],
            *,
            env: dict[str, str],
            cwd: str | None,
            check: bool,
            stdin: object,
            capture_output: bool,
            text: bool,
            timeout: float,
        ):
            del args, env, cwd, check, stdin, capture_output, text, timeout

            class Result:
                returncode = 0
                stdout = (
                    '{"type":"step_start","part":{"type":"step-start"}}\n'
                    '{"type":"text","part":{"type":"text","text":"hello from part"}}\n'
                    '{"type":"step_finish","part":{"type":"step-finish"}}\n'
                )
                stderr = ""

            return Result()

        from . import opencode_client as client

        original_run = client.subprocess.run
        client.subprocess.run = fake_run  # type: ignore[assignment]
        try:
            result = run_opencode(
                message="hello",
                env={"OPENCODE_BIN": "/bin/echo"},
            )
        finally:
            client.subprocess.run = original_run  # type: ignore[assignment]

        assert result.text == "hello from part"


class ChatResponseTaskTest(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="alice", password="pw")
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
        self.pull_request = PullRequest.objects.create(
            repository=self.repo,
            pr_number=1,
            pr_id=111,
            title="Test PR",
            state="open",
            html_url="https://github.com/alice-org/repo/pull/1",
            last_reviewed_sha="",
            created_at=timezone.now(),
            updated_at=timezone.now(),
        )
        self.chat_message = ChatMessage.objects.create(
            pull_request=self.pull_request,
            author="alice",
            body="@codereview can you double-check auth edge cases?",
            github_comment_id=555,
        )
        self.review_run = ReviewRun.objects.create(
            pull_request=self.pull_request,
            head_sha="abcdef1234567890",
            status=ReviewRun.STATUS_DONE,
            summary="review summary",
        )
        ReviewComment.objects.create(
            review_run=self.review_run,
            body="Automated review content",
            github_comment_id=777,
        )
        UserApiKey.objects.create(
            user=self.user,
            provider=UserApiKey.PROVIDER_ZAI,
            api_key="test-key",
            is_active=True,
        )

    def test_handle_chat_response_v2_uses_pr_and_conversation_context(self) -> None:
        from .github import GithubAppAuth
        from .opencode_client import OpenCodeResult

        captured: dict[str, object] = {}

        def fake_run_opencode(
            *,
            message: str,
            files: list[Path] | None,
            env: dict,
            cwd: Path | None,
            auth: dict[str, object] | None,
        ):
            captured["message"] = message
            captured["files"] = files or []
            captured["env"] = env
            captured["cwd"] = cwd
            captured["auth"] = auth
            return OpenCodeResult(text="Here is a contextual answer.")

        def fake_prepare_repo_snapshot(*, tmp_path: Path, **_kwargs):
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "README.md").write_text("# Repo\n", encoding="utf-8")
            return repo_dir, "# Repository snapshot\n\n- ok\n"

        fake_post = MagicMock(return_value=999)

        with (
            patch(
                "web.tasks.github.auth_for_installation",
                return_value=GithubAppAuth(
                    app_id="1",
                    private_key_pem="x",
                    webhook_secret="y",
                ),
            ),
            patch("web.tasks.github.post_issue_comment", fake_post),
            patch("web.tasks.github.get_installation_token", return_value="tok"),
            patch(
                "web.tasks.github.fetch_pull_request_json",
                return_value={
                    "head": {"sha": "deadbeef", "ref": "feature"},
                    "base": {"ref": "main"},
                    "body": "PR description",
                },
            ),
            patch(
                "web.tasks.github.fetch_pull_request_diff",
                return_value="diff --git a/a b/a\n",
            ),
            patch(
                "web.tasks._prepare_repo_snapshot",
                side_effect=fake_prepare_repo_snapshot,
            ),
            patch("web.tasks.run_opencode", side_effect=fake_run_opencode),
        ):
            handle_chat_response_v2(
                pull_request_id=self.pull_request.id,
                chat_message_id=self.chat_message.id,
            )

        assert fake_post.called
        assert "double-check auth edge cases" in str(captured["message"])
        assert "@codereview can you" not in str(captured["message"]).lower()

        files = captured["files"]
        assert isinstance(files, list)
        file_names = [Path(p).name for p in files]
        assert "conversation.md" in file_names
        assert "pull_request.diff" in file_names
        assert "latest_review_summary.md" in file_names
        assert "pull_request.md" in file_names
        assert "repo_snapshot.md" in file_names
        assert "repo_index.md" in file_names
        assert isinstance(captured.get("cwd"), Path)
        assert str(Path(captured["cwd"]).name).startswith("codereview-ai-chat-")
        assert captured.get("auth") == {
            "zai-coding-plan": {"type": "api", "key": "test-key"}
        }

        assert ChatMessage.objects.filter(
            pull_request=self.pull_request,
            github_comment_id=999,
            author="codereview",
        ).exists()


class OpenCodeProbeCommandTest(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="alice", password="pw")
        UserApiKey.objects.create(
            user=self.user,
            provider=UserApiKey.PROVIDER_ZAI,
            api_key="test-key",
            is_active=True,
        )

    def test_opencode_probe_uses_db_key_and_prints_output(self) -> None:
        import io

        from .opencode_client import OpenCodeResult

        with patch(
            "web.management.commands.opencode_probe.run_opencode",
            return_value=OpenCodeResult(text="hello from model"),
        ):
            stdout = io.StringIO()
            out = call_command(
                "opencode_probe",
                "hi",
                "there",
                "--user",
                "alice",
                "--no-files",
                stdout=stdout,
            )
        assert "hello from model" in str(out)
        assert "hello from model" in stdout.getvalue()


class GithubDiffFallbackTest(SimpleTestCase):
    def test_fetch_pull_request_diff_falls_back_to_files_patches_on_406(self) -> None:
        class FakeResponse:
            def __init__(self, status_code: int, reason_phrase: str) -> None:
                self.status_code = status_code
                self.reason_phrase = reason_phrase
                self.headers: dict[str, str] = {}
                self.text = ""

        class FakeClient:
            def __init__(self, responses: list[FakeResponse]) -> None:
                self._responses = responses

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                del exc_type, exc, tb
                return False

            def get(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
                del url, headers
                return self._responses.pop(0)

        responses = [
            FakeResponse(406, "Not Acceptable"),
            FakeResponse(406, "Not Acceptable"),
        ]

        with (
            patch.object(github.httpx, "Client", return_value=FakeClient(responses)),
            patch.object(
                github,
                "list_pull_request_files",
                return_value=[
                    {
                        "filename": "foo.py",
                        "status": "modified",
                        "patch": "@@ -1 +1 @@\n-print('a')\n+print('b')\n",
                    }
                ],
            ),
        ):
            diff_text = github.fetch_pull_request_diff(
                installation_id=1,
                auth=github.GithubAppAuth(
                    app_id="1",
                    private_key_pem="pem",
                    webhook_secret="secret",
                ),
                repo_full_name="owner/repo",
                pull_number=8,
                token="token",
            )

        assert "NOTE: GitHub did not return a unified PR diff" in diff_text
        assert "diff --git a/foo.py b/foo.py" in diff_text
        assert "@@ -1 +1 @@" in diff_text


class GithubInstallationTokenTest(SimpleTestCase):
    def test_get_installation_token_timeout_is_actionable(self) -> None:
        class FakeClient:
            def __enter__(self) -> FakeClient:
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                del exc_type, exc, tb
                return False

            def post(self, url: str, *, headers: dict[str, str]) -> None:
                del url, headers
                raise github.httpx.ConnectTimeout("timed out")

        with (
            patch.object(github, "build_jwt", return_value="jwt"),
            patch.object(github.httpx, "Client", return_value=FakeClient()),
            patch.object(github.time, "sleep", return_value=None),
        ):
            try:
                github.get_installation_token(
                    123,
                    github.GithubAppAuth(
                        app_id="1",
                        private_key_pem="pem",
                        webhook_secret="secret",
                    ),
                )
            except RuntimeError as e:
                message = str(e)
                assert "installation token" in message.lower()
                assert "api.github.com:443" in message
            else:
                raise AssertionError("Expected RuntimeError")
