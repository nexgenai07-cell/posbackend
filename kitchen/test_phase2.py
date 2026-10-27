"""
Kitchen stations, phase 2: who is making a line, moving a line to another
station, stations whose chefs are not clocked in, and the head chef's pass.
See docs/kitchen-stations-plan.md §5.
"""

from unittest.mock import patch

from django.utils import timezone

from accounts.models import Shift, StaffRole
from branches.models import Branch
from orders.models import Order, OrderItem
from orders.test_customer_session import make_staff

from .models import KitchenStation
from .tests import StationTestCase


class Phase2TestCase(StationTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second_bbq_chef = make_staff(cls.branch, "BbqChef2", StaffRole.KITCHEN, "2003")
        cls.second_bbq_chef.stations.set([cls.bbq])
        cls.head_chef = make_staff(cls.branch, "HeadChef", StaffRole.KITCHEN, "2004")
        cls.head_chef.is_head_chef = True
        cls.head_chef.save(update_fields=["is_head_chef"])

    @property
    def second_bbq_client(self):
        return self.staff_client(self.second_bbq_chef, "2003")

    @property
    def head_client(self):
        return self.staff_client(self.head_chef, "2004")

    def take_order(self, *products):
        """An order of `products` still waiting for the cashier. Returns its id."""
        response = self.qr(self.new_table(), items=[{"product": product.pk, "quantity": 1} for product in products])
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()["id"]

    def send(self, order_id):
        """Cashier review and waiter confirm: the lines go to the kitchen."""
        self.assertEqual(self.cashier_client.post(
            f"/api/orders/{order_id}/cashier-review/", {"waiter": self.waiter.pk}, format="json",
        ).status_code, 200)
        self.assertEqual(self.waiter_client.post(f"/api/orders/{order_id}/waiter-confirm/").status_code, 200)

    def move(self, client, order_id, item_id, station):
        return client.post(f"/api/orders/{order_id}/items/{item_id}/station/",
                           {"station": station.pk if station else None}, format="json")

    def sent(self, event, action):
        captured = []
        with patch("orders.views.publish_staff_event",
                   side_effect=lambda staff_id, name, payload: captured.append((staff_id, name))):
            action()
        return {staff_id for staff_id, name in captured if name == event}

    def require_clock_in(self, value=True):
        Branch.objects.filter(pk=self.branch.pk).update(kitchen_requires_clock_in=value)

    def clock_in(self, staff):
        Shift.objects.create(staff=staff, clock_in=timezone.now())


# =========================================================================== 1
class ClaimTests(Phase2TestCase):
    """Start puts the chef's name on the line, so nobody makes it twice."""

    def test_start_records_who_and_when_and_the_ticket_says_so(self):
        order_id = self.fire(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        self.set_status(self.bbq_client, order_id, item_id, "preparing")
        line = self.line(order_id, self.kebab)
        self.assertEqual(line.started_by_id, self.bbq_chef.pk)
        self.assertIsNotNone(line.started_at)
        ticket = next(t for t in self.tickets(self.second_bbq_client) if t["id"] == order_id)
        self.assertEqual(ticket["items"][0]["started_by_name"], "BbqChef")
        self.assertIsNotNone(ticket["items"][0]["started_at"])

    def test_a_second_start_is_refused_with_the_name_of_who_has_it(self):
        order_id = self.fire(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        self.set_status(self.bbq_client, order_id, item_id, "preparing")
        response = self.item_status(self.second_bbq_client, order_id, item_id, "preparing")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"], "error.itemAlreadyStarted")
        self.assertEqual(response.json()["started_by_name"], "BbqChef")
        self.assertEqual(self.line(order_id, self.kebab).started_by_id, self.bbq_chef.pk)

    def test_another_chef_of_the_station_may_still_finish_it(self):
        order_id = self.fire(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        self.set_status(self.bbq_client, order_id, item_id, "preparing")
        self.set_status(self.second_bbq_client, order_id, item_id, "ready")
        line = self.line(order_id, self.kebab)
        self.assertEqual((line.status, line.started_by_id), ("ready", self.bbq_chef.pk))


# =========================================================================== 2
class MoveLineTests(Phase2TestCase):
    """The cashier (or the head chef) sends a line to another station."""

    def test_the_cashier_sees_where_each_line_will_go_before_sending(self):
        order_id = self.take_order(self.kebab, self.burger)
        lines = {item["name_en_snapshot"]: item for item in
                 self.cashier_client.get(f"/api/orders/{order_id}/").json()["items"]}
        self.assertEqual(lines["Kebab"]["route_station"]["name_en"], "BBQ")
        self.assertIsNone(lines["Burger"]["route_station"])  # General
        self.assertFalse(lines["Kebab"]["station_moved"])

    def test_a_pending_line_moved_by_the_cashier_keeps_that_station_when_fired(self):
        order_id = self.take_order(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        response = self.move(self.cashier_client, order_id, item_id, self.fryer)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["route_station"]["name_en"], "Fryer")
        self.assertTrue(response.json()["station_moved"])
        self.send(order_id)
        self.assertEqual(self.line(order_id, self.kebab).station_id, self.fryer.pk)
        self.assertIn(order_id, self.ticket_order_ids(self.fryer_client))
        self.assertNotIn(order_id, self.ticket_order_ids(self.bbq_client))

    def test_moving_to_general_puts_it_on_every_screen(self):
        order_id = self.take_order(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        self.assertEqual(self.move(self.cashier_client, order_id, item_id, None).status_code, 200)
        self.send(order_id)
        self.assertIsNone(self.line(order_id, self.kebab).station_id)
        self.assertIn(order_id, self.ticket_order_ids(self.fryer_client))

    def test_the_move_is_on_the_order_history(self):
        order_id = self.take_order(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        self.move(self.cashier_client, order_id, item_id, self.fryer)
        event = Order.objects.get(pk=order_id).history.get(event="item_station_moved")
        self.assertEqual((event.details["from"], event.details["to"]), (None, self.fryer.pk))

    def test_a_station_switched_off_after_the_move_sends_the_line_to_general(self):
        order_id = self.take_order(self.kebab)
        self.move(self.cashier_client, order_id, self.line(order_id, self.kebab).pk, self.fryer)
        KitchenStation.objects.filter(pk=self.fryer.pk).update(is_active=False)
        self.send(order_id)
        self.assertIsNone(self.line(order_id, self.kebab).station_id)

    def test_in_the_kitchen_only_the_head_chef_or_a_manager_moves_it(self):
        order_id = self.fire(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        self.assertEqual(self.move(self.cashier_client, order_id, item_id, self.fryer).status_code, 403)
        self.assertEqual(self.move(self.bbq_client, order_id, item_id, self.fryer).status_code, 403)
        self.assertEqual(self.move(self.head_client, order_id, item_id, self.fryer).status_code, 200)
        self.assertEqual(self.line(order_id, self.kebab).station_id, self.fryer.pk)
        self.assertEqual(self.move(self.manager_client, order_id, item_id, self.bbq).status_code, 200)

    def test_moving_a_fired_line_tells_both_stations(self):
        order_id = self.fire(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        told_new = self.sent("order:kitchen_new", lambda: self.move(self.manager_client, order_id, item_id, self.fryer))
        self.assertIn(self.fryer_chef.pk, told_new)
        self.assertNotIn(self.bbq_chef.pk, told_new)
        told_changed = self.sent("order:changed", lambda: self.move(self.manager_client, order_id, item_id, self.bbq))
        self.assertIn(self.fryer_chef.pk, told_changed)   # Fryer drops it again

    def test_a_line_already_being_made_stays_where_it_is(self):
        order_id = self.fire(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        self.set_status(self.bbq_client, order_id, item_id, "preparing")
        response = self.move(self.manager_client, order_id, item_id, self.fryer)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "error.itemAlreadyStarted")

    def test_only_an_active_station_of_this_branch(self):
        other_branch = Branch.objects.create(
            name_en="Other", name_ar="Other", timezone="Asia/Riyadh", currency="SAR",
            latitude="24.713600", longitude="46.675300",
        )
        elsewhere = KitchenStation.objects.create(branch=other_branch, name_en="X", name_ar="X")
        off = KitchenStation.objects.create(branch=self.branch, name_en="Off", name_ar="Off", is_active=False)
        order_id = self.take_order(self.kebab)
        item_id = self.line(order_id, self.kebab).pk
        for station in (elsewhere, off):
            response = self.move(self.cashier_client, order_id, item_id, station)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "error.stationNotFound")

    def test_a_waiter_cannot_move_lines(self):
        order_id = self.take_order(self.kebab)
        response = self.move(self.waiter_client, order_id, self.line(order_id, self.kebab).pk, self.fryer)
        self.assertEqual(response.status_code, 403)


# =========================================================================== 3
class ClockInTests(Phase2TestCase):
    """With clock-in required, a station whose chefs are all off shift is
    nobody's — so its lines show on every kitchen screen."""

    def test_off_by_default_every_active_chef_counts(self):
        order_id = self.fire(self.kebab)
        self.assertNotIn(order_id, self.ticket_order_ids(self.fryer_client))

    def test_no_bbq_chef_clocked_in_puts_bbq_on_every_screen(self):
        self.require_clock_in()
        self.clock_in(self.fryer_chef)
        order_id = self.fire(self.kebab)
        self.assertIn(order_id, self.ticket_order_ids(self.fryer_client))

    def test_once_a_bbq_chef_clocks_in_it_is_theirs_again(self):
        self.require_clock_in()
        self.clock_in(self.second_bbq_chef)
        order_id = self.fire(self.kebab)
        self.assertNotIn(order_id, self.ticket_order_ids(self.fryer_client))
        self.assertIn(order_id, self.ticket_order_ids(self.second_bbq_client))

    def test_a_clocked_out_chef_does_not_count(self):
        self.require_clock_in()
        Shift.objects.create(staff=self.bbq_chef, clock_in=timezone.now(), clock_out=timezone.now())
        order_id = self.fire(self.kebab)
        self.assertIn(order_id, self.ticket_order_ids(self.fryer_client))

    def test_the_kitchen_is_told_the_same_way(self):
        self.require_clock_in()
        self.clock_in(self.second_bbq_chef)
        told = self.sent("order:kitchen_new", lambda: self.fire(self.kebab))
        # Only the BBQ chef on shift — plus the head chef, who hears everything.
        self.assertEqual(told, {self.second_bbq_chef.pk, self.head_chef.pk})

    def test_the_station_list_says_who_is_on_duty(self):
        self.require_clock_in()
        self.clock_in(self.bbq_chef)
        rows = {row["name_en"]: row for row in self.manager_client.get("/api/kitchen-stations/").json()["results"]}
        on_duty = {chef["name"]: chef["on_duty"] for chef in rows["BBQ"]["chefs"]}
        self.assertEqual(on_duty, {"BbqChef": True, "BbqChef2": False})

    def test_the_setup_checklist_is_about_who_works_where_not_who_is_in(self):
        self.require_clock_in()
        summary = self.manager_client.get("/api/kitchen-stations/summary/").json()
        self.assertEqual([row["name_en"] for row in summary["unstaffed_stations"]], ["Desserts"])

    def test_the_setting_is_read_by_anyone_and_changed_by_a_manager(self):
        url = "/api/kitchen-stations/settings/"
        self.assertEqual(self.bbq_client.get(url).json(), {"kitchen_requires_clock_in": False})
        self.assertEqual(self.bbq_client.patch(url, {"kitchen_requires_clock_in": True}, format="json").status_code, 403)
        response = self.manager_client.patch(url, {"kitchen_requires_clock_in": True}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Branch.objects.get(pk=self.branch.pk).kitchen_requires_clock_in)


# =========================================================================== 4
class ExpoTests(Phase2TestCase):
    """The head chef's pass: every station at once, then hand over."""

    def expo(self, client):
        response = client.get("/api/kds/tickets/", {"scope": "expo"})
        self.assertEqual(response.status_code, 200, response.content)
        return {ticket["id"]: ticket for ticket in response.json()["results"]}

    def cook(self, client, order_id, product):
        item_id = self.line(order_id, product).pk
        self.set_status(client, order_id, item_id, "preparing")
        self.set_status(client, order_id, item_id, "ready")

    def test_a_head_chef_sees_every_station_by_default(self):
        bbq_order, fryer_order = self.fire(self.kebab), self.fire(self.fries)
        self.assertLessEqual({bbq_order, fryer_order}, self.ticket_order_ids(self.head_client))

    def test_the_pass_is_for_the_head_chef_and_managers_only(self):
        self.assertEqual(self.bbq_client.get("/api/kds/tickets/", {"scope": "expo"}).status_code, 403)
        self.expo(self.head_client)
        self.expo(self.manager_client)

    def test_the_pass_shows_every_line_and_says_when_all_is_ready(self):
        order_id = self.fire(self.kebab, self.fries)
        self.cook(self.bbq_client, order_id, self.kebab)
        ticket = self.expo(self.head_client)[order_id]
        statuses = {item["name_en_snapshot"]: item["status"] for item in ticket["items"]}
        self.assertEqual(statuses, {"Kebab": "ready", "Fries": "fired"})
        self.assertFalse(ticket["all_ready"])
        self.cook(self.fryer_client, order_id, self.fries)
        self.assertTrue(self.expo(self.head_client)[order_id]["all_ready"])

    def test_handover_is_refused_while_a_station_is_cooking(self):
        order_id = self.fire(self.kebab, self.fries)
        self.cook(self.bbq_client, order_id, self.kebab)
        response = self.head_client.post(f"/api/orders/{order_id}/handover/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "error.orderNotReady")

    def test_handover_tells_the_waiter_and_leaves_serving_to_them(self):
        order_id = self.fire(self.kebab, self.fries)
        self.cook(self.bbq_client, order_id, self.kebab)
        self.cook(self.fryer_client, order_id, self.fries)
        told = self.sent("order:handed_over", lambda: self.assertEqual(
            self.head_client.post(f"/api/orders/{order_id}/handover/").status_code, 200,
        ))
        self.assertIn(self.waiter.pk, told)
        # Still ready: the food has left the kitchen, not reached the table.
        lines = list(OrderItem.objects.filter(order_id=order_id))
        self.assertEqual({line.status for line in lines}, {"ready"})
        self.assertTrue(all(line.handed_over_at for line in lines))
        self.assertEqual(Order.objects.get(pk=order_id).status, "ready")
        self.assertNotIn(order_id, self.expo(self.head_client))
        self.assertEqual(self.head_client.post(f"/api/orders/{order_id}/handover/").status_code, 409)
        # The waiter serves it at the table, as before.
        for line in lines:
            self.assertEqual(self.item_status(self.waiter_client, order_id, line.pk, "served").status_code, 200)
        self.assertEqual(Order.objects.get(pk=order_id).status, "served")

    def test_a_plain_chef_cannot_hand_over(self):
        order_id = self.fire(self.kebab)
        self.cook(self.bbq_client, order_id, self.kebab)
        self.assertEqual(self.bbq_client.post(f"/api/orders/{order_id}/handover/").status_code, 403)

    def test_head_chef_is_a_kitchen_title_only(self):
        response = self.manager_client.patch(f"/api/staff/{self.waiter.pk}/", {"is_head_chef": True}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["is_head_chef"])
        response = self.manager_client.patch(f"/api/staff/{self.bbq_chef.pk}/", {"is_head_chef": True}, format="json")
        self.assertTrue(response.json()["is_head_chef"])
