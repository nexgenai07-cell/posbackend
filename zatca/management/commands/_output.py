"""Printing ZATCA results the same way in every command (never a secret)."""


def check_lines(result):
    verdict = "PASS" if result.passed else "FAIL"
    accepted = result.reporting_status if result.simplified else result.clearance_status
    lines = [f"[{verdict}] {result.label} ({result.number}): HTTP {result.http_status}, "
             f"status {result.status or '-'}, {'reportingStatus' if result.simplified else 'clearanceStatus'} "
             f"{accepted or '-'}"]
    if result.lines:
        lines.append("       lines sent: " + ", ".join(f"{q} x {price}" for q, price in result.lines)
                     + (f"; PayableRoundingAmount {result.payable_rounding}" if result.payable_rounding else ""))
    lines += [f"       WARNING {m['code']}: {m['message']}" for m in result.warnings]
    lines += [f"       ERROR {m['code']}: {m['message']}" for m in result.errors]
    return lines


def document_lines(document):
    accepted = document.status.upper()
    lines = [f"[{'PASS' if document.status in ('reported', 'cleared') else 'FAIL'}] "
             f"{'Simplified' if document.simplified else 'Standard'} {document.kind.replace('_', ' ')} "
             f"{document.number} (ICV {document.icv}): HTTP {document.http_status or '-'}, "
             f"status {document.zatca_status or '-'}, {accepted}"]
    if document.last_error:
        lines.append(f"       {document.last_error}")
    lines += [f"       WARNING {m['code']}: {m['message']}" for m in document.warnings]
    lines += [f"       ERROR {m['code']}: {m['message']}" for m in document.errors]
    return lines
