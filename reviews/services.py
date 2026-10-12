from orders.billing import billable_orders
from orders.models import Bill, BillStatus, OrderStatus

from .models import Review

# A guest can review once the meal is over: the bill is settled (and maybe
# already released by the cashier), and every order on it reached the table.
_SETTLED_BILL = (BillStatus.PAID, BillStatus.CLOSED)
_SERVED_ORDER = (OrderStatus.SERVED, OrderStatus.PAID, OrderStatus.CLOSED)


def guest_bill(table, phone, bill_id):
    """
    The settled bill this phone may review at this table, or None.

    Unlike orders' _customer_bill(), closed bills count: a cashier usually
    releases the table straight after payment, and the guest should still be
    able to leave a review from the screen they are looking at. Another
    phone's bill is never found, so a table token alone can't review for
    somebody else.
    """
    from customers.services import phones_match

    if not str(bill_id or "").isdigit():
        return None
    bill = (
        Bill.objects.filter(pk=int(bill_id), table=table, customer__isnull=False, status__in=_SETTLED_BILL)
        .select_related("customer", "branch")
        .prefetch_related("orders")
        .first()
    )
    if bill is None or not phones_match(bill.customer.phone, phone):
        return None
    return bill


def review_eligibility(bill):
    """(can_review, error_code) for a bill returned by guest_bill()."""
    if bill is None:
        return False, "error.reviewNotAllowed"
    if Review.all_objects.filter(bill=bill).exists():
        return False, "error.reviewAlreadySubmitted"
    orders = billable_orders(bill)
    if not orders or any(order.status not in _SERVED_ORDER for order in orders):
        return False, "error.reviewNotAllowed"
    return True, None
