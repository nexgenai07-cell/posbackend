from rest_framework import serializers

from .models import Review


class ReviewSerializer(serializers.ModelSerializer):
    """Staff view of a review — includes where a guest review came from."""

    table_label = serializers.SerializerMethodField()
    bill_code = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()
    moderated_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = [
            "id", "name", "rating", "comment", "source", "status", "posted_at",
            "bill", "bill_code", "table_label", "customer_phone",
            "moderated_at", "moderated_by_name", "created_by_name", "created_at",
        ]
        read_only_fields = [
            "id", "source", "status", "bill", "bill_code", "table_label", "customer_phone",
            "moderated_at", "moderated_by_name", "created_by_name", "created_at",
        ]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("error.reviewNameRequired")
        return value

    def validate_comment(self, value):
        return value.strip()

    def get_table_label(self, review):
        return review.bill.table.label_en if review.bill_id and review.bill.table_id else None

    def get_bill_code(self, review):
        return review.bill.bill_code if review.bill_id else None

    def get_customer_phone(self, review):
        return review.customer.phone if review.customer_id else None

    def get_moderated_by_name(self, review):
        return review.moderated_by.name if review.moderated_by_id else None

    def get_created_by_name(self, review):
        return review.created_by.name if review.created_by_id else None


class PublicReviewSerializer(serializers.ModelSerializer):
    """What the website shows: no phone, bill or staff details."""

    class Meta:
        model = Review
        fields = ["id", "name", "rating", "comment", "source", "posted_at"]
        read_only_fields = fields


class GuestReviewSerializer(serializers.Serializer):
    session_token = serializers.CharField(max_length=256)
    phone = serializers.CharField(max_length=32)
    bill_id = serializers.IntegerField(min_value=1)
    name = serializers.CharField(max_length=80)
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("error.reviewNameRequired")
        return value

    def validate_comment(self, value):
        return value.strip()
