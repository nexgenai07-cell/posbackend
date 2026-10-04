import re

# Table.open() (tables/models.py) writes the QR payload as this relative path.
# Kept as a constant here so the writer and the parser can't drift apart.
QR_PAYLOAD_PREFIX = "/t/"

# secrets.token_urlsafe() emits the base64url alphabet, unpadded. Anything
# outside it can't be one of our tokens, so a garbage manual entry ("Table 3",
# the raw "/t/" prefix on its own, …) is rejected here instead of being sent
# off as a doomed lookup. `_`/`-` are in the set, `/` deliberately is not —
# that is what makes splitting on QR_PAYLOAD_PREFIX unambiguous.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_qr_payload(raw):
    """
    Normalises "whatever the customer's phone actually has" into the bare
    session token every public QR endpoint expects (TableBySessionView,
    PublicOrderCreateView, and the wss://…/ws/table/?session= channel all
    compare it against Table.session_token directly).

    Accepts the three real-world shapes:
      - the raw QR payload a scan returns:      "/t/<token>"
      - a bare token typed into restaurant-mobile's manual-entry fallback,
        copied from the admin/POS tables list:  "<token>"
      - a full URL, if a label was ever printed
        as one:                                 "https://host/t/<token>"

    Returns "" for anything that can't be one of our tokens, so callers treat
    "" as "not found" rather than guessing — no exception is raised, because a
    bad scan/manual entry is expected user input, not an internal error.
    """
    if raw is None:
        return ""
    value = str(raw).strip()
    if not value:
        return ""
    # A QR printed as a full link may carry a query string/fragment.
    value = value.split("?", 1)[0].split("#", 1)[0]
    # rfind, not find: keep only the LAST "/t/" so a host that happens to
    # contain the sequence (e.g. https://t.example/t/<token>) still resolves.
    # Safe because tokens themselves never contain "/" (see _TOKEN_RE).
    marker = value.rfind(QR_PAYLOAD_PREFIX)
    if marker != -1:
        value = value[marker + len(QR_PAYLOAD_PREFIX):]
    value = value.strip("/")
    if not value or not _TOKEN_RE.match(value):
        return ""
    return value
