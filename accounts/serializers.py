from rest_framework import serializers
from django.db import transaction

from branches.models import Branch
from common.validators import reject_branch_mismatch

from .models import Shift, ShiftTemplate, Staff


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
    is_late = serializers.BooleanField(read_only=True)

    class Meta:
        model = Shift
        fields = ["id", "staff", "clock_in", "clock_out", "is_late", "created_at", "updated_at"]
        read_only_fields = fields


class AssignedStaffSerializer(serializers.ModelSerializer):
    class Meta:
        model = Staff
        fields = ["id", "name", "role"]
        read_only_fields = fields


class ShiftTemplateSerializer(serializers.ModelSerializer):
    assigned_staff = AssignedStaffSerializer(many=True, read_only=True)
    assigned_staff_ids = serializers.PrimaryKeyRelatedField(
        queryset=Staff.objects.all(), many=True, required=False, write_only=True,
    )
    crosses_midnight = serializers.BooleanField(read_only=True)

    class Meta:
        model = ShiftTemplate
        fields = [
            "id", "branch", "name", "start_time", "end_time", "active",
            "crosses_midnight", "assigned_staff", "assigned_staff_ids",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "branch", "crosses_midnight", "assigned_staff", "created_at", "updated_at"]

    def validate(self, attrs):
        start_time = attrs.get("start_time", getattr(self.instance, "start_time", None))
        end_time = attrs.get("end_time", getattr(self.instance, "end_time", None))
        if start_time == end_time:
            raise serializers.ValidationError({"end_time": "error.shiftTemplateTimesEqual"})
        name = attrs.get("name", getattr(self.instance, "name", "")).strip()
        if name:
            duplicates = ShiftTemplate.objects.filter(
                branch=self.context["request"].user.branch, name__iexact=name,
            )
            if self.instance:
                duplicates = duplicates.exclude(pk=self.instance.pk)
            if duplicates.exists():
                raise serializers.ValidationError({"name": "error.shiftTemplateNameExists"})
        staff = attrs.get("assigned_staff_ids", [])
        branch_id = self.context["request"].user.branch_id
        if any(member.branch_id != branch_id for member in staff):
            raise serializers.ValidationError({"assigned_staff_ids": "error.staffBranchMismatch"})
        return attrs

    def _save_assignments(self, template, staff_members):
        if staff_members is None:
            return
        selected_ids = [member.pk for member in staff_members]
        Staff.objects.filter(shift_template=template).exclude(pk__in=selected_ids).update(shift_template=None)
        Staff.objects.filter(pk__in=selected_ids).update(shift_template=template)

    def create(self, validated_data):
        staff_members = validated_data.pop("assigned_staff_ids", None)
        with transaction.atomic():
            template = ShiftTemplate.objects.create(branch=self.context["request"].user.branch, **validated_data)
            self._save_assignments(template, staff_members)
        return template

    def update(self, instance, validated_data):
        staff_members = validated_data.pop("assigned_staff_ids", None)
        with transaction.atomic():
            for name, value in validated_data.items():
                setattr(instance, name, value)
            instance.save()
            self._save_assignments(instance, staff_members)
            instance._prefetched_objects_cache = {}
        return instance


class AttendanceSettingsSerializer(serializers.ModelSerializer):
    max_clock_ins_per_day = serializers.IntegerField(min_value=1, max_value=24)
    max_clock_outs_per_day = serializers.IntegerField(min_value=1, max_value=24)
    late_threshold_minutes = serializers.IntegerField(min_value=0, max_value=1440)

    class Meta:
        model = Branch
        fields = ["max_clock_ins_per_day", "max_clock_outs_per_day", "late_threshold_minutes"]
