from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import TableBySessionView, TableViewSet

router = DefaultRouter()
router.register("tables", TableViewSet, basename="table")

urlpatterns = [
    path("tables/by-session/<str:token>/", TableBySessionView.as_view(), name="table-by-session"),
] + router.urls
