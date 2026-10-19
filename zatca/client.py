"""
HTTP client for ZATCA's Fatoora API (V2). The base URL comes only from
environments.profile(); credentials are passed in per call, already
decrypted by the caller, and never logged or put into an exception.

Outcomes:
  - ZatcaResponse for anything ZATCA answered with a body it meant (200,
    202 = accepted with warnings, 400 = rejected, 409 = already submitted);
  - ZatcaUnavailable for no answer, a timeout or a 5xx/429: the document
    stays pending and the retry job sends it again, unchanged;
  - ZatcaAuthError for 401/403: credentials problem, retrying won't help.
"""

import base64
import logging
from dataclasses import dataclass, field

import requests

from .environments import profile

log = logging.getLogger(__name__)

TIMEOUT = (10, 30)


class ZatcaUnavailable(Exception):
    """ZATCA couldn't be reached or failed on its side; safe to retry."""


class ZatcaAuthError(Exception):
    """ZATCA refused the credentials (401/403)."""


@dataclass
class ZatcaResponse:
    http_status: int
    data: dict = field(default_factory=dict)

    @property
    def validation(self):
        return self.data.get("validationResults") or {}

    @property
    def status(self):
        """PASS / WARNING / ERROR, ZATCA's own verdict."""
        return self.validation.get("status") or ""

    @property
    def warnings(self):
        return _messages(self.validation.get("warningMessages"))

    @property
    def errors(self):
        return _messages(self.validation.get("errorMessages")) or _messages(self.data.get("errors"))

    @property
    def reporting_status(self):
        return self.data.get("reportingStatus") or ""

    @property
    def clearance_status(self):
        return self.data.get("clearanceStatus") or ""

    @property
    def cleared_invoice(self):
        """ZATCA's cleared XML (standard invoices), decoded, or ''."""
        encoded = self.data.get("clearedInvoice")
        return base64.b64decode(encoded).decode("utf-8") if encoded else ""

    def summary(self):
        """The response without the (large) cleared invoice, for storing and printing."""
        return {key: value for key, value in self.data.items() if key != "clearedInvoice"}


@dataclass(frozen=True)
class Csid:
    request_id: str
    token: str  # binarySecurityToken
    secret: str
    disposition: str


class ZatcaClient:
    def __init__(self, environment, session=None, timeout=TIMEOUT):
        self.environment = environment
        self.base_url = profile(environment).base_url
        self.session = session or requests.Session()
        self.timeout = timeout

    # Onboarding ------------------------------------------------------------

    def compliance_csid(self, csr_pem, otp):
        response = self._post("/compliance", {"csr": base64.b64encode(csr_pem.encode("ascii")).decode("ascii")},
                              headers={"OTP": otp})
        return self._csid(response)

    def production_csid(self, compliance_request_id, auth):
        response = self._post("/production/csids", {"compliance_request_id": compliance_request_id}, auth=auth)
        return self._csid(response)

    def compliance_check(self, signed, document_uuid, auth):
        return self._document("/compliance/invoices", signed, document_uuid, auth)

    # Issuing ---------------------------------------------------------------

    def report(self, signed, document_uuid, auth):
        return self._document("/invoices/reporting/single", signed, document_uuid, auth, clearance_status="0")

    def clear(self, signed, document_uuid, auth):
        return self._document("/invoices/clearance/single", signed, document_uuid, auth, clearance_status="1")

    # -----------------------------------------------------------------------

    def _document(self, path, signed, document_uuid, auth, clearance_status=None):
        body = {
            "invoiceHash": signed.invoice_hash,
            "uuid": document_uuid,
            "invoice": base64.b64encode(signed.xml.encode("utf-8")).decode("ascii"),
        }
        headers = {"Clearance-Status": clearance_status} if clearance_status is not None else {}
        response = self._post(path, body, headers=headers, auth=auth)
        return ZatcaResponse(response.status_code, _json(response))

    def _csid(self, response):
        data = _json(response)
        if response.status_code != 200 or not data.get("binarySecurityToken"):
            return ZatcaResponse(response.status_code, data)
        return Csid(
            request_id=str(data.get("requestID", "")),
            token=data["binarySecurityToken"],
            secret=data.get("secret", ""),
            disposition=data.get("dispositionMessage", ""),
        )

    def _post(self, path, body, headers=None, auth=None):
        all_headers = {
            "Accept": "application/json",
            "Accept-Language": "en",
            "Accept-Version": "V2",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        if auth is not None:
            token, secret = auth
            credentials = base64.b64encode(f"{token}:{secret}".encode("utf-8")).decode("ascii")
            all_headers["Authorization"] = f"Basic {credentials}"
        url = self.base_url + path
        try:
            response = self.session.post(url, json=body, headers=all_headers, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            # The exception text can include the URL, never the headers.
            raise ZatcaUnavailable(f"ZATCA {self.environment} unreachable at {path}: {type(exc).__name__}") from None
        log.info("ZATCA %s POST %s -> %s", self.environment, path, response.status_code)
        if response.status_code in (401, 403):
            raise ZatcaAuthError(f"ZATCA {self.environment} refused the credentials at {path} ({response.status_code})")
        if response.status_code == 429 or response.status_code >= 500:
            raise ZatcaUnavailable(f"ZATCA {self.environment} answered {response.status_code} at {path}")
        return response


def _json(response):
    try:
        data = response.json()
    except ValueError:
        return {"message": (response.text or "")[:500]}
    return data if isinstance(data, dict) else {"data": data}


def _messages(items):
    return [
        {"code": item.get("code", ""), "category": item.get("category", ""), "message": item.get("message", "")}
        for item in (items or [])
        if isinstance(item, dict)
    ]
