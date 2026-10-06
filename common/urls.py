from django.urls import path

from .views import UploadSignatureView

urlpatterns = [
    path("uploads/signature/", UploadSignatureView.as_view(), name="upload-signature"),
]
