from rest_framework import serializers

from accounts.models import StaffRole
from common.validators import require_bilingual_pair

from .models import KitchenStation


class KitchenStationSerializer(serializers.ModelSerializer):
    """
    A station with who works there and how many dishes it makes. The counts
    are filled in by KitchenStationViewSet (one query for the whole list);
    `dish_count` counts each dish where it actually goes — its own station,
    else its category's — and `own_dish_count` only dishes set directly.
    """

    name_en = serializers.CharField(required=False, allow_blank=True, max_length=120)
    name_ar = serializers.CharField(required=False, allow_blank=True, max_length=120)
    chefs = serializers.SerializerMethodField()
    dish_count = serializers.SerializerMethodField()
    own_dish_count = serializers.SerializerMethodField()
    category_count = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = KitchenStation
        fields = [
            "id", "branch", "name_en", "name_ar", "sort_order", "is_active",
            "chefs", "dish_count", "own_dish_count", "category_count", "is_mine",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "branch", "created_at", "updated_at"]

    def get_chefs(self, station):
        return [
            {"id": chef.pk, "name": chef.name}
            for chef in station.chefs.all()
            if chef.is_active and chef.role == StaffRole.KITCHEN
        ]

    def _counts(self, station):
        return self.context.get("counts", {}).get(station.pk, {})

    def get_dish_count(self, station):
        return self._counts(station).get("dishes", 0)

    def get_own_dish_count(self, station):
        return self._counts(station).get("own_dishes", 0)

    def get_category_count(self, station):
        return self._counts(station).get("categories", 0)

    def get_is_mine(self, station):
        request = self.context.get("request")
        user_id = getattr(getattr(request, "user", None), "pk", None)
        return any(chef.pk == user_id for chef in station.chefs.all())

    def validate(self, attrs):
        require_bilingual_pair(attrs, self.instance, "name")
        branch = self.instance.branch if self.instance else self.context["request"].user.branch
        for field in ("name_en", "name_ar"):
            if field not in attrs:
                continue
            name = (attrs[field] or "").strip()
            attrs[field] = name
            if not name:
                continue
            duplicates = KitchenStation.objects.filter(branch=branch, **{f"{field}__iexact": name})
            if self.instance:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise serializers.ValidationError({field: "error.stationNameExists"})
        return attrs


def validate_station_branch(station, request):
    """A dish, category or chef may only use a station of its own branch."""
    if station is not None and request is not None and station.branch_id != request.user.branch_id:
        raise serializers.ValidationError("error.stationBranchMismatch")
    return station
