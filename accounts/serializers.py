from rest_framework import serializers
from django.db import transaction

from branches.models import Branch
from common.validators import reject_branch_mismatch
from kitchen.models import KitchenStation

from .models import Shift, ShiftTemplate, Staff, StaffRole
from .passwords import validate_new_password as check_password_strength
from .usernames import USERNAME_MAX_LENGTH, is_valid_username, normalize_username


class PasswordLoginSerializer(serializers.Serializer):
    """Shape only. The credential check, counter and lockout live in
    accounts/services.py authenticate_staff(), which needs a transaction."""

    username = serializers.CharField(max_length=USERNAME_MAX_LENGTH)
    password = serializers.CharField(max_length=128, trim_whitespace=False)


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(max_length=128, trim_whitespace=False)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False)

    def validate(self, attrs):
        staff = self.context["request"].user
        if not staff.check_password(attrs["current_password"]):
            raise serializers.ValidationError({"current_password": "error.invalidCurrentPassword"})
        # The first login after the PIN migration uses the old PIN as the
        # password; "changing" it to the same value would defeat the point.
        if staff.check_password(attrs["new_password"]):
            raise serializers.ValidationError({"new_password": "error.passwordSameAsCurrent"})
        check_password_strength(attrs["new_password"], staff, field="new_password")
        return attrs


class TemporaryPasswordSerializer(serializers.Serializer):
    """Owner-side unlock (password optional) and reset-password (required)."""

    new_password = serializers.CharField(max_length=128, trim_whitespace=False, required=False, allow_blank=True)

    def __init__(self, *args, staff, required=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.staff = staff
        self.fields["new_password"].required = required
        self.fields["new_password"].allow_blank = not required

    def validate_new_password(self, value):
        if value:
            check_password_strength(value, self.staff, field="new_password")
        return value or None


class StaffSerializer(serializers.ModelSerializer):
    # Optional on create: a blank username is generated from the name
    # (Staff.save), which the owner can then edit. Ignored when blank on update.
    username = serializers.CharField(required=False, allow_blank=True, max_length=USERNAME_MAX_LENGTH)
    # Create only. An owner changes someone's password through
    # /staff/<id>/reset-password/, which is Owner-only and leaves an audit
    # trail; PATCH is open to managers too and must not be a back door.
    password = serializers.CharField(write_only=True, required=False, max_length=128, trim_whitespace=False)
    is_locked = serializers.BooleanField(read_only=True)
    # Kitchen stations this chef works at. Only kept for the kitchen role:
    # anyone else is saved with none.
    stations = serializers.PrimaryKeyRelatedField(
        queryset=KitchenStation.objects.all(), many=True, required=False,
        error_messages={"does_not_exist": "error.stationNotFound"},
    )

    class Meta:
        model = Staff
        fields = [
            "id", "branch", "name", "username", "role", "password", "is_active", "stations", "is_head_chef",
            "must_change_password", "is_locked", "locked_at", "unlocked_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "must_change_password", "is_locked", "locked_at", "unlocked_at", "created_at", "updated_at",
        ]

    def validate_username(self, value):
        value = normalize_username(value)
        if not value:
            return value
        if not is_valid_username(value):
            raise serializers.ValidationError("error.usernameInvalid")
        taken = Staff.all_objects.filter(username=value)
        if self.instance:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise serializers.ValidationError("error.usernameTaken")
        return value

    def validate(self, attrs):
        branch = attrs.get("branch") or getattr(self.instance, "branch", None)
        reject_branch_mismatch(self.context.get("request"), branch)
        if any(station.branch_id != getattr(branch, "pk", None) for station in attrs.get("stations", [])):
            raise serializers.ValidationError({"stations": "error.stationBranchMismatch"})

        if self.instance is None:
            password = attrs.get("password")
            if not password:
                raise serializers.ValidationError({"password": "error.passwordRequired"})
            # Unsaved stand-in so the similarity check sees the new username/name.
            check_password_strength(password, Staff(name=attrs.get("name", ""), username=attrs.get("username", "")))
        elif "password" in attrs:
            raise serializers.ValidationError({"password": "error.useResetPassword"})
        return attrs

    def _save_stations(self, staff, stations):
        if staff.role != StaffRole.KITCHEN:
            staff.stations.clear()
        elif stations is not None:
            staff.stations.set(stations)

    def _kitchen_only(self, validated_data, role):
        # Head chef is a kitchen title; anyone else is saved without it.
        if role != StaffRole.KITCHEN:
            validated_data["is_head_chef"] = False

    def create(self, validated_data):
        password = validated_data.pop("password")
        stations = validated_data.pop("stations", None)
        if not validated_data.get("username"):
            validated_data.pop("username", None)
        self._kitchen_only(validated_data, validated_data.get("role"))
        with transaction.atomic():
            staff = Staff(**validated_data)
            staff.set_password(password)
            # Somebody else chose this password, so it is temporary by definition.
            staff.must_change_password = True
            staff.save()
            self._save_stations(staff, stations)
        return staff

    def update(self, instance, validated_data):
        stations = validated_data.pop("stations", None)
        if not validated_data.get("username"):
            validated_data.pop("username", None)
        self._kitchen_only(validated_data, validated_data.get("role", instance.role))
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        with transaction.atomic():
            instance.save()
            self._save_stations(instance, stations)
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
