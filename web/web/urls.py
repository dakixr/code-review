from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("app", views.dashboard, name="dashboard"),
    path("rules", views.rules, name="rules"),
    path("rules/create", views.create_rule_set, name="create_rule_set"),
    path("rules/<int:rule_set_id>/add", views.add_rule, name="add_rule"),
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
    path("account/signup", views.signup, name="signup"),
    path("account/login", views.login, name="login"),
    path("account/logout", views.logout, name="logout"),
]
