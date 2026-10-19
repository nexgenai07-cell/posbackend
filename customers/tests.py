from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Staff, StaffRole
from branches.models import Branch

from .models import Customer


class CustomerListPagingTests(TestCase):
    """The admin pages this list on the server: one page at a time, sorted
    by the server, so every row is on exactly one page."""

    def setUp(self):
        self.branch = Branch.objects.create(name_en="Main", name_ar="Main")
        owner = Staff(branch=self.branch, name="Owner", role=StaffRole.OWNER)
        owner.set_pin("1234")
        owner.save()
        self.client = APIClient()
        self.client.force_authenticate(user=owner)
        now = timezone.now()
        for index, name in enumerate(["Bilal", "Ali", "Danish", "Comfort", "Erum"]):
            Customer.objects.create(
                branch=self.branch, phone=f"05000000{index}", name=name,
                last_order_at=now - timedelta(days=index) if index != 2 else None,
            )

    def names(self, query):
        return [row["name"] for row in self.client.get(f"/api/customers/{query}").json()["results"]]

    def test_one_page_at_a_time_with_the_total(self):
        body = self.client.get("/api/customers/?page_size=2&page=3").json()
        self.assertEqual((body["count"], len(body["results"])), (5, 1))

    def test_default_is_latest_order_first_and_never_ordered_last(self):
        self.assertEqual(self.names(""), ["Bilal", "Ali", "Comfort", "Erum", "Danish"])

    def test_sorts_by_an_allowed_field_in_both_directions(self):
        self.assertEqual(self.names("?ordering=name"), ["Ali", "Bilal", "Comfort", "Danish", "Erum"])
        self.assertEqual(self.names("?ordering=-name"), ["Erum", "Danish", "Comfort", "Bilal", "Ali"])

    def test_pages_split_the_sorted_list_without_gaps_or_repeats(self):
        pages = [self.names(f"?ordering=name&page_size=2&page={page}") for page in (1, 2, 3)]
        self.assertEqual(sum(pages, []), ["Ali", "Bilal", "Comfort", "Danish", "Erum"])

    def test_an_unknown_ordering_falls_back_to_the_default(self):
        self.assertEqual(self.names("?ordering=branch__name_en"), self.names(""))
