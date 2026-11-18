"""fbb.db read path: Neon HTTP response parsing and transport selection. No network."""
import datetime as dt

import pandas as pd
import pytest

from fbb import config, db

NEON_URL = "postgresql://analyst:pw@ep-x-pooler.c-8.us-east-2.aws.neon.tech/fantasy-bball?sslmode=require"


def field(name, type_id):
    return {"name": name, "dataTypeID": type_id}


def test_http_rows_convert_like_psycopg2():
    # values as Neon's /sql endpoint returns them (Neon-Array-Mode: true)
    result = {
        "fields": [field("i", 23), field("b", 20), field("n", 1700), field("f", 701), field("t", 16),
                   field("j", 3802), field("d", 1082), field("ts", 1184), field("arr", 1009), field("s", 25)],
        "rows": [
            [1, "2", "1.5", 2.5, True, {"a": 1}, "2026-11-18", "2026-11-18 15:09:13.618025+00", ["PG", "SG"], "x%"],
            [3, "9007199254740993", "NaN", 0.5, False, [1], "2026-11-19", "2026-11-19 01:00:00+00", [], "y"],
        ],
    }
    df = db._http_rows_to_frame(result)
    assert list(df.columns) == ["i", "b", "n", "f", "t", "j", "d", "ts", "arr", "s"]
    assert df.i.dtype == "int64" and df.b.dtype == "int64"
    assert df.b[1] == 9007199254740993          # bigint survives without float rounding
    assert df.n.dtype == "float64" and df.n.isna()[1]
    assert df.t.dtype == bool
    assert df.j[0] == {"a": 1}
    assert df.d[0] == dt.date(2026, 11, 18)
    assert str(df.ts.dtype).startswith("datetime64") and str(df.ts.dt.tz) == "UTC"
    assert df.ts[1] == pd.Timestamp("2026-11-19 01:00", tz="UTC")
    assert df.arr[0] == ["PG", "SG"]
    assert df.s[0] == "x%"


def test_http_nulls_and_empty_results():
    df = db._http_rows_to_frame({"fields": [field("b", 20), field("d", 1082), field("ts", 1184)],
                                 "rows": [[None, None, None], ["5", "2026-01-01", "2026-01-01 00:00:00+00"]]})
    assert df.b.dtype == "float64" and df.b.isna()[0] and df.b[1] == 5   # like pd.read_sql: int + NULL -> float
    assert df.d[0] is None
    assert pd.isna(df.ts[0])

    empty = db._http_rows_to_frame({"fields": [field("team_id", 23), field("as_of", 1184)], "rows": []})
    assert empty.shape == (0, 2) and list(empty.columns) == ["team_id", "as_of"]


def test_http_duplicate_column_names_kept():
    df = db._http_rows_to_frame({"fields": [field("x", 23), field("x", 20)], "rows": [[1, "2"]]})
    assert list(df.columns) == ["x", "x"] and df.iloc[0].tolist() == [1, 2]


@pytest.fixture
def env(monkeypatch):
    def set_env(database_url, transport=None, tcp_ok=True):
        monkeypatch.setattr(config, "DATABASE_URL", database_url)
        if transport is None:
            monkeypatch.delenv("FBB_DB_TRANSPORT", raising=False)
        else:
            monkeypatch.setenv("FBB_DB_TRANSPORT", transport)
        monkeypatch.setattr(db, "_tcp_reachable", lambda host, port=5432, timeout=3: tcp_ok)
        db.transport.cache_clear()
    yield set_env
    db.transport.cache_clear()


def test_transport_auto(env):
    env(NEON_URL, tcp_ok=False)
    assert db.transport() == "http"
    env(NEON_URL, tcp_ok=True)
    assert db.transport() == "tcp"
    env("postgresql://u:p@localhost:5433/fbb", tcp_ok=False)   # not Neon: never http
    assert db.transport() == "tcp"
    env(None, tcp_ok=False)                                     # local Docker settings
    assert db.transport() == "tcp"


def test_transport_forced(env):
    env(NEON_URL, transport="tcp", tcp_ok=False)
    assert db.transport() == "tcp"
    env(NEON_URL, transport="HTTP", tcp_ok=True)
    assert db.transport() == "http"
    env("postgresql://u:p@localhost:5433/fbb", transport="http")
    with pytest.raises(ValueError, match="Neon"):
        db.transport()
    env(NEON_URL, transport="carrier-pigeon")
    with pytest.raises(ValueError, match="FBB_DB_TRANSPORT"):
        db.transport()


def test_neon_host_accepts_driver_prefixes(monkeypatch):
    for url in (NEON_URL, NEON_URL.replace("postgresql://", "postgres://"),
                NEON_URL.replace("postgresql://", "postgresql+psycopg2://")):
        monkeypatch.setattr(config, "DATABASE_URL", url)
        assert db._neon_host() == "ep-x-pooler.c-8.us-east-2.aws.neon.tech"


def test_tcp_path_escapes_percent(env, monkeypatch):
    env(NEON_URL, transport="tcp")
    seen = {}
    monkeypatch.setattr(pd, "read_sql", lambda sql, con: seen.setdefault("sql", sql))
    db.read_sql("SELECT 'FG%' WHERE x LIKE 'a%'")
    assert seen["sql"] == "SELECT 'FG%%' WHERE x LIKE 'a%%'"


def test_http_path_posts_to_neon(env, monkeypatch):
    env(NEON_URL, transport="http")
    calls = []

    class Resp:
        def __init__(self, status, body):
            self.status_code, self.ok, self._body = status, status == 200, body
            self.headers = {"content-type": "application/json"}
            self.text = str(body)

        def json(self):
            return self._body

    replies = [Resp(500, {"message": "compute waking", "neon:retryable": True}),
               Resp(200, {"fields": [field("one", 23)], "rows": [[1]]}),
               Resp(400, {"message": 'column "nope" does not exist', "code": "42703"})]

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json))
        return replies.pop(0)

    monkeypatch.setattr(db.requests, "post", fake_post)
    assert db.read_sql("SELECT 1 AS one").one.tolist() == [1]      # retried once, then succeeded
    assert len(calls) == 2
    url, headers, body = calls[0]
    assert url == "https://ep-x-pooler.c-8.us-east-2.aws.neon.tech/sql"
    assert headers["Neon-Connection-String"] == NEON_URL and headers["Neon-Array-Mode"] == "true"
    assert body == {"query": "SELECT 1 AS one", "params": []}       # "%" not doubled over http

    with pytest.raises(db.NeonHTTPError, match="42703"):
        db.read_sql("SELECT nope")
