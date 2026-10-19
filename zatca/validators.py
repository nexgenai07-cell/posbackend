"""
Format checks for the identity and address fields ZATCA validates.

Each returns an error code (translated to a plain-language message in the
admin's errors.json) or None. Shared by the seller settings now and by
business customers (the B2B buyer) later, which follow the same rules.
"""

import re

_DIGITS = re.compile(r"\d+")


def _digits(value, length):
    return bool(value) and len(value) == length and bool(_DIGITS.fullmatch(value))


def vat_number_error(value):
    """15 digits, first and last digit 3 (e.g. 310123456700003)."""
    if not (_digits(value, 15) and value[0] == "3" and value[-1] == "3"):
        return "error.zatcaVatNumberInvalid"
    return None


def is_vat_group(vat_number):
    """A VAT group's number has 1 as its 11th digit."""
    return len(vat_number) == 15 and vat_number[10] == "1"


def building_no_error(value):
    return None if _digits(value, 4) else "error.zatcaBuildingNoInvalid"


def additional_no_error(value):
    # Optional on the national address; 4 digits when given.
    return None if not value or _digits(value, 4) else "error.zatcaAdditionalNoInvalid"


def postal_code_error(value):
    return None if _digits(value, 5) else "error.zatcaPostalCodeInvalid"


def other_id_error(value, scheme):
    if not value:
        return "error.zatcaOtherIdRequired"
    if scheme == "CRN" and not _digits(value, 10):
        return "error.zatcaCrnInvalid"
    if not value.isalnum():
        return "error.zatcaOtherIdInvalid"
    return None
