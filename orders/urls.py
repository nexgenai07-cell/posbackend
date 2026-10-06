from django.urls import path

from .billing_views import (
    BillBySessionView,
    BillCancelPayView,
    BillDetailView,
    BillListView,
    BillMarkPaidView,
    BillReleaseTableView,
    BillReopenView,
    BillRequestPayView,
    BillVerifyOnlineView,
    MoyasarWebhookView,
)
from .views import (
    CloseOrderView,
    CancelOrderView,
    CashierReviewView,
    AssignWaiterView,
    WaiterConfirmView,
    OrderHistoryView,
    KitchenTicketsView,
    OrderCustomerView,
    OrderDetailView,
    OrderItemDetailView,
    OrderItemsView,
    OrderItemStatusView,
    OrderListCreateView,
    OrderPaymentsView,
    PublicOrderCreateView,
    PublicQrOrderCreateView,
    PublicOrderTrackView,
    SendToKitchenView,
    TableOpenOrderView,
)

urlpatterns = [
    # -- billing: customer-facing (AllowAny, authenticated by the table's QR
    # session token, same as the public order endpoints below) --------------
    path("bills/by-session/<str:token>/", BillBySessionView.as_view(), name="bill-by-session"),
    path("bills/request-pay/", BillRequestPayView.as_view(), name="bill-request-pay"),
    path("bills/cancel-pay-request/", BillCancelPayView.as_view(), name="bill-cancel-pay"),
    path("bills/verify-online/", BillVerifyOnlineView.as_view(), name="bill-verify-online"),
    # Machine caller. Must stay reachable without auth and without the
    # customer's browser being open — this is what makes payment reliable.
    path("payments/moyasar/webhook/", MoyasarWebhookView.as_view(), name="moyasar-webhook"),

    # -- billing: cashier-facing (JWT, owner/manager/cashier) ---------------
    path("bills/", BillListView.as_view(), name="bill-list"),
    path("bills/<int:pk>/", BillDetailView.as_view(), name="bill-detail"),
    path("bills/<int:pk>/mark-paid/", BillMarkPaidView.as_view(), name="bill-mark-paid"),
    path("bills/<int:pk>/reopen/", BillReopenView.as_view(), name="bill-reopen"),
    path("bills/<int:pk>/release-table/", BillReleaseTableView.as_view(), name="bill-release-table"),

    path("orders/qr/", PublicQrOrderCreateView.as_view(), name="order-qr-create"),
    path("orders/customer/", PublicOrderCreateView.as_view(), name="order-customer-create"),
    path("orders/track/", PublicOrderTrackView.as_view(), name="order-public-track"),
    path("orders/", OrderListCreateView.as_view(), name="order-list-create"),
    path("orders/<int:pk>/", OrderDetailView.as_view(), name="order-detail"),
    path("orders/<int:pk>/history/", OrderHistoryView.as_view(), name="order-history"),
    path("orders/<int:pk>/cashier-review/", CashierReviewView.as_view(), name="order-cashier-review"),
    path("orders/<int:pk>/assign-waiter/", AssignWaiterView.as_view(), name="order-assign-waiter"),
    path("orders/<int:pk>/waiter-confirm/", WaiterConfirmView.as_view(), name="order-waiter-confirm"),
    path("orders/<int:pk>/cancel/", CancelOrderView.as_view(), name="order-cancel"),
    path("tables/<int:pk>/open-order/", TableOpenOrderView.as_view(), name="table-open-order"),
    path("orders/<int:pk>/items/", OrderItemsView.as_view(), name="order-items"),
    path("orders/<int:pk>/items/<int:item_id>/", OrderItemDetailView.as_view(), name="order-item-detail"),
    path("orders/<int:pk>/send-to-kitchen/", SendToKitchenView.as_view(), name="order-send-to-kitchen"),
    path("orders/<int:pk>/customer/", OrderCustomerView.as_view(), name="order-customer"),
    path("orders/<int:pk>/payments/", OrderPaymentsView.as_view(), name="order-payments"),
    path("orders/<int:pk>/close/", CloseOrderView.as_view(), name="order-close"),
    path("kds/tickets/", KitchenTicketsView.as_view(), name="kds-tickets"),
    path("orders/<int:pk>/items/<int:item_id>/status/", OrderItemStatusView.as_view(), name="order-item-status"),
]
