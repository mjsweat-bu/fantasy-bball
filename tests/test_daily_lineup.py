"""analysis/daily_lineup.py: projections, lineups, odds and the full report on a
small made-up league. No network: a fake stands in for fbb.espn_live.LiveLeague."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import daily_lineup as dl  # noqa: E402

SEASON = 2027
CATS = [dl.PTS, dl.REB, dl.AST, dl.TPM, dl.STL, dl.BLK, dl.FGP, dl.FTP, dl.TO]


def avg(pts=10, reb=4, ast=3, tpm=1, stl=1, blk=0.5, to=1.5, fgm=4, fga=9, ftm=2, fta=2.5, gp=10):
    s = {"0": pts, "6": reb, "3": ast, "17": tpm, "18": tpm * 2.8, "2": stl, "1": blk, "11": to,
         "13": fgm, "14": fga, "15": ftm, "16": fta, "42": gp}
    return s


def split(prefix, season=SEASON, gp=10, **kw):
    return {"id": f"{prefix}{season}", "averageStats": avg(gp=gp, **kw), "stats": {"42": gp}}


def pool_entry(pid, name, team, slots, status="ONTEAM", injury="ACTIVE", **line):
    stats = [split("00", **line), split("02", gp=5, **line), split("10", **line)]
    return {"id": pid, "status": status, "onTeamId": 0, "player": {
        "id": pid, "fullName": name, "proTeamId": team, "eligibleSlots": slots + [11, 12, 13],
        "injuryStatus": injury, "droppable": True, "stats": stats, "ownership": {"percentOwned": 10}}}


def player(pid, slots, **line):
    return dl.make_player(pool_entry(pid, f"P{pid}", 1, slots, **line), SEASON)


# --- unit pieces -------------------------------------------------------------------

def test_per_game_line_blends_splits_and_needs_some_stats():
    stats = [split("00", pts=20, gp=15), split("02", pts=30, gp=6), split("10", pts=10)]
    line = dl.per_game_line(stats, SEASON)
    assert line[dl.PTS] == pytest.approx((0.45 * 20 + 0.25 * 30 + 0.30 * 10) / 1.0)
    assert dl.per_game_line([], SEASON) is None
    # thin current season: last season's average joins the blend
    thin = [split("00", pts=20, gp=1), split("00", season=SEASON - 1, pts=10, gp=70)]
    assert 10 < dl.per_game_line(thin, SEASON)[dl.PTS] < 20


def test_best_lineup_respects_positions_and_benches_negative_value():
    slots = [0, 1, 4, 11]               # PG SG C UT
    pg, sg, c, wing, bad = (player(1, [0]), player(2, [1]), player(3, [4]), player(4, [2]),
                            player(5, [0]))
    lineup = dl.best_lineup([(pg, 5), (sg, 4), (c, 3), (wing, 2), (bad, -1)], slots)
    assert lineup == {1: 0, 2: 1, 3: 4, 4: 11}
    # a negative-value player stays out even with an open slot...
    assert 5 not in dl.best_lineup([(pg, 5), (bad, -1)], slots)
    # ...unless slots must be filled (projecting a team that starts everyone)
    assert 5 in dl.best_lineup([(pg, 5), (bad, -1)], slots, allow_empty=False)
    # a center can't play PG
    assert dl.best_lineup([(c, 3)], [0]) == {}


def test_week_odds_and_leverage():
    win, tie, exp = dl.week_odds([0.5] * 9)
    assert win == pytest.approx(0.5) and tie == 0 and exp == pytest.approx(4.5)
    assert dl.week_odds([1.0] * 5 + [0.0] * 4)[0] == pytest.approx(1.0)
    # 8 categories: a 4-4 split is a tie
    assert dl.week_odds([1.0] * 4 + [0.0] * 4)[1] == pytest.approx(1.0)

    week = dl.Week(season=SEASON, today=1, days=[1], cats=[dl.PTS, dl.REB, dl.TO], reverse={dl.TO},
                   slots=[11], me={}, opp={}, my_team={}, opp_team={}, matchup_period=1)
    odds = {dl.PTS: dict(p=0.999, density=0.0001, sd=10),     # decided
            dl.REB: dict(p=0.5, density=0.1, sd=4),           # coin flip
            dl.TO: dict(p=0.5, density=0.1, sd=3)}
    lev = dl.leverage(week, odds)
    assert abs(lev[dl.PTS]) < 1e-3
    assert lev[dl.REB] > 0 and lev[dl.TO] < 0                 # more turnovers hurt


# --- full report on a fake league -------------------------------------------------

TODAY, LAST = 10, 12
SLOT_COUNTS = {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "11": 2, "12": 9, "13": 2}


def roster_entry(pool, slot):
    return {"lineupSlotId": slot, "playerPoolEntry": pool, "injuryStatus": "NORMAL"}


class FakeLive:
    """Two teams of eight, NBA teams 1-4. Team 1 plays every day, team 2 never."""
    season = SEASON

    def __init__(self, games_today=True):
        self.games_today = games_today
        mine = [pool_entry(100 + i, f"Mine {i}", 1 + i % 3, [i % 5]) for i in range(8)]
        mine[7] = pool_entry(107, "Mine Hurt", 1, [0], injury="OUT", pts=30)
        theirs = [pool_entry(200 + i, f"Theirs {i}", 1 + i % 3, [i % 5], pts=12) for i in range(8)]
        self.fas = [pool_entry(300, "Big Man", 1, [4], status="FREEAGENT", reb=14, blk=3, pts=14),
                    pool_entry(301, "Waiver Guy", 1, [4], status="WAIVERS", reb=15, blk=3),
                    pool_entry(302, "No Games", 2, [0], status="FREEAGENT", pts=40)]

        def team(tid, name, pools, slots):
            return {"id": tid, "name": name, "roster": {"entries": [roster_entry(p, s) for p, s in zip(pools, slots)]},
                    "transactionCounter": {"matchupAcquisitionTotals": {}}}
        self._league = {
            "scoringPeriodId": TODAY,
            "status": {"isActive": True, "finalScoringPeriod": 100, "currentMatchupPeriod": 2},
            "settings": {
                "rosterSettings": {"lineupSlotCounts": SLOT_COUNTS},
                "scoringSettings": {"scoringItems": [{"statId": c, "isReverseItem": c == dl.TO} for c in CATS]},
                "acquisitionSettings": {"matchupAcquisitionLimit": 1, "matchupLimitPerScoringPeriod": True},
            },
            "teams": [team(4, "Me", mine, [0, 1, 2, 3, 4, 11, 11, 13]),
                      team(6, "Them", theirs, [0, 1, 2, 3, 4, 11, 11, 12])],
        }

    def league(self):
        return self._league

    def today(self):
        return TODAY

    def pro_schedule(self):
        def games(team, every):
            if not self.games_today and every:
                return {}
            return {str(sp): [{"homeProTeamId": team, "awayProTeamId": 4, "date": 1795055400000 + sp * 86400000}]
                    for sp in range(TODAY, LAST + 1) if every or sp == TODAY + 1}
        return [{"id": 1, "abbrev": "AAA", "proGamesByScoringPeriod": games(1, True)},
                {"id": 2, "abbrev": "BBB", "proGamesByScoringPeriod": {}},
                {"id": 3, "abbrev": "CCC", "proGamesByScoringPeriod": games(3, False)},
                {"id": 4, "abbrev": "DDD", "proGamesByScoringPeriod": {}}]

    def matchup_period_of(self, sp):
        return 2 if sp <= LAST else 3

    def day(self, sp):
        score = lambda pts: {"cumulativeScore": {"scoreByStat": {"0": {"score": pts}, "6": {"score": 20}}}}  # noqa: E731
        return [{"matchupPeriodId": self.matchup_period_of(sp),
                 "home": {"teamId": 4, **score(100)}, "away": {"teamId": 6, **score(90)}}]

    def free_agents(self, sp):
        return self.fas


def test_report_end_to_end():
    md, summary = dl.build(FakeLive(), team_id=4)
    assert md is not None, summary
    assert "week 2, Me vs Them" in md
    assert "## Start today" in md and "## Waiver wire" in md
    start = md.split("## Start today")[1].split("##")[0]
    assert "Mine 0" in start                       # plays today (NBA team 1)
    assert "Mine 1" not in start.split("**")[0]     # team 2 never plays
    assert "Mine Hurt" not in start                  # OUT and on IR
    waiver = md.split("## Waiver wire")[1]
    assert "Big Man" in waiver                       # free agent with games and boards
    assert "Waiver Guy" not in waiver                # on waivers: can't play today
    assert "No Games" not in waiver
    assert "1 add per day" in waiver
    assert 0 <= summary["win"] <= 1


def test_no_games_today_is_quiet():
    md, reason = dl.build(FakeLive(games_today=False), team_id=4)
    assert md is None and "no NBA games" in reason


def test_after_first_tip_adds_count_from_tomorrow():
    late = dl.datetime(2030, 1, 1, tzinfo=dl.timezone.utc)          # long after every fake tip-off
    md, _ = dl.build(FakeLive(), team_id=4, now=late)
    waiver = md.split("## Waiver wire")[1]
    assert "adds count from tomorrow" in waiver
    assert "| today" not in waiver


def test_rest_of_week_and_avail_override():
    md, _ = dl.build(FakeLive(), team_id=4, avail={"Mine 0": 0.0})
    rest = md.split("## Rest of the week")[1].split("## Categories")[0]
    assert rest.count("\n| ") >= 3                                 # header row + one row per later day
    start = md.split("## Start today")[1].split("##")[0]
    assert not [ln for ln in start.splitlines() if ln.startswith("| ") and "Mine 0" in ln]   # ruled out
    assert "Mine 0 (--avail)" in start


def test_preview_of_a_later_week():
    md, summary = dl.build(FakeLive(), team_id=4, week_no=3)
    assert md is not None, summary
    assert md.startswith("# Preview: week 3, Me vs Them")
    cats = md.split("## Categories")[1].split("Swing ●")[0]
    assert "| — | — |" in cats                                      # nothing played yet


# --- add-day timing ------------------------------------------------------------------

def timing_case(fa_days):
    """One UT slot. My weak player plays days 1-2; the free agent plays fa_days."""
    def p(pid, name, days, pts, status=""):
        x = player(pid, [0], pts=pts, reb=pts / 3)
        x.name, x.games, x.fa_status = name, {sp: {} for sp in days}, status
        return x
    star = p(1, "Star", [], 30)                  # rostered, never plays this week
    weak = p(2, "Weak", [1, 2], 4)
    fa = p(3, "Streamer", fa_days, 20, "FREEAGENT")
    opp = [p(9, "Opp", [1, 2, 3], 12)]
    week = dl.Week(season=SEASON, today=1, days=[1, 2, 3], cats=CATS, reverse={dl.TO}, slots=[11],
                   me={}, opp={}, my_team={}, opp_team={}, matchup_period=2)
    scale = dl.generic_scaler([star, weak, fa] + opp, CATS, {dl.TO})
    for x in (star, weak, fa):
        x.season_value = dl.generic_value(x, CATS, {dl.TO}, scale)
    gen = lambda x: dl.generic_value(x, CATS, {dl.TO}, scale) + 10.0   # noqa: E731
    opp_lineups, _ = dl.plan(opp, week.days, week.slots, gen, allow_empty=False)
    base = dl.solve(week, [star, weak], opp_lineups, scale)
    return week, [star, weak], [fa], opp_lineups, base, scale


def test_add_day_waits_for_the_dropped_players_last_game():
    week, mine, fas, opp_lineups, base, scale = timing_case(fa_days=[3])
    _, picks = dl.waiver_moves(week, mine, fas, opp_lineups, base, scale)
    assert picks and picks[0].drop.name == "Weak"
    assert picks[0].add_day == 3 and picks[0].drop_games_lost == 0   # Weak plays days 1-2 first


def test_add_day_is_today_when_the_free_agent_plays_now():
    week, mine, fas, opp_lineups, base, scale = timing_case(fa_days=[1, 2, 3])
    _, picks = dl.waiver_moves(week, mine, fas, opp_lineups, base, scale)
    assert picks and picks[0].add_day == 1 and picks[0].games_left == 3
    _, kept = dl.waiver_moves(week, mine, fas, opp_lineups, base, scale, keep={"Weak"})
    assert all(x.drop.name != "Weak" for x in kept)


def test_week_flag_plans_even_without_games_today():
    md, reason = dl.build(FakeLive(games_today=False), team_id=4, week_no=2)
    assert md is not None, reason
    assert "## Rest of the week" in md
