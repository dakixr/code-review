from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Iterable, cast
from uuid import UUID

from components.ui._types import AlertVariant
from components.ui.alert import alert
from components.ui.badge import badge_count, badge_status
from components.ui.button import button_component
from components.ui.card import card
from components.ui.form import form_component, form_field
from components.ui.input import input_component
from components.ui.lucide import lucide_auto_init_script, lucide_cdn_script, lucide_icon
from components.ui.navbar import navbar
from components.ui.section import section_header
from components.ui.table import table_component
from components.ui.textarea import textarea_component
from components.ui.theme_toggle import theme_toggle
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.models import User
from django.contrib.messages.storage.base import Message
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.templatetags.static import static
from django.utils import timezone
from django.utils.html import escape
from django.utils.timezone import localtime
from django.views.decorators.csrf import csrf_exempt
from htpy import (
    Node,
    Renderable,
    a,
    body,
    details,
    div,
    h1,
    h2,
    head,
    html,
    label,
    li,
    link,
    main,
    meta,
    option,
    p,
    script,
    select,
    span,
    strong,
    summary,
    title,
    ul,
)
from htpy import input as input_el

from . import github
from .github import parse_webhook_body, verify_webhook_signature
from .models import (
    ChatMessage,
    FeedbackSignal,
    GithubApp,
    GithubInstallation,
    GithubRepository,
    PullRequest,
    ReviewRun,
    Rule,
    RuleSet,
    UserApiKey,
    UserProfile,
)
from .services import (
    deactivate_repository,
    queue_review,
    record_chat_message,
    upsert_installation,
    upsert_installation_for_app,
    upsert_pull_request,
    upsert_repository,
)

PAGE_SHELL_CLASS = "min-h-screen bg-background text-foreground relative"
CONTENT_CLASS = "max-w-7xl mx-auto px-4 sm:px-6 lg:px-8"
ANIMATE_STAGGER_CLASS = "animate-stagger"
HERO_PATTERN_CLASS = "hero-pattern"

logger = logging.getLogger(__name__)


def _review_run_status_badge(status: str) -> Renderable:
    mapping = {
        ReviewRun.STATUS_QUEUED: "pending",
        ReviewRun.STATUS_RUNNING: "processing",
        ReviewRun.STATUS_DONE: "completed",
        ReviewRun.STATUS_FAILED: "failed",
    }
    return badge_status(mapping.get(status, "pending"))


def _format_datetime(value: datetime | None) -> str:
    if not value:
        return "—"
    return localtime(value).strftime("%Y-%m-%d %H:%M:%S")


def _mark_stale_review_runs(*, owner: User, now: datetime) -> int:
    stale_queued_before = now - timedelta(hours=1)
    stale_running_before = now - timedelta(hours=2)

    base = ReviewRun.objects.filter(
        pull_request__repository__installation__github_app__owner=owner,
    )

    stale_queued = base.filter(
        status=ReviewRun.STATUS_QUEUED,
        created_at__lt=stale_queued_before,
    ).update(
        status=ReviewRun.STATUS_FAILED,
        finished_at=now,
        error_message="Marked stale: queued > 1h (worker may be down).",
    )

    stale_running = base.filter(
        status=ReviewRun.STATUS_RUNNING,
        started_at__isnull=False,
        started_at__lt=stale_running_before,
    ).update(
        status=ReviewRun.STATUS_FAILED,
        finished_at=now,
        error_message="Marked stale: running > 2h (worker may have crashed).",
    )

    stale_running_no_start = base.filter(
        status=ReviewRun.STATUS_RUNNING,
        started_at__isnull=True,
        created_at__lt=stale_running_before,
    ).update(
        status=ReviewRun.STATUS_FAILED,
        finished_at=now,
        error_message="Marked stale: running > 2h (missing start time).",
    )

    return int(stale_queued) + int(stale_running) + int(stale_running_no_start)


def render_htpy(content: Renderable) -> HttpResponse:
    return HttpResponse(str(content))


def github_app_install_url(request: HttpRequest) -> str:
    if request.user.is_authenticated:
        github_app = (
            GithubApp.objects.filter(owner=request.user, status=GithubApp.STATUS_READY)
            .exclude(slug="")
            .order_by("-updated_at")
            .first()
        )
        if github_app:
            return f"https://github.com/apps/{github_app.slug}/installations/new"

    slug_source = settings.GITHUB_APP_SLUG or settings.GITHUB_APP_NAME
    slug = slug_source.strip().lower().replace(" ", "-")
    if slug:
        return f"https://github.com/apps/{slug}/installations/new"
    return "/account"


def layout(request: HttpRequest, content: Node, *, page_title: str) -> HttpResponse:
    flash = _flash_messages(request)

    # Logo with terminal-style accent
    logo = a(href="/", class_="flex items-center gap-2.5 group")[
        # Terminal icon
        div(
            class_="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center "
            "group-hover:bg-primary/20 transition-colors"
        )[
            span(class_="text-primary font-mono font-bold text-sm")[">_"]
        ],
        span(class_="font-semibold text-foreground tracking-tight")["CodeReview"],
        span(class_="text-primary font-medium")["AI"],
    ]

    # Navigation links with active state indicators
    nav_links = div(class_="hidden md:flex items-center gap-1")[
        a(
            href="/app",
            class_="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground "
            "hover:bg-accent/50 rounded-md transition-all",
        )["Dashboard"],
        a(
            href="/rules",
            class_="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground "
            "hover:bg-accent/50 rounded-md transition-all",
        )["Rules"],
        a(
            href="/feedback",
            class_="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground "
            "hover:bg-accent/50 rounded-md transition-all",
        )["Feedback"],
        a(
            href="/account",
            class_="px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground "
            "hover:bg-accent/50 rounded-md transition-all",
        )["Account"],
    ]

    # Right side actions
    actions = div(class_="flex items-center gap-2")[
        theme_toggle(),
        a(
            href=github_app_install_url(request),
            class_="hidden sm:inline-flex items-center gap-1.5 px-3 py-1.5 text-xs "
            "font-medium text-primary-foreground bg-primary hover:bg-primary/90 "
            "rounded-md transition-colors shadow-sm",
        )[
            span(class_="opacity-80")["Install"],
            span["GitHub App"],
        ],
    ]

    top_nav = navbar(
        left=logo,
        center=nav_links,
        right=actions,
    )

    return render_htpy(
        html(lang="en")[
            head[
                meta(charset="utf-8"),
                meta(name="viewport", content="width=device-width, initial-scale=1"),
                title[page_title],
                link(rel="stylesheet", href=static("css/output.css")),
                script(
                    src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js",
                    defer=True,
                ),
                script(src="https://unpkg.com/htmx.org@1.9.12"),
                lucide_cdn_script(),
            ],
            body(class_=PAGE_SHELL_CLASS)[
                # Subtle background gradient
                div(
                    class_="fixed inset-0 -z-10 bg-gradient-to-br from-primary/[0.02] via-transparent to-accent/[0.02]"
                ),
                top_nav,
                main(class_=f"py-8 sm:py-12 {CONTENT_CLASS} animate-page-in")[
                    flash, content
                ],
                # Initialize Lucide icons
                lucide_auto_init_script(),
            ],
        ]
    )


def home(request: HttpRequest) -> HttpResponse:
    install_url = github_app_install_url(request)

    # Hero badges with subtle styling
    hero_badges = div(class_="flex flex-wrap gap-2")[
        span(
            class_="inline-flex items-center gap-1.5 rounded-full border border-border/50 "
            "bg-background/80 px-3 py-1 text-xs text-muted-foreground backdrop-blur-sm"
        )[
            span(class_="w-1.5 h-1.5 rounded-full bg-success"),
            "GitHub App",
        ],
        span(
            class_="rounded-full border border-border/50 bg-background/80 px-3 py-1 "
            "text-xs text-muted-foreground backdrop-blur-sm"
        )["Multi-user"],
        span(
            class_="rounded-full border border-border/50 bg-background/80 px-3 py-1 "
            "text-xs text-muted-foreground font-mono backdrop-blur-sm"
        )["HTMX + htpy"],
        span(
            class_="rounded-full border border-primary/30 bg-primary/5 px-3 py-1 "
            "text-xs text-primary backdrop-blur-sm"
        )["Learns your taste"],
    ]

    # Hero actions with improved styling
    hero_actions = div(class_="flex flex-wrap items-center gap-3")[
        a(href=install_url, class_="group")[
            button_component(
                variant="primary",
                class_="shadow-lg shadow-primary/20 hover:shadow-xl hover:shadow-primary/30 transition-shadow",
            )[
                span["Install GitHub App"],
                span(class_="ml-1 group-hover:translate-x-0.5 transition-transform")[
                    "→"
                ],
            ]
        ],
        a(href="/app")[
            button_component(variant="outline")["Go to dashboard"]
        ],
    ]

    # Terminal-style preview card
    terminal_preview = div(
        class_="rounded-xl border border-border overflow-hidden bg-card shadow-xl shadow-black/5"
    )[
        # Terminal header
        div(class_="terminal-header")[
            span(class_="terminal-dot terminal-dot-red"),
            span(class_="terminal-dot terminal-dot-yellow"),
            span(class_="terminal-dot terminal-dot-green"),
            span(class_="ml-3 text-xs text-muted-foreground font-mono")[
                "PR #142 — Add user authentication"
            ],
        ],
        # Content
        div(class_="p-4 space-y-3")[
            div(class_="gh-comment")[
                span(class_="text-primary font-medium")["@codereview-bot"],
                span(class_="text-muted-foreground")["  ·  just now"],
                p(class_="mt-2 text-sm")["👁 Reviewing this PR now..."],
            ],
            div(class_="gh-comment")[
                span(class_="text-primary font-medium")["@codereview-bot"],
                span(class_="text-muted-foreground")["  ·  2m ago"],
                p(class_="mt-2 text-sm")[
                    '✅ Found 2 improvements: Add null guard on "user.email", '
                    "consider caching lint results."
                ],
            ],
            div(class_="gh-comment border-primary/30 bg-primary/[0.02]")[
                span(class_="text-foreground font-medium")["@developer"],
                span(class_="text-muted-foreground")["  ·  1m ago"],
                p(class_="mt-2 text-sm font-mono text-primary")["/ai like"],
            ],
        ],
    ]

    # Hero section with improved layout
    hero = div(class_="relative rounded-2xl border border-border/60 overflow-hidden")[
        # Gradient background
        div(
            class_="absolute inset-0 bg-gradient-to-br from-muted/50 via-transparent to-primary/[0.03]"
        ),
        div(class_="absolute inset-0 hero-pattern"),
        # Content
        div(class_="relative p-6 sm:p-10 lg:p-12")[
            div(class_="grid gap-8 lg:grid-cols-[1.2fr_0.9fr] lg:gap-12 items-center")[
                div(class_="space-y-6")[
                    hero_badges,
                    h1(
                        class_="text-3xl sm:text-4xl lg:text-5xl font-bold tracking-tight leading-tight"
                    )[
                        span(class_="block")["Automated PR reviews"],
                        span(class_="block text-gradient")["that learn your taste"],
                    ],
                    p(class_="text-base sm:text-lg text-muted-foreground max-w-xl")[
                        "Create your own GitHub App, install it on repos, and let the "
                        "reviewer comment directly in PRs. Per-user API keys and "
                        "customizable rule sets."
                    ],
                    hero_actions,
                    div(class_="flex items-start gap-3 text-sm text-muted-foreground")[
                        lucide_icon("check", class_="size-5 text-success shrink-0"),
                        div[
                            strong(class_="text-foreground block")[
                                "Works entirely from GitHub"
                            ],
                            span[
                                "The UI is a control plane—conversation happens in PR comments."
                            ],
                        ],
                    ],
                ],
                # Preview card (hidden on small screens)
                div(class_="hidden lg:block")[terminal_preview],
            ],
        ],
    ]

    # Stats with icons and improved design
    stats = div(class_="grid gap-4 sm:grid-cols-3")[
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-success/10 flex items-center justify-center shrink-0"
                )[lucide_icon("zap", class_="size-5 text-success")],
                div[
                    p(class_="font-semibold text-foreground")["<10 seconds"],
                    p(class_="text-sm text-muted-foreground mt-0.5")[
                        "Time to first review comment"
                    ],
                ],
            ]
        ],
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0"
                )[lucide_icon("settings", class_="size-5 text-primary")],
                div[
                    p(class_="font-semibold text-foreground")["Global + repo rules"],
                    p(class_="text-sm text-muted-foreground mt-0.5")[
                        "Configure once or per repository"
                    ],
                ],
            ]
        ],
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-warning/10 flex items-center justify-center shrink-0"
                )[lucide_icon("target", class_="size-5 text-warning")],
                div[
                    p(class_="font-semibold text-foreground")["Feedback loop"],
                    p(class_="text-sm text-muted-foreground mt-0.5")[
                        "Like, ignore, dislike signals"
                    ],
                ],
            ]
        ],
    ]

    # Setup flow with numbered steps
    setup_flow = div(class_=f"grid gap-4 md:grid-cols-3 {ANIMATE_STAGGER_CLASS}")[
        _step_card("1", "Create account", "Control plane access", [
            "Each user brings their own GitHub App and AI provider API keys."
        ]),
        _step_card("2", "Create GitHub App", "Manifest flow", [
            "We redirect you to GitHub with a pre-filled manifest and store credentials."
        ]),
        _step_card("3", "Install the app", "Org or repo", [
            "Choose which repos to grant access for webhooks and PR diffs."
        ]),
    ]

    runtime_flow = div(class_=f"grid gap-4 md:grid-cols-3 {ANIMATE_STAGGER_CLASS}")[
        _step_card("4", "Webhook ingestion", "PR + comment events", [
            "GitHub calls per-app webhook URL. We verify signatures securely."
        ]),
        _step_card("5", "Background review", "Celery + OpenCode", [
            "Worker fetches PR diff and runs review with your model API key."
        ]),
        _step_card("6", "GitHub-native loop", "Comments + feedback", [
            "Placeholder 👁 comment posted, then edited with full review."
        ]),
    ]

    # Features with visual distinction
    features = div(class_=f"grid gap-4 md:grid-cols-3 {ANIMATE_STAGGER_CLASS}")[
        card(class_="hover-lift group")[
            div(class_="space-y-3")[
                div(
                    class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center "
                    "group-hover:bg-primary/20 transition-colors"
                )[lucide_icon("refresh-cw", class_="size-5 text-primary")],
                div[
                    h2(class_="font-semibold text-foreground")["Auto review"],
                    p(class_="text-sm text-muted-foreground mt-1")[
                        "Runs on PR open or sync. Status comment updates when ready."
                    ],
                ],
            ]
        ],
        card(class_="hover-lift group")[
            div(class_="space-y-3")[
                div(
                    class_="w-10 h-10 rounded-lg bg-success/10 flex items-center justify-center "
                    "group-hover:bg-success/20 transition-colors"
                )[lucide_icon("brain", class_="size-5 text-success")],
                div[
                    h2(class_="font-semibold text-foreground")["Learns you"],
                    p(class_="text-sm text-muted-foreground mt-1")[
                        "Records like, dislike, ignore signals to improve reviews."
                    ],
                ],
            ]
        ],
        card(class_="hover-lift group")[
            div(class_="space-y-3")[
                div(
                    class_="w-10 h-10 rounded-lg bg-warning/10 flex items-center justify-center "
                    "group-hover:bg-warning/20 transition-colors"
                )[lucide_icon("clipboard-list", class_="size-5 text-warning")],
                div[
                    h2(class_="font-semibold text-foreground")["Configurable"],
                    p(class_="text-sm text-muted-foreground mt-1")[
                        "Tune instruction sets per repo without editing config files."
                    ],
                ],
            ]
        ],
    ]

    # Architecture section
    architecture = div(class_="grid gap-4 md:grid-cols-2")[
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0 font-mono text-sm text-primary"
                )["UI"],
                div(class_="space-y-2")[
                    h2(class_="font-semibold")["Control plane"],
                    p(class_="text-xs text-muted-foreground")["Django + HTMX + htpy"],
                    ul(class_="space-y-1 text-sm text-muted-foreground mt-2")[
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Manage GitHub App + installations",
                        ],
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Create global and per-repo rules",
                        ],
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Store per-user API keys securely",
                        ],
                    ],
                ],
            ]
        ],
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-accent flex items-center justify-center shrink-0"
                )[lucide_icon("cog", class_="size-5 text-accent-foreground")],
                div(class_="space-y-2")[
                    h2(class_="font-semibold")["Data plane"],
                    p(class_="text-xs text-muted-foreground")[
                        "Webhook → Queue → Review"
                    ],
                    ul(class_="space-y-1 text-sm text-muted-foreground mt-2")[
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Validates per-app signatures",
                        ],
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Celery job fetches PR diff",
                        ],
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Posts/edits GitHub comments",
                        ],
                    ],
                ],
            ]
        ],
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-success/10 flex items-center justify-center shrink-0"
                )[lucide_icon("shield-check", class_="size-5 text-success")],
                div(class_="space-y-2")[
                    h2(class_="font-semibold")["Security model"],
                    p(class_="text-xs text-muted-foreground")["Per-user isolation"],
                    ul(class_="space-y-1 text-sm text-muted-foreground mt-2")[
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Own GitHub App credentials",
                        ],
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Own model API keys in DB",
                        ],
                        li(class_="flex items-center gap-2")[
                            span(class_="text-success text-xs")["•"],
                            "Runtime key injection",
                        ],
                    ],
                ],
            ]
        ],
        card(class_="hover-lift border-dashed")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-muted flex items-center justify-center shrink-0"
                )[lucide_icon("laptop", class_="size-5 text-muted-foreground")],
                div(class_="space-y-2")[
                    h2(class_="font-semibold")["Local dev note"],
                    p(class_="text-xs text-muted-foreground")[
                        "GitHub requires public webhooks"
                    ],
                    p(class_="text-sm text-muted-foreground mt-2")[
                        "Use a tunnel (Cloudflare/ngrok) to expose webhooks for local dev."
                    ],
                ],
            ]
        ],
    ]

    # CTA with gradient border
    cta = div(
        class_="relative rounded-2xl border border-border overflow-hidden card-gradient-border"
    )[
        div(class_="absolute inset-0 bg-gradient-to-r from-primary/[0.03] to-transparent"),
        div(
            class_="relative p-6 sm:p-8 flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between"
        )[
            div(class_="space-y-2")[
                h2(class_="text-xl sm:text-2xl font-semibold")[
                    "Ready to ship calmer PR reviews?"
                ],
                p(class_="text-sm text-muted-foreground")[
                    "Install the GitHub App and set the first rule set in minutes."
                ],
            ],
            a(href=install_url, class_="group shrink-0")[
                button_component(
                    variant="primary",
                    class_="shadow-lg shadow-primary/20",
                )[
                    span["Get started"],
                    span(
                        class_="ml-1 group-hover:translate-x-0.5 transition-transform"
                    )["→"],
                ]
            ],
        ],
    ]

    content = div(class_="space-y-16")[
        hero,
        stats,
        # How it works section
        div(class_="space-y-8")[
            div(class_="text-center space-y-2")[
                span(
                    class_="inline-block px-3 py-1 rounded-full bg-primary/10 text-primary text-xs font-medium"
                )["How it works"],
                h2(class_="text-2xl sm:text-3xl font-bold tracking-tight")[
                    "End-to-end flow"
                ],
            ],
            setup_flow,
            runtime_flow,
        ],
        # Features section
        div(class_="space-y-8")[
            div(class_="text-center space-y-2")[
                h2(class_="text-2xl sm:text-3xl font-bold tracking-tight")[
                    "What you get"
                ],
                p(class_="text-muted-foreground max-w-lg mx-auto")[
                    "Everything you need for automated, learning code reviews"
                ],
            ],
            features,
        ],
        # Architecture section
        div(class_="space-y-8")[
            div(class_="space-y-2")[
                span(
                    class_="inline-block px-3 py-1 rounded-full bg-muted text-muted-foreground text-xs font-medium"
                )["Architecture"],
                h2(class_="text-2xl sm:text-3xl font-bold tracking-tight")[
                    "Control plane vs data plane"
                ],
            ],
            architecture,
        ],
        cta,
    ]

    return layout(request, content, page_title="CodeReview AI")


def _step_card(
    number: str, title: str, subtitle: str, bullets: list[str]
) -> Renderable:
    """Render a step card with a number indicator."""
    bullet_items = [
        li(class_="text-sm text-muted-foreground")[bullet] for bullet in bullets
    ]
    return card(class_="hover-lift relative overflow-hidden")[
        # Faded number background
        span(
            class_="absolute -top-4 -right-2 text-7xl font-bold text-muted-foreground/[0.06] select-none"
        )[number],
        div(class_="relative space-y-2")[
            div(class_="flex items-center gap-3")[
                span(
                    class_="w-7 h-7 rounded-full bg-primary/10 flex items-center justify-center text-xs font-semibold text-primary"
                )[number],
                div[
                    h2(class_="font-semibold text-foreground leading-tight")[title],
                    p(class_="text-xs text-muted-foreground")[subtitle],
                ],
            ],
            ul(class_="space-y-1 pl-10")[*bullet_items],
        ],
    ]


def _page_header(title: str, subtitle: str | None = None) -> Renderable:
    """Render a consistent page header."""
    return div(class_="space-y-1")[
        h1(class_="text-2xl sm:text-3xl font-bold tracking-tight")[title],
        p(class_="text-muted-foreground")[subtitle] if subtitle else span(),
    ]


def _auth_required_card() -> Renderable:
    """Render a card prompting the user to sign in."""
    return card(class_="max-w-md")[
        div(class_="flex items-start gap-4")[
            div(
                class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0"
            )[lucide_icon("key-round", class_="size-5 text-primary")],
            div(class_="space-y-3")[
                div[
                    h2(class_="font-semibold")["Sign in required"],
                    p(class_="text-sm text-muted-foreground")[
                        "Create an account to access this feature."
                    ],
                ],
                a(href="/account")[
                    button_component(variant="primary")["Go to account"]
                ],
            ],
        ]
    ]


def dashboard(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        content = div(class_="space-y-6")[
            _page_header(
                "Dashboard",
                "Sign in to manage installs and rules.",
            ),
            card(class_="max-w-md")[
                div(class_="flex items-start gap-4")[
                    div(
                        class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0"
                    )[lucide_icon("key-round", class_="size-5 text-primary")],
                    div(class_="space-y-3")[
                        div[
                            h2(class_="font-semibold")["Sign in required"],
                            p(class_="text-sm text-muted-foreground")[
                                "Create an account to connect GitHub."
                            ],
                        ],
                        a(href="/account")[
                            button_component(variant="primary")["Go to account"]
                        ],
                    ],
                ]
            ],
        ]
        return layout(request, content, page_title="Dashboard")

    now = timezone.now()
    _mark_stale_review_runs(owner=cast(User, request.user), now=now)

    github_apps = (
        GithubApp.objects.filter(owner=request.user).order_by("-updated_at").all()
    )
    cards: list[Renderable] = []

    if not github_apps.exists():
        cards.append(
            card(
                title="Create your GitHub App",
                description="We use GitHub's App Manifest flow (like Coolify).",
            )[
                form_component(action="/github/apps/create", method="post")[
                    csrf_input(request),
                    button_component(type="submit", variant="primary")[
                        "Create GitHub App"
                    ],
                ],
            ]
        )

    for github_app in github_apps:
        installations = (
            GithubInstallation.objects.filter(github_app=github_app)
            .prefetch_related("repositories")
            .order_by("-updated_at")
            .all()
        )
        install_link = (
            a(
                href=f"https://github.com/apps/{github_app.slug}/installations/new",
            )[button_component(variant="outline")["Install / Manage repos"]]
            if github_app.status == GithubApp.STATUS_READY and github_app.slug
            else span(class_="text-sm text-muted-foreground")[
                "Finish creating the app to get install link."
            ]
        )
        installation_list: list[Renderable] = []
        for installation in installations:
            active_repos = list(
                installation.repositories.filter(is_active=True)
                .order_by("full_name")
                .values_list("full_name", flat=True)
            )
            repo_limit = 10
            visible_repos = active_repos[:repo_limit]
            hidden_count = max(0, len(active_repos) - len(visible_repos))

            repos = [
                li(class_="text-sm text-muted-foreground")[name]
                for name in visible_repos
            ]
            more_repos: Renderable | None = None
            if hidden_count:
                more_repos = details(class_="pt-2")[
                    summary(
                        class_="cursor-pointer text-xs text-muted-foreground hover:text-foreground transition-colors"
                    )[f"Show {hidden_count} more repositories"],
                    div(class_="pt-2 max-h-64 overflow-y-auto")[
                        ul(class_="space-y-1")[
                            *[
                                li(class_="text-sm text-muted-foreground")[name]
                                for name in active_repos
                            ]
                        ]
                    ],
                ]
            installation_list.append(
                card(
                    title=installation.account_login or "Installation",
                    description=f"Installation ID: {installation.installation_id}",
                )[
                    div(class_="flex flex-wrap items-center gap-2")[
                        span(class_="text-xs text-muted-foreground")["Repositories"],
                        badge_count(
                            len(active_repos),
                            cap=999,
                            variant="secondary",
                        ),
                    ],
                    ul(class_="space-y-1")[*repos]
                    if repos
                    else p(class_="text-sm text-muted-foreground")[
                        "No repositories installed yet."
                    ],
                    more_repos if more_repos else span(),
                ]
            )

        cards.append(
            card(
                title=github_app.slug or github_app.desired_name,
                description=f"Status: {github_app.status}",
            )[
                div(class_="flex flex-wrap items-center gap-3")[
                    install_link,
                    a(href=f"/github/apps/{github_app.uuid}/setup")[
                        button_component(variant="outline")["Open setup"]
                    ],
                ],
                div(class_="pt-4 space-y-4")[*installation_list]
                if installation_list
                else div(class_="pt-4")[
                    p(class_="text-sm text-muted-foreground")[
                        "Install the app on an org/repo to start receiving webhooks."
                    ]
                ],
            ]
        )

    since = now - timedelta(days=7)
    recent_runs = (
        ReviewRun.objects.select_related("pull_request__repository")
        .filter(pull_request__repository__installation__github_app__owner=request.user)
        .order_by("-created_at")[:25]
    )
    run_count_7d = ReviewRun.objects.filter(
        pull_request__repository__installation__github_app__owner=request.user,
        created_at__gte=since,
    ).count()
    failed_count_7d = ReviewRun.objects.filter(
        pull_request__repository__installation__github_app__owner=request.user,
        created_at__gte=since,
        status=ReviewRun.STATUS_FAILED,
    ).count()

    run_rows: list[list[Node]] = []
    for run in recent_runs:
        pr = run.pull_request
        repo = pr.repository
        created = localtime(run.created_at).strftime("%Y-%m-%d %H:%M")
        sha_short = run.head_sha[:7]
        error_preview = run.error_message.strip()
        if len(error_preview) > 80:
            error_preview = f"{error_preview[:77]}..."

        run_rows.append(
            [
                created,
                a(
                    href=repo.html_url or "#",
                    class_="text-sm text-muted-foreground hover:text-foreground transition-colors",
                    target="_blank",
                    rel="noreferrer",
                )[
                    span(class_="inline-block max-w-[18rem] truncate align-middle")[
                        escape(repo.full_name)
                    ]
                ],
                a(
                    href=pr.html_url,
                    class_="text-sm text-muted-foreground hover:text-foreground transition-colors",
                    target="_blank",
                    rel="noreferrer",
                )[f"#{pr.pr_number}"],
                span(class_="font-mono text-xs text-muted-foreground")[sha_short],
                _review_run_status_badge(run.status),
                span(class_="text-xs text-muted-foreground")[
                    escape(error_preview)
                    if run.status == ReviewRun.STATUS_FAILED
                    else ""
                ],
                a(href=f"/app/review-runs/{run.id}")[
                    button_component(variant="outline")["Details"]
                ],
            ]
        )

    # Stats row
    stats_row = div(class_="grid gap-4 sm:grid-cols-3")[
        div(
            class_="flex items-center gap-3 p-4 rounded-lg border border-border bg-card"
        )[
            div(
                class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center"
            )[span(class_="text-primary font-mono text-sm font-bold")[run_count_7d]],
            div[
                p(class_="text-sm font-medium")["Review runs"],
                p(class_="text-xs text-muted-foreground")["Last 7 days"],
            ],
        ],
        div(
            class_="flex items-center gap-3 p-4 rounded-lg border border-border bg-card"
        )[
            div(
                class_="w-10 h-10 rounded-lg flex items-center justify-center "
                + ("bg-destructive/10" if failed_count_7d > 0 else "bg-success/10")
            )[
                span(
                    class_="font-mono text-sm font-bold "
                    + ("text-destructive" if failed_count_7d > 0 else "text-success")
                )[failed_count_7d]
            ],
            div[
                p(class_="text-sm font-medium")["Failures"],
                p(class_="text-xs text-muted-foreground")["Last 7 days"],
            ],
        ],
        div(
            class_="flex items-center gap-3 p-4 rounded-lg border border-border bg-card"
        )[
            div(
                class_="w-10 h-10 rounded-lg bg-success/10 flex items-center justify-center"
            )[lucide_icon("circle-check", class_="size-5 text-success")],
            div[
                p(class_="text-sm font-medium")[
                    f"{run_count_7d - failed_count_7d} Successful"
                ],
                p(class_="text-xs text-muted-foreground")["Last 7 days"],
            ],
        ],
    ]

    ops_card = card(bordered_header=True)[
        # Header
        div(class_="flex items-center justify-between")[
            div(class_="flex items-center gap-3")[
                div(
                    class_="w-8 h-8 rounded-lg bg-muted flex items-center justify-center"
                )[lucide_icon("chart-bar", class_="size-4 text-muted-foreground")],
                div[
                    h2(class_="font-semibold")["Operational visibility"],
                    p(class_="text-xs text-muted-foreground")[
                        "Review runs from your repositories"
                    ],
                ],
            ],
        ],
        # Table
        div(class_="pt-2")[
            table_component(
                headers=["When", "Repo", "PR", "SHA", "Status", "Error", ""],
                rows=run_rows,
            )
            if run_rows
            else div(class_="py-8 text-center")[
                div(
                    class_="w-12 h-12 rounded-full bg-muted flex items-center justify-center mx-auto mb-3"
                )[lucide_icon("inbox", class_="size-6 text-muted-foreground")],
                p(class_="text-sm font-medium")["No review runs yet"],
                p(class_="text-xs text-muted-foreground mt-1")[
                    "Open a PR on an installed repo to get started"
                ],
            ]
        ],
    ]

    content = div(class_="space-y-8")[
        _page_header("Dashboard", "Manage installs and repo coverage."),
        stats_row,
        ops_card,
        # GitHub Apps section
        div(class_="space-y-4")[
            div(class_="flex items-center gap-2")[
                lucide_icon("github", class_="size-5 text-foreground"),
                h2(class_="font-semibold")["GitHub Apps"],
            ],
            div(class_="grid gap-4 md:grid-cols-2")[*cards],
        ]
        if cards
        else span(),
    ]

    return layout(request, content, page_title="Dashboard")


def review_run_detail(request: HttpRequest, review_run_id: int) -> HttpResponse:
    if not request.user.is_authenticated:
        content = div(class_="space-y-6")[
            _page_header("Review run", "Sign in to see operational details."),
            _auth_required_card(),
        ]
        return layout(request, content, page_title="Review run")

    now = timezone.now()
    _mark_stale_review_runs(owner=cast(User, request.user), now=now)

    try:
        review_run = ReviewRun.objects.select_related(
            "pull_request__repository__installation__github_app"
        ).get(
            id=review_run_id,
            pull_request__repository__installation__github_app__owner=request.user,
        )
    except ReviewRun.DoesNotExist as e:
        raise Http404("Review run not found") from e

    pr = review_run.pull_request
    repo = pr.repository

    started_at = review_run.started_at
    finished_at = review_run.finished_at
    duration_text = "—"
    if started_at and finished_at:
        duration_text = str(finished_at - started_at).split(".", maxsplit=1)[0]

    meta_rows: list[list[Node]] = [
        [
            strong["Repository"],
            a(
                href=repo.html_url,
                target="_blank",
                rel="noreferrer",
                class_="text-sm text-muted-foreground hover:text-foreground transition-colors",
            )[escape(repo.full_name)],
        ],
        [
            strong["Pull request"],
            a(
                href=pr.html_url,
                target="_blank",
                rel="noreferrer",
                class_="text-sm text-muted-foreground hover:text-foreground transition-colors",
            )[escape(f"#{pr.pr_number} — {pr.title}")],
        ],
        [
            strong["Head SHA"],
            span(class_="font-mono text-xs text-muted-foreground")[review_run.head_sha],
        ],
        [strong["Status"], _review_run_status_badge(review_run.status)],
        [strong["Created"], _format_datetime(review_run.created_at)],
        [strong["Started"], _format_datetime(review_run.started_at)],
        [strong["Finished"], _format_datetime(review_run.finished_at)],
        [
            strong["Duration"],
            span(class_="text-sm text-muted-foreground")[duration_text],
        ],
        [
            strong["Run ID"],
            span(class_="font-mono text-xs text-muted-foreground")[str(review_run.id)],
        ],
    ]

    comments = review_run.comments.order_by("created_at").all()
    comment_nodes: list[Renderable] = []
    for comment in comments:
        comment_nodes.append(
            card(
                title=f"Comment {comment.github_comment_id or ''}".strip(),
                description=_format_datetime(comment.created_at),
                bordered_header=True,
            )[
                textarea_component(
                    value=escape(comment.body),
                    readonly=True,
                    rows=8,
                    class_="font-mono text-xs",
                )
            ]
        )

    content = div(class_="space-y-6")[
        # Header with back button
        div(class_="flex flex-wrap items-center justify-between gap-4")[
            div(class_="space-y-1")[
                div(class_="flex items-center gap-2")[
                    a(
                        href="/app",
                        class_="text-muted-foreground hover:text-foreground transition-colors",
                    )["← Dashboard"],
                    span(class_="text-muted-foreground/50")["/"],
                    span(class_="text-sm text-muted-foreground")["Review run"],
                ],
                h1(class_="text-2xl sm:text-3xl font-bold tracking-tight")[
                    f"Run #{review_run.id}"
                ],
            ],
            _review_run_status_badge(review_run.status),
        ],
        # Metadata card
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-muted flex items-center justify-center shrink-0"
                )[lucide_icon("file-text", class_="size-5 text-muted-foreground")],
                div(class_="flex-1 space-y-3")[
                    div[
                        h2(class_="font-semibold")["Run metadata"],
                        p(class_="text-xs text-muted-foreground")[
                            "Details about this review run"
                        ],
                    ],
                    table_component(headers=["Field", "Value"], rows=meta_rows),
                ],
            ]
        ],
        # Summary card
        card(bordered_header=True, class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-success/10 flex items-center justify-center shrink-0"
                )[lucide_icon("check", class_="size-5 text-success")],
                div(class_="flex-1 space-y-3")[
                    div[
                        h2(class_="font-semibold")["Summary"],
                        p(class_="text-xs text-muted-foreground")[
                            "The final text posted to GitHub"
                        ],
                    ],
                    textarea_component(
                        value=escape(review_run.summary or ""),
                        readonly=True,
                        rows=14,
                        class_="font-mono text-xs",
                    ),
                ],
            ]
        ],
        # Error card (only show if there's an error)
        card(bordered_header=True, class_="hover-lift border-destructive/30")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-destructive/10 flex items-center justify-center shrink-0"
                )[lucide_icon("triangle-alert", class_="size-5 text-destructive")],
                div(class_="flex-1 space-y-3")[
                    div[
                        h2(class_="font-semibold")["Error"],
                        p(class_="text-xs text-muted-foreground")[
                            "Populated only for failed runs"
                        ],
                    ],
                    textarea_component(
                        value=escape(review_run.error_message or "No errors"),
                        readonly=True,
                        rows=6,
                        class_="font-mono text-xs",
                    ),
                ],
            ]
        ]
        if review_run.error_message
        else span(),
        # Comments card
        card(bordered_header=True, class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0"
                )[lucide_icon("message-square", class_="size-5 text-primary")],
                div(class_="flex-1 space-y-3")[
                    div[
                        h2(class_="font-semibold")["Comments"],
                        p(class_="text-xs text-muted-foreground")[
                            "GitHub comments created for this run"
                        ],
                    ],
                    div(class_="space-y-4")[*comment_nodes]
                    if comment_nodes
                    else div(class_="py-4 text-center")[
                        p(class_="text-sm text-muted-foreground")[
                            "No comments recorded."
                        ]
                    ],
                ],
            ]
        ],
    ]
    return layout(request, content, page_title="Review run")


def account(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        if request.method == "POST":
            profile.github_login = request.POST.get("github_login", "").strip()
            profile.save(update_fields=["github_login"])
            return redirect("/account")
        user = cast(User, request.user)
        zai_key = (
            UserApiKey.objects.filter(
                user=user, provider=UserApiKey.PROVIDER_ZAI, is_active=True
            )
            .order_by("-updated_at")
            .first()
        )
        masked_zai = ""
        if zai_key:
            raw = zai_key.api_key.strip()
            masked_zai = f"****{raw[-4:]}" if len(raw) >= 4 else "****"

        github_app = (
            GithubApp.objects.filter(owner=user).order_by("-updated_at").first()
        )
        # GitHub App status badge
        app_status_badge = (
            div(
                class_="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs "
                + (
                    "bg-success/10 text-success"
                    if github_app and github_app.status == GithubApp.STATUS_READY
                    else "bg-warning/10 text-warning"
                )
            )[
                span(class_="w-1.5 h-1.5 rounded-full bg-current"),
                github_app.status if github_app else "Not created",
            ]
            if github_app
            else span(
                class_="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs bg-muted text-muted-foreground"
            )[
                span(class_="w-1.5 h-1.5 rounded-full bg-current"),
                "Not created",
            ]
        )

        content = div(class_="space-y-8")[
            _page_header("Account", "Manage your profile and integrations."),
            # Profile section
            div(class_="grid gap-6 lg:grid-cols-2")[
                card(class_="hover-lift")[
                    div(class_="flex items-start gap-4")[
                        div(
                            class_="w-12 h-12 rounded-full bg-primary/10 flex items-center justify-center shrink-0"
                        )[
                            span(class_="text-primary font-semibold text-lg")[
                                user.username[0].upper()
                            ]
                        ],
                        div(class_="flex-1 space-y-4")[
                            div(class_="flex items-center justify-between")[
                                div[
                                    h2(class_="font-semibold text-lg")[user.username],
                                    p(class_="text-xs text-muted-foreground")[
                                        user.email or "No email set"
                                    ],
                                ],
                                a(href="/account/logout")[
                                    button_component(variant="ghost", size="sm")[
                                        "Sign out"
                                    ]
                                ],
                            ],
                            form_component(action="/account", method="post")[
                                csrf_input(request),
                                form_field[
                                    input_component(
                                        name="github_login",
                                        label_text="GitHub login",
                                        placeholder="your-handle",
                                        value=profile.github_login,
                                    )
                                ],
                                button_component(type="submit", size="sm")["Save"],
                            ],
                        ],
                    ]
                ],
                # API Keys card
                card(class_="hover-lift")[
                    div(class_="flex items-start gap-4")[
                        div(
                            class_="w-10 h-10 rounded-lg bg-warning/10 flex items-center justify-center shrink-0"
                        )[span(class_="text-warning")["🔑"]],
                        div(class_="flex-1 space-y-3")[
                            div[
                                h2(class_="font-semibold")["API Keys"],
                                p(class_="text-xs text-muted-foreground")[
                                    "Per-user keys for model inference"
                                ],
                            ],
                            form_component(action="/account/api-keys", method="post")[
                                csrf_input(request),
                                form_field[
                                    input_component(
                                        name="zai_api_key",
                                        label_text="Z.AI Coding Plan API key (for zai-coding-plan/glm-4.7)",
                                        placeholder="paste your key",
                                        value=masked_zai,
                                    )
                                ],
                                button_component(type="submit", size="sm")["Save"],
                            ],
                            p(class_="text-xs text-muted-foreground")[
                                "Keys are injected at runtime when running reviews."
                            ],
                        ],
                    ]
                ],
            ],
            # GitHub App section
            card(class_="hover-lift")[
                div(class_="flex items-start gap-4")[
                    div(
                        class_="w-10 h-10 rounded-lg bg-muted flex items-center justify-center shrink-0"
                    )[span(class_="font-mono text-sm")["GH"]],
                    div(class_="flex-1 space-y-4")[
                        div(class_="flex items-start justify-between gap-4")[
                            div[
                                div(class_="flex items-center gap-2")[
                                    h2(class_="font-semibold")["GitHub App"],
                                    app_status_badge,
                                ],
                                p(class_="text-sm text-muted-foreground mt-1")[
                                    "Create your own GitHub App using the manifest flow."
                                ],
                            ],
                        ],
                        div(class_="flex flex-wrap items-center gap-2")[
                            form_component(
                                action="/github/apps/create",
                                method="post",
                                class_="inline",
                            )[
                                csrf_input(request),
                                button_component(type="submit", variant="primary")[
                                    "Create new app" if github_app else "Create GitHub App"
                                ],
                            ],
                            a(
                                href=f"/github/apps/{github_app.uuid}/setup"
                                if github_app
                                else "/app",
                            )[button_component(variant="outline")["Open setup"]]
                            if github_app
                            else span(),
                            a(
                                href=f"https://github.com/apps/{github_app.slug}/installations/new"
                                if github_app and github_app.slug
                                else "/app",
                            )[button_component(variant="outline")["Install on repos"]]
                            if github_app and github_app.slug
                            else span(),
                        ],
                    ],
                ]
            ],
        ]
        return layout(request, content, page_title="Account")

    # Unauthenticated view
    content = div(class_="space-y-8")[
        _page_header("Account", "Create an account to manage installs and rules."),
        div(class_="grid gap-6 md:grid-cols-2 max-w-3xl")[
            _signup_form(request),
            _login_form(request),
        ],
    ]
    return layout(request, content, page_title="Account")


def signup(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("/account")
    username = request.POST.get("username", "").strip()
    email = request.POST.get("email", "").strip()
    password = request.POST.get("password", "").strip()
    if not username or not password:
        messages.error(request, "Username and password are required.")
        return redirect("/account")
    if User.objects.filter(username=username).exists():
        messages.error(request, "Username already exists.")
        return redirect("/account")
    user = User.objects.create_user(username=username, email=email, password=password)
    UserProfile.objects.get_or_create(user=user)
    auth_login(request, user)
    return redirect("/account")


def login(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("/account")
    username = request.POST.get("username", "").strip()
    password = request.POST.get("password", "").strip()
    user = authenticate(request, username=username, password=password)
    if user is None:
        messages.error(request, "Invalid credentials.")
        return redirect("/account")
    auth_login(request, user)
    return redirect("/account")


def logout(request: HttpRequest) -> HttpResponse:
    auth_logout(request)
    return redirect("/")


def save_api_keys(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("/account")
    if not request.user.is_authenticated:
        return redirect("/account")
    user = cast(User, request.user)
    zai_api_key = request.POST.get("zai_api_key", "").strip()
    if zai_api_key and not zai_api_key.startswith("****"):
        UserApiKey.objects.update_or_create(
            user=user,
            provider=UserApiKey.PROVIDER_ZAI,
            defaults={"api_key": zai_api_key, "is_active": True},
        )
    return redirect("/account")


def create_github_app(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("/app")
    if not request.user.is_authenticated:
        return redirect("/account")
    user = cast(User, request.user)
    suffix = secrets.token_hex(3)
    desired_name = f"CodeReview AI - {user.username} - {suffix}"
    github_app = GithubApp.objects.create(owner=user, desired_name=desired_name)
    return redirect(f"/github/apps/{github_app.uuid}/setup")


def github_app_setup(request: HttpRequest, app_uuid: UUID) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("/account")
    github_app = GithubApp.objects.filter(uuid=app_uuid, owner=request.user).first()
    if not github_app:
        raise Http404

    base_url = request.build_absolute_uri("/").rstrip("/")
    manifest = {
        "name": github_app.desired_name,
        "url": base_url,
        "hook_attributes": {
            "url": f"{base_url}/github/webhook/{github_app.uuid}",
            "active": True,
        },
        "redirect_url": f"{base_url}/github/apps/redirect",
        "public": False,
        "request_oauth_on_install": False,
        "setup_url": f"{base_url}/github/apps/install?source={github_app.uuid}",
        "setup_on_update": True,
        "default_permissions": {
            "contents": "read",
            "metadata": "read",
            "pull_requests": "write",
            "issues": "write",
        },
        "default_events": [
            "pull_request",
            "issue_comment",
        ],
    }

    script_body = (
        "(() => {"
        f"const manifest = {json.dumps(manifest)};"
        f"const state = {json.dumps(str(github_app.uuid))};"
        "const form = document.createElement('form');"
        "form.method = 'post';"
        "form.action = `https://github.com/settings/apps/new?state=${state}`;"
        "const input = document.createElement('input');"
        "input.type = 'hidden';"
        "input.name = 'manifest';"
        "input.value = JSON.stringify(manifest);"
        "form.appendChild(input);"
        "document.body.appendChild(form);"
        "form.submit();"
        "})();"
    )

    content = div(class_="space-y-6")[
        section_header(
            "Create GitHub App",
            subtitle="Redirecting you to GitHub to create the app…",
            align="left",
        ),
        card(
            title="GitHub App manifest",
            description="If you are not redirected, click the button.",
        )[
            form_component(
                action=f"https://github.com/settings/apps/new?state={github_app.uuid}",
                method="post",
            )[
                input_el(type="hidden", name="manifest", value=json.dumps(manifest)),
                button_component(type="submit", variant="primary")[
                    "Continue to GitHub"
                ],
            ],
            script[script_body],
        ],
    ]
    return layout(request, content, page_title="Create GitHub App")


def github_app_redirect(request: HttpRequest) -> HttpResponse:
    code = request.GET.get("code", "").strip()
    state = request.GET.get("state", "").strip()
    if not code or not state:
        raise Http404

    github_app = GithubApp.objects.filter(uuid=state).first()
    if not github_app:
        raise Http404

    data = github.convert_manifest_code(code)
    github_app.app_id = data.get("id")
    github_app.slug = data.get("slug", "")
    github_app.client_id = data.get("client_id", "")
    github_app.client_secret = data.get("client_secret", "")
    github_app.private_key_pem = data.get("pem", "")
    github_app.webhook_secret = data.get("webhook_secret", "")
    github_app.status = GithubApp.STATUS_READY
    github_app.save(
        update_fields=[
            "app_id",
            "slug",
            "client_id",
            "client_secret",
            "private_key_pem",
            "webhook_secret",
            "status",
            "updated_at",
        ]
    )

    return redirect("/app")


def github_app_install(request: HttpRequest) -> HttpResponse:
    source = request.GET.get("source", "").strip()
    setup_action = request.GET.get("setup_action", "").strip()
    installation_id = request.GET.get("installation_id", "").strip()
    if source and setup_action == "install":
        messages.success(
            request,
            f"GitHub App installed (installation_id={installation_id}). Waiting for webhooks.",
        )
    return redirect("/app")


def rules(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("/account")

    rule_sets = (
        RuleSet.objects.prefetch_related("rules", "repository")
        .filter(owner=request.user)
        .all()
    )
    repositories = (
        GithubRepository.objects.filter(
            is_active=True,
            installation__github_app__owner=request.user,
        )
        .order_by("full_name")
        .all()
    )

    content = div(class_="space-y-8")[
        _page_header(
            "Review Rules",
            "Global rules apply everywhere. Repo rules override or extend them.",
        ),
        div(class_="grid gap-6 lg:grid-cols-[1fr_1.5fr]")[
            _rule_set_form(request, repositories),
            div(class_="space-y-4")[*_rule_sets_block(request, rule_sets)]
            if list(rule_sets)
            else div(class_="flex items-center justify-center p-12 rounded-lg border border-dashed border-border")[
                div(class_="text-center space-y-2")[
                    lucide_icon("clipboard-list", class_="size-8 text-muted-foreground mx-auto"),
                    p(class_="font-medium")["No rule sets yet"],
                    p(class_="text-sm text-muted-foreground")[
                        "Create your first rule set to get started"
                    ],
                ]
            ],
        ],
    ]

    return layout(request, content, page_title="Rules")


def create_rule_set(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("/rules")
    if not request.user.is_authenticated:
        return redirect("/account")
    name = request.POST.get("name", "New Rules")
    scope = request.POST.get("scope", RuleSet.SCOPE_GLOBAL)
    repo_id = request.POST.get("repository_id")
    instructions = request.POST.get("instructions", "")
    repository_id = int(repo_id) if repo_id else None
    if scope == RuleSet.SCOPE_REPO and not repository_id:
        messages.error(request, "Select a repository for repo-scoped rules.")
        return redirect("/rules")
    RuleSet.objects.create(
        owner=request.user,
        name=name,
        scope=scope,
        repository_id=repository_id,
        instructions=instructions,
    )
    return redirect("/rules")


def add_rule(request: HttpRequest, rule_set_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("/rules")
    if not request.user.is_authenticated:
        return redirect("/account")
    title = request.POST.get("title", "New rule")
    description = request.POST.get("description", "")
    severity = request.POST.get("severity", "info")
    rule_set = RuleSet.objects.filter(id=rule_set_id, owner=request.user).first()
    if not rule_set:
        raise Http404
    Rule.objects.create(
        rule_set=rule_set,
        title=title,
        description=description,
        severity=severity,
    )
    return redirect("/rules")


def delete_rule_set(request: HttpRequest, rule_set_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("/rules")
    if not request.user.is_authenticated:
        return redirect("/account")
    rule_set = RuleSet.objects.filter(id=rule_set_id, owner=request.user).first()
    if not rule_set:
        raise Http404
    rule_set.delete()
    messages.success(request, "Rule set deleted.")
    return redirect("/rules")


def delete_rule(request: HttpRequest, rule_set_id: int, rule_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("/rules")
    if not request.user.is_authenticated:
        return redirect("/account")
    rule_set = RuleSet.objects.filter(id=rule_set_id, owner=request.user).first()
    if not rule_set:
        raise Http404
    Rule.objects.filter(id=rule_id, rule_set=rule_set).delete()
    messages.success(request, "Rule deleted.")
    return redirect("/rules")


def feedback(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return redirect("/account")

    repositories = (
        GithubRepository.objects.filter(
            is_active=True,
            installation__github_app__owner=request.user,
        )
        .order_by("full_name")
        .all()
    )
    repo_id_raw = request.GET.get("repo_id", "").strip()
    valid_repo_ids = {repo.id for repo in repositories}
    repo_id = (
        int(repo_id_raw)
        if repo_id_raw.isdigit() and int(repo_id_raw) in valid_repo_ids
        else None
    )

    limit_raw = request.GET.get("limit", "").strip()
    limit = 50
    if limit_raw.isdigit():
        limit = max(10, min(200, int(limit_raw)))

    feedback_qs = FeedbackSignal.objects.select_related(
        "review_comment__review_run__pull_request__repository"
    ).filter(
        review_comment__review_run__pull_request__repository__installation__github_app__owner=request.user
    )
    mention_qs = ChatMessage.objects.select_related(
        "pull_request__repository",
    ).filter(
        pull_request__repository__installation__github_app__owner=request.user,
        is_hidden=False,
        body__icontains="@codereview",
    )

    if repo_id:
        feedback_qs = feedback_qs.filter(
            review_comment__review_run__pull_request__repository_id=repo_id
        )
        mention_qs = mention_qs.filter(pull_request__repository_id=repo_id)

    recent_feedback = feedback_qs.order_by("-created_at")[:limit]
    recent_mentions = mention_qs.order_by("-created_at")[:limit]

    repo_select = select(
        name="repo_id",
        class_="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground",
    )[
        option(value="")["All repositories"],
        *[
            option(value=str(repo.id), selected=str(repo.id) == str(repo_id_raw))[
                repo.full_name
            ]
            for repo in repositories
        ],
    ]

    feedback_items: list[Renderable] = []
    for signal in recent_feedback:
        review_comment = signal.review_comment
        review_run = review_comment.review_run
        pull_request = review_run.pull_request
        repo = pull_request.repository
        created = localtime(signal.created_at).strftime("%Y-%m-%d %H:%M")
        excerpt = (review_comment.body or "").strip().splitlines()[0:1]
        excerpt_text = excerpt[0][:160] if excerpt else ""
        github_link = ""
        if pull_request.html_url and review_comment.github_comment_id:
            github_link = f"{pull_request.html_url}#issuecomment-{review_comment.github_comment_id}"
        feedback_items.append(
            li(class_="rounded-lg border border-border/60 p-4")[
                div(class_="flex flex-wrap items-center justify-between gap-3")[
                    div(class_="grid gap-1")[
                        strong[f"{repo.full_name} #{pull_request.pr_number}"],
                        span(class_="text-xs text-muted-foreground")[
                            f"{created} • signal={signal.signal}"
                        ],
                        a(
                            href=github_link,
                            class_="text-xs text-muted-foreground hover:text-foreground",
                        )["Open comment in GitHub"]
                        if github_link
                        else span(),
                        span(class_="text-sm text-muted-foreground")[
                            escape(excerpt_text)
                        ],
                    ],
                    div(class_="flex flex-wrap items-center gap-2")[
                        form_component(
                            action=f"/feedback/signals/{signal.id}/update",
                            method="post",
                            class_="flex items-center gap-2",
                        )[
                            csrf_input(request),
                            select(
                                name="signal",
                                class_="rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground",
                            )[
                                option(
                                    value=FeedbackSignal.SIGNAL_LIKE,
                                    selected=signal.signal
                                    == FeedbackSignal.SIGNAL_LIKE,
                                )["like"],
                                option(
                                    value=FeedbackSignal.SIGNAL_IGNORE,
                                    selected=signal.signal
                                    == FeedbackSignal.SIGNAL_IGNORE,
                                )["ignore"],
                                option(
                                    value=FeedbackSignal.SIGNAL_DISLIKE,
                                    selected=signal.signal
                                    == FeedbackSignal.SIGNAL_DISLIKE,
                                )["dislike"],
                            ],
                            button_component(
                                type="submit",
                                variant="outline",
                                size="sm",
                            )["Update"],
                        ],
                        form_component(
                            action=f"/feedback/signals/{signal.id}/delete",
                            method="post",
                            class_="inline",
                        )[
                            csrf_input(request),
                            button_component(
                                type="submit",
                                variant="destructive",
                                size="sm",
                            )["Delete"],
                        ],
                    ],
                ]
            ]
        )

    mention_items: list[Renderable] = []
    for message in recent_mentions:
        pull_request = message.pull_request
        repo = pull_request.repository
        created = localtime(message.created_at).strftime("%Y-%m-%d %H:%M")
        first_line = (message.body or "").strip().splitlines()[0:1]
        excerpt_text = first_line[0][:200] if first_line else ""
        github_link = ""
        if pull_request.html_url and message.github_comment_id:
            github_link = (
                f"{pull_request.html_url}#issuecomment-{message.github_comment_id}"
            )
        mention_items.append(
            li(class_="rounded-lg border border-border/60 p-4")[
                div(class_="flex flex-wrap items-center justify-between gap-3")[
                    div(class_="grid gap-1")[
                        strong[f"{repo.full_name} #{pull_request.pr_number}"],
                        span(class_="text-xs text-muted-foreground")[
                            f"{created} • author={message.author}"
                        ],
                        a(
                            href=github_link,
                            class_="text-xs text-muted-foreground hover:text-foreground",
                        )["Open comment in GitHub"]
                        if github_link
                        else span(),
                        span(class_="text-sm text-muted-foreground")[
                            escape(excerpt_text)
                        ],
                    ],
                    form_component(
                        action=f"/feedback/mentions/{message.id}/delete",
                        method="post",
                        class_="inline",
                    )[
                        csrf_input(request),
                        button_component(
                            type="submit",
                            variant="destructive",
                            size="sm",
                        )["Hide"],
                    ],
                ]
            ]
        )

    # Stats summary
    stats_row = div(class_="grid gap-4 sm:grid-cols-3")[
        div(
            class_="flex items-center gap-3 p-4 rounded-lg border border-border bg-card"
        )[
            div(
                class_="w-10 h-10 rounded-lg bg-success/10 flex items-center justify-center"
            )[lucide_icon("thumbs-up", class_="size-5 text-success")],
            div[
                p(class_="text-sm font-medium")["Feedback signals"],
                p(class_="text-xs text-muted-foreground")[
                    f"{len(list(recent_feedback))} recorded"
                ],
            ],
        ],
        div(
            class_="flex items-center gap-3 p-4 rounded-lg border border-border bg-card"
        )[
            div(
                class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center"
            )[lucide_icon("at-sign", class_="size-5 text-primary")],
            div[
                p(class_="text-sm font-medium")["Mentions"],
                p(class_="text-xs text-muted-foreground")[
                    f"{len(list(recent_mentions))} recorded"
                ],
            ],
        ],
        div(
            class_="flex items-center gap-3 p-4 rounded-lg border border-border bg-card"
        )[
            div(
                class_="w-10 h-10 rounded-lg bg-muted flex items-center justify-center"
            )[lucide_icon("target", class_="size-5 text-muted-foreground")],
            div[
                p(class_="text-sm font-medium")["Learning"],
                p(class_="text-xs text-muted-foreground")["Improving reviews"],
            ],
        ],
    ]

    content = div(class_="space-y-8")[
        _page_header(
            "Feedback",
            "Inspect and clean up what the reviewer has learned.",
        ),
        stats_row,
        # Filter card
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-muted flex items-center justify-center shrink-0"
                )[lucide_icon("search", class_="size-5 text-muted-foreground")],
                div(class_="flex-1 space-y-3")[
                    div[
                        h2(class_="font-semibold")["Filter"],
                        p(class_="text-xs text-muted-foreground")[
                            "Scope results by repository"
                        ],
                    ],
                    form_component(
                        action="/feedback", method="get", class_="flex items-end gap-3"
                    )[
                        div(class_="flex-1")[form_field[repo_select]],
                        input_el(type="hidden", name="limit", value=str(limit)),
                        button_component(type="submit", variant="outline")["Apply"],
                    ],
                ],
            ]
        ],
        # Feedback signals
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-success/10 flex items-center justify-center shrink-0"
                )[lucide_icon("thumbs-up", class_="size-5 text-success")],
                div(class_="flex-1 space-y-3")[
                    div[
                        h2(class_="font-semibold")["Feedback signals"],
                        p(class_="text-xs text-muted-foreground")[
                            "Signals from /ai like, /ai dislike, /ai ignore"
                        ],
                    ],
                    ul(class_="space-y-3")[*feedback_items]
                    if feedback_items
                    else div(class_="py-4 text-center")[
                        p(class_="text-sm text-muted-foreground")[
                            "No feedback signals recorded yet."
                        ]
                    ],
                    a(
                        href=_feedback_more_link(repo_id_raw, limit),
                        class_="inline-block text-sm text-primary hover:underline",
                    )["Load more →"]
                    if len(recent_feedback) == limit
                    else span(),
                ],
            ]
        ],
        # Mentions
        card(class_="hover-lift")[
            div(class_="flex items-start gap-4")[
                div(
                    class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0"
                )[lucide_icon("message-square", class_="size-5 text-primary")],
                div(class_="flex-1 space-y-3")[
                    div[
                        h2(class_="font-semibold")["Mentions"],
                        p(class_="text-xs text-muted-foreground")[
                            "PR comments where @codereview was mentioned"
                        ],
                    ],
                    ul(class_="space-y-3")[*mention_items]
                    if mention_items
                    else div(class_="py-4 text-center")[
                        p(class_="text-sm text-muted-foreground")[
                            "No @codereview mentions recorded yet."
                        ]
                    ],
                    a(
                        href=_feedback_more_link(repo_id_raw, limit),
                        class_="inline-block text-sm text-primary hover:underline",
                    )["Load more →"]
                    if len(recent_mentions) == limit
                    else span(),
                ],
            ]
        ],
    ]
    return layout(request, content, page_title="Feedback")


def _feedback_more_link(repo_id_raw: str, limit: int) -> str:
    new_limit = min(200, limit + 50)
    if repo_id_raw:
        return f"/feedback?repo_id={repo_id_raw}&limit={new_limit}"
    return f"/feedback?limit={new_limit}"


def update_feedback_signal(request: HttpRequest, signal_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("/feedback")
    if not request.user.is_authenticated:
        return redirect("/account")

    signal = (
        FeedbackSignal.objects.select_related(
            "review_comment__review_run__pull_request__repository__installation__github_app"
        )
        .filter(
            id=signal_id,
            review_comment__review_run__pull_request__repository__installation__github_app__owner=request.user,
        )
        .first()
    )
    if not signal:
        raise Http404

    new_signal = request.POST.get("signal", "").strip()
    allowed = {
        FeedbackSignal.SIGNAL_LIKE,
        FeedbackSignal.SIGNAL_IGNORE,
        FeedbackSignal.SIGNAL_DISLIKE,
    }
    if new_signal not in allowed:
        messages.error(request, "Invalid signal value.")
        return redirect("/feedback")
    signal.signal = new_signal
    signal.save(update_fields=["signal"])
    messages.success(request, "Feedback updated.")
    return redirect("/feedback")


def delete_feedback_signal(request: HttpRequest, signal_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("/feedback")
    if not request.user.is_authenticated:
        return redirect("/account")

    signal = (
        FeedbackSignal.objects.select_related(
            "review_comment__review_run__pull_request__repository__installation__github_app"
        )
        .filter(
            id=signal_id,
            review_comment__review_run__pull_request__repository__installation__github_app__owner=request.user,
        )
        .first()
    )
    if not signal:
        raise Http404
    signal.delete()
    messages.success(request, "Feedback deleted.")
    return redirect("/feedback")


def delete_mention_message(request: HttpRequest, message_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("/feedback")
    if not request.user.is_authenticated:
        return redirect("/account")

    message = (
        ChatMessage.objects.select_related(
            "pull_request__repository__installation__github_app"
        )
        .filter(
            id=message_id,
            pull_request__repository__installation__github_app__owner=request.user,
        )
        .first()
    )
    if not message:
        raise Http404
    message.is_hidden = True
    message.hidden_at = timezone.now()
    message.save(update_fields=["is_hidden", "hidden_at"])
    messages.success(request, "Message hidden.")
    return redirect("/feedback")


@csrf_exempt
def github_webhook(request: HttpRequest) -> HttpResponse:
    return _github_webhook_impl(request, github_app=None)


@csrf_exempt
def github_webhook_app(request: HttpRequest, app_uuid: UUID) -> HttpResponse:
    github_app = GithubApp.objects.filter(uuid=app_uuid).first()
    if not github_app:
        raise Http404
    if not github_app.webhook_secret:
        return JsonResponse({"error": "github app not ready"}, status=400)
    return _github_webhook_impl(request, github_app=github_app)


def _github_webhook_impl(
    request: HttpRequest, *, github_app: GithubApp | None
) -> HttpResponse:
    signature = request.headers.get("X-Hub-Signature-256", "")
    secret = github_app.webhook_secret if github_app else settings.GITHUB_WEBHOOK_SECRET
    if not verify_webhook_signature(request.body, signature, secret):
        logger.warning(
            "github_webhook.invalid_signature delivery=%s event=%s app_uuid=%s",
            request.headers.get("X-GitHub-Delivery", ""),
            request.headers.get("X-GitHub-Event", ""),
            str(getattr(github_app, "uuid", "")),
        )
        return JsonResponse({"error": "invalid signature"}, status=400)

    event = request.headers.get("X-GitHub-Event", "")
    payload = parse_webhook_body(request.body)
    installation_id = payload.get("installation", {}).get("id")
    repo_full_name = payload.get("repository", {}).get("full_name")
    action = payload.get("action")
    logger.info(
        "github_webhook.received delivery=%s event=%s action=%s app_uuid=%s installation_id=%s repo=%s",
        request.headers.get("X-GitHub-Delivery", ""),
        event,
        action,
        str(getattr(github_app, "uuid", "")),
        installation_id,
        repo_full_name,
    )

    if event == "installation":
        installation = (
            upsert_installation_for_app(payload["installation"], github_app)
            if github_app
            else upsert_installation(payload["installation"])
        )
        logger.info(
            "github_webhook.installation_upserted app_uuid=%s installation_id=%s account=%s",
            str(getattr(github_app, "uuid", "")),
            installation.installation_id,
            installation.account_login,
        )
        try:
            auth = github.auth_for_installation(installation)
            repos = github.list_installation_repositories(
                installation_id=installation.installation_id,
                auth=auth,
            )
            synced_repo_ids: set[int] = set()
            for repo in repos:
                upsert_repository(installation, repo)
                repo_id = repo.get("id")
                if isinstance(repo_id, int):
                    synced_repo_ids.add(repo_id)
            if synced_repo_ids:
                GithubRepository.objects.filter(installation=installation).exclude(
                    repo_id__in=synced_repo_ids
                ).update(is_active=False)
            logger.info(
                "github_webhook.installation_repo_sync app_uuid=%s installation_id=%s repos=%s",
                str(getattr(github_app, "uuid", "")),
                installation.installation_id,
                len(synced_repo_ids),
            )
        except Exception:
            logger.exception(
                "github_webhook.installation_repo_sync_failed app_uuid=%s installation_id=%s",
                str(getattr(github_app, "uuid", "")),
                installation.installation_id,
            )
        return JsonResponse(
            {"status": "ok", "installation": installation.installation_id}
        )

    if event == "installation_repositories":
        installation = (
            upsert_installation_for_app(payload["installation"], github_app)
            if github_app
            else upsert_installation(payload["installation"])
        )
        for repo in payload.get("repositories_added", []):
            upsert_repository(installation, repo)
        for repo in payload.get("repositories_removed", []):
            deactivate_repository(installation, repo)
        logger.info(
            "github_webhook.installation_repositories app_uuid=%s installation_id=%s added=%s removed=%s",
            str(getattr(github_app, "uuid", "")),
            installation.installation_id,
            len(payload.get("repositories_added", [])),
            len(payload.get("repositories_removed", [])),
        )
        return JsonResponse({"status": "ok"})

    if event == "pull_request":
        action = payload.get("action")
        if action in {"opened", "reopened", "synchronize"}:
            installation = (
                upsert_installation_for_app(payload["installation"], github_app)
                if github_app
                else upsert_installation(payload["installation"])
            )
            repo = upsert_repository(installation, payload["repository"])
            pull_request = upsert_pull_request(repo, payload["pull_request"])
            head_sha = payload["pull_request"]["head"]["sha"]
            queue_review(pull_request, head_sha)
            logger.info(
                "github_webhook.review_queued app_uuid=%s installation_id=%s repo=%s pr=%s sha=%s",
                str(getattr(github_app, "uuid", "")),
                installation.installation_id,
                repo.full_name,
                pull_request.pr_number,
                head_sha,
            )
        return JsonResponse({"status": "ok"})

    if event == "issue_comment":
        if "pull_request" in payload.get("issue", {}):
            sender = payload.get("sender") or {}
            sender_type = str(sender.get("type", ""))
            sender_login = str(sender.get("login", ""))
            if sender_type.lower() == "bot" or sender_login.endswith("[bot]"):
                logger.info(
                    "github_webhook.ignore_bot_comment delivery=%s app_uuid=%s login=%s",
                    request.headers.get("X-GitHub-Delivery", ""),
                    str(getattr(github_app, "uuid", "")),
                    sender_login,
                )
                return JsonResponse({"status": "ok"})
            installation_id = payload.get("installation", {}).get("id")
            repo_id = payload.get("repository", {}).get("id")
            pr_number = payload["issue"]["number"]
            qs = PullRequest.objects.filter(
                repository__repo_id=repo_id,
                repository__installation__installation_id=installation_id,
                pr_number=pr_number,
            )
            if github_app:
                qs = qs.filter(repository__installation__github_app=github_app)
            pull_request = qs.first()
            if pull_request:
                body_text = payload["comment"]["body"]
                _try_record_feedback(pull_request, body_text)
                normalized = body_text.strip().lower()
                is_feedback = normalized.startswith(
                    ("/ai like", "/ai dislike", "/ai ignore")
                )
                should_respond = ("@codereview" in normalized) and not is_feedback
                if should_respond:
                    try:
                        comment_id = int(payload["comment"]["id"])
                        repository = pull_request.repository
                        installation = repository.installation
                        auth = github.auth_for_installation(installation)
                        github.add_reaction_to_issue_comment(
                            installation_id=installation.installation_id,
                            auth=auth,
                            repo_full_name=repository.full_name,
                            comment_id=comment_id,
                            content="eyes",
                        )
                    except Exception:
                        logger.exception(
                            "github_webhook.react_failed delivery=%s app_uuid=%s",
                            request.headers.get("X-GitHub-Delivery", ""),
                            str(getattr(github_app, "uuid", "")),
                        )
                record_chat_message(
                    pull_request,
                    payload["comment"],
                    respond=should_respond,
                )
        return JsonResponse({"status": "ok"})

    return JsonResponse({"status": "ignored"})


def _try_record_feedback(pull_request: PullRequest, body_text: str) -> None:
    normalized = body_text.strip().lower()
    signal = None
    if normalized.startswith("/ai like"):
        signal = FeedbackSignal.SIGNAL_LIKE
    elif normalized.startswith("/ai dislike"):
        signal = FeedbackSignal.SIGNAL_DISLIKE
    elif normalized.startswith("/ai ignore"):
        signal = FeedbackSignal.SIGNAL_IGNORE
    if not signal:
        return
    latest_comment = (
        pull_request.review_runs.order_by("-id").prefetch_related("comments").first()
    )
    if not latest_comment or not latest_comment.comments.exists():
        return
    review_comment = latest_comment.comments.latest("id")
    FeedbackSignal.objects.create(review_comment=review_comment, signal=signal)


def _rule_set_form(
    request: HttpRequest, repositories: Iterable[GithubRepository]
) -> Renderable:
    repo_list = list(repositories)
    if repo_list:
        repo_block: Renderable = select(
            name="repository_id",
            class_="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground",
        )[
            option(value="")["Select a repository…"],
            *[option(value=str(repo.id))[repo.full_name] for repo in repo_list],
        ]
    else:
        repo_block = p(class_="text-sm text-muted-foreground")[
            "No repositories found yet. Install the GitHub App on repos (or open a PR) to populate the list."
        ]

    return card(
        title="Create Rule Set", description="Define global or repo-specific rules."
    )[
        div(x_data="{ scope: 'global' }")[
            form_component(action="/rules/create", method="post")[
                csrf_input(request),
                form_field[
                    input_component(
                        name="name",
                        label_text="Name",
                        placeholder="API Review Rules",
                    )
                ],
                form_field[
                    div(class_="grid gap-2")[
                        span(class_="text-sm font-medium")["Scope"],
                        div(class_="flex flex-wrap gap-4")[
                            label_with_radio(
                                "Global",
                                name="scope",
                                value="global",
                                checked=True,
                                x_model="scope",
                            ),
                            label_with_radio(
                                "Repository",
                                name="scope",
                                value="repo",
                                x_model="scope",
                            ),
                        ],
                    ]
                ],
                form_field[
                    div(
                        class_="grid gap-2",
                        x_show="scope === 'repo'",
                    )[
                        span(class_="text-sm font-medium")["Repository"],
                        repo_block,
                    ]
                ],
                form_field[
                    textarea_component(
                        name="instructions",
                        label_text="Instructions",
                        placeholder="Keep reviews crisp, prefer diff-only comments.",
                        rows=4,
                    )
                ],
                button_component(type="submit")[["Create"]],
            ]
        ]
    ]


def _rule_sets_block(
    request: HttpRequest, rule_sets: Iterable[RuleSet]
) -> list[Renderable]:
    blocks: list[Renderable] = []
    for rule_set in rule_sets:
        repo_label = ""
        if rule_set.scope == RuleSet.SCOPE_REPO and rule_set.repository:
            repo_label = f" • repo={rule_set.repository.full_name}"
        rules = [
            li(class_="flex flex-wrap items-start justify-between gap-3")[
                div(class_="grid gap-1")[
                    strong[rule.title],
                    span(class_="text-muted-foreground")[
                        f" — {escape(rule.description)}"
                    ],
                    span(class_="text-xs text-muted-foreground")[
                        f"severity={rule.severity}"
                    ],
                ],
                form_component(
                    action=f"/rules/{rule_set.id}/rules/{rule.id}/delete",
                    method="post",
                    class_="inline",
                )[
                    csrf_input(request),
                    button_component(
                        type="submit",
                        variant="destructive",
                        size="sm",
                    )["Delete"],
                ],
            ]
            for rule in rule_set.rules.all()
        ]

        blocks.append(
            card(
                title=rule_set.name,
                description=f"Scope: {rule_set.scope}{repo_label}",
                action=form_component(
                    action=f"/rules/{rule_set.id}/delete",
                    method="post",
                    class_="inline",
                )[
                    csrf_input(request),
                    button_component(
                        type="submit",
                        variant="destructive",
                        size="sm",
                    )["Delete set"],
                ],
            )[
                p(class_="text-sm text-muted-foreground")[
                    rule_set.instructions or "No instructions yet."
                ],
                ul(class_="mt-4 space-y-2")[*rules]
                if rules
                else p(class_="text-sm text-muted-foreground")["No rules added yet."],
                form_component(
                    action=f"/rules/{rule_set.id}/add", method="post", class_="mt-4"
                )[
                    csrf_input(request),
                    form_field[
                        input_component(
                            name="title",
                            label_text="Rule title",
                            placeholder="Prefer smaller diffs",
                        )
                    ],
                    form_field[
                        input_component(
                            name="description",
                            label_text="Description",
                            placeholder="Flag large PRs without tests.",
                        )
                    ],
                    form_field[
                        input_component(
                            name="severity",
                            label_text="Severity",
                            placeholder="info | warn | block",
                        )
                    ],
                    button_component(type="submit", variant="outline")[["Add Rule"]],
                ],
            ]
        )
    return blocks


def _signup_form(request: HttpRequest) -> Renderable:
    return card(class_="hover-lift")[
        div(class_="flex items-start gap-4")[
            div(
                class_="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0"
            )[lucide_icon("sparkles", class_="size-5 text-primary")],
            div(class_="flex-1 space-y-4")[
                div[
                    h2(class_="font-semibold")["Create account"],
                    p(class_="text-sm text-muted-foreground")[
                        "Sign up to manage installs and rules."
                    ],
                ],
                form_component(action="/account/signup", method="post")[
                    csrf_input(request),
                    form_field[
                        input_component(
                            name="username",
                            label_text="Username",
                            placeholder="yourname",
                        )
                    ],
                    form_field[
                        input_component(
                            name="email",
                            label_text="Email",
                            placeholder="you@example.com",
                            type="email",
                        )
                    ],
                    form_field[
                        input_component(
                            name="password", label_text="Password", type="password"
                        )
                    ],
                    button_component(
                        type="submit",
                        variant="primary",
                        class_="w-full",
                    )["Create account"],
                ],
            ],
        ]
    ]


def _login_form(request: HttpRequest) -> Renderable:
    return card(class_="hover-lift")[
        div(class_="flex items-start gap-4")[
            div(
                class_="w-10 h-10 rounded-lg bg-muted flex items-center justify-center shrink-0"
            )[lucide_icon("log-in", class_="size-5 text-muted-foreground")],
            div(class_="flex-1 space-y-4")[
                div[
                    h2(class_="font-semibold")["Sign in"],
                    p(class_="text-sm text-muted-foreground")[
                        "Access your existing account."
                    ],
                ],
                form_component(action="/account/login", method="post")[
                    csrf_input(request),
                    form_field[
                        input_component(name="username", label_text="Username")
                    ],
                    form_field[
                        input_component(
                            name="password", label_text="Password", type="password"
                        )
                    ],
                    button_component(
                        type="submit",
                        variant="outline",
                        class_="w-full",
                    )["Sign in"],
                ],
            ],
        ]
    ]


def _flash_messages(request: HttpRequest) -> Renderable:
    def message_variant(message: Message) -> AlertVariant:
        if message.level >= messages.ERROR:
            return "destructive"
        if message.level >= messages.WARNING:
            return "warning"
        if message.level >= messages.INFO:
            return "info"
        if message.level >= messages.SUCCESS:
            return "success"
        return "default"

    items = [
        alert(
            title=str(message),
            variant=message_variant(message),
            class_="border border-border/60 bg-background/80 backdrop-blur-sm",
        )
        for message in messages.get_messages(request)
    ]
    if not items:
        return div()
    return div(class_="space-y-3")[*items]


def csrf_input(request: HttpRequest) -> Renderable:
    return input_el(type="hidden", name="csrfmiddlewaretoken", value=get_token(request))


def label_with_radio(
    label_text: str,
    *,
    name: str = "repository_id",
    value: str,
    checked: bool = False,
    x_model: str | None = None,
) -> Renderable:
    attrs: dict[str, str] = {}
    if x_model:
        attrs["x-model"] = x_model
    return label(class_="inline-flex items-center gap-2 text-sm text-muted-foreground")[
        input_el(type="radio", name=name, value=value, checked=checked, **attrs),
        span[label_text],
    ]
