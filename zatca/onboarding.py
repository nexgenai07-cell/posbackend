"""
Onboard a new EGS device with ZATCA: the one code path behind both
`manage.py zatca_onboard` and the go-live wizard (golive.py), which calls
the steps one at a time.

  1. create_device: key + CSR generated here; key encrypted at once   keys_ready
  2. request_compliance_csid: POST /compliance with the OTP              compliance_csid
  3. run_compliance_checks: every required check accepted               compliance_passed
     (repeatable with the same compliance CSID: no new OTP)
  4. request_production_csid: POST /production/csids                     ready
  5. activate_device: the branch's current device is archived and
     the new one takes over                                              active

The new device always starts its own chain (ICV 0, initial PIH) and is
bound to one environment for life. Until step 5 it is not active, so the
branch keeps issuing with its current device the whole time. A failed step
leaves the device at the last good status (or needs_attention after a
failed compliance check) and is written to the audit log.

`identity` is the ZatcaSettings the device is for. The wizard passes an
unsaved copy holding the details being onboarded, so the live settings (and
the device issuing with them) are untouched until activation.
"""

from dataclasses import dataclass, field

from django.db import transaction

from . import certs, compliance, crypto, csr
from .client import Csid, ZatcaAuthError, ZatcaClient, ZatcaUnavailable
from .environments import profile
from .models import DeviceStatus, ZatcaAuditLog, ZatcaDevice, ZatcaSettings


class OnboardingError(Exception):
    def __init__(self, message, device=None, checks=None):
        super().__init__(message)
        self.device = device
        self.checks = checks or []


@dataclass
class OnboardResult:
    device: ZatcaDevice
    checks: list = field(default_factory=list)
    replaced: ZatcaDevice | None = None


def onboard(branch, *, otp, environment=None, activate=False, actor=None, actor_label="", client=None):
    settings = ZatcaSettings.for_branch(branch)
    environment = environment or settings.environment
    client = client or ZatcaClient(environment)
    log = Audit(branch, actor, actor_label)
    otp = (otp or profile(environment).default_otp).strip()
    if not otp:
        raise log.fail(None, "onboard_start", "An OTP from the Fatoora portal is needed for "
                       f"{environment}.")

    device = create_device(branch, settings, environment, log=log)
    request_compliance_csid(device, otp, client=client, log=log)
    results = run_compliance_checks(device, settings, client=client, log=log)
    request_production_csid(device, client=client, log=log)
    replaced = activate_device(device, actor=actor, actor_label=actor_label) if activate else None
    return OnboardResult(device=device, checks=results, replaced=replaced)


def create_device(branch, identity, environment, *, log):
    """Step 1: a new, inactive device with its key pair and CSR."""
    configured, problem = crypto.encryption_status()
    if not configured:
        raise log.fail(None, "onboard_start", "ZATCA_ENCRYPTION_KEY is "
                       + ("not set" if problem == "missing" else "not a valid Fernet key") + ".")
    device = ZatcaDevice.objects.create(branch=branch, environment=environment, is_active=False,
                                        status=DeviceStatus.NOT_STARTED, invoice_types=identity.invoice_types)
    request = csr.generate(identity, environment, device_label=f"{branch.pk}-{device.pk}")
    device.csr = request.csr_pem
    device.common_name, device.egs_serial, device.location = request.common_name, request.egs_serial, request.location
    device.set_secret("private_key", request.private_key_pem)
    device.status = DeviceStatus.KEYS_READY
    device.save()
    log.ok(device, "generate_keys", f"Key pair and CSR generated for device #{device.pk} ({environment}).")
    return device


def request_compliance_csid(device, otp, *, client, log):
    """Step 2. The OTP is used once here and never stored or logged."""
    if device.status != DeviceStatus.KEYS_READY:
        raise log.fail(device, "compliance_csid", f"Device #{device.pk} is {device.get_status_display()}; "
                       "a compliance CSID is requested once, right after the keys are generated.")
    issued = _call(log, device, "compliance_csid", lambda: client.compliance_csid(device.csr, otp))
    device.compliance_cert = certs.cert_b64_from_token(issued.token)
    device.set_secret("compliance_token", issued.token)
    device.set_secret("compliance_secret", issued.secret)
    device.set_secret("compliance_request_id", str(issued.request_id))
    device.status = DeviceStatus.COMPLIANCE_CSID
    device.save()
    log.ok(device, "compliance_csid", f"Compliance CSID issued ({issued.disposition or 'ISSUED'}).")
    return device


# Where the compliance checks can be (re)run with the same compliance CSID.
CHECKABLE = (DeviceStatus.COMPLIANCE_CSID, DeviceStatus.NEEDS_ATTENTION, DeviceStatus.COMPLIANCE_PASSED)


def run_compliance_checks(device, identity, *, client, log):
    """Step 3: send every required check; returns the CheckResults. After a
    failure it can simply be run again: no new OTP, no new CSID."""
    if device.status not in CHECKABLE:
        raise log.fail(device, "compliance_checks", f"Device #{device.pk} is {device.get_status_display()}; "
                       "compliance checks need its compliance CSID first.")
    checks = compliance.standard_checks(device.invoice_types or identity.invoice_types)
    try:
        results = compliance.run(device, identity, checks, client=client)
    except (ZatcaUnavailable, ZatcaAuthError) as exc:
        raise log.fail(device, "compliance_checks", str(exc)) from None
    failed = [result for result in results if not result.passed]
    if failed:
        device.status = DeviceStatus.NEEDS_ATTENTION
        device.save(update_fields=["status", "updated_at"])
        raise log.fail(device, "compliance_checks",
                       f"{len(failed)} of {len(results)} compliance checks failed: "
                       + "; ".join(f"{r.label}: " + ", ".join(e["code"] for e in r.errors) for r in failed),
                       checks=results)
    device.status = DeviceStatus.COMPLIANCE_PASSED
    device.save(update_fields=["status", "updated_at"])
    log.ok(device, "compliance_checks", f"All {len(results)} compliance checks accepted.")
    return results


# Devices whose compliance CSID can still be used to re-run the checks.
RECHECKABLE = (DeviceStatus.READY, DeviceStatus.ACTIVE)


def recheck_compliance(device, identity, *, client, log):
    """Re-run every compliance check for a device that already went live,
    with the compliance CSID it was given (no new OTP). The checks have
    their own numbering, so the device's invoice chain and status are left
    exactly as they are, pass or fail."""
    if device.status not in RECHECKABLE or device.archived_at is not None:
        raise log.fail(device, "compliance_recheck", f"Device #{device.pk} is {device.get_status_display()}; "
                       "only a ready or active device can repeat its compliance checks.")
    checks = compliance.standard_checks(device.invoice_types or identity.invoice_types)
    try:
        results = compliance.run(device, identity, checks, client=client)
    except (ZatcaUnavailable, ZatcaAuthError) as exc:
        raise log.fail(device, "compliance_recheck", str(exc)) from None
    failed = [result for result in results if not result.passed]
    if failed:
        log.fail(device, "compliance_recheck",
                 f"{len(failed)} of {len(results)} compliance checks failed: "
                 + "; ".join(f"{r.label}: " + ", ".join(e["code"] for e in r.errors) for r in failed))
    else:
        log.ok(device, "compliance_recheck", f"All {len(results)} compliance checks accepted again.")
    return results


def request_production_csid(device, *, client, log):
    """Step 4: the certificate the device reports and clears with."""
    if device.status != DeviceStatus.COMPLIANCE_PASSED:
        raise log.fail(device, "production_csid", f"Device #{device.pk} is {device.get_status_display()}; "
                       "every compliance check must pass first.")
    request_id = device.get_secret("compliance_request_id")
    # ZATCA sends the request ID as a number; send it back the same way.
    request_id = int(request_id) if request_id.isdigit() else request_id
    production = _call(log, device, "production_csid",
                       lambda: client.production_csid(request_id, device.compliance_auth()))
    device.production_cert = certs.cert_b64_from_token(production.token)
    device.set_secret("production_token", production.token)
    device.set_secret("production_secret", production.secret)
    device.status = DeviceStatus.READY
    device.save()
    log.ok(device, "production_csid", "Production CSID issued.")
    return device


def activate_device(device, *, actor=None, actor_label=""):
    """Make a ready device the branch's device, archiving the current one."""
    if device.status != DeviceStatus.READY:
        raise OnboardingError(f"Device #{device.pk} is {device.get_status_display()}, not ready.", device)
    with transaction.atomic():
        current = (ZatcaDevice.objects.select_for_update()
                   .filter(branch=device.branch, is_active=True).exclude(pk=device.pk).first())
        if current:
            current.archive()
        device.is_active = True
        device.status = DeviceStatus.ACTIVE
        device.save(update_fields=["is_active", "status", "updated_at"])
    Audit(device.branch, actor, actor_label).ok(
        device, "activate_device",
        f"Device #{device.pk} is now the branch's device" + (f", replacing #{current.pk}." if current else "."))
    return current


def _call(log, device, step, request):
    try:
        result = request()
    except (ZatcaUnavailable, ZatcaAuthError) as exc:
        raise log.fail(device, step, str(exc)) from None
    if not isinstance(result, Csid):
        messages = result.errors or [{"code": "", "message": result.data.get("message", "")}]
        raise log.fail(device, step, f"ZATCA answered {result.http_status}: "
                       + "; ".join(f"{m['code']} {m['message']}".strip() for m in messages))
    return result


class Audit:
    """Writes every onboarding step to ZatcaAuditLog. Summaries never hold
    a secret or an OTP."""

    def __init__(self, branch, actor, actor_label=""):
        self.branch, self.actor, self.actor_label = branch, actor, actor_label

    def ok(self, device, action, summary):
        ZatcaAuditLog.objects.create(branch=self.branch, device=device, actor=self.actor, actor_label=self.actor_label,
                                     action=action, ok=True, summary=summary)

    def fail(self, device, action, summary, checks=None):
        ZatcaAuditLog.objects.create(branch=self.branch, device=device, actor=self.actor, actor_label=self.actor_label,
                                     action=action, ok=False, summary=summary)
        return OnboardingError(summary, device, checks)
