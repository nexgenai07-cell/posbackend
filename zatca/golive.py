"""
The go-live wizard: onboarding a new device for Sandbox (practice),
Simulation or Production, one step at a time, from the owner's browser.

  start          choose the environment                       -> details
  update_details the business details for that environment    -> details
  generate_keys  key + CSR from those details (server side)   -> otp
  compliance_csid  OTP from the Fatoora portal                -> checks
  run_checks     every compliance check (repeatable)          -> production
  production_csid                                              -> activate
  finish         make it the branch's device (practice: may
                 stop here and leave the device Ready)         -> done

Each step is a thin layer over onboarding.py, which also backs
`manage.py zatca_onboard`, so the two can never drift apart.

The rules that make it safe:
  - every run creates a new device with its own chain (ICV 1, initial PIH);
    the device in use carries on issuing until `finish` replaces it;
  - the wizard's details reach ZatcaSettings only in `finish`, together with
    the new environment, so settings and device always match;
  - Simulation and Production need a real (non-test) VAT number and a real
    OTP; Production also needs the owner to confirm LEGAL_WARNING;
  - "Back to Sandbox" (a Sandbox run while live) needs a confirmation, and
    is refused once the device in use has issued a Production document;
  - nothing here stores or logs an OTP or a secret.
"""

from django.db import IntegrityError, transaction
from django.forms.models import model_to_dict
from django.utils import timezone

from .client import ZatcaClient
from .environments import SANDBOX_IDENTITY, Environment, is_test_vat, otp_error, profile
from .models import DeviceStatus, GoLiveStatus, ZatcaGoLive, ZatcaSettings, sandbox_defaults
from .onboarding import (
    CHECKABLE,
    Audit,
    OnboardingError,
    activate_device,
    create_device,
    recheck_compliance,
    request_compliance_csid,
    request_production_csid,
    run_compliance_checks,
)

# What the wizard asks for: the seller identity a certificate is bound to.
DETAIL_FIELDS = (
    "seller_name_en", "seller_name_ar", "vat_number", "other_id", "other_id_scheme",
    "street", "building_no", "additional_no", "district", "city", "postal_code",
    "org_unit", "business_category", "invoice_types",
)
# Practice runs use the sandbox's own identity: its reporting endpoint
# accepts no other seller VAT.
PRACTICE_FIXED = ("vat_number", "other_id", "other_id_scheme")


class GoLiveError(Exception):
    """A step can't run. `code` is an error.* code; `fields` maps fields to
    codes; `detail` is ZATCA's own message when ZATCA said no."""

    def __init__(self, code, *, status=400, fields=None, detail="", checks=None):
        super().__init__(code)
        self.code, self.status, self.fields, self.detail, self.checks = code, status, fields or {}, detail, checks


def current(branch):
    return ZatcaGoLive.objects.filter(branch=branch, status=GoLiveStatus.OPEN).select_related("device").first()


def step_of(go_live):
    """Where the wizard is, from the device's own status."""
    if go_live.status != GoLiveStatus.OPEN:
        return "done"
    device = go_live.device
    if device is None:
        return "details"
    return {
        DeviceStatus.KEYS_READY: "otp",
        DeviceStatus.COMPLIANCE_CSID: "checks",
        DeviceStatus.NEEDS_ATTENTION: "checks",
        DeviceStatus.COMPLIANCE_PASSED: "production",
        DeviceStatus.READY: "activate",
    }.get(device.status, "done")


def back_to_sandbox_block(settings):
    """None if a Sandbox run may replace the device in use, else why not."""
    device = settings.active_device()
    if device and device.environment == Environment.PRODUCTION and device.documents.exists():
        return "error.zatcaBackToSandboxProduction"
    return None


# -- steps ---------------------------------------------------------------------


def start(branch, environment, *, actor, confirm_back_to_sandbox=False):
    if environment not in dict(Environment.CHOICES):
        raise GoLiveError("error.zatcaChoiceInvalid", fields={"environment": "error.zatcaChoiceInvalid"})
    settings = ZatcaSettings.for_branch(branch)
    if environment == Environment.SANDBOX and settings.environment != Environment.SANDBOX:
        blocked = back_to_sandbox_block(settings)
        if blocked:
            raise GoLiveError(blocked, status=409)
        if not confirm_back_to_sandbox:
            raise GoLiveError("error.zatcaBackToSandboxConfirm")
    try:
        with transaction.atomic():  # a savepoint: a clash must not break the caller's transaction
            go_live = ZatcaGoLive.objects.create(branch=branch, environment=environment, started_by=actor,
                                                 details=initial_details(settings, environment))
    except IntegrityError:
        raise GoLiveError("error.zatcaGoLiveInProgress", status=409) from None
    _log(go_live, actor).ok(None, "go_live_start", f"Go-live wizard started for {environment}"
                            + (" (practice)." if go_live.practice else "."))
    return go_live


def initial_details(settings, environment):
    """Pre-filled details: the sandbox identity for practice; for a live
    environment, the current details if they're real, else the branch's own
    name and address with every test value cleared."""
    current_values = {name: getattr(settings, name) for name in DETAIL_FIELDS}
    if environment == Environment.SANDBOX:
        base = current_values if settings.environment == Environment.SANDBOX else {
            name: value for name, value in sandbox_defaults(settings.branch).items() if name in DETAIL_FIELDS}
        return {**base, **{name: SANDBOX_IDENTITY[name] for name in PRACTICE_FIXED}}
    if not is_test_vat(settings.vat_number):
        return current_values
    branch = settings.branch
    return {
        **{name: "" for name in DETAIL_FIELDS},
        "seller_name_en": branch.name_en, "seller_name_ar": branch.name_ar, "other_id_scheme": "CRN",
        "street": branch.address[:255], "org_unit": branch.name_en or branch.name_ar,
        "business_category": settings.business_category, "invoice_types": settings.invoice_types,
    }


def update_details(go_live, data):
    """Save the details (all of them, as the form sends them) once valid."""
    _require_open(go_live)
    if go_live.device_id:
        raise GoLiveError("error.zatcaGoLiveDetailsLocked", status=409)
    details = {**go_live.details, **{name: _clean(data[name]) for name in DETAIL_FIELDS if name in data}}
    errors = validate_details(go_live, details)
    if errors:
        raise GoLiveError("error.zatcaFixDetails", fields=errors)
    go_live.details = details
    go_live.save(update_fields=["details", "updated_at"])
    return go_live


def validate_details(go_live, details):
    """{field: error code} for these details in the wizard's environment."""
    from .serializers import ZatcaSettingsSerializer

    identity = identity_for(go_live, details)
    serializer = ZatcaSettingsSerializer(identity, data={name: details.get(name, "") for name in DETAIL_FIELDS},
                                         partial=True, context={"go_live": True})
    errors = {} if serializer.is_valid() else {name: codes[0] if isinstance(codes, list) else codes
                                               for name, codes in serializer.errors.items()}
    env = profile(go_live.environment)
    if env.requires_real_identity and "vat_number" not in errors and is_test_vat(details.get("vat_number")):
        errors["vat_number"] = "error.zatcaTestVatNotAllowed"
    if go_live.practice:
        for name in PRACTICE_FIXED:
            if details.get(name) != SANDBOX_IDENTITY[name]:
                errors[name] = "error.zatcaPracticeIdentityFixed"
    return {name: str(code) for name, code in errors.items()}


def generate_keys(go_live, *, actor):
    _require_open(go_live)
    device = go_live.device
    if device is not None:
        if device.status != DeviceStatus.KEYS_READY:
            raise GoLiveError("error.zatcaGoLiveKeysUsed", status=409)
        device.archive()  # never used: its CSR went nowhere
    errors = validate_details(go_live, go_live.details)
    if errors:
        raise GoLiveError("error.zatcaFixDetails", fields=errors)
    try:
        device = create_device(go_live.branch, identity_for(go_live), go_live.environment, log=_log(go_live, actor))
    except OnboardingError as exc:
        raise GoLiveError("error.zatcaStepFailed", detail=str(exc)) from None
    go_live.device = device
    go_live.save(update_fields=["device", "updated_at"])
    return go_live


def compliance_csid(go_live, *, otp, confirm_legal=False, actor):
    _require_open(go_live)
    device = _require_device(go_live, DeviceStatus.KEYS_READY)
    env = profile(go_live.environment)
    otp = (otp or "").strip() or ("" if env.requires_real_identity else env.default_otp)
    problem = otp_error(go_live.environment, otp)
    if problem:
        raise GoLiveError(problem, fields={"otp": problem})
    errors = validate_details(go_live, go_live.details)
    if errors:  # re-checked: the production guard must hold at the moment it counts
        raise GoLiveError("error.zatcaFixDetails", fields=errors)
    if env.needs_legal_confirmation and not confirm_legal:
        raise GoLiveError("error.zatcaLegalConfirmRequired", fields={"confirm_legal": "error.zatcaLegalConfirmRequired"})
    if env.needs_legal_confirmation:
        go_live.legal_confirmed_at = timezone.now()
        go_live.save(update_fields=["legal_confirmed_at", "updated_at"])
    try:
        request_compliance_csid(device, otp, client=ZatcaClient(go_live.environment), log=_log(go_live, actor))
    except OnboardingError as exc:
        raise GoLiveError("error.zatcaStepFailed", detail=str(exc)) from None
    return go_live


def run_checks(go_live, *, actor):
    """Repeatable: after a failed check, run again with the same CSID."""
    _require_open(go_live)
    device = _require_device(go_live, *CHECKABLE)
    try:
        results = run_compliance_checks(device, identity_for(go_live), client=ZatcaClient(go_live.environment),
                                        log=_log(go_live, actor))
    except OnboardingError as exc:
        go_live.checks = [_check(result) for result in exc.checks]
        go_live.save(update_fields=["checks", "updated_at"])
        raise GoLiveError("error.zatcaChecksFailed", detail=str(exc), checks=go_live.checks) from None
    go_live.checks = [_check(result) for result in results]
    go_live.save(update_fields=["checks", "updated_at"])
    return go_live


def production_csid(go_live, *, actor):
    _require_open(go_live)
    device = _require_device(go_live, DeviceStatus.COMPLIANCE_PASSED)
    try:
        request_production_csid(device, client=ZatcaClient(go_live.environment), log=_log(go_live, actor))
    except OnboardingError as exc:
        raise GoLiveError("error.zatcaStepFailed", detail=str(exc)) from None
    return go_live


def finish(go_live, *, activate, actor):
    """Activate the new device (and its details and environment) — or, for a
    practice run only, stop with the device Ready and nothing switched."""
    _require_open(go_live)
    device = _require_device(go_live, DeviceStatus.READY)
    if not activate and not go_live.practice:
        raise GoLiveError("error.zatcaGoLiveMustActivate")
    log = _log(go_live, actor)
    with transaction.atomic():
        if activate:
            settings = ZatcaSettings.objects.select_for_update().get(branch=go_live.branch)
            if go_live.practice and settings.environment != Environment.SANDBOX:
                blocked = back_to_sandbox_block(settings)  # nothing may have changed since start
                if blocked:
                    raise GoLiveError(blocked, status=409)
            for name in DETAIL_FIELDS:
                setattr(settings, name, go_live.details[name])
            settings.environment = go_live.environment
            settings.save()
            activate_device(device, actor=actor)
        go_live.status, go_live.activated, go_live.finished_at = GoLiveStatus.COMPLETED, activate, timezone.now()
        go_live.save(update_fields=["status", "activated", "finished_at", "updated_at"])
    log.ok(device, "go_live_complete",
           f"Go-live to {go_live.environment} completed; device #{device.pk} "
           + ("is now in use." if activate else "is ready, not in use (practice)."))
    return go_live


def cancel(go_live, *, actor):
    _require_open(go_live)
    device = go_live.device
    if device is not None and not device.is_active and device.archived_at is None:
        device.archive()
    go_live.status, go_live.finished_at = GoLiveStatus.CANCELLED, timezone.now()
    go_live.save(update_fields=["status", "finished_at", "updated_at"])
    _log(go_live, actor).ok(device, "go_live_cancel", f"Go-live to {go_live.environment} cancelled.")
    return go_live


# -- helpers ---------------------------------------------------------------------


def identity_for(go_live, details=None):
    """An unsaved ZatcaSettings: the branch's settings with the wizard's
    details on top. What the CSR and the compliance checks are built from."""
    settings = ZatcaSettings.for_branch(go_live.branch)
    values = model_to_dict(settings, exclude=["id", "branch"])
    identity = ZatcaSettings(branch=settings.branch, **values)
    identity.environment = go_live.environment
    for name, value in (details if details is not None else go_live.details).items():
        if name in DETAIL_FIELDS:
            setattr(identity, name, value)
    return identity


def _require_open(go_live):
    if go_live.status != GoLiveStatus.OPEN:
        raise GoLiveError("error.zatcaGoLiveNotOpen", status=409)


def _require_device(go_live, *statuses):
    device = go_live.device
    if device is None:
        raise GoLiveError("error.zatcaGoLiveWrongStep", status=409)
    device.refresh_from_db()
    if device.status not in statuses:
        raise GoLiveError("error.zatcaGoLiveWrongStep", status=409)
    return device


def _check(result):
    return {
        "label": result.label, "kind": result.kind, "simplified": result.simplified, "passed": result.passed,
        "http_status": result.http_status, "status": result.status,
        "warnings": result.warnings, "errors": result.errors,
    }


def _clean(value):
    return value.strip() if isinstance(value, str) else value


def _log(go_live, actor):
    return Audit(go_live.branch, actor)


def device_summary(device):
    if device is None:
        return None
    return {"id": device.pk, "environment": device.environment, "status": device.status,
            "common_name": device.common_name, "egs_serial": device.egs_serial, "is_active": device.is_active,
            "last_icv": device.last_icv}



def recheck(device, *, actor):
    """After go-live: repeat the compliance checks with the device's own
    compliance CSID. Returns the check results (passed or not) as dicts."""
    run = ZatcaGoLive.objects.filter(device=device).order_by("-pk").first()
    if device.is_active or run is None:
        identity = ZatcaSettings.for_branch(device.branch)
    else:  # a practice device left Ready: checked with the details it was made from
        identity = identity_for(run)
    identity.environment = device.environment
    try:
        results = recheck_compliance(device, identity, client=ZatcaClient(device.environment),
                                     log=Audit(device.branch, actor))
    except OnboardingError as exc:
        raise GoLiveError("error.zatcaStepFailed", status=409, detail=str(exc)) from None
    return [_check(result) for result in results]
