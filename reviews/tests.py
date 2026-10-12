"""Guest reviews from the QR pay screen, admin moderation and the public list."""

from orders.models import Bill
from orders.test_customer_session import CustomerSessionTestCase

from .models import Review, ReviewSource, ReviewStatus

PHONE = "0501234567"
OTHER_PHONE = "0509999999"


class ReviewTestCase(CustomerSessionTestCase):
    @property
    def manager_client(self):
        return self.staff_client(self.manager, "1005")

    def served_bill(self, phone=PHONE):
        """A QR order, served in full; returns (table, bill_id)."""
        table, order_id, _, body = self.order_to_ready(phone=phone)
        self.serve_everything(order_id)
        return table, body["bill_id"]

    def submit(self, table, bill_id, phone=PHONE, **extra):
        payload = {"session_token": table.session_token, "phone": phone, "bill_id": bill_id,
                   "name": "Sara", "rating": 5, "comment": "Lovely karahi"}
        payload.update(extra)
        return self.public.post("/api/reviews/qr/", payload, format="json")

    def eligibility(self, table, bill_id, phone=PHONE):
        return self.public.get("/api/reviews/qr/eligibility/",
                               {"session_token": table.session_token, "phone": phone, "bill_id": bill_id}).json()


class GuestReviewTests(ReviewTestCase):
    def test_cannot_review_before_the_bill_is_paid(self):
        table, bill_id = self.served_bill()
        self.assertFalse(self.eligibility(table, bill_id)["can_review"])
        self.assertEqual(self.submit(table, bill_id).status_code, 409)

    def test_paid_bill_can_be_reviewed_once_and_starts_pending(self):
        table, bill_id = self.served_bill()
        self.cashier_client.post(f"/api/bills/{bill_id}/mark-paid/", {"method": "cash"}, format="json")
        self.assertEqual(self.eligibility(table, bill_id), {"can_review": True, "reason": None})

        response = self.submit(table, bill_id, comment="")
        self.assertEqual(response.status_code, 201)
        review = Review.objects.get()
        self.assertEqual((review.status, review.source, review.bill_id), (ReviewStatus.PENDING, ReviewSource.GUEST, bill_id))

        again = self.submit(table, bill_id)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json(), {"error": "error.reviewAlreadySubmitted"})
        self.assertEqual(self.eligibility(table, bill_id)["reason"], "error.reviewAlreadySubmitted")

    def test_released_table_can_still_be_reviewed(self):
        table, bill_id = self.served_bill()
        self.pay_and_release(bill_id)
        self.assertEqual(Bill.objects.get(pk=bill_id).status, "closed")
        self.assertEqual(self.submit(table, bill_id).status_code, 201)

    def test_other_phone_cannot_review_someone_elses_bill(self):
        table, bill_id = self.served_bill()
        self.pay_and_release(bill_id)
        self.assertEqual(self.submit(table, bill_id, phone=OTHER_PHONE).status_code, 409)
        self.assertFalse(Review.objects.exists())

    def test_name_and_rating_are_validated(self):
        table, bill_id = self.served_bill()
        self.pay_and_release(bill_id)
        self.assertEqual(self.submit(table, bill_id, name="   ").status_code, 400)
        self.assertEqual(self.submit(table, bill_id, rating=6).status_code, 400)
        self.assertFalse(Review.objects.exists())


class ModerationTests(ReviewTestCase):
    def guest_review(self):
        table, bill_id = self.served_bill()
        self.pay_and_release(bill_id)
        self.submit(table, bill_id)
        return Review.objects.get()

    def public_names(self):
        return [row["name"] for row in self.public.get("/api/reviews/public/").json()["reviews"]]

    def test_only_approved_reviews_are_public(self):
        review = self.guest_review()
        self.assertEqual(self.public_names(), [])

        self.assertEqual(self.manager_client.post(f"/api/reviews/{review.pk}/approve/").status_code, 200)
        self.assertEqual(self.public_names(), ["Sara"])
        public_row = self.public.get("/api/reviews/public/").json()["reviews"][0]
        self.assertNotIn("customer_phone", public_row)

        self.manager_client.post(f"/api/reviews/{review.pk}/reject/")
        self.assertEqual(self.public_names(), [])

    def test_admin_added_review_is_published_immediately(self):
        response = self.manager_client.post("/api/reviews/", {"name": "Ahmed", "rating": 4, "comment": "Great"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual((response.json()["source"], response.json()["status"]), ("admin", "approved"))
        self.assertEqual(self.public_names(), ["Ahmed"])

    def test_guest_wording_is_not_editable_but_admin_reviews_are(self):
        review = self.guest_review()
        response = self.manager_client.patch(f"/api/reviews/{review.pk}/", {"comment": "edited"}, format="json")
        self.assertEqual(response.status_code, 409)

        added = self.manager_client.post("/api/reviews/", {"name": "Ahmed", "rating": 4}, format="json").json()
        response = self.manager_client.patch(f"/api/reviews/{added['id']}/", {"rating": 5}, format="json")
        self.assertEqual(response.json()["rating"], 5)

    def test_moderation_is_owner_or_manager_only(self):
        review = self.guest_review()
        self.assertEqual(self.cashier_client.get("/api/reviews/").status_code, 403)
        self.assertEqual(self.cashier_client.post(f"/api/reviews/{review.pk}/approve/").status_code, 403)
        self.assertEqual(self.public.get("/api/reviews/").status_code, 401)

    def test_summary_and_filters(self):
        self.guest_review()
        self.manager_client.post("/api/reviews/", {"name": "Ahmed", "rating": 3}, format="json")
        summary = self.manager_client.get("/api/reviews/summary/").json()
        self.assertEqual(summary, {"pending": 1, "approved": 1, "rejected": 0, "all": 2})
        pending = self.manager_client.get("/api/reviews/", {"status": "pending"}).json()
        self.assertEqual([row["name"] for row in pending["results"]], ["Sara"])
        self.assertEqual(pending["results"][0]["customer_phone"], PHONE)
        three = self.manager_client.get("/api/reviews/", {"rating": "3"}).json()
        self.assertEqual([row["name"] for row in three["results"]], ["Ahmed"])

    def test_deleted_guest_review_does_not_reopen_the_bill(self):
        review = self.guest_review()
        self.assertEqual(self.manager_client.delete(f"/api/reviews/{review.pk}/").status_code, 204)
        self.assertEqual(self.submit(review.bill.table, review.bill_id).status_code, 409)
