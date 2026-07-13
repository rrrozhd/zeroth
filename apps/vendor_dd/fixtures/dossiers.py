"""Vendor dossiers the runbook submits — one per scenario lane."""

CLEAN_VENDOR: dict = {
    "vendor_name": "Nimbus Analytics",
    "website": "https://nimbus-analytics.example",
    "description": (
        "Product analytics SaaS ingesting anonymized clickstream events; "
        "no direct access to customer PII."
    ),
    "jurisdiction": "DE",
    "category": "product_analytics",
    "annual_spend_usd": 84000.0,
    "data_access": "internal",
    "subprocessors": ["AWS Frankfurt"],
}

RISKY_VENDOR: dict = {
    "vendor_name": "Crimson Bridge Analytics",
    "website": "https://crimson-bridge.example",
    "description": (
        "Offshore data-enrichment bureau matching customer records against "
        "third-party identity graphs; processes payroll and identity documents."
    ),
    "jurisdiction": "KY",
    "category": "data_enrichment",
    "annual_spend_usd": 1250000.0,
    "data_access": "regulated_pii",
    "subprocessors": ["Volkov Digital Services", "Cayman Hosting Ltd"],
}

# Names that hit the bundled sanctions denylist (see units.SANCTIONS_DENYLIST).
