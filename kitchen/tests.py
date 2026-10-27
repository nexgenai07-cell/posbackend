"""
Kitchen stations: where a dish's line goes, whose screen shows it, and who
hears about it — the whole of kitchen/routing.py seen from the API.

The order itself never splits: every test here has one order, and the
cashier, the waiter and the customer keep seeing all of it.
"""

from unittest.mock import patch

from branches.models import Branch
from catalog.models import Category, Product
from inventory.models import RecipeItem
from orders.models import Order, OrderItem
from orders.test_customer_session import CustomerSessionTestCase, make_staff
from accounts.models import StaffRole

from .models import KitchenStation
from .routing import station_for


class StationTestCase(CustomerSessionTestCase):
    """A kitchen with two staffed stations (BBQ, Fryer), one nobody works at
    (Desserts) and dishes reaching their station each of the three ways:
    its own (Shawarma), its category's (Kebab) and neither (Burger)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bbq = KitchenStation.objects.create(branch=cls.branch, name_en="BBQ", name_ar="BBQ", sort_order=1)
        cls.fryer = KitchenStation.objects.create(branch=cls.branch, name_en="Fryer", name_ar="Fryer", sort_order=2)
        cls.desserts = KitchenStation.objects.create(branch=cls.branch, name_en="Desserts", name_ar="Desserts", sort_order=3)

        grill = Category.objects.create(branch=cls.branch, name_en="Grill", name_ar="Grill", station=cls.bbq)
        sweets = Category.objects.create(branch=cls.branch, name_en="Sweets", name_ar="Sweets", station=cls.desserts)
        # Station from the category.
        cls.kebab = Product.objects.create(branch=cls.branch, category=grill, name_en="Kebab", name_ar="Kebab",
                                           price="30.00", cost_price="10.00")
        # Its own station, overriding the category's.
        cls.shawarma = Product.objects.create(branch=cls.branch, category=grill, name_en="Shawarma", name_ar="Shawarma",
                                              price="20.00", cost_price="7.00", station=cls.fryer)
        # A station nobody works at.
        cls.cake = Product.objects.create(branch=cls.branch, category=sweets, name_en="Cake", name_ar="Cake",
                                          price="15.00", cost_price="5.00")
        # A dish with no recipe can't be ordered, so the new ones get one.
        for dish in (cls.kebab, cls.shawarma, cls.cake):
            RecipeItem.objects.create(product=dish, inventory_item=cls.patty, quantity=1, unit="unit")
        # cls.fries keeps its own station; cls.burger stays General.
        Product.objects.filter(pk=cls.fries.pk).update(station=cls.fryer)
        cls.fries.refresh_from_db()

        cls.bbq_chef = make_staff(cls.branch, "BbqChef", StaffRole.KITCHEN, "2001")
        cls.bbq_chef.stations.set([cls.bbq])
        cls.fryer_chef = make_staff(cls.branch, "FryerChef", StaffRole.KITCHEN, "2002")
        cls.fryer_chef.stations.set([cls.fryer])
        # cls.kitchen (from the base case) works at no station at all.

    # -- helpers -------------------------------------------------------------
    @property
    def bbq_client(self):
        return self.staff_client(self.bbq_chef, "2001")

    @property
    def fryer_client(self):
        return self.staff_client(self.fryer_chef, "2002")

    @property
    def manager_client(self):
        return self.staff_client(self.manager, "1005")

    def fire(self, *products):
        """One order of `products`, confirmed by the waiter so its lines are
        in the kitchen. Returns the order id."""
        table = self.new_table()
        response = self.qr(table, items=[{"product": product.pk, "quantity": 1} for product in products])
        self.assertIn(response.status_code, (200, 201), response.content)
        order_id = response.json()["id"]
        self.assertEqual(self.cashier_client.post(
            f"/api/orders/{order_id}/cashier-review/", {"waiter": self.waiter.pk}, format="json",
        ).status_code, 200)
        self.assertEqual(self.waiter_client.post(f"/api/orders/{order_id}/waiter-confirm/").status_code, 200)
        return order_id

    def tickets(self, client, **params):
        response = client.get("/api/kds/tickets/", params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def ticket_order_ids(self, client, **params):
        return {ticket["id"] for ticket in self.tickets(client, **params)}

    def line(self, order_id, product):
        return OrderItem.objects.get(order_id=order_id, product=product)

    def set_status(self, client, order_id, item_id, value):
        response = self.item_status(client, order_id, item_id, value)
        self.assertEqual(response.status_code, 200, response.content)


# =========================================================================== 1
class ResolveTests(StationTestCase):
    """Dish → category → General, and an inactive station is no station."""

    def test_dish_uses_its_own_station_then_its_category_then_general(self):
        self.assertEqual(station_for(self.shawarma), self.fryer.pk)  # its own, over the category's BBQ
        self.assertEqual(station_for(self.kebab), self.bbq.pk)       # from the category
        self.assertIsNone(station_for(self.burger))                  # neither — General

    def test_switched_off_station_falls_back(self):
        KitchenStation.objects.filter(pk=self.fryer.pk).update(is_active=False)
        # The dish's own station is off, so its category's BBQ takes over.
        self.assertEqual(station_for(self.shawarma), self.bbq.pk)
        # Fries has no category station behind it, so it goes to General.
        self.assertIsNone(station_for(self.fries))

    def test_both_off_is_general(self):
        KitchenStation.objects.filter(pk__in=[self.fryer.pk, self.bbq.pk]).update(is_active=False)
        self.assertIsNone(station_for(self.shawarma))
        self.assertIsNone(station_for(self.kebab))

    def test_api_says_where_a_dish_goes_and_why(self):
        rows = {row["id"]: row["effective_station"] for row in
                self.manager_client.get("/api/products/", {"page_size": 100}).json()["results"]}
        self.assertEqual(rows[self.kebab.pk]["source"], "category")
        self.assertEqual(rows[self.kebab.pk]["station"]["id"], self.bbq.pk)
        self.assertEqual(rows[self.shawarma.pk]["source"], "product")
        self.assertEqual(rows[self.shawarma.pk]["station"]["id"], self.fryer.pk)
        self.assertEqual(rows[self.burger.pk], {"source": "general", "station": None})


# =========================================================================== 2
class SnapshotTests(StationTestCase):
    """A fired line keeps the station it was sent to."""

    def test_line_takes_its_station_when_it_is_fired(self):
        order_id = self.fire(self.kebab, self.fries, self.burger)
        self.assertEqual(self.line(order_id, self.kebab).station_id, self.bbq.pk)
        self.assertEqual(self.line(order_id, self.fries).station_id, self.fryer.pk)
        self.assertIsNone(self.line(order_id, self.burger).station_id)

    def test_pending_line_has_no_station_until_it_is_fired(self):
        table = self.new_table()
        order_id = self.qr(table, items=[{"product": self.kebab.pk, "quantity": 1}]).json()["id"]
        self.assertIsNone(self.line(order_id, self.kebab).station_id)
        self.cashier_client.post(f"/api/orders/{order_id}/cashier-review/", {"waiter": self.waiter.pk}, format="json")
        self.waiter_client.post(f"/api/orders/{order_id}/waiter-confirm/")
        self.assertEqual(self.line(order_id, self.kebab).station_id, self.bbq.pk)

    def test_moving_a_dish_later_does_not_move_lines_already_cooking(self):
        order_id = self.fire(self.kebab)
        response = self.manager_client.patch(
            f"/api/products/{self.kebab.pk}/", {"station": self.fryer.pk}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.line(order_id, self.kebab).station_id, self.bbq.pk)
        # Only the next order follows the dish to its new station.
        self.assertEqual(self.line(self.fire(self.kebab), self.kebab).station_id, self.fryer.pk)

    def test_switched_off_station_sends_new_lines_to_general(self):
        KitchenStation.objects.filter(pk=self.fryer.pk).update(is_active=False)
        order_id = self.fire(self.fries)
        self.assertIsNone(self.line(order_id, self.fries).station_id)
        # The line fired before it was switched off keeps pointing at it.
        KitchenStation.objects.filter(pk=self.fryer.pk).update(is_active=True)
        older = self.fire(self.fries)
        KitchenStation.objects.filter(pk=self.fryer.pk).update(is_active=False)
        self.assertEqual(self.line(older, self.fries).station_id, self.fryer.pk)


# =========================================================================== 3
class ChefScreenTests(StationTestCase):
    """?stations= — mine, all, and a chosen few."""

    def test_mine_shows_only_orders_with_my_lines_or_general(self):
        bbq_order = self.fire(self.kebab)
        fryer_order = self.fire(self.fries)
        general_order = self.fire(self.burger)

        bbq_seen = self.ticket_order_ids(self.bbq_client)
        self.assertIn(bbq_order, bbq_seen)
        self.assertIn(general_order, bbq_seen)
        self.assertNotIn(fryer_order, bbq_seen)

        fryer_seen = self.ticket_order_ids(self.fryer_client)
        self.assertIn(fryer_order, fryer_seen)
        self.assertIn(general_order, fryer_seen)
        self.assertNotIn(bbq_order, fryer_seen)

    def test_all_shows_everything_and_is_the_default_for_a_manager(self):
        bbq_order, fryer_order = self.fire(self.kebab), self.fire(self.fries)
        self.assertLessEqual({bbq_order, fryer_order}, self.ticket_order_ids(self.bbq_client, stations="all"))
        self.assertLessEqual({bbq_order, fryer_order}, self.ticket_order_ids(self.manager_client))

    def test_chosen_stations_and_general(self):
        bbq_order, fryer_order, general_order = self.fire(self.kebab), self.fire(self.fries), self.fire(self.burger)
        only_bbq = self.ticket_order_ids(self.manager_client, stations=str(self.bbq.pk))
        self.assertEqual(only_bbq & {bbq_order, fryer_order, general_order}, {bbq_order})
        with_general = self.ticket_order_ids(self.manager_client, stations=f"{self.bbq.pk},general")
        self.assertEqual(with_general & {bbq_order, fryer_order, general_order}, {bbq_order, general_order})

    def test_the_whole_order_is_on_the_ticket_with_the_other_lines_marked_not_mine(self):
        order_id = self.fire(self.kebab, self.fries, self.burger)
        ticket = next(t for t in self.tickets(self.bbq_client) if t["id"] == order_id)
        lines = {item["name_en_snapshot"]: item for item in ticket["items"]}
        self.assertEqual(set(lines), {"Kebab", "Fries", "Burger"})
        self.assertTrue(lines["Kebab"]["is_mine"])
        self.assertTrue(lines["Burger"]["is_mine"])   # General is everyone's
        self.assertFalse(lines["Fries"]["is_mine"])   # greyed on the BBQ screen
        self.assertEqual(lines["Kebab"]["station_name_en"], "BBQ")
        self.assertIsNone(lines["Burger"]["station"])

    def test_a_mixed_order_reaches_both_chefs_once_as_one_order(self):
        order_id = self.fire(self.kebab, self.fries)
        self.assertIn(order_id, self.ticket_order_ids(self.bbq_client))
        self.assertIn(order_id, self.ticket_order_ids(self.fryer_client))
        # The cashier still sees one order with both lines.
        detail = self.cashier_client.get(f"/api/orders/{order_id}/").json()
        self.assertEqual(len(detail["items"]), 2)


# =========================================================================== 4
class UnstaffedStationTests(StationTestCase):
    """A station no chef works at is nobody's — so it is everybody's."""

    def test_lines_of_a_station_without_chefs_show_on_every_kitchen_screen(self):
        order_id = self.fire(self.cake)
        self.assertEqual(self.line(order_id, self.cake).station_id, self.desserts.pk)
        for client in (self.bbq_client, self.fryer_client, self.kitchen_client):
            self.assertIn(order_id, self.ticket_order_ids(client))

    def test_once_a_chef_works_there_it_is_only_theirs(self):
        self.bbq_chef.stations.add(self.desserts)
        order_id = self.fire(self.cake)
        self.assertIn(order_id, self.ticket_order_ids(self.bbq_client))
        self.assertNotIn(order_id, self.ticket_order_ids(self.fryer_client))

    def test_a_chef_with_no_station_sees_general_and_the_unstaffed_ones(self):
        general_order, cake_order, bbq_order = self.fire(self.burger), self.fire(self.cake), self.fire(self.kebab)
        seen = self.ticket_order_ids(self.kitchen_client)
        self.assertIn(general_order, seen)
        self.assertIn(cake_order, seen)
        self.assertNotIn(bbq_order, seen)


# =========================================================================== 5
class NotificationTests(StationTestCase):
    """Only the chefs of the lines' stations are told."""

    def sent(self, event, action):
        captured = []
        with patch("orders.views.publish_staff_event",
                   side_effect=lambda staff_id, name, payload: captured.append((staff_id, name, payload))):
            action()
        return [(staff_id, payload) for staff_id, name, payload in captured if name == event]

    def test_kitchen_is_told_station_by_station(self):
        kitchen_staff = {self.bbq_chef.pk, self.fryer_chef.pk, self.kitchen.pk}
        for products, expected in (
            ((self.kebab,), {self.bbq_chef.pk}),
            ((self.fries,), {self.fryer_chef.pk}),
            ((self.kebab, self.fries), {self.bbq_chef.pk, self.fryer_chef.pk}),
            ((self.burger,), kitchen_staff),      # General is everyone's
            ((self.cake,), kitchen_staff),        # nobody works at Desserts
            ((self.kebab, self.burger), kitchen_staff),
        ):
            with self.subTest(dishes=[product.name_en for product in products]):
                told = self.sent("order:kitchen_new", lambda: self.fire(*products))
                self.assertEqual({staff_id for staff_id, _ in told}, expected)

    def test_the_event_names_the_stations_involved(self):
        told = self.sent("order:kitchen_new", lambda: self.fire(self.kebab, self.burger))
        for _, payload in told:
            self.assertEqual(payload["station_ids"], [self.bbq.pk, None])

    def test_a_cancelled_order_reaches_the_same_chefs(self):
        order_id = self.fire(self.kebab)
        # Once in the kitchen only an owner or manager may cancel, with a reason.
        told = self.sent("order:cancelled", lambda: self.assertEqual(self.manager_client.post(
            f"/api/orders/{order_id}/cancel/", {"reason": "Customer left"}, format="json",
        ).status_code, 200))
        # The order's own waiter and cashier hear too; of the kitchen, only BBQ.
        kitchen_staff = {self.bbq_chef.pk, self.fryer_chef.pk, self.kitchen.pk}
        self.assertEqual({staff_id for staff_id, _ in told} & kitchen_staff, {self.bbq_chef.pk})

    def test_a_line_moving_on_reaches_the_same_chefs(self):
        order_id = self.fire(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        told = self.sent("order:changed", lambda: self.set_status(self.bbq_client, order_id, item_id, "preparing"))
        # The chef who acted is not told about their own action.
        self.assertEqual({staff_id for staff_id, _ in told}, set())
        told = self.sent("order:changed", lambda: self.set_status(self.kitchen_client, order_id, item_id, "ready"))
        self.assertEqual({staff_id for staff_id, _ in told}, {self.bbq_chef.pk})


# =========================================================================== 6
class BranchScopeTests(StationTestCase):
    """A station belongs to its branch and nothing else may reach for it."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_branch = Branch.objects.create(
            name_en="Other", name_ar="Other", timezone="Asia/Riyadh", currency="SAR",
            latitude="24.713600", longitude="46.675300",
        )
        cls.other_station = KitchenStation.objects.create(
            branch=cls.other_branch, name_en="Other BBQ", name_ar="Other BBQ",
        )

    def test_a_dish_cannot_be_sent_to_another_branch_station(self):
        response = self.manager_client.patch(
            f"/api/products/{self.kebab.pk}/", {"station": self.other_station.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("error.stationBranchMismatch", response.content.decode())

    def test_a_category_cannot_be_sent_to_another_branch_station(self):
        category_id = self.kebab.category_id
        response = self.manager_client.patch(
            f"/api/categories/{category_id}/", {"station": self.other_station.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("error.stationBranchMismatch", response.content.decode())

    def test_a_chef_cannot_be_put_at_another_branch_station(self):
        response = self.manager_client.patch(
            f"/api/staff/{self.bbq_chef.pk}/", {"stations": [self.other_station.pk]}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("error.stationBranchMismatch", response.content.decode())

    def test_bulk_set_refuses_another_branch_station(self):
        response = self.manager_client.post(
            "/api/products/bulk-station/", {"products": [self.kebab.pk], "station": self.other_station.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], "error.stationNotFound")

    def test_the_station_list_is_this_branch_only(self):
        rows = self.manager_client.get("/api/kitchen-stations/").json()["results"]
        self.assertEqual({row["name_en"] for row in rows}, {"BBQ", "Fryer", "Desserts"})


# =========================================================================== 7
class OrderStatusTests(StationTestCase):
    """One order, two stations: it follows the slowest of them."""

    def test_order_is_ready_only_when_every_station_is_ready(self):
        order_id = self.fire(self.kebab, self.fries)
        kebab_id, fries_id = self.line(order_id, self.kebab).pk, self.line(order_id, self.fries).pk

        self.set_status(self.bbq_client, order_id, kebab_id, "preparing")
        self.set_status(self.bbq_client, order_id, kebab_id, "ready")
        self.set_status(self.fryer_client, order_id, fries_id, "preparing")
        self.assertEqual(Order.objects.get(pk=order_id).status, "preparing")

        self.set_status(self.fryer_client, order_id, fries_id, "ready")
        self.assertEqual(Order.objects.get(pk=order_id).status, "ready")

    def test_a_station_ready_is_visible_line_by_line_to_everyone(self):
        order_id = self.fire(self.kebab, self.fries)
        kebab_id = self.line(order_id, self.kebab).pk
        self.set_status(self.bbq_client, order_id, kebab_id, "preparing")
        self.set_status(self.bbq_client, order_id, kebab_id, "ready")
        statuses = {item["name_en_snapshot"]: item["status"]
                    for item in self.cashier_client.get(f"/api/orders/{order_id}/").json()["items"]}
        self.assertEqual(statuses, {"Kebab": "ready", "Fries": "fired"})


# =========================================================================== 8
class StationApiTests(StationTestCase):
    """The setup screens' endpoint."""

    def test_a_manager_adds_a_station_and_a_duplicate_name_is_refused(self):
        response = self.manager_client.post(
            "/api/kitchen-stations/", {"name_en": "Salads", "name_ar": "سلطات"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["sort_order"], 4)
        again = self.manager_client.post(
            "/api/kitchen-stations/", {"name_en": " salads ", "name_ar": "سلطات 2"}, format="json",
        )
        self.assertEqual(again.status_code, 400)
        self.assertIn("error.stationNameExists", again.content.decode())

    def test_a_chef_may_read_the_stations_but_not_change_them(self):
        self.assertEqual(self.bbq_client.get("/api/kitchen-stations/").status_code, 200)
        response = self.bbq_client.post("/api/kitchen-stations/", {"name_en": "X", "name_ar": "X"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_a_station_is_switched_off_not_deleted(self):
        self.assertEqual(self.manager_client.delete(f"/api/kitchen-stations/{self.bbq.pk}/").status_code, 405)
        response = self.manager_client.patch(f"/api/kitchen-stations/{self.bbq.pk}/", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(KitchenStation.objects.filter(pk=self.bbq.pk).exists())

    def test_the_list_counts_dishes_and_chefs(self):
        rows = {row["name_en"]: row for row in self.manager_client.get("/api/kitchen-stations/").json()["results"]}
        # Kebab (via its category) and Shawarma's category is BBQ but its own
        # station is Fryer, so BBQ cooks one dish.
        self.assertEqual(rows["BBQ"]["dish_count"], 1)
        self.assertEqual(rows["BBQ"]["own_dish_count"], 0)
        self.assertEqual(rows["BBQ"]["category_count"], 1)
        self.assertEqual([chef["name"] for chef in rows["BBQ"]["chefs"]], ["BbqChef"])
        self.assertEqual(rows["Fryer"]["dish_count"], 2)   # Shawarma + Fries, both set on the dish
        self.assertEqual(rows["Desserts"]["chefs"], [])

    def test_the_checklist_summary(self):
        summary = self.manager_client.get("/api/kitchen-stations/summary/").json()
        self.assertEqual((summary["station_count"], summary["active_station_count"]), (3, 3))
        self.assertEqual(summary["general_dish_count"], 1)  # Burger alone
        self.assertEqual([row["name_en"] for row in summary["unstaffed_stations"]], ["Desserts"])
        self.assertEqual(summary["kitchen_staff_without_station"], 1)  # the base case's Kitchen

    def test_bulk_set_station_moves_many_dishes_at_once(self):
        response = self.manager_client.post(
            "/api/products/bulk-station/",
            {"products": [self.burger.pk, self.kebab.pk], "station": self.desserts.pk}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["updated"], 2)
        self.assertEqual(station_for(self.burger), self.desserts.pk)
        # And null gives a dish back to its category.
        self.manager_client.post("/api/products/bulk-station/", {"products": [self.kebab.pk], "station": None}, format="json")
        self.assertEqual(station_for(self.kebab), self.bbq.pk)

    def test_dishes_can_be_listed_by_where_they_are_cooked(self):
        ids = {row["id"] for row in self.manager_client.get("/api/products/", {"station": self.bbq.pk}).json()["results"]}
        self.assertEqual(ids, {self.kebab.pk})
        general = {row["id"] for row in self.manager_client.get("/api/products/", {"station": "general"}).json()["results"]}
        self.assertEqual(general, {self.burger.pk})
