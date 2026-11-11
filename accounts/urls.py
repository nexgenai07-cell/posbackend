from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AttendanceSettingsView,
    ChangePasswordView,
    LoginChoicesView,
    LoginView,
    LogoutView,
    MeView,
    RefreshView,
    ShiftTemplateViewSet,
    StaffViewSet,
)

router = DefaultRouter()
router.register("staff", StaffViewSet, basename="staff")
router.register("shift-templates", ShiftTemplateViewSet, basename="shift-template")

urlpatterns = [
    path("auth/login-choices/", LoginChoicesView.as_view(), name="login-choices"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/refresh/", RefreshView.as_view(), name="token-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("branches/active/attendance/", AttendanceSettingsView.as_view(), name="attendance-settings"),
] + router.urls
