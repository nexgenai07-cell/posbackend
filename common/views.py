import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager

from . import cloudinary

logger = logging.getLogger("common.cloudinary")


class UploadSignatureView(APIView):
    """
    POST /api/uploads/signature/

    Hands the browser a short-lived, scoped Cloudinary upload signature so it
    can upload a product image or a logo DIRECTLY to Cloudinary. The file never
    passes through this server and CLOUDINARY_API_SECRET never leaves it — see
    common/cloudinary.py for why that shape was chosen.

    Owner/manager only: an upload signature is a write capability against the
    restaurant's media account, so it follows the same gate as every other
    administrative configuration action (requirement §22).
    """

    permission_classes = [IsOwnerOrManager]

    def post(self, request):
        if not cloudinary.is_configured():
            # A clear, actionable code rather than a 500 — the usual cause is
            # simply that the CLOUDINARY_* env vars have not been filled in.
            return Response(
                {"error": "error.uploadsNotConfigured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        try:
            payload = cloudinary.build_signature()
        except cloudinary.CloudinaryNotConfigured:
            return Response(
                {"error": "error.uploadsNotConfigured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        logger.info("Issued a Cloudinary upload signature to staff %s", request.user.pk)
        return Response(payload)
