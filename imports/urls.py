from django.urls import path

from .views import ImportAiDraftView, ImportGuideView, ImportMarkedView, ImportTemplateView, ImportView

urlpatterns = [
    path("imports/template/", ImportTemplateView.as_view(), name="import-template"),
    path("imports/guide/", ImportGuideView.as_view(), name="import-guide"),
    path("imports/ai-draft/", ImportAiDraftView.as_view(), name="import-ai-draft"),
    path("imports/marked/", ImportMarkedView.as_view(), name="import-marked"),
    path("imports/", ImportView.as_view(), name="import"),
]
