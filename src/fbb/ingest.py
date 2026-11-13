"""Load ESPN fantasy basketball league data into the raw.* tables.

    python -m fbb.ingest                    -> current season, everything
    python -m fbb.ingest 2027               -> one season
    python -m fbb.ingest 2020 2027          -> a range of seasons
    python -m fbb.ingest history            -> every season the league has existed
    python -m fbb.ingest 2027 --full        -> refetch every day, not just new/recent ones
    python -m fbb.ingest matchup_days history -> only the day -> matchup week map (all seasons)

League ID and (for private leagues) the espn_s2 / SWID cookies come from .api.env.
Requests go through espn_api's client, which handles cookies and the older
leagueHistory endpoint. Daily box scores and transactions are only available from
2019 on; earlier seasons load the league and draft only.
"""
import json, sys, time

from psycopg2.extras import execute_values, Json
from espn_api.requests.espn_requests import EspnFantasyRequests
from espn_api.basketball import constant as C

from fbb import config
from fbb.db import get_conn

LEAGUE_VIEWS = ["mTeam", "mRoster", "mMatchup", "mSettings", "mStandings"]
RECENT_PERIODS = 3          # refetch the last few days each run (stat corrections, late games)
PAUSE = 0.3                 # seconds between ESPN requests
FIRST_DETAIL_SEASON = 2019  # box scores / transactions / player pool exist from here on


def client(season):
    if not config.ESPN_LEAGUE_ID:
        raise RuntimeError("ESPN_LEAGUE_ID is not set: add it to .api.env at the project root")
    cookies = None
    if config.ESPN_S2 and config.ESPN_SWID:
        cookies = {"espn_s2": config.ESPN_S2, "SWID": config.ESPN_SWID}
    return EspnFantasyRequests(sport="nba", year=season, league_id=config.ESPN_LEAGUE_ID, cookies=cookies)


def filter_header(obj):
    return {"x-fantasy-filter": json.dumps(obj)}


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute((config.SQL_BUILD_DIR / "01_raw.sql").read_text())
    conn.commit()


def load_refs(conn):
    """Lookup tables from the library's constants (stat ids, slots, NBA teams)."""
    stats = [(str(k), v) for k, v in C.STATS_MAP.items() if v]
    slots = [(k, v) for k, v in C.POSITION_MAP.items() if isinstance(k, int)]
    pro = [(k, v) for k, v in C.PRO_TEAM_MAP.items()]
    splits = [(k, v) for k, v in C.STAT_ID_MAP.items()]
    with conn.cursor() as cur:
        for table, rows in [("ref.stats", stats), ("ref.lineup_slots", slots),
                            ("ref.pro_teams", pro), ("ref.stat_splits", splits)]:
            cur.execute(f"TRUNCATE {table}")
            execute_values(cur, f"INSERT INTO {table} VALUES %s", rows)
    conn.commit()


def upsert_league(cur, season, kind, payload):
    cur.execute("""
        INSERT INTO raw.espn_league (season, kind, payload) VALUES (%s, %s, %s)
        ON CONFLICT (season, kind) DO UPDATE SET payload = EXCLUDED.payload, fetched_at = now()
    """, (season, kind, Json(payload)))


def periods_to_fetch(cur, table, season, last_period, full):
    if full:
        return list(range(1, last_period + 1))
    cur.execute(f"SELECT scoring_period FROM {table} WHERE season = %s", (season,))
    have = {r[0] for r in cur.fetchall()}
    recent = set(range(max(1, last_period - RECENT_PERIODS + 1), last_period + 1))
    return sorted((set(range(1, last_period + 1)) - have) | recent)


def load_season(conn, season, full=False):
    req = client(season)
    cur = conn.cursor()

    league = req.league_get(params={"view": LEAGUE_VIEWS})
    upsert_league(cur, season, "league", league)
    upsert_league(cur, season, "draft", req.league_get(params={"view": "mDraftDetail"}))
    conn.commit()
    teams = len(league.get("teams", []))
    print(f"--- {season}: league + draft ({teams} teams, scoring {league.get('settings', {}).get('scoringSettings', {}).get('scoringType')})")

    if season < FIRST_DETAIL_SEASON:
        print(f"    (box scores, transactions and player pool start in {FIRST_DETAIL_SEASON})")
        return

    status = league["status"]
    last_period = min(league["scoringPeriodId"], status["finalScoringPeriod"])
    if not status.get("isActive", True) or season < config.current_season():
        last_period = status["finalScoringPeriod"]

    # NBA schedule
    sched = req.get(params={"view": "proTeamSchedules_wl"})
    cur.execute("""
        INSERT INTO raw.espn_pro_schedule (season, payload) VALUES (%s, %s)
        ON CONFLICT (season) DO UPDATE SET payload = EXCLUDED.payload, fetched_at = now()
    """, (season, Json(sched)))

    # player pool: every player with ownership and stat splits
    players = req.league_get(
        params={"view": "kona_player_info", "scoringPeriodId": last_period},
        headers=filter_header({"players": {"limit": 3000, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}),
    ).get("players", [])
    cur.execute("DELETE FROM raw.espn_players WHERE season = %s", (season,))
    execute_values(cur, "INSERT INTO raw.espn_players (season, player_id, payload) VALUES %s",
                   [(season, p["id"], Json(p)) for p in players])
    conn.commit()
    print(f"    player pool: {len(players)} players")

    # daily box scores. Asked for a day without a matchup filter, ESPN attaches
    # rosterForCurrentScoringPeriod only to the matchups that day belongs to, so
    # those matchups are kept and give the day's matchup period (works for points
    # and category leagues alike).
    todo = periods_to_fetch(cur, "raw.espn_scoring_periods", season, last_period, full)
    loaded = 0
    for sp in todo:
        data = req.league_get(params={"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": sp})
        day = [m for m in data.get("schedule", [])
               if "rosterForCurrentScoringPeriod" in m.get("home", {})
               or "rosterForCurrentScoringPeriod" in m.get("away", {})]
        time.sleep(PAUSE)
        if not day:
            continue   # no fantasy games that day
        cur.execute("""
            INSERT INTO raw.espn_scoring_periods (season, scoring_period, matchup_period, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (season, scoring_period) DO UPDATE
            SET matchup_period = EXCLUDED.matchup_period, payload = EXCLUDED.payload, fetched_at = now()
        """, (season, sp, day[0]["matchupPeriodId"], Json({"schedule": day})))
        conn.commit()
        loaded += 1
    print(f"    box scores: {loaded} days loaded (through day {last_period})")

    # transactions
    todo = periods_to_fetch(cur, "raw.espn_transactions", season, last_period, full)
    for sp in todo:
        data = req.league_get(params={"view": "mTransactions2", "scoringPeriodId": sp})
        cur.execute("""
            INSERT INTO raw.espn_transactions (season, scoring_period, payload) VALUES (%s, %s, %s)
            ON CONFLICT (season, scoring_period) DO UPDATE SET payload = EXCLUDED.payload, fetched_at = now()
        """, (season, sp, Json({"transactions": data.get("transactions", [])})))
        conn.commit()
        time.sleep(PAUSE)
    print(f"    transactions: {len(todo)} days loaded")

    load_matchup_days(conn, req, season, status["finalScoringPeriod"], league["scoringPeriodId"])


def matchup_period_for_day(req, sp):
    """The matchup period a day belongs to, or None. Same trick as the box scores:
    ESPN attaches rosterForCurrentScoringPeriod only to that day's matchups."""
    data = req.league_get(params={"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": sp})
    for m in data.get("schedule", []):
        if "rosterForCurrentScoringPeriod" in m.get("home", {}) or "rosterForCurrentScoringPeriod" in m.get("away", {}):
            return m["matchupPeriodId"]
    return None


def load_matchup_days(conn, req, season, final_period, today_period):
    """Fill raw.espn_matchup_days for every day of the season. Played days are copied
    from raw.espn_scoring_periods (same ESPN answer, already fetched); only missing days
    are requested. A day with no answer is stored as null only once it's in the past,
    so future days (e.g. playoff rounds not set yet) are asked again next run."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO raw.espn_matchup_days (season, scoring_period, matchup_period)
        SELECT season, scoring_period, matchup_period FROM raw.espn_scoring_periods WHERE season = %s
        ON CONFLICT (season, scoring_period) DO UPDATE SET matchup_period = EXCLUDED.matchup_period
    """, (season,))
    conn.commit()
    cur.execute("SELECT scoring_period FROM raw.espn_matchup_days WHERE season = %s", (season,))
    have = {r[0] for r in cur.fetchall()}
    todo = [sp for sp in range(1, final_period + 1) if sp not in have]
    fetched = 0
    for sp in todo:
        mp = matchup_period_for_day(req, sp)
        time.sleep(PAUSE)
        if mp is None and sp >= today_period:
            continue
        cur.execute("""
            INSERT INTO raw.espn_matchup_days (season, scoring_period, matchup_period) VALUES (%s, %s, %s)
            ON CONFLICT (season, scoring_period) DO UPDATE SET matchup_period = EXCLUDED.matchup_period, fetched_at = now()
        """, (season, sp, mp))
        conn.commit()
        fetched += 1
    print(f"    matchup days: {len(have)} known, {fetched} fetched (season has {final_period} days)")


def league_history():
    """Every season the league has existed, from the current season's status."""
    season = config.current_season()
    status = client(season).league_get(params={"view": "mStatus"})["status"]
    return sorted(set(status.get("previousSeasons", [])) | {season})


def main(argv):
    full = "--full" in argv
    args = [a for a in argv if not a.startswith("--")]
    only_matchup_days = bool(args) and args[0] == "matchup_days"
    if only_matchup_days:
        args = args[1:]
    if args == ["history"]:
        seasons = league_history()
    elif len(args) == 2:
        seasons = list(range(int(args[0]), int(args[1]) + 1))
    elif len(args) == 1:
        seasons = [int(args[0])]
    else:
        seasons = [config.current_season()]

    conn = get_conn()
    try:
        ensure_schema(conn)
        load_refs(conn)
        for season in seasons:
            if only_matchup_days:
                req = client(season)
                st = req.league_get(params={"view": "mStatus"})
                print(f"--- {season}: matchup days")
                load_matchup_days(conn, req, season, st["status"]["finalScoringPeriod"], st["scoringPeriodId"])
            else:
                load_season(conn, season, full)
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
