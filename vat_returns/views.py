from datetime import date

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsOwner, IsOwnerOrManager
from zatca.environments import profile
from zatca.models import VatFilingFrequency, ZatcaSettings

from . import adjustments, exports, figures, filing as vat_filing, history as vat_history, payments as vat_payments
from . import reminders as vat_reminders
from . import periods as vat_periods
from .models import VatAuditLog, VatPayment, VatPeriod, VatRefund

# The VAT filing settings, as stored on ZatcaSettings.
SETTINGS_FIELDS = ("vat_filing_frequency", "vat_frequency_effective_from", "vat_registration_date",
                   "vat_tracking_start", "vat_reminder_days")
# Changing any of these moves the period dates, so the periods are rebuilt
# (only while nothing is filed; a frequency change after that takes an
# effective date instead).
PERIOD_FIELDS = ("vat_filing_frequency", "vat_registration_date", "vat_tracking_start")


def _settings_dict(settings):
    periods = vat_periods.ensure(settings.branch, settings)
    filed = any(period.locked for period in periods)
    return {
        "frequency": settings.vat_filing_frequency,
        # The frequency of the latest period, and from when the setting
        # above applies, when a switch is scheduled.
        "current_frequency": periods[-1].frequency if periods else None,
        "frequency_effective_from": settings.vat_frequency_effective_from,
        "periods_filed": filed,
        # Once a period is filed, a switch takes effect on a quarter start
        # from this date on.
        "earliest_switch": vat_periods.earliest_switch(periods) if filed else None,
        "registration_date": settings.vat_registration_date,
        "tracking_start": settings.vat_tracking_start,
        "reminder_days": settings.vat_reminder_days,
        "first_period_start": vat_periods.first_start(settings),
        "environment": settings.environment,
        "is_test": profile(settings.environment).is_test,
    }


def _parse(data):
    """(values by model field, errors) from the PATCH body. Only the keys
    present are changed; dates may be null to clear them."""
    values, errors = {}, {}
    if "frequency" in data:
        if data["frequency"] in VatFilingFrequency.values:
            values["vat_filing_frequency"] = data["frequency"]
        else:
            errors["frequency"] = "error.vatFrequencyInvalid"
    for key, field in (("registration_date", "vat_registration_date"), ("tracking_start", "vat_tracking_start")):
        if key not in data:
            continue
        raw = data[key]
        if raw in (None, ""):
            values[field] = None
            continue
        try:
            values[field] = date.fromisoformat(str(raw))
        except ValueError:
            errors[key] = "error.dateInvalid"
    if "reminder_days" in data:
        try:
            days = int(data["reminder_days"])
        except (TypeError, ValueError):
            days = -1
        if 1 <= days <= 60:
            values["vat_reminder_days"] = days
        else:
            errors["reminder_days"] = "error.vatReminderDaysInvalid"
    return values, errors


class VatSettingsView(APIView):
    """
    GET/PATCH the branch's VAT filing settings: frequency, VAT registration
    date, the date to track VAT returns from, and reminder days. Owners and
    managers read them; only the owner changes them. A change to the dates
    or frequency rebuilds the periods, which is refused once one is filed;
    from then on a frequency change takes `frequency_effective_from` (a
    quarter start from `earliest_switch` on). Every change is written to
    the VAT audit log.
    """

    def get_permissions(self):
        return [IsOwner()] if self.request.method == "PATCH" else [IsOwnerOrManager()]

    def get(self, request):
        return Response(_settings_dict(ZatcaSettings.for_branch(request.user.branch)))

    def patch(self, request):
        branch = request.user.branch
        settings = ZatcaSettings.for_branch(branch)
        values, errors = _parse(request.data)
        now = vat_periods.today(settings)
        tracking = values.get("vat_tracking_start", settings.vat_tracking_start)
        if "tracking_start" not in errors and tracking and tracking > now:
            errors["tracking_start"] = "error.vatTrackingStartInFuture"
        periods = vat_periods.ensure(branch, settings)
        filed = any(period.locked for period in periods)
        if not errors:
            _schedule_switch(request.data, values, errors, settings, periods, filed)
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        before = {field: getattr(settings, field) for field in SETTINGS_FIELDS}
        changed = {field: value for field, value in values.items() if before[field] != value}
        if not changed:
            return Response(_settings_dict(settings))
        # Once filed, a frequency change is scheduled instead (_schedule_switch).
        rebuild_fields = set(PERIOD_FIELDS) - ({"vat_filing_frequency"} if filed else set())
        moves_periods = bool(rebuild_fields.intersection(changed))
        try:
            with transaction.atomic():
                for field, value in changed.items():
                    setattr(settings, field, value)
                settings.save(update_fields=[*changed, "updated_at"])
                VatAuditLog.objects.create(
                    branch=branch, actor=request.user, action="settings_changed",
                    summary=", ".join(sorted(changed)),
                    data={"before": {f: _json(before[f]) for f in changed},
                          "after": {f: _json(v) for f, v in changed.items()}},
                )
                if moves_periods:
                    vat_periods.rebuild(branch, settings, actor=request.user)
        except vat_periods.PeriodsLocked:
            # Rolled back as a whole: filed periods keep the dates they were filed with.
            return Response({"error": "error.vatPeriodsFiled"}, status=status.HTTP_409_CONFLICT)
        return Response(_settings_dict(settings))


def _schedule_switch(data, values, errors, settings, periods, filed):
    """A frequency change. While nothing is filed it applies from the
    first period (the periods are rebuilt). Once a period is filed it
    needs `frequency_effective_from`: a quarter start on or after
    earliest_switch(); the periods before it keep their frequency.
    Changing back to the current frequency cancels a scheduled switch."""
    if "vat_filing_frequency" not in values and "frequency_effective_from" not in data:
        return
    if not filed:
        values["vat_frequency_effective_from"] = None
        return
    target = values.get("vat_filing_frequency", settings.vat_filing_frequency)
    if target == periods[-1].frequency:
        values["vat_frequency_effective_from"] = None
        return
    try:
        effective = date.fromisoformat(str(data.get("frequency_effective_from") or ""))
    except ValueError:
        errors["frequency_effective_from"] = "error.vatSwitchDateRequired"
        return
    if not vat_periods.quarter_start(effective):
        errors["frequency_effective_from"] = "error.vatSwitchNotQuarterStart"
    elif effective < vat_periods.earliest_switch(periods):
        errors["frequency_effective_from"] = "error.vatSwitchTooEarly"
    else:
        values["vat_frequency_effective_from"] = effective


def _json(value):
    return value.isoformat() if isinstance(value, date) else value


class VatPeriodsView(APIView):
    """All periods of the branch's current environment, newest first, with
    the current one marked. Sandbox/Simulation periods come back with
    is_test; Production never lists them."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        branch = request.user.branch
        settings = ZatcaSettings.for_branch(branch)
        periods = vat_periods.ensure(branch, settings)
        rows = [vat_periods.as_dict(period, settings) for period in reversed(periods)]
        current = next((row["id"] for row in rows if row["is_current"]), None)
        return Response({
            "settings": _settings_dict(settings),
            "setup_needed": vat_periods.first_start(settings) is None,
            "today": vat_periods.today(settings),
            "current": current,
            "periods": rows,
        })


def _period(request, pk):
    """(period, settings): a period of the user's branch in its current
    environment, 404 otherwise."""
    settings = ZatcaSettings.for_branch(request.user.branch)
    period = get_object_or_404(VatPeriod, pk=pk, branch=request.user.branch, environment=settings.environment)
    return period, settings


def _period_detail(period, settings):
    filing = figures.current_filing(period)
    data = figures.compute(period, settings, filing=filing)
    return {
        **vat_periods.as_dict(period, settings),
        # Only the first period takes an opening credit.
        "is_first": not VatPeriod.objects.filter(branch=period.branch, environment=period.environment,
                                                 start_date__lt=period.start_date).exists(),
        "figures": data,
        "checklist": vat_filing.checklist(period, settings, data),
        "filing": vat_filing.filing_dict(filing),
        "filings": vat_filing.filings_of(period),
        "payments": vat_payments.payments_of(period),
        "refunds": vat_payments.refunds_of(period),
    }


class VatPeriodView(APIView):
    """One period of the branch's current environment, with its cards,
    return boxes and warnings (vat_returns/figures.py), the ready-to-file
    checklist and the filing, if any."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, pk):
        period, settings = _period(request, pk)
        return Response(_period_detail(period, settings))


class VatPeriodFileView(APIView):
    """POST (owner): Mark as filed. Body: filed_on, zatca_amount,
    difference_reason (when it differs from ours), credit_treatment (when
    negative), attachment_url/name, confirm_warnings. Returns the period."""

    permission_classes = [IsOwner]

    def post(self, request, pk):
        period, settings = _period(request, pk)
        try:
            vat_filing.file_period(period, settings, request.user, request.data)
        except vat_filing.FilingRejected as exc:
            return Response(exc.body, status=exc.status)
        period.refresh_from_db()
        return Response(_period_detail(period, settings), status=status.HTTP_201_CREATED)


class VatPeriodAmendView(APIView):
    """POST (owner): record an amended return filed on the portal. Body as
    for filing, plus amendment_reason. The new filing supersedes the
    current one, which stays listed. Returns the period."""

    permission_classes = [IsOwner]

    def post(self, request, pk):
        period, settings = _period(request, pk)
        try:
            vat_filing.file_period(period, settings, request.user, request.data, amend=True)
        except vat_filing.FilingRejected as exc:
            return Response(exc.body, status=exc.status)
        period.refresh_from_db()
        return Response(_period_detail(period, settings), status=status.HTTP_201_CREATED)


class VatPeriodAdjustmentView(APIView):
    """POST (owner) {kind: opening_credit | correction, amount, reason}:
    the first period's opening credit, or a box 14 correction, before the
    period is filed. Returns the period."""

    permission_classes = [IsOwner]

    def post(self, request, pk):
        period, settings = _period(request, pk)
        try:
            period = adjustments.set_adjustment(period, request.user, request.data)
        except vat_filing.FilingRejected as exc:
            return Response(exc.body, status=exc.status)
        return Response(_period_detail(period, settings))


class VatPeriodPaymentsView(APIView):
    """POST (owner): record a SADAD payment of a filed period. Body: amount,
    paid_on, sadad_reference, receipt_url/name. Returns the period."""

    permission_classes = [IsOwner]

    def post(self, request, pk):
        period, settings = _period(request, pk)
        try:
            vat_payments.record_payment(period, settings, request.user, request.data)
        except vat_filing.FilingRejected as exc:
            return Response(exc.body, status=exc.status)
        period.refresh_from_db()
        return Response(_period_detail(period, settings), status=status.HTTP_201_CREATED)


class VatPeriodRefundsView(APIView):
    """POST (owner): record a refund ZATCA paid for a period filed with a
    credit and "refund requested". Body: amount, received_on, reference,
    attachment_url/name. Returns the period."""

    permission_classes = [IsOwner]

    def post(self, request, pk):
        period, settings = _period(request, pk)
        try:
            vat_payments.record_refund(period, settings, request.user, request.data)
        except vat_filing.FilingRejected as exc:
            return Response(exc.body, status=exc.status)
        return Response(_period_detail(period, settings), status=status.HTTP_201_CREATED)


class VoidView(APIView):
    """POST (owner) {reason}: void a payment or refund. It stays listed,
    struck through. Returns its period."""

    permission_classes = [IsOwner]
    model = None

    def post(self, request, pk):
        settings = ZatcaSettings.for_branch(request.user.branch)
        record = get_object_or_404(self.model, pk=pk, period__branch=request.user.branch,
                                   period__environment=settings.environment)
        try:
            vat_payments.void(record, request.user, request.data.get("reason"))
        except vat_filing.FilingRejected as exc:
            return Response(exc.body, status=exc.status)
        period = VatPeriod.objects.get(pk=record.period_id)
        return Response(_period_detail(period, settings))


class VatPaymentVoidView(VoidView):
    model = VatPayment


class VatRefundVoidView(VoidView):
    model = VatRefund


def _download(content, content_type, filename):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _audit_export(request, action, summary, period=None, **data):
    VatAuditLog.objects.create(branch=request.user.branch, period=period, actor=request.user, action=action,
                               summary=summary, data=data)


class VatRemindersView(APIView):
    """GET: periods due within the reminder days, or overdue, that are not
    filed or not fully paid (Production only). Shown as a banner on every
    admin page."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        settings = ZatcaSettings.for_branch(request.user.branch)
        return Response({"reminders": vat_reminders.reminders(request.user.branch, settings)})


class VatHistoryView(APIView):
    """GET: one row per period of the current environment, newest first,
    with the same figures as each period's cards (vat_returns/history.py)."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        settings = ZatcaSettings.for_branch(request.user.branch)
        return Response({"settings": _settings_dict(settings),
                         "rows": vat_history.rows(request.user.branch, settings)})


class VatHistoryCsvView(APIView):
    """GET: the history as CSV for the accountant (UTF-8 with BOM)."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        branch = request.user.branch
        settings = ZatcaSettings.for_branch(branch)
        rows = vat_history.rows(branch, settings)
        _audit_export(request, "history_exported", "VAT history exported (CSV)", format="csv", periods=len(rows))
        return _download(exports.history_csv(branch, settings, rows).encode("utf-8"), "text/csv; charset=utf-8",
                         f"vat-history-{settings.environment}.csv")


class VatHistoryPdfView(APIView):
    """GET: the history as a one-table PDF."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        branch = request.user.branch
        settings = ZatcaSettings.for_branch(branch)
        rows = vat_history.rows(branch, settings)
        _audit_export(request, "history_exported", "VAT history exported (PDF)", format="pdf", periods=len(rows))
        return _download(exports.history_pdf(branch, settings, rows), "application/pdf",
                         f"vat-history-{settings.environment}.pdf")


class VatPeriodPdfView(APIView):
    """GET: one period as a PDF: cards, return boxes, filing, payments."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, pk):
        period, settings = _period(request, pk)
        detail = _period_detail(period, settings)
        refund = detail["figures"]["refund"]
        detail.update(name=vat_periods.label_text(period), refund_state=refund["state"] if refund else None,
                      overdue=period.status in ("open", "filed") and detail["days_left"] < 0)
        _audit_export(request, "period_pdf_exported", f"{detail['name']} exported (PDF)", period=period, format="pdf")
        return _download(exports.period_pdf(request.user.branch, settings, detail), "application/pdf",
                         f"vat-return-{period.start_date:%Y-%m-%d}-{period.end_date:%Y-%m-%d}.pdf")


class VatPeriodAuditView(APIView):
    """GET: the period's activity (filing, overrides, ...), newest first."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, pk):
        period, _settings = _period(request, pk)
        rows = VatAuditLog.objects.filter(period=period).select_related("actor")[:200]
        return Response([{
            "id": row.pk, "action": row.action, "summary": row.summary, "data": row.data,
            "actor": row.actor.name if row.actor else "", "created_at": row.created_at,
        } for row in rows])


class VatPeriodDocumentsView(APIView):
    """GET ?list=output|not_counted|input|review&page=N: the documents behind
    a card, 100 a page, with totals over the whole list. Amounts are signed
    (credit notes negative) so the rows add up to the card."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, pk):
        period, settings = _period(request, pk)
        which = request.query_params.get("list", "")
        if which not in figures.LISTS:
            return Response({"list": "error.vatListInvalid"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            page = int(request.query_params.get("page", 1))
        except ValueError:
            page = 1
        return Response(figures.document_list(period, settings, which, page))
