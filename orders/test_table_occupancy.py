from orders.models import Bill, Order
from orders.test_customer_session import CustomerSessionTestCase

FIRST = "0501234567"
SECOND = "0507777777"


class OnePartyPerTableTests(CustomerSessionTestCase):
    """
    An occupied table belongs to the party already on it until staff release
    it: someone else scanning the same QR with another number is refused
    (error.tableOccupied), whether the bill is open, awaiting payment or
    already paid. The party itself keeps ordering on its own number.
    """

    def served_and_paid(self, table):
        _, order_id, _, body = self.order_to_ready(table, phone=FIRST)
        self.serve_everything(order_id)
        bill_id = body["bill_id"]
        self.assertEqual(self.cashier_client.post(f"/api/bills/{bill_id}/mark-paid/", {"method": "cash"}, format="json").status_code, 200)
        return bill_id

    def assert_occupied(self, response):
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"], "error.tableOccupied")

    def test_second_number_refused_while_first_party_is_ordering(self):
        table = self.new_table()
        self.assertEqual(self.qr(table, phone=FIRST).status_code, 201)
        self.assert_occupied(self.qr(table, phone=SECOND))
        self.assertEqual(Bill.objects.filter(table=table).count(), 1)

    def test_second_number_refused_after_bill_is_paid_but_not_released(self):
        table = self.new_table()
        self.served_and_paid(table)
        self.assert_occupied(self.qr(table, phone=SECOND))
        self.assertEqual(Bill.objects.filter(table=table).count(), 1)
        self.assertFalse(Order.objects.filter(table=table, customer__phone=SECOND).exists())

    def test_same_party_can_add_after_paying_on_its_own_bill(self):
        table = self.new_table()
        bill_id = self.served_and_paid(table)
        response = self.qr(table, phone=FIRST, items=[{"product": self.burger.pk}])
        self.assertIn(response.status_code, (200, 201), response.content)
        self.assertEqual(response.json()["bill_id"], bill_id)
        self.assertEqual(Bill.objects.filter(table=table).count(), 1)

    def test_same_number_in_another_format_is_the_same_party(self):
        table = self.new_table()
        self.served_and_paid(table)
        response = self.qr(table, phone="+966501234567", items=[{"product": self.burger.pk}])
        self.assertIn(response.status_code, (200, 201), response.content)

    def test_table_lookup_reports_occupied_and_locks_out_another_number(self):
        table = self.new_table()
        self.served_and_paid(table)
        body = self.public.get(f"/api/tables/by-session/{table.session_token}/", {"phone": SECOND}).json()
        self.assertTrue(body["occupied"])
        self.assertFalse(body["can_order"])
        self.assertIsNone(body["bill_id"])

    def test_staff_bill_on_table_also_blocks_a_customer_number(self):
        table = self.new_table()
        self.assertEqual(self.cashier_client.post("/api/orders/", {"table": table.pk}, format="json").status_code, 201)
        self.assert_occupied(self.qr(table, phone=SECOND))

    def test_new_party_can_order_once_released(self):
        table = self.new_table()
        bill_id = self.served_and_paid(table)
        self.assertEqual(self.cashier_client.post(f"/api/bills/{bill_id}/release-table/").status_code, 200)
        self.assertEqual(self.qr(table, phone=SECOND).status_code, 201)

    def test_staff_addition_after_paid_goes_on_the_partys_bill(self):
        table = self.new_table()
        bill_id = self.served_and_paid(table)
        response = self.cashier_client.post("/api/orders/", {"table": table.pk}, format="json")
        self.assertIn(response.status_code, (200, 201), response.content)
        order_id = response.json()["id"]
        added = self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.burger.pk}, format="json")
        self.assertEqual(added.status_code, 201, added.content)
        self.assertEqual(list(Bill.objects.filter(table=table).exclude(status="closed").values_list("pk", flat=True)), [bill_id])
