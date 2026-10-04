from rest_framework.permissions import BasePermission

from .models import StaffRole


def _role(request):
    return getattr(request.user, "role", None)


class IsOwnerOrManager(BasePermission):
    """Admin CRUD, reports — matches restaurant-admin's AREA_ROLES for 'admin'."""

    def has_permission(self, request, view):
        return _role(request) in (StaffRole.OWNER, StaffRole.MANAGER)


class IsPOSStaff(BasePermission):
    """POS order/payment/table mutation — matches AREA_ROLES for 'pos'."""

    def has_permission(self, request, view):
        return _role(request) in (StaffRole.OWNER, StaffRole.MANAGER, StaffRole.CASHIER)


class IsKitchenStaff(BasePermission):
    """KDS ticket/status updates — matches AREA_ROLES for 'kds'."""

    def has_permission(self, request, view):
        return _role(request) in (StaffRole.OWNER, StaffRole.MANAGER, StaffRole.KITCHEN)


class IsWaiterStaff(BasePermission):
    """Waiter workflow actions, with managers allowed to resolve exceptions."""

    def has_permission(self, request, view):
        return _role(request) in (StaffRole.OWNER, StaffRole.MANAGER, StaffRole.WAITER)


class IsCashierStaff(BasePermission):
    """Cashier review, payment and assignment actions."""

    def has_permission(self, request, view):
        return _role(request) in (StaffRole.OWNER, StaffRole.MANAGER, StaffRole.CASHIER)


class IsTableReleaseStaff(BasePermission):
    """Cashiers and waiters can explicitly release a paid/cancelled table."""

    def has_permission(self, request, view):
        return _role(request) in (
            StaffRole.OWNER, StaffRole.MANAGER, StaffRole.CASHIER, StaffRole.WAITER
        )


class IsOrderStaff(BasePermission):
    """Staff allowed to edit an order before its items reach the kitchen."""

    def has_permission(self, request, view):
        return _role(request) in (
            StaffRole.OWNER, StaffRole.MANAGER, StaffRole.CASHIER, StaffRole.WAITER
        )
