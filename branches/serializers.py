from decimal import Decimal

from rest_framework import serializers

from common.validators import require_bilingual_pair

from .models import Branch


class BranchSerializer(serializers.ModelSerializer):
    # Neither language is unconditionally required — see require_bilingual_pair
    # in validate(), which only requires whichever one the admin UI has active.
    name_en = serializers.CharField(required=False, allow_blank=True)
    name_ar = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Branch
        fields = ["id", "name_en", "name_ar", "address", "timezone", "currency", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        require_bilingual_pair(attrs, self.instance, "name")
        return attrs


class BranchLocationSerializer(serializers.ModelSerializer):
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, min_value=Decimal("-90"), max_value=Decimal("90"))
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, min_value=Decimal("-180"), max_value=Decimal("180"))
    geofence_radius_m = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=Decimal("0.01"), max_value=Decimal("10000"), required=False,
    )
    attendance_radius_m = serializers.DecimalField(
        max_digits=8, decimal_places=2, min_value=Decimal("1"), max_value=Decimal("10000"), required=False,
    )

    class Meta:
        model = Branch
        fields = ["latitude", "longitude", "geofence_radius_m", "attendance_radius_m"]

    def validate(self, attrs):
        latitude = attrs.get("latitude", self.instance.latitude if self.instance else None)
        longitude = attrs.get("longitude", self.instance.longitude if self.instance else None)
        if (latitude is None) != (longitude is None):
            raise serializers.ValidationError({"location": "error.locationCoordinatesPairRequired"})
        return attrs
