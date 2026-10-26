from django.test import SimpleTestCase
from rest_framework import serializers

from common.exceptions import error_code_exception_handler


class _Sample(serializers.Serializer):
    name = serializers.CharField(max_length=3)
    seats = serializers.IntegerField()
    label = serializers.CharField(error_messages={"blank": "error.tableLabelExists"})


class ErrorCodeHandlerTests(SimpleTestCase):
    """Every error the API sends is a code the apps can translate."""

    def handle(self, exc):
        return error_code_exception_handler(exc, {"view": None})

    def errors_for(self, data):
        serializer = _Sample(data=data)
        self.assertFalse(serializer.is_valid())
        return self.handle(serializers.ValidationError(serializer.errors)).data

    def test_drf_messages_become_codes(self):
        data = self.errors_for({"name": "toolong", "seats": "x"})
        self.assertEqual(data["name"], ["error.fieldTooLong"])
        self.assertEqual(data["seats"], ["error.fieldInvalid"])
        self.assertEqual(data["label"], ["error.fieldRequired"])

    def test_own_codes_pass_through(self):
        data = self.errors_for({"name": "ab", "seats": 2, "label": ""})
        self.assertEqual(data, {"label": ["error.tableLabelExists"]})

    def test_unexpected_exception_is_a_json_server_error(self):
        with self.assertLogs("common.exceptions", level="ERROR"):
            response = self.handle(RuntimeError("boom"))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data, {"error": "error.serverError"})
