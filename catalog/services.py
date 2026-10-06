from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone


def current_price(product, variant=None):
    """
    The price a product should actually be sold at right now: its active
    deal price if a deal window is open, otherwise its base price. "Now" is
    resolved in the product's own branch's IANA timezone (Branch.timezone),
    not server/UTC time — a deal window like 11:00-14:00 means the
    restaurant's local clock.

    Shared by PublicMenuView (what a customer sees on the menu) and
    AddOrderItemSerializer (what actually gets snapshotted onto an order
    item) — found via live testing that these had drifted apart: an order
    item was snapshotting the full base price even while its product's deal
    was active and being displayed on the menu, silently overcharging.
    """
    # The variant's own price is the base when one is selected; the product's
    # price is what an unvarianted product costs. Either way a deal is applied
    # on top, because deals are product-level (one deal, every size) — see the
    # requirements' D2.
    base_price = variant.price if variant is not None else product.price

    deal = getattr(product, "deal", None)
    if not deal:
        return base_price

    now = timezone.now().astimezone(ZoneInfo(product.branch.timezone))
    today = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"][now.isoweekday() % 7]
    now_time = now.time()

    for window in deal.windows.all():
        if window.day == today and window.start_time <= now_time < window.end_time:
            if variant is None:
                return deal.price
            # A product-level deal against per-variant prices can only mean a
            # proportional discount: "20% off Chicken Karahi" must take 20% off
            # every size, not flatten a 2 KG tray to the Half KG deal price.
            # Derived from the deal's own discount on the base price, so the
            # admin still configures exactly one number.
            if product.price and product.price > 0:
                ratio = deal.price / product.price
                return (variant.price * ratio).quantize(Decimal("0.01"))
            return deal.price
    return base_price


def is_available_today(product):
    """Match the public menu's branch-local weekday availability rule."""
    now = timezone.now().astimezone(ZoneInfo(product.branch.timezone))
    today = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"][now.isoweekday() % 7]
    return product.is_available and (not product.days or today in product.days)
