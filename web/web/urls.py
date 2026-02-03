from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("app", views.dashboard, name="dashboard"),
    path("rules", views.rules, name="rules"),
    path("rules/create", views.create_rule_set, name="create_rule_set"),
    path("rules/<int:rule_set_id>/add", views.add_rule, name="add_rule"),
    path("github/webhook", views.github_webhook, name="github_webhook"),
    path("account", views.account, name="account"),
    path("account/signup", views.signup, name="signup"),
    path("account/login", views.login, name="login"),
    path("account/logout", views.logout, name="logout"),
]
