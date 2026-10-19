from django.urls import path

from .views import (
    BillBuyerView,
    BusinessCustomersView,
    DebitNoteView,
    DeviceRecheckView,
    DocumentRetryView,
    DocumentsView,
    GoLiveStepView,
    GoLiveView,
    OrderBuyerView,
    PosStatusView,
    PublicReceiptView,
    ReceiptPrintedView,
    ReceiptsView,
    ZatcaDevicesView,
    ZatcaSettingsView,
)

urlpatterns = [
    path("zatca/settings/", ZatcaSettingsView.as_view(), name="zatca-settings"),
    path("zatca/devices/", ZatcaDevicesView.as_view(), name="zatca-devices"),
    path("zatca/devices/<int:pk>/compliance-checks/", DeviceRecheckView.as_view(), name="zatca-device-recheck"),
    path("zatca/go-live/", GoLiveView.as_view(), name="zatca-go-live"),
    path("zatca/go-live/<slug:step>/", GoLiveStepView.as_view(), name="zatca-go-live-step"),
    path("zatca/documents/", DocumentsView.as_view(), name="zatca-documents"),
    path("zatca/documents/<int:pk>/retry/", DocumentRetryView.as_view(), name="zatca-document-retry"),
    path("zatca/documents/<int:pk>/debit-note/", DebitNoteView.as_view(), name="zatca-document-debit-note"),
    path("zatca/pos-status/", PosStatusView.as_view(), name="zatca-pos-status"),
    path("zatca/receipts/", ReceiptsView.as_view(), name="zatca-receipts"),
    path("zatca/receipts/<int:pk>/printed/", ReceiptPrintedView.as_view(), name="zatca-receipt-printed"),
    path("zatca/public/receipts/<str:uuid>/", PublicReceiptView.as_view(), name="zatca-public-receipt"),
    path("zatca/business-customers/", BusinessCustomersView.as_view(), name="zatca-business-customers"),
    path("zatca/orders/<int:pk>/buyer/", OrderBuyerView.as_view(), name="zatca-order-buyer"),
    path("zatca/bills/<int:pk>/buyer/", BillBuyerView.as_view(), name="zatca-bill-buyer"),
]
