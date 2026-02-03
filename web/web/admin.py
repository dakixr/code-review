from django.contrib import admin

from .models import (
    AppSetting,
    ChatMessage,
    FeedbackSignal,
    GithubInstallation,
    GithubRepository,
    GithubUser,
    PullRequest,
    ReviewComment,
    ReviewRun,
    Rule,
    RuleSet,
    UserProfile,
)

admin.site.register(GithubUser)
admin.site.register(GithubInstallation)
admin.site.register(GithubRepository)
admin.site.register(RuleSet)
admin.site.register(Rule)
admin.site.register(PullRequest)
admin.site.register(ReviewRun)
admin.site.register(ReviewComment)
admin.site.register(FeedbackSignal)
admin.site.register(ChatMessage)
admin.site.register(AppSetting)
admin.site.register(UserProfile)
