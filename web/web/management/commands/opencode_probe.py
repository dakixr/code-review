from __future__ import annotations

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from web.models import UserApiKey
from web.opencode_client import run_opencode


class Command(BaseCommand):
    help = "Send a prompt to OpenCode using the same codepath as background tasks."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "message",
            nargs="+",
            help="Prompt text to send to OpenCode.",
        )
        parser.add_argument(
            "--user",
            dest="username",
            default="",
            help="Username to load the active API key from (defaults to required unless --api-key is passed).",
        )
        parser.add_argument(
            "--user-id",
            dest="user_id",
            type=int,
            default=0,
            help="User id to load the active API key from (alternative to --user).",
        )
        parser.add_argument(
            "--api-key",
            dest="api_key",
            default="",
            help="Explicit API key override (does not read from DB).",
        )
        parser.add_argument(
            "--opencode-bin",
            dest="opencode_bin",
            default="",
            help="Override OpenCode binary path/name (sets OPENCODE_BIN for this run).",
        )
        parser.add_argument(
            "--no-files",
            action="store_true",
            help="Do not attach a probe context file.",
        )

    def handle(self, *args, **options) -> str:
        message = " ".join(options["message"]).strip()
        if not message:
            raise CommandError("Message must not be empty.")

        api_key = (options.get("api_key") or "").strip()
        if not api_key:
            api_key = self._load_api_key(
                username=str(options.get("username") or "").strip(),
                user_id=int(options.get("user_id") or 0),
            )

        env: dict[str, str] = {"ZAI_API_KEY": api_key}
        opencode_bin = str(options.get("opencode_bin") or "").strip()
        if opencode_bin:
            env["OPENCODE_BIN"] = opencode_bin

        if not options.get("no_files"):
            with tempfile.TemporaryDirectory(prefix="codereview-opencode-probe-") as td:
                context_path = Path(td) / "probe_context.md"
                context_path.write_text(
                    "# OpenCode probe context\n\n"
                    "If you can read attached files, reply with the title of this file.\n",
                    encoding="utf-8",
                )
                result = run_opencode(message=message, files=[context_path], env=env)
                output = result.text.strip()
        else:
            result = run_opencode(message=message, files=None, env=env)
            output = result.text.strip()

        if not output:
            raise CommandError("OpenCode returned an empty response.")

        self.stdout.write(output)
        return output

    def _load_api_key(self, *, username: str, user_id: int) -> str:
        user_model = get_user_model()
        if user_id:
            user = user_model.objects.filter(id=user_id).first()
        elif username:
            user = user_model.objects.filter(username=username).first()
        else:
            raise CommandError("Pass --api-key, --user, or --user-id.")

        if not user:
            raise CommandError("User not found.")

        api_key = (
            UserApiKey.objects.filter(
                user=user,
                provider=UserApiKey.PROVIDER_ZAI,
                is_active=True,
            )
            .order_by("-updated_at")
            .values_list("api_key", flat=True)
            .first()
        )
        api_key = (api_key or "").strip()
        if not api_key:
            raise CommandError(
                "No active ZAI API key found for that user. Set it at Account → API Keys."
            )
        return api_key
