"""Database access shared by scripts and notebooks.

    from fbb.db import read_sql, read_query
    df = read_sql("SELECT * FROM core.teams WHERE season = 2027")
    df = read_query("standings.sql", season=2027)

Connects to DATABASE_URL when it's set (hosted Postgres such as Neon), otherwise to
the local Docker database from the POSTGRES_* settings in .env.

Reads (read_sql, read_query) use one of two transports, chosen by FBB_DB_TRANSPORT:
  tcp   a normal Postgres connection through SQLAlchemy/psycopg2
  http  Neon's SQL-over-HTTP endpoint (POST https://<host>/sql), for machines that
        can only reach the internet through an HTTPS proxy (Claude cloud sessions)
  auto  (default) http when DATABASE_URL is a Neon host and port 5432 can't be
        reached, otherwise tcp
Writes (refresh, ingest) always use tcp through `engine` / get_conn().
"""
import datetime as dt
import os
import re
import socket
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import psycopg2
import requests
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from fbb import config


def _libpq_url(url):
    """A DATABASE_URL in the form libpq/psycopg2 accepts (postgresql://...)."""
    return re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql://", url)


def _sqlalchemy_url():
    if config.DATABASE_URL:
        return _libpq_url(config.DATABASE_URL).replace("postgresql://", "postgresql+psycopg2://", 1)
    # URL.create quotes special characters in the password
    return URL.create(
        "postgresql+psycopg2",
        username=config.DB_USER, password=config.DB_PASSWORD,
        host=config.DB_HOST, port=config.DB_PORT, database=config.DB_NAME,
    )


# pool_pre_ping: hosted databases (Neon) suspend when idle and drop open connections
engine = create_engine(_sqlalchemy_url(), pool_pre_ping=True)


def get_conn():
    """A raw psycopg2 connection (for bulk inserts in the ingest script)."""
    if config.DATABASE_URL:
        return psycopg2.connect(_libpq_url(config.DATABASE_URL))
    return psycopg2.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        dbname=config.DB_NAME, user=config.DB_USER, password=config.DB_PASSWORD,
    )


def sql_literal(value):
    """A Python value as a SQL literal for a params CTE (text or integer)."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'::text"
    return str(int(value))


def read_sql_file(path, **params):
    """The text of a .sql file. Keyword arguments replace the values in its
    `params` CTE (the first `params AS ( SELECT ... )` block), so the file
    still runs on its own in a SQL editor with its default values."""
    sql = Path(path).read_text()
    if params:
        cols = ", ".join(f"{sql_literal(v)} AS {k}" for k, v in params.items())
        sql, n = re.subn(r"params AS \(\s*SELECT .*?\n\)", f"params AS (\n    SELECT {cols}\n)",
                         sql, count=1, flags=re.S)
        if n != 1:
            raise ValueError(f"{path}: no params CTE to replace")
    return sql


# --- reads ---------------------------------------------------------------------

def _neon_host():
    """The DATABASE_URL host when it's a Neon endpoint, else None."""
    host = urlparse(_libpq_url(config.DATABASE_URL)).hostname if config.DATABASE_URL else None
    return host if host and host.endswith(".neon.tech") else None


def _tcp_reachable(host, port=5432, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@cache
def transport():
    """'tcp' or 'http': how read_sql reaches the database (see module docstring)."""
    choice = os.environ.get("FBB_DB_TRANSPORT", "auto").strip().lower()
    if choice not in ("auto", "tcp", "http"):
        raise ValueError(f"FBB_DB_TRANSPORT must be auto, tcp or http, not {choice!r}")
    if choice == "http" and not _neon_host():
        raise ValueError("FBB_DB_TRANSPORT=http needs DATABASE_URL to point at a Neon (*.neon.tech) host")
    if choice == "auto":
        host = _neon_host()
        choice = "http" if host and not _tcp_reachable(host) else "tcp"
    return choice


# Neon's HTTP API already returns int4, float, bool, json and arrays as JSON values;
# these types come back as strings, so convert them the way psycopg2 + pd.read_sql would.
_HTTP_CONVERTERS = {
    20: int,                       # int8 / bigint (count(*), sum(int))
    1700: float,                   # numeric (pd.read_sql coerces Decimal to float)
    1082: dt.date.fromisoformat,   # date
}
_TIMESTAMP_TYPES = {1114: False, 1184: True}   # timestamp, timestamptz -> utc?


class NeonHTTPError(RuntimeError):
    pass


def _http_rows_to_frame(result):
    """A DataFrame from a Neon /sql response made with Neon-Array-Mode: true."""
    fields = result.get("fields") or []
    names = [f["name"] for f in fields]
    convs = [_HTTP_CONVERTERS.get(f["dataTypeID"]) for f in fields]
    rows = [
        [v if v is None or c is None else c(v) for v, c in zip(row, convs)]
        for row in result.get("rows") or []
    ]
    df = pd.DataFrame.from_records(rows, columns=names, coerce_float=True)
    for i, f in enumerate(fields):
        if f["dataTypeID"] in _TIMESTAMP_TYPES:
            df.isetitem(i, pd.to_datetime(df.iloc[:, i], utc=_TIMESTAMP_TYPES[f["dataTypeID"]], format="ISO8601"))
    return df


def _read_sql_http(sql, timeout=120):
    url = _libpq_url(config.DATABASE_URL)
    for attempt in (1, 2):   # one retry for errors Neon flags as retryable (e.g. compute waking up)
        resp = requests.post(
            f"https://{_neon_host()}/sql",
            headers={"Neon-Connection-String": url, "Neon-Array-Mode": "true"},
            json={"query": sql, "params": []},
            timeout=timeout,
        )
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.ok:
            return _http_rows_to_frame(body)
        if attempt == 1 and body.get("neon:retryable"):
            continue
        msg = body.get("message") or resp.text[:500]
        raise NeonHTTPError(f"Neon HTTP {resp.status_code} [{body.get('code', '?')}]: {msg}")


def read_sql(sql):
    """Run a SELECT and return a DataFrame, over tcp or http (see transport()).
    Use this instead of pd.read_sql(sql, engine) so code also runs where only HTTPS
    is reachable. Write "%" as-is (LIKE 'a%'); it's escaped for psycopg2 here."""
    if transport() == "http":
        return _read_sql_http(sql)
    # "%" doubled because psycopg2 treats it as a parameter marker
    return pd.read_sql(sql.replace("%", "%%"), engine)


def read_query(name, **params):
    """Run a sql/analysis file and return a DataFrame. See read_sql_file for params."""
    return read_sql(read_sql_file(config.SQL_ANALYSIS_DIR / name, **params))
