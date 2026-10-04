from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import LoginChoicesView, LogoutView, PinLoginView, RefreshView, StaffViewSet

router = DefaultRouter()
router.register("staff", StaffViewSet, basename="staff")

urlpatterns = [
    path("auth/login-choices/", LoginChoicesView.as_view(), name="login-choices"),
    path("auth/pin-login/", PinLoginView.as_view(), name="pin-login"),
    path("auth/refresh/", RefreshView.as_view(), name="token-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
] + router.urls
