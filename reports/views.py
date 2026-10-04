from decimal import Decimal
from collections import defaultdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import ExtractHour, TruncDate
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwnerOrManager
from catalog.models import Product
from inventory.models import InventoryItem, RecipeItem, StockMovement, StockMovementReason
from inventory.services import required_quantity_in_inventory_unit
from orders.models import Order, OrderItem, OrderItemStatus, OrderStatus, Payment, PaymentStatus
from purchasing.models import Purchase, PurchaseItem, PurchaseStatus
from orders.serializers import OrderSerializer

from .filters import branch_datetime_bounds, parse_date_range


class BaseReportView(APIView):
    """
    All reports are owner/manager only and share the same ?days=&from=&to=
    convention (parse_date_range). Revenue-oriented reports (all but
    recent-orders and wastage) count only CLOSED orders — an open tab isn't
    "sales" yet — for a consistent definition across dashboard-summary,
    sales-overview, product-performance, peak-hours, margins and
    table-turnover.
    """

    permission_classes = [IsOwnerOrManager]

    def get_range(self, request):
        return parse_date_range(request)

    def closed_orders(self, request, from_date, to_date):
        start, end = branch_datetime_bounds(request.user.branch, from_date, to_date)
        return Order.objects.filter(
            branch=request.user.branch,
            status=OrderStatus.CLOSED,
        ).filter(
            Q(closed_at__gte=start, closed_at__lt=end)
            | Q(closed_at__isnull=True, opened_at__gte=start, opened_at__lt=end)
        )


class DashboardSummaryView(BaseReportView):
    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date)

        items = OrderItem.objects.filter(order__in=orders).exclude(status=OrderItemStatus.VOIDED)
        total_sales = items.aggregate(total=Sum(F("price_snapshot") * F("quantity")))["total"] or Decimal("0.00")
        order_count = orders.count()
        average_order_value = (total_sales / order_count) if order_count else Decimal("0.00")

        open_orders_count = (
            Order.objects.filter(branch=request.user.branch)
            .exclude(status__in=(OrderStatus.CLOSED, OrderStatus.CANCELLED))
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
    """?limit= (default 25, capped at 100) — the only report endpoint that
    isn't an aggregate, matching the frontend's getRecentOrders(filters, limit=25)."""

    def get(self, request):
        from_date, to_date = self.get_range(request)
        try:
            limit = int(request.query_params.get("limit", 25))
        except ValueError:
            return Response({"error": "error.limitInvalid"}, status=400)
        limit = max(1, min(limit, 100))

        orders = (
            Order.objects.filter(
                branch=request.user.branch,
                opened_at__date__gte=from_date,
                opened_at__date__lte=to_date,
            )
            .select_related("table", "customer", "staff")
            .prefetch_related("items", "payments")
            .order_by("-opened_at")[:limit]
        )
        return Response(OrderSerializer(orders, many=True).data)


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
        try:
            branch_tz = ZoneInfo(request.user.branch.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            branch_tz = ZoneInfo("UTC")
        for payment in paid_payments:
            day_value = payment.order.closed_at or payment.order.opened_at
            day = day_value.astimezone(branch_tz).date().isoformat()
            by_day_map[day]["total"] += payment.amount
            by_day_map[day]["orders"].add(payment.order_id)
        by_day = [
            {"day": day, "total": values["total"], "order_count": len(values["orders"])}
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
    """Optional ?category= narrows to one category."""

    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date)
        items = OrderItem.objects.filter(order__in=orders).exclude(status=OrderItemStatus.VOIDED)

        category_param = request.query_params.get("category")
        if category_param:
            items = items.filter(product__category_id=category_param)

        results = list(
            items.values("product_id", "name_en_snapshot")
            .annotate(quantity_sold=Sum("quantity"), revenue=Sum(F("price_snapshot") * F("quantity")))
            .order_by("-revenue")
        )
        return Response({"from": from_date, "to": to_date, "results": results})


class PeakHoursView(BaseReportView):
    """Order count by hour-of-day (0-23), summed across the whole range —
    footfall/order-volume timing, not revenue."""

    def get(self, request):
        from_date, to_date = self.get_range(request)
        orders = self.closed_orders(request, from_date, to_date)

        results = list(
            orders.annotate(hour=ExtractHour("opened_at"))
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
            items.values("product_id", "name_en_snapshot")
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


class WastageView(BaseReportView):
    def get(self, request):
        from_date, to_date = self.get_range(request)
        movements = StockMovement.objects.filter(
            inventory_item__branch=request.user.branch,
            reason=StockMovementReason.WASTE,
            created_at__date__gte=from_date,
            created_at__date__lte=to_date,
        )
        rows = (
            movements.values("inventory_item_id", "inventory_item__name", "inventory_item__cost_per_unit")
            .annotate(quantity_wasted=Sum("quantity_delta"))
            .order_by("quantity_wasted")  # most negative (most wasted) first
        )
        results = [
            {
                "inventory_item_id": row["inventory_item_id"],
                "inventory_item_name": row["inventory_item__name"],
                "quantity_wasted": abs(row["quantity_wasted"]),
                "estimated_cost": abs(row["quantity_wasted"]) * row["inventory_item__cost_per_unit"],
            }
            for row in rows
        ]
        return Response({"from": from_date, "to": to_date, "results": results})


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
        orders = (
            Order.objects.filter(
                branch=request.user.branch,
                customer__isnull=False,
                opened_at__date__gte=from_date,
                opened_at__date__lte=to_date,
            )
            .exclude(status=OrderStatus.CANCELLED)
        )
        results = list(
            orders.values("customer_id", "customer__phone", "customer__name")
            .annotate(order_count=Count("id"))
            .filter(order_count__gt=1)
            .order_by("-order_count")
        )
        return Response({"from": from_date, "to": to_date, "results": results})


def _financial_context(request):
    branch = request.user.branch
    from_date, to_date = parse_date_range(request)
    start, end = branch_datetime_bounds(branch, from_date, to_date)
    orders = Order.objects.filter(
        branch=branch,
        status=OrderStatus.CLOSED,
        payment_status=PaymentStatus.PAID,
        closed_at__gte=start,
        closed_at__lt=end,
    )
    items = OrderItem.objects.filter(order__in=orders).exclude(status=OrderItemStatus.VOIDED).select_related("product")
    payments = Payment.objects.filter(order__in=orders)
    movements = StockMovement.objects.filter(
        reason=StockMovementReason.SALE,
        order__in=orders,
        inventory_item__branch=branch,
    ).select_related("inventory_item", "order_item__product")
    return branch, from_date, to_date, start, end, orders, items, payments, movements


class FinancialSummaryView(BaseReportView):
    """Owner summary using completed, paid orders and the stock ledger."""

    def get(self, request):
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
        return Response({
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
            "total_purchases": purchase_total,
            "received_purchase_count": received.count(),
        })


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
    """Historical profit uses cost snapshots on SALE movements; legacy gaps stay null."""

    def get(self, request):
        branch, from_date, to_date, _start, _end, orders, items, _payments, movements = _financial_context(request)
        current_costs, current_complete = _current_recipe_costs(branch)
        item_rows = list(items)
        product_names = {}
        by_product = defaultdict(lambda: {"units": 0, "revenue": Decimal("0"), "cost": Decimal("0"), "cost_complete": True})
        for item in item_rows:
            row = by_product[item.product_id]
            product_names[item.product_id] = item.name_en_snapshot
            row["units"] += item.quantity
            row["revenue"] += item.price_snapshot * item.quantity
        movement_item_ids = set()
        for movement in movements:
            if movement.order_item_id is None:
                if movement.unit_cost_snapshot is None:
                    for row in by_product.values():
                        row["cost_complete"] = False
                continue
            movement_item_ids.add(movement.order_item_id)
            row = by_product[movement.order_item.product_id]
            if movement.unit_cost_snapshot is None:
                row["cost_complete"] = False
            else:
                row["cost"] += abs(movement.quantity_delta) * movement.unit_cost_snapshot
        for item in item_rows:
            if item.pk not in movement_item_ids:
                by_product[item.product_id]["cost_complete"] = False

        results = []
        for product_id, values in by_product.items():
            cost = values["cost"] if values["cost_complete"] else None
            profit = values["revenue"] - cost if cost is not None else None
            results.append({
                "product_id": product_id,
                "product_name": product_names[product_id],
                "units_sold": values["units"],
                "average_selling_price": values["revenue"] / values["units"] if values["units"] else None,
                "revenue": values["revenue"],
                "ingredient_cost": cost,
                "gross_profit_per_unit": profit / values["units"] if profit is not None and values["units"] else None,
                "gross_profit": profit,
                "gross_margin_pct": (profit / values["revenue"] * 100) if profit is not None and values["revenue"] else None,
                "current_recipe_cost_per_unit": current_costs.get(product_id) if current_complete[product_id] else None,
                "historical_cost_complete": values["cost_complete"],
            })
        results.sort(key=lambda row: row["revenue"], reverse=True)
        return Response({"from": from_date, "to": to_date, "results": results})


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
        return Response({"from": from_date, "to": to_date, "results": results})
