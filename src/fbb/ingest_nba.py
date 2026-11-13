"""Load NBA player game logs (stats.nba.com via nba_api) into raw.nba_game_logs.

    python -m fbb.ingest_nba                 -> current season
    python -m fbb.ingest_nba 2022 2027       -> a range of seasons (ESPN-style end years)

One request per season per season type returns every player-game, so a season is
3 requests: Regular Season, PlayIn, Playoffs. Rows are upserted, so rerunning a
season just refreshes it. stats.nba.com throttles scripted traffic; requests are
retried with backoff.
"""
import sys, time

from nba_api.stats.endpoints import leaguegamelog
from psycopg2.extras import execute_values, Json

from fbb import config
from fbb.db import get_conn
from fbb.ingest import ensure_schema

SEASON_TYPES = ["Regular Season", "PlayIn", "Playoffs"]
PAUSE = 2   # seconds between requests


def fetch(season, season_type):
    """Rows for one season/type as dicts, or [] if that type has no games yet."""
    for attempt in range(4):
        try:
            r = leaguegamelog.LeagueGameLog(
                season=config.nba_season_label(season),
                season_type_all_star=season_type,
                player_or_team_abbreviation="P",
                timeout=60,
            )
            rs = r.get_dict()["resultSets"][0]
            return [dict(zip(rs["headers"], row)) for row in rs["rowSet"]]
        except Exception as e:
            wait = 10 * 2 ** attempt
            print(f"    {season_type}: {type(e).__name__}, retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"stats.nba.com kept failing for {season} {season_type}")


def load_season(conn, season):
    with conn.cursor() as cur:
        for season_type in SEASON_TYPES:
            rows = fetch(season, season_type)
            if rows:
                execute_values(cur, """
                    INSERT INTO raw.nba_game_logs (game_id, nba_player_id, season, season_type, payload)
                    VALUES %s
                    ON CONFLICT (game_id, nba_player_id) DO UPDATE
                    SET season = EXCLUDED.season, season_type = EXCLUDED.season_type,
                        payload = EXCLUDED.payload, fetched_at = now()
                """, [(r["GAME_ID"], r["PLAYER_ID"], season, season_type, Json(r)) for r in rows])
                conn.commit()
            print(f"--- nba {config.nba_season_label(season)} {season_type}: {len(rows)} player-games")
            time.sleep(PAUSE)


def main(argv):
    if len(argv) == 2:
        seasons = list(range(int(argv[0]), int(argv[1]) + 1))
    elif len(argv) == 1:
        seasons = [int(argv[0])]
    else:
        seasons = [config.current_season()]
    conn = get_conn()
    try:
        ensure_schema(conn)
        for season in seasons:
            load_season(conn, season)
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
