"""Daily lineup report: who to start today to win this week's matchup, and which
free agents would help more than someone on the roster.

    python analysis/daily_lineup.py --team 4                # today's report (Markdown, stdout)
    python analysis/daily_lineup.py --team 4 --out report.md
    python analysis/daily_lineup.py --team 4 --league 123456
    python analysis/daily_lineup.py --team 4 --week 6       # preview next week's matchup
    python analysis/daily_lineup.py --team 4 --avail "Darius Garland=0.5" --keep "Tyrese Proctor"

Reads ESPN live (fbb.espn_live), not the database: lineups, injury tags, the
week's category totals and the free-agent pool change during the day.
Prints "NO_GAMES" and exits 0 when no NBA games are scheduled today or there is
no matchup this week (off-season, eliminated), so a scheduler can stay quiet.

How it decides:

1. Per-game line for every player: a blend of this season's average, the last
   15 days and ESPN's projection (weights in BLEND, scaled down when a split has
   few games). Makes and attempts are kept separately for FG% and FT%.
2. Availability: ESPN's injury tag gives a chance of playing each game
   (PLAY_PROB; OUT = 0). Players in IR slots don't play.
3. Rest of week: for every remaining day of the matchup, each team starts its
   best legal lineup (PG SG SF PF C UT UT) from the players whose NBA team plays.
   Adding those lines to the week's totals so far gives a mean and variance for
   each category's final margin, so a win probability per category, and from
   those (assumed independent) the chance of winning the week.
4. Leverage: a category already won or lost is worth nothing; a close one is
   worth a lot. Each category's weight is d P(win week) / d margin. A player's
   value on a day is his projected line times those weights (FG%/FT% by makes
   above your team's rate on his attempts). Today's lineup maximizes that value.
   A player with negative value (he mostly hurts close FG%/FT%/TO) is flagged
   as an optional sit. Re-weighting can swing between plans (a big lead makes
   counting stats worth ~0, so the next plan benches people and loses the lead),
   so every plan tried is scored and the one with the best P(win week) is kept.
5. Waiver wire: for the most promising free agents, swap them for each of your
   weakest droppable players (by season-long value, so injured stars are never
   suggested as drops, and never anyone in --keep) on each day the add could
   happen, re-plan every remaining day, and rank by the change in P(win week).
   Before the swap the dropped player still plays, after it the free agent does,
   so the best add day is reported, with the latest day that's just as good.
   Once today's first game has tipped, adds count from tomorrow.
6. --week N previews a later matchup: that opponent, every day of that week,
   totals starting at zero, today's rosters.
"""
import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))   # run without pip install -e

from fbb import config  # noqa: E402

ET = ZoneInfo("America/New_York")

# ESPN stat ids
PTS, BLK, STL, AST, REB, TO, FGM, FGA, FTM, FTA, TPM, TPA = 0, 1, 2, 3, 6, 11, 13, 14, 15, 16, 17, 18
FGP, FTP, TPP = 19, 20, 21
RATIO = {FGP: (FGM, FGA), FTP: (FTM, FTA), TPP: (TPM, TPA)}      # pct stat -> (makes, attempts)
STAT_NAMES = {PTS: "PTS", BLK: "BLK", STL: "STL", AST: "AST", REB: "REB", TO: "TO",
              FGP: "FG%", FTP: "FT%", TPP: "3P%", TPM: "3PM", FGM: "FGM", FGA: "FGA",
              FTM: "FTM", FTA: "FTA", TPA: "3PA"}
LINE_STATS = [PTS, BLK, STL, AST, REB, TO, FGM, FGA, FTM, FTA, TPM, TPA]

# per-game variance as a multiple of the per-game mean (counting stats are
# over-dispersed relative to Poisson; PTS comes in 2s and 3s)
DISPERSION = {PTS: 2.6, REB: 1.5, AST: 1.5, STL: 1.1, BLK: 1.4, TO: 1.2, TPM: 1.2}

# split weights for the per-game line: (split id prefix, weight, games for full weight)
BLEND = [("00", 0.45, 15), ("02", 0.25, 6), ("10", 0.30, 0)]
PRIOR_SEASON_WEIGHT = 0.30          # last season's average, used only when this season is thin

PLAY_PROB = {"ACTIVE": 1.0, "NORMAL": 1.0, "PROBABLE": 0.9, "DAY_TO_DAY": 0.65,
             "QUESTIONABLE": 0.5, "DOUBTFUL": 0.2, "OUT": 0.0, "INJURY_RESERVE": 0.0,
             "SUSPENSION": 0.0}
STATUS_SHORT = {"DAY_TO_DAY": "DTD", "QUESTIONABLE": "Q", "DOUBTFUL": "D", "OUT": "OUT",
                "PROBABLE": "P", "SUSPENSION": "SUSP", "INJURY_RESERVE": "IR"}

SLOT_NAMES = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 7: "SG/SF", 8: "G/F",
              9: "PF/C", 10: "F/C", 11: "UT", 12: "BE", 13: "IR"}
BENCH, IR = 12, 13

DROP_POOL = 4         # weakest droppable players (season-long value) considered as drops
FA_POOL = 60          # free agents evaluated in full


# --- players ---------------------------------------------------------------------

@dataclass
class Player:
    id: int
    name: str
    pro_team: int
    slots: list               # eligible lineup slot ids
    line: dict                # stat id -> per-game mean
    status: str = "ACTIVE"    # ESPN injury tag
    slot: int = BENCH         # current lineup slot (rostered players)
    droppable: bool = True
    owned: float = 0.0        # % owned across ESPN
    fa_status: str = ""       # FREEAGENT / WAIVERS for unrostered players
    season_value: float = 0.0
    games: dict = field(default_factory=dict)     # scoring period -> game dict
    avail: float = None       # --avail override of the chance he plays a game

    @property
    def play_prob(self):
        if self.slot == IR:
            return 0.0
        return self.avail if self.avail is not None else PLAY_PROB.get(self.status, 1.0)

    @property
    def tag(self):
        return STATUS_SHORT.get(self.status, "")


def per_game_line(stats, season):
    """Blend ESPN stat splits into one per-game line {stat id: mean}, or None."""
    by_id = {s.get("id"): s for s in stats or []}
    parts = []
    for prefix, weight, full_games in BLEND:
        s = by_id.get(f"{prefix}{season}")
        if not s or not s.get("averageStats"):
            continue
        gp = (s.get("stats") or {}).get("42", 0) or 0
        if prefix != "10":
            if gp <= 0:
                continue
            weight *= min(1.0, gp / full_games)
        parts.append((weight, s["averageStats"]))
    this_season = sum(w for w, _ in parts)
    prior = by_id.get(f"00{season - 1}")
    if prior and prior.get("averageStats") and this_season < 0.6:
        parts.append((PRIOR_SEASON_WEIGHT, prior["averageStats"]))
    total = sum(w for w, _ in parts)
    if total <= 0:
        return None
    return {k: sum(w * float(avg.get(str(k), 0) or 0) for w, avg in parts) / total for k in LINE_STATS}


def make_player(entry, season, slot=BENCH):
    """A Player from an ESPN playerPoolEntry (roster entry or kona_player_info row)."""
    p = entry["player"]
    line = per_game_line(p.get("stats"), season)
    if line is None:
        return None
    return Player(
        id=p["id"], name=p.get("fullName", str(p["id"])), pro_team=p.get("proTeamId", 0),
        slots=p.get("eligibleSlots", []), line=line,
        status=p.get("injuryStatus") or "ACTIVE", slot=slot,
        droppable=p.get("droppable", True),
        owned=(p.get("ownership") or {}).get("percentOwned", 0.0),
        fa_status=entry.get("status", ""),
    )


# --- league state ------------------------------------------------------------------

@dataclass
class Week:
    season: int
    today: int                 # scoring period
    days: list                 # scoring periods left in the matchup, today first
    cats: list                 # stat ids scored
    reverse: set               # stat ids where lower wins (TO)
    slots: list                # active lineup slots, expanded (PG SG SF PF C UT UT)
    me: dict                   # week totals so far {stat id: value}, incl. makes/attempts
    opp: dict
    my_team: dict
    opp_team: dict
    matchup_period: int
    first_add: int = 0         # index into days of the first day an add can play
    labels: dict = field(default_factory=dict)    # scoring period -> "Thu 11/19"
    preview: bool = False      # a later week (--week): no games played yet

    def label(self, sp):
        return self.labels.get(sp, f"day {sp}")


def team_name(t):
    return t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip()


def totals(side):
    by_stat = (side.get("cumulativeScore") or {}).get("scoreByStat") or {}
    return {int(k): float(v.get("score") or 0) for k, v in by_stat.items()}


def active_slots(settings):
    counts = settings["rosterSettings"]["lineupSlotCounts"]
    return [int(s) for s, n in sorted(counts.items(), key=lambda kv: int(kv[0]))
            for _ in range(int(n)) if int(s) not in (BENCH, IR)]


def attach_games(players, pro_teams, days):
    games = {t["id"]: t.get("proGamesByScoringPeriod", {}) for t in pro_teams}
    for p in players:
        team_games = games.get(p.pro_team, {})
        p.games = {sp: team_games[str(sp)][0] for sp in days if team_games.get(str(sp))}


def week_days(live, today, matchup_period, final_period, max_days=21):
    """Scoring periods from today to the end of this matchup. Week lengths vary
    (opening week, All-Star break, playoff rounds), so ask ESPN day by day."""
    days = [today]
    for sp in range(today + 1, min(final_period, today + max_days) + 1):
        if live.matchup_period_of(sp) != matchup_period:
            break
        days.append(sp)
    return days


# --- projection --------------------------------------------------------------------

def stat_var(stat, mean):
    return DISPERSION.get(stat, 1.0) * max(mean, 0.0)


def side_projection(lineups, cats):
    """Expected totals and variances added by a team's planned lineups.
    lineups: list over days of [(player, prob)]."""
    mean, var = {}, {}
    for day in lineups:
        for p, q in day:
            for c in cats:
                if c in RATIO:
                    m, a = RATIO[c]
                    for s in (m, a):
                        mean[s] = mean.get(s, 0) + q * p.line[s]
                    # makes given attempts ~ binomial, plus attempt-volume noise
                    att = p.line[a]
                    pct = p.line[m] / att if att > 0 else 0
                    var[c] = var.get(c, 0) + q * (att * pct * (1 - pct) + 0.5 * att * pct * pct) \
                        + q * (1 - q) * p.line[m] ** 2
                else:
                    mean[c] = mean.get(c, 0) + q * p.line[c]
                    var[c] = var.get(c, 0) + q * stat_var(c, p.line[c]) + q * (1 - q) * p.line[c] ** 2
    return mean, var


def category_odds(week, my_lineups, opp_lineups):
    """Per category: projected finals, P(win), margin sd. Returns dict stat -> info."""
    my_add, my_var = side_projection(my_lineups, week.cats)
    op_add, op_var = side_projection(opp_lineups, week.cats)
    out = {}
    for c in week.cats:
        if c in RATIO:
            m, a = RATIO[c]
            my_m, my_a = week.me.get(m, 0) + my_add.get(m, 0), week.me.get(a, 0) + my_add.get(a, 0)
            op_m, op_a = week.opp.get(m, 0) + op_add.get(m, 0), week.opp.get(a, 0) + op_add.get(a, 0)
            mine = my_m / my_a if my_a else 0.0
            theirs = op_m / op_a if op_a else 0.0
            sd = math.sqrt((my_var.get(c, 0) / my_a ** 2 if my_a else 0)
                           + (op_var.get(c, 0) / op_a ** 2 if op_a else 0))
            out[c] = dict(mine=mine, theirs=theirs, sd=sd, my_att=my_a)
        else:
            mine = week.me.get(c, 0) + my_add.get(c, 0)
            theirs = week.opp.get(c, 0) + op_add.get(c, 0)
            sd = math.sqrt(my_var.get(c, 0) + op_var.get(c, 0))
            out[c] = dict(mine=mine, theirs=theirs, sd=sd)
        margin = out[c]["mine"] - out[c]["theirs"]
        if c in week.reverse:
            margin = -margin
        out[c]["margin"] = margin
        if out[c]["sd"] > 1e-9:
            z = margin / out[c]["sd"]
            out[c]["p"] = float(norm.cdf(z))
            out[c]["density"] = float(norm.pdf(z)) / out[c]["sd"]
        else:
            out[c]["p"] = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
            out[c]["density"] = 0.0
    return out


def wins_distribution(ps):
    """P(k categories won) for independent categories with win chances ps."""
    dist = np.zeros(len(ps) + 1)
    dist[0] = 1.0
    for p in ps:
        dist[1:] = dist[1:] * (1 - p) + dist[:-1] * p
        dist[0] *= 1 - p
    return dist


def week_odds(ps):
    """(P(win), P(tie), expected categories) for a most-categories matchup."""
    n = len(ps)
    dist = wins_distribution(ps)
    win = float(sum(dist[k] for k in range(n + 1) if k > n - k))
    tie = float(sum(dist[k] for k in range(n + 1) if k == n - k))
    return win, tie, float(sum(ps))


def leverage(week, odds):
    """d P(win week) / d margin, per category (sign: + means more of the stat helps)."""
    cats = week.cats
    ps = [odds[c]["p"] for c in cats]
    lev = {}
    for i, c in enumerate(cats):
        others = ps[:i] + ps[i + 1:]
        n = len(cats)
        dist = wins_distribution(others)
        # win week needs wins > n/2; ties at n/2 count half
        def p_week(extra):
            return sum(d * (1.0 if k + extra > n - k - extra else 0.5 if k + extra == n - k - extra else 0.0)
                       for k, d in enumerate(dist))
        swing = p_week(1) - p_week(0)
        sign = -1.0 if c in week.reverse else 1.0
        lev[c] = sign * odds[c]["density"] * swing
    return lev


def leverage_value(p, week, odds, lev):
    """A player's contribution to P(win week) for one game (before availability)."""
    v = 0.0
    for c in week.cats:
        if c in RATIO:
            m, a = RATIO[c]
            att = odds[c].get("my_att") or 0
            if att > 0:
                v += lev[c] * (p.line[m] - odds[c]["mine"] * p.line[a]) / att
        else:
            v += lev[c] * p.line[c]
    return v


# --- generic (category-neutral) value, for opponents and season-long ranking ------

def generic_scaler(players, cats, reverse):
    """z-score scales over a player pool: per stat mean / sd, pct by impact."""
    pool = [p for p in players if p.line.get(FGA, 0) > 0]
    scale = {}
    for c in cats:
        if c in RATIO:
            m, a = RATIO[c]
            rate = sum(p.line[m] for p in pool) / max(sum(p.line[a] for p in pool), 1e-9)
            vals = np.array([p.line[m] - rate * p.line[a] for p in pool])
            scale[c] = (rate, 0.0, float(vals.std()) or 1.0)
        else:
            vals = np.array([p.line[c] for p in pool])
            scale[c] = (None, float(vals.mean()), float(vals.std()) or 1.0)
    return scale


def generic_value(p, cats, reverse, scale):
    v = 0.0
    for c in cats:
        rate, mu, sd = scale[c]
        if c in RATIO:
            m, a = RATIO[c]
            z = (p.line[m] - rate * p.line[a]) / sd
        else:
            z = (p.line[c] - mu) / sd
        v += -z if c in reverse else z
    return v


# --- lineups ----------------------------------------------------------------------

def best_lineup(candidates, slots, allow_empty=True):
    """Max-value legal lineup. candidates: [(player, value)]. Returns {player id: slot}.
    With allow_empty, a player whose value is <= 0 stays on the bench."""
    if not candidates:
        return {}
    n, k = len(candidates), len(slots)
    big = 1e6
    cost = np.full((n, k + n), 0.0)
    for i, (p, v) in enumerate(candidates):
        for j, s in enumerate(slots):
            cost[i, j] = -v if s in p.slots else big
        if not allow_empty:
            cost[i, k:] = 0.0
    if not allow_empty:
        # force filling slots: shift all eligible values up so starting beats the bench
        shift = max(0.0, -min(v for _, v in candidates)) + 1.0
        for i, (p, v) in enumerate(candidates):
            for j, s in enumerate(slots):
                if s in p.slots:
                    cost[i, j] = -(v + shift)
    rows, cols = linear_sum_assignment(cost)
    return {candidates[r][0].id: slots[c] for r, c in zip(rows, cols) if c < k and cost[r, c] < 0}


def plan(players, days, slots, value_fn, allow_empty):
    """Lineups for each day: list of [(player, prob)] and the per-day assignments."""
    lineups, assignments = [], []
    for sp in days:
        cands = [(p, value_fn(p) * p.play_prob) for p in players if sp in p.games and p.play_prob > 0]
        lineup = best_lineup(cands, slots, allow_empty)
        by_id = {p.id: p for p, _ in cands}
        lineups.append([(by_id[i], by_id[i].play_prob) for i in lineup])
        assignments.append(lineup)
    return lineups, assignments


@dataclass
class Plan:
    odds: dict
    lev: dict
    win: float
    tie: float
    exp_cats: float
    assignments: list
    lineups: list


def solve(week, mine, opp_lineups, scale, iterations=3):
    """Plan my lineups for the rest of the week around category leverage.

    Starts from everyone's best generic lineup, then re-plans with the leverage of
    the last plan. That step can swing: a big lead makes counting stats worth ~0,
    so the next plan benches players to protect FG%/FT%, loses the lead, and so on.
    Every plan tried is scored, and the one with the best P(win week) is kept."""
    gen = lambda p: generic_value(p, week.cats, week.reverse, scale) + 10.0   # noqa: E731
    my_lineups, assignments = plan(mine, week.days, week.slots, gen, allow_empty=False)
    best = None
    for i in range(iterations + 1):
        odds = category_odds(week, my_lineups, opp_lineups)
        win, tie, exp = week_odds([odds[c]["p"] for c in week.cats])
        cand = Plan(odds, leverage(week, odds), win, tie, exp, assignments, my_lineups)
        if best is None or (win + 0.5 * tie, exp) > (best.win + 0.5 * best.tie, best.exp_cats):
            best = cand
        if i == iterations:
            break
        val = lambda p, odds=odds, lev=cand.lev: leverage_value(p, week, odds, lev)   # noqa: E731
        my_lineups, assignments = plan(mine, week.days, week.slots, val, allow_empty=True)
    return best


# --- waiver wire --------------------------------------------------------------------

@dataclass
class Pickup:
    add: Player
    drop: Player
    win: float
    d_win: float
    d_cats: float
    games_left: int
    plays_today: bool
    starts_today: bool
    helps: list
    add_day: int = 0          # scoring period to make the move
    add_by: int = 0           # latest scoring period that's as good
    drop_games_lost: int = 0  # games the dropped player has from add_day on


def waiver_moves(week, mine, free_agents, opp_lineups, base, scale, keep=(), top=5):
    """Best add/drop swaps by change in P(win week), each on its best day. Returns
    (P(win week) as the roster stands, i.e. base.win, [Pickup]). Each swapped roster is
    planned with solve(), like the baseline."""
    healthy = [p for p in mine if p.slot != IR]
    drops = sorted([p for p in healthy if p.droppable and p.name not in keep],
                   key=lambda p: p.season_value)[:DROP_POOL]
    val = lambda p: leverage_value(p, week, base.odds, base.lev)   # noqa: E731
    days = week.days
    add_days = range(week.first_add, len(days))
    # free agents only: a waiver claim clears at an unknown time, so it can't be planned
    fas = [p for p in free_agents if p.fa_status == "FREEAGENT" and p.play_prob > 0
           and any(sp in p.games for sp in days[week.first_add:])]
    fas.sort(key=lambda p: -sum(max(val(p), 0) * p.play_prob for sp in days[week.first_add:] if sp in p.games))

    def odds_of(lineups):
        o = category_odds(week, lineups, opp_lineups)
        win, tie, exp = week_odds([o[c]["p"] for c in week.cats])
        return win, exp, o

    # the baseline is the headline plan, so "now" in the report matches P(win week)
    base_lineups, base_win, base_cats = base.lineups, base.win, base.exp_cats
    out = []
    if not add_days:
        return base_win, out
    for fa in fas[:FA_POOL]:
        best = None
        for d in drops:
            roster = [p for p in mine if p.id != d.id] + [fa]
            swapped_plan = solve(week, roster, opp_lineups, scale)   # same planner as the baseline
            swapped, swapped_assign = swapped_plan.lineups, swapped_plan.assignments
            tries = []
            for a in add_days:   # before day a the old roster plays, from day a the new one
                win, cats, odds = odds_of(base_lineups[:a] + swapped[a:])
                tries.append((win, cats, a, odds))
            win, cats, a, odds = max(tries, key=lambda t: (t[0] + 0.01 * t[1], -t[2]))
            if best is None or win + 0.01 * cats > best[0] + 0.01 * best[1]:
                # latest day that's as good: waiting keeps options open (injury news)
                by = max(t[2] for t in tries if t[0] >= win - 0.002 and t[1] >= cats - 0.02)
                best = (win, cats, d, a, by, odds, swapped_assign)
        if best is None:
            continue
        win, cats, d, a, by, odds, assign = best
        helps = [STAT_NAMES[c] for c in week.cats
                 if (odds[c]["p"] - base.odds[c]["p"]) > 0.02]
        out.append(Pickup(fa, d, win, win - base_win, cats - base_cats,
                          games_left=sum(1 for sp in days[a:] if sp in fa.games),
                          plays_today=week.today in fa.games,
                          starts_today=a == 0 and fa.id in assign[0], helps=helps,
                          add_day=days[a], add_by=days[by],
                          drop_games_lost=sum(1 for sp in days[a:] if sp in d.games)))
    # same score as each player's best swap: near 0% or 100%, P(win) barely moves, so categories decide
    out.sort(key=lambda x: -(x.d_win + 0.01 * x.d_cats))
    return base_win, [x for x in out if x.d_win > 0.001 or x.d_cats > 0.02][:top]


# --- report ----------------------------------------------------------------------

def fmt_stat(c, v):
    return f"{v:.3f}".lstrip("0") if c in RATIO else f"{v:.0f}"


def game_label(p, sp, abbrevs):
    g = p.games.get(sp)
    if not g:
        return "—"
    home = g["homeProTeamId"] == p.pro_team
    other = abbrevs.get(g["awayProTeamId"] if home else g["homeProTeamId"], "?")
    tip = datetime.fromtimestamp(g["date"] / 1000, tz=timezone.utc).astimezone(ET)
    when = "TBD" if g.get("startTimeTBD") else tip.strftime("%-I:%M%p").lower().replace(":00", "")
    return f"{'vs' if home else '@'} {other} {when} ET"


def pct(x):
    return f"{100 * x:.0f}%"


def render(week, mine, opp_lineups, base, pickups, abbrevs, today_date, acq_note, roster_win=None):
    my, op = team_name(week.my_team), team_name(week.opp_team)
    today_assign = base.assignments[0]
    lines = []
    games_me = sum(len(d) for d in base.lineups)
    opp_games = sum(len(d) for d in opp_lineups)
    if week.preview:
        lines.append(f"# Preview: week {week.matchup_period}, {my} vs {op} "
                     f"({week.label(week.days[0])} - {week.label(week.days[-1])})")
    else:
        lines.append(f"# {today_date:%a %b %-d}: week {week.matchup_period}, {my} vs {op}")
    lines.append("")
    lines.append(f"**P(win week): {pct(base.win)}**"
                 + (f" (tie {pct(base.tie)})" if base.tie >= 0.03 else "")
                 + f" · projected {base.exp_cats:.1f}–{len(week.cats) - base.exp_cats:.1f} categories"
                 + f" · {len(week.days)} day{'s' if len(week.days) > 1 else ''} left,"
                 + f" ~{games_me} starts for you vs ~{opp_games} for them")
    lines.append("")

    # --- start today
    lines.append("## Start today" if not week.preview else f"## Start {week.label(week.today)}")
    lines.append("")
    lines.append("| Slot | Player | Game | Note |")
    lines.append("|---|---|---|---|")
    by_id = {p.id: p for p in mine}
    order = {s: i for i, s in enumerate(week.slots)}
    starters = sorted(today_assign.items(), key=lambda kv: order.get(kv[1], 99))
    used = set()
    for pid, slot in starters:
        p = by_id[pid]
        note = []
        if p.tag:
            note.append(f"{p.tag}: check status before tip")
        if p.slot == BENCH and not week.preview:
            note.append("**move in** (on your bench)")
        lines.append(f"| {SLOT_NAMES[slot]} | {p.name} | {game_label(p, week.today, abbrevs)} | {'; '.join(note)} |")
        used.add(pid)
    empty = len(week.slots) - len(starters)
    if empty:
        lines.append(f"| — | *{empty} slot{'s' if empty > 1 else ''} empty: not enough players with a game* | | |")
    lines.append("")

    val = {p.id: leverage_value(p, week, base.odds, base.lev) for p in mine}

    def why_sit(p):
        if week.today not in p.games:
            return "no game"
        if p.play_prob == 0:
            return p.tag or "out"
        if val[p.id] <= 0:
            return f"optional sit: hurts {', '.join(hurt_cats(p, week, base))}"
        return "no slot left"

    move_out = [p for p in mine if p.slot not in (BENCH, IR) and p.id not in used and not week.preview]
    if move_out:
        lines.append("**Move to bench:** " + ", ".join(f"{p.name} ({why_sit(p)})" for p in move_out))
    sitting = [p for p in mine if week.today in p.games and p.id not in used
               and (p.slot == BENCH or week.preview) and p.play_prob > 0]
    if sitting:
        lines.append("**Also has a game, stays benched:** " + ", ".join(
            f"{p.name} ({why_sit(p)})" for p in sorted(sitting, key=lambda p: -val[p.id])))
    out_today = [p for p in mine if week.today in p.games and p.play_prob == 0 and p.slot != IR]
    if out_today:
        lines.append("**Out:** " + ", ".join(
            f"{p.name} ({'--avail' if p.avail is not None else p.status.replace('_', ' ').lower()})" for p in out_today))
    lines.append("")

    # --- the other days
    if len(week.days) > 1:
        lines.append("## Rest of the week")
        lines.append("")
        lines.append("| Day | Start | Bench, next up first | Open |")
        lines.append("|---|---|---|---|")
        for i, sp in enumerate(week.days[1:], 1):
            assign = base.assignments[i]
            start = sorted(assign.items(), key=lambda kv: order.get(kv[1], 99))
            names = ", ".join(f"{SLOT_NAMES[s]} {by_id[pid].name}{'*' if by_id[pid].tag else ''}" for pid, s in start)
            bench = sorted((p for p in mine if sp in p.games and p.play_prob > 0 and p.id not in assign),
                           key=lambda p: -val[p.id])
            open_slots = list(week.slots)
            for s_ in assign.values():
                open_slots.remove(s_)
            lines.append(f"| {week.label(sp)} | {names or '—'} | {', '.join(p.name for p in bench) or '—'} "
                         f"| {' '.join(SLOT_NAMES[s_] for s_ in open_slots) or '—'} |")
        lines.append("")
        lines.append("\\* = injury tag; recheck before that day. Open slots are what a pickup can fill.")
        lines.append("")

    # --- categories
    lines.append("## Categories")
    lines.append("")
    lines.append("| Cat | You so far | Opp so far | You proj. | Opp proj. | P(win) | Swing |")
    lines.append("|---|---|---|---|---|---|---|")
    levs = {c: abs(base.lev[c]) * (base.odds[c]["sd"] or 0) for c in week.cats}
    top = max(levs.values()) or 1.0
    for c in week.cats:
        o = base.odds[c]
        so_far = lambda side, c=c: (side.get(RATIO[c][0], 0) / side[RATIO[c][1]]   # noqa: E731
                                    if c in RATIO and side.get(RATIO[c][1]) else side.get(c, 0))
        bar = "●" * round(3 * levs[c] / top) if levs[c] > 0.02 * top else "·"
        mine_now, opp_now = ("—", "—") if week.preview else (fmt_stat(c, so_far(week.me)), fmt_stat(c, so_far(week.opp)))
        lines.append(f"| {STAT_NAMES[c]} | {mine_now} | {opp_now} "
                     f"| {fmt_stat(c, o['mine'])} | {fmt_stat(c, o['theirs'])} | {pct(o['p'])} | {bar} |")
    lines.append("")
    lines.append("Swing ●●● = the categories your lineup choices move most; · = already decided.")
    lines.append("")

    # --- waiver wire
    lines.append("## Waiver wire")
    lines.append("")
    if pickups:
        now = base.win if roster_win is None else roster_win
        lines.append(f"| Add | Drop | When | P(win week), now {pct(now)} | Games left | Helps |")
        lines.append("|---|---|---|---|---|---|")
        for x in pickups:
            if x.add_day == week.days[0] and not week.preview:
                when = "today"
                if x.plays_today:
                    when += f", {game_label(x.add, week.today, abbrevs)}" + (" · **start**" if x.starts_today else "")
            else:
                when = week.label(x.add_day)
            if x.add_by != x.add_day:
                when += f" (or by {week.label(x.add_by)})"
            tag = f" ({x.add.tag})" if x.add.tag else ""
            lost = f" (loses {x.drop_games_lost} game{'s' * (x.drop_games_lost != 1)})" if x.drop_games_lost else ""
            lines.append(f"| {x.add.name}{tag} | {x.drop.name}{lost} | {when} | {pct(x.win)} "
                         f"({x.d_cats:+.2f} cats) | {x.games_left} | {', '.join(x.helps) or '—'} |")
        lines.append("")
        lines.append(acq_note)
    else:
        lines.append("No free agent raises your chances this week by more than a rounding error.")
    lines.append("")

    # --- footnotes
    healthy_ir = [p for p in mine if p.slot == IR and PLAY_PROB.get(p.status, 1) > 0]
    if healthy_ir:
        lines.append("**Healthy on IR:** " + ", ".join(p.name for p in healthy_ir)
                     + " (not counted; activate them if you have room)")
        lines.append("")
    if week.preview:
        lines.append("_Preview from today's rosters and injury tags; both teams start at zero. "
                     "Waiver moves are planned for days of that week._")
    lines.append(f"_Live from ESPN {datetime.now(ET):%-I:%M%p ET}. Injury tags change during the day: "
                 "recheck DTD/Q players before tip-off. Lines blend season, last-15 and ESPN projections._")
    return "\n".join(lines)


def hurt_cats(p, week, base):
    out = []
    for c in week.cats:
        if c in RATIO:
            m, a = RATIO[c]
            att = base.odds[c].get("my_att") or 0
            contrib = (p.line[m] - base.odds[c]["mine"] * p.line[a]) / att if att else 0
        else:
            contrib = p.line[c]
        if base.lev[c] * contrib < 0:
            out.append(STAT_NAMES[c])
    return out or ["close categories"]


# --- main ---------------------------------------------------------------------------

def preview_days(live, today, target, final_period, max_ahead=35):
    """Scoring periods of a later matchup period, asking ESPN day by day."""
    days = []
    for sp in range(today + 1, min(final_period, today + max_ahead) + 1):
        mp = live.matchup_period_of(sp)
        if mp == target:
            days.append(sp)
        elif days or (mp is not None and mp > target):
            break
    return days


def build(live, team_id, today_date=None, week_no=None, avail=None, keep=(), now=None):
    """Everything the report needs. Returns (markdown, summary dict), or (None, reason).
    week_no: a later matchup period to preview. avail: {name: chance he plays a game}.
    keep: names never offered as drops."""
    league = live.league()
    season = live.season
    status = league["status"]
    settings = league["settings"]
    today = league["scoringPeriodId"]
    if not status.get("isActive", True) or today > status["finalScoringPeriod"]:
        return None, "season not active"

    pro_teams = live.pro_schedule()
    games_today = [g for t in pro_teams for g in t.get("proGamesByScoringPeriod", {}).get(str(today), [])]
    current = status.get("currentMatchupPeriod")
    preview = week_no is not None and week_no != current
    if not games_today and week_no is None:   # quiet for the scheduler; --week plans anyway
        return None, "no NBA games today"

    if preview:
        if current is not None and week_no < current:
            return None, f"week {week_no} is over"
        days = preview_days(live, today, week_no, status["finalScoringPeriod"])
        if not days:
            return None, f"ESPN has no days for week {week_no} yet"
        day = live.day(days[0])
    else:
        day = live.day(today)
    match = next((m for m in day if team_id in (m["home"]["teamId"], m.get("away", {}).get("teamId"))), None)
    if match is None or "away" not in match:
        return None, "no matchup this week"
    me_side, opp_side = (match["home"], match["away"]) if match["home"]["teamId"] == team_id \
        else (match["away"], match["home"])
    teams = {t["id"]: t for t in league["teams"]}

    items = settings["scoringSettings"]["scoringItems"]
    cats = [int(i["statId"]) for i in items]
    order = [PTS, REB, AST, TPM, STL, BLK, FGP, FTP, TO]
    cats.sort(key=lambda c: order.index(c) if c in order else 99)
    reverse = {int(i["statId"]) for i in items if i.get("isReverseItem")}
    mp = match["matchupPeriodId"]
    if not preview:
        days = week_days(live, today, mp, status["finalScoringPeriod"])

    # calendar dates: today's games give today's date, and each scoring period is one day
    now = now or datetime.now(ET)
    today_date = today_date or (min(datetime.fromtimestamp(g["date"] / 1000, tz=timezone.utc).astimezone(ET)
                                    for g in games_today).date() if games_today else now.date())
    labels = {sp: f"{(d := today_date + timedelta(days=sp - today)):%a} {d.month}/{d.day}" for sp in days}
    # adds lock at the day's first tip-off: after it, a pickup plays from tomorrow
    tipped = bool(games_today) and now >= min(
        datetime.fromtimestamp(g["date"] / 1000, tz=timezone.utc) for g in games_today)
    first_add = 1 if (not preview and tipped) else 0

    week = Week(season=season, today=days[0], days=days, cats=cats, reverse=reverse,
                slots=active_slots(settings),
                me={} if preview else totals(me_side), opp={} if preview else totals(opp_side),
                my_team=teams[team_id], opp_team=teams[opp_side["teamId"]], matchup_period=mp,
                first_add=first_add, labels=labels, preview=preview)

    def roster(team):
        out = []
        for e in team["roster"]["entries"]:
            p = make_player(e["playerPoolEntry"], season, slot=e.get("lineupSlotId", BENCH))
            if p:
                p.status = e["playerPoolEntry"]["player"].get("injuryStatus") or p.status
                out.append(p)
        return out

    mine, opp = roster(teams[team_id]), roster(teams[opp_side["teamId"]])
    everyone = [p for t in league["teams"] for p in roster(t)]
    rostered = {p.id for p in everyone}
    free_agents = [p for p in (make_player(e, season) for e in live.free_agents(today))
                   if p and p.id not in rostered]
    attach_games(mine + opp + free_agents, pro_teams, days)
    for p in mine + opp + free_agents:
        if avail and p.name in avail:
            p.avail = float(avail[p.name])

    scale = generic_scaler(everyone, cats, reverse)
    for p in mine + free_agents:
        p.season_value = generic_value(p, cats, reverse, scale)

    gen = lambda p: generic_value(p, cats, reverse, scale) + 10.0   # noqa: E731
    opp_lineups, _ = plan(opp, days, week.slots, gen, allow_empty=False)
    base = solve(week, mine, opp_lineups, scale)
    roster_win, pickups = waiver_moves(week, mine, free_agents, opp_lineups, base, scale, keep=set(keep))

    acq = settings.get("acquisitionSettings", {})
    limit = acq.get("matchupAcquisitionLimit", -1)
    if limit and limit > 0:
        per = "day" if acq.get("matchupLimitPerScoringPeriod") else "week"
        used = (teams[team_id].get("transactionCounter", {}).get("matchupAcquisitionTotals") or {}).get(str(mp), 0)
        acq_note = (f"League limit: {int(limit)} add per {per}"
                    + (f" (you've made {int(used)} this week)." if per == "week" else "."))
    else:
        acq_note = ""
    acq_note += " Free agents only: players on waivers can't play until their claim clears."
    if first_add:
        acq_note += " Today's first game has tipped, so adds count from tomorrow."

    abbrevs = {t["id"]: t["abbrev"] for t in pro_teams}
    md = render(week, mine, opp_lineups, base, pickups, abbrevs, today_date, acq_note, roster_win)
    summary = dict(win=base.win, cats=base.exp_cats, opponent=team_name(week.opp_team),
                   moves=[f"start {p.name}" for p in mine if p.id in base.assignments[0] and p.slot == BENCH],
                   pickup=(f"add {pickups[0].add.name} / drop {pickups[0].drop.name}"
                           + ("" if pickups[0].add_day == days[0] and not preview
                              else f" on {week.label(pickups[0].add_day)}") if pickups else None))
    return md, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily lineup + waiver report for this week's matchup.")
    ap.add_argument("--team", type=int, default=4, help="your ESPN team id (default 4)")
    ap.add_argument("--league", type=int, default=None, help="ESPN league id (default: ESPN_LEAGUE_ID)")
    ap.add_argument("--season", type=int, default=config.current_season())
    ap.add_argument("--out", type=Path, help="also write the Markdown report here")
    ap.add_argument("--week", type=int, help="preview a later matchup period (default: this week)")
    ap.add_argument("--avail", action="append", default=[], metavar='"NAME=P"',
                    help="chance he plays each game, e.g. \"Darius Garland=0.5\" (repeatable)")
    ap.add_argument("--keep", nargs="+", default=[], metavar="NAME", help="never suggest dropping these players")
    a = ap.parse_args(argv)
    avail = {k.strip(): float(v) for k, v in (x.rsplit("=", 1) for x in a.avail)}

    from fbb.espn_live import LiveLeague
    live = LiveLeague(league_id=a.league, season=a.season)
    md, summary = build(live, a.team, week_no=a.week, avail=avail, keep=a.keep)
    if md is None:
        print(f"NO_GAMES: {summary}")
        return 0
    print(md)
    s = summary
    print(f"\nSUMMARY: {pct(s['win'])} to beat {s['opponent']}"
          + (f"; {', '.join(s['moves'])}" if s["moves"] else "; lineup moves: none")
          + (f"; {s['pickup']}" if s["pickup"] else ""))
    if a.out:
        a.out.write_text(md + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
