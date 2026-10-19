"""
Check the Python signer against ZATCA's SDK on real files. Reads only; uses
a throwaway key for its own signing, never the device's.

  python manage.py zatca_sdk_check --samples C:\\zatca\\work\\ZATCA_Samples_fixed
      --signed C:\\zatca\\work\\signed_prod_Simplified_Invoice.xml [--sdk-home C:\\zatca\\sdk]
"""

import base64
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.core.management.base import BaseCommand, CommandError

from zatca import qr, sdk, signing
from zatca.hashing import NS, invoice_hash, parse


class Command(BaseCommand):
    help = "Compare the Python invoice hash, signature, SignedProperties digest and QR with ZATCA's SDK."

    def add_arguments(self, parser):
        parser.add_argument("--samples", help="Folder of unsigned sample invoices to hash with both.")
        parser.add_argument("--signed", nargs="*", default=[], help="SDK-signed invoices to verify and rebuild the QR of.")
        parser.add_argument("--sdk-home", default="", help="SDK folder; defaults to ZATCA_SDK_HOME.")

    def handle(self, *args, **options):
        failures = 0
        if options["samples"]:
            failures += self.check_samples(Path(options["samples"]), options["sdk_home"])
        for path in options["signed"]:
            failures += self.check_signed(Path(path))
        if failures:
            raise CommandError(f"{failures} check(s) failed.")
        self.stdout.write(self.style.SUCCESS("All checks passed."))

    def check_samples(self, folder, sdk_home):
        files = sorted(folder.glob("*.xml"))
        if not files:
            raise CommandError(f"No .xml files in {folder}.")
        key, cert_b64 = _throwaway_signer()
        failures = 0
        self.stdout.write(f"Invoice hash, Python vs SDK -generateHash ({len(files)} files):")
        with tempfile.TemporaryDirectory() as scratch:
            for path in files:
                ours = invoice_hash(path.read_bytes())
                theirs = sdk.generate_hash(path, sdk_home)
                # Re-sign in Python (throwaway key): the hash must not move,
                # and the SDK must hash the Python-signed file the same way.
                signed = signing.sign(path.read_bytes(), certificate=cert_b64, private_key_pem=key)
                signed_path = Path(scratch) / f"py_signed_{path.name}"
                signed_path.write_text(signed.xml, encoding="utf-8")
                theirs_signed = sdk.generate_hash(signed_path, sdk_home)
                ok = ours == theirs == signed.invoice_hash == theirs_signed and signing.verify(signed.xml)
                failures += not ok
                self.stdout.write(f"  {'OK  ' if ok else 'FAIL'} {path.name}: python {ours}  sdk {theirs}"
                                  + ("" if ok else f"  (python-signed: sdk {theirs_signed})"))
        return failures

    def check_signed(self, path):
        xml = path.read_text(encoding="utf-8")
        root = parse(xml)
        failures = 0
        self.stdout.write(f"{path.name}:")

        ok = signing.verify(xml)
        failures += not ok
        self.stdout.write(f"  {'OK  ' if ok else 'FAIL'} signature verifies over the invoice hash")

        # Rebuild SignedProperties from the template with the file's own
        # values; its digest must equal the one the SDK put in SignedInfo.
        text = lambda p: root.find(p, NS).text.strip()
        cert_b64 = "".join(text(".//ds:X509Certificate").split())
        xades = {"xades": signing.XADES_NS, **NS}
        rebuilt = signing._SIGNED_PROPERTIES.format(
            signing_time=root.find(".//xades:SigningTime", xades).text.strip(),
            cert_digest=signing.hex_digest_b64(cert_b64.encode("ascii")),
            issuer=root.find(".//ds:X509IssuerName", NS).text.strip(),
            serial=root.find(".//ds:X509SerialNumber", NS).text.strip(),
        )
        in_file = re.search(r"<xades:SignedProperties.*?</xades:SignedProperties>", xml, re.S).group(0)
        expected = text(".//ds:Reference[@URI='#xadesSignedProperties']/ds:DigestValue")
        ok = rebuilt == in_file and signing.signed_properties_digest(rebuilt) == expected
        failures += not ok
        self.stdout.write(f"  {'OK  ' if ok else 'FAIL'} SignedProperties digest {signing.signed_properties_digest(rebuilt)} (file {expected})")
        ok = signing.issuer_name(signing.certs.load(cert_b64)) == root.find(".//ds:X509IssuerName", NS).text.strip()
        failures += not ok
        self.stdout.write(f"  {'OK  ' if ok else 'FAIL'} X509IssuerName format")

        rebuilt_qr, embedded_qr = qr.from_signed_xml(xml), qr.embedded(xml)
        ok = rebuilt_qr == embedded_qr
        failures += not ok
        self.stdout.write(f"  {'OK  ' if ok else 'FAIL'} rebuilt QR equals the QR in the file ({len(rebuilt_qr)} chars)")
        return failures


def _throwaway_signer():
    key = ec.generate_private_key(ec.SECP256K1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "zatca_sdk_check")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1792393316834).not_valid_before(now).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode("ascii")
    return pem, base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")
