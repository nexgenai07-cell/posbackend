"""
Nothing to register here.

Every endpoint in this app is a read-only aggregation over orders, inventory
and catalog rows (reports/views.py + reports/filters.py) — there is no model,
which is why "Reports" has no section in the admin index. The data behind the
reports is browsable under Orders, Inventory and Catalog.
"""
