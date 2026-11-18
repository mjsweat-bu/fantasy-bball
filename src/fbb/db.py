"""Database access shared by scripts and notebooks.

    from fbb.db import engine, read_query
    df = read_query("standings.sql", season=2027)

Connects to DATABASE_URL when it's set (hosted Postgres such as Neon), otherwise to
the local Docker database from the POSTGRES_* settings in .env.
"""
import re
from pathlib import Path

import pandas as pd
import psycopg2
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


def read_query(name, **params):
    """Run a sql/analysis file and return a DataFrame. See read_sql_file for params."""
    sql = read_sql_file(config.SQL_ANALYSIS_DIR / name, **params)
    # "%" doubled because psycopg2 treats it as a parameter marker (LIKE '%...')
    return pd.read_sql(sql.replace("%", "%%"), engine)
