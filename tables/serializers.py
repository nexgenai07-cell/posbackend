from rest_framework import serializers

from common.validators import reject_branch_mismatch, require_bilingual_pair

from .models import Table


class TableSerializer(serializers.ModelSerializer):
    # Neither language is unconditionally required — see require_bilingual_pair
    # in validate(), which only requires whichever one the admin UI has active.
    label_en = serializers.CharField(required=False, allow_blank=True)
    label_ar = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Table
        fields = ["id", "branch", "label_en", "label_ar", "qr_code", "session_token", "status", "created_at", "updated_at"]
        # qr_code/session_token/status are only ever changed via open/close/needs-bill.
        read_only_fields = ["id", "qr_code", "session_token", "status", "created_at", "updated_at"]

    def validate(self, attrs):
        branch = attrs.get("branch", getattr(self.instance, "branch", None))
        reject_branch_mismatch(self.context.get("request"), branch)
        require_bilingual_pair(attrs, self.instance, "label")

        # Same convention as catalog's name_en uniqueness check: only the
        # English label is checked, matching the old mock (single-language,
        # no bilingual labels) and catalog's own name_en-only precedent.
        label_en = attrs.get("label_en", getattr(self.instance, "label_en", None))
        if label_en and branch:
            qs = Table.objects.filter(branch=branch, label_en__iexact=label_en.strip())
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"label_en": "error.tableLabelExists"})
        return attrs
