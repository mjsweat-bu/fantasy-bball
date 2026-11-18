"""Data checks on the built tables.

    python -m fbb.validate                  -> every check
    python -m fbb.validate team_schedule    -> checks for one table (matches on the name)

Each check is a query that returns the rows breaking a rule: zero rows = pass.
"error" checks protect a table's grain or meaning and fail the run (exit code 1);
"warn" checks flag data that is unusual but can be legitimate.
fbb.refresh runs every check after rebuilding the tables.
"""
import sys
from dataclasses import dataclass

from fbb.db import get_conn


@dataclass
class Check:
    table: str
    name: str
    sql: str                 # returns offending rows; zero rows = pass
    severity: str = "error"  # "error" fails the run, "warn" only reports


CHECKS = [
    # ---- core.team_schedule: one row per NBA team per game ----
    Check("core.team_schedule", "every game has exactly two rows (one per team)", """
        SELECT espn_game_id, count(*) AS rows
        FROM core.team_schedule GROUP BY espn_game_id HAVING count(*) <> 2"""),

    Check("core.team_schedule", "every game has one home side and one away side", """
        SELECT espn_game_id, count(*) FILTER (WHERE is_home) AS home_rows
        FROM core.team_schedule GROUP BY espn_game_id
        HAVING count(*) FILTER (WHERE is_home) <> 1"""),

    Check("core.team_schedule", "the two sides of a game agree on teams and date", """
        SELECT a.espn_game_id, a.team, a.opponent, a.game_date
        FROM core.team_schedule a
        WHERE NOT EXISTS (
            SELECT 1 FROM core.team_schedule b
            WHERE b.espn_game_id = a.espn_game_id
              AND b.pro_team_id = a.opponent_pro_team_id
              AND b.opponent_pro_team_id = a.pro_team_id
              AND b.game_date = a.game_date)"""),

    Check("core.team_schedule", "team and opponent names are filled in (ref.pro_teams join)", """
        SELECT espn_game_id, pro_team_id, opponent_pro_team_id, team, opponent
        FROM core.team_schedule WHERE team IS NULL OR opponent IS NULL"""),

    Check("core.team_schedule", "no team plays itself", """
        SELECT espn_game_id, team FROM core.team_schedule WHERE pro_team_id = opponent_pro_team_id"""),

    Check("core.team_schedule", "exactly one opening night (null prev_game_date) per team-season", """
        SELECT season, team, count(*) FILTER (WHERE prev_game_date IS NULL) AS null_prev
        FROM core.team_schedule GROUP BY season, team
        HAVING count(*) FILTER (WHERE prev_game_date IS NULL) <> 1"""),

    Check("core.team_schedule", "previous games come before this game", """
        SELECT espn_game_id, team, game_date, prev_game_date, prev_game_date_any_season
        FROM core.team_schedule
        WHERE prev_game_date >= game_date OR prev_game_date_any_season >= game_date"""),

    Check("core.team_schedule", "in-season and any-season previous games agree after opening night", """
        SELECT espn_game_id, team, game_date, prev_game_date, prev_game_date_any_season
        FROM core.team_schedule
        WHERE prev_game_date IS NOT NULL AND prev_game_date <> prev_game_date_any_season"""),

    Check("core.team_schedule", "known answer: game 401910034 is NYK home on a back-to-back, IND away not", """
        SELECT e.team, e.is_home AS want_home, e.b2b AS want_b2b, t.is_home, t.is_back_to_back
        FROM (VALUES ('NYK', true, true), ('IND', false, false)) AS e(team, is_home, b2b)
        LEFT JOIN core.team_schedule t ON t.espn_game_id = 401910034 AND t.team = e.team
        WHERE t.team IS NULL OR t.is_home <> e.is_home OR t.is_back_to_back <> e.b2b"""),

    Check("core.team_schedule", "games per team-season between 80 and 85", """
        SELECT season, team, count(*) AS games
        FROM core.team_schedule GROUP BY season, team
        HAVING count(*) NOT BETWEEN 80 AND 85""", severity="warn"),
    # 82 regular-season games, plus NBA Cup knockout games (up to 85 seen). The current
    # season shows 80 until the Cup knockout games are scheduled.

    Check("core.team_schedule", "no missing fantasy week between a season's first and last fantasy week", """
        WITH span AS (
            SELECT season, min(game_date) AS first_day, max(game_date) AS last_day
            FROM core.team_schedule WHERE matchup_period IS NOT NULL GROUP BY season)
        SELECT t.season, t.game_date, t.team
        FROM core.team_schedule t JOIN span USING (season)
        WHERE t.matchup_period IS NULL AND t.game_date BETWEEN span.first_day AND span.last_day""",
        severity="warn"),
]

SHOW_ROWS = 5


def run(only=None):
    """Run the checks (optionally only tables whose name contains `only`).
    Prints a report; returns True if no error-level check failed."""
    checks = [c for c in CHECKS if not only or only in c.table]
    if not checks:
        print(f"no checks match {only!r}")
        return False
    errors = 0
    with get_conn() as conn, conn.cursor() as cur:
        table = None
        for c in checks:
            if c.table != table:
                table = c.table
                print(f"\n{table}")
            cur.execute(f"SELECT * FROM ({c.sql}) bad LIMIT {SHOW_ROWS + 1}")
            bad = cur.fetchall()
            if not bad:
                print(f"  pass  {c.name}")
                continue
            cur.execute(f"SELECT count(*) FROM ({c.sql}) bad")
            n = cur.fetchone()[0]
            label = "FAIL" if c.severity == "error" else "warn"
            errors += c.severity == "error"
            print(f"  {label}  {c.name}  ({n} rows)")
            for row in bad[:SHOW_ROWS]:
                print(f"          {row}")
    print(f"\n{len(checks)} checks, {errors} failed")
    return errors == 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    return run(argv[0] if argv else None)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
