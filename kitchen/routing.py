"""
How an order line reaches a kitchen station, and who in the kitchen sees it.

Routing happens once, when a line is fired: the line takes its dish's station
(the product's own, else its category's) as a snapshot on OrderItem.station.
Changing a dish's station later never moves lines already in the kitchen.

A line with no station is General — every kitchen screen sees it. So does a
line whose station has no chef on duty: nothing ever waits on a screen nobody
is looking at. "On duty" is every active kitchen chef, or — when the branch
sets kitchen_requires_clock_in — only those clocked in right now.

A cashier or head chef may move a line to another station by hand
(move_line); firing then keeps that station instead of the dish's own.
"""

from collections import defaultdict

from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Staff, StaffRole
from branches.models import Branch

from .models import KitchenStation


def stations_for_products(product_ids):
    """product id -> station id (or None for General) for each product.

    A station that is switched off is skipped: the product's own inactive
    station falls back to its category's, and an inactive category station
    falls back to General."""
    from catalog.models import Product

    rows = Product.all_objects.filter(pk__in=set(product_ids)).values(
        "pk", "station_id", "station__is_active", "category__station_id", "category__station__is_active",
    )
    resolved = {}
    for row in rows:
        if row["station_id"] and row["station__is_active"]:
            resolved[row["pk"]] = row["station_id"]
        elif row["category__station_id"] and row["category__station__is_active"]:
            resolved[row["pk"]] = row["category__station_id"]
        else:
            resolved[row["pk"]] = None
    return resolved


def station_for(product):
    """The station a new line of `product` goes to, or None for General."""
    return stations_for_products([product.pk]).get(product.pk)


def fire_items(items, now=None):
    """
    pending -> fired for `items`, routing each to its station — the dish's,
    unless someone moved the line by hand before it was sent. Every place
    that sends lines to the kitchen goes through here, so none of them can
    forget the station.

    Updates the in-memory items too, so callers can build notices from them.
    A bulk update fires no per-item signal, so the order's change is
    broadcast here once.
    """
    from orders.models import OrderItem, OrderItemStatus
    from realtime.publisher import publish_event

    items = list(items)
    if not items:
        return items
    now = now or timezone.now()
    stations = stations_for_products(item.product_id for item in items)
    # A line moved by hand keeps its station — unless that station has since
    # been switched off, when it goes to General like any other line would.
    moved_to = {item.station_id for item in items if item.station_moved_by_id and item.station_id}
    switched_off = set(KitchenStation.objects.filter(pk__in=moved_to, is_active=False).values_list("pk", flat=True))
    by_station = defaultdict(list)
    for item in items:
        item.status = OrderItemStatus.FIRED
        item.fired_at = now
        if item.station_moved_by_id is None:
            item.station_id = stations.get(item.product_id)
        elif item.station_id in switched_off:
            item.station_id = None
        by_station[item.station_id].append(item.pk)
    for station_id, pks in by_station.items():
        OrderItem.objects.filter(pk__in=pks).update(
            status=OrderItemStatus.FIRED, fired_at=now, station_id=station_id, updated_at=now,
        )
    orders = {(item.order_id, item.order.branch_id, item.order.table_id) for item in items}
    for order_id, branch_id, table_id in orders:
        publish_event(branch_id, "order:updated", order_id, table_id=table_id)
    return items


def can_run_kitchen(staff):
    """Owners, managers and head chefs: they see every station by default,
    move lines between stations and hand finished orders over."""
    if staff.role in (StaffRole.OWNER, StaffRole.MANAGER):
        return True
    return staff.role == StaffRole.KITCHEN and staff.is_head_chef


def requires_clock_in(branch_id):
    return Branch.objects.filter(pk=branch_id, kitchen_requires_clock_in=True).exists()


def on_duty_chefs(branch_id):
    """Kitchen chefs a station counts as staffed by: every active one, or
    only those clocked in when the branch says so."""
    chefs = Staff.objects.filter(branch_id=branch_id, role=StaffRole.KITCHEN, is_active=True)
    if requires_clock_in(branch_id):
        chefs = chefs.filter(shifts__clock_out__isnull=True, shifts__is_deleted=False).distinct()
    return chefs


def unstaffed_station_ids(branch, on_duty=True):
    """Stations with no chef on duty — their lines show on every kitchen
    screen. `on_duty=False` asks only whether anyone works there at all (the
    setup checklist), clocked in or not."""
    chefs = on_duty_chefs(branch.pk) if on_duty else Staff.objects.filter(
        branch=branch, role=StaffRole.KITCHEN, is_active=True,
    )
    return set(
        KitchenStation.objects.filter(branch=branch)
        .annotate(chef_count=Count("chefs", filter=Q(chefs__in=chefs.values("pk"))))
        .filter(chef_count=0)
        .values_list("pk", flat=True)
    )


class StationScope:
    """Which lines a kitchen screen treats as its own.

    `station_ids` are the stations shown; `general` adds lines with no
    station. `everything` is the All view."""

    def __init__(self, station_ids=(), general=False, everything=False):
        self.station_ids = set(station_ids)
        self.general = general
        self.everything = everything

    def includes(self, station_id):
        if self.everything:
            return True
        return station_id in self.station_ids if station_id else self.general

    def item_q(self, prefix="items__"):
        """Q matching order lines in this scope (None for All)."""
        if self.everything:
            return None
        q = Q(**{f"{prefix}station_id__in": self.station_ids})
        if self.general:
            q |= Q(**{f"{prefix}station__isnull": True})
        return q


def station_scope(staff, raw):
    """
    The scope for ?stations= on the kitchen screen:

      mine     the chef's own stations, General, and unstaffed stations
      all      every line
      1,4,general   those stations (and General when listed)

    With no value a kitchen chef gets `mine`; head chefs, owners and
    managers get `all`.
    """
    raw = (raw or "").strip().lower()
    if not raw:
        raw = "all" if can_run_kitchen(staff) else "mine"
    if raw == "all":
        return StationScope(everything=True)
    if raw == "mine":
        mine = set(staff.stations.values_list("pk", flat=True)) if staff.role == StaffRole.KITCHEN else set()
        return StationScope(mine | unstaffed_station_ids(staff.branch), general=True)
    ids, general = set(), False
    for part in raw.split(","):
        part = part.strip()
        if part == "general":
            general = True
        elif part.isdigit():
            ids.add(int(part))
    return StationScope(ids, general=general)


def kitchen_recipient_ids(branch_id, lines):
    """
    Kitchen staff who should hear about `lines`: each station's chefs on
    duty, and every kitchen chef when any line is General or its station has
    no chef on duty. Empty `lines` also means every kitchen chef. Head chefs
    hear about every line, whatever its station.
    """
    everyone = set(
        Staff.objects.filter(branch_id=branch_id, role=StaffRole.KITCHEN, is_active=True).values_list("pk", flat=True)
    )
    head_chefs = set(
        Staff.objects.filter(pk__in=everyone, is_head_chef=True).values_list("pk", flat=True)
    )
    kitchen = list(on_duty_chefs(branch_id).prefetch_related("stations"))
    station_ids = {line.station_id for line in lines}
    if not station_ids or None in station_ids:
        return everyone
    chefs_by_station = defaultdict(set)
    for chef in kitchen:
        for station in chef.stations.all():
            chefs_by_station[station.pk].add(chef.pk)
    recipients = set()
    for station_id in station_ids:
        chefs = chefs_by_station.get(station_id)
        if not chefs:
            return everyone
        recipients |= chefs
    return recipients | head_chefs


def move_line(item, station, actor):
    """
    Send one line to `station` (None = General) by hand — the cashier
    before it is sent, or a head chef while nobody has started it. Returns
    the station id it was at. The caller records and announces the move.
    """
    previous = item.station_id
    item.station = station
    item.station_moved_by = actor
    update_fields = ["station", "station_moved_by", "updated_at"]
    if previous != item.station_id and item.printed_at is not None:
        # The new station's printer has not printed it yet.
        item.printed_at = None
        update_fields.append("printed_at")
    item.save(update_fields=update_fields)
    return previous
