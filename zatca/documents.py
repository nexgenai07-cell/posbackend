"""
Issuing documents into a device's chain, and sending them to ZATCA.

issue() is the only place the chain advances: under select_for_update() on
the device it takes ICV = last_icv + 1 and PIH = last_hash, builds and signs
the document, saves it and moves the device on — all in one transaction, so
two tills can never get the same ICV and a failed signature consumes none.

submit() sends a signed document as it is (report for simplified, clear for
standard). It never re-signs, so a retry is always the same document; a
rejected one stays in the chain with ZATCA's errors.
"""

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from . import qr, signing, ubl
from .client import ZatcaAuthError, ZatcaClient, ZatcaUnavailable
from .models import DeviceStatus, DocumentStatus, ZatcaDevice, ZatcaDocument, ZatcaSettings

_PREFIXES = {ubl.DocumentKind.INVOICE: "INV", ubl.DocumentKind.CREDIT_NOTE: "CRN", ubl.DocumentKind.DEBIT_NOTE: "DBN"}
_SEQUENCES = {ubl.DocumentKind.INVOICE: "invoice_seq", ubl.DocumentKind.CREDIT_NOTE: "credit_seq",
              ubl.DocumentKind.DEBIT_NOTE: "debit_seq"}


class IssueError(Exception):
    """The branch can't issue right now (no active device, ZATCA off...)."""


def seller_party(settings, vat_number=""):
    return ubl.Party(
        name=settings.seller_name_ar or settings.seller_name_en,
        vat_number=vat_number or settings.vat_number,
        other_id=settings.other_id,
        other_id_scheme=settings.other_id_scheme,
        address=ubl.Address(
            street=settings.street,
            building_no=settings.building_no,
            additional_no=settings.additional_no,
            district=settings.district,
            city=settings.city,
            postal_code=settings.postal_code,
            country=settings.country,
        ),
    )


@dataclass(frozen=True)
class Signer:
    certificate: str
    private_key_pem: str

    @classmethod
    def for_device(cls, device, *, compliance=False):
        """Compliance checks always sign with the compliance certificate;
        issuing signs with device.signing_cert (see environments)."""
        return cls(device.compliance_cert if compliance else device.signing_cert, device.get_secret("private_key"))

    def sign(self, xml):
        return signing.sign(xml, certificate=self.certificate, private_key_pem=self.private_key_pem)


def issue(branch, *, kind, simplified, vat, names, payment_means=(ubl.PaymentMeans.CASH,), buyer=None,
          original=None, reason="", source="pos", issued_at=None):
    settings = ZatcaSettings.for_branch(branch)
    with transaction.atomic():
        device = (ZatcaDevice.objects.select_for_update()
                  .filter(branch=branch, is_active=True, environment=settings.environment).first())
        if device is None or device.status != DeviceStatus.ACTIVE:
            raise IssueError(f"{branch} has no active ZATCA device in {settings.get_environment_display()}.")
        sequence = _SEQUENCES[kind]
        seq = getattr(device, sequence) + 1
        number = f"{_PREFIXES[kind]}-{device.pk}-{seq:06d}"
        icv, pih = device.last_icv + 1, device.last_hash
        doc = ubl.Document(
            kind=kind, simplified=simplified, number=number, issued_at=issued_at or timezone.now(),
            icv=icv, pih=pih, seller=seller_party(settings), vat=vat, names=names,
            payment_means=tuple(payment_means), buyer=buyer,
            billing_reference=original.number if original else "", reason=reason, timezone=settings.timezone,
        )
        signed = Signer.for_device(device).sign(ubl.build(doc))
        document = ZatcaDocument.objects.create(
            branch=branch, device=device, environment=device.environment, kind=kind, simplified=simplified,
            number=number, uuid=doc.uuid, icv=icv, pih=pih, invoice_hash=signed.invoice_hash,
            issued_at=doc.issued_at, total=vat.payable, vat_total=vat.vat_total, original=original,
            reason=reason, source=source, signed_xml=signed.xml, qr=signed.qr,
        )
        setattr(device, sequence, seq)
        device.last_icv, device.last_hash = icv, signed.invoice_hash
        device.save(update_fields=[sequence, "last_icv", "last_hash", "updated_at"])
    return document


def submit(document, client=None):
    """Send one pending document; returns it updated. Safe to call again."""
    if document.is_final:
        return document
    device = document.device
    client = client or ZatcaClient(device.environment)
    signed = signing.SignedInvoice(xml=document.signed_xml, invoice_hash=document.invoice_hash, signature="",
                                   qr=document.qr)
    document.attempts += 1
    document.last_attempt_at = timezone.now()
    try:
        auth = device.production_auth()
        if document.simplified:
            response = client.report(signed, document.uuid, auth)
        else:
            response = client.clear(signed, document.uuid, auth)
    except (ZatcaUnavailable, ZatcaAuthError) as exc:
        document.last_error = str(exc)
        document.save(update_fields=["attempts", "last_attempt_at", "last_error"])
        return document

    document.http_status = response.http_status
    document.zatca_status = response.status
    document.warnings = response.warnings
    document.errors = response.errors
    document.response = response.summary()
    document.last_error = ""
    if response.reporting_status == "REPORTED" or response.http_status == 409 and document.simplified:
        document.status = DocumentStatus.REPORTED
    elif response.clearance_status == "CLEARED":
        document.status = DocumentStatus.CLEARED
        document.cleared_xml = response.cleared_invoice
        if document.cleared_xml:
            document.qr = qr.embedded(document.cleared_xml) or document.qr
    elif response.http_status == 400:
        document.status = DocumentStatus.REJECTED
    else:
        # e.g. 303 (clearance switched off) or an unexpected body: keep it
        # pending with what ZATCA said, for a person or the retry job.
        document.last_error = f"Unexpected ZATCA answer {response.http_status}"
    if document.status in (DocumentStatus.REPORTED, DocumentStatus.CLEARED):
        document.accepted_at = timezone.now()
    document.save()
    return document
