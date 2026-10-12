"""Shared Postgres connection plumbing for the worker features.

Both features write to the same Prisma-owned database and both needed the same
two things: a Neon-safe connect (some libpq builds reject the
``channel_binding=require`` parameter) and one reusable connection per thread.
Query functions and retry behaviour stay in each feature's own ``db.py``.
"""
import threading
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import psycopg
from psycopg.rows import dict_row

_tls = threading.local()


def connect(url):
    """Open a psycopg connection, dropping channel_binding if the driver balks."""
    if not url:
        raise RuntimeError("DATABASE_URL is not set; add it to .env")
    try:
        return psycopg.connect(url, row_factory=dict_row,
                               autocommit=True, connect_timeout=10)
    except Exception:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query.pop("channel_binding", None)
        fallback = urlunparse((parsed.scheme, parsed.netloc, parsed.path,
                               parsed.params, urlencode(query, doseq=True),
                               parsed.fragment))
        return psycopg.connect(fallback, row_factory=dict_row,
                               autocommit=True, connect_timeout=10)


def get_conn(url):
    """Return the calling thread's connection, opening one if needed."""
    conn = getattr(_tls, "conn", None)
    if conn is not None and conn.closed == 0:
        return conn
    _tls.conn = connect(url)
    return _tls.conn


def reset_conn():
    """Forget the thread's connection so the next call reconnects."""
    _tls.conn = None


def close_conn():
    """Close and forget the calling thread's connection."""
    conn = getattr(_tls, "conn", None)
    if conn is not None and conn.closed == 0:
        conn.close()
    _tls.conn = None
