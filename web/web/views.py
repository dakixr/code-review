from __future__ import annotations

from typing import Iterable, cast

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.templatetags.static import static
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from htpy import Node
from htpy import Renderable
from htpy import a
from htpy import body
from htpy import div
from htpy import h1
from htpy import h2
from htpy import head
from htpy import html
from htpy import input as input_el
from htpy import label
from htpy import li
from htpy import link
from htpy import main
from htpy import meta
from htpy import p
from htpy import script
from htpy import section
from htpy import span
from htpy import strong
from htpy import title
from htpy import ul

from components.ui.button import button_component
from components.ui.card import card
from components.ui.form import form_component
from components.ui.form import form_field
from components.ui.input import input_component
from components.ui.navbar import navbar
from components.ui.section import section_block
from components.ui.section import section_header
from components.ui.textarea import textarea_component

from .github import parse_webhook_body
from .github import verify_webhook_signature
from .models import FeedbackSignal
from .models import GithubInstallation
from .models import GithubRepository
from .models import PullRequest
from .models import Rule
from .models import RuleSet
from .models import UserProfile
from .services import deactivate_repository
from .services import queue_review
from .services import record_chat_message
from .services import upsert_installation
from .services import upsert_pull_request
from .services import upsert_repository


PAGE_SHELL_CLASS = "min-h-screen bg-background text-foreground"
CONTENT_CLASS = "max-w-7xl mx-auto px-4 sm:px-6 lg:px-8"


def render_htpy(content: Renderable) -> HttpResponse:
    return HttpResponse(str(content))


def layout(request: HttpRequest, content: Node, *, page_title: str) -> HttpResponse:
    flash = _flash_messages(request)
    top_nav = navbar(
        left=a(href="/", class_="text-lg font-semibold text-foreground")["CodeReview AI"],
        center=div(class_="flex items-center gap-6 text-sm text-muted-foreground")
        [
            a(href="/app", class_="hover:text-foreground transition-colors")["Dashboard"],
            a(href="/rules", class_="hover:text-foreground transition-colors")["Rules"],
            a(href="/account", class_="hover:text-foreground transition-colors")["Account"],
        ],
        right=div(class_="flex items-center gap-3")
        [
            a(
                href=f"https://github.com/apps/{settings.GITHUB_APP_NAME}/installations/new",
                class_="text-xs text-muted-foreground hover:text-foreground",
            )["Install GitHub App"],
        ],
    )

    return render_htpy(
        html(lang="en")
        [
            head[
                meta(charset="utf-8"),
                meta(name="viewport", content="width=device-width, initial-scale=1"),
                title[page_title],
                link(rel="stylesheet", href=static("css/output.css")),
                script(src="https://unpkg.com/htmx.org@1.9.12"),
            ],
            body(class_=PAGE_SHELL_CLASS)[
                top_nav,
                main(class_=f"py-10 {CONTENT_CLASS}")[flash, content],
            ],
        ]
    )


def home(request: HttpRequest) -> HttpResponse:
    install_url = f"https://github.com/apps/{settings.GITHUB_APP_NAME}/installations/new"
    hero_actions = div(class_="flex flex-wrap items-center gap-3")[
        a(href=install_url)[button_component(variant="primary")["Install GitHub App"]],
        a(href="/app")[button_component(variant="outline")["Go to dashboard"]],
    ]
    hero_section = section_block(tone="muted", class_="rounded-2xl border border-border/60")[
        div(class_="grid gap-6")[
            section_header(
                "Automated PR reviews that learn your taste",
                subtitle=(
                    "Connect your GitHub org, set global and repo-specific rules, and let the reviewer leave live comments."
                ),
                align="left",
            ),
            hero_actions,
        ]
    ]

    features = div(class_="grid gap-6 md:grid-cols-3")[
        card(title="Auto review", description="Runs on PR open or sync.")[
            p(class_="text-sm text-muted-foreground")[
                "A live status comment starts with 👁 and updates when the review is ready."
            ]
        ],
        card(title="Learns you", description="Records feedback.")[
            p(class_="text-sm text-muted-foreground")[
                "Capture likes, dislikes, and ignore signals to tighten future reviews."
            ]
        ],
        card(title="Configurable", description="Global + repo rules.")[
            p(class_="text-sm text-muted-foreground")[
                "Tune instruction sets per repo without hand-editing config files."
            ]
        ],
    ]

    how_section = section_block()[
        div(class_="grid gap-6")[section_header("How it works"), features]
    ]

    content = div(class_="space-y-12")[hero_section, how_section]

    return layout(request, content, page_title="CodeReview AI")


def dashboard(request: HttpRequest) -> HttpResponse:
    installations = GithubInstallation.objects.prefetch_related("repositories").all()
    cards: list[Renderable] = []

    for installation in installations:
        repos = [
            li(class_="text-sm text-muted-foreground")[repo.full_name]
            for repo in installation.repositories.all()
        ]
        cards.append(
            card(
                title=installation.account_login,
                description=f"Installation ID: {installation.installation_id}",
            )[
                ul(class_="space-y-1")[*repos]
                if repos
                else p(class_="text-sm text-muted-foreground")["No repositories installed yet."]
            ]
        )

    content = div(class_="space-y-6")[
        section_header("Dashboard", subtitle="Manage installs and repo coverage.", align="left"),
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
                    a(href="/account/logout")[button_component(variant="outline")["Sign out"]]
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
        div(class_="grid gap-6 md:grid-cols-2")[_signup_form(request), _login_form(request)],
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


def rules(request: HttpRequest) -> HttpResponse:
    rule_sets = RuleSet.objects.prefetch_related("rules", "repository").all()
    repositories = GithubRepository.objects.filter(is_active=True).all()

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
    name = request.POST.get("name", "New Rules")
    scope = request.POST.get("scope", RuleSet.SCOPE_GLOBAL)
    repo_id = request.POST.get("repository_id")
    instructions = request.POST.get("instructions", "")
    repository_id = int(repo_id) if repo_id else None
    RuleSet.objects.create(
        name=name,
        scope=scope,
        repository_id=repository_id,
        instructions=instructions,
    )
    return redirect("/rules")


def add_rule(request: HttpRequest, rule_set_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("/rules")
    title = request.POST.get("title", "New rule")
    description = request.POST.get("description", "")
    severity = request.POST.get("severity", "info")
    Rule.objects.create(
        rule_set_id=rule_set_id,
        title=title,
        description=description,
        severity=severity,
    )
    return redirect("/rules")


@csrf_exempt
def github_webhook(request: HttpRequest) -> HttpResponse:
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(request.body, signature, settings.GITHUB_WEBHOOK_SECRET):
        return JsonResponse({"error": "invalid signature"}, status=400)

    event = request.headers.get("X-GitHub-Event", "")
    payload = parse_webhook_body(request.body)

    if event == "installation":
        installation = upsert_installation(payload["installation"])
        return JsonResponse({"status": "ok", "installation": installation.installation_id})

    if event == "installation_repositories":
        installation = upsert_installation(payload["installation"])
        for repo in payload.get("repositories_added", []):
            upsert_repository(installation, repo)
        for repo in payload.get("repositories_removed", []):
            deactivate_repository(installation, repo)
        return JsonResponse({"status": "ok"})

    if event == "pull_request":
        action = payload.get("action")
        if action in {"opened", "reopened", "synchronize"}:
            installation = upsert_installation(payload["installation"])
            repo = upsert_repository(installation, payload["repository"])
            pull_request = upsert_pull_request(repo, payload["pull_request"])
            head_sha = payload["pull_request"]["head"]["sha"]
            queue_review(pull_request, head_sha)
        return JsonResponse({"status": "ok"})

    if event == "issue_comment":
        if "pull_request" in payload.get("issue", {}):
            pr_number = payload["issue"]["number"]
            repo_full_name = payload["repository"]["full_name"]
            pull_request = PullRequest.objects.filter(
                repository__full_name=repo_full_name,
                pr_number=pr_number,
            ).first()
            if pull_request:
                body_text = payload["comment"]["body"]
                record_chat_message(pull_request, payload["comment"])
                _try_record_feedback(pull_request, body_text)
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
    latest_comment = pull_request.review_runs.order_by("-id").prefetch_related("comments").first()
    if not latest_comment or not latest_comment.comments.exists():
        return
    review_comment = latest_comment.comments.latest("id")
    FeedbackSignal.objects.create(review_comment=review_comment, signal=signal)


def _rule_set_form(request: HttpRequest, repositories: Iterable[GithubRepository]) -> Renderable:
    repo_options = [label_with_radio(repo.full_name, value=str(repo.id)) for repo in repositories]
    repo_block: Renderable = (
        div(class_="grid gap-2")[*repo_options]
        if repo_options
        else p(class_="text-sm text-muted-foreground")["Install a repo to enable repo rules."]
    )

    return card(title="Create Rule Set", description="Define global or repo-specific rules.")[
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
                        label_with_radio("Global", name="scope", value="global", checked=True),
                        label_with_radio("Repository", name="scope", value="repo"),
                    ],
                ]
            ],
            form_field[
                div(class_="grid gap-2")[
                    span(class_="text-sm font-medium")["Repository (optional)"],
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
            button_component(type="submit")["Create"],
        ]
    ]


def _rule_sets_block(request: HttpRequest, rule_sets: Iterable[RuleSet]) -> list[Renderable]:
    blocks: list[Renderable] = []
    for rule_set in rule_sets:
        rules = [
            li[
                strong[rule.title],
                span(class_="text-muted-foreground")[f" — {escape(rule.description)}"],
            ]
            for rule in rule_set.rules.all()
        ]

        blocks.append(
            card(
                title=rule_set.name,
                description=f"Scope: {rule_set.scope}",
            )[
                p(class_="text-sm text-muted-foreground")[
                    rule_set.instructions or "No instructions yet."
                ],
                ul(class_="mt-4 space-y-2")[*rules]
                if rules
                else p(class_="text-sm text-muted-foreground")["No rules added yet."],
                form_component(action=f"/rules/{rule_set.id}/add", method="post", class_="mt-4")[
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
                    button_component(type="submit", variant="outline")["Add Rule"],
                ],
            ]
        )
    return blocks


def _signup_form(request: HttpRequest) -> Renderable:
    return card(title="Create account", description="Sign up to manage installs.")[
        form_component(action="/account/signup", method="post")[
            csrf_input(request),
            form_field[
                input_component(name="username", label_text="Username", placeholder="yourname")
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
            button_component(type="submit")["Sign up"],
        ]
    ]


def _login_form(request: HttpRequest) -> Renderable:
    return card(title="Sign in", description="Access your existing account.")[
        form_component(action="/account/login", method="post")[
            csrf_input(request),
            form_field[input_component(name="username", label_text="Username")],
            form_field[input_component(name="password", label_text="Password", type="password")],
            button_component(type="submit", variant="outline")["Sign in"],
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
) -> Renderable:
    return label(class_="inline-flex items-center gap-2 text-sm text-muted-foreground")[
        input_el(type="radio", name=name, value=value, checked=checked),
        span[label_text],
    ]
