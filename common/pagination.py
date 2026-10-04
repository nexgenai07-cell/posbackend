from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    """
    DRF's page-number pagination, with a client-settable page size.

    The default stays 25 (settings.PAGE_SIZE), which is what every existing
    caller already gets. Screens that render a whole menu grid, table map or
    report at once ask for more via ?page_size= — capped so one request can't
    ask for the entire table.

    Purely additive: ?page_size was ignored until this landed (settings.py's
    comment claimed "?page=/?page_size= for free", but the plain
    PageNumberPagination it used has no page_size_query_param), so no existing
    caller changes behaviour.
    """

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 200
