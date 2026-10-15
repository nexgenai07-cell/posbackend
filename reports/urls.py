from django.urls import path

from .views import (
    BusinessSummaryView,
    DailySalesView,
    DashboardSummaryView,
    FinancialSummaryView,
    IngredientCostView,
    MarginsView,
    OrderDetailsView,
    PeakHoursView,
    ProductPerformanceView,
    ProductProfitabilityView,
    RecentOrdersView,
    RepeatCustomersView,
    SalesOverviewView,
    StaffAttendanceView,
    TableTurnoverView,
    WastageView,
)

urlpatterns = [
    path("reports/business-summary/", BusinessSummaryView.as_view(), name="report-business-summary"),
    path("reports/financial-summary/", FinancialSummaryView.as_view(), name="report-financial-summary"),
    path("reports/daily-sales/", DailySalesView.as_view(), name="report-daily-sales"),
    path("reports/ingredient-costs/", IngredientCostView.as_view(), name="report-ingredient-costs"),
    path("reports/product-profitability/", ProductProfitabilityView.as_view(), name="report-product-profitability"),
    path("reports/dashboard-summary/", DashboardSummaryView.as_view(), name="report-dashboard-summary"),
    path("reports/order-details/", OrderDetailsView.as_view(), name="report-order-details"),
    path("reports/recent-orders/", RecentOrdersView.as_view(), name="report-recent-orders"),
    path("reports/sales-overview/", SalesOverviewView.as_view(), name="report-sales-overview"),
    path("reports/product-performance/", ProductPerformanceView.as_view(), name="report-product-performance"),
    path("reports/peak-hours/", PeakHoursView.as_view(), name="report-peak-hours"),
    path("reports/margins/", MarginsView.as_view(), name="report-margins"),
    path("reports/table-turnover/", TableTurnoverView.as_view(), name="report-table-turnover"),
    path("reports/wastage/", WastageView.as_view(), name="report-wastage"),
    path("reports/repeat-customers/", RepeatCustomersView.as_view(), name="report-repeat-customers"),
    path("reports/staff-attendance/", StaffAttendanceView.as_view(), name="report-staff-attendance"),
]
