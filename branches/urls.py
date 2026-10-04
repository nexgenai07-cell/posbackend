from django.urls import path

from .views import ActiveBranchView, BranchLocationView, BranchUpdateView

urlpatterns = [
    path("branches/active/", ActiveBranchView.as_view(), name="branch-active"),
    path("branches/active/location/", BranchLocationView.as_view(), name="branch-location"),
    path("branches/<int:pk>/", BranchUpdateView.as_view(), name="branch-update"),
]
