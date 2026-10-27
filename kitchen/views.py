from collections import Counter

from django.db.models import Max, Prefetch
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import Staff, StaffRole
from accounts.permissions import IsOwnerOrManager
from common.mixins import BranchScopedQuerysetMixin

from .models import KitchenStation
from .routing import on_duty_chefs, unstaffed_station_ids
from .serializers import KitchenSettingsSerializer, KitchenStationSerializer


def station_counts(branch):
    """station id -> dishes routed there, dishes set on it directly, and
    categories defaulting to it — one query each for the whole branch."""
    from catalog.models import Category, Product

    counts = {}

    def bump(station_id, key):
        if station_id:
            counts.setdefault(station_id, Counter())[key] += 1

    for own, inherited in Product.objects.filter(branch=branch).values_list("station_id", "category__station_id"):
        bump(own or inherited, "dishes")
        bump(own, "own_dishes")
    for station_id in Category.objects.filter(branch=branch).values_list("station_id", flat=True):
        bump(station_id, "categories")
    return counts


class KitchenStationViewSet(BranchScopedQuerysetMixin, viewsets.ModelViewSet):
    """
    /api/kitchen-stations/ — read by any staff (the kitchen screen's station
    chips, the product and staff forms); written by owner/manager only.

    No DELETE: a station is switched off with is_active, so the order lines
    already routed to it keep their history.
    """

    serializer_class = KitchenStationSerializer
    http_method_names = ["get", "post", "patch", "put", "head", "options"]
    queryset = KitchenStation.objects.prefetch_related(
        Prefetch("chefs", queryset=Staff.objects.order_by("name")),
    )

    def get_permissions(self):
        if self.action in ("list", "retrieve", "kitchen_settings"):
            return [IsAuthenticated()]
        return [IsOwnerOrManager()]

    def get_queryset(self):
        qs = super().get_queryset()
        active = (self.request.query_params.get("active") or "").strip().lower()
        if active in ("true", "1", "yes"):
            qs = qs.filter(is_active=True)
        elif active in ("false", "0", "no"):
            qs = qs.filter(is_active=False)
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        user = getattr(self.request, "user", None)
        if user is not None and user.is_authenticated and getattr(user, "branch_id", None):
            context["counts"] = station_counts(user.branch)
            context["on_duty"] = set(on_duty_chefs(user.branch_id).values_list("pk", flat=True))
        return context

    def perform_create(self, serializer):
        branch = self.request.user.branch
        if "sort_order" in serializer.validated_data:
            serializer.save(branch=branch)
            return
        last = KitchenStation.objects.filter(branch=branch).aggregate(last=Max("sort_order"))["last"]
        serializer.save(branch=branch, sort_order=(last or 0) + 1)

    @action(detail=False, methods=["get"], url_path="summary", permission_classes=[IsOwnerOrManager])
    def summary(self, request):
        """What the setup checklist reports: dishes going to General, stations
        no chef works at, and kitchen staff with no station."""
        from catalog.models import Product

        branch = request.user.branch
        stations = KitchenStation.objects.filter(branch=branch)
        # Who works where at all — clocked in or not, that is set-up, not a shift.
        unstaffed = unstaffed_station_ids(branch, on_duty=False)
        products = Product.objects.filter(branch=branch)
        kitchen_staff = Staff.objects.filter(branch=branch, role=StaffRole.KITCHEN, is_active=True)
        return Response({
            "station_count": stations.count(),
            "active_station_count": stations.filter(is_active=True).count(),
            "dish_count": products.count(),
            "general_dish_count": products.filter(station__isnull=True, category__station__isnull=True).count(),
            "unstaffed_stations": [
                {"id": station.pk, "name_en": station.name_en, "name_ar": station.name_ar}
                for station in stations.filter(pk__in=unstaffed, is_active=True)
            ],
            "kitchen_staff_count": kitchen_staff.count(),
            "kitchen_staff_without_station": kitchen_staff.filter(stations__isnull=True).count(),
        })

    @action(detail=False, methods=["get", "patch"], url_path="settings")
    def kitchen_settings(self, request):
        """The branch's kitchen routing settings — read by any staff (the
        kitchen screen says whether clock-in counts), changed by owner/manager."""
        if request.method == "PATCH":
            if not IsOwnerOrManager().has_permission(request, self):
                self.permission_denied(request)
            serializer = KitchenSettingsSerializer(request.user.branch, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)
        return Response(KitchenSettingsSerializer(request.user.branch).data)
