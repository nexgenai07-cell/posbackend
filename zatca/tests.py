import base64
import io
import json
import random
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch

from . import certs, crypto
from .environments import Environment, profile
from .importer import CredentialImportError, import_sandbox_credentials
from .models import INITIAL_PIH, ZatcaAuditLog, ZatcaDevice, ZatcaSettings
from .vat import SaleLine, VatCategory, calculate, vat_on

D = Decimal


def check_invariants(test, sale_lines, result, rate=D("15")):
    """Every arithmetic rule ZATCA applies to the totals and lines."""
    for line in result.lines:
        test.assertEqual(line.unit_net * line.quantity, line.net, line)
        test.assertEqual(line.vat, (line.net * line.rate / 100).quantize(D("0.01"), rounding="ROUND_HALF_UP"), line)
        test.assertEqual(line.total, line.net + line.vat, line)
        test.assertGreaterEqual(line.unit_net, 0)
    test.assertEqual(sum((line.net for line in result.lines), D("0")), result.net_total)
    for category in result.categories:
        test.assertEqual(category.vat * 100, vat_on(int(category.taxable * 100), category.rate))
    test.assertEqual(sum((c.vat for c in result.categories), D("0")), result.vat_total)
    test.assertEqual(result.tax_inclusive, result.net_total + result.vat_total)
    test.assertEqual(result.payable, result.tax_inclusive + result.payable_rounding)
    test.assertLessEqual(abs(result.payable_rounding), D("0.01"))
    gross = sum((line.unit_gross * line.quantity for line in sale_lines), D("0"))
    test.assertEqual(result.payable, gross, "payable must equal what the customer was charged")
    # Each sale line's units are all accounted for, at most one split.
    for sale in sale_lines:
        parts = [line for line in result.lines if line.key == sale.key]
        test.assertEqual(sum(part.quantity for part in parts), sale.quantity)
        test.assertLessEqual(len(parts), 2)


class VatEngineTests(SimpleTestCase):
    def calc(self, *lines, **kwargs):
        sale = [SaleLine(i, quantity, D(gross), *rest) for i, (quantity, gross, *rest) in enumerate(lines)]
        result = calculate(sale, standard_rate=kwargs.pop("rate", 15), **kwargs)
        return sale, result

    def test_single_dish(self):
        sale, result = self.calc((1, "62.00"))
        check_invariants(self, sale, result)
        self.assertEqual((result.net_total, result.vat_total, result.payable), (D("53.91"), D("8.09"), D("62.00")))

    def test_three_times_ten_splits_one_unit(self):
        sale, result = self.calc((3, "10.00"))
        check_invariants(self, sale, result)
        self.assertEqual(
            [(line.quantity, line.unit_net) for line in result.lines],
            [(2, D("8.70")), (1, D("8.69"))],
        )
        self.assertEqual((result.net_total, result.vat_total), (D("26.09"), D("3.91")))

    def test_seven_times_13_99(self):
        sale, result = self.calc((7, "13.99"))
        check_invariants(self, sale, result)
        self.assertEqual(result.payable, D("97.93"))

    def test_free_line_never_moves(self):
        sale, result = self.calc((1, "0.00"), (3, "10.00"))
        check_invariants(self, sale, result)
        free = [line for line in result.lines if line.key == 0]
        self.assertEqual([(line.unit_net, line.vat) for line in free], [(D("0.00"), D("0.00"))])

    def test_zero_rated_lines_carry_no_vat(self):
        sale, result = self.calc((2, "5.00", VatCategory.ZERO), (1, "23.00"))
        check_invariants(self, sale, result)
        zero = next(c for c in result.categories if c.category == VatCategory.ZERO)
        self.assertEqual((zero.taxable, zero.vat, zero.rate), (D("10.00"), D("0.00"), D("0")))

    def test_prices_excluding_vat_add_vat_on_top(self):
        sale = [SaleLine(1, 2, D("10.00"))]
        result = calculate(sale, standard_rate=15, prices_include_vat=False)
        self.assertEqual((result.net_total, result.vat_total, result.payable), (D("20.00"), D("3.00"), D("23.00")))

    def test_gap_values_use_payable_rounding(self):
        # 1.11 has no net N with N + round(N x 15%) == 1.11.
        self.assertFalse(any(n + vat_on(n, D(15)) == 111 for n in range(80, 110)))
        sale, result = self.calc((1, "1.11"))
        check_invariants(self, sale, result)
        self.assertNotEqual(result.payable_rounding, 0)

    def test_every_single_price_up_to_300(self):
        for halalas in range(1, 30001):
            sale, result = self.calc((1, D(halalas) / 100))
            self.assertEqual(result.payable, D(halalas) / 100, halalas)
            self.assertLessEqual(abs(result.payable_rounding), D("0.01"))

    def test_random_baskets(self):
        rng = random.Random(20261019)
        for _ in range(2000):
            lines = [(rng.randint(1, 12), D(rng.randint(0, 25000)) / 100) for _ in range(rng.randint(1, 15))]
            if not any(gross for _, gross in lines):
                continue
            sale, result = self.calc(*lines)
            check_invariants(self, sale, result)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            calculate([SaleLine(1, 1, D("1.005"))], standard_rate=15)
        with self.assertRaises(ValueError):
            calculate([SaleLine(1, 0, D("1.00"))], standard_rate=15)
        with self.assertRaises(ValueError):
            calculate([SaleLine(1, 1, D("1.00")), SaleLine(1, 1, D("2.00"))], standard_rate=15)


class EnvironmentTests(SimpleTestCase):
    def test_each_environment_has_its_own_url_and_template(self):
        keys = [key for key, _ in Environment.CHOICES]
        self.assertEqual(len({profile(key).base_url for key in keys}), 3)
        self.assertEqual(
            [profile(key).csr_template for key in keys],
            ["TSTZATCA-Code-Signing", "PREZATCA-Code-Signing", "ZATCA-Code-Signing"],
        )
        self.assertEqual([profile(key).is_live for key in keys], [False, False, True])
        self.assertEqual([profile(key).is_test for key in keys], [True, True, False])
        self.assertEqual([profile(key).two_vat_fallback for key in keys], [True, False, False])

    def test_unknown_environment(self):
        with self.assertRaises(ValueError):
            profile("staging")


def make_staff(branch, name, role):
    staff = Staff(branch=branch, name=name, role=role)
    staff.set_pin("1234")
    staff.save()
    return staff


class ZatcaSettingsApiTests(TestCase):
    url = "/api/zatca/settings/"

    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر", timezone="Asia/Riyadh", currency="SAR")
        cls.other = Branch.objects.create(name_en="Other", name_ar="آخر")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)
        cls.manager = make_staff(cls.branch, "Manager", StaffRole.MANAGER)
        cls.cashier = make_staff(cls.branch, "Cashier", StaffRole.CASHIER)
        cls.other_owner = make_staff(cls.other, "Other owner", StaffRole.OWNER)

    def as_staff(self, staff):
        client = APIClient()
        client.force_authenticate(user=staff)
        return client

    def patch(self, data, staff=None):
        return self.as_staff(staff or self.owner).patch(self.url, data, format="json")

    def test_first_visit_prefills_sandbox_identity_and_branch_name(self):
        response = self.as_staff(self.owner).get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(
            (data["environment"], data["is_test"], data["is_live"], data["enabled"]),
            ("sandbox", True, False, False),
        )
        self.assertEqual(
            (data["vat_number"], data["other_id"], data["building_no"], data["additional_no"], data["postal_code"]),
            ("399999999900003", "1010010000", "2322", "1234", "12222"),
        )
        self.assertEqual((data["seller_name_en"], data["seller_name_ar"], data["org_unit"]), ("Burger House", "بيت البرجر", "Burger House"))
        self.assertEqual((data["invoice_types"], data["default_vat_rate"], data["prices_include_vat"]), ("1100", "15.00", True))
        self.assertEqual(data["locked_fields"], [])

    def test_manager_reads_but_cannot_edit(self):
        self.assertEqual(self.as_staff(self.manager).get(self.url).status_code, status.HTTP_200_OK)
        response = self.patch({"city": "Jeddah"}, staff=self.manager)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cashier_cannot_read(self):
        self.assertEqual(self.as_staff(self.cashier).get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_edits_and_next_read_shows_it(self):
        response = self.patch({"seller_name_en": " New Name ", "city": "Jeddah", "building_no": "1111"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        settings = ZatcaSettings.objects.get(branch=self.branch)
        self.assertEqual((settings.seller_name_en, settings.city, settings.building_no), ("New Name", "Jeddah", "1111"))

    def test_branches_are_separate(self):
        self.patch({"city": "Jeddah"})
        data = self.as_staff(self.other_owner).get(self.url).data
        self.assertEqual((data["seller_name_en"], data["city"]), ("Other", "Riyadh"))

    def test_environment_and_country_cannot_be_written(self):
        self.patch({"environment": "production", "country": "AE"})
        settings = ZatcaSettings.objects.get(branch=self.branch)
        self.assertEqual((settings.environment, settings.country), ("sandbox", "SA"))

    def test_validation_codes(self):
        cases = {
            "vat_number": ("123456789012345", "error.zatcaVatNumberInvalid"),
            "building_no": ("12", "error.zatcaBuildingNoInvalid"),
            "additional_no": ("12a4", "error.zatcaAdditionalNoInvalid"),
            "postal_code": ("1234", "error.zatcaPostalCodeInvalid"),
            "other_id": ("12345", "error.zatcaCrnInvalid"),
            "city": ("  ", "error.zatcaFieldRequired"),
            "seller_name_ar": ("", "error.zatcaFieldRequired"),
            "invoice_types": ("1111", "error.zatcaChoiceInvalid"),
            "default_vat_rate": ("150", "error.zatcaVatRateInvalid"),
        }
        for field, (value, code) in cases.items():
            with self.subTest(field=field):
                response = self.patch({field: value})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data[field], [code])

    def test_blank_additional_number_is_allowed(self):
        self.assertEqual(self.patch({"additional_no": ""}).status_code, status.HTTP_200_OK)

    def test_non_crn_scheme_accepts_other_lengths(self):
        response = self.patch({"other_id_scheme": "MOM", "other_id": "A12345"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_vat_group_needs_tin_as_org_unit(self):
        group_vat = "300000000010003"  # 11th digit 1
        response = self.patch({"vat_number": group_vat})
        self.assertEqual(response.data["org_unit"], ["error.zatcaVatGroupOrgUnit"])
        response = self.patch({"vat_number": group_vat, "org_unit": "3000000000"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["is_vat_group"])

    def test_identity_locked_while_a_live_device_exists(self):
        self.as_staff(self.owner).get(self.url)
        with mock.patch.object(ZatcaSettings, "has_live_device", return_value=True):
            response = self.patch({"vat_number": "310000000000003", "city": "Jeddah"})
            self.assertEqual(response.data["vat_number"], ["error.zatcaFieldLocked"])
            # Unlocked fields still save, and resending the same value is fine.
            response = self.patch({"vat_number": "399999999900003", "city": "Jeddah"})
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assertEqual(response.data["locked_fields"], ["vat_number", "other_id", "other_id_scheme"])


# -- milestone 2: devices, encryption, import -------------------------------

TEST_KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()


def make_cert(private_key, vat, serial="1-TST|2-TST|3-test-uuid", days=365):
    """A self-signed certificate shaped like ZATCA's: VAT in UID, EGS serial in SN."""
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"TST-886431145-{vat}"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "SA"),
    ])
    directory = x509.Name([
        x509.NameAttribute(NameOID.SURNAME, serial),
        x509.NameAttribute(ObjectIdentifier("0.9.2342.19200300.100.1.1"), vat),
        x509.NameAttribute(NameOID.TITLE, "1100"),
        x509.NameAttribute(ObjectIdentifier("2.5.4.26"), "Riyadh"),
    ])
    now = datetime.now(dt_timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DirectoryName(directory)]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


def pem_of(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption(),
    ).decode()


def token_for(cert_b64):
    return base64.b64encode(cert_b64.encode()).decode()


def write_work_folder(folder):
    """Files shaped like C:\\zatca\\work after a manual sandbox onboarding."""
    key = ec.generate_private_key(ec.SECP256K1())
    # The sandbox's demo production certificate belongs to a different key.
    other = ec.generate_private_key(ec.SECP256K1())
    pem = pem_of(key)
    (folder / "my_private_key.pem").write_text(pem)
    secrets = {
        "compliance_token": token_for(make_cert(key, "312345678900003")),
        "compliance_secret": "compliance-secret-value-XYZ",
        "production_token": token_for(make_cert(other, "399999999900003", days=10)),
        "production_secret": "production-secret-value-XYZ",
    }
    (folder / "compliance_csid.json").write_text(json.dumps({
        "requestID": 1234567890123, "binarySecurityToken": secrets["compliance_token"],
        "secret": secrets["compliance_secret"],
    }))
    (folder / "production_csid.json").write_text(json.dumps({
        "requestID": 30368, "binarySecurityToken": secrets["production_token"],
        "secret": secrets["production_secret"],
    }))
    return pem, secrets


@override_settings(ZATCA_ENCRYPTION_KEY=TEST_KEY)
class CryptoTests(SimpleTestCase):
    def test_round_trip_and_ciphertext_hides_value(self):
        token = crypto.encrypt("super secret")
        self.assertNotIn("super secret", token)
        self.assertEqual(crypto.decrypt(token), "super secret")
        self.assertEqual((crypto.encrypt(""), crypto.decrypt("")), ("", ""))

    def test_rotation_keeps_old_secrets_readable(self):
        with override_settings(ZATCA_ENCRYPTION_KEY=OTHER_KEY):
            old = crypto.encrypt("value")
        with override_settings(ZATCA_ENCRYPTION_KEY=f"{TEST_KEY},{OTHER_KEY}"):
            self.assertEqual(crypto.decrypt(old), "value")

    def test_wrong_or_missing_key(self):
        token = crypto.encrypt("value")
        with override_settings(ZATCA_ENCRYPTION_KEY=OTHER_KEY):
            with self.assertRaises(crypto.EncryptionKeyMismatch):
                crypto.decrypt(token)
        with override_settings(ZATCA_ENCRYPTION_KEY=""):
            self.assertEqual(crypto.encryption_status(), (False, "missing"))
            with self.assertRaises(crypto.EncryptionNotConfigured):
                crypto.encrypt("value")
        with override_settings(ZATCA_ENCRYPTION_KEY="not-a-key"):
            self.assertEqual(crypto.encryption_status(), (False, "invalid"))


@override_settings(ZATCA_ENCRYPTION_KEY=TEST_KEY)
class ImportAndDeviceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر")
        cls.owner = make_staff(cls.branch, "Owner", StaffRole.OWNER)
        cls.manager = make_staff(cls.branch, "Manager", StaffRole.MANAGER)
        cls.cashier = make_staff(cls.branch, "Cashier", StaffRole.CASHIER)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.pem, self.secrets = write_work_folder(self.folder)

    def tearDown(self):
        self.tmp.cleanup()

    def client_for(self, staff):
        client = APIClient()
        client.force_authenticate(user=staff)
        return client

    def test_import_stores_everything_encrypted(self):
        result = import_sandbox_credentials(self.branch, self.folder)
        device = ZatcaDevice.objects.get(pk=result.device.pk)
        self.assertEqual((device.environment, device.status, device.is_active), ("sandbox", "active", True))
        self.assertEqual((device.last_icv, device.last_hash), (0, INITIAL_PIH))
        self.assertEqual(device.egs_serial, "1-TST|2-TST|3-test-uuid")
        self.assertEqual(device.get_secret("private_key"), certs.normalize_private_key(self.pem))
        self.assertEqual(device.get_secret("compliance_secret"), "compliance-secret-value-XYZ")
        self.assertEqual(device.get_secret("compliance_request_id"), "1234567890123")
        self.assertEqual(device.get_secret("production_token"), self.secrets["production_token"])
        stored = " ".join(getattr(device, f"{name}_enc") for name in ZatcaDevice.SECRETS)
        for value in (*self.secrets.values(), "PRIVATE KEY", "1234567890123"):
            self.assertNotIn(value, stored)
        self.assertEqual(device.signing_cert, device.compliance_cert)  # the sandbox quirk
        # The compliance VAT differs from the settings VAT: expected in Sandbox, so a note.
        self.assertEqual(len(result.warnings), 1)
        log = ZatcaAuditLog.objects.get()
        self.assertEqual((log.action, log.ok, log.device_id), ("import_credentials", True, device.pk))

    def test_reads_the_sdks_mislabelled_and_headerless_keys(self):
        key = ec.generate_private_key(ec.SECP256K1())
        pkcs8 = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        body = base64.b64encode(pkcs8).decode()
        mislabelled = f"-----BEGIN EC PRIVATE KEY-----\r\n{body}\r\n-----END EC PRIVATE KEY-----\r\n"
        expected = certs.normalize_private_key(pem_of(key))
        self.assertEqual(certs.normalize_private_key(mislabelled), expected)
        self.assertEqual(certs.normalize_private_key(body), expected)
        with self.assertRaises(ValueError):
            certs.normalize_private_key("not a key")

    def test_refuses_a_key_that_doesnt_match(self):
        (self.folder / "my_private_key.pem").write_text(pem_of(ec.generate_private_key(ec.SECP256K1())))
        with self.assertRaisesMessage(CredentialImportError, "not the key"):
            import_sandbox_credentials(self.branch, self.folder)
        self.assertFalse(ZatcaDevice.objects.exists())
        self.assertFalse(ZatcaAuditLog.objects.get().ok)

    def test_refuses_missing_files_and_key(self):
        with override_settings(ZATCA_ENCRYPTION_KEY=""):
            with self.assertRaisesMessage(CredentialImportError, "ZATCA_ENCRYPTION_KEY is not set"):
                import_sandbox_credentials(self.branch, self.folder)
        (self.folder / "production_csid.json").unlink()
        with self.assertRaisesMessage(CredentialImportError, "production_csid.json wasn't found"):
            import_sandbox_credentials(self.branch, self.folder)

    def test_only_sandbox_can_import(self):
        settings = ZatcaSettings.for_branch(self.branch)
        ZatcaSettings.objects.filter(pk=settings.pk).update(environment=Environment.SIMULATION)
        with self.assertRaisesMessage(CredentialImportError, "sandbox credentials only"):
            import_sandbox_credentials(self.branch, self.folder)

    def test_replace_archives_the_old_device(self):
        first = import_sandbox_credentials(self.branch, self.folder).device
        with self.assertRaisesMessage(CredentialImportError, "--replace"):
            import_sandbox_credentials(self.branch, self.folder)
        second = import_sandbox_credentials(self.branch, self.folder, replace=True)
        first.refresh_from_db()
        self.assertEqual((first.is_active, second.replaced.pk), (False, first.pk))
        self.assertIsNotNone(first.archived_at)
        self.assertEqual(ZatcaDevice.objects.filter(is_active=True).count(), 1)

    def test_command_prints_no_secret(self):
        out = io.StringIO()
        call_command("zatca_import", branch=self.branch.pk, dir=str(self.folder), stdout=out)
        text = out.getvalue()
        self.assertIn("Imported into device", text)
        for value in (*self.secrets.values(), "PRIVATE KEY"):
            self.assertNotIn(value, text)

    def test_device_api_shows_status_but_never_a_secret(self):
        import_sandbox_credentials(self.branch, self.folder)
        response = self.client_for(self.manager).get("/api/zatca/devices/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = json.dumps(response.json())
        for value in (*self.secrets.values(), "PRIVATE KEY", "1234567890123", "_enc"):
            self.assertNotIn(value, body)
        data = response.json()
        self.assertEqual(data["encryption"], {"configured": True, "problem": ""})
        device = data["devices"][0]
        self.assertTrue(all(item["set"] and item["set_at"] for item in device["credentials"]))
        self.assertEqual(device["signing_certificate"], "compliance")
        self.assertEqual(device["certificates"]["production"]["vat_number"], "399999999900003")
        # This fixture's production certificate expires in 10 days.
        self.assertIn("certExpiringSoon", [warning["code"] for warning in device["warnings"]])
        self.assertEqual(self.client_for(self.cashier).get("/api/zatca/devices/").status_code, status.HTTP_403_FORBIDDEN)

    def test_vat_mismatch_warning(self):
        import_sandbox_credentials(self.branch, self.folder)
        self.client_for(self.owner).patch("/api/zatca/settings/", {"vat_number": "310000000000003"}, format="json")
        device = self.client_for(self.owner).get("/api/zatca/devices/").json()["devices"][0]
        mismatch = [warning for warning in device["warnings"] if warning["code"] == "vatMismatch"]
        self.assertEqual(mismatch[0]["certificate_vat"], "399999999900003")

    def test_live_device_locks_identity(self):
        settings = ZatcaSettings.for_branch(self.branch)
        ZatcaDevice.objects.create(branch=self.branch, environment=Environment.SANDBOX, status="active")
        self.assertEqual(settings.locked_fields(), [])  # Sandbox never locks
        ZatcaDevice.objects.update(is_active=False)
        ZatcaDevice.objects.create(branch=self.branch, environment=Environment.SIMULATION, status="active")
        self.assertEqual(settings.locked_fields(), ["vat_number", "other_id", "other_id_scheme"])
        response = self.client_for(self.owner).patch("/api/zatca/settings/", {"vat_number": "310000000000003"}, format="json")
        self.assertEqual(response.data["vat_number"], ["error.zatcaFieldLocked"])

    def test_wrong_key_blocks_secrets_but_not_the_status_page(self):
        import_sandbox_credentials(self.branch, self.folder)
        with override_settings(ZATCA_ENCRYPTION_KEY=OTHER_KEY):
            with self.assertRaises(crypto.EncryptionKeyMismatch):
                ZatcaDevice.objects.get().get_secret("private_key")
            self.assertEqual(self.client_for(self.owner).get("/api/zatca/devices/").status_code, status.HTTP_200_OK)


# -- milestone 3: build, sign, QR, send, onboard ----------------------------

import re  # noqa: E402

import requests  # noqa: E402

from . import compliance, documents, hashing, qr, signing, ubl  # noqa: E402
from .client import ZatcaClient  # noqa: E402
from .models import DeviceStatus, DocumentStatus, ZatcaDocument  # noqa: E402
from .onboarding import OnboardingError, activate_device, onboard  # noqa: E402

UID = ObjectIdentifier("0.9.2342.19200300.100.1.1")
_QR_VALUE = re.compile(r"(<cbc:ID>QR</cbc:ID>\s*<cac:Attachment>\s*<cbc:EmbeddedDocumentBinaryObject[^>]*>)[^<]*")


class FakeResponse:
    def __init__(self, status_code, data):
        self.status_code, self._data, self.text = status_code, data, json.dumps(data)

    def json(self):
        return self._data


def issue_cert(public_key, vat, serial="1-NexgenPOS|2-1.0|3-x"):
    """A certificate from a fake ZATCA CA for `public_key`."""
    ca = ec.generate_private_key(ec.SECP256K1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"EGS-{vat}")])
    directory = x509.Name([x509.NameAttribute(NameOID.SURNAME, serial), x509.NameAttribute(UID, vat)])
    now = datetime.now(dt_timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject)
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "eInvoicing")]))
        .public_key(public_key).serial_number(1792393316834)
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DirectoryName(directory)]), critical=False)
        .sign(ca, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


class FakeZatca:
    """Stands in for requests.Session against ZATCA. Checks every document's
    signature and hash like the real API, and records what it was sent."""

    def __init__(self, *, sandbox_demo_cert=False, compliance_vat="", reject_checks=False, reject_reports=False):
        self.sandbox_demo_cert = sandbox_demo_cert
        self.compliance_vat = compliance_vat
        self.reject_checks, self.reject_reports, self.outage = reject_checks, reject_reports, False
        self.calls, self.documents, self.csrs = [], [], []

    def post(self, url, json=None, headers=None, timeout=None, allow_redirects=True):
        if self.outage:
            raise requests.ConnectionError("no route")
        self.calls.append({"url": url, "headers": headers, "json": json})
        if url.endswith("/compliance"):
            csr = x509.load_pem_x509_csr(base64.b64decode(json["csr"]))
            self.csrs.append(csr)
            san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            vat = self.compliance_vat or san.get_values_for_type(x509.DirectoryName)[0].get_attributes_for_oid(UID)[0].value
            return FakeResponse(200, {"requestID": 1234567890123, "dispositionMessage": "ISSUED",
                                      "binarySecurityToken": token_for(issue_cert(csr.public_key(), vat)),
                                      "secret": "compliance-secret-FAKE"})
        if url.endswith("/production/csids"):
            # The real sandbox hands out a fixed demo certificate for another key.
            if self.sandbox_demo_cert:
                key = ec.generate_private_key(ec.SECP256K1()).public_key()
            else:
                key = self.csrs[-1].public_key()
            return FakeResponse(200, {"requestID": 30368, "secret": "production-secret-FAKE",
                                      "binarySecurityToken": token_for(issue_cert(key, "399999999900003"))})
        xml = base64.b64decode(json["invoice"]).decode("utf-8")
        self.documents.append(xml)
        simplified = 'name="02' in xml
        valid = signing.verify(xml) and hashing.invoice_hash(xml) == json["invoiceHash"]
        rejected = (not valid or (self.reject_checks and url.endswith("/compliance/invoices"))
                    or (self.reject_reports and "/invoices/" in url and "/compliance/" not in url))
        body = {"validationResults": {
            "status": "ERROR" if rejected else "PASS", "warningMessages": [],
            "errorMessages": [{"code": "BR-KSA-TEST", "category": "KSA", "message": "rejected"}] if rejected else []}}
        if simplified:
            body["reportingStatus"] = "NOT_REPORTED" if rejected else "REPORTED"
        else:
            body["clearanceStatus"] = "NOT_CLEARED" if rejected else "CLEARED"
            if not rejected and url.endswith("/clearance/single"):
                cleared = _QR_VALUE.sub(lambda m: m.group(1) + "Q0xFQVJFRA==", xml)
                body["clearedInvoice"] = base64.b64encode(cleared.encode("utf-8")).decode()
        return FakeResponse(400 if rejected else 200, body)

    def auth_of(self, call):
        return base64.b64decode(call["headers"]["Authorization"].split()[1]).decode()


def seller_vats(fake):
    return [re.search(r"<cac:AccountingSupplierParty>.*?<cbc:CompanyID>(\d+)</cbc:CompanyID>", xml, re.S).group(1)
            for xml in fake.documents]


@override_settings(ZATCA_ENCRYPTION_KEY=TEST_KEY)
class OnboardingTests(TestCase):
    def make_branch(self, environment=Environment.SANDBOX):
        branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر")
        ZatcaSettings.for_branch(branch)
        ZatcaSettings.objects.filter(branch=branch).update(environment=environment)
        return branch

    def run_onboard(self, branch, environment, fake, **kwargs):
        otp = kwargs.pop("otp", "" if environment == Environment.SANDBOX else "654321")
        return onboard(branch, otp=otp, environment=environment, client=ZatcaClient(environment, session=fake),
                       actor_label="test", **kwargs)

    def test_onboards_in_every_environment_against_its_own_api(self):
        for environment in (Environment.SANDBOX, Environment.SIMULATION, Environment.PRODUCTION):
            with self.subTest(environment=environment):
                branch = self.make_branch(environment)
                fake = FakeZatca(sandbox_demo_cert=environment == Environment.SANDBOX)
                result = self.run_onboard(branch, environment, fake, activate=True)
                env = profile(environment)
                device = ZatcaDevice.objects.get(pk=result.device.pk)

                self.assertTrue(all(call["url"].startswith(env.base_url + "/") for call in fake.calls))
                template = fake.csrs[0].extensions.get_extension_for_oid(ObjectIdentifier("1.3.6.1.4.1.311.20.2"))
                self.assertEqual(template.value.value[2:].decode(), env.csr_template)
                self.assertEqual(fake.calls[0]["headers"]["OTP"], env.default_otp or "654321")
                self.assertEqual(fake.csrs[0].public_key().curve.name, "secp256k1")

                # 6 compliance documents (1100), all accepted, then the production CSID.
                self.assertEqual(len(result.checks), 6)
                self.assertTrue(all(check.passed for check in result.checks))
                self.assertTrue(fake.calls[-1]["url"].endswith("/production/csids"))
                self.assertEqual(fake.auth_of(fake.calls[-1]).split(":")[1], "compliance-secret-FAKE")

                self.assertEqual((device.status, device.is_active, device.environment),
                                 (DeviceStatus.ACTIVE, True, environment))
                self.assertEqual((device.last_icv, device.last_hash), (0, INITIAL_PIH))
                self.assertTrue(all(device.has_secret(name) for name in ZatcaDevice.SECRETS))
                self.assertTrue(certs.key_matches_cert(device.get_secret("private_key"), device.compliance_cert))
                # Only the sandbox signs with the compliance certificate.
                expected = device.compliance_cert if environment == Environment.SANDBOX else device.production_cert
                self.assertEqual(device.signing_cert, expected)
                self.assertTrue(certs.key_matches_cert(device.get_secret("private_key"), device.signing_cert))

    def test_two_vat_fallback_only_in_sandbox(self):
        for environment, expected in ((Environment.SANDBOX, "312345678900003"),
                                      (Environment.SIMULATION, "399999999900003")):
            with self.subTest(environment=environment):
                fake = FakeZatca(compliance_vat="312345678900003")
                self.run_onboard(self.make_branch(environment), environment, fake)
                self.assertEqual(set(seller_vats(fake)), {expected})

    def test_without_replace_the_current_device_is_untouched(self):
        branch = self.make_branch()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        write_work_folder(Path(tmp.name))
        imported = import_sandbox_credentials(branch, tmp.name).device
        before = ZatcaDevice.objects.filter(pk=imported.pk).values().get()

        new = self.run_onboard(branch, Environment.SANDBOX, FakeZatca(sandbox_demo_cert=True)).device
        new.refresh_from_db()
        self.assertEqual((new.status, new.is_active, new.archived_at), (DeviceStatus.READY, False, None))
        self.assertEqual(ZatcaDevice.objects.filter(pk=imported.pk).values().get(), before)
        self.assertEqual(ZatcaSettings.for_branch(branch).active_device(), imported)

        self.assertEqual(activate_device(new), imported)
        imported.refresh_from_db()
        self.assertFalse(imported.is_active)
        self.assertIsNotNone(imported.archived_at)
        self.assertEqual(ZatcaSettings.for_branch(branch).active_device(), new)

    def test_failed_compliance_check_stops_before_production(self):
        fake = FakeZatca(reject_checks=True)
        with self.assertRaises(OnboardingError) as caught:
            self.run_onboard(self.make_branch(), Environment.SANDBOX, fake, activate=True)
        device = caught.exception.device
        device.refresh_from_db()
        self.assertEqual((device.status, device.is_active), (DeviceStatus.NEEDS_ATTENTION, False))
        self.assertFalse(any(call["url"].endswith("/production/csids") for call in fake.calls))
        self.assertEqual(caught.exception.checks[0].errors[0]["code"], "BR-KSA-TEST")
        self.assertFalse(ZatcaAuditLog.objects.get(action="compliance_checks").ok)

    def test_live_environments_need_a_real_otp(self):
        with self.assertRaises(OnboardingError):
            self.run_onboard(self.make_branch(Environment.SIMULATION), Environment.SIMULATION, FakeZatca(), otp="")
        self.assertFalse(ZatcaDevice.objects.exists())

    def test_command_onboards_and_prints_no_secret(self):
        branch = self.make_branch()
        fake = FakeZatca(sandbox_demo_cert=True)
        out = io.StringIO()
        with mock.patch("zatca.onboarding.ZatcaClient", lambda environment: ZatcaClient(environment, session=fake)):
            call_command("zatca_onboard", "--branch", str(branch.pk), stdout=out)
        text = out.getvalue()
        self.assertIn("onboarded", text)
        self.assertIn("Not in use", text)
        device = ZatcaDevice.objects.get()
        for secret in ("compliance-secret-FAKE", "production-secret-FAKE", device.get_secret("compliance_token"),
                       device.get_secret("private_key").splitlines()[1]):
            self.assertNotIn(secret, text)
            self.assertFalse(ZatcaAuditLog.objects.filter(summary__contains=secret).exists())

    def test_production_command_needs_confirmation(self):
        branch = self.make_branch(Environment.PRODUCTION)
        with self.assertRaisesMessage(Exception, "--confirm-production"):
            call_command("zatca_onboard", "--branch", str(branch.pk), "--otp", "123456", stdout=io.StringIO())


@override_settings(ZATCA_ENCRYPTION_KEY=TEST_KEY)
class IssueAndSubmitTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name_en="Burger House", name_ar="بيت البرجر")
        ZatcaSettings.for_branch(self.branch)
        self.fake = FakeZatca(sandbox_demo_cert=True)
        self.client = ZatcaClient(Environment.SANDBOX, session=self.fake)
        onboard(self.branch, otp="", client=self.client, activate=True)
        self.device = ZatcaDevice.objects.get(is_active=True)

    def issue(self, sample=compliance.SPLIT_10, simplified=True, **kwargs):
        totals, names = compliance.sale(sample[1], D("15.00"))
        return documents.issue(self.branch, kind=kwargs.pop("kind", ubl.DocumentKind.INVOICE), simplified=simplified,
                               vat=totals, names=names, buyer=None if simplified else compliance.SAMPLE_BUYER, **kwargs)

    def test_chain_numbers_and_signature(self):
        first, second = self.issue(), self.issue(compliance.SPLIT_13_99)
        self.assertEqual((first.icv, first.pih, first.number), (1, INITIAL_PIH, f"INV-{self.device.pk}-000001"))
        self.assertEqual((second.icv, second.pih), (2, first.invoice_hash))
        self.device.refresh_from_db()
        self.assertEqual((self.device.last_icv, self.device.last_hash, self.device.invoice_seq),
                         (2, second.invoice_hash, 2))
        for document in (first, second):
            self.assertTrue(signing.verify(document.signed_xml))
            self.assertEqual(hashing.invoice_hash(document.signed_xml), document.invoice_hash)
            self.assertIn(f"<cbc:UUID>{document.icv}</cbc:UUID>", document.signed_xml)
            self.assertIn(f">{document.pih}</cbc:EmbeddedDocumentBinaryObject>", document.signed_xml)

    def test_split_lines_and_rounding_in_the_xml(self):
        xml = self.issue().signed_xml
        prices = re.findall(r'unitCode="PCE">(\d+)</cbc:InvoicedQuantity>.*?<cbc:PriceAmount currencyID="SAR">([\d.]+)<',
                            xml, re.S)
        self.assertEqual(prices, [("2", "8.70"), ("1", "8.69")])
        self.assertIn('<cbc:PayableAmount currencyID="SAR">30.00</cbc:PayableAmount>', xml)
        self.assertNotIn("PayableRoundingAmount", xml)
        rounded = self.issue(compliance.ROUNDING).signed_xml
        self.assertRegex(rounded, r'<cbc:PayableRoundingAmount currencyID="SAR">-?0\.01</cbc:PayableRoundingAmount>')
        self.assertIn('<cbc:PayableAmount currencyID="SAR">1.11</cbc:PayableAmount>', rounded)

    def test_qr_fields(self):
        simplified = self.issue()
        fields = qr.decode(simplified.qr)
        self.assertEqual(fields[1].decode(), "بيت البرجر")
        self.assertEqual(fields[2].decode(), "399999999900003")
        self.assertEqual((fields[4].decode(), fields[5].decode()), ("30.00", "3.91"))
        self.assertEqual(fields[6].decode(), simplified.invoice_hash)
        self.assertIn(9, fields)
        self.assertEqual(qr.from_signed_xml(simplified.signed_xml), simplified.qr)
        self.assertNotIn(9, qr.decode(self.issue(simplified=False).qr))
        # Tag 4 is the amount due, rounding included.
        self.assertEqual(qr.decode(self.issue(compliance.ROUNDING).qr)[4].decode(), "1.11")

    def test_hash_ignores_signature_qr_and_comments_but_not_content(self):
        xml = self.issue().signed_xml
        digest = hashing.invoice_hash(xml)
        self.assertEqual(hashing.invoice_hash(xml.replace("<cbc:ProfileID>", "<!-- note --><cbc:ProfileID>")), digest)
        self.assertEqual(hashing.invoice_hash(re.sub(r"<ds:SignatureValue>[^<]+", "<ds:SignatureValue>AAAA", xml)), digest)
        self.assertEqual(hashing.invoice_hash(_QR_VALUE.sub(lambda m: m.group(1) + "AAAA", xml)), digest)
        self.assertNotEqual(hashing.invoice_hash(xml.replace(">30.00<", ">30.01<", 1)), digest)

    def test_report_and_clear(self):
        reported = documents.submit(self.issue(), self.client)
        self.assertEqual((reported.status, reported.zatca_status, reported.attempts),
                         (DocumentStatus.REPORTED, "PASS", 1))
        self.assertTrue(self.fake.auth_of(self.fake.calls[-1]).endswith(":production-secret-FAKE"))
        self.assertEqual(self.fake.calls[-1]["headers"]["Clearance-Status"], "0")

        cleared = documents.submit(self.issue(compliance.ROUNDING, simplified=False), self.client)
        self.assertEqual(cleared.status, DocumentStatus.CLEARED)
        self.assertEqual(self.fake.calls[-1]["headers"]["Clearance-Status"], "1")
        self.assertIn("Q0xFQVJFRA==", cleared.cleared_xml)
        self.assertEqual(cleared.qr, "Q0xFQVJFRA==")  # the receipt prints ZATCA's QR
        calls = len(self.fake.calls)
        self.assertEqual(documents.submit(cleared, self.client).status, DocumentStatus.CLEARED)
        self.assertEqual(len(self.fake.calls), calls)  # final: not sent again

    def test_unreachable_keeps_it_pending_and_resends_it_unchanged(self):
        document = self.issue()
        self.fake.outage = True
        document = documents.submit(document, self.client)
        self.assertEqual((document.status, document.attempts), (DocumentStatus.PENDING, 1))
        self.assertIn("unreachable", document.last_error)
        self.fake.outage = False
        document = documents.submit(document, self.client)
        self.assertEqual((document.status, document.attempts), (DocumentStatus.REPORTED, 2))
        self.assertEqual(self.fake.documents[-1], ZatcaDocument.objects.get(pk=document.pk).signed_xml)

    def test_rejected_stays_in_the_chain(self):
        self.fake.reject_reports = True
        rejected = documents.submit(self.issue(), self.client)
        self.assertEqual(rejected.status, DocumentStatus.REJECTED)
        self.assertEqual(rejected.errors[0]["code"], "BR-KSA-TEST")
        following = self.issue()
        self.assertEqual((following.icv, following.pih), (rejected.icv + 1, rejected.invoice_hash))

    def test_credit_note_references_the_invoice(self):
        invoice = self.issue()
        note = self.issue(kind=ubl.DocumentKind.CREDIT_NOTE, original=invoice, reason="Order cancelled")
        self.assertEqual(note.number, f"CRN-{self.device.pk}-000001")
        self.assertIn(f"<cbc:ID>{invoice.number}</cbc:ID>", note.signed_xml)
        self.assertIn('name="0200000">381</cbc:InvoiceTypeCode>', note.signed_xml)
        self.assertIn("<cbc:InstructionNote>Order cancelled</cbc:InstructionNote>", note.signed_xml)
        with self.assertRaises(ValueError):
            self.issue(kind=ubl.DocumentKind.CREDIT_NOTE)

    def test_no_active_device_no_invoice(self):
        ZatcaDevice.objects.update(is_active=False)
        with self.assertRaises(documents.IssueError):
            self.issue()
        self.assertFalse(ZatcaDocument.objects.exists())
