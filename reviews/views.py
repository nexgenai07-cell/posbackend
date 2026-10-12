from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager
from branches.models import Branch
from common.filters import choice_filter, text_filter
from common.mixins import BranchScopedQuerysetMixin
from realtime.publisher import publish_event
from reports.filters import branch_datetime_bounds
from tables.models import Table
from tables.services import parse_qr_payload

from .models import Review, ReviewSource, ReviewStatus
from .serializers import GuestReviewSerializer, PublicReviewSerializer, ReviewSerializer
from .services import guest_bill, review_eligibility

PUBLIC_REVIEW_LIMIT = 60


def _table_for_token(raw_token):
    token = parse_qr_payload(raw_token)
    if not token:
        return None
    return Table.objects.filter(session_token=token).select_related("branch").first()


# -- guest (AllowAny, table session token + phone) ---------------------------


class GuestReviewEligibilityView(APIView):
    """
    GET /api/reviews/qr/eligibility/?session_token=&phone=&bill_id=

    Whether the pay screen should offer "Leave a review" for this bill:
    {"can_review": bool, "reason": error code or null}. Never says anything
    about a bill that isn't this phone's.
    """

    permission_classes = [AllowAny]
    throttle_scope = "table-lookup"

    def get(self, request):
        params = request.query_params
        table = _table_for_token(params.get("session_token", ""))
        if table is None:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)
        bill = guest_bill(table, params.get("phone", ""), params.get("bill_id"))
        can_review, reason = review_eligibility(bill)
        return Response({"can_review": can_review, "reason": reason})


class GuestReviewCreateView(APIView):
    """
    POST /api/reviews/qr/  {session_token, phone, bill_id, name, rating, comment?}

    One review per settled, fully served bill. Always saved as `pending` —
    an owner/manager approves it before the website shows it.
    """

    permission_classes = [AllowAny]
    throttle_scope = "review"

    def post(self, request):
        serializer = GuestReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        table = _table_for_token(data["session_token"])
        if table is None:
            return Response({"error": "error.notFound"}, status=status.HTTP_404_NOT_FOUND)
        bill = guest_bill(table, data["phone"], data["bill_id"])
        can_review, reason = review_eligibility(bill)
        if not can_review:
            return Response({"error": reason}, status=status.HTTP_409_CONFLICT)

        review = Review.objects.create(
            branch=bill.branch,
            bill=bill,
            customer=bill.customer,
            name=data["name"],
            rating=data["rating"],
            comment=data["comment"],
            source=ReviewSource.GUEST,
            status=ReviewStatus.PENDING,
        )
        publish_event(review.branch_id, "review:created", review.pk)
        return Response({"id": review.pk, "status": review.status}, status=status.HTTP_201_CREATED)


# -- website (AllowAny, approved only) ---------------------------------------


class PublicReviewListView(APIView):
    """GET /api/reviews/public/?branch=  Approved reviews, newest first."""

    permission_classes = [AllowAny]
    throttle_scope = "menu"

    def get(self, request):
        branch_id = request.query_params.get("branch")
        if branch_id:
            branch = get_object_or_404(Branch, pk=branch_id)
        else:
            branch = Branch.objects.order_by("id").first()
            if branch is None:
                return Response({"error": "error.noBranch"}, status=status.HTTP_404_NOT_FOUND)
        reviews = Review.objects.filter(branch=branch, status=ReviewStatus.APPROVED)[:PUBLIC_REVIEW_LIMIT]
        return Response({"reviews": PublicReviewSerializer(reviews, many=True).data})


# -- owner / manager ---------------------------------------------------------


class ReviewViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    Moderation and admin-added reviews.

    Filters: ?status=pending|approved|rejected, ?source=guest|admin, ?rating=1-5,
    ?search= (name/comment), ?from=&?to= (YYYY-MM-DD, branch time, on posted_at).

    Guest reviews can be approved, rejected or deleted, but their wording is
    never edited — the website would otherwise show words the guest didn't
    write under their name. Admin-added reviews are editable.
    """

    queryset = Review.objects.select_related("bill__table", "customer", "moderated_by", "created_by")
    serializer_class = ReviewSerializer
    permission_classes = [IsOwnerOrManager]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        qs = choice_filter(qs, params, "status", allowed=ReviewStatus.values)
        qs = choice_filter(qs, params, "source", allowed=ReviewSource.values)
        qs = choice_filter(qs, params, "rating", allowed=["1", "2", "3", "4", "5"])
        qs = text_filter(qs, params, "search", ["name", "comment"])
        from_date, to_date = parse_date(params.get("from") or ""), parse_date(params.get("to") or "")
        if from_date or to_date:
            branch = self.request.user.branch
            start, end = branch_datetime_bounds(branch, from_date or to_date, to_date or from_date)
            if from_date:
                qs = qs.filter(posted_at__gte=start)
            if to_date:
                qs = qs.filter(posted_at__lt=end)
        return qs

    def perform_create(self, serializer):
        now = timezone.now()
        review = serializer.save(
            branch=self.request.user.branch,
            source=ReviewSource.ADMIN,
            status=ReviewStatus.APPROVED,
            created_by=self.request.user,
            moderated_by=self.request.user,
            moderated_at=now,
            posted_at=serializer.validated_data.get("posted_at") or now,
        )
        publish_event(review.branch_id, "review:updated", review.pk)

    def update(self, request, *args, **kwargs):
        if self.get_object().source != ReviewSource.ADMIN:
            return Response({"error": "error.reviewNotEditable"}, status=status.HTTP_409_CONFLICT)
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        review = serializer.save()
        publish_event(review.branch_id, "review:updated", review.pk)

    def perform_destroy(self, instance):
        instance.delete()
        publish_event(instance.branch_id, "review:updated", instance.pk)

    def _moderate(self, request, new_status):
        review = self.get_object()
        review.status = new_status
        review.moderated_by = request.user
        review.moderated_at = timezone.now()
        review.save(update_fields=["status", "moderated_by", "moderated_at", "updated_at"])
        publish_event(review.branch_id, "review:updated", review.pk)
        return Response(self.get_serializer(review).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._moderate(request, ReviewStatus.APPROVED)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._moderate(request, ReviewStatus.REJECTED)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts per status for the sidebar badge and the page tabs."""
        rows = (
            Review.objects.filter(branch=request.user.branch)
            .values("status").annotate(count=Count("id"))
        )
        counts = {value: 0 for value in ReviewStatus.values}
        counts.update({row["status"]: row["count"] for row in rows})
        counts["all"] = sum(counts[value] for value in ReviewStatus.values)
        return Response(counts)
