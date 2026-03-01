"""
BigQuery helpers for name suggestions.

Supports two auth modes:
1) Streamlit secrets: st.secrets["gcp_service_account"] (service account JSON as a dict)
2) Application Default Credentials (ADC) if secrets are not provided

Docs:
- Streamlit BigQuery tutorial (secrets + service account). citeturn0search0
- BigQuery parameterized queries. citeturn0search3
- BigQuery CONTAINS_SUBSTR. citeturn0search2
"""

from __future__ import annotations

from typing import List, Optional

import streamlit as st

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    bigquery = None  # type: ignore
    service_account = None  # type: ignore


@st.cache_resource(show_spinner=False)
def get_bq_client(project: Optional[str] = None):
    """Create and cache a BigQuery client."""
    if bigquery is None:
        raise RuntimeError("google-cloud-bigquery is not installed. Add it to requirements.txt")

    creds = None
    if hasattr(st, "secrets") and "gcp_service_account" in st.secrets and st.secrets["gcp_service_account"]:
        if service_account is None:
            raise RuntimeError("google-auth is not available (service_account). Add google-auth to requirements.txt")
        creds = service_account.Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]))

    return bigquery.Client(project=project, credentials=creds)


@st.cache_data(show_spinner=False, ttl=600)
def bq_name_matches(
    text: str,
    *,
    project: str,
    dataset: str,
    table: str,
    column: str = "NAME",
    limit: int = 25,
) -> List[str]:
    """Return matching names from BigQuery (Title Case), filtered by substring."""
    text = (text or "").strip()
    if not text:
        return []
    if bigquery is None:
        raise RuntimeError("google-cloud-bigquery is not installed. Add it to requirements.txt")

    client = get_bq_client(project=project)

    sql = f"""
    SELECT DISTINCT INITCAP(LOWER(CAST({column} AS STRING))) AS NAME
    FROM `{project}.{dataset}.{table}`
    WHERE {column} IS NOT NULL
      AND CONTAINS_SUBSTR(LOWER(CAST({column} AS STRING)), LOWER(@text))
    ORDER BY NAME
    LIMIT @limit
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("text", "STRING", text),
            bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
        ]
    )

    rows = client.query(sql, job_config=job_config).result()
    out: List[str] = []
    for r in rows:
        v = getattr(r, "NAME", None)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out
