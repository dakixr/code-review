from __future__ import annotations

import datetime as _dt
import enum
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(add_completion=False, no_args_is_help=False)


class DiffScope(str, enum.Enum):
    all = "all"
    staged = "staged"
    unstaged = "unstaged"


class ReviewStyle(str, enum.Enum):
    greptile = "greptile"
    simple = "simple"


DEFAULT_COMMENT_TYPES = ("logic", "syntax", "style", "info")


@dataclass(frozen=True)
class ReviewOptions:
    repo: Path | None
    scope: DiffScope
    style: ReviewStyle
    strictness: int
    comment_types: list[str]
    unified: int
    include_untracked: bool
    max_untracked_bytes: int
    max_diff_bytes: int
    opencode_bin: str
    model: str | None
    variant: str | None
    extra: str | None
    dry_run: bool


def _run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    return _run(["git", *args], cwd=cwd, check=check).stdout


def _repo_root(cwd: Path) -> Path:
    root = _git(cwd, "rev-parse", "--show-toplevel", check=True).strip()
    return Path(root)


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data


def _has_head(repo_root: Path) -> bool:
    proc = _run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo_root, check=False)
    return proc.returncode == 0


def _read_untracked_files(
    repo_root: Path,
    *,
    max_total_bytes: int,
) -> tuple[list[str], str]:
    untracked = [
        p
        for p in _git(
            repo_root, "ls-files", "--others", "--exclude-standard", check=True
        ).splitlines()
        if p.strip()
    ]
    if not untracked:
        return [], ""

    total = 0
    chunks: list[str] = []
    skipped: list[str] = []
    for rel in untracked:
        path = repo_root / rel
        try:
            raw = path.read_bytes()
        except OSError:
            skipped.append(rel)
            continue

        if _looks_binary(raw):
            skipped.append(rel)
            continue

        remaining = max_total_bytes - total
        if remaining <= 0:
            skipped.append(rel)
            continue

        raw = raw[:remaining]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                skipped.append(rel)
                continue

        total += len(raw)
        chunks.append(
            "\n".join(
                [
                    f"### Untracked file: `{rel}`",
                    "```",
                    text.rstrip(),
                    "```",
                ]
            )
        )

    out = ""
    if chunks:
        out += "\n\n## Untracked Files (Attached)\n" + "\n\n".join(chunks) + "\n"
    if skipped:
        out += (
            "\n\n## Untracked Files (Skipped)\n"
            + "\n".join(f"- `{p}`" for p in skipped)
            + "\n"
        )
    return untracked, out


def _build_system_prompt(
    *,
    style: ReviewStyle,
    strictness: int,
    comment_types: tuple[str, ...],
) -> str:
    strictness = min(3, max(1, strictness))
    enabled = ", ".join(comment_types) if comment_types else "(none)"

    if style == ReviewStyle.simple:
        return f"""You are **Code Reviewer**.

Goal: review **uncommitted changes** in the repository (staged and unstaged) and produce a concise, actionable review. You assess whether the changes break anything, introduce low-quality or nonsensical code, logic bugs, inconsistencies, or other issues—and report clearly without modifying the codebase.

Hard rules:
- Do **not** modify any files. Read/analyze only.
- Base your review on the provided diff and attached context.
- Be specific: cite file paths and line numbers from the diff when reporting issues or praise.
- Distinguish severity: **blocker** (likely breakage/critical bug), **major** (should fix), **minor** (suggestion).

Noise controls:
- Strictness: {strictness}/3 (1=verbose, 2=balanced, 3=critical-only).
- Comment types enabled: {enabled}.
- Avoid low-value nits; combine related minor points.

Output contract:
- Reply with **only** the code review in Markdown.
- Use this structure:
```markdown
# Code review
Date: YYYY-MM-DD HH:MM

## Summary
- ...

## Confidence Score (0–5)
# Confidence Score X/5
- Reasoning: 3–6 lines (complexity, severity, evidence strength, alignment with repo patterns).

## Files changed
- `path` — ...

## Blockers
- ...

## Major issues
- ...

## Minor / suggestions
- ...

## Positives
- ...
```
"""

    return f"""You are an **Independent Code Review Agent**.

Mission
- Review the provided change set (diff + metadata) and identify issues that matter: correctness, security, performance, breaking changes, and maintainability.
- Operate as an auditor: you validate code; you do not author features. You may suggest fixes, but do not apply changes.
- Be high-signal: prefer a small number of well-evidenced comments over speculative output.

Noise controls
- Strictness: {strictness}/3 (1=verbose, 2=balanced, 3=critical-only).
- Comment types enabled: {enabled}.
- Avoid purely stylistic feedback unless it impacts correctness/maintainability meaningfully.
- Combine related minor points.

Evidence threshold
- Do not report an issue unless you can cite the relevant location(s) from the diff (file and line numbers) or attached context.
- If something depends on unknown runtime behavior, ask a question instead of claiming a finding.

Output contract (single response)
- Reply with **only** the code review in Markdown (no preamble, no meta commentary, no tool output).
- Always include the Confidence Score header with the score inline (e.g., `# Confidence Score 4/5`).
- Use this structure:
```markdown
# Review Summary
- 4–8 bullets describing what changed (plain language).

# Confidence Score X/5
- Reasoning: 3–6 lines (complexity, severity, evidence strength, alignment with repo patterns).

# Key Issues
For each issue (sorted by severity desc, then confidence desc):
- Type: one of [logic, syntax, style, info]
- Severity: one of [blocker, major, minor]
- Location: path:line
- What / Why: concise explanation
- Evidence: cite relevant diff lines
- Suggested Fix: minimal change (pseudo-diff is OK)

# Positives
- 2-5 bullets highlighting good changes.
```

Termination
- If there is no diff content, say: "No changes detected to review." and stop.
"""


def _format_review_message(
    *,
    repo_root: Path,
    scope: DiffScope,
    diff_text: str,
    status_text: str,
    untracked_section: str,
    extra_instructions: str | None,
) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts: list[str] = [
        "IMPORTANT: Reply ONLY with the code review Markdown. No preamble, no analysis, no other text.",
        "",
        "## Context",
        f"- Date: {now}",
        f"- Repo root: {repo_root}",
        f"- Diff scope: {scope.value}",
        "",
        "## git status --porcelain=v1",
        "```",
        status_text.rstrip(),
        "```",
    ]
    if extra_instructions:
        parts += ["", "## Extra instructions", extra_instructions.strip()]
    parts += [
        "",
        "## Diff",
        "```diff",
        diff_text.rstrip(),
        "```",
    ]
    if untracked_section:
        parts.append(untracked_section.rstrip())
    return "\n".join(parts).strip() + "\n"


def _opencode_available(opencode_bin: str) -> bool:
    try:
        subprocess.run(
            [opencode_bin, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except Exception:
        return False


def _stream_process(args: list[str], *, cwd: Path) -> int:
    proc = subprocess.Popen(
        args, cwd=str(cwd), stdout=sys.stdout, stderr=sys.stderr, text=True
    )
    return proc.wait()


def _write_temp_agent(
    repo_root: Path,
    system_prompt: str,
) -> tuple[str, Path, bool, bool]:
    opencode_dir = repo_root / ".opencode"
    agent_dir = opencode_dir / "agent"
    created_opencode_dir = False
    created_agent_dir = False

    if not opencode_dir.exists():
        opencode_dir.mkdir(parents=True, exist_ok=True)
        created_opencode_dir = True
    if not agent_dir.exists():
        agent_dir.mkdir(parents=True, exist_ok=True)
        created_agent_dir = True

    name = f"codereview-tmp-{uuid.uuid4().hex[:10]}"
    agent_path = agent_dir / f"{name}.md"

    frontmatter_lines = [
        "---",
        "description: Temporary code review agent (auto-generated)",
        "mode: primary",
        "tools:",
        "  read: true",
        "  bash: true",
        "  write: false",
        "---",
        "",
    ]

    agent_path.write_text(
        "\n".join(frontmatter_lines) + system_prompt.strip() + "\n",
        encoding="utf-8",
    )
    return name, agent_path, created_agent_dir, created_opencode_dir


def _review_impl(options: ReviewOptions) -> None:
    """Run a Greptile-style local code review via `opencode run`, injecting the system prompt."""
    target = options.repo if options.repo is not None else Path.cwd()
    try:
        repo_root = _repo_root(target)
    except subprocess.CalledProcessError:
        typer.echo(
            "Error: not inside a git repository (or git is not available).", err=True
        )
        raise typer.Exit(code=2)

    if not _opencode_available(options.opencode_bin):
        typer.echo(
            f"Error: '{options.opencode_bin}' not found or not runnable. Install opencode or pass --opencode.",
            err=True,
        )
        raise typer.Exit(code=127)

    status_text = _git(repo_root, "status", "--porcelain=v1", check=True)

    diff_args: list[str]
    if options.scope == DiffScope.all:
        if _has_head(repo_root):
            diff_args = ["diff", f"--unified={options.unified}", "HEAD"]
        else:
            diff_args = []
    elif options.scope == DiffScope.staged:
        diff_args = ["diff", f"--unified={options.unified}", "--cached"]
    else:
        diff_args = ["diff", f"--unified={options.unified}"]

    if options.scope == DiffScope.all and not diff_args:
        staged = _git(
            repo_root, "diff", f"--unified={options.unified}", "--cached", check=True
        )
        unstaged = _git(repo_root, "diff", f"--unified={options.unified}", check=True)
        diff_text = "\n".join(p for p in [staged.strip(), unstaged.strip()] if p)
    else:
        diff_text = _git(repo_root, *diff_args, check=True)
    if options.max_diff_bytes > 0 and len(diff_text.encode("utf-8")) > options.max_diff_bytes:
        diff_text = diff_text.encode("utf-8")[: options.max_diff_bytes].decode(
            "utf-8", errors="replace"
        )
        diff_text += "\n\n# NOTE: Diff truncated due to --max-diff-bytes.\n"

    untracked_section = ""
    if options.include_untracked:
        _, untracked_section = _read_untracked_files(
            repo_root,
            max_total_bytes=options.max_untracked_bytes,
        )

    if not diff_text.strip() and not untracked_section.strip():
        typer.echo("No changes detected to review.")
        raise typer.Exit(code=0)

    normalized_comment_types = tuple(
        ct.strip().lower()
        for ct in options.comment_types
        if ct.strip().lower() in DEFAULT_COMMENT_TYPES
    )
    invalid_comment_types = [
        ct for ct in options.comment_types if ct.strip().lower() not in DEFAULT_COMMENT_TYPES
    ]
    if invalid_comment_types:
        typer.echo(
            "Warning: ignoring unknown comment types: "
            + ", ".join(invalid_comment_types),
            err=True,
        )
    system_prompt = _build_system_prompt(
        style=options.style,
        strictness=options.strictness,
        comment_types=normalized_comment_types,
    )
    message = _format_review_message(
        repo_root=repo_root,
        scope=options.scope,
        diff_text=diff_text,
        status_text=status_text,
        untracked_section=untracked_section,
        extra_instructions=options.extra,
    )

    if options.dry_run:
        typer.echo("## System prompt\n" + system_prompt.rstrip() + "\n")
        typer.echo("## Message\n" + message)
        raise typer.Exit(code=0)

    agent_name, agent_path, created_agent_dir, created_opencode_dir = _write_temp_agent(
        repo_root,
        system_prompt,
    )
    try:
        args = [options.opencode_bin]
        if options.model:
            args += ["-m", options.model]
        if options.variant:
            args += ["--variant", options.variant]
        args += ["run", "--agent", agent_name, message]
        raise typer.Exit(code=_stream_process(args, cwd=repo_root))
    finally:
        try:
            agent_path.unlink(missing_ok=True)
        finally:
            if created_agent_dir:
                try:
                    (repo_root / ".opencode" / "agent").rmdir()
                except Exception:
                    pass
            if created_opencode_dir:
                try:
                    (repo_root / ".opencode").rmdir()
                except Exception:
                    pass


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Path to the repo to review (defaults to the process working directory).",
        ),
    ] = None,
    scope: Annotated[
        DiffScope,
        typer.Option(
            "--scope",
            help="Which changes to review (uses git diff). 'all' reviews both staged+unstaged vs HEAD.",
        ),
    ] = DiffScope.all,
    style: Annotated[
        ReviewStyle,
        typer.Option("--style", help="Output format contract for the review."),
    ] = ReviewStyle.greptile,
    strictness: Annotated[
        int,
        typer.Option(
            "--strictness", min=1, max=3, help="1=verbose, 2=balanced, 3=critical-only."
        ),
    ] = 2,
    comment_types: Annotated[
        list[str],
        typer.Option(
            "--comment-type",
            help="Enable comment types (repeatable): logic, syntax, style, info.",
        ),
    ] = list(DEFAULT_COMMENT_TYPES),
    unified: Annotated[
        int,
        typer.Option("--unified", min=0, max=50, help="Context lines for git diff."),
    ] = 5,
    include_untracked: Annotated[
        bool,
        typer.Option(
            "--include-untracked/--no-include-untracked", help="Attach untracked files."
        ),
    ] = True,
    max_untracked_bytes: Annotated[
        int,
        typer.Option(
            "--max-untracked-bytes",
            min=0,
            help="Max bytes to attach across untracked files.",
        ),
    ] = 200_000,
    max_diff_bytes: Annotated[
        int,
        typer.Option(
            "--max-diff-bytes",
            min=0,
            help="Max bytes to send from the diff (truncates if needed).",
        ),
    ] = 800_000,
    opencode_bin: Annotated[
        str,
        typer.Option("--opencode", help="Path to the opencode executable."),
    ] = "opencode",
    model: Annotated[
        str | None,
        typer.Option("-m", "--model", help="Override opencode model (provider/model)."),
    ] = None,
    variant: Annotated[
        str | None,
        typer.Option(
            "--variant",
            help="Provider-specific model variant (e.g. high, max, minimal).",
        ),
    ] = None,
    extra: Annotated[
        str | None,
        typer.Option(
            "--extra", help="Additional instructions appended to the review request."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Print the message/prompt but do not call opencode."
        ),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _review_impl(
        ReviewOptions(
            repo=repo,
            scope=scope,
            style=style,
            strictness=strictness,
            comment_types=comment_types,
            unified=unified,
            include_untracked=include_untracked,
            max_untracked_bytes=max_untracked_bytes,
            max_diff_bytes=max_diff_bytes,
            opencode_bin=opencode_bin,
            model=model,
            variant=variant,
            extra=extra,
            dry_run=dry_run,
        )
    )


@app.command()
def review(
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="Path to the repo to review (defaults to the process working directory).",
        ),
    ] = None,
    scope: Annotated[
        DiffScope,
        typer.Option(
            "--scope",
            help="Which changes to review (uses git diff). 'all' reviews both staged+unstaged vs HEAD.",
        ),
    ] = DiffScope.all,
    style: Annotated[
        ReviewStyle,
        typer.Option("--style", help="Output format contract for the review."),
    ] = ReviewStyle.greptile,
    strictness: Annotated[
        int,
        typer.Option(
            "--strictness", min=1, max=3, help="1=verbose, 2=balanced, 3=critical-only."
        ),
    ] = 2,
    comment_types: Annotated[
        list[str],
        typer.Option(
            "--comment-type",
            help="Enable comment types (repeatable): logic, syntax, style, info.",
        ),
    ] = list(DEFAULT_COMMENT_TYPES),
    unified: Annotated[
        int,
        typer.Option("--unified", min=0, max=50, help="Context lines for git diff."),
    ] = 5,
    include_untracked: Annotated[
        bool,
        typer.Option(
            "--include-untracked/--no-include-untracked", help="Attach untracked files."
        ),
    ] = True,
    max_untracked_bytes: Annotated[
        int,
        typer.Option(
            "--max-untracked-bytes",
            min=0,
            help="Max bytes to attach across untracked files.",
        ),
    ] = 200_000,
    max_diff_bytes: Annotated[
        int,
        typer.Option(
            "--max-diff-bytes",
            min=0,
            help="Max bytes to send from the diff (truncates if needed).",
        ),
    ] = 800_000,
    opencode_bin: Annotated[
        str,
        typer.Option("--opencode", help="Path to the opencode executable."),
    ] = "opencode",
    model: Annotated[
        str | None,
        typer.Option("-m", "--model", help="Override opencode model (provider/model)."),
    ] = None,
    variant: Annotated[
        str | None,
        typer.Option(
            "--variant",
            help="Provider-specific model variant (e.g. high, max, minimal).",
        ),
    ] = None,
    extra: Annotated[
        str | None,
        typer.Option(
            "--extra", help="Additional instructions appended to the review request."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Print the message/prompt but do not call opencode."
        ),
    ] = False,
) -> None:
    _review_impl(
        ReviewOptions(
            repo=repo,
            scope=scope,
            style=style,
            strictness=strictness,
            comment_types=comment_types,
            unified=unified,
            include_untracked=include_untracked,
            max_untracked_bytes=max_untracked_bytes,
            max_diff_bytes=max_diff_bytes,
            opencode_bin=opencode_bin,
            model=model,
            variant=variant,
            extra=extra,
            dry_run=dry_run,
        )
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()
