from asgiref.sync import async_to_sync
from django.test import TestCase

from branches.models import Branch
from tables.models import Table

from .consumers import _get_table_id_for_session


class TableSessionConsumerAuthTests(TestCase):
    """
    /ws/table/ authenticates the customer's phone against a live table session
    token. This exercises the resolver directly rather than driving a full
    WebsocketCommunicator handshake: the resolver is the only logic in the
    consumer's connect(), and what's worth pinning is the token normalisation
    it now shares with the REST endpoints (tables/services.py).

    async_to_sync() from a sync test is safe here because Channels'
    database_sync_to_async is thread_sensitive by default, so the query still
    runs on this thread's connection and sees the test's transaction.
    """

    @classmethod
    def setUpTestData(cls):
        cls.branch = Branch.objects.create(name_en="Smoke & Char — Riyadh", name_ar="سموك آند تشار — الرياض")
        cls.table = Table.objects.create(branch=cls.branch, label_en="Table 3", label_ar="طاولة ٣")
        cls.table.open()

    def resolve(self, token):
        return async_to_sync(_get_table_id_for_session)(token)

    def test_bare_token_resolves_to_the_table(self):
        self.assertEqual(self.resolve(self.table.session_token), self.table.id)

    def test_qr_payload_resolves_to_the_table(self):
        """A phone connecting straight off a scan sends /t/<token>, and "/" is
        legal in a query string, so it arrives here unencoded."""
        self.assertEqual(self.resolve(f"/t/{self.table.session_token}"), self.table.id)

    def test_unknown_and_garbage_tokens_resolve_to_none(self):
        for raw in (None, "", "/t/", "Table 3", "nope00000000000000000000000000"):
            with self.subTest(raw=raw):
                self.assertIsNone(self.resolve(raw))

    def test_a_closed_tables_old_token_resolves_to_none(self):
        stale_token = self.table.session_token
        self.table.close()
        self.assertIsNone(self.resolve(stale_token))

