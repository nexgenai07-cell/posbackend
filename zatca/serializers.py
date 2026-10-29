from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from . import certs
from .environments import profile
from .models import BusinessCustomer, ZatcaDevice, ZatcaDocument, ZatcaSettings
from .validators import (
    additional_no_error,
    building_no_error,
    is_vat_group,
    other_id_error,
    postal_code_error,
    vat_number_error,
)

# Blank is checked in validate() against the merged values, so a PATCH of one
# field doesn't trip over another and the error lands on the empty field.
_REQUIRED = (
    "seller_name_en", "seller_name_ar", "street", "district", "city",
    "org_unit", "business_category",
)

# DRF's own messages are English sentences; this API speaks in codes.
_FIELD_MESSAGES = {
    "blank": "error.zatcaFieldRequired",
    "required": "error.zatcaFieldRequired",
    "null": "error.zatcaFieldRequired",
    "max_length": "error.zatcaFieldTooLong",
    "invalid_choice": "error.zatcaChoiceInvalid",
    "invalid": "error.zatcaNumberInvalid",
    "max_digits": "error.zatcaVatRateInvalid",
    "max_decimal_places": "error.zatcaVatRateInvalid",
    "max_whole_digits": "error.zatcaVatRateInvalid",
}


class ZatcaSettingsSerializer(serializers.ModelSerializer):
    environment = serializers.CharField(read_only=True)
    is_test = serializers.SerializerMethodField()
    is_live = serializers.SerializerMethodField()
    locked_fields = serializers.SerializerMethodField()
    is_vat_group = serializers.SerializerMethodField()

    class Meta:
        model = ZatcaSettings
        fields = [
            "environment", "is_test", "is_live", "enabled",
            "seller_name_en", "seller_name_ar", "vat_number", "other_id", "other_id_scheme",
            "street", "building_no", "additional_no", "district", "city", "postal_code", "country",
            "org_unit", "business_category", "invoice_types", "default_vat_rate", "prices_include_vat",
            "receipt_footer_en", "receipt_footer_ar",
            "locked_fields", "is_vat_group", "enabled_at", "updated_at",
        ]
        read_only_fields = ["environment", "country", "enabled_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if field.read_only:
                continue
            field.error_messages.update(_FIELD_MESSAGES)
            if name in _REQUIRED or isinstance(field, serializers.CharField):
                field.required = False
                field.allow_blank = True

    def update(self, instance, validated_data):
        # Switching ZATCA on starts the retry job's backlog window afresh, so
        # sales made while it was off are never invoiced after the fact.
        if validated_data.get("enabled") and not instance.enabled:
            validated_data["enabled_at"] = timezone.now()
        return super().update(instance, validated_data)

    def get_is_test(self, obj):
        return profile(obj.environment).is_test

    def get_is_live(self, obj):
        return profile(obj.environment).is_live

    def get_locked_fields(self, obj):
        return obj.locked_fields()

    def get_is_vat_group(self, obj):
        return is_vat_group(obj.vat_number)

    def validate(self, attrs):
        attrs = {key: value.strip() if isinstance(value, str) else value for key, value in attrs.items()}
        instance = self.instance

        def merged(name):
            return attrs[name] if name in attrs else getattr(instance, name, "")

        errors = {}
        # The go-live wizard is the one way to change a locked identity: it
        # onboards a new device for the new details.
        locked = instance.locked_fields() if instance and not self.context.get("go_live") else []
        for name in locked:
            if name in attrs and attrs[name] != getattr(instance, name):
                errors[name] = "error.zatcaFieldLocked"

        for name in _REQUIRED:
            if not merged(name):
                errors.setdefault(name, "error.zatcaFieldRequired")

        checks = {
            "vat_number": vat_number_error(merged("vat_number")),
            "building_no": building_no_error(merged("building_no")),
            "additional_no": additional_no_error(merged("additional_no")),
            "postal_code": postal_code_error(merged("postal_code")),
            "other_id": other_id_error(merged("other_id"), merged("other_id_scheme")),
        }
        for name, code in checks.items():
            if code:
                errors.setdefault(name, code)

        # A VAT group's CSR carries the group's 10-digit TIN as its unit.
        vat = merged("vat_number")
        org_unit = merged("org_unit")
        if "vat_number" not in errors and is_vat_group(vat) and not (len(org_unit) == 10 and org_unit.isdigit()):
            errors.setdefault("org_unit", "error.zatcaVatGroupOrgUnit")

        rate = merged("default_vat_rate")
        if rate is not None and not (Decimal("0") <= Decimal(rate) <= Decimal("100")):
            errors.setdefault("default_vat_rate", "error.zatcaVatRateInvalid")

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


_EXPIRY_WARNING = timedelta(days=30)


def _certificate(cert_b64):
    if not cert_b64:
        return None
    try:
        info = certs.info(cert_b64)
    except ValueError:
        return {"error": "unreadable"}
    return {
        "common_name": info.common_name,
        "subject": info.subject,
        "issuer": info.issuer,
        "serial_number": info.serial_number,
        "vat_number": info.vat_number,
        "not_before": info.not_before,
        "not_after": info.not_after,
    }


class ZatcaDeviceSerializer(serializers.ModelSerializer):
    """
    A device as the Credentials page shows it. Built field by field on
    purpose: no *_enc column and no decrypted value can ever appear here —
    for each secret only whether it is set and when.
    """

    credentials = serializers.SerializerMethodField()
    certificates = serializers.SerializerMethodField()
    signing_certificate = serializers.SerializerMethodField()
    warnings = serializers.SerializerMethodField()

    class Meta:
        model = ZatcaDevice
        fields = [
            "id", "environment", "is_active", "status", "egs_serial", "common_name", "location",
            "invoice_types", "last_icv", "last_hash", "credentials", "certificates",
            "signing_certificate", "warnings", "created_at", "archived_at",
        ]
        read_only_fields = fields

    def get_credentials(self, device):
        return [
            {"name": name, "set": device.has_secret(name), "set_at": device.secrets_set_at.get(name)}
            for name in ZatcaDevice.SECRETS
        ]

    def get_certificates(self, device):
        return {
            "compliance": _certificate(device.compliance_cert),
            "production": _certificate(device.production_cert),
        }

    def get_signing_certificate(self, device):
        return "compliance" if profile(device.environment).signs_with_compliance_cert else "production"

    def get_warnings(self, device):
        """Problems worth an owner's attention, as codes + values for the page to word."""
        warnings = []
        settings = self.context.get("settings")
        now = timezone.now()
        for kind, cert_b64 in (("compliance", device.compliance_cert), ("production", device.production_cert)):
            if not cert_b64:
                continue
            try:
                info = certs.info(cert_b64)
            except ValueError:
                warnings.append({"code": "certUnreadable", "certificate": kind})
                continue
            if info.not_after <= now:
                warnings.append({"code": "certExpired", "certificate": kind, "date": info.not_after})
            elif kind == "production" and info.not_after - now <= _EXPIRY_WARNING:
                warnings.append({"code": "certExpiringSoon", "certificate": kind, "date": info.not_after})
            # Reporting/clearance authenticate with the production CSID, whose
            # VAT must be the seller VAT on every invoice.
            if kind == "production" and settings and info.vat_number and info.vat_number != settings.vat_number:
                warnings.append({"code": "vatMismatch", "certificate": kind,
                                 "certificate_vat": info.vat_number, "settings_vat": settings.vat_number})
        if device.is_active and settings and device.environment != settings.environment:
            warnings.append({"code": "environmentMismatch", "device_environment": device.environment,
                             "settings_environment": settings.environment})
        return warnings


class BusinessCustomerSerializer(serializers.ModelSerializer):
    """The B2B buyer typed in at payment. Same format rules as the seller."""

    class Meta:
        model = BusinessCustomer
        fields = ["id", "legal_name", "vat_number", "crn", "street", "building_no", "additional_no", "district",
                  "city", "postal_code", "country"]
        read_only_fields = ["id", "country"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not field.read_only:
                field.error_messages.update(_FIELD_MESSAGES)
                field.required = False
                field.allow_blank = True

    def validate(self, attrs):
        attrs = {key: value.strip() if isinstance(value, str) else value for key, value in attrs.items()}
        errors = {}
        for name in ("legal_name", "street", "district", "city"):
            if not attrs.get(name):
                errors[name] = "error.zatcaFieldRequired"
        checks = {
            "vat_number": vat_number_error(attrs.get("vat_number", "")),
            "building_no": building_no_error(attrs.get("building_no", "")),
            "additional_no": additional_no_error(attrs.get("additional_no", "")),
            "postal_code": postal_code_error(attrs.get("postal_code", "")),
            "crn": other_id_error(attrs["crn"], "CRN") if attrs.get("crn") else None,
        }
        errors.update({name: code for name, code in checks.items() if code and name not in errors})
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class ZatcaDocumentSerializer(serializers.ModelSerializer):
    """A document as the managers' Invoices page lists it — no XML."""

    order_numbers = serializers.SerializerMethodField()
    original_number = serializers.CharField(source="original.number", default="", read_only=True)
    buyer_name = serializers.CharField(source="buyer.legal_name", default="", read_only=True)

    class Meta:
        model = ZatcaDocument
        fields = ["id", "number", "kind", "simplified", "environment", "icv", "status", "zatca_status",
                  "http_status", "warnings", "errors", "last_error", "attempts", "last_attempt_at", "accepted_at",
                  "issued_at", "total", "vat_total", "net_total", "vat_breakdown", "order_numbers", "original_number",
                  "buyer_name", "source"]
        read_only_fields = fields

    def get_order_numbers(self, obj):
        return (obj.receipt or {}).get("order_numbers", [])
