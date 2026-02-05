from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from shutil import which


@dataclass(frozen=True)
class OpenCodeResult:
    text: str


def _compact_output(text: str, *, max_chars: int = 2000) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "…"


def _default_timeout_seconds() -> float:
    configured = (os.getenv("OPENCODE_TIMEOUT_SECONDS") or "").strip()
    if configured:
        try:
            value = float(configured)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return 900.0


def _resolve_opencode_bin(*, merged_env: dict[str, str], configured_bin: str) -> str:
    candidates = [configured_bin]
    if configured_bin == "opencode":
        candidates.extend(
            [
                "/usr/local/bin/opencode",
                "/usr/bin/opencode",
            ]
        )

    for candidate in candidates:
        if "/" in candidate:
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return candidate
            continue

        resolved = which(candidate, path=merged_env.get("PATH"))
        if resolved:
            return resolved

    raise RuntimeError(
        "OpenCode binary not found. Set OPENCODE_BIN or ensure it is installed "
        f"and on PATH. PATH={merged_env.get('PATH', '')!r}"
    )


def _format_opencode_start_error(
    *, opencode_bin: str, merged_env: dict[str, str]
) -> str:
    message = (
        "OpenCode executable could not be started. "
        f"Tried {opencode_bin!r}. PATH={merged_env.get('PATH', '')!r}"
    )

    path = Path(opencode_bin)
    if not path.exists():
        return message

    try:
        head = path.read_bytes()[:8192]
    except Exception:
        return message

    if head.startswith(b"#!"):
        first_line = head.splitlines()[0][2:].decode("utf-8", errors="replace").strip()
        return (
            f"{message} (The opencode entrypoint is a script; its interpreter "
            f"may be missing: {first_line!r}.)"
        )

    if b"ld-musl" in head:
        return (
            f"{message} (This opencode binary appears to be musl-linked; "
            "ensure musl is installed in the runtime image.)"
        )

    return message


def run_opencode(
    *,
    message: str,
    files: list[Path] | None = None,
    env: dict[str, str],
    cwd: Path | None = None,
    timeout_seconds: float | None = None,
) -> OpenCodeResult:
    """Run OpenCode in headless mode and return the assistant text.

    Args:
        message: The user prompt.
        files: Optional list of files to attach to the message.
        env: Environment overrides (e.g. per-user provider API keys).
        cwd: Optional working directory to run OpenCode in.
        timeout_seconds: Maximum runtime before aborting. Defaults to
            OPENCODE_TIMEOUT_SECONDS or 900 seconds.

    Raises:
        RuntimeError: If OpenCode fails or returns an error event.
    """
    merged_env = os.environ.copy()
    merged_env.update(env)

    configured_bin = (
        env.get("OPENCODE_BIN") or os.getenv("OPENCODE_BIN", "") or "opencode"
    )
    opencode_bin = _resolve_opencode_bin(
        merged_env=merged_env,
        configured_bin=configured_bin,
    )

    args = [opencode_bin, "run", "--format", "json"]
    for file_path in files or []:
        args.extend(["--file", str(file_path)])
    # Important: `opencode run --file` takes an array value; without `--`,
    # the message can be mis-parsed as an additional file argument.
    args.append("--")
    args.append(message)

    try:
        effective_timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else _default_timeout_seconds()
        )
        log_level = (merged_env.get("OPENCODE_LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARN", "ERROR"}:
            log_level = "INFO"

        # Ensure OpenCode cannot block waiting for user input (permissions, auth, etc).
        merged_env.setdefault("CI", "1")
        merged_env.setdefault("TERM", "dumb")

        # Helpful when diagnosing worker hangs: logs go to stderr, JSON events stay on stdout.
        if "--print-logs" not in args:
            args.insert(2, "--print-logs")
        if "--log-level" not in args:
            args.insert(3, "--log-level")
            args.insert(4, log_level)

        proc = subprocess.run(
            args,
            env=merged_env,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            _format_opencode_start_error(
                opencode_bin=opencode_bin, merged_env=merged_env
            )
        ) from e
    except subprocess.TimeoutExpired as e:
        stdout = _compact_output(str(e.stdout or ""))
        stderr = _compact_output(str(e.stderr or ""))
        details = stderr or stdout or "no output captured"
        raise RuntimeError(
            f"opencode timed out after {effective_timeout:.0f}s: {details}"
        ) from e

    # OpenCode emits line-delimited JSON events on stdout in `--format json` mode.
    stdout = proc.stdout.strip()
    if not stdout:
        stderr = _compact_output(proc.stderr or "")
        raise RuntimeError(
            f"opencode produced no output (exit={proc.returncode}): {stderr}"
        )

    assistant_chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(event, dict) and event.get("type") == "error":
            data = (event.get("error") or {}).get("data") or {}
            message_text = (
                data.get("message")
                or (event.get("error") or {}).get("name")
                or "OpenCode error"
            )
            raise RuntimeError(str(message_text))

        if not isinstance(event, dict):
            continue

        # Heuristic extraction: OpenCode event schemas can vary; capture likely assistant content.
        msg = event.get("message")
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                assistant_chunks.append(content.strip())
            continue

        content = event.get("content")
        if isinstance(content, str) and content.strip():
            assistant_chunks.append(content.strip())

        text_value = event.get("text")
        if isinstance(text_value, str) and text_value.strip():
            assistant_chunks.append(text_value.strip())

    final_text = "\n\n".join(chunk for chunk in assistant_chunks if chunk)
    if not final_text:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(
            f"opencode returned no assistant text (exit={proc.returncode}): {stderr}"
        )
    return OpenCodeResult(text=final_text)
