"""
Import credentials that were onboarded by hand with the ZATCA Java SDK, so a
sandbox that already works doesn't have to be onboarded again.

Sandbox only, on purpose: Simulation and Production devices are onboarded
through the go-live wizard, whose key is generated on the server and never
exists as a file. Reads, from one folder:

  my_private_key.pem      the EC private key (PEM with headers)
  compliance_csid.json    POST /compliance response (token, secret, requestID)
  production_csid.json    POST /production/csids response (token, secret)
  my_csr.pem              optional, kept for reference

Nothing read here is printed or logged; the caller gets back the new device
and a list of plain-language warnings.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction

from . import certs, crypto
from .environments import Environment, profile
from .models import DeviceStatus, ZatcaAuditLog, ZatcaDevice, ZatcaSettings


class CredentialImportError(Exception):
    """A plain-language reason the import can't go ahead."""


@dataclass
class ImportResult:
    device: ZatcaDevice
    warnings: list = field(default_factory=list)
    replaced: ZatcaDevice | None = None


def import_sandbox_credentials(branch, folder, *, replace=False, actor=None, actor_label="manage.py zatca_import"):
    folder = Path(folder)
    settings = ZatcaSettings.for_branch(branch)
    try:
        result = _import(settings, folder, replace)
    except CredentialImportError as exc:
        ZatcaAuditLog.objects.create(
            branch=branch, actor=actor, actor_label=actor_label, action="import_credentials", ok=False, summary=str(exc),
        )
        raise
    ZatcaAuditLog.objects.create(
        branch=branch, device=result.device, actor=actor, actor_label=actor_label, action="import_credentials", ok=True,
        summary=f"Imported sandbox credentials from {folder} into device #{result.device.pk}"
        + (f", replacing device #{result.replaced.pk}" if result.replaced else ""),
    )
    return result


def _import(settings, folder, replace):
    if settings.environment != Environment.SANDBOX:
        raise CredentialImportError("Importing is for sandbox credentials only. This branch is set to "
                           f"{settings.get_environment_display()}; use the go-live wizard instead.")
    configured, problem = crypto.encryption_status()
    if not configured:
        raise CredentialImportError("ZATCA_ENCRYPTION_KEY is "
                           + ("not set" if problem == "missing" else "not a valid Fernet key")
                           + " in the backend's .env, so the secrets can't be stored encrypted.")

    try:
        private_key = certs.normalize_private_key(_read(folder / "my_private_key.pem"))
    except (ValueError, TypeError):
        raise CredentialImportError("my_private_key.pem can't be read as an unencrypted EC private key.") from None
    compliance = _read_json(folder / "compliance_csid.json", ("binarySecurityToken", "secret", "requestID"))
    production = _read_json(folder / "production_csid.json", ("binarySecurityToken", "secret"))
    csr_path = folder / "my_csr.pem"
    csr = csr_path.read_text(encoding="ascii").strip() if csr_path.exists() else ""

    try:
        compliance_cert = certs.cert_b64_from_token(compliance["binarySecurityToken"])
        production_cert = certs.cert_b64_from_token(production["binarySecurityToken"])
        compliance_info = certs.info(compliance_cert)
        production_info = certs.info(production_cert)
    except ValueError:
        raise CredentialImportError("A binarySecurityToken in the CSID files isn't a valid certificate.") from None
    if not certs.key_matches_cert(private_key, compliance_cert):
        raise CredentialImportError("my_private_key.pem is not the key the compliance certificate was issued for.")

    warnings = []
    if production_info.vat_number and production_info.vat_number != settings.vat_number:
        warnings.append(
            f"The production certificate is for VAT {production_info.vat_number}, but Business details say "
            f"{settings.vat_number}. ZATCA rejects reporting/clearance when these differ."
        )
    if profile(Environment.SANDBOX).two_vat_fallback and compliance_info.vat_number != settings.vat_number:
        warnings.append(
            f"The compliance certificate is for VAT {compliance_info.vat_number}. In Sandbox that's expected: "
            "compliance-check documents will use that number, reporting/clearance use "
            f"{settings.vat_number}."
        )

    with transaction.atomic():
        current = ZatcaDevice.objects.select_for_update().filter(branch=settings.branch, is_active=True).first()
        if current and not replace:
            raise CredentialImportError(f"This branch already has an active device (#{current.pk}). "
                               "Run again with --replace to archive it and use the imported credentials.")
        if current:
            current.archive()
        device = ZatcaDevice(
            branch=settings.branch,
            environment=Environment.SANDBOX,
            status=DeviceStatus.ACTIVE,
            egs_serial=compliance_info.egs_serial,
            common_name=compliance_info.common_name,
            location=compliance_info.location,
            invoice_types=settings.invoice_types,
            csr=csr,
            compliance_cert=compliance_cert,
            production_cert=production_cert,
        )
        device.set_secret("private_key", private_key)
        device.set_secret("compliance_token", compliance["binarySecurityToken"])
        device.set_secret("compliance_secret", compliance["secret"])
        device.set_secret("compliance_request_id", str(compliance["requestID"]))
        device.set_secret("production_token", production["binarySecurityToken"])
        device.set_secret("production_secret", production["secret"])
        device.save()
    return ImportResult(device=device, warnings=warnings, replaced=current)


def _read(path):
    if not path.exists():
        raise CredentialImportError(f"{path.name} wasn't found in {path.parent}.")
    return path.read_text(encoding="ascii").strip()


def _read_json(path, required):
    try:
        data = json.loads(_read(path))
    except json.JSONDecodeError:
        raise CredentialImportError(f"{path.name} isn't valid JSON.") from None
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise CredentialImportError(f"{path.name} is missing {', '.join(missing)}.")
    return data
