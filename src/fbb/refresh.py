"""Refresh the league: load new ESPN data, then rebuild the core tables.

    python -m fbb.refresh                  -> load + rebuild the current season (ESPN + NBA game logs)
    python -m fbb.refresh --season 2026    -> a different season
    python -m fbb.refresh --history        -> load every season the league has existed
    python -m fbb.refresh --skip-load      -> rebuild only (no API calls)

Run from anywhere. Connects through fbb.db: DATABASE_URL (Neon) if set, otherwise the
local Docker database. No psql or Docker CLI needed.
Each build script runs in a transaction, so a failure leaves that table as it was.
After the build, the data checks in fbb.validate run; a failed check exits with code 1.
"""
import argparse, sys, time

from fbb import config
from fbb import ingest, ingest_nba, validate

from fbb.db import get_conn


def build():
    # numeric order = build order (raw -> core)
    scripts = sorted(config.SQL_BUILD_DIR.glob("[0-9][0-9]_*.sql"))
    conn = get_conn()
    try:
        for path in scripts:
            start = time.time()
            try:
                with conn:                      # one transaction per script (commit / rollback)
                    with conn.cursor() as cur:
                        cur.execute(path.read_text())
            except Exception as e:
                print(f"--- FAIL  {path.name}")
                print(str(e).strip())
                sys.exit(1)
            print(f"--- built {path.name} ({time.time() - start:.0f}s)")
    finally:
        conn.close()


def summary():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.season, s.league_name, s.scoring_type, s.num_teams,
                       (SELECT count(*) FROM core.matchups m WHERE m.season = s.season) AS matchups,
                       (SELECT count(*) FROM core.player_days d WHERE d.season = s.season) AS player_days,
                       (SELECT count(*) FROM core.transactions t WHERE t.season = s.season) AS transactions
                FROM core.seasons s ORDER BY s.season""")
            print("|".join(d[0] for d in cur.description))
            for row in cur.fetchall():
                print("|".join(str(v) for v in row))
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Load ESPN league data and rebuild the tables.")
    p.add_argument("--season", type=int, default=config.current_season())
    p.add_argument("--history", action="store_true", help="load every season of the league")
    p.add_argument("--full", action="store_true", help="refetch every day, not just new/recent")
    p.add_argument("--skip-load", action="store_true", help="rebuild only, no API calls")
    a = p.parse_args()

    if not a.skip_load:
        argv = ["history"] if a.history else [str(a.season)]
        ingest.main(argv + (["--full"] if a.full else []))
        ingest_nba.main([str(a.season)])   # NBA game logs; backfill with python -m fbb.ingest_nba 2022 2027
    build()
    summary()
    if not validate.run():
        sys.exit(1)
