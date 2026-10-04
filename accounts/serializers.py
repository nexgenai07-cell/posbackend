from rest_framework import serializers

from common.validators import reject_branch_mismatch

from .models import Shift, Staff


class PinLoginSerializer(serializers.Serializer):
    staff_id = serializers.IntegerField()
    pin = serializers.CharField()

    def validate(self, attrs):
        try:
            staff = Staff.objects.get(pk=attrs["staff_id"], is_active=True)
        except Staff.DoesNotExist:
            raise serializers.ValidationError({"error": "error.invalidCredentials"})

        if not staff.check_pin(attrs["pin"]):
            raise serializers.ValidationError({"error": "error.invalidCredentials"})

        attrs["staff"] = staff
        return attrs


class LoginStaffChoiceSerializer(serializers.ModelSerializer):
    """
    The login screen's name picker, and nothing more. Deliberately omits
    `pin`, `is_active` and `created_at`/`updated_at`: this endpoint is
    reachable *before* authentication, so it must not hand out anything a
    name dropdown doesn't need. See LoginChoicesView.
    """

    class Meta:
        model = Staff
        fields = ["id", "name", "role"]
        read_only_fields = fields


class StaffSerializer(serializers.ModelSerializer):
    pin = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = Staff
        fields = ["id", "branch", "name", "role", "pin", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        branch = attrs.get("branch") or getattr(self.instance, "branch", None)
        reject_branch_mismatch(self.context.get("request"), branch)

        pin = attrs.get("pin")
        if pin:
            exclude_pk = self.instance.pk if self.instance else None
            if Staff.pin_taken_in_branch(branch, pin, exclude_pk=exclude_pk):
                raise serializers.ValidationError({"pin": "error.pinTaken"})
        elif self.instance is None:
            raise serializers.ValidationError({"pin": "error.pinRequired"})
        return attrs

    def create(self, validated_data):
        pin = validated_data.pop("pin")
        staff = Staff(**validated_data)
        staff.set_pin(pin)
        staff.save()
        return staff

    def update(self, instance, validated_data):
        pin = validated_data.pop("pin", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if pin:
            instance.set_pin(pin)
        instance.save()
        return instance


class ShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shift
        fields = ["id", "staff", "clock_in", "clock_out", "created_at", "updated_at"]
        read_only_fields = fields
