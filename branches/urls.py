from django.urls import path

from .views import ActiveBranchView, BranchLocationView, BranchUpdateView, PublicBranchView

urlpatterns = [
    # Public branding for customer-facing clients — must stay above the
    # authenticated routes so it is never shadowed by them.
    path("branches/public/", PublicBranchView.as_view(), name="branch-public"),
    path("branches/active/", ActiveBranchView.as_view(), name="branch-active"),
    path("branches/active/location/", BranchLocationView.as_view(), name="branch-location"),
    path("branches/<int:pk>/", BranchUpdateView.as_view(), name="branch-update"),
]
