"""Plan a head-to-head categories week: who to start each day, and which free agents to add.

    python analysis/matchup_plan.py                    # this week, team 4
    python analysis/matchup_plan.py --week 6           # look ahead to next week's matchup
    python analysis/matchup_plan.py --avail "Darius Garland=0.5" --keep "Cameron Boozer"

Reads core.seasons, core.team_matchups, core.matchup_categories, core.rosters, core.players,
core.player_seasons, core.player_games, core.player_days, core.team_schedule, raw.espn_league,
raw.espn_matchup_days, raw.espn_pro_schedule and ref.espn_scoring_dates. Read-only.

Method:
  1. Projection: each player's per-game line is an exponentially weighted average of this season's
     games (half-life 15 games), shrunk toward last season's per-game line (worth 6 games).
     Chance of playing each scheduled game comes from injury status and the share of his team's
     last 10 games he played (`--avail "Name=p"` overrides it).
  2. The week so far: actual stats of both teams' starters on days already loaded.
  3. Rest of the week: Monte Carlo. Every sim, every remaining day, each team starts its best legal
     lineup from the players who play that day. Per-game stats are Poisson around the projection,
     with a shared gamma "game quality" factor per player (so points, rebounds... move together and
     weekly totals are as noisy as real ones). FG/FT makes are binomial on attempts.
  4. Matchup-tuned lineups. The opponent ranks players by generic 9-cat value (each category scaled
     by how much team weekly totals vary across the league). For my team, each category gets a
     leverage weight = P(the category decides the matchup) x density of the category margin at 0.
     Locked and lost categories get ~0 weight; close ones dominate. Player value = sum of his
     expected contribution x leverage (FG%/FT% as makes above my team's rate, per my team's attempts).
     A player whose tuned value is negative is better left on the bench (e.g. protecting FG% or TO).
  5. Waiver wire: every free agent x every droppable player x every possible add day is screened by
     the change in expected lineup value over the remaining days (positions included), then the best
     few are simulated in full against the same opponent draws. Adds are chosen greedily up to
     --max-adds, each kept only if it raises P(win week).
"""
import argparse
import datetime as dt
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment
from scipy.stats import norm

from fbb import config
from fbb.db import read_sql

pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40); pd.set_option('display.max_rows', 300)

ET = ZoneInfo('America/New_York')
CATS = ['PTS', 'REB', 'AST', 'STL', 'BLK', '3PM', 'TO', 'FG%', 'FT%']
LINE = ['PTS', 'REB', 'AST', 'STL', 'BLK', '3PM', 'TO', 'FGM', 'FGA', 'FTM', 'FTA']   # ESPN stat names
NBA_COLS = ['pts', 'reb', 'ast', 'stl', 'blk', 'fg3m', 'tov', 'fgm', 'fga', 'ftm', 'fta']  # core.player_games
I = {c: i for i, c in enumerate(LINE)}
HALF_LIFE = 15          # games: weight of this season's games halves every 15 games back
PRIOR_GAMES = 6         # last season's per-game line counts as this many games
DISPERSION = 10         # gamma shape of the per-game "quality" factor: CV = 1/sqrt(10) ~ 0.32
STATUS_P = {'ACTIVE': 0.95, 'PROBABLE': 0.85, 'DAY_TO_DAY': 0.6, 'QUESTIONABLE': 0.5, 'DOUBTFUL': 0.2,
            'OUT': 0.0, 'INJURY_RESERVE': 0.0, 'SUSPENSION': 0.0}
TUNED_SHARE = 0.85      # tuned value = 85% matchup leverage + 15% generic 9-cat value


# ---------------------------------------------------------------- queries
def q_context(season):
    return f"""
SELECT s.current_matchup_period,
       (SELECT min(game_date - scoring_period) FROM ref.espn_scoring_dates WHERE season = {season}) AS day0,
       (SELECT max(as_of) FROM core.rosters WHERE season = {season})                            AS rosters_as_of
FROM core.seasons s WHERE s.season = {season}"""


def q_slots(season):
    return f"""
SELECT ls.slot_name, k.value::int AS n
FROM raw.espn_league l
CROSS JOIN LATERAL jsonb_each_text(l.payload #> '{{settings,rosterSettings,lineupSlotCounts}}') k
JOIN ref.lineup_slots ls ON ls.slot_id = k.key::int
WHERE l.season = {season} AND l.kind = 'league' AND k.value::int > 0
ORDER BY ls.slot_id"""


def q_matchup(season, team, week):
    return f"""
SELECT tm.opponent_id, me.team_name AS my_name, op.team_name AS opp_name
FROM core.team_matchups tm
JOIN core.teams me ON me.season = tm.season AND me.team_id = tm.team_id
LEFT JOIN core.teams op ON op.season = tm.season AND op.team_id = tm.opponent_id
WHERE tm.season = {season} AND tm.team_id = {team} AND tm.matchup_period = {week}"""


def q_week_days(season, week):
    return f"""
SELECT scoring_period FROM raw.espn_matchup_days
WHERE season = {season} AND matchup_period = {week} ORDER BY 1"""


def q_schedule(season, first_sp, last_sp):
    # core.team_schedule.team and core.players.pro_team both use ESPN abbreviations (PHL, PHO)
    return f"""
SELECT team, scoring_period FROM core.team_schedule
WHERE season = {season} AND scoring_period BETWEEN {first_sp} AND {last_sp}"""


def q_first_tip(season, sp):
    return f"""
SELECT min((g ->> 'date')::bigint) AS ms
FROM raw.espn_pro_schedule s
CROSS JOIN LATERAL jsonb_array_elements(s.payload #> '{{settings,proTeams}}') t
CROSS JOIN LATERAL jsonb_each(t -> 'proGamesByScoringPeriod') sp
CROSS JOIN LATERAL jsonb_array_elements(sp.value) g
WHERE s.season = {season} AND sp.key = '{int(sp)}'"""


def q_actuals(season, week, teams, through_sp):
    sums = ',\n       '.join(f"coalesce(sum((stats ->> '{c}')::numeric), 0) AS \"{c}\"" for c in LINE)
    return f"""
SELECT team_id,
       {sums}
FROM core.player_days
WHERE season = {season} AND matchup_period = {week} AND team_id IN ({', '.join(map(str, teams))})
  AND scoring_period <= {through_sp} AND is_starter AND played
GROUP BY team_id"""


def q_league_norm(season):
    """Mean and spread of team weekly totals in complete 7-day regular-season weeks, this season and last."""
    return f"""
WITH wk AS (
    SELECT season, matchup_period, count(*) AS days
    FROM raw.espn_matchup_days WHERE season IN ({season - 1}, {season}) GROUP BY 1, 2
)
SELECT c.stat_name, avg(c.value) AS mean, stddev_samp(c.value) AS sd, count(*) AS n
FROM core.matchup_categories c
JOIN wk ON wk.season = c.season AND wk.matchup_period = c.matchup_period
JOIN core.matchups m ON m.season = c.season AND m.matchup_id = c.matchup_id
JOIN core.seasons s  ON s.season = c.season
-- is_playoff is null in some seasons, so playoffs are told apart by week number
WHERE wk.days = 7 AND c.matchup_period <= s.regular_season_matchups AND m.winner IN ('HOME', 'AWAY', 'TIE')
GROUP BY 1"""


def q_players(season, teams, today):
    """Both rosters plus every unrostered player with a game this season, with projection inputs."""
    cols = ', '.join(f'sum(wt * {c}) / sum(wt) AS {c}' for c in ['minutes'] + NBA_COLS)
    cur = ', '.join(f'c.{c} AS cur_{c}' for c in ['minutes'] + NBA_COLS)
    prv = ', '.join(f'l.{c} AS prev_{c}' for c in ['minutes'] + NBA_COLS)
    return f"""
WITH g AS (
    SELECT espn_player_id AS player_id, season, game_date, minutes, {', '.join(NBA_COLS)},
           row_number() OVER (PARTITION BY espn_player_id, season ORDER BY game_date DESC) AS rn
    FROM core.player_games
    WHERE season IN ({season - 1}, {season}) AND season_type = 'Regular Season'
      AND espn_player_id IS NOT NULL AND game_date < DATE '{today}'
),
w AS (SELECT *, CASE WHEN season = {season} THEN power(0.5, (rn - 1) / {HALF_LIFE}.0) ELSE 1 END AS wt FROM g),
agg AS (
    SELECT player_id, season, count(*) AS gp, sum(wt) AS wsum, max(game_date) AS last_game, {cols}
    FROM w GROUP BY 1, 2
),
tg AS (
    SELECT team, game_date, row_number() OVER (PARTITION BY team ORDER BY game_date DESC) AS rn
    FROM core.team_schedule WHERE season = {season} AND game_date < DATE '{today}'
),
last10 AS (SELECT team, min(game_date) AS since, count(*) AS n FROM tg WHERE rn <= 10 GROUP BY team)
SELECT p.player_id, p.player_name, p.pro_team, p.eligible_positions,
       coalesce(r.injury_status, p.injury_status) AS injury_status,
       r.team_id AS fantasy_team_id, r.lineup_slot, ps.roster_status, ps.pct_owned,
       c.gp AS cur_gp, c.wsum AS cur_wsum, c.last_game, {cur},
       l.gp AS prev_gp, {prv},
       t.n AS team_last10,
       (SELECT count(*) FROM core.player_games pg
         WHERE pg.espn_player_id = p.player_id AND pg.season = {season} AND pg.season_type = 'Regular Season'
           AND pg.game_date >= t.since AND pg.game_date < DATE '{today}') AS gp_last10
FROM core.players p
LEFT JOIN core.rosters r        ON r.season = {season} AND r.player_id = p.player_id
LEFT JOIN core.player_seasons ps ON ps.season = {season} AND ps.player_id = p.player_id
LEFT JOIN agg c ON c.player_id = p.player_id AND c.season = {season}
LEFT JOIN agg l ON l.player_id = p.player_id AND l.season = {season - 1}
LEFT JOIN last10 t ON t.team = p.pro_team
WHERE r.team_id IN ({', '.join(map(str, teams))})
   OR (r.team_id IS NULL AND c.gp > 0 AND ps.roster_status IN ('FREEAGENT', 'WAIVERS'))"""


# ---------------------------------------------------------------- projections
def project(r):
    """Per-game line (LINE order) for one player row of q_players, or None without recent games."""
    cur = np.array([r.get(f'cur_{c}') for c in NBA_COLS], dtype=float)
    prev = np.array([r.get(f'prev_{c}') for c in NBA_COLS], dtype=float)
    w = float(r.get('cur_wsum') or 0) if pd.notna(r.get('cur_wsum')) else 0.0
    has_cur, has_prev = w > 0 and not np.isnan(cur).any(), not np.isnan(prev).any()
    if has_cur and has_prev:
        k = min(PRIOR_GAMES, float(r.get('prev_gp') or 0))
        line = (w * cur + k * prev) / (w + k)
    elif has_cur:
        line = cur
    elif has_prev:
        line = prev
    else:
        return None
    line = np.clip(line, 0, None)
    line[I['FGM']] = min(line[I['FGM']], line[I['FGA']]); line[I['FTM']] = min(line[I['FTM']], line[I['FTA']])
    return line


def play_prob(r, overrides):
    """Chance he plays a given scheduled game this week."""
    if r['player_name'] in overrides:
        return float(overrides[r['player_name']])
    if r.get('lineup_slot') == 'IR':
        return 0.0
    p = STATUS_P.get(r.get('injury_status') or 'ACTIVE', STATUS_P['ACTIVE'])
    n = r.get('team_last10')
    if pd.notna(n) and n:
        played = float(r.get('gp_last10') or 0)
        # hasn't played lately (DNPs, minor injury, G League): scale down, with a little shrinkage
        p *= min(1.0, (played + 1) / (n * 0.9 + 1))
    if not (r.get('cur_gp') or 0) > 0:
        p *= 0.5   # no games this season yet
    return float(np.clip(p, 0, 0.97))


# ---------------------------------------------------------------- value
def contrib(lines, fg_pct, fga_tot, ft_pct, fta_tot):
    """Per-game contribution to each category (CATS order); TO counts negative, FG%/FT% as
    makes above the team rate per team attempt."""
    L = np.atleast_2d(lines)
    out = np.column_stack([L[:, I[c]] for c in ['PTS', 'REB', 'AST', 'STL', 'BLK', '3PM']] +
                          [-L[:, I['TO']],
                           (L[:, I['FGM']] - fg_pct * L[:, I['FGA']]) / max(fga_tot, 1),
                           (L[:, I['FTM']] - ft_pct * L[:, I['FTA']]) / max(fta_tot, 1)])
    return out


def generic_weights(norm_df):
    """Weights and context for generic 9-cat value from league weekly spreads."""
    s = norm_df.set_index('stat_name')
    w = np.array([1 / s.loc[c, 'sd'] for c in CATS])
    ctx = dict(fg_pct=s.loc['FGM', 'mean'] / s.loc['FGA', 'mean'], fga_tot=s.loc['FGA', 'mean'],
               ft_pct=s.loc['FTM', 'mean'] / s.loc['FTA', 'mean'], fta_tot=s.loc['FTA', 'mean'])
    return w, ctx


def team_ctx(tot):
    """Context for contrib() from projected team weekly totals [sims, LINE]."""
    m = tot.mean(0)
    return dict(fg_pct=m[I['FGM']] / max(m[I['FGA']], 1), fga_tot=m[I['FGA']],
                ft_pct=m[I['FTM']] / max(m[I['FTA']], 1), fta_tot=m[I['FTA']])


def leverage(r, margins, floors):
    """Weight of each category: P(it decides the matchup) x density of its margin at 0.
    r: [sims, 9] results (+1/0/-1); margins: [sims, 9] my margin (TO reversed)."""
    lev = np.zeros(len(CATS))
    tot = r.sum(1)
    for c in range(len(CATS)):
        d = tot - r[:, c]
        pivot = ((d == 0) + 0.5 * (np.abs(d) == 1)).mean()
        mu, sd = margins[:, c].mean(), max(margins[:, c].std(), floors[c])
        lev[c] = pivot * norm.pdf(mu / sd) / sd
    return lev


# ---------------------------------------------------------------- lineups and simulation
def best_lineup(val, elig, bench_negative):
    """(player indices, slot indices) of the highest-value legal lineup.
    elig: [n, slots] bool. bench_negative: leave players with value <= 0 out."""
    n = len(val)
    if not n:
        return np.array([], int), np.array([], int)
    if bench_negative:
        keep = val > 0
        cost = np.where(elig & keep[:, None], -val[:, None], 1e6)
    else:   # start anyone who can play; value only decides who when there are too many
        cost = np.where(elig, -(val[:, None] + 1e3), 1e6)
    ri, ci = linear_sum_assignment(cost)
    ok = cost[ri, ci] < 1e5
    return ri[ok], ci[ok]


class Team:
    """Arrays for one fantasy team over the simulated days."""

    def __init__(self, players, slots, n_days):
        self.players = players                   # list of dicts
        self.n_days = n_days
        self.slots = slots
        self.refresh()

    def refresh(self):
        P = self.players
        self.names = [p['name'] for p in P]
        self.line = np.stack([p['line'] for p in P]) if P else np.zeros((0, len(LINE)))
        self.p = np.array([p['p'] for p in P])
        self.has = np.stack([p['has'] for p in P]) if P else np.zeros((0, self.n_days), bool)
        self.elig = np.array([[s == 'UT' or s in p['pos'] for s in self.slots] for p in P]).reshape(len(P), len(self.slots))
        self.pid = [p['player_id'] for p in P]


def simulate(team, val, bench_negative, sims, seed, actual):
    """Weekly totals [sims, LINE] and starts [sims, players].
    val: [players, days] lineup value; bench_negative: [days] bool.
    Each player's random draws come from his own stream, so scenarios that share a player share his luck."""
    N, D = team.has.shape
    rngs = [np.random.default_rng([seed, int(pid)]) for pid in team.pid]
    u = np.stack([g.random((sims, D)) for g in rngs], 1) if N else np.zeros((sims, 0, D))
    avail = (u < team.p[None, :, None]) & team.has[None]
    starts = np.zeros((sims, N))
    for d in range(D):
        a = avail[:, :, d]
        if not a.any():
            continue
        pats, inv = np.unique(a, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        for k, pat in enumerate(pats):
            idx = np.flatnonzero(pat)
            if not len(idx):
                continue
            ri, _ = best_lineup(val[idx, d], team.elig[idx], bench_negative[d])
            if len(ri):
                starts[np.ix_(inv == k, idx[ri])] += 1
    tot = np.tile(np.asarray(actual, float), (sims, 1))
    for i, g in enumerate(rngs):
        s = starts[:, i]
        m = np.where(s > 0, g.gamma(np.maximum(DISPERSION * s, 1e-9), 1 / DISPERSION), 0.0)
        L = team.line[i]
        for c in ['PTS', 'REB', 'AST', 'STL', 'BLK', '3PM', 'TO']:
            tot[:, I[c]] += g.poisson(m * L[I[c]])
        for mk, at in (('FGM', 'FGA'), ('FTM', 'FTA')):
            att = g.poisson(m * L[I[at]])
            pct = L[I[mk]] / L[I[at]] if L[I[at]] > 0 else 0.0
            tot[:, I[at]] += att
            tot[:, I[mk]] += g.binomial(att, min(pct, 1.0))
    return tot, starts


def compare(A, B):
    """Category results of A vs B, [sims, 9] in CATS order: +1 A wins, -1 B wins, 0 tie;
    and A's margins (TO reversed so positive is good)."""
    res, mar = [], []
    for c in ['PTS', 'REB', 'AST', 'STL', 'BLK', '3PM']:
        mar.append(A[:, I[c]] - B[:, I[c]])
    mar.append(B[:, I['TO']] - A[:, I['TO']])
    for mk, at in (('FGM', 'FGA'), ('FTM', 'FTA')):
        pa = A[:, I[mk]] / np.maximum(A[:, I[at]], 1); pb = B[:, I[mk]] / np.maximum(B[:, I[at]], 1)
        mar.append(pa - pb)
    mar = np.column_stack(mar)
    res = np.sign(np.where(np.abs(mar) < 1e-12, 0, mar))
    return res, mar


def summarize(res):
    w, l = (res > 0).sum(1), (res < 0).sum(1)
    return {'P(win)': float((w > l).mean()), 'P(tie)': float((w == l).mean()),
            'exp_cats': float(((res > 0) + 0.5 * (res == 0)).sum(1).mean()),
            'cats': ((res > 0) + 0.5 * (res == 0)).mean(0)}


# ---------------------------------------------------------------- the week
class Week:
    def __init__(self, a):
        self.a = a
        season, team = a.season, a.team
        ctx = read_sql(q_context(season)).iloc[0]
        self.week = int(a.week or ctx.current_matchup_period)
        self.day0 = pd.Timestamp(ctx.day0).date()
        as_of = pd.Timestamp(ctx.rosters_as_of)
        self.as_of = (as_of.tz_localize('UTC') if as_of.tzinfo is None else as_of).tz_convert(ET)
        now = dt.datetime.now(ET)
        self.today = dt.date.fromisoformat(a.today) if a.today else now.date()
        self.today_sp = (self.today - self.day0).days

        sl = read_sql(q_slots(season))
        self.slots = [s for s, n in zip(sl.slot_name, sl.n) for _ in range(n) if s not in ('BE', 'IR')]
        self.roster_spots = int(sl[sl.slot_name != 'IR'].n.sum())

        m = read_sql(q_matchup(season, team, self.week))
        if m.empty or pd.isna(m.opponent_id.iloc[0]):
            sys.exit(f'team {team} has no opponent in week {self.week}')
        m = m.iloc[0]
        self.opp, self.my_name, self.opp_name = int(m.opponent_id), m.my_name.strip(), m.opp_name.strip()

        sps = read_sql(q_week_days(season, self.week)).scoring_period.astype(int).tolist()
        if not sps:
            sys.exit(f'week {self.week}: ESPN has not placed its days yet')
        self.week_sps = sps
        if self.today_sp > sps[-1]:
            sys.exit(f'week {self.week} is over (it ended on {self.date(sps[-1])})')

        # days with actual stats: before today and loaded by the last refresh (it runs in the morning,
        # so the refresh date's previous day is complete)
        loaded_through = (self.as_of.date() - self.day0).days - 1
        self.actual_through = min(self.today_sp - 1, loaded_through)
        self.first_sim = max(self.actual_through + 1, sps[0])
        self.sim_sps = [sp for sp in sps if sp >= self.first_sim]
        self.stale = [sp for sp in self.sim_sps if sp < self.today_sp]
        self.plan_sps = [sp for sp in self.sim_sps if sp >= self.today_sp]

        # earliest day a free agent can join: today unless today's first game has tipped
        tip = read_sql(q_first_tip(season, self.today_sp)).ms.iloc[0] if self.today_sp >= sps[0] else None
        self.tipped = (not a.today and tip is not None and pd.notna(tip)
                       and now >= dt.datetime.fromtimestamp(int(tip) / 1000, ET))
        self.first_add = max(self.today_sp + (1 if self.tipped else 0), sps[0])

        sched = read_sql(q_schedule(season, sps[0], sps[-1]))
        self.games = set(zip(sched.team, sched.scoring_period.astype(int)))

        act = read_sql(q_actuals(season, self.week, [team, self.opp], self.actual_through)).set_index('team_id')
        self.actual = {t: (act.loc[t, LINE].astype(float).values if t in act.index else np.zeros(len(LINE)))
                       for t in (team, self.opp)}

        self.norm = read_sql(q_league_norm(season))
        self.w_gen, self.ctx_gen = generic_weights(self.norm)
        sd = self.norm.set_index('stat_name').sd
        self.floors = np.array([max(sd[c] * 0.02, 0.5) if c not in ('FG%', 'FT%') else sd[c] * 0.02 for c in CATS])

        overrides = {k.strip(): float(v) for k, v in (s.rsplit('=', 1) for s in a.avail)}
        P = read_sql(q_players(season, [team, self.opp], self.today.isoformat()))
        self.skipped = []
        self.pool = {}
        for _, r in P.iterrows():
            line = project(r)
            if line is None:
                if pd.notna(r.fantasy_team_id):
                    self.skipped.append((r.player_name, 'no NBA games in two seasons'))
                continue
            pos = set(r.eligible_positions) if isinstance(r.eligible_positions, (list, np.ndarray)) else set()
            self.pool[r.player_name] = {
                'player_id': int(r.player_id), 'name': r.player_name, 'pro_team': r.pro_team, 'pos': pos,
                'line': line, 'p': play_prob(r, overrides),
                'status': r.injury_status or 'ACTIVE', 'slot': r.lineup_slot,
                'fteam': int(r.fantasy_team_id) if pd.notna(r.fantasy_team_id) else None,
                'roster_status': r.roster_status, 'pct_owned': r.pct_owned,
                'mpg': r.cur_minutes if pd.notna(r.cur_minutes) else r.prev_minutes,
                'gp': int(r.cur_gp) if pd.notna(r.cur_gp) else 0,
                'has': np.array([(r.pro_team, sp) in self.games for sp in self.sim_sps]),
            }
        missing = [n for n in overrides if n not in self.pool]
        if missing:
            print(f'  note: --avail names not found: {missing}')

    def date(self, sp):
        return self.day0 + dt.timedelta(days=int(sp))

    def label(self, sp):
        d = self.date(sp)
        return f"{d:%a} {d.month}/{d.day}"

    def roster(self, team_id):
        return [dict(p) for p in self.pool.values() if p['fteam'] == team_id]


# ---------------------------------------------------------------- planning
class Planner:
    def __init__(self, W, sims, seed):
        self.W, self.sims, self.seed = W, sims, seed
        D = len(W.sim_sps)
        self.D = D
        self.stale_mask = np.array([sp in W.stale for sp in W.sim_sps])
        self.me = Team(W.roster(W.a.team), W.slots, D)
        self.opp = Team(W.roster(W.opp), W.slots, D)
        v_opp = self.gen_value(self.opp.line)
        self.opp_tot, self.opp_starts = simulate(self.opp, np.tile(v_opp[:, None], (1, D)),
                                                 np.zeros(D, bool), sims, seed + 1, W.actual[W.opp])

    def gen_value(self, lines):
        return contrib(lines, **self.W.ctx_gen) @ self.W.w_gen if len(lines) else np.zeros(0)

    def values(self, team, lev, ctx):
        """[players, days] lineup values: tuned on plan days, generic on stale (already played) days.
        Both parts are scaled by my original roster, so every scenario ranks players the same way."""
        g = self.gen_value(team.line) / (np.mean(np.abs(self.gen_value(self.me.line))) or 1)
        if lev is None or not np.any(lev > 0):
            v = g
        else:
            t = contrib(team.line, **ctx) @ lev / (np.mean(np.abs(contrib(self.me.line, **ctx) @ lev)) or 1)
            v = TUNED_SHARE * t + (1 - TUNED_SHARE) * g
        V = np.tile(v[:, None], (1, self.D))
        V[:, self.stale_mask] = g[:, None]
        return V

    def run(self, team, lev=None, ctx=None, tuned=True):
        V = self.values(team, lev if tuned else None, ctx)
        bench_neg = (~self.stale_mask) & tuned & (lev is not None)
        tot, starts = simulate(team, V, bench_neg, self.sims, self.seed, self.W.actual[self.W.a.team])
        res, mar = compare(tot, self.opp_tot)
        return dict(tot=tot, starts=starts, res=res, mar=mar, V=V, bench_neg=bench_neg, **summarize(res))

    def tune(self):
        """Default (generic) lineups, then two rounds of leverage-tuned lineups."""
        self.base = self.run(self.me, tuned=False)
        lev, ctx, out = leverage(self.base['res'], self.base['mar'], self.W.floors), team_ctx(self.base['tot']), self.base
        for _ in range(2):
            out = self.run(self.me, lev, ctx)
            lev2, ctx2 = leverage(out['res'], out['mar'], self.W.floors), team_ctx(out['tot'])
            lev, ctx = 0.5 * lev + 0.5 * lev2, ctx2
        self.lev, self.ctx = lev, ctx
        self.tuned = self.run(self.me, lev, ctx)
        if self.tuned['P(win)'] + 0.5 * self.tuned['P(tie)'] < self.base['P(win)'] + 0.5 * self.base['P(tie)'] - 0.005:
            # leverage can't beat plain 9-cat value here: plan with that instead
            self.lev = None
            self.tuned = dict(self.base)
        return self

    # ------------------------------------------------ waiver wire
    def day_values(self, team, V):
        """Expected lineup value of each plan day, from players likely to play (deterministic)."""
        out = np.zeros(self.D)
        for d in range(self.D):
            idx = np.flatnonzero(team.has[:, d] & (team.p > 0.25))
            ev = V[idx, d] * team.p[idx]
            ri, _ = best_lineup(ev, team.elig[idx], True)
            out[d] = ev[ri].sum()
        return out

    def scan(self, max_adds, drop_pool, keep, force_drop, n_full):
        W = self.W
        adds, log = [], []
        me = Team([dict(p) for p in self.me.players], W.slots, self.D)
        current = self.tuned
        taken = {p['name'] for p in me.players}
        fas = [p for p in W.pool.values() if p['fteam'] is None and p['p'] > 0 and p['has'].any()
               and p['name'] not in taken]
        for step in range(max_adds):
            # drop candidates: lowest generic per-game value among active players (injuries ignored,
            # so a hurt star is never offered), minus --keep; --drop names always included
            active = [i for i, p in enumerate(me.players)
                      if p['slot'] != 'IR' and not p.get('added') and not p.get('dropped')]
            gen = self.gen_value(me.line)
            order = sorted(active, key=lambda i: gen[i])
            drops = [i for i in order if me.players[i]['name'] not in keep][:drop_pool]
            drops += [i for i in active if me.players[i]['name'] in force_drop and i not in drops]
            n_active = sum(1 for p in me.players if p['slot'] != 'IR' and not p.get('dropped'))
            if n_active < W.roster_spots:
                drops = [None] + drops
            V = self.values(me, self.lev, self.ctx)
            base_days = self.day_values(me, V)
            first = [W.sim_sps.index(sp) for sp in W.sim_sps if sp >= W.first_add]
            if not first:
                break
            screened = []
            for c in fas:
                if any(c['name'] == p['name'] for p in me.players):
                    continue
                earliest = W.first_add + (1 if c['roster_status'] == 'WAIVERS' else 0)
                days_ok = [d for d in first if W.sim_sps[d] >= earliest]
                if not days_ok or not c['has'][days_ok].any():
                    continue
                for j in drops:
                    trial = [p for k, p in enumerate(me.players) if k != j] + [c]
                    T = Team(trial, W.slots, self.D)
                    Vt = self.values(T, self.lev, self.ctx)
                    delta = self.day_values(T, Vt) - base_days        # if added before day d and j dropped
                    gain = {a: delta[a:].sum() for a in days_ok}
                    top = max(gain.values())
                    ok = [a for a in days_ok if gain[a] >= top - 1e-9]
                    screened.append((top, c, j, ok[0], ok[-1]))
            screened.sort(key=lambda x: -x[0])
            seen, short = set(), []
            for s in screened:
                if s[0] <= 0 or s[1]['name'] in seen:
                    continue
                seen.add(s[1]['name']); short.append(s)
                if len(short) >= n_full:
                    break
            rows = []
            for score, c, j, a, a_last in short:
                trial = [dict(p) for p in me.players]
                if j is not None:
                    trial[j]['has'] = trial[j]['has'] & (np.arange(self.D) < a)
                    trial[j]['dropped'] = True
                cc = dict(c); cc['has'] = c['has'] & (np.arange(self.D) >= a); cc['added'] = True
                trial.append(cc)
                T = Team(trial, W.slots, self.D)
                r = self.run(T, self.lev, self.ctx, tuned=self.lev is not None)
                helps = contrib(c['line'], **self.ctx) @ np.diag(self.lev) if self.lev is not None else contrib(c['line'], **W.ctx_gen) @ np.diag(W.w_gen)
                order_c = np.argsort(-helps[0])
                rows.append({'add': c['name'], 'team': c['pro_team'], 'status': {'FREEAGENT': 'FA', 'WAIVERS': 'WV'}.get(c['roster_status'], ''),
                             'inj': '' if c['status'] == 'ACTIVE' else c['status'],
                             'own%': round(c['pct_owned'] or 0, 1), 'mpg': round(c['mpg'] or 0, 1),
                             'games': int(c['has'][a:].sum()), 'add_on': W.label(W.sim_sps[a]),
                             'add_by': W.label(W.sim_sps[a_last]),
                             'drop': me.players[j]['name'] if j is not None else '(open spot)',
                             'drop_games_lost': int(me.players[j]['has'][a:].sum()) if j is not None else 0,
                             'dP(win)': r['P(win)'] - current['P(win)'], 'd_exp_cats': r['exp_cats'] - current['exp_cats'],
                             'helps': ' '.join(CATS[k] for k in order_c[:3] if helps[0, k] > 0),
                             '_res': r, '_trial': trial, '_c': c, '_day': a})
            if not rows:
                break
            # P(win) is what counts, but it moves in lumps; expected categories breaks near-ties
            rows.sort(key=lambda x: -(x['dP(win)'] + 0.1 * x['d_exp_cats']))
            log.append((step + 1, current, rows))
            best = rows[0]
            if best['dP(win)'] < 0.01 and best['d_exp_cats'] < 0.15:
                break
            adds.append(best)
            me = Team([p for p in best['_trial']], W.slots, self.D)
            current = best['_res']
            fas = [p for p in fas if p['name'] != best['add']]
        return adds, log, me

# ---------------------------------------------------------------- report
def lineup_plan(P, team, V, bench_neg):
    """Text plan per remaining day: players expected to play (p > 0.25), best lineup by value."""
    W = P.W
    out = []
    gen = P.gen_value(team.line)
    for d, sp in enumerate(W.sim_sps):
        if sp < W.today_sp:
            continue
        idx = np.flatnonzero(team.has[:, d] & (team.p > 0.25))
        ri, ci = best_lineup(V[idx, d], team.elig[idx], bench_neg[d])
        started = set(idx[ri])
        tag = lambda i: team.names[i] + ('*' if team.p[i] < 0.8 else '')
        lineup = ', '.join(f'{W.slots[c]} {tag(idx[r])}' for c, r in sorted(zip(ci, ri)))
        sitting = sorted((i for i in idx if i not in started), key=lambda i: -V[i, d])
        open_slots = [W.slots[c] for c in range(len(W.slots)) if c not in set(ci)]
        gi, _ = best_lineup(gen[idx], team.elig[idx], False)
        gen_started = set(idx[gi])
        changes = [f'start {team.names[i]}' for i in started - gen_started]
        changes += [f'sit {team.names[i]}' + (' (hurts more than helps)' if V[i, d] <= 0 else '')
                    for i in gen_started - started]
        out.append(f'  {W.label(sp)} ({len(idx)} with games): {lineup or "-"}')
        if sitting:
            out.append(f'      bench, next up first: {", ".join(tag(i) for i in sitting)}')
        if open_slots:
            out.append(f'      open: {" ".join(open_slots)}')
        if changes:
            out.append(f'      vs 9-cat default: {"; ".join(changes)}')
    return '\n'.join(out)


def report(W, P, adds, log):
    a = W.a
    print(f"\n=== Week {W.week}: {W.my_name} vs {W.opp_name}  ({W.label(W.week_sps[0])} - {W.label(W.week_sps[-1])})")
    age = (dt.datetime.now(ET) - W.as_of).total_seconds() / 3600
    print(f"    data as of {W.as_of:%a %m/%d %H:%M} ET ({age:.0f}h ago); today {W.label(W.today_sp)}; "
          f"actual stats through {W.label(W.actual_through) if W.actual_through >= W.week_sps[0] else '(none yet)'}")
    if W.stale:
        print(f"    WARNING: days {', '.join(W.label(s) for s in W.stale)} are past but not loaded; they are simulated. "
              f"Run `python -m fbb.refresh` for actuals and current rosters.")
    if age > 18:
        print("    WARNING: rosters and injury news are over 18h old; recent adds/drops are missing.")
    if W.tipped:
        print("    Today's first game has tipped: adds made now count from tomorrow.")
    if W.skipped:
        print('    left out: ' + '; '.join(f'{n} ({why})' for n, why in W.skipped))

    me, op = W.actual[a.team], W.actual[W.opp]
    mt, ot = P.tuned['tot'].mean(0), P.opp_tot.mean(0)

    def cat_vals(v):
        return [v[I['PTS']], v[I['REB']], v[I['AST']], v[I['STL']], v[I['BLK']], v[I['3PM']], v[I['TO']],
                v[I['FGM']] / max(v[I['FGA']], 1), v[I['FTM']] / max(v[I['FTA']], 1)]
    lev = P.lev if P.lev is not None else np.zeros(len(CATS))
    lev_share = lev * np.mean(np.abs(contrib(P.me.line, **P.ctx)), 0) if P.lev is not None else lev
    lev_share = lev_share / lev_share.sum() if lev_share.sum() > 0 else lev_share
    pc = P.tuned['cats']
    status = ['LOCKED' if p >= 0.95 else 'LOST' if p <= 0.05 else 'SWING' if 0.25 <= p <= 0.75 else
              ('LEAN WIN' if p > 0.5 else 'LEAN LOSS') for p in pc]
    T = pd.DataFrame({'me so far': cat_vals(me), 'opp so far': cat_vals(op), 'me proj': cat_vals(mt),
                      'opp proj': cat_vals(ot), 'P(win cat)': pc, 'weight': lev_share, 'status': status}, index=CATS)
    fmt = T.copy().astype(object)
    for c in ['me so far', 'opp so far', 'me proj', 'opp proj']:
        fmt[c] = [f'{v:.3f}' if k in ('FG%', 'FT%') else f'{v:.0f}' if 'so far' in c else f'{v:.1f}' for k, v in zip(CATS, T[c])]
    fmt['P(win cat)'] = [f'{v:.0%}' for v in T['P(win cat)']]
    fmt['weight'] = [f'{v:.0%}' for v in T['weight']]
    print('\n--- Categories (projection = actual so far + simulated rest of week, matchup-tuned lineups)')
    print(fmt.to_string())
    b, t = P.base, P.tuned
    print(f"\n    P(win week): {t['P(win)']:.0%} with matchup-tuned lineups vs {b['P(win)']:.0%} starting by 9-cat value"
          f"  (P(tie) {t['P(tie)']:.0%}; expected cats {t['exp_cats']:.2f} of 9)")
    if P.lev is None:
        print("    (tuning didn't beat plain 9-cat value here, so the plan uses 9-cat value)")

    print('\n--- Games left (players likely to play / starting slots)')
    rows = []
    for d, sp in enumerate(W.sim_sps):
        if sp < W.today_sp:
            continue
        rows.append({'day': W.label(sp), 'me': int((P.me.has[:, d] & (P.me.p > 0.25)).sum()),
                     'opp': int((P.opp.has[:, d] & (P.opp.p > 0.25)).sum()), 'slots': len(W.slots)})
    G = pd.DataFrame(rows)
    if len(G):
        G['me starts (max)'] = np.minimum(G.me, G.slots); G['opp starts (max)'] = np.minimum(G.opp, G.slots)
        print(G.to_string(index=False))
        print(f"    total: me {G['me starts (max)'].sum()} vs opp {G['opp starts (max)'].sum()} (upper bound; positions can cap it)")

    print('\n--- Daily lineups (as rostered now; * = less than 80% to play; re-check injuries before lock)')
    print(lineup_plan(P, P.me, P.tuned['V'], P.tuned['bench_neg']))
    out = [(n, p['status']) for n, p in zip(P.me.names, P.me.players) if p['p'] == 0 and p['slot'] != 'IR']
    if out:
        print('    not expected to play: ' + ', '.join(f'{n} ({s})' for n, s in out))

    if a.no_waivers:
        return
    print(f"\n--- Waiver wire (adds from {W.label(W.first_add)}; ΔP from {a.sims} sims, ±~1pp noise)")
    if not log:
        print('    no free agent adds value this week')
        return
    for step, cur, rows in log:
        R = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith('_')} for r in rows])
        R['dP(win)'] = R['dP(win)'].map(lambda v: f'{v:+.1%}'); R['d_exp_cats'] = R['d_exp_cats'].map(lambda v: f'{v:+.2f}')
        title = 'best single adds' if step == 1 else f'after add {step - 1}, next best'
        print(f'  {title} (P(win) now {cur["P(win)"]:.0%}):')
        print(R.to_string(index=False))
    if adds:
        print('\n  Recommended moves, by day:')
        for k, r in enumerate(sorted(adds, key=lambda r: r['_day']), 1):
            when = r['add_on'] if r['add_on'] == r['add_by'] else f"{r['add_on']} (no later than {r['add_by']})"
            print(f"   {k}. {when}: add {r['add']} ({r['team']}, {r['games']} game{'s' * (r['games'] != 1)} left), "
                  f"drop {r['drop']}  [{r['dP(win)']:+.1%} P(win); helps {r['helps']}]")
        print(f"  P(win week) with all of them: {adds[-1]['_res']['P(win)']:.0%} (now {P.tuned['P(win)']:.0%})")
    else:
        print('\n  No add clears the bar (+1pp P(win) or +0.15 expected cats).')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--team', type=int, default=4, help='your ESPN team_id (default 4)')
    ap.add_argument('--week', type=int, help='matchup period (default: the current one)')
    ap.add_argument('--season', type=int, default=config.current_season())
    ap.add_argument('--today', help='pretend today is this date (YYYY-MM-DD)')
    ap.add_argument('--avail', action='append', default=[], help='"Name=0.5": chance he plays each game')
    ap.add_argument('--keep', nargs='+', default=[], help='never suggest dropping these players')
    ap.add_argument('--drop', nargs='+', default=[], help='always consider dropping these players')
    ap.add_argument('--drop-pool', type=int, default=3, help='consider dropping my N lowest-value players')
    ap.add_argument('--max-adds', type=int, default=2)
    ap.add_argument('--candidates', type=int, default=10, help='free agents simulated in full per add')
    ap.add_argument('--no-waivers', action='store_true')
    ap.add_argument('--sims', type=int, default=2000)
    ap.add_argument('--seed', type=int, default=11)
    a = ap.parse_args(argv)

    W = Week(a)
    if not W.sim_sps:
        sys.exit('nothing left to simulate this week')
    P = Planner(W, a.sims, a.seed).tune()
    adds, log = [], []
    if not a.no_waivers and W.plan_sps:
        adds, log, _ = P.scan(a.max_adds, a.drop_pool, set(a.keep), set(a.drop), a.candidates)
    report(W, P, adds, log)


if __name__ == '__main__':
    main()
