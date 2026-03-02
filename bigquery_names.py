"""
BigQuery helpers for name suggestions.

This module provides:
- bq_name_matches: returns list[str] of matching names (legacy)
- bq_person_matches: returns list[dict] with fields: name, first_name, last_name, other_name

Auth:
- If st.secrets["gcp_service_account"] exists, it will be used (service account JSON dict).
- Otherwise, Application Default Credentials (ADC) will be used if configured.
"""

from __future__ import annotations

from typing import List, Optional, Dict

import re
import streamlit as st

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    bigquery = None  # type: ignore
    service_account = None  # type: ignore


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str, *, fallback: str) -> str:
    """Very small safety check for SQL identifiers (column names)."""
    name = (name or "").strip()
    if not name:
        return fallback
    if not _IDENT_RE.match(name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return name


@st.cache_resource(show_spinner=False)
def get_bq_client(project: Optional[str] = None):
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
    column: str = "name",
    limit: int = 25,
) -> List[str]:
    """Return matching names from BigQuery (Title Case), filtered by substring."""
    text = (text or "").strip()
    if not text:
        return []
    if bigquery is None:
        raise RuntimeError("google-cloud-bigquery is not installed. Add it to requirements.txt")

    client = get_bq_client(project=project)
    col = _safe_ident(column, fallback="name")

    sql = f"""
    SELECT DISTINCT INITCAP(LOWER(CAST({col} AS STRING))) AS name
    FROM `{project}.{dataset}.{table}`
    WHERE {col} IS NOT NULL
      AND CONTAINS_SUBSTR(LOWER(CAST({col} AS STRING)), LOWER(@text))
    ORDER BY name
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
        v = getattr(r, "name", None)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


@st.cache_data(show_spinner=False, ttl=600)
def bq_person_matches(
    text: str,
    *,
    project: str,
    dataset: str,
    table: str,
    name_col: str = "name",
    first_col: str = "first_name",
    last_col: str = "last_name",
    other_col: str = "other_name",
    limit: int = 25,
) -> List[Dict[str, str]]:
    """Return matching people rows.

    If first_name/last_name are NULL for a row, caller should prefer `name` and NOT attempt splitting.
    """
    text = (text or "").strip()
    if not text:
        return []
    if bigquery is None:
        raise RuntimeError("google-cloud-bigquery is not installed. Add it to requirements.txt")

    client = get_bq_client(project=project)

    ncol = _safe_ident(name_col, fallback="name")
    fcol = _safe_ident(first_col, fallback="first_name")
    lcol = _safe_ident(last_col, fallback="last_name")
    ocol = _safe_ident(other_col, fallback="other_name")

    sql = f"""
    SELECT DISTINCT
      INITCAP(LOWER(CAST({ncol} AS STRING))) AS name,
      INITCAP(LOWER(CAST({fcol} AS STRING))) AS first_name,
      INITCAP(LOWER(CAST({lcol} AS STRING))) AS last_name,
      INITCAP(LOWER(CAST({ocol} AS STRING))) AS other_name
    FROM `{project}.{dataset}.{table}`
    WHERE {ncol} IS NOT NULL
      AND CONTAINS_SUBSTR(LOWER(CAST({ncol} AS STRING)), LOWER(@text))
    ORDER BY name
    LIMIT @limit
    """

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("text", "STRING", text),
            bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
        ]
    )

    rows = client.query(sql, job_config=job_config).result()
    out: List[Dict[str, str]] = []
    for r in rows:
        out.append({
            "name": (getattr(r, "name", "") or "").strip(),
            "first_name": (getattr(r, "first_name", "") or "").strip(),
            "last_name": (getattr(r, "last_name", "") or "").strip(),
            "other_name": (getattr(r, "other_name", "") or "").strip(),
        })
    return out
