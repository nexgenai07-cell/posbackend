from django.db.models import Value
from django.db.models.functions import Replace, Upper
from rest_framework import serializers

from common.validators import reject_branch_mismatch, require_bilingual_pair

from .models import Table


def normalize_label(value, uppercase=False):
    """Trim and collapse inner whitespace runs to one space ("table   1 " -> "table 1")."""
    value = " ".join((value or "").split())
    return value.upper() if uppercase else value


def label_key(value):
    """Comparison key for duplicates: case- and space-insensitive, so "Table 1", "TABLE1" and "table  1" all collide."""
    return "".join((value or "").split()).upper()


class TableSerializer(serializers.ModelSerializer):
    # Neither language is unconditionally required — see require_bilingual_pair
    # in validate(), which only requires whichever one the admin UI has active.
    label_en = serializers.CharField(required=False, allow_blank=True)
    label_ar = serializers.CharField(required=False, allow_blank=True)
    # A table that seats nobody is a data-entry mistake, not a configuration.
    seats = serializers.IntegerField(
        required=False, min_value=1, max_value=100,
        error_messages={"min_value": "error.seatsInvalid", "max_value": "error.seatsInvalid", "invalid": "error.seatsInvalid"},
    )

    class Meta:
        model = Table
        fields = ["id", "branch", "label_en", "label_ar", "seats", "qr_code", "session_token", "status", "created_at", "updated_at"]
        # qr_code/session_token/status are only ever changed via open/close/needs-bill.
        read_only_fields = ["id", "qr_code", "session_token", "status", "created_at", "updated_at"]

    def validate(self, attrs):
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        reject_branch_mismatch(self.context.get("request"), branch)
        require_bilingual_pair(attrs, self.instance, "label")

        # English labels are stored upper-case so "table 1" and "TABLE 1" never
        # render as two different-looking tables; Arabic has no case.
        if "label_en" in attrs:
            attrs["label_en"] = normalize_label(attrs["label_en"], uppercase=True)
        if "label_ar" in attrs:
            attrs["label_ar"] = normalize_label(attrs["label_ar"])

        # Duplicates are checked per language, ignoring case and all spaces, so
        # neither "Table 1" vs "table1" nor an Arabic re-entry slips through.
        if branch:
            others = Table.objects.filter(branch=branch)
            if self.instance:
                others = others.exclude(pk=self.instance.pk)
            for field in ("label_en", "label_ar"):
                key = label_key(attrs.get(field, getattr(self.instance, field, None)))
                if not key:
                    continue
                stripped = Upper(Replace(field, Value(" "), Value("")))
                if others.annotate(_key=stripped).filter(_key=key).exists():
                    raise serializers.ValidationError({field: "error.tableLabelExists"})
        return attrs
