from rest_framework import serializers

from .models import Customer


class CustomerSerializer(serializers.ModelSerializer):
    """
    `last_order_at` is read-only: it's set by linking a customer to an order
    (orders' OrderCustomerView), never by hand.

    `branch` is writable like everywhere else in this API, but
    CustomerViewSet.create() fills it in from the requester's token so the POS
    payment screen never has to know its own branch id.
    """

    phone = serializers.CharField(error_messages={"blank": "error.phoneRequired", "required": "error.phoneRequired"})

    class Meta:
        model = Customer
        fields = ["id", "branch", "phone", "name", "last_order_at", "created_at", "updated_at"]
        read_only_fields = ["id", "last_order_at", "created_at", "updated_at"]

    def validate_phone(self, value):
        return value.strip()

    def validate(self, attrs):
        phone = attrs.get("phone")
        branch = attrs.get("branch") or getattr(self.instance, "branch", None)
        if not phone or not branch:
            return attrs

        # Phone is unique per branch, but a *soft-deleted* row still occupies
        # it (BaseModel deletes by flagging, not by removing the row) — so this
        # checks all_objects, and CustomerViewSet.create() resolves a clash by
        # restoring the old row instead of ever inserting a duplicate.
        clash = Customer.all_objects.filter(branch=branch, phone=phone)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({"phone": "error.phoneTaken"})
        return attrs
