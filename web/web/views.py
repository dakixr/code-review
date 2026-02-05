from __future__ import annotations

import json
import logging
import secrets
from typing import Iterable, cast
from uuid import UUID

from components.ui.button import button_component
from components.ui.card import card
from components.ui.form import form_component, form_field
from components.ui.input import input_component
from components.ui.navbar import navbar
from components.ui.section import section_block, section_header
from components.ui.textarea import textarea_component
from components.ui.theme_toggle import theme_toggle
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.models import User
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.templatetags.static import static
from django.utils.html import escape
from django.utils import timezone
from django.utils.timezone import localtime
from django.views.decorators.csrf import csrf_exempt
from htpy import (
    Node,
    Renderable,
    a,
    body,
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

PAGE_SHELL_CLASS = "min-h-screen bg-background text-foreground"
CONTENT_CLASS = "max-w-7xl mx-auto px-4 sm:px-6 lg:px-8"

logger = logging.getLogger(__name__)


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
    top_nav = navbar(
        left=a(href="/", class_="text-lg font-semibold text-foreground")[
            "CodeReview AI"
        ],
        center=div(class_="flex items-center gap-6 text-sm text-muted-foreground")[
            a(href="/app", class_="hover:text-foreground transition-colors")[
                "Dashboard"
            ],
            a(href="/rules", class_="hover:text-foreground transition-colors")["Rules"],
            a(href="/feedback", class_="hover:text-foreground transition-colors")[
                "Feedback"
            ],
            a(href="/account", class_="hover:text-foreground transition-colors")[
                "Account"
            ],
        ],
        right=div(class_="flex items-center gap-3")[
            theme_toggle(),
            a(
                href=github_app_install_url(request),
                class_="text-xs text-muted-foreground hover:text-foreground",
            )["Install GitHub App"],
        ],
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
            ],
            body(class_=PAGE_SHELL_CLASS)[
                top_nav,
                main(class_=f"py-10 {CONTENT_CLASS}")[flash, content],
            ],
        ]
    )


def home(request: HttpRequest) -> HttpResponse:
    install_url = github_app_install_url(request)
    hero_actions = div(class_="flex flex-wrap items-center gap-3")[
        a(href=install_url)[button_component(variant="primary")["Install GitHub App"]],
        a(href="/app")[button_component(variant="outline")["Go to dashboard"]],
    ]

    hero_badges = div(class_="flex flex-wrap gap-2")[
        span(
            class_="rounded-full border border-border/70 px-3 py-1 text-xs text-muted-foreground"
        )["GitHub App"],
        span(
            class_="rounded-full border border-border/70 px-3 py-1 text-xs text-muted-foreground"
        )["Multi-user"],
        span(
            class_="rounded-full border border-border/70 px-3 py-1 text-xs text-muted-foreground"
        )["HTMX + htpy"],
        span(
            class_="rounded-full border border-border/70 px-3 py-1 text-xs text-muted-foreground"
        )["Learns your taste"],
    ]

    stats = div(class_="grid gap-4 sm:grid-cols-3")[
        card(title="<10 sec", description="Time to first 👁 comment")[
            p(class_="text-sm text-muted-foreground")["Immediate feedback on every PR."]
        ],
        card(title="Global + repo", description="Rule sets")[
            p(class_="text-sm text-muted-foreground")["Tune once or per repo."]
        ],
        card(title="Feedback loop", description="Likes, ignore, dislike")[
            p(class_="text-sm text-muted-foreground")["Signals train the reviewer."]
        ],
    ]

    hero = section_block(tone="muted", class_="rounded-2xl border border-border/60")[
        div(class_="grid gap-8 lg:grid-cols-[1.2fr_0.8fr]")[
            div(class_="grid gap-4")[
                hero_badges,
                h1(class_="text-4xl sm:text-5xl font-semibold tracking-tight")[
                    "Automated PR reviews that learn your taste"
                ],
                p(class_="text-lg text-muted-foreground")[
                    "Create your own GitHub App, install it on repos, and let the reviewer comment directly in PRs. Add per-user model API keys and tune rule sets globally or per repo."
                ],
                hero_actions,
                div(class_="grid gap-2 text-sm text-muted-foreground")[
                    strong(class_="text-foreground")["Works entirely from GitHub."],
                    span[
                        "The UI is a control plane for installs, rules, and ops — the conversation happens in PR comments."
                    ],
                ],
            ],
            card(title="Live review preview", description="GitHub comment thread")[
                div(class_="grid gap-3 text-sm text-muted-foreground")[
                    div(
                        class_="rounded-lg border border-border/60 bg-background/70 p-3"
                    )["👁 Reviewing this PR now. I'll post details shortly."],
                    div(
                        class_="rounded-lg border border-border/60 bg-background/70 p-3"
                    )[
                        '✅ Found 2 improvements. 1) Add a null guard on "user.email". 2) Consider caching the lint results.'
                    ],
                    div(
                        class_="rounded-lg border border-border/60 bg-background/70 p-3"
                    )["/ai like"],
                ],
            ],
        ],
    ]

    setup_flow = div(class_="grid gap-6 md:grid-cols-3")[
        card(title="1. Create an account", description="Control plane access")[
            p(class_="text-sm text-muted-foreground")[
                "Each user brings their own GitHub App and AI provider keys."
            ]
        ],
        card(
            title="2. Create your GitHub App",
            description="Manifest flow (Coolify-style)",
        )[
            p(class_="text-sm text-muted-foreground")[
                "We redirect you to GitHub with a pre-filled App Manifest and store the app credentials on return."
            ]
        ],
        card(title="3. Install the app", description="Org or repo install")[
            p(class_="text-sm text-muted-foreground")[
                "Choose which repos to grant access to so we can receive webhooks and fetch PR diffs."
            ]
        ],
    ]

    runtime_flow = div(class_="grid gap-6 md:grid-cols-3")[
        card(title="4. Webhook ingestion", description="PR + comment events")[
            p(class_="text-sm text-muted-foreground")[
                "GitHub calls a per-app webhook URL. We verify the signature using the app’s webhook secret."
            ]
        ],
        card(title="5. Background review", description="Celery worker + OpenCode")[
            p(class_="text-sm text-muted-foreground")[
                "The worker fetches the PR diff via the GitHub API and runs OpenCode with your per-user model key."
            ]
        ],
        card(title="6. GitHub-native loop", description="Comments + feedback")[
            p(class_="text-sm text-muted-foreground")[
                "A placeholder 👁 comment is posted immediately, then edited with the full review. Use /ai like, /ai dislike, /ai ignore."
            ]
        ],
    ]

    features = div(class_="grid gap-6 md:grid-cols-3")[
        card(title="Auto review", description="Runs on PR open or sync.")[
            p(class_="text-sm text-muted-foreground")[
                "A live status comment starts with 👁 and updates when ready."
            ]
        ],
        card(title="Learns you", description="Records feedback.")[
            p(class_="text-sm text-muted-foreground")[
                "Capture likes, dislikes, and ignore signals to tighten reviews."
            ]
        ],
        card(title="Configurable", description="Global + repo rules.")[
            p(class_="text-sm text-muted-foreground")[
                "Tune instruction sets per repo without editing config files."
            ]
        ],
    ]

    architecture = div(class_="grid gap-6 md:grid-cols-2")[
        card(title="Control plane (this UI)", description="Django + HTMX + htpy")[
            ul(class_="space-y-1 text-sm text-muted-foreground")[
                li["Manage your GitHub App + installation status."],
                li["Create global and per-repo rule sets."],
                li["Store per-user AI provider API keys (no env vars)."],
            ]
        ],
        card(title="Data plane", description="Webhook → queue → review")[
            ul(class_="space-y-1 text-sm text-muted-foreground")[
                li["Webhook endpoint validates per-app signatures."],
                li["Celery job fetches PR diff and generates a review."],
                li["Bot posts/edits GitHub issue comments with results."],
            ]
        ],
        card(title="Security model", description="Per-user isolation")[
            ul(class_="space-y-1 text-sm text-muted-foreground")[
                li[
                    "Each user has their own GitHub App credentials and webhook secret."
                ],
                li["Each user stores their own model API keys in the database."],
                li["The worker injects keys at runtime when calling OpenCode."],
            ]
        ],
        card(title="Local dev note", description="GitHub requires public webhooks")[
            p(class_="text-sm text-muted-foreground")[
                "GitHub App webhooks must be reachable from the public Internet. For local dev, use a tunnel (e.g. Cloudflare Tunnel/ngrok) to expose this app."
            ]
        ],
    ]

    cta = section_block(tone="bordered", class_="rounded-2xl border border-border/60")[
        div(
            class_="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between"
        )[
            div(class_="grid gap-2")[
                h2(class_="text-2xl font-semibold")["Ready to ship calmer PR reviews?"],
                p(class_="text-sm text-muted-foreground")[
                    "Install the GitHub App and set the first rule set in minutes."
                ],
            ],
            a(href=install_url)[
                button_component(variant="primary")["Install GitHub App"]
            ],
        ]
    ]

    content = div(class_="space-y-12")[
        hero,
        stats,
        section_block()[
            div(class_="grid gap-6")[
                section_header(
                    "How it works", subtitle="End-to-end flow", align="left"
                ),
                setup_flow,
                runtime_flow,
            ]
        ],
        section_block()[
            div(class_="grid gap-6")[section_header("What you get"), features]
        ],
        section_block()[
            div(class_="grid gap-6")[
                section_header(
                    "Architecture",
                    subtitle="Control plane vs data plane (MVP)",
                    align="left",
                ),
                architecture,
            ]
        ],
        cta,
    ]

    return layout(request, content, page_title="CodeReview AI")


def dashboard(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        content = div(class_="space-y-6")[
            section_header(
                "Dashboard",
                subtitle="Sign in to manage installs and rules.",
                align="left",
            ),
            card(
                title="Sign in required",
                description="Create an account to connect GitHub.",
            )[
                a(href="/account")[
                    button_component(variant="primary")["Go to account"]
                ],
            ],
        ]
        return layout(request, content, page_title="Dashboard")

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
            repos = [
                li(class_="text-sm text-muted-foreground")[repo.full_name]
                for repo in installation.repositories.filter(is_active=True).all()
            ]
            installation_list.append(
                card(
                    title=installation.account_login or "Installation",
                    description=f"Installation ID: {installation.installation_id}",
                )[
                    ul(class_="space-y-1")[*repos]
                    if repos
                    else p(class_="text-sm text-muted-foreground")[
                        "No repositories installed yet."
                    ]
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

    content = div(class_="space-y-6")[
        section_header(
            "Dashboard", subtitle="Manage installs and repo coverage.", align="left"
        ),
        div(class_="grid gap-6 md:grid-cols-2")[*cards],
    ]

    return layout(request, content, page_title="Dashboard")


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
        content = div(class_="space-y-6")[
            section_header("Account", subtitle="Manage your profile.", align="left"),
            card(title=f"Signed in as {user.username}")[
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
                    button_component(type="submit")["Save"],
                ],
                div(class_="pt-2")[
                    a(href="/account/logout")[
                        button_component(variant="outline")["Sign out"]
                    ]
                ],
            ],
            card(
                title="GitHub App", description="Create your own GitHub App (per user)."
            )[
                p(class_="text-sm text-muted-foreground")[
                    "This uses GitHub's App Manifest flow to create an app on your GitHub account, then stores the credentials server-side."
                ],
                div(class_="pt-3 flex flex-wrap items-center gap-3")[
                    form_component(action="/github/apps/create", method="post")[
                        csrf_input(request),
                        button_component(type="submit", variant="primary")[
                            "Create GitHub App"
                        ],
                    ],
                    a(
                        href=f"/github/apps/{github_app.uuid}/setup"
                        if github_app
                        else "/app",
                    )[button_component(variant="outline")["Open setup"]],
                    a(
                        href=f"https://github.com/apps/{github_app.slug}/installations/new"
                        if github_app and github_app.slug
                        else "/app",
                    )[button_component(variant="outline")["Install / Manage repos"]],
                ],
                div(class_="pt-3")[
                    p(class_="text-xs text-muted-foreground")[
                        f"Status: {github_app.status}"
                        if github_app
                        else "No GitHub App yet."
                    ]
                ],
            ],
            card(title="API Keys", description="Per-user keys (not env vars).")[
                form_component(action="/account/api-keys", method="post")[
                    csrf_input(request),
                    form_field[
                        input_component(
                            name="zai_api_key",
                            label_text="ZAI API key (for zai/glm-4.7)",
                            placeholder="zai_...",
                            value=masked_zai,
                        )
                    ],
                    button_component(type="submit")["Save"],
                ],
                p(class_="pt-2 text-xs text-muted-foreground")[
                    "Keys are stored per user and will be injected into the review worker when running models."
                ],
            ],
        ]
        return layout(request, content, page_title="Account")

    content = div(class_="space-y-8")[
        section_header(
            "Account",
            subtitle="Create an account to manage installs and rules.",
            align="left",
        ),
        div(class_="grid gap-6 md:grid-cols-2")[
            _signup_form(request), _login_form(request)
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
        section_header(
            "Review Rules",
            subtitle="Global rules apply everywhere. Repo rules override or extend them.",
            align="left",
        ),
        div(class_="grid gap-6 lg:grid-cols-[1.2fr_2fr]")[
            _rule_set_form(request, repositories),
            *_rule_sets_block(request, rule_sets),
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

    content = div(class_="space-y-8")[
        section_header(
            "Feedback",
            subtitle="Inspect and clean up what the reviewer has learned (per repo).",
            align="left",
        ),
        card(title="Filter", description="Choose a repository to scope results.")[
            form_component(action="/feedback", method="get", class_="max-w-xl")[
                form_field[repo_select],
                input_el(type="hidden", name="limit", value=str(limit)),
                button_component(type="submit", variant="outline")["Apply"],
            ]
        ],
        card(
            title="Feedback signals",
            description="Signals recorded from /ai like, /ai dislike, /ai ignore.",
        )[
            ul(class_="space-y-3")[*feedback_items]
            if feedback_items
            else p(class_="text-sm text-muted-foreground")[
                "No feedback signals recorded yet."
            ],
            a(
                href=_feedback_more_link(repo_id_raw, limit),
                class_="text-sm text-muted-foreground hover:text-foreground",
            )["Load more"]
            if len(recent_feedback) == limit
            else span(),
        ],
        card(
            title="Mentions",
            description="PR comments where @codereview was mentioned.",
        )[
            ul(class_="space-y-3")[*mention_items]
            if mention_items
            else p(class_="text-sm text-muted-foreground")[
                "No @codereview mentions recorded yet."
            ],
            a(
                href=_feedback_more_link(repo_id_raw, limit),
                class_="text-sm text-muted-foreground hover:text-foreground",
            )["Load more"]
            if len(recent_mentions) == limit
            else span(),
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
    return card(title="Create account", description="Sign up to manage installs.")[
        form_component(action="/account/signup", method="post")[
            csrf_input(request),
            form_field[
                input_component(
                    name="username", label_text="Username", placeholder="yourname"
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
                input_component(name="password", label_text="Password", type="password")
            ],
            button_component(type="submit")[["Sign up"]],
        ]
    ]


def _login_form(request: HttpRequest) -> Renderable:
    return card(title="Sign in", description="Access your existing account.")[
        form_component(action="/account/login", method="post")[
            csrf_input(request),
            form_field[input_component(name="username", label_text="Username")],
            form_field[
                input_component(name="password", label_text="Password", type="password")
            ],
            button_component(type="submit", variant="outline")[["Sign in"]],
        ]
    ]


def _flash_messages(request: HttpRequest) -> Renderable:
    items = [
        card(description=str(message), class_="border border-destructive/30")[
            p(class_="text-sm text-muted-foreground")[str(message)]
        ]
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
