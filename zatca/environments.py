"""
Everything that differs between ZATCA's three environments, in one place.

The rest of the app asks `profile(settings.environment)` for what it needs —
base URL, certificate template, which certificate signs, whether the sandbox
two-VAT fallback applies, the receipt's test label — and never branches on
the environment name itself. Moving a restaurant from Sandbox to Simulation
to Production is therefore a settings-page change, never a code change.
"""

import re
from dataclasses import dataclass


class Environment:
    SANDBOX = "sandbox"
    SIMULATION = "simulation"
    PRODUCTION = "production"

    CHOICES = [
        (SANDBOX, "Sandbox"),
        (SIMULATION, "Simulation"),
        (PRODUCTION, "Production"),
    ]


@dataclass(frozen=True)
class EnvironmentProfile:
    key: str
    base_url: str
    # Certificate template name the CSR carries (OID 1.3.6.1.4.1.311.20.2).
    csr_template: str
    # OTP to pre-fill on the onboarding step; blank = the owner must paste a
    # real one from the Fatoora portal.
    default_otp: str
    # Sandbox only: the production CSID is a fixed demo certificate for a
    # different key, so invoices are signed with the compliance certificate.
    signs_with_compliance_cert: bool
    # Sandbox only: compliance documents carry the VAT inside the compliance
    # certificate, reporting/clearance carry the fixed sandbox seller VAT.
    two_vat_fallback: bool
    # Printed on every receipt and shown as a banner on every ZATCA page.
    is_test: bool
    # Whether invoices from this environment are legally real.
    is_live: bool
    # Go-live wizard: the business details must be the company's own (no
    # test VAT number) and the OTP must come from the Fatoora portal.
    requires_real_identity: bool
    # Go-live wizard: the owner must confirm LEGAL_WARNING before the
    # compliance CSID is requested.
    needs_legal_confirmation: bool


SANDBOX_IDENTITY = {
    # The developer portal's fixed demo production certificate is issued to
    # this VAT number; reporting/clearance reject any other seller VAT.
    "vat_number": "399999999900003",
    "other_id": "1010010000",
    "other_id_scheme": "CRN",
    "seller_name_en": "Sandbox Test Restaurant",
    "seller_name_ar": "مطعم تجريبي",
    "street": "Prince Sultan",
    "building_no": "2322",
    "additional_no": "1234",
    "district": "Al-Murabba",
    "city": "Riyadh",
    "postal_code": "12222",
    "country": "SA",
    "business_category": "Restaurant",
    "invoice_types": "1100",
}

_PROFILES = {
    Environment.SANDBOX: EnvironmentProfile(
        key=Environment.SANDBOX,
        base_url="https://gw-fatoora.zatca.gov.sa/e-invoicing/developer-portal",
        csr_template="TSTZATCA-Code-Signing",
        default_otp="123345",
        signs_with_compliance_cert=True,
        two_vat_fallback=True,
        is_test=True,
        is_live=False,
        requires_real_identity=False,
        needs_legal_confirmation=False,
    ),
    Environment.SIMULATION: EnvironmentProfile(
        key=Environment.SIMULATION,
        base_url="https://gw-fatoora.zatca.gov.sa/e-invoicing/simulation",
        csr_template="PREZATCA-Code-Signing",
        default_otp="",
        signs_with_compliance_cert=False,
        two_vat_fallback=False,
        is_test=True,
        is_live=False,
        requires_real_identity=True,
        needs_legal_confirmation=False,
    ),
    Environment.PRODUCTION: EnvironmentProfile(
        key=Environment.PRODUCTION,
        base_url="https://gw-fatoora.zatca.gov.sa/e-invoicing/core",
        csr_template="ZATCA-Code-Signing",
        default_otp="",
        signs_with_compliance_cert=False,
        two_vat_fallback=False,
        is_test=False,
        is_live=True,
        requires_real_identity=True,
        needs_legal_confirmation=True,
    ),
}

LEGAL_WARNING = "Production invoices are legally real and are reported to ZATCA under your VAT number."

# VAT numbers that only ever appear in ZATCA's test material: the sandbox
# seller, the sample buyer, and the samples' own seller. The developer
# portal's test numbers all start 399999999.
_TEST_VAT_NUMBERS = {"399999999900003", "399999999800003", "300000000000003", "310122393500003"}
_REPEATED = re.compile(r"^3(\d)\1{12}3$")
_OTP = re.compile(r"^\d{6}$")


def is_test_vat(vat_number):
    vat_number = (vat_number or "").strip()
    return vat_number in _TEST_VAT_NUMBERS or vat_number.startswith("399999999") or bool(_REPEATED.match(vat_number))


def otp_error(environment, otp):
    """None, or why this OTP can't be used in `environment`."""
    otp = (otp or "").strip()
    env = profile(environment)
    if not otp:
        return "error.zatcaOtpRequired"
    if env.requires_real_identity and (not _OTP.match(otp) or otp == _PROFILES[Environment.SANDBOX].default_otp):
        return "error.zatcaOtpInvalid"
    return None


def profile(environment):
    try:
        return _PROFILES[environment]
    except KeyError:
        raise ValueError(f"Unknown ZATCA environment: {environment!r}") from None
