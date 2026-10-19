"""
VAT split for ZATCA invoices, exact to the halala.

Menu prices include VAT, but a ZATCA invoice states every line net of VAT and
checks its arithmetic:

  - each line:   PriceAmount x InvoicedQuantity == LineExtensionAmount
                 TaxAmount == round(LineExtensionAmount x rate)
  - each VAT category: TaxAmount == round(TaxableAmount x rate)
  - the document: TaxInclusiveAmount == TaxExclusiveAmount + VAT,
                  PayableAmount == TaxInclusiveAmount + PayableRoundingAmount

and the payable amount must be what the customer was actually charged. So per
VAT category the net total N is chosen first — the N whose N + round(N x rate)
equals the gross charged — and then the lines are made to add up to N. Unit
prices are whole halalas, so where the lines can't hit N with one unit price
each, a line is split in two (e.g. 2 x 8.70 and 1 x 8.69 for 3 x 10.00): same
dish, two invoice lines, every product of price and quantity still exact.

For about 1 gross value in 7 no N satisfies N + round(N x rate) == gross (the
VAT steps by a halala and skips one). Then the nearest N is used and the gap of
one halala goes to PayableRoundingAmount, as in ZATCA's sample "Standard
Invoice with Payable Rounding Adjustment".

All arithmetic is in integer halalas; Decimals appear only at the edges.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal


class VatCategory:
    STANDARD = "S"
    ZERO = "Z"
    EXEMPT = "E"
    OUT_OF_SCOPE = "O"

    CHOICES = [
        (STANDARD, "Standard rate"),
        (ZERO, "Zero rated"),
        (EXEMPT, "Exempt"),
        (OUT_OF_SCOPE, "Out of scope"),
    ]


_HUNDRED = Decimal(100)


@dataclass(frozen=True)
class SaleLine:
    """One order line as charged: `unit_gross` is the menu (price_snapshot) price."""

    key: object
    quantity: int
    unit_gross: Decimal
    category: str = VatCategory.STANDARD


@dataclass(frozen=True)
class InvoiceLine:
    key: object
    quantity: int
    unit_net: Decimal  # cbc:PriceAmount
    net: Decimal  # cbc:LineExtensionAmount
    vat: Decimal  # line cac:TaxTotal/cbc:TaxAmount
    total: Decimal  # line cbc:RoundingAmount
    category: str
    rate: Decimal


@dataclass(frozen=True)
class CategoryTotal:
    category: str
    rate: Decimal
    taxable: Decimal  # cac:TaxSubtotal/cbc:TaxableAmount
    vat: Decimal  # cac:TaxSubtotal/cbc:TaxAmount


@dataclass
class VatResult:
    lines: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    net_total: Decimal = Decimal("0.00")  # LineExtensionAmount and TaxExclusiveAmount
    vat_total: Decimal = Decimal("0.00")
    tax_inclusive: Decimal = Decimal("0.00")
    payable_rounding: Decimal = Decimal("0.00")
    payable: Decimal = Decimal("0.00")


def calculate(lines, *, standard_rate, prices_include_vat=True):
    """Split `lines` (SaleLine) into ZATCA invoice lines and totals."""
    standard_rate = Decimal(standard_rate)
    if len({line.key for line in lines}) != len(lines):
        raise ValueError("Sale line keys must be unique")
    groups = {}
    for line in lines:
        if line.quantity <= 0:
            raise ValueError(f"Line {line.key!r} has quantity {line.quantity}")
        if line.unit_gross < 0:
            raise ValueError(f"Line {line.key!r} has a negative price")
        rate = standard_rate if line.category == VatCategory.STANDARD else Decimal(0)
        groups.setdefault((line.category, rate), []).append(line)

    result = VatResult()
    net_total = vat_total = rounding = 0
    produced = {}
    for (category, rate), group in groups.items():
        group_lines, taxable, vat, gap = _split_group(group, rate, prices_include_vat)
        for invoice_line in group_lines:
            produced.setdefault(invoice_line.key, []).append(invoice_line)
        result.categories.append(CategoryTotal(category, rate, _money(taxable), _money(vat)))
        net_total += taxable
        vat_total += vat
        rounding += gap

    # Invoice lines in the order the sale lines came in, split parts together.
    for line in lines:
        result.lines.extend(produced[line.key])

    result.net_total = _money(net_total)
    result.vat_total = _money(vat_total)
    result.tax_inclusive = _money(net_total + vat_total)
    result.payable_rounding = _money(rounding)
    result.payable = _money(net_total + vat_total + rounding)
    return result


def vat_on(net_halalas, rate):
    """round_half_up(net x rate%), in halalas."""
    return int((Decimal(net_halalas) * rate / _HUNDRED).to_integral_value(rounding=ROUND_HALF_UP))


def _split_group(group, rate, prices_include_vat):
    """One VAT category: returns (invoice lines, taxable, vat, rounding gap) in halalas."""
    gross_units = [_halalas(line.unit_gross) for line in group]

    if not prices_include_vat or rate == 0:
        # Net prices as given: nothing to back out, nothing to round.
        lines = [_invoice_line(line, line.quantity, unit, rate) for line, unit in zip(group, gross_units)]
        taxable = sum(line.quantity * unit for line, unit in zip(group, gross_units))
        return lines, taxable, vat_on(taxable, rate), 0

    gross = sum(line.quantity * unit for line, unit in zip(group, gross_units))
    target = _target_net(gross, rate)

    # Each unit's exact net, and the whole-halala price it rounds to.
    divisor = _HUNDRED + rate
    exact = [Decimal(unit) * _HUNDRED / divisor for unit in gross_units]
    base = [int(value.to_integral_value(rounding=ROUND_HALF_UP)) for value in exact]
    delta = target - sum(line.quantity * unit for line, unit in zip(group, base))

    # Per line: how many units move by how many halalas. Units that rounded
    # furthest from their exact net move first, so prices stay as close to
    # exact as they can; free (0.00) lines never move.
    adjust = [[] for _ in group]
    if delta:
        step = 1 if delta > 0 else -1
        order = sorted(
            (i for i, unit in enumerate(gross_units) if unit > 0),
            key=lambda i: (exact[i] - base[i]) * -step,
        )
        if not order:
            raise ValueError("Cannot split VAT across lines that are all free")
        remaining = abs(delta)
        # First pass: whole lines whose quantity fits, so most lines stay unsplit.
        for i in order:
            if remaining and group[i].quantity <= remaining:
                adjust[i].append((group[i].quantity, step))
                remaining -= group[i].quantity
        # Then part of a line (one split), round and round until done.
        while remaining:
            for i in order:
                if not remaining:
                    break
                units = min(group[i].quantity, remaining)
                adjust[i].append((units, step))
                remaining -= units

    lines = []
    for i, line in enumerate(group):
        per_unit = [0] * line.quantity
        cursor = 0
        for units, step in adjust[i]:
            for k in range(units):
                per_unit[(cursor + k) % line.quantity] += step
            cursor = (cursor + units) % line.quantity
        by_price = {}
        for shift in per_unit:
            price = base[i] + shift
            by_price[price] = by_price.get(price, 0) + 1
        for price in sorted(by_price, reverse=True):
            if price < 0:
                raise ValueError(f"Line {line.key!r} would get a negative net price")
            lines.append(_invoice_line(line, by_price[price], price, rate))

    vat = vat_on(target, rate)
    return lines, target, vat, gross - target - vat


def _target_net(gross, rate):
    """The net N with N + round(N x rate) == gross, else the nearest one."""
    centre = int((Decimal(gross) * _HUNDRED / (_HUNDRED + rate)).to_integral_value(rounding=ROUND_HALF_UP))
    candidates = range(max(centre - 3, 0), centre + 4)
    exact = [n for n in candidates if n + vat_on(n, rate) == gross]
    if exact:
        return min(exact, key=lambda n: abs(n - centre))
    # No exact net: the nearest total, preferring the one just under the
    # gross (a positive rounding adjustment, never charging more VAT).
    return min(candidates, key=lambda n: (abs(gross - n - vat_on(n, rate)), n + vat_on(n, rate) > gross))


def _invoice_line(line, quantity, unit_net, rate):
    net = quantity * unit_net
    vat = vat_on(net, rate)
    return InvoiceLine(
        key=line.key,
        quantity=quantity,
        unit_net=_money(unit_net),
        net=_money(net),
        vat=_money(vat),
        total=_money(net + vat),
        category=line.category,
        rate=rate,
    )


def _halalas(amount):
    value = Decimal(amount) * 100
    if value != value.to_integral_value():
        raise ValueError(f"{amount} has more than 2 decimal places")
    return int(value)


def _money(halalas):
    return (Decimal(halalas) / _HUNDRED).quantize(Decimal("0.01"))

