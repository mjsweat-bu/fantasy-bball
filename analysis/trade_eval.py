"""Evaluate a dynasty trade: rest-of-season lineup simulation + multi-year value with an aging curve.

    python analysis/trade_eval.py --team 4 \
        --give "Cameron Boozer" "Jaylen Brown" \
        --get "Trae Young" "James Harden" "Dillon Brooks" \
        --targets PTS AST STL FG3M FT \
        --avail "Stephen Curry=0.6" \
        --alt "Jaylen Brown > Trae Young" \
        --alt "Jaylen Brown > Trae Young, James Harden / Klay Thompson"

Reads core.player_games, core.rosters, core.players, core.teams, core.team_matchups,
core.matchup_categories, ref.player_xwalk, raw.espn_league and raw.espn_pro_schedule; fetches
per-season player ages from stats.nba.com once and caches them in data/nba_player_seasons.csv
(--refresh-ages to refetch).

Method (see docs/trade_2026-11-16_boozer_brown.md for a worked example):
  1. Value: 9-cat per-game z-scores vs each season's top-156 pool (12 teams x 13).
     FG%/FT% are volume-weighted impact (makes above the pool rate x attempts); TO counts negative.
  2. Aging curve: delta method on per-game stats, consecutive seasons with GP >= 20 both years,
     smoothed over a 3-year age window. Expected games next season by age (0 if gone).
  3. Current talent: this season + prior two seasons (aged to today), recency-weighted per game.
  4. Dynasty: talent aged forward 5 seasons; value above replacement (avg of players 157-180)
     x expected share of games.
  5. Rest of season: Monte Carlo over the real NBA schedule for all 12 teams. Each day every
     team starts its best legal lineup (PG SG SF PF C UT UT) from the players who play that day
     (my team by target-weighted value, everyone else by 9-cat value). Weekly totals decide the
     real remaining H2H schedule, then the 6-team bracket -> P(win week), final wins,
     P(playoffs), P(title). Teams the trade doesn't touch reuse identical simulated weeks
     (common random numbers), so differences between scenarios come from the trade, not noise.
"""
import argparse, math, sys, time

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from fbb import config
from fbb.db import read_sql

pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40); pd.set_option('display.max_rows', 300)

CNT = ['PTS', 'REB', 'AST', 'STL', 'BLK', 'FG3M', 'TOV']
CATS = CNT + ['FG', 'FT']
LINE = ['PTS', 'REB', 'AST', 'STL', 'BLK', 'FG3M', 'TOV', 'FGM', 'FGA', 'FTM', 'FTA']
POOL = 156                       # 12 teams x 13 roster-relevant players
RECENCY = {0: 1.0, 1: 0.5, 2: 0.25}   # weight per game played: this season, last season, two ago
SLOTS = ['PG', 'SG', 'SF', 'PF', 'C', 'UT', 'UT']
# ESPN abbreviations differ between core.players and the ESPN pro schedule
ALIAS = {'GSW': 'GS', 'PHL': 'PHI', 'WAS': 'WSH', 'NOP': 'NO', 'NYK': 'NY', 'SAS': 'SA', 'UTA': 'UTAH', 'PHO': 'PHX'}
AGES_CSV = config.ROOT / 'data' / 'nba_player_seasons.csv'


# ---------------------------------------------------------------- data
def load_player_seasons(first, last, refresh=False):
    """Per-game season lines with AGE from stats.nba.com (cached)."""
    if AGES_CSV.exists() and not refresh:
        A = pd.read_csv(AGES_CSV)
        if A.season.min() <= first and A.season.max() >= last:
            return A
    from nba_api.stats.endpoints import leaguedashplayerstats
    out = []
    for end in range(first, last + 1):
        for attempt in range(4):
            try:
                d = leaguedashplayerstats.LeagueDashPlayerStats(
                    season=config.nba_season_label(end), per_mode_detailed='PerGame', timeout=60
                ).get_data_frames()[0]
                break
            except Exception as e:
                print(f'  {end}: {type(e).__name__}, retrying'); time.sleep(10 * 2 ** attempt)
        else:
            raise RuntimeError(f'stats.nba.com kept failing for {end}')
        d['season'] = end
        out.append(d[['PLAYER_ID', 'PLAYER_NAME', 'season', 'AGE', 'GP', 'MIN'] + LINE])
        time.sleep(1)
    A = pd.concat(out)
    AGES_CSV.parent.mkdir(exist_ok=True)
    A.to_csv(AGES_CSV, index=False)
    return A


# ---------------------------------------------------------------- value model
def zscore_season(d, min_gp, targets):
    """9-cat per-game z-scores vs the season's top-156 pool; returns (rows, pool params)."""
    d = d[d.GP >= min_gp].copy()

    def params(pool):
        p = {'fg': pool.FGM.sum() / pool.FGA.sum(), 'ft': pool.FTM.sum() / pool.FTA.sum()}
        fg_imp = pool.FGM - p['fg'] * pool.FGA; ft_imp = pool.FTM - p['ft'] * pool.FTA
        for c in CNT: p[c] = (pool[c].mean(), pool[c].std())
        p['FG'] = (fg_imp.mean(), fg_imp.std()); p['FT'] = (ft_imp.mean(), ft_imp.std())
        return p

    def apply(p):
        z = zrows(d, p, targets)
        for c in z: d[c] = z[c]

    p = params(d); apply(p)
    for _ in range(2):                       # iterate: pool = top 156 by z9
        p = params(d.nlargest(POOL, 'z9')); apply(p)
    p['repl9'] = d.z9.sort_values(ascending=False).iloc[POOL:POOL + 24].mean()
    p['replT'] = d.zT.sort_values(ascending=False).iloc[POOL:POOL + 24].mean()
    return d, p


def zrows(x, p, targets):
    """z-scores for a DataFrame or Series of per-game lines."""
    z = {}
    for c in CNT: z['z_' + c] = (x[c] - p[c][0]) / p[c][1] * (-1 if c == 'TOV' else 1)
    z['z_FG'] = (x['FGM'] - p['fg'] * x['FGA'] - p['FG'][0]) / p['FG'][1]
    z['z_FT'] = (x['FTM'] - p['ft'] * x['FTA'] - p['FT'][0]) / p['FT'][1]
    z['z9'] = sum(z['z_' + c] for c in CATS)
    z['zT'] = sum(z['z_' + c] for c in targets)
    return z


# ---------------------------------------------------------------- aging
def aging_curves(A, season):
    """Stat-space delta curve, z-space summary, and expected games next season, by age."""
    full = A[A.season < season]
    nxt = full.copy(); nxt['season'] -= 1
    pairs = full.merge(nxt, on=['PLAYER_ID', 'season'], suffixes=('', '_n'))
    pairs = pairs[(pairs.GP >= 20) & (pairs.GP_n >= 20)]
    w = np.minimum(pairs.GP, pairs.GP_n)
    curve = {}
    for a in range(19, 40):
        m = (pairs.AGE >= a - 1) & (pairs.AGE <= a + 1)     # 3-year smoothing window
        if m.sum() < 8: continue
        curve[a] = {c: np.average(pairs.loc[m, c + '_n'] - pairs.loc[m, c], weights=w[m]) for c in LINE}
    curve = pd.DataFrame(curve).T.sort_index()

    base = full[full.GP >= 20][['PLAYER_ID', 'season', 'AGE']]
    gpn = full[['PLAYER_ID', 'season', 'GP']].copy(); gpn['season'] -= 1
    ret = base[base.season < season - 1].merge(gpn, on=['PLAYER_ID', 'season'], how='left').fillna({'GP': 0})
    ret['band'] = ret.AGE.clip(20, 37)
    games = ret.groupby('band').agg(n=('GP', 'size'), exp_gp=('GP', 'mean'), p_20plus=('GP', lambda x: (x >= 20).mean()))
    games['exp_gp'] = games.exp_gp.rolling(3, center=True, min_periods=1).mean()
    return curve, games


# ---------------------------------------------------------------- the model
class Model:
    def __init__(self, A, season, targets):
        self.A, self.season, self.targets = A, season, targets
        A['AGE'] = A['AGE'].round().astype(int)
        Z, self.PP = [], {}
        for s in sorted(A.season.unique()):
            d, p = zscore_season(A[A.season == s], 5 if s == season else 20, targets)
            Z.append(d); self.PP[s] = p
        self.Z = pd.concat(Z)
        self.curve, self.games = aging_curves(A, season)
        zf = self.Z[self.Z.season < season][['PLAYER_ID', 'season', 'AGE', 'GP', 'z9', 'zT']]
        zn = zf.copy(); zn['season'] -= 1
        zp = zf.merge(zn, on=['PLAYER_ID', 'season'], suffixes=('', '_n'))
        zp = zp[(zp.GP >= 20) & (zp.GP_n >= 20)]
        self.zcurve = zp.assign(d9=zp.z9_n - zp.z9, dT=zp.zT_n - zp.zT).groupby('AGE').agg(
            n=('d9', 'size'), d_z9=('d9', 'mean'), d_zT=('dT', 'mean'))

    def line(self, pid, s):
        r = self.A[(self.A.PLAYER_ID == pid) & (self.A.season == s)]
        return None if r.empty else r.iloc[0]

    def exp_games(self, age):
        return float(self.games.exp_gp.loc[min(max(age, 20), 37)])

    def age_line(self, line, from_age, to_age):
        out = line.copy()
        for a in range(from_age, to_age):
            aa = min(max(a, self.curve.index.min()), self.curve.index.max())
            for c in LINE: out[c] = max(out[c] + self.curve.loc[aa, c], 0)
        out['FGM'] = min(out.FGM, out.FGA); out['FTM'] = min(out.FTM, out.FTA)
        return out

    def talent(self, pid):
        """Recency-weighted per-game line, prior seasons aged to the current age.
        None if the player has no season with 5+ games in the last three."""
        cur = self.line(pid, self.season)
        if cur is not None:
            age = int(cur.AGE)
        else:   # hasn't played this season: age from his latest season
            prior = [self.line(pid, self.season - b) for b in (1, 2)]
            prior = [(b, r) for b, r in zip((1, 2), prior) if r is not None]
            if not prior: return None, None
            age = int(prior[0][1].AGE) + prior[0][0]
        num = pd.Series(0.0, index=LINE); den = 0
        for back, w in RECENCY.items():
            r = self.line(pid, self.season - back)
            if r is None or r.GP < 5: continue
            l = r[LINE].astype(float)
            if back: l = self.age_line(l, int(r.AGE), age)
            num += l * r.GP * w; den += r.GP * w
        return (num / den, age) if den else (None, None)

    def durability(self, pid, age):
        """Own games/82 over the last 3 full seasons (missing = 0), shrunk 50/50 to the age norm.
        Players with no prior seasons get the age norm."""
        gps = [self.line(pid, s).GP if self.line(pid, s) is not None else 0 for s in range(self.season - 3, self.season)]
        norm = self.exp_games(age) / 82
        if not any(gps): return np.nan, norm
        own = np.mean(gps) / 82
        return own, 0.5 * own + 0.5 * norm

    def z(self, line):
        return zrows(line, self.PP[self.season], self.targets)

    def dynasty(self, pid, name, side, years=5):
        talent, age = self.talent(pid)
        _, dur = self.durability(pid, age)
        R9, RT = self.PP[self.season]['repl9'], self.PP[self.season]['replT']
        rows, line = [], talent.copy()
        for k in range(years):
            a = age + k
            if k:
                line = self.age_line(line, a - 1, a)
                g = self.exp_games(a - 1) / 82 * dur / (self.exp_games(age) / 82)
            else:
                g = dur
            g = min(g, 1.0); zz = self.z(line)
            rows.append({'player': name, 'side': side, 'season': self.season + k, 'age': a, 'z9': zz['z9'], 'zT': zz['zT'],
                         'games_frac': g, 'VAR9': max(zz['z9'] - R9, 0) * g, 'VART': max(zz['zT'] - RT, 0) * g})
        return rows


# ---------------------------------------------------------------- rest-of-season league simulation
CAT_IDX = {c: i for i, c in enumerate(LINE)}
SIM_CATS = ['PTS', 'REB', 'AST', 'STL', 'BLK', 'FG3M', 'TOV', 'FG%', 'FT%']


def compare(A, B):
    """Category results of A vs B for arrays [..., LINE]: +1 A wins, -1 B wins, 0 tie. Shape [..., 9]."""
    out = []
    for c in ['PTS', 'REB', 'AST', 'STL', 'BLK', 'FG3M']:
        out.append(np.sign(A[..., CAT_IDX[c]] - B[..., CAT_IDX[c]]))
    out.append(np.sign(B[..., CAT_IDX['TOV']] - A[..., CAT_IDX['TOV']]))       # fewer turnovers wins
    for m_, a_ in (('FGM', 'FGA'), ('FTM', 'FTA')):
        pa = A[..., CAT_IDX[m_]] / np.maximum(A[..., CAT_IDX[a_]], 1)
        pb = B[..., CAT_IDX[m_]] / np.maximum(B[..., CAT_IDX[a_]], 1)
        out.append(np.sign(pa - pb))
    return np.stack(out, -1)


class League:
    """Every team's best legal lineup each day, over the real remaining schedule and playoffs."""

    def __init__(self, model, season, my_team, overrides, seed=7):
        self.m, self.season, self.my_team, self.overrides, self.seed = model, season, my_team, overrides, seed
        sched = read_sql(f"SELECT payload FROM raw.espn_pro_schedule WHERE season={season}").payload[0]
        self.games = {t['abbrev']: {int(sp) for sp in t.get('proGamesByScoringPeriod', {})}
                      for t in sched['settings']['proTeams']}
        lg = read_sql(f"SELECT payload FROM raw.espn_league WHERE season={season}").payload[0]
        ss = lg['settings']['scheduleSettings']
        self.cur_mp, self.n_reg = lg['status']['currentMatchupPeriod'], ss['matchupPeriodCount']
        self.n_playoff_teams = ss['playoffTeamCount']
        n_rounds = math.ceil(math.log2(self.n_playoff_teams))
        final_sp = lg['status']['finalScoringPeriod']
        start = int(read_sql(f"SELECT min(scoring_period) FROM core.player_days WHERE season={season} "
                                f"AND matchup_period={self.cur_mp}").iloc[0, 0])
        # remaining weeks are 7 days, except that the week holding the All-Star break (the last run of
        # 4+ days with no NBA games) absorbs whatever days are left so the season ends on final_sp
        n_weeks = self.n_reg + n_rounds - self.cur_mp + 1
        lengths = [7] * n_weeks
        extra = final_sp - (start + 7 * n_weeks - 1)
        self.note = ''
        if extra > 0:
            empty = [sp for sp in range(start, final_sp + 1) if not any(sp in g for g in self.games.values())]
            runs, run = [], []
            for sp in empty:
                if run and sp != run[-1] + 1: runs.append(run); run = []
                run.append(sp)
            if run: runs.append(run)
            brk = [r for r in runs if len(r) >= 4][-1]
            i = (brk[0] - start) // 7
            lengths[i] += extra
            self.note = (f'week {self.cur_mp + i} is {lengths[i]} days (All-Star break, '
                         f'scoring periods {brk[0]}-{brk[-1]} have no games)')
        self.weeks, sp = [], start
        for L in lengths:
            self.weeks.append(range(sp, sp + L)); sp += L
        self.days = np.arange(start, sp)
        self.week_of_day = np.concatenate([[w] * len(r) for w, r in enumerate(self.weeks)])
        self.week_ids = list(range(self.cur_mp, self.cur_mp + n_weeks))

        tm = read_sql(f"SELECT matchup_period, team_id, opponent_id FROM core.team_matchups "
                         f"WHERE season={season} AND matchup_period >= {self.cur_mp} AND team_id < opponent_id")
        self.schedule = {k: list(zip(g.team_id, g.opponent_id)) for k, g in tm.groupby('matchup_period')}
        t = read_sql(f"SELECT team_id, team_name, wins, losses, ties FROM core.teams WHERE season={season}")
        self.teams = list(t.team_id); self.names = dict(zip(t.team_id, t.team_name.str.strip()))
        self.wins0 = dict(zip(t.team_id, t.wins + 0.5 * t.ties))
        cw = read_sql(f"SELECT team_id, sum(cat_wins) cw FROM core.team_matchups WHERE season={season} "
                         f"AND matchup_period < {self.cur_mp} GROUP BY 1")
        self.catwins0 = dict(zip(cw.team_id, cw.cw))

        tg = read_sql(f"""SELECT g.nba_player_id, count(*) gp, max(t.n) team_g FROM core.player_games g
            JOIN (SELECT team, count(DISTINCT game_id) n FROM core.player_games
                  WHERE season={season} AND season_type='Regular Season' GROUP BY 1) t USING (team)
            WHERE g.season={season} AND g.season_type='Regular Season' GROUP BY 1""")
        self.rate_now = dict(zip(tg.nba_player_id, (tg.gp / tg.team_g).clip(upper=1)))
        self.rosters = read_sql(f"""SELECT r.team_id, r.player_id, r.player_name, r.injury_status,
                p.eligible_positions, p.pro_team, x.nba_player_id
            FROM core.rosters r JOIN core.players p USING (player_id)
            LEFT JOIN ref.player_xwalk x ON x.espn_player_id = r.player_id
            WHERE r.season={season}""")
        # free agents: unrostered players who have played this season (to fill an open roster spot)
        self.fa = read_sql(f"""SELECT p.player_id, p.player_name, p.eligible_positions, p.pro_team,
                x.nba_player_id, 'ACTIVE' AS injury_status
            FROM core.players p JOIN ref.player_xwalk x ON x.espn_player_id = p.player_id
            WHERE p.player_id NOT IN (SELECT player_id FROM core.rosters WHERE season={season})
              AND x.nba_player_id IN (SELECT nba_player_id FROM core.player_games WHERE season={season})""")
        self.skipped = []

    def free_agents(self, form_only, exclude=()):
        """Free agents as simulation inputs, best 9-cat value first."""
        log, self.skipped = self.skipped, []
        ps = [self.player(r, form_only, False) for _, r in self.fa.iterrows() if r.player_name not in exclude]
        self.skipped = log
        return sorted([p for p in ps if p], key=lambda p: -p['val'])

    def player(self, r, form_only, my_targets):
        """Simulation inputs for one rostered player, or None if he can't contribute."""
        if pd.isna(r.nba_player_id):
            self.skipped.append((r.player_name, 'no NBA id')); return None
        pid = int(r.nba_player_id)
        talent, age = self.m.talent(pid)
        if talent is None:
            self.skipped.append((r.player_name, 'no recent NBA games')); return None
        cur = self.m.line(pid, self.season)
        if form_only and cur is not None: talent = cur[LINE].astype(float)
        _, dur = self.m.durability(pid, age)
        if cur is None and r.player_name not in self.overrides:
            # hasn't played this season (all such players are OUT): assume out unless --avail says otherwise
            self.skipped.append((r.player_name, 'no games this season; pass --avail "Name=x" to include')); return None
        p = 0.5 * dur + 0.5 * self.rate_now.get(pid, dur)       # blend in this season's games-played rate
        p *= self.overrides.get(r.player_name, 1.0)
        team = ALIAS.get(r.pro_team, r.pro_team)
        if team not in self.games:
            self.skipped.append((r.player_name, f'pro team {r.pro_team}')); return None
        zz = self.m.z(talent)
        # my team sets lineups for its target categories; everyone else for all 9
        val = zz['zT'] + 0.3 * (zz['z9'] - zz['zT']) if my_targets else zz['z9']
        return {'name': r.player_name, 'pos': set(r.eligible_positions), 'team': team, 'p_play': min(p, 0.97),
                'line': np.clip(talent.values.astype(float), 0, None), 'val': val,
                'val_targets': zz['zT'] + 0.3 * (zz['z9'] - zz['zT'])}

    def inputs(self, form_only):
        """{team_id: {player name: inputs}} for the current rosters."""
        out = {}
        for tid, g in self.rosters.groupby('team_id'):
            ps = [self.player(r, form_only, tid == self.my_team) for _, r in g.iterrows()]
            out[tid] = {p['name']: p for p in ps if p}
        return out

    def sim_team(self, team_id, players, sims):
        """Weekly totals [sims, weeks, LINE] and starts per week by player.
        Random draws are seeded by (team, sim), so an unchanged roster reproduces exactly."""
        plist = list(players.values()); n = len(plist)
        has = np.array([[d in self.games[p['team']] for d in self.days] for p in plist])
        p_play = np.array([p['p_play'] for p in plist])
        elig = np.array([[sl == 'UT' or sl in p['pos'] for sl in SLOTS] for p in plist])
        val = np.array([p['val'] for p in plist]); lines = np.stack([p['line'] for p in plist])
        W = len(self.weeks)
        out = np.zeros((sims, W, len(LINE))); starts = np.zeros(n); cache = {}
        for s in range(sims):
            rng = np.random.default_rng([self.seed, int(team_id), s])
            avail = (rng.random((n, len(self.days))) < p_play[:, None]) & has
            cnt = np.zeros((n, W))
            for d in range(len(self.days)):
                idx = np.flatnonzero(avail[:, d])
                if not len(idx): continue
                key = idx.tobytes()
                st = cache.get(key)
                if st is None:      # best legal lineup for this set of available players
                    M = np.where(elig[idx], -(val[idx, None] + 100), 1e6)
                    ri, ci = linear_sum_assignment(M)
                    st = cache[key] = idx[ri[M[ri, ci] < 1e5]]
                cnt[st, self.week_of_day[d]] += 1
            out[s] = rng.poisson(cnt.T @ lines)   # sum of per-game Poissons = Poisson of the summed means
            starts += cnt.sum(1)
        return out, {p['name']: starts[i] / (sims * W) for i, p in enumerate(plist)}

    def season_results(self, T, sims):
        """T: {team_id: [sims, weeks, LINE]} -> per-team outcome arrays and my category results."""
        teams = self.teams; ti = {t: i for i, t in enumerate(teams)}
        wins = np.tile([self.wins0[t] for t in teams], (sims, 1)).astype(float)
        catw = np.tile([self.catwins0.get(t, 0) for t in teams], (sims, 1)).astype(float)
        mine_cats, mine_week = [], []
        for k in range(self.cur_mp, self.n_reg + 1):
            w = k - self.cur_mp
            for a_, b_ in self.schedule[k]:
                r = compare(T[a_][:, w], T[b_][:, w])
                wa, wb = (r > 0).sum(1), (r < 0).sum(1)
                wins[:, ti[a_]] += (wa > wb) + 0.5 * (wa == wb)
                wins[:, ti[b_]] += (wb > wa) + 0.5 * (wa == wb)
                catw[:, ti[a_]] += wa; catw[:, ti[b_]] += wb
                if self.my_team in (a_, b_):
                    rr = r if a_ == self.my_team else -r
                    mine_cats.append(rr)
                    mine_week.append((wa > wb) + 0.5 * (wa == wb) if a_ == self.my_team else (wb > wa) + 0.5 * (wa == wb))
        # seeding: H2H record, then category wins, then coin flip
        rng = np.random.default_rng(self.seed)
        key = wins * 1e4 + catw + rng.random(wins.shape) * 0.1
        order = np.argsort(-key, axis=1)
        seed_of = np.empty_like(order); seed_of[np.arange(sims)[:, None], order] = np.arange(len(teams))
        champ = np.zeros((sims, len(teams)))
        r1, r2, r3 = (self.n_reg + 1 - self.cur_mp, self.n_reg + 2 - self.cur_mp, self.n_reg + 3 - self.cur_mp)

        def winner(s, hi, lo, w):   # higher seed advances on a tie
            r = compare(T[teams[hi]][s, w], T[teams[lo]][s, w])
            return lo if (r < 0).sum() > (r > 0).sum() else hi
        for s in range(sims):
            sd = order[s]   # team index by seed (0 = 1st seed); 6-team bracket, top 2 get byes
            a45 = winner(s, sd[3], sd[4], r1); a36 = winner(s, sd[2], sd[5], r1)
            f1 = winner(s, sd[0], a45, r2); f2 = winner(s, sd[1], a36, r2)
            hi, lo = (f1, f2) if seed_of[s, f1] < seed_of[s, f2] else (f2, f1)
            champ[s, winner(s, hi, lo, r3)] = 1
        return {'wins': wins, 'seed': seed_of, 'champ': champ,
                'mine_cats': np.stack(mine_cats, 1), 'mine_week': np.stack(mine_week, 1)}

    def summary(self, res, team_id):
        i = self.teams.index(team_id)
        return {'final_wins': res['wins'][:, i].mean(), 'P(playoffs)': (res['seed'][:, i] < self.n_playoff_teams).mean(),
                'P(top-2 bye)': (res['seed'][:, i] < 2).mean(), 'P(title)': res['champ'][:, i].mean()}

    def my_summary(self, res):
        mc = res['mine_cats']   # [sims, my remaining matchups, 9]
        out = {c: ((mc[..., j] > 0) + 0.5 * (mc[..., j] == 0)).mean() for j, c in enumerate(SIM_CATS)}
        out['exp_cats_won'] = ((mc > 0) + 0.5 * (mc == 0)).sum(-1).mean()
        out['P(win week)'] = res['mine_week'].mean()
        out.update(self.summary(res, self.my_team))
        return out


# ---------------------------------------------------------------- main
def parse_alt(s):
    """'A, B > C, D / Drop Name' -> (give, get, drop)."""
    left, _, drop = s.partition('/')
    give, _, get = left.partition('>')
    split = lambda x: [n.strip() for n in x.split(',') if n.strip()]
    return split(give), split(get), drop.strip() or None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--team', type=int, required=True, help='your ESPN team_id')
    ap.add_argument('--give', nargs='+', required=True); ap.add_argument('--get', nargs='+', required=True)
    ap.add_argument('--drop', help='player you would drop to make room (default: best by simulation)')
    ap.add_argument('--targets', nargs='+', default=CATS, help=f'categories you build for, from {CATS}')
    ap.add_argument('--avail', action='append', default=[], help='"Name=0.6": scale a player\'s games-played rate')
    ap.add_argument('--alt', action='append', default=[], help='"Give A, B > Get C / Drop D" extra scenario')
    ap.add_argument('--season', type=int, default=config.current_season())
    ap.add_argument('--sims', type=int, default=300)
    ap.add_argument('--refresh-ages', action='store_true')
    a = ap.parse_args(argv)
    season = a.season

    A = load_player_seasons(season - 8, season, a.refresh_ages)
    m = Model(A, season, a.targets)
    xw = read_sql("SELECT espn_player_id, nba_player_id FROM ref.player_xwalk")
    players = read_sql("SELECT player_id, player_name, eligible_positions, pro_team FROM core.players")
    players = players.merge(xw, left_on='player_id', right_on='espn_player_id')
    byname = players.set_index('player_name')
    missing = [n for n in a.give + a.get if n not in byname.index]
    if missing: sys.exit(f'not found in core.players / crosswalk: {missing}')

    R9, RT = m.PP[season]['repl9'], m.PP[season]['replT']
    print(f'season {season}; targets {a.targets}; replacement level z9 {R9:.2f}, target z {RT:.2f}')

    # 1. player value now
    zs = m.Z[m.Z.season == season].copy()
    zs['rank9'] = zs.z9.rank(ascending=False); zs['rankT'] = zs.zT.rank(ascending=False)
    rows = []
    for side, names in (('GIVE', a.give), ('GET', a.get)):
        for n in names:
            pid = byname.loc[n, 'nba_player_id']; t, age = m.talent(pid); own, dur = m.durability(pid, age)
            # players with < 5 games this season aren't in the current-season table
            hit = zs[zs.PLAYER_ID == pid]
            cur = hit.iloc[0] if len(hit) else pd.Series({'GP': m.line(pid, season).GP if m.line(pid, season) is not None else 0,
                                                         'z9': np.nan, 'rank9': np.nan, 'zT': np.nan, 'rankT': np.nan})
            zt = m.z(t)
            rows.append({'side': side, 'player': n, 'age': age, 'gp': cur.GP, 'z9_now': cur.z9, 'rank9_now': cur.rank9,
                         'zT_now': cur.zT, 'rankT_now': cur.rankT, 'z9_talent': zt['z9'], 'zT_talent': zt['zT'],
                         'gp_rate_3y': own, **{c: zt['z_' + c] for c in CATS}})
    print('\n=== 1. Player value: this season (now) and projected talent (per-game z)')
    print(pd.DataFrame(rows).round(2).to_string(index=False))

    # 2. aging
    print('\n=== 2. Aging curve: mean change in per-game z next season, by age (both seasons GP>=20)')
    print(m.zcurve.loc[19:39].round(2).T.to_string())
    print('\nexpected games next season by age (GP>=20 this season; 0 if out of the league)')
    print(m.games.round(2).T.to_string())

    # 3. dynasty
    dyn = pd.DataFrame([r for side, names in (('GIVE', a.give), ('GET', a.get))
                        for n in names for r in m.dynasty(byname.loc[n, 'nba_player_id'], n, side)])
    ref9 = m.Z[m.Z.season == season - 1].z9.sort_values(ascending=False).values
    refT = m.Z[m.Z.season == season - 1].zT.sort_values(ascending=False).values
    dyn['rank9'] = dyn.z9.apply(lambda z: (ref9 > z).sum() + 1)
    dyn['rankT'] = dyn.zT.apply(lambda z: (refT > z).sum() + 1)
    print(f'\n=== 3. Dynasty: projected rank-equivalent (vs {season - 1} full season) and value above replacement')
    print(dyn.pivot_table(index=['side', 'player'], columns='season', values=['rank9', 'rankT']).to_string())
    print(dyn.pivot_table(index=['side', 'player'], columns='season', values=['VAR9', 'VART']).round(2).to_string())
    for disc in (1.0, 0.85):
        for h in (1, 3, 5):
            sub = dyn[dyn.season < season + h]; df = disc ** (sub.season - season)
            g = sub.assign(v9=sub.VAR9 * df, vT=sub.VART * df).groupby('side')[['v9', 'vT']].sum()
            print(f'  {h}y, discount {disc}: 9-cat GET {g.loc["GET", "v9"]:.2f} vs GIVE {g.loc["GIVE", "v9"]:.2f}'
                  f' | targets GET {g.loc["GET", "vT"]:.2f} vs GIVE {g.loc["GIVE", "vT"]:.2f}')

    # rookie comps for any young player in the deal
    first = m.Z.sort_values('season').groupby('PLAYER_ID').head(1)
    for n in a.give + a.get:
        pid = byname.loc[n, 'nba_player_id']; hit = zs[zs.PLAYER_ID == pid]
        if hit.empty: continue
        cur = hit.iloc[0]
        if cur.AGE > 21 or m.line(pid, season - 1) is not None: continue
        rk = first[(first.AGE <= 21) & first.season.between(season - 7, season - 3) & (first.MIN >= 22)
                   & ((first.z9 - cur.z9).abs() <= 2.5)]
        traj = []
        for _, c in rk.iterrows():
            row = {'player': c.PLAYER_NAME, 'rookie_season': c.season, 'age': c.AGE, 'y1_z9': c.z9}
            for k in (1, 2, 3):
                r = m.Z[(m.Z.PLAYER_ID == c.PLAYER_ID) & (m.Z.season == c.season + k)]
                row[f'y{k + 1}_z9'] = r.z9.iloc[0] if len(r) else np.nan
            traj.append(row)
        traj = pd.DataFrame(traj).sort_values('y1_z9', ascending=False)
        print(f'\nrookie comps for {n} (y1 z9 {cur.z9:.2f}): age<=21 rookies, 22+ mpg, y1 z9 within 2.5')
        print(traj.round(2).to_string(index=False))
        print('  mean:', traj.filter(like='_z9').mean().round(2).to_dict())

    # 4. rest of season: whole-league simulation
    overrides = {k.strip(): float(v) for k, v in (s.rsplit('=', 1) for s in a.avail)}
    L = League(m, season, a.team, overrides)
    owner = dict(zip(L.rosters.player_name, L.rosters.team_id))

    def scenario(give, get):
        partners = {owner.get(n) for n in get}
        if len(partners) != 1 or a.team in partners: sys.exit(f'{get} must all be on one other team')
        return partners.pop()

    scen = {'as is': ([], [], None)}
    scen['offer'] = (a.give, a.get, a.drop)
    for s_ in a.alt:
        g, t, d = parse_alt(s_)
        scen[f"{', '.join(g)} > {', '.join(t)}"] = (g, t, d)
    print(f'\n=== 4. Rest of season: all {len(L.teams)} teams set their best legal lineup every day; '
          f'weeks {L.week_ids[0]}-{L.n_reg} + playoffs {L.n_reg + 1}-{L.week_ids[-1]}; {a.sims} sims')
    if L.note: print('   ', L.note)

    for mode in ('talent', 'form'):
        label = 'projected talent' if mode == 'talent' else f'{season} form only'
        L.skipped = []
        base = L.inputs(form_only=mode == 'form')
        t0 = time.time()
        T0, S0 = {}, {}
        for tid, ps in base.items():
            T0[tid], S0[tid] = L.sim_team(tid, ps, a.sims)
        if mode == 'talent':
            print(f'    base league simulated in {time.time() - t0:.0f}s')
            print('    left out:', '; '.join(f'{n} ({why})' for n, why in L.skipped))
            # calibration: league-average simulated 7-day regular-season week vs actual weeks so far
            reg = [w for w, k in enumerate(L.week_ids) if k <= L.n_reg and len(L.weeks[w]) == 7]
            sim_wk = np.concatenate([T0[t][:, reg].reshape(-1, len(LINE)) for t in T0]).mean(0)
            mc = read_sql(f"SELECT matchup_period, stat_name, value FROM core.matchup_categories "
                             f"WHERE season={season} AND matchup_period < {L.cur_mp}")
            days = read_sql(f"SELECT matchup_period, count(DISTINCT scoring_period) d FROM core.player_days "
                               f"WHERE season={season} AND matchup_period < {L.cur_mp} GROUP BY 1")
            mc = mc.merge(days, on='matchup_period')
            act = mc.assign(v=np.where(mc.stat_name.isin(['FG%', 'FT%']), mc.value, mc.value * 7 / mc.d)) \
                .groupby('stat_name').v.mean()
            sim_vals = dict(zip(LINE, sim_wk))
            cal = pd.DataFrame({'sim': [sim_vals['PTS'], sim_vals['REB'], sim_vals['AST'], sim_vals['STL'], sim_vals['BLK'],
                                        sim_vals['FG3M'], sim_vals['TOV'], sim_vals['FGM'] / sim_vals['FGA'],
                                        sim_vals['FTM'] / sim_vals['FTA']],
                                'actual': [act[c] for c in ['PTS', 'REB', 'AST', 'STL', 'BLK', '3PM', 'TO', 'FG%', 'FT%']]},
                               index=SIM_CATS)
            print('    calibration, league-average team week (sim vs actual weeks so far):')
            print(cal.round(3).T.to_string())

        def build(give, get, drop):
            """Rosters for a scenario: my team and the partner swap players; I drop one if needed."""
            partner = scenario(give, get)
            mine = {k: v for k, v in base[a.team].items() if k not in give and k != drop}
            # a received player's lineup value follows MY targets
            inc = {n: L.player(L.rosters[L.rosters.player_name == n].iloc[0], mode == 'form', True) for n in get}
            mine.update({k: v for k, v in inc.items() if v})
            part = {k: v for k, v in base[partner].items() if k not in get}
            out = {n: L.player(L.rosters[L.rosters.player_name == n].iloc[0], mode == 'form', False) for n in give}
            part.update({k: v for k, v in out.items() if v})
            # a team left with an open roster spot picks up the best free agent(s)
            fas = iter(fa_pool)
            for team, ros in ((partner, part), (a.team, mine)):
                while len(ros) < len(base[team]):
                    p = next(fas); ros[p['name']] = p if team != a.team else {**p, 'val': p['val_targets']}
                    pickups.setdefault(team, []).append(p['name'])
            return partner, mine, part

        fa_pool = L.free_agents(form_only=mode == 'form')
        if mode == 'talent':
            print('    best free agents:', ', '.join(f"{p['name']} ({p['val']:.2f})" for p in fa_pool[:5]))
        rows, partner_rows, starts = {}, {}, {}
        base_res = L.season_results(T0, a.sims)
        for name, (give, get, drop) in scen.items():
            if name == 'as is':
                res, st, partner = base_res, S0[a.team], scenario(a.give, a.get)
            else:
                pickups = {}
                partner, mine, part = build(give, get, drop)
                if drop is None and len(mine) > len(base[a.team]):
                    # pick the drop that keeps the most expected category wins (quick 80-sim runs)
                    n_q = min(80, a.sims); best = None
                    cands = sorted([p for p in mine.values() if p['name'] not in get], key=lambda p: p['val'])[:5]
                    Tq = {t: v[:n_q] for t, v in T0.items()}
                    Tq[partner] = L.sim_team(partner, part, n_q)[0]
                    for c in cands:
                        Tq[a.team] = L.sim_team(a.team, {k: v for k, v in mine.items() if k != c['name']}, n_q)[0]
                        score = L.my_summary(L.season_results(Tq, n_q))['exp_cats_won']
                        if best is None or score > best[1]: best = (c['name'], score)
                    drop = best[0]
                    mine.pop(drop)
                T = dict(T0)
                T[a.team], st = L.sim_team(a.team, mine, a.sims)
                T[partner], _ = L.sim_team(partner, part, a.sims)
                res = L.season_results(T, a.sims)
            key = name + (f' (drop {drop})' if drop else '')
            if name != 'as is' and pickups:
                key += ''.join(f"; {L.names[t]} adds {', '.join(ns)}" for t, ns in pickups.items())
            rows[key] = L.my_summary(res); starts[key] = st
            partner_rows[key] = {f'{L.names[partner]}: ' + k: v for k, v in L.summary(res, partner).items()}

        print(f'\n  [{label}] {L.names[a.team]}, weeks {L.week_ids[0]}-{L.n_reg} vs the real schedule:')
        R = pd.DataFrame(rows).T
        print(R.round(3).to_string())
        print('  trade partner:'); print(pd.DataFrame(partner_rows).T.round(3).to_string())
        if mode == 'talent':
            S = pd.DataFrame(starts).fillna(0).round(2)
            print('  my starts per week:'); print(S.iloc[:, :2].sort_values(S.columns[0], ascending=False).to_string())
            # league table for context
            lt = pd.DataFrame({L.names[t]: L.summary(base_res, t) for t in L.teams}).T.sort_values('final_wins', ascending=False)
            print('  league outlook as is:'); print(lt.round(2).to_string())


if __name__ == '__main__':
    main()
