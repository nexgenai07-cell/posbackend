"""
Kitchen stations, phase 3: each station's printer screen prints its own
lines exactly once, and the report says how long every station took.
See docs/kitchen-stations-plan.md §6.
"""

from datetime import timedelta

from zoneinfo import ZoneInfo

from django.utils import timezone

from orders.models import OrderItem
from reports.filters import branch_datetime_bounds

from .tests import StationTestCase


class Phase3TestCase(StationTestCase):
    def time_line(self, order_id, product, *, fired, started=None, ready=None, status=None):
        """Give a fired line exact kitchen times (datetimes)."""
        fields = {"fired_at": fired, "started_at": started, "ready_at": ready}
        if status:
            fields["status"] = status
        OrderItem.objects.filter(order_id=order_id, product=product).update(**fields)


# =========================================================================== 1
class PrintClaimTests(Phase3TestCase):
    """A printer screen takes each of its station's lines once."""

    def setUp(self):
        super().setUp()
        self.since = (timezone.now() - timedelta(minutes=1)).isoformat()

    def claim(self, client, stations):
        response = client.post("/api/kds/print/", {"stations": stations, "since": self.since}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["results"]

    def printed_names(self, tickets):
        return sorted(item["name_en_snapshot"] for ticket in tickets for item in ticket["items"])

    def test_a_station_prints_only_its_own_lines_of_a_mixed_order(self):
        order_id = self.fire(self.kebab, self.fries, self.burger)
        tickets = self.claim(self.bbq_client, str(self.bbq.pk))
        self.assertEqual([ticket["id"] for ticket in tickets], [order_id])
        self.assertEqual(self.printed_names(tickets), ["Kebab"])
        self.assertEqual(self.printed_names(self.claim(self.fryer_client, f"{self.fryer.pk},general")), ["Burger", "Fries"])

    def test_two_screens_at_one_station_never_print_a_line_twice(self):
        self.fire(self.kebab)
        self.assertEqual(self.printed_names(self.claim(self.bbq_client, str(self.bbq.pk))), ["Kebab"])
        self.assertEqual(self.claim(self.kitchen_client, str(self.bbq.pk)), [])

    def test_a_line_fired_before_auto_print_was_switched_on_is_not_printed(self):
        order_id = self.fire(self.kebab)
        OrderItem.objects.filter(order_id=order_id).update(fired_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(self.claim(self.bbq_client, str(self.bbq.pk)), [])

    def test_all_prints_every_station_on_one_printer(self):
        self.fire(self.kebab, self.fries)
        self.assertEqual(self.printed_names(self.claim(self.bbq_client, "all")), ["Fries", "Kebab"])

    def test_a_line_moved_to_another_station_prints_there_too(self):
        order_id = self.fire(self.kebab)
        self.claim(self.bbq_client, str(self.bbq.pk))
        item_id = self.line(order_id, self.kebab).pk
        self.assertEqual(self.manager_client.post(
            f"/api/orders/{order_id}/items/{item_id}/station/", {"station": self.fryer.pk}, format="json",
        ).status_code, 200)
        self.assertEqual(self.printed_names(self.claim(self.fryer_client, str(self.fryer.pk))), ["Kebab"])

    def test_stations_and_since_are_required(self):
        for body in ({"since": self.since}, {"stations": "mine", "since": self.since}, {"stations": "all"}):
            self.assertEqual(self.bbq_client.post("/api/kds/print/", body, format="json").status_code, 400)

    def test_only_kitchen_staff_print(self):
        response = self.waiter_client.post("/api/kds/print/", {"stations": "all", "since": self.since}, format="json")
        self.assertEqual(response.status_code, 403)


# =========================================================================== 2
class StationReportTests(Phase3TestCase):
    """How long each station took — wait, cook and total — by day, hour and chef."""

    @property
    def today(self):
        return timezone.localdate(timezone=ZoneInfo(self.branch.timezone))

    def report(self, **params):
        response = self.manager_client.get("/api/reports/kitchen-stations/", {"preset": "today", **params})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def setUp(self):
        super().setUp()
        # Just after the branch's own midnight, so every line is "today".
        start, _end = branch_datetime_bounds(self.branch, self.today, self.today)
        self.t0 = start + timedelta(minutes=1)
        first = self.fire(self.kebab, self.fries)
        second = self.fire(self.kebab)
        # Kebab #1: 4 min wait, 10 min cook. Kebab #2: 2 min wait, 6 min cook.
        self.time_line(first, self.kebab, fired=self.t0, started=self.t0 + timedelta(minutes=4),
                       ready=self.t0 + timedelta(minutes=14), status="ready")
        self.time_line(second, self.kebab, fired=self.t0, started=self.t0 + timedelta(minutes=2),
                       ready=self.t0 + timedelta(minutes=8), status="ready")
        OrderItem.objects.filter(order_id__in=[first, second], product=self.kebab).update(started_by=self.bbq_chef)
        # Fries: ready with no Start recorded (an old line) — only a total time.
        self.time_line(first, self.fries, fired=self.t0, ready=self.t0 + timedelta(minutes=5), status="ready")

    def station(self, report, name):
        return next(row for row in report["stations"] if row["name_en"] == name)

    def test_each_station_says_its_average_wait_cook_and_total(self):
        bbq = self.station(self.report(), "BBQ")
        self.assertEqual((bbq["lines"], bbq["portions"]), (2, 2))
        self.assertEqual(bbq["avg_wait_min"], 3.0)
        self.assertEqual(bbq["avg_cook_min"], 8.0)
        self.assertEqual(bbq["avg_total_min"], 11.0)

    def test_a_line_without_a_start_has_only_a_total_and_says_so(self):
        fryer = self.station(self.report(), "Fryer")
        self.assertEqual(fryer["avg_total_min"], 5.0)
        self.assertEqual((fryer["total_lines"], fryer["wait_lines"], fryer["cook_lines"]), (1, 0, 0))
        self.assertIsNone(fryer["avg_wait_min"])

    def test_voided_lines_are_counted_but_not_timed(self):
        order_id = self.fire(self.kebab)
        self.time_line(order_id, self.kebab, fired=self.t0, ready=self.t0 + timedelta(minutes=60), status="voided")
        bbq = self.station(self.report(), "BBQ")
        self.assertEqual((bbq["lines"], bbq["voided"], bbq["avg_total_min"]), (3, 1, 11.0))

    def test_the_day_by_day_rows(self):
        report = self.report(preset="last_7_days")
        today = self.today.isoformat()
        bbq_today = next(row for row in report["days"] if row["day"] == today and row["name_en"] == "BBQ")
        self.assertEqual(bbq_today["avg_total_min"], 11.0)
        self.assertEqual(len([row for row in report["days"] if row["day"] == today]), 2)  # BBQ and Fryer

    def test_a_day_with_nothing_fired_is_empty(self):
        yesterday = self.report(preset="yesterday")
        self.assertEqual(yesterday["stations"], [])
        self.assertEqual(yesterday["totals"]["lines"], 0)

    def test_the_chef_who_pressed_start(self):
        chefs = self.report()["chefs"]
        self.assertEqual([(row["name"], row["lines"], row["avg_cook_min"]) for row in chefs], [("BbqChef", 2, 8.0)])

    def test_slowest_dishes_and_hours(self):
        report = self.report()
        bbq = self.station(report, "BBQ")
        self.assertEqual(bbq["slowest_dishes"][0]["name_en"], "Kebab")
        hours = [row for row in report["hours"] if row["name_en"] == "BBQ"]
        self.assertEqual(sum(row["lines"] for row in hours), 2)

    def test_one_station_or_general(self):
        only_bbq = self.report(station=str(self.bbq.pk))
        self.assertEqual([row["name_en"] for row in only_bbq["stations"]], ["BBQ"])
        order_id = self.fire(self.burger)
        self.time_line(order_id, self.burger, fired=self.t0, ready=self.t0 + timedelta(minutes=3), status="ready")
        general = self.report(station="general")
        self.assertEqual([(row["station_id"], row["avg_total_min"]) for row in general["stations"]], [(None, 3.0)])

    def test_owners_and_managers_only(self):
        self.assertEqual(self.bbq_client.get("/api/reports/kitchen-stations/").status_code, 403)
