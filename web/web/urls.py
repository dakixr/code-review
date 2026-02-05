from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("app", views.dashboard, name="dashboard"),
    path("rules", views.rules, name="rules"),
    path("rules/create", views.create_rule_set, name="create_rule_set"),
    path("rules/<int:rule_set_id>/add", views.add_rule, name="add_rule"),
    path(
        "rules/<int:rule_set_id>/delete", views.delete_rule_set, name="delete_rule_set"
    ),
    path(
        "rules/<int:rule_set_id>/rules/<int:rule_id>/delete",
        views.delete_rule,
        name="delete_rule",
    ),
    path("github/webhook", views.github_webhook, name="github_webhook"),
    path(
        "github/webhook/<uuid:app_uuid>",
        views.github_webhook_app,
        name="github_webhook_app",
    ),
    path("github/apps/create", views.create_github_app, name="create_github_app"),
    path(
        "github/apps/<uuid:app_uuid>/setup",
        views.github_app_setup,
        name="github_app_setup",
    ),
    path("github/apps/redirect", views.github_app_redirect, name="github_app_redirect"),
    path("github/apps/install", views.github_app_install, name="github_app_install"),
    path("account", views.account, name="account"),
    path("account/api-keys", views.save_api_keys, name="save_api_keys"),
    path("feedback", views.feedback, name="feedback"),
    path(
        "feedback/signals/<int:signal_id>/update",
        views.update_feedback_signal,
        name="update_feedback_signal",
    ),
    path(
        "feedback/signals/<int:signal_id>/delete",
        views.delete_feedback_signal,
        name="delete_feedback_signal",
    ),
    path(
        "feedback/mentions/<int:message_id>/delete",
        views.delete_mention_message,
        name="delete_mention_message",
    ),
    path("account/signup", views.signup, name="signup"),
    path("account/login", views.login, name="login"),
    path("account/logout", views.logout, name="logout"),
]
