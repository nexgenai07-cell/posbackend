from orders.models import OrderItem
from orders.test_customer_session import CustomerSessionTestCase


class SameDishGrowsOneLineTests(CustomerSessionTestCase):
    """The same dish added again grows the line still waiting to be sent; a
    line already in the kitchen is never changed — the addition is its own
    kitchen line."""

    def lines(self, order_id):
        return list(OrderItem.objects.filter(order_id=order_id).order_by("id").values_list("product_id", "quantity", "status"))

    def test_staff_taps_merge_into_the_pending_line(self):
        table = self.new_table()
        order_id = self.cashier_client.post("/api/orders/", {"table": table.pk}, format="json").json()["id"]
        for _ in range(3):
            response = self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.fries.pk}, format="json")
            self.assertEqual(response.status_code, 201, response.content)
        self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.burger.pk}, format="json")
        self.assertEqual(self.lines(order_id), [(self.fries.pk, 3, "pending"), (self.burger.pk, 1, "pending")])

    def test_different_note_stays_a_separate_line(self):
        table = self.new_table()
        order_id = self.cashier_client.post("/api/orders/", {"table": table.pk}, format="json").json()["id"]
        self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.fries.pk}, format="json")
        self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.fries.pk, "notes": "no salt"}, format="json")
        self.assertEqual(len(self.lines(order_id)), 2)

    def test_customer_cart_and_later_round_merge_while_pending(self):
        table = self.new_table()
        body = self.qr(table, items=[{"product": self.fries.pk, "quantity": 1}, {"product": self.fries.pk, "quantity": 2}]).json()
        self.qr(table, items=[{"product": self.fries.pk, "quantity": 1}])
        self.assertEqual(self.lines(body["id"]), [(self.fries.pk, 4, "pending")])

    def test_addition_after_kitchen_is_a_new_kitchen_line(self):
        table, order_id, item_id, _ = self.order_to_ready()
        response = self.cashier_client.post(f"/api/orders/{order_id}/items/", {"product": self.fries.pk}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertNotEqual(response.json()["id"], item_id)
        self.assertEqual(self.lines(order_id), [(self.fries.pk, 1, "ready"), (self.fries.pk, 1, "fired")])
