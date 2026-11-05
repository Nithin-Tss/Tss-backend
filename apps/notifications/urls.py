from django.urls import path

from .views import NotificationSettingsView, NotificationTestView

urlpatterns = [
    path("settings/", NotificationSettingsView.as_view(), name="notification-settings"),
    path("settings/test/", NotificationTestView.as_view(), name="notification-settings-test"),
]
