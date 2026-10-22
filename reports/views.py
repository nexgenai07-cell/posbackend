from django.db.models import Value
from django.db.models import F
from decimal import Decimal
from collections import defaultdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce, ExtractHour, TruncDate
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager
from catalog.models import Product
from inventory.models import InventoryItem, RecipeItem, StockMovement, StockMovementReason
from inventory.services import required_quantity_in_inventory_unit, variant_multiplier
from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus, Payment, PaymentStatus
from purchasing.models import Purchase, PurchaseItem, PurchaseStatus
from orders.serializers import OrderSerializer

from .filters import branch_datetime_bounds, parse_date_range
from .insights import BANDS, NEEDS_ATTENTION, attention_items, cost_rose, margin_verdict


def branch_zone(branch):
    try:
        return ZoneInfo(branch.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def sales_orders(branch, start, end):
    """
    The orders every revenue report counts: PAID (money taken), not
    cancelled, dated by WHEN THEY WERE PAID — latest payment time, falling
    back to closed/opened time for old rows without one.

    Previously only orders whose table had been RELEASED counted, dated by
    release time, so "Today" stayed empty until tables were released and an
    order paid at 23:50 but released at 00:10 landed on the next day.
    `start`/`end` are branch-timezone day bounds (branch_datetime_bounds).
    """
    last_paid = Subquery(Payment.objects.filter(order=OuterRef("pk")).order_by("-paid_at").values("paid_at")[:1])
    return (
        Order.objects.filter(branch=branch, payment_status=PaymentStatus.PAID)
        .exclude(status=OrderStatus.CANCELLED)
        .annotate(sold_at=Coalesce(last_paid, "closed_at", "opened_at"))
        .filter(sold_at__gte=start, sold_at__lt=end)
    )


class BaseReportView(APIView):
    """
    All reports are owner/manager only and share the same ?preset=/?days=/
    ?from=&to= convention (parse_date_range), with days in the BRANCH
    timezone. Revenue reports count paid orders on the day they were paid
    (sales_orders); order lists filter on opened_at in branch-local days.
    """

    permission_classes = [IsOwnerOrManager]

    def get_range(self, request):
        return parse_date_range(request)

    def bounds(self, request, from_date, to_date):
        return branch_datetime_bounds(request.user.branch, from_date, to_date)

    def closed_orders(self, request, from_date, to_date):
        """Kept under its old name: the paid orders in range (see sales_orders)."""
        start, end = self.bounds(request, from_date, to_date)
        return sales_orders(request.user.branch, start, end)


class DashboardSummaryView(BaseReportView):
    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date)

        # Money actually taken (after discounts, without tips), the same figure
        # as the sales, daily and financial reports. It used to add up menu
        # prices instead, so the dashboard and the reports disagreed.
        total_sales = Payment.objects.filter(order__in=orders).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        order_count = orders.count()
        average_order_value = (total_sales / order_count) if order_count else Decimal("0.00")

        # Still being served: not paid, closed or cancelled.
        open_orders_count = (
            Order.objects.filter(branch=request.user.branch)
            .exclude(status__in=(OrderStatus.PAID, OrderStatus.CLOSED, OrderStatus.CANCELLED))
            .count()
        )
        low_stock_count = InventoryItem.objects.filter(
            branch=request.user.branch, current_stock__lte=F("par_level")
        ).count()

        return Response({
            "from": from_date,
            "to": to_date,
            "total_sales": total_sales,
            "order_count": order_count,
            "average_order_value": average_order_value,
            "open_orders_count": open_orders_count,
            "low_stock_count": low_stock_count,
        })


class RecentOrdersView(BaseReportView):
    """
    Orders opened in range (branch-local days), newest first. Every filter is
    applied HERE, before ?limit= (default 25, max 100) — filtering the first
    25 in the browser used to drop matching orders entirely:
      ?status=  ?table=  ?method= (paid with)  ?search= (order number or table)
    """

    def get(self, request):
        from orders.codes import resolve_order_code

        from_date, to_date = self.get_range(request)
        start, end = self.bounds(request, from_date, to_date)
        try:
            limit = int(request.query_params.get("limit", 25))
        except ValueError:
            return Response({"error": "error.limitInvalid"}, status=400)
        limit = max(1, min(limit, 100))

        orders = Order.objects.filter(branch=request.user.branch, opened_at__gte=start, opened_at__lt=end)
        params = request.query_params
        if params.get("status") and params["status"] != "all":
            orders = orders.filter(status=params["status"])
        if params.get("table") and params["table"] != "all":
            orders = orders.filter(table_id=params["table"])
        if params.get("method") and params["method"] != "all":
            orders = orders.filter(payments__method=params["method"]).distinct()
        search = (params.get("search") or "").strip()
        if search:
            order_id = resolve_order_code(search)
            match = Q(table__label_en__icontains=search) | Q(table__label_ar__icontains=search)
            if order_id:
                match |= Q(pk=order_id)
            orders = orders.filter(match)
        orders = (
            orders.select_related("table", "customer", "staff")
            .prefetch_related("items", "payments", "history")
            .order_by("-opened_at", "-pk")
        )
        if "page" in params or "page_size" in params:
            from common.pagination import DefaultPagination
            from common.filters import ordering_filter
            from django.db.models import OuterRef, Subquery, Sum, DecimalField, IntegerField, CharField
            from django.db.models.functions import Coalesce
            from orders.models import OrderItem
            items = OrderItem.objects.filter(order_id=OuterRef("pk")).exclude(status="voided").values("order_id")
            amounts = items.annotate(value=Sum(F("price_snapshot") * F("quantity"))).values("value")
            quantities = items.annotate(value=Sum("quantity")).values("value")
            method = Payment.objects.filter(order_id=OuterRef("pk")).order_by("method").values("method")[:1]
            orders = orders.annotate(report_method=Coalesce(Subquery(method, output_field=CharField()), Value("unpaid")), report_total=Coalesce(Subquery(amounts, output_field=DecimalField()), Decimal("0")),
                                     report_items=Coalesce(Subquery(quantities, output_field=IntegerField()), 0))
            total = orders.aggregate(value=Sum("report_total"))["value"] or 0
            orders = ordering_filter(orders, params,
                {"order": "pk", "date": "opened_at", "table": "table__label_en", "status": "status", "source": "source", "total": "report_total", "items": "report_items", "payment": "report_method"}, ["-opened_at"])
            pager = DefaultPagination()
            page = pager.paginate_queryset(orders, request, view=self)
            response = pager.get_paginated_response(OrderSerializer(page, many=True).data)
            response.data["metadata"] = {"total_spend": total}
            return response
        return Response(OrderSerializer(orders[:limit], many=True).data)


class SalesOverviewView(BaseReportView):
    """Optional ?method= filters the by-payment-method breakdown only (the
    daily total and grand total still reflect all sales in range)."""

    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date)
        paid_orders = orders.filter(payment_status=PaymentStatus.PAID, payments__isnull=False).distinct()
        paid_payments = list(Payment.objects.filter(order__in=paid_orders).select_related("order"))
        total_sales = sum((payment.amount for payment in paid_payments), Decimal("0"))
        by_day_map = defaultdict(lambda: {"total": Decimal("0"), "orders": set()})
        branch_tz = branch_zone(request.user.branch)
        for payment in paid_payments:
            # The day the money was taken, in the branch's own timezone.
            day = payment.paid_at.astimezone(branch_tz).date().isoformat()
            by_day_map[day]["total"] += payment.amount
            by_day_map[day]["orders"].add(payment.order_id)
        by_day = [
            {
                "day": day, "total": values["total"], "order_count": len(values["orders"]),
                "average_order_value": values["total"] / len(values["orders"]) if values["orders"] else Decimal("0"),
            }
            for day, values in sorted(by_day_map.items())
        ]

        payments = paid_payments
        method_param = request.query_params.get("method")
        if method_param:
            payments = [payment for payment in payments if payment.method == method_param]
        method_amounts = defaultdict(lambda: Decimal("0"))
        for payment in payments:
            method_amounts[payment.method] += payment.amount
        last_methods = {}
        for payment in paid_payments:
            current = last_methods.get(payment.order_id)
            if current is None or (payment.paid_at, payment.pk) > (current.paid_at, current.pk):
                last_methods[payment.order_id] = payment
        method_orders = defaultdict(int)
        for payment in last_methods.values():
            method_orders[payment.method] += 1
        by_payment_method = [
            {"method": method, "total": amount, "order_count": method_orders[method]}
            for method, amount in sorted(method_amounts.items())
        ]
        total_paid_amount = total_sales

        return Response({
            "from": from_date,
            "to": to_date,
            "total_sales": total_sales,
            "by_day": by_day,
            "by_payment_method": by_payment_method,
            "paid_order_count": paid_orders.count(),
            "total_paid_amount": total_paid_amount,
        })


class ProductPerformanceView(BaseReportView):
    """One row per dish and size, with its share of dish sales. Optional
    ?category= narrows to one category."""

    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date)
        items = OrderItem.objects.filter(order__in=orders).exclude(status=OrderItemStatus.VOIDED)

        category_param = request.query_params.get("category")
        if category_param:
            items = items.filter(product__category_id=category_param)

        results = list(
            items.values("product_id", "name_en_snapshot", "name_ar_snapshot", "variant_name_en_snapshot", "variant_name_ar_snapshot")
            .annotate(quantity_sold=Sum("quantity"), revenue=Sum(F("price_snapshot") * F("quantity")))
            .order_by("-revenue")
        )
        total = sum((row["revenue"] for row in results), Decimal("0"))
        for row in results:
            row["average_price"] = row["revenue"] / row["quantity_sold"] if row["quantity_sold"] else None
            row["share_pct"] = row["revenue"] / total * 100 if total else None
        return Response({
            "from": from_date, "to": to_date, "results": results,
            "totals": {"quantity_sold": sum(row["quantity_sold"] for row in results), "revenue": total},
        })


class PeakHoursView(BaseReportView):
    """Order count by hour-of-day (0-23), summed across the whole range —
    footfall/order-volume timing, not revenue."""

    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date)

        # Hour on the restaurant's clock, not UTC (Riyadh is UTC+3).
        results = list(
            orders.annotate(hour=ExtractHour("opened_at", tzinfo=branch_zone(request.user.branch)))
            .values("hour")
            .annotate(order_count=Count("id"))
            .order_by("hour")
        )
        return Response({"from": from_date, "to": to_date, "results": results})


class MarginsView(BaseReportView):
    """
    Optional ?category= narrows to one category. Margin is revenue minus
    quantity × the product's CURRENT cost_price — OrderItem only snapshots
    the selling price, not cost, so this is not a historical cost snapshot.
    A product whose cost_price changed since a sale will show today's
    margin on yesterday's sale, not the margin actually realized at the
    time. Acceptable for now; revisit by adding a cost_price snapshot to
    OrderItem if historically-accurate margins are ever needed.
    """

    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date)
        items = OrderItem.objects.filter(order__in=orders).exclude(status=OrderItemStatus.VOIDED)

        category_param = request.query_params.get("category")
        if category_param:
            items = items.filter(product__category_id=category_param)

        rows = (
            items.values("product_id", "name_en_snapshot", "variant_name_en_snapshot")
            .annotate(
                quantity_sold=Sum("quantity"),
                revenue=Sum(F("price_snapshot") * F("quantity")),
                cost=Sum(F("quantity") * F("product__cost_price")),
            )
            .order_by("-revenue")
        )
        results = []
        for row in rows:
            revenue = row["revenue"] or Decimal("0.00")
            cost = row["cost"] or Decimal("0.00")
            results.append({**row, "margin": revenue - cost})
        return Response({"from": from_date, "to": to_date, "results": results})


class TableTurnoverView(BaseReportView):
    """Optional ?table= narrows to one table."""

    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date).exclude(table__isnull=True)

        table_param = request.query_params.get("table")
        if table_param:
            orders = orders.filter(table_id=table_param)

        rows = (
            orders.values("table_id", "table__label_en")
            .annotate(
                order_count=Count("id"),
                average_duration=Avg(ExpressionWrapper(F("closed_at") - F("opened_at"), output_field=DurationField())),
            )
            .order_by("-order_count")
        )
        results = [
            {
                "table_id": row["table_id"],
                "table_label_en": row["table__label_en"],
                "order_count": row["order_count"],
                # Explicit float, not the raw timedelta — avoids relying on
                # DRF's default duration serialization.
                "average_duration_seconds": row["average_duration"].total_seconds() if row["average_duration"] else None,
            }
            for row in rows
        ]
        return Response({"from": from_date, "to": to_date, "results": results})


def wastage(request, from_date, to_date):
    """
    Waste per ingredient, valued at each movement's own cost snapshot (the
    purchase price of that stock when it was thrown away), not the hand-typed
    cost per unit on the stock item, which purchases never update and is
    often 0. Movements from before snapshots existed are counted in quantity
    and listed as unpriced instead of guessed.
    """
    start, end = branch_datetime_bounds(request.user.branch, from_date, to_date)
    movements = StockMovement.objects.filter(
        inventory_item__branch=request.user.branch,
        reason=StockMovementReason.WASTE,
        created_at__gte=start,
        created_at__lt=end,
    ).select_related("inventory_item")
    by_item = {}
    for movement in movements:
        row = by_item.setdefault(movement.inventory_item_id, {
            "inventory_item_id": movement.inventory_item_id,
            "inventory_item_name": movement.inventory_item.name,
            "unit": movement.inventory_item.unit,
            "quantity_wasted": Decimal("0"),
            "estimated_cost": Decimal("0"),
            "unpriced_quantity": Decimal("0"),
        })
        quantity = abs(movement.quantity_delta)
        row["quantity_wasted"] += quantity
        if movement.unit_cost_snapshot is None:
            row["unpriced_quantity"] += quantity
        else:
            row["estimated_cost"] += quantity * movement.unit_cost_snapshot
    rows = sorted(by_item.values(), key=lambda row: (row["estimated_cost"], row["quantity_wasted"]), reverse=True)
    for row in rows:
        row["cost_complete"] = row["unpriced_quantity"] == 0
    return {
        "rows": rows,
        "total": sum((row["estimated_cost"] for row in rows), Decimal("0")),
        "unpriced_items": sum(1 for row in rows if not row["cost_complete"]),
    }


class WastageView(BaseReportView):
    def get(self, request):
        from_date, to_date = self.get_range(request)
        waste = wastage(request, from_date, to_date)
        return Response({
            "from": from_date, "to": to_date, "results": waste["rows"],
            "totals": {"cost": waste["total"], "unpriced_items": waste["unpriced_items"]},
        })


class RepeatCustomersView(BaseReportView):
    """
    Customers with more than one order in range. Currently always returns
    an empty result set in practice: Order.customer is only ever set by the
    public QR ordering flow (Phase 13), which doesn't exist yet — every
    order placed so far is POS-initiated with customer=None. The
    aggregation itself is verified correct against orders with a customer
    set directly (see the smoke test), just with no real data path feeding
    it until Phase 13.
    """

    def get(self, request):
        from_date, to_date = self.get_range(request)
        start, end = self.bounds(request, from_date, to_date)
        orders = (
            Order.objects.filter(
                branch=request.user.branch,
                customer__isnull=False,
                opened_at__gte=start,
                opened_at__lt=end,
            )
            .exclude(status=OrderStatus.CANCELLED)
        )
        # Spend and last-visit come from the same query rather than being left
        # for the client to invent: the page showed a hard-coded 0.00 spend and
        # a blank date for every row, which is why it read as broken.
        results = list(
            orders.values("customer_id", "customer__phone", "customer__name")
            .annotate(
                order_count=Count("id", distinct=True),
                total_spend=Sum(
                    F("items__price_snapshot") * F("items__quantity"),
                    filter=~Q(items__status=OrderItemStatus.VOIDED),
                ),
                last_order_at=Max("opened_at"),
            )
            .filter(order_count__gt=1)
            .order_by("-order_count")
        )
        for row in results:
            row["total_spend"] = row["total_spend"] or Decimal("0.00")

        # Share of orders in range that came from a returning customer — the
        # single number this report exists to answer.
        total_orders = orders.count()
        repeat_orders = sum(row["order_count"] for row in results)
        repeat_share = (
            (Decimal(repeat_orders) / Decimal(total_orders) * 100).quantize(Decimal("0.01"))
            if total_orders else Decimal("0.00")
        )

        return Response({
            "from": from_date, "to": to_date,
            "results": results,
            "repeat_order_share": repeat_share,
        })


def _financial_context(request):
    branch = request.user.branch
    from_date, to_date = parse_date_range(request)
    start, end = branch_datetime_bounds(branch, from_date, to_date)
    orders = sales_orders(branch, start, end)
    items = OrderItem.objects.filter(order__in=orders).exclude(status=OrderItemStatus.VOIDED).select_related("product")
    payments = Payment.objects.filter(order__in=orders)
    movements = StockMovement.objects.filter(
        reason=StockMovementReason.SALE,
        order__in=orders,
        inventory_item__branch=branch,
    ).select_related("inventory_item", "order_item__product")
    return branch, from_date, to_date, start, end, orders, items, payments, movements


def financial_summary(request):
    """Owner summary using completed, paid orders and the stock ledger."""
    branch, from_date, to_date, _start, _end, orders, items, payments, movements = _financial_context(request)
    revenue = sum((payment.amount for payment in payments), Decimal("0"))
    tips = sum((payment.tip or Decimal("0") for payment in payments), Decimal("0"))
    by_method = defaultdict(lambda: Decimal("0"))
    for payment in payments:
        by_method[payment.method] += payment.amount
    units_sold = sum((item.quantity for item in items), 0)

    known_cost = Decimal("0")
    movements_by_item = defaultdict(list)
    legacy_cost_movements = 0
    for movement in movements:
        if movement.unit_cost_snapshot is None:
            legacy_cost_movements += 1
        else:
            known_cost += abs(movement.quantity_delta) * movement.unit_cost_snapshot
        if movement.order_item_id:
            movements_by_item[movement.order_item_id].append(movement)
    cost_complete = legacy_cost_movements == 0
    for item in items:
        if not movements_by_item.get(item.pk):
            cost_complete = False
    ingredient_cost = known_cost if cost_complete else None
    gross_profit = revenue - ingredient_cost if ingredient_cost is not None else None
    gross_margin = (gross_profit / revenue * 100) if gross_profit is not None and revenue else None

    received = Purchase.objects.filter(
        branch=branch, status=PurchaseStatus.RECEIVED,
        received_at__gte=_start, received_at__lt=_end,
    )
    purchase_total = sum(
        (line.quantity * line.unit_cost for line in PurchaseItem.objects.filter(purchase__in=received)),
        Decimal("0"),
    )
    return {
        "from": from_date,
        "to": to_date,
        "currency": branch.currency,
        "revenue": revenue,
        "paid_order_count": orders.count(),
        "items_sold": units_sold,
        "average_order_value": revenue / orders.count() if orders.count() else Decimal("0"),
        "cash_revenue": by_method.get("cash", Decimal("0")),
        "card_revenue": by_method.get("card", Decimal("0")),
        "other_revenue": by_method.get("other", Decimal("0")),
        "tips": tips,
        "ingredient_cost_consumed": ingredient_cost,
        "known_ingredient_cost": known_cost,
        "historical_cost_complete": cost_complete,
        "unpriced_movement_count": legacy_cost_movements,
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin,
        # What is left after the ingredients we DO know the cost of; equal
        # to gross_profit when the cost is complete, higher than the truth
        # otherwise (historical_cost_complete says which).
        "known_gross_profit": revenue - known_cost,
        "known_cost_share_pct": (known_cost / revenue * 100) if revenue else None,
        "total_purchases": purchase_total,
        "received_purchase_count": received.count(),
    }


class FinancialSummaryView(BaseReportView):
    def get(self, request):
        return Response(financial_summary(request))


def _current_recipe_costs(branch):
    lines = RecipeItem.objects.filter(
        product__branch=branch, inventory_item__branch=branch,
    ).select_related("product", "inventory_item")
    costs = defaultdict(lambda: Decimal("0"))
    complete = defaultdict(lambda: True)
    seen = set()
    latest_costs = {}
    for purchase_line in PurchaseItem.objects.filter(
        purchase__branch=branch, purchase__status=PurchaseStatus.RECEIVED,
    ).select_related("purchase").order_by("inventory_item_id", "-purchase__received_at", "-id"):
        latest_costs.setdefault(purchase_line.inventory_item_id, purchase_line.unit_cost)
    for line in lines:
        seen.add(line.product_id)
        quantity = required_quantity_in_inventory_unit(line, line.inventory_item)
        unit_cost = latest_costs.get(line.inventory_item_id)
        if quantity is None or unit_cost is None:
            complete[line.product_id] = False
        else:
            costs[line.product_id] += quantity * unit_cost
    for product_id in Product.objects.filter(branch=branch).values_list("id", flat=True):
        if product_id not in seen:
            complete[product_id] = False
    return costs, complete


class ProductProfitabilityView(BaseReportView):
    """
    One row per dish AND size: "Chicken Karahi · Half KG" and "· 1 KG" are
    priced and cost differently, so averaging them into one row hid which
    size actually makes money. Historical cost comes from the SALE stock
    movements' own cost snapshots (legacy gaps stay null); today's cost per
    plate is the current recipe at the latest purchase prices times the
    size's recipe multiplier, so the two are comparable size for size.
    """

    def get(self, request):
        from_date, to_date, results, totals = profitability(request)
        return Response({"from": from_date, "to": to_date, "results": results, "totals": totals, "bands": BANDS})


def profitability(request):
    """Rows per dish and size (see ProductProfitabilityView), with the
    verdict, cost per plate and cost-rise flag, plus totals."""
    branch, from_date, to_date, _start, _end, orders, items, _payments, movements = _financial_context(request)
    current_costs, current_complete = _current_recipe_costs(branch)
    item_rows = list(items.select_related("variant"))
    by_line = defaultdict(lambda: {"units": 0, "revenue": Decimal("0"), "cost": Decimal("0"), "cost_complete": True})
    meta = {}
    for item in item_rows:
        key = (item.product_id, item.variant_id)
        row = by_line[key]
        meta.setdefault(key, item)
        row["units"] += item.quantity
        row["revenue"] += item.price_snapshot * item.quantity
    movement_item_ids = set()
    for movement in movements:
        if movement.order_item_id is None:
            if movement.unit_cost_snapshot is None:
                for row in by_line.values():
                    row["cost_complete"] = False
            continue
        movement_item_ids.add(movement.order_item_id)
        row = by_line[(movement.order_item.product_id, movement.order_item.variant_id)]
        if movement.unit_cost_snapshot is None:
            row["cost_complete"] = False
        else:
            row["cost"] += abs(movement.quantity_delta) * movement.unit_cost_snapshot
    for item in item_rows:
        if item.pk not in movement_item_ids:
            by_line[(item.product_id, item.variant_id)]["cost_complete"] = False

    results = []
    for key, values in by_line.items():
        if key not in meta:
            continue
        item = meta[key]
        product_id, variant_id = key
        multiplier = variant_multiplier(item.variant)
        cost = values["cost"] if values["cost_complete"] else None
        profit = values["revenue"] - cost if cost is not None else None
        margin = (profit / values["revenue"] * 100) if profit is not None and values["revenue"] else None
        cost_per_plate = cost / values["units"] if cost is not None and values["units"] else None
        current = current_costs.get(product_id, Decimal("0")) * multiplier if current_complete[product_id] else None
        results.append({
            "product_id": product_id,
            "variant_id": variant_id,
            "product_name": item.name_en_snapshot,
            "product_name_ar": item.name_ar_snapshot,
            "variant_name": item.variant_name_en_snapshot,
            "variant_name_ar": item.variant_name_ar_snapshot,
            "recipe_multiplier": multiplier,
            "units_sold": values["units"],
            "average_selling_price": values["revenue"] / values["units"] if values["units"] else None,
            "revenue": values["revenue"],
            "ingredient_cost": cost,
            "gross_profit_per_unit": profit / values["units"] if profit is not None and values["units"] else None,
            "gross_profit": profit,
            "gross_margin_pct": margin,
            "cost_per_plate": cost_per_plate,
            "current_recipe_cost_per_unit": current,
            "cost_rose": cost_rose(cost_per_plate, current),
            "verdict": margin_verdict(margin),
            "needs_attention": margin_verdict(margin) in NEEDS_ATTENTION,
            "historical_cost_complete": values["cost_complete"],
        })
    results.sort(key=lambda row: row["revenue"], reverse=True)
    priced = [row for row in results if row["ingredient_cost"] is not None]
    priced_revenue = sum((row["revenue"] for row in priced), Decimal("0"))
    profit_total = sum((row["gross_profit"] for row in priced), Decimal("0"))
    revenue_total = sum((row["revenue"] for row in results), Decimal("0"))
    totals = {
        "units_sold": sum(row["units_sold"] for row in results),
        "revenue": revenue_total,
        "ingredient_cost": sum((row["ingredient_cost"] for row in priced), Decimal("0")),
        # Profit only over dishes whose cost is known; the rest is listed
        # as unpriced revenue rather than counted as all profit.
        "gross_profit": profit_total,
        "priced_revenue": priced_revenue,
        "gross_margin_pct": profit_total / priced_revenue * 100 if priced_revenue else None,
        "unpriced_revenue": revenue_total - priced_revenue,
        "no_recipe_count": len(results) - len(priced),
        "attention_count": sum(1 for row in results if row["needs_attention"]),
    }
    return from_date, to_date, results, totals


class IngredientCostView(BaseReportView):
    def get(self, request):
        branch, from_date, to_date, start, end, orders, _items, _payments, movements = _financial_context(request)
        ingredients = list(InventoryItem.objects.filter(branch=branch).order_by("name"))
        purchases = Purchase.objects.filter(
            branch=branch, status=PurchaseStatus.RECEIVED,
            received_at__gte=start, received_at__lt=end,
        )
        purchase_lines = PurchaseItem.objects.filter(purchase__in=purchases).select_related("inventory_item")
        all_time_lines = PurchaseItem.objects.filter(
            purchase__branch=branch, purchase__status=PurchaseStatus.RECEIVED,
        ).select_related("purchase").order_by("inventory_item_id", "-purchase__received_at", "-id")
        latest_actual_costs = {}
        for line in all_time_lines:
            latest_actual_costs.setdefault(line.inventory_item_id, line.unit_cost)
        purchase_by_item = defaultdict(lambda: {"quantity": Decimal("0"), "spend": Decimal("0"), "weighted_quantity": Decimal("0"), "weighted_cost": Decimal("0"), "latest_cost": None, "latest_at": None})
        for line in purchase_lines:
            row = purchase_by_item[line.inventory_item_id]
            row["quantity"] += line.quantity
            row["spend"] += line.quantity * line.unit_cost
            row["weighted_quantity"] += line.quantity
            row["weighted_cost"] += line.quantity * line.unit_cost
            received_at = line.purchase.received_at
            if row["latest_at"] is None or received_at > row["latest_at"]:
                row["latest_at"] = received_at
                row["latest_cost"] = line.unit_cost
        used = defaultdict(lambda: {"quantity": Decimal("0"), "cost": Decimal("0"), "complete": True})
        for movement in movements:
            row = used[movement.inventory_item_id]
            row["quantity"] += abs(movement.quantity_delta)
            if movement.unit_cost_snapshot is None:
                row["complete"] = False
            else:
                row["cost"] += abs(movement.quantity_delta) * movement.unit_cost_snapshot

        results = []
        for ingredient in ingredients:
            purchased = purchase_by_item[ingredient.pk]
            consumed = used[ingredient.pk]
            # Nothing bought, used or on the shelf: nothing to account for.
            if not purchased["quantity"] and not consumed["quantity"] and not ingredient.current_stock:
                continue
            results.append({
                "inventory_item_id": ingredient.pk,
                "name": ingredient.name,
                "unit": ingredient.unit,
                "current_stock": ingredient.current_stock,
                "purchase_quantity": purchased["quantity"],
                "purchase_spending": purchased["spend"],
                "average_purchase_cost": purchased["weighted_cost"] / purchased["weighted_quantity"] if purchased["weighted_quantity"] else None,
                "latest_purchase_cost": purchased["latest_cost"],
                "used_quantity": consumed["quantity"],
                "consumed_cost": consumed["cost"] if consumed["complete"] else None,
                "current_stock_value_at_latest_cost": (
                    ingredient.current_stock * latest_actual_costs[ingredient.pk]
                    if ingredient.pk in latest_actual_costs else None
                ),
            })
        totals = {
            "purchase_spending": sum((row["purchase_spending"] for row in results), Decimal("0")),
            "consumed_cost": sum((row["consumed_cost"] or Decimal("0") for row in results), Decimal("0")),
            "consumed_cost_complete": all(row["used_quantity"] == 0 or row["consumed_cost"] is not None for row in results),
            "current_stock_value": sum((row["current_stock_value_at_latest_cost"] or Decimal("0") for row in results), Decimal("0")),
            "unvalued_items": sum(
                1 for row in results if row["current_stock"] > 0 and row["current_stock_value_at_latest_cost"] is None
            ),
        }
        return Response({"from": from_date, "to": to_date, "results": results, "totals": totals})


def _pct(part, whole):
    return (part / whole * 100) if part is not None and whole else None


class DailySalesView(BaseReportView):
    """
    Day-by-day owner report: for every day in range, revenue, orders, what was
    sold (product + size), what it cost, and which ingredients left stock.

    Uses the same rules as the financial summary so the numbers agree:
    - an order belongs to the branch-local day it was PAID (sales_orders);
    - revenue is the payments taken (after discounts, without tips);
    - cost is HISTORICAL — each SALE stock movement's own unit_cost_snapshot,
      never today's ingredient price.

    `cost_complete` is False when any sold item has no priced stock movement
    (no recipe, or sold before cost snapshots existed). The known cost is
    still returned so the owner sees a figure; the flag says it is partial.
    """

    def get(self, request):
        branch, from_date, to_date, _start, _end, orders, items, payments, movements = _financial_context(request)
        tz = branch_zone(branch)
        order_day = {order.pk: order.sold_at.astimezone(tz).date().isoformat() for order in orders}

        def new_day():
            return {
                "revenue": Decimal("0"), "orders": set(), "items_sold": 0,
                "products": {}, "ingredients": {}, "cost": Decimal("0"), "cost_complete": True,
            }

        days = defaultdict(new_day)

        for payment in payments:
            day = days[order_day[payment.order_id]]
            day["revenue"] += payment.amount
            day["orders"].add(payment.order_id)

        # Cost of each order item from its own SALE movements.
        item_cost = defaultdict(lambda: Decimal("0"))
        item_priced = defaultdict(lambda: True)
        item_has_movement = set()
        for movement in movements:
            day = days[order_day[movement.order_id]]
            ingredient = day["ingredients"].setdefault(movement.inventory_item_id, {
                "inventory_item_id": movement.inventory_item_id,
                "name": movement.inventory_item.name,
                "unit": movement.inventory_item.unit,
                "quantity": Decimal("0"),
                "cost": Decimal("0"),
                "cost_complete": True,
            })
            quantity = abs(movement.quantity_delta)
            ingredient["quantity"] += quantity
            if movement.unit_cost_snapshot is None:
                ingredient["cost_complete"] = False
            else:
                ingredient["cost"] += quantity * movement.unit_cost_snapshot
            if movement.order_item_id is None:
                # A sale movement not tied to an item still costs money that day.
                if movement.unit_cost_snapshot is None:
                    day["cost_complete"] = False
                else:
                    day["cost"] += quantity * movement.unit_cost_snapshot
                continue
            item_has_movement.add(movement.order_item_id)
            if movement.unit_cost_snapshot is None:
                item_priced[movement.order_item_id] = False
            else:
                item_cost[movement.order_item_id] += quantity * movement.unit_cost_snapshot

        for item in items:
            day = days[order_day[item.order_id]]
            day["items_sold"] += item.quantity
            key = (item.product_id, item.variant_name_en_snapshot)
            row = day["products"].setdefault(key, {
                "product_id": item.product_id,
                "name_en": item.name_en_snapshot,
                "name_ar": item.name_ar_snapshot,
                "variant_name_en": item.variant_name_en_snapshot,
                "variant_name_ar": item.variant_name_ar_snapshot,
                "quantity": 0,
                "revenue": Decimal("0"),
                "cost": Decimal("0"),
                "cost_complete": True,
            })
            row["quantity"] += item.quantity
            row["revenue"] += item.price_snapshot * item.quantity
            row["cost"] += item_cost[item.pk]
            day["cost"] += item_cost[item.pk]
            if item.pk not in item_has_movement or not item_priced[item.pk]:
                row["cost_complete"] = False
                day["cost_complete"] = False

        results = []
        totals = {"revenue": Decimal("0"), "orders": 0, "items_sold": 0, "cost": Decimal("0"), "cost_complete": True}
        for day_key in sorted(days, reverse=True):
            values = days[day_key]
            products = sorted(values["products"].values(), key=lambda row: row["revenue"], reverse=True)
            for row in products:
                row["profit"] = row["revenue"] - row["cost"]
                row["gross_margin_pct"] = _pct(row["profit"], row["revenue"]) if row["cost_complete"] else None
                row["verdict"] = margin_verdict(row["gross_margin_pct"])
            ingredients = sorted(values["ingredients"].values(), key=lambda row: row["cost"], reverse=True)
            profit = values["revenue"] - values["cost"]
            results.append({
                "day": day_key,
                "revenue": values["revenue"],
                "orders": len(values["orders"]),
                "items_sold": values["items_sold"],
                "ingredient_cost": values["cost"],
                "gross_profit": profit,
                "gross_margin_pct": _pct(profit, values["revenue"]),
                "verdict": margin_verdict(_pct(profit, values["revenue"])),
                "cost_complete": values["cost_complete"],
                "products": products,
                "ingredients": ingredients,
            })
            totals["revenue"] += values["revenue"]
            totals["orders"] += len(values["orders"])
            totals["items_sold"] += values["items_sold"]
            totals["cost"] += values["cost"]
            totals["cost_complete"] = totals["cost_complete"] and values["cost_complete"]

        total_profit = totals["revenue"] - totals["cost"]
        return Response({
            "from": from_date,
            "to": to_date,
            "currency": branch.currency,
            "totals": {
                "revenue": totals["revenue"],
                "orders": totals["orders"],
                "items_sold": totals["items_sold"],
                "ingredient_cost": totals["cost"],
                "gross_profit": total_profit,
                "gross_margin_pct": _pct(total_profit, totals["revenue"]),
                "cost_complete": totals["cost_complete"],
            },
            "days": results,
        })


class StaffAttendanceView(BaseReportView):
    """
    One row per staff member: on shift now, last clock-in/out, and hours
    worked on shifts that STARTED inside the range (branch-local days; an
    open shift counts up to now). Optional ?role=.

    Replaces the page fetching every staff member's full shift history one
    request at a time and comparing UTC dates in the browser.
    """

    def get(self, request):
        from django.utils import timezone as dj_timezone

        from accounts.models import Shift, Staff

        from_date, to_date = self.get_range(request)
        start, end = self.bounds(request, from_date, to_date)
        staff = Staff.objects.filter(branch=request.user.branch).order_by("name")
        role = request.query_params.get("role")
        if role and role != "all":
            staff = staff.filter(role=role)
        staff = list(staff)
        latest = {}
        for shift in Shift.objects.filter(staff__in=staff).order_by("staff_id", "-clock_in").distinct():
            latest.setdefault(shift.staff_id, shift)
        now = dj_timezone.now()
        hours = defaultdict(float)
        for shift in Shift.objects.filter(staff__in=staff, clock_in__gte=start, clock_in__lt=end):
            hours[shift.staff_id] += ((shift.clock_out or now) - shift.clock_in).total_seconds() / 3600
        results = []
        for member in staff:
            last = latest.get(member.pk)
            results.append({
                "staff_id": member.pk,
                "name": member.name,
                "role": member.role,
                "on_shift": bool(last and last.clock_out is None),
                "last_clock_in": last.clock_in if last else None,
                "last_clock_out": last.clock_out if last else None,
                "hours_in_window": round(hours.get(member.pk, 0.0), 2),
            })
        return Response({"from": from_date, "to": to_date, "results": results})


def _minutes(start, end):
    if not start or not end or end < start:
        return None
    return round((end - start).total_seconds() / 60)


def _person(staff):
    return staff.name if staff else ""


def _line(item):
    return {
        "name_en": item.name_en_snapshot,
        "name_ar": item.name_ar_snapshot,
        "variant_en": item.variant_name_en_snapshot,
        "variant_ar": item.variant_name_ar_snapshot,
        "quantity": item.quantity,
        "unit_price": item.price_snapshot,
        "total": item.price_snapshot * item.quantity,
        "voided": item.status == OrderItemStatus.VOIDED,
        "notes": item.notes,
    }


def _table(order):
    if not order.table_id:
        return None
    return {"id": order.table_id, "label_en": order.table.label_en, "label_ar": order.table.label_ar}


def _customer(order):
    if not order.customer_id:
        return None
    return {"id": order.customer_id, "name": order.customer.name, "phone": order.customer.phone}


def _with_time(entry):
    sittings = [minutes for minutes in entry.pop("sessions").values() if minutes is not None]
    entry["visits"] = len(sittings)
    entry["total_minutes"] = sum(sittings)
    entry["average_minutes"] = round(sum(sittings) / len(sittings)) if sittings else None
    # Takeaway-only customers have no sittings: their spend is per order then.
    entry["spend_per_visit"] = entry["spend"] / (len(sittings) or entry["orders"]) if entry["orders"] else Decimal("0")
    entry["spend_per_hour"] = entry["spend"] / Decimal(entry["total_minutes"]) * 60 if entry["total_minutes"] else None
    return entry


def _group_totals(rows):
    return {
        "orders": sum(row["orders"] for row in rows),
        "visits": sum(row["visits"] for row in rows),
        "total_minutes": sum(row["total_minutes"] for row in rows),
        "spend": sum((row["spend"] for row in rows), Decimal("0")),
    }


class OrderDetailsView(BaseReportView):
    """
    Every riyal per order, for the sales report's detail tabs.

    - `orders`: each paid order in range (the same set and day rule as every
      revenue report, sales_orders), with its lines, what the menu prices
      added up to, what was actually paid (the gap is the discount), tips,
      how it was paid, its ingredient cost from the stock it used, who served
      it, its table and customer, and how long the table was held.
    - `cancelled`: orders cancelled in range: when, by whom, at what stage,
      why, what they were worth, what has to be refunded and what the cooked
      food cost in wasted stock.
    - `customers` / `tables`: the same paid orders grouped, with spend and
      time seated.

    Time seated is the table session: the bill's open-to-release time when
    the order was on a table bill (several orders at one sitting share it),
    otherwise the order's own open-to-close time. Takeaway has none.
    """

    LIMIT = 3000

    def get(self, request):
        branch = request.user.branch
        from_date, to_date = self.get_range(request)
        start, end = self.bounds(request, from_date, to_date)
        orders = list(
            sales_orders(branch, start, end)
            .select_related("table", "customer", "bill", "staff", "assigned_waiter", "assigned_cashier")
            .prefetch_related("items", "payments")
            .order_by("-sold_at")[: self.LIMIT + 1]
        )
        truncated = len(orders) > self.LIMIT
        orders = orders[: self.LIMIT]

        cost_by_order = defaultdict(lambda: {"cost": Decimal("0"), "complete": True})
        for movement in StockMovement.objects.filter(order__in=[o.pk for o in orders], reason=StockMovementReason.SALE):
            row = cost_by_order[movement.order_id]
            if movement.unit_cost_snapshot is None:
                row["complete"] = False
            else:
                row["cost"] += abs(movement.quantity_delta) * movement.unit_cost_snapshot

        def session(order):
            """(key, minutes) of the sitting this order belongs to."""
            if not order.table_id:
                return None, None
            if order.bill_id:
                bill = order.bill
                return f"bill-{bill.pk}", _minutes(bill.created_at, bill.closed_at or order.closed_at or order.sold_at)
            return f"order-{order.pk}", _minutes(order.opened_at, order.closed_at or order.sold_at)

        rows = []
        customers = {}
        tables = {}
        totals = {
            "orders": 0, "items_total": Decimal("0"), "paid": Decimal("0"), "discount": Decimal("0"),
            "tips": Decimal("0"), "voided_total": Decimal("0"), "cost": Decimal("0"), "cost_complete": True,
        }
        for order in orders:
            items = list(order.items.all())
            payments = list(order.payments.all())
            live = [item for item in items if item.status != OrderItemStatus.VOIDED]
            items_total = sum((i.price_snapshot * i.quantity for i in live), Decimal("0"))
            voided_total = sum((i.price_snapshot * i.quantity for i in items if i.status == OrderItemStatus.VOIDED), Decimal("0"))
            paid = sum((p.amount for p in payments), Decimal("0"))
            tips = sum((p.tip or Decimal("0") for p in payments), Decimal("0"))
            discount = items_total - paid if items_total > paid else Decimal("0")
            costs = cost_by_order[order.pk] if order.pk in cost_by_order else {"cost": Decimal("0"), "complete": not live}
            # An order whose lines took nothing from stock has no recipe cost to show.
            cost_complete = costs["complete"]
            session_key, seated = session(order)
            methods = defaultdict(lambda: Decimal("0"))
            for payment in payments:
                methods[payment.method] += payment.amount
            rows.append({
                "id": order.pk,
                "order_number": order.order_number,
                "source": order.source,
                "table": _table(order),
                "customer": _customer(order),
                "waiter": _person(order.assigned_waiter),
                "cashier": _person(order.assigned_cashier),
                "taken_by": _person(order.staff),
                "opened_at": order.opened_at,
                "paid_at": order.sold_at,
                "closed_at": order.closed_at,
                "seated_minutes": seated,
                "items": [_line(item) for item in items],
                "plates": sum(item.quantity for item in live),
                "items_total": items_total,
                "voided_total": voided_total,
                "paid": paid,
                "discount": discount,
                "tips": tips,
                "methods": [{"method": method, "amount": amount} for method, amount in sorted(methods.items())],
                "cost": costs["cost"],
                "cost_complete": cost_complete,
                "profit": paid - costs["cost"],
            })
            totals["orders"] += 1
            totals["items_total"] += items_total
            totals["paid"] += paid
            totals["discount"] += discount
            totals["tips"] += tips
            totals["voided_total"] += voided_total
            totals["cost"] += costs["cost"]
            totals["cost_complete"] = totals["cost_complete"] and cost_complete

            if order.customer_id:
                entry = customers.setdefault(order.customer_id, {
                    **_customer(order), "orders": 0, "spend": Decimal("0"), "sessions": {}, "last_visit": order.sold_at,
                })
                entry["orders"] += 1
                entry["spend"] += paid
                entry["last_visit"] = max(entry["last_visit"], order.sold_at)
                if session_key:
                    entry["sessions"][session_key] = seated
            if order.table_id:
                entry = tables.setdefault(order.table_id, {**_table(order), "orders": 0, "spend": Decimal("0"), "sessions": {}})
                entry["orders"] += 1
                entry["spend"] += paid
                if session_key:
                    entry["sessions"][session_key] = seated

        cancelled_orders = list(
            Order.objects.filter(branch=branch, status=OrderStatus.CANCELLED, cancelled_at__gte=start, cancelled_at__lt=end)
            .select_related("table", "customer", "cancelled_by")
            .prefetch_related("items")
            .order_by("-cancelled_at")[: self.LIMIT]
        )
        waste_by_order = defaultdict(lambda: Decimal("0"))
        for movement in StockMovement.objects.filter(
            order__in=[o.pk for o in cancelled_orders], reason=StockMovementReason.WASTE, unit_cost_snapshot__isnull=False,
        ):
            waste_by_order[movement.order_id] += abs(movement.quantity_delta) * movement.unit_cost_snapshot
        cancelled = []
        for order in cancelled_orders:
            items = list(order.items.all())
            cancelled.append({
                "id": order.pk,
                "order_number": order.order_number,
                "table": _table(order),
                "customer": _customer(order),
                "opened_at": order.opened_at,
                "cancelled_at": order.cancelled_at,
                "cancelled_by": _person(order.cancelled_by),
                "from_status": order.cancelled_from_status,
                "reason": order.cancel_reason,
                # Orders cancelled before these fields existed: their lines' prices.
                "total": order.cancelled_total if order.cancelled_total is not None
                else sum((i.price_snapshot * i.quantity for i in items), Decimal("0")),
                "refunded": order.refunded_amount or Decimal("0"),
                "waste_cost": waste_by_order[order.pk],
                "items": [_line(item) for item in items],
            })

        customer_rows = sorted((_with_time(entry) for entry in customers.values()), key=lambda row: row["spend"], reverse=True)
        table_rows = sorted((_with_time(entry) for entry in tables.values()), key=lambda row: row["spend"], reverse=True)
        return Response({
            "from": from_date,
            "to": to_date,
            "truncated": truncated,
            "totals": {
                **totals,
                "cancelled_count": len(cancelled),
                "cancelled_total": sum((row["total"] for row in cancelled), Decimal("0")),
                "refunded": sum((row["refunded"] for row in cancelled), Decimal("0")),
                "cancelled_waste": sum((row["waste_cost"] for row in cancelled), Decimal("0")),
                "profit": totals["paid"] - totals["cost"],
            },
            "orders": rows,
            "cancelled": cancelled,
            "customers": customer_rows,
            "tables": table_rows,
            "customer_totals": _group_totals(customer_rows),
            "table_totals": _group_totals(table_rows),
        })


class BusinessSummaryView(BaseReportView):
    """
    The owner's one-page summary in one call: money in and where it went,
    stock bought and thrown away, best and most-sold dishes, busiest hours,
    and the "needs your attention" list -- every figure and every judgement
    made here (insights.py), so the admin only formats and translates.
    """

    def get(self, request):
        branch = request.user.branch
        from_date, to_date = self.get_range(request)
        start, end = self.bounds(request, from_date, to_date)
        money = financial_summary(request)
        _from, _to, dishes, dish_totals = profitability(request)
        waste = wastage(request, from_date, to_date)
        low_stock = list(
            InventoryItem.objects.filter(branch=branch, current_stock__lte=F("par_level"))
            .order_by("current_stock", "name").values("id", "name", "unit", "current_stock", "par_level")
        )
        cancelled_orders = Order.objects.filter(
            branch=branch, status=OrderStatus.CANCELLED, cancelled_at__gte=start, cancelled_at__lt=end,
        )
        cancelled = {
            "count": cancelled_orders.count(),
            "total": cancelled_orders.aggregate(total=Sum("cancelled_total"))["total"] or Decimal("0"),
            "refunded": cancelled_orders.aggregate(total=Sum("refunded_amount"))["total"] or Decimal("0"),
        }
        hours = list(
            self.closed_orders(request, from_date, to_date)
            .annotate(hour=ExtractHour("opened_at", tzinfo=branch_zone(branch)))
            .values("hour").annotate(order_count=Count("id")).order_by("-order_count", "hour")[:3]
        )
        top_profit = sorted(
            (row for row in dishes if row["gross_profit"] is not None and row["gross_profit"] > 0),
            key=lambda row: row["gross_profit"], reverse=True,
        )[:5]
        most_sold = sorted(dishes, key=lambda row: row["units_sold"], reverse=True)[:5]
        return Response({
            "from": from_date,
            "to": to_date,
            "money": money,
            "waste": {"total": waste["total"], "unpriced_items": waste["unpriced_items"], "item_count": len(waste["rows"])},
            "cancelled": cancelled,
            "dish_totals": dish_totals,
            "top_profit": top_profit,
            "most_sold": most_sold,
            "busiest_hours": hours,
            "low_stock": low_stock,
            "attention": attention_items(dishes, [row["name"] for row in low_stock], waste, cancelled),
        })
