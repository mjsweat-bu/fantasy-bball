"""Start/sit for one day of an H2H category matchup: which lineup gives the best chance to win the week.

    python analysis/lineup.py --team 4                      # tomorrow
    python analysis/lineup.py --team 4 --date 2026-11-18
    python analysis/lineup.py --team 4 --avail "Jalen Duren=0.5" --sims 20000

Reads live from ESPN, not the database, because a lineup decision depends on today's rosters,
injury tags and matchup score. Needs only .api.env.

Method:
  1. Projection: each player's per-game line blends this season (weight = games played), the last
     15 days (half weight) and ESPN's preseason projection (worth 8 games). Shooting is projected as
     attempts and makes, so FG% and FT% come out of the simulated totals.
  2. Availability: ACTIVE players play 95% of their team's games and DAY_TO_DAY players 60%.
     OUT, suspended and IR-slot players don't play. Change this with --avail "Name=p".
  3. Simulation: each simulated game multiplies the player's line by one random factor, which
     stands in for minutes, foul trouble and blowouts. Counting stats are then Poisson draws, and
     makes are binomial draws from the simulated attempts.
  4. The other days: on every simulated day except the target day, both teams start their best
     legal lineup (PG SG SF PF C UT UT) from the players who actually play, ranked by 9-cat
     z-score. The same goes for the opponent on the target day: managers can react to news.
  5. The decision: every legal set of your players who have a game on the target day is scored
     on the same random draws. Your choice is made before you know who sits, so a starter who
     sits scores 0. The lineup with the highest P(win week) is recommended (exact ties go to more
     expected category wins). The report also lists every lineup within 2 standard errors of it:
     players in all of those are locks, and the rest are toss-ups.
"""
import argparse, itertools, sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from fbb import config
from fbb.ingest import client, matchup_period_for_day
from trade_eval import CATS, LINE, SIM_CATS, compare, zrows, zscore_season

ET = ZoneInfo('America/New_York')
SLOTS = ['PG', 'SG', 'SF', 'PF', 'C', 'UT', 'UT']
SLOT_OF_ID = {0: 'PG', 1: 'SG', 2: 'SF', 3: 'PF', 4: 'C', 11: 'UT'}
IR_SLOT, BENCH_SLOT = 13, 12
STAT_ID = {'PTS': '0', 'REB': '6', 'AST': '3', 'STL': '2', 'BLK': '1', 'FG3M': '17', 'TOV': '11',
           'FGM': '13', 'FGA': '14', 'FTM': '15', 'FTA': '16'}
I = {c: i for i, c in enumerate(LINE)}
P_PLAY = {'ACTIVE': 0.95, 'DAY_TO_DAY': 0.6}     # any other status (OUT, SUSPENSION, ...) = 0
PRIOR_GAMES = 8          # ESPN's preseason projection counts as this many games
SHAPE = 8.0              # per-game gamma factor on the whole line: CV = 1/sqrt(8) ~ 0.35


# ---------------------------------------------------------------- ESPN data
def stat_entry(p, season, source, split):
    for s in p.get('stats', []):
        if s['seasonId'] == season and s['statSourceId'] == source and s['statSplitTypeId'] == split:
            return s
    return None


def per_game(s):
    avg = s.get('averageStats') or {}
    return np.array([float(avg.get(STAT_ID[c], 0.0)) for c in LINE])


def projection(p, season):
    """Blended per-game line and this season's games played, or (None, 0)."""
    cur, l15 = stat_entry(p, season, 0, 0), stat_entry(p, season, 0, 2)
    proj = stat_entry(p, season, 1, 0) or stat_entry(p, season - 1, 0, 0)    # no projection: last season
    gp = float((cur or {}).get('stats', {}).get('42', 0) or 0)
    gp15 = float((l15 or {}).get('stats', {}).get('42', 0) or 0)
    parts = []
    if gp: parts.append((gp, per_game(cur)))
    if gp15: parts.append((0.5 * gp15, per_game(l15)))
    if proj and proj.get('averageStats'): parts.append((PRIOR_GAMES, per_game(proj)))
    if not parts: return None, 0
    w = np.array([x[0] for x in parts])
    line = (w[:, None] * np.stack([x[1] for x in parts])).sum(0) / w.sum()
    line[I['FGM']] = min(line[I['FGM']], line[I['FGA']]); line[I['FTM']] = min(line[I['FTM']], line[I['FTA']])
    return np.clip(line, 0, None), gp


def roster(team, season, overrides):
    rows = []
    for e in team['roster']['entries']:
        p = e['playerPoolEntry']['player']
        line, gp = projection(p, season)
        status = p.get('injuryStatus', 'ACTIVE')
        pp = 0.0 if e['lineupSlotId'] == IR_SLOT else P_PLAY.get(status, 0.0)
        pp = overrides.get(p['fullName'], pp)
        rows.append({'name': p['fullName'], 'pro': p['proTeamId'], 'status': status, 'slot': e['lineupSlotId'],
                     'elig': {SLOT_OF_ID[s] for s in p['eligibleSlots'] if s in SLOT_OF_ID}, 'p_play': pp,
                     'line': line, 'gp': gp})
    return [r for r in rows if r['line'] is not None]


def score_line(team_side):
    """Live matchup totals (games so far, including today's) in LINE order."""
    by = (team_side.get('cumulativeScoreLive') or team_side.get('cumulativeScore') or {}).get('scoreByStat') or {}
    return np.array([float((by.get(STAT_ID[c]) or {}).get('score') or 0) for c in LINE])


def period_days(req, target, mp, today, final):
    """Scoring periods of matchup period mp, from the target day out to both ends."""
    lo = target
    while lo - 1 > today and matchup_period_for_day(req, lo - 1) == mp: lo -= 1
    hi = target
    while hi + 1 <= final and matchup_period_for_day(req, hi + 1) == mp: hi += 1
    return lo, hi


# ---------------------------------------------------------------- simulation
def draw(rng, means):
    """Integer stat lines for per-game means [..., LINE], one gamma factor per game."""
    g = rng.gamma(SHAPE, 1 / SHAPE, means.shape[:-1])[..., None]
    out = rng.poisson(means * g).astype(float)
    for m, a in (('FGM', 'FGA'), ('FTM', 'FTA')):
        pct = np.divide(means[..., I[m]], means[..., I[a]], out=np.zeros(means.shape[:-1]), where=means[..., I[a]] > 0)
        out[..., I[m]] = rng.binomial(out[..., I[a]].astype(int), np.clip(pct, 0, 1))
    out[..., I['FG3M']] = np.minimum(out[..., I['FG3M']], out[..., I['FGM']])
    return out


def legal(players):
    """Slot for each player if they can all start together, else None."""
    if len(players) > len(SLOTS): return None
    if not players: return []
    M = np.array([[0 if s in p['elig'] or s == 'UT' else 1 for s in SLOTS] for p in players])
    r, c = linear_sum_assignment(M)
    if M[r, c].sum(): return None
    out = [None] * len(players)
    for ri, ci in zip(r, c): out[ri] = SLOTS[ci]
    return out


def best_starters(players, val, avail):
    """Indices of the highest-value legal lineup among available players (most starters first)."""
    idx = np.flatnonzero(avail)
    if not len(idx): return idx
    M = np.array([[-(val[i] + 100) if (s == 'UT' or s in players[i]['elig']) else 1e6 for s in SLOTS] for i in idx])
    r, c = linear_sum_assignment(M)
    return idx[r[M[r, c] < 1e5]]


def sim_team(rng, players, val, days, games, sims):
    """Totals [sims, LINE] over days, best lineup each day from who actually plays."""
    tot = np.zeros((sims, len(LINE)))
    pp = np.array([p['p_play'] for p in players]); lines = np.stack([p['line'] for p in players])
    for d in days:
        plays = np.array([d in games.get(p['pro'], {}) for p in players])
        avail = (rng.random((sims, len(players))) < pp) & plays
        mask = np.zeros_like(avail); cache = {}
        for s in range(sims):
            key = avail[s].tobytes()
            if key not in cache: cache[key] = best_starters(players, val, avail[s])
            mask[s, cache[key]] = True
        tot += (draw(rng, np.broadcast_to(lines, (sims,) + lines.shape)) * mask[..., None]).sum(1)
    return tot


def results(me, opp):
    """Category results [..., 9] (+1/0/-1 for me), P(win week) and expected categories won."""
    r = compare(me, opp)
    w, l = (r > 0).sum(-1), (r < 0).sum(-1)
    return r, (w > l) + 0.5 * (w == l), ((r > 0) + 0.5 * (r == 0)).sum(-1)


# ---------------------------------------------------------------- analysis
def analyze(team, target_date=None, overrides=None, sims=10000, seed=7):
    """Everything the report and the notebook need, as a dict."""
    season = config.current_season()
    target_date = target_date or datetime.now(ET).date() + timedelta(days=1)
    if isinstance(target_date, str): target_date = date.fromisoformat(target_date)
    overrides = overrides or {}
    req = client(season)

    # NBA schedule: {pro team id: {scoring period: (start time ET, opponent abbrev, home?)}}
    pro = req.get(params={'view': 'proTeamSchedules_wl'})['settings']['proTeams']
    abbrev = {t['id']: t['abbrev'] for t in pro}
    games, official = {}, {}
    for t in pro:
        for sp, gs in t.get('proGamesByScoringPeriod', {}).items():
            g = gs[0]; home = g['homeProTeamId'] == t['id']
            games.setdefault(t['id'], {})[int(sp)] = (datetime.fromtimestamp(g['date'] / 1000, ET),
                                                      abbrev.get(g['awayProTeamId' if home else 'homeProTeamId'], '?'), home)
            official[g['id']] = (int(sp), g.get('statsOfficial', False))
    sp_dates = {sp: v[0].date() for t in games.values() for sp, v in t.items()}
    target = next((sp for sp, d in sp_dates.items() if d == target_date), None)
    if target is None: sys.exit(f'no NBA games on {target_date}')

    lg = req.league_get(params={'view': ['mStatus', 'mSettings', 'mTeam']})
    today, final = lg['scoringPeriodId'], lg['status']['finalScoringPeriod']
    if target <= today: sys.exit(f'{target_date} is scoring period {target}; today is {today}: pick a future day')
    names = {t['id']: t['name'].strip() for t in lg['teams']}

    d = req.league_get(params={'view': ['mMatchupScore', 'mScoreboard', 'mRoster'], 'scoringPeriodId': target})
    mp = matchup_period_for_day(req, target)
    m = next(x for x in d['schedule'] if x['matchupPeriodId'] == mp and team in (x['home']['teamId'], x['away']['teamId']))
    me_side, opp_side = (m['home'], m['away']) if m['home']['teamId'] == team else (m['away'], m['home'])
    opp_id = opp_side['teamId']
    lo, hi = period_days(req, target, mp, today, final)
    live = mp == lg['status']['currentMatchupPeriod']
    base_me = score_line(me_side) if live else np.zeros(len(LINE))
    base_opp = score_line(opp_side) if live else np.zeros(len(LINE))
    sim_days = list(range(lo, hi + 1))

    teams = {t['id']: t for t in d['teams']}
    mine, theirs = roster(teams[team], season, overrides), roster(teams[opp_id], season, overrides)
    unknown = [n for n in overrides if not any(p['name'] == n for p in mine + theirs)]

    # 9-cat value (z9 vs the top 156 rostered players) ranks players for the other days' lineups
    allp = [p for t in d['teams'] for p in roster(t, season, {})]
    pool = pd.DataFrame([dict(zip(LINE, p['line'])) | {'GP': max(p['gp'], 1)} for p in allp])
    _, params = zscore_season(pool, 0, CATS)
    zline = lambda p: zrows(pd.Series(dict(zip(LINE, p['line']))), params, CATS)
    for p in mine + theirs: p['z'] = zline(p)
    val_me, val_opp = np.array([p['z']['z9'] for p in mine]), np.array([p['z']['z9'] for p in theirs])

    rng = np.random.default_rng(seed); S = sims
    rest_me = sim_team(rng, mine, val_me, [x for x in sim_days if x != target], games, S)
    tot_opp = base_opp + sim_team(rng, theirs, val_opp, sim_days, games, S)

    # target day: my candidates' draws; availability is unknown when I set the lineup
    C_ = [p for p in mine if target in games.get(p['pro'], {}) and p['p_play'] > 0]
    if not C_: sys.exit(f'none of your players has a game on {target_date}')
    plays = rng.random((S, len(C_))) < np.array([p['p_play'] for p in C_])
    tdraw = draw(rng, np.broadcast_to(np.stack([p['line'] for p in C_]), (S, len(C_), len(LINE)))) * plays[..., None]
    base = base_me + rest_me

    def sims_for(c):
        """Per-sim final totals and results for starting the candidate set c."""
        tot = base + tdraw[:, list(c)].sum(1) if len(c) else base
        r, win, cats = results(tot, tot_opp)
        return {'tot': tot, 'r': r, 'win': win, 'cats': cats}

    evals = []
    for k in range(len(C_) + 1):
        for c in itertools.combinations(range(len(C_)), k):
            slots = legal([C_[j] for j in c])
            if slots is None: continue
            x = sims_for(c)
            evals.append({'set': c, 'slots': slots, 'p_win': x['win'].mean(), 'cats': x['cats'].mean(),
                          'per_cat': ((x['r'] > 0) + 0.5 * (x['r'] == 0)).mean(0), 'my_mean': x['tot'].mean(0)})
    best = max(evals, key=lambda e: (e['p_win'], e['cats']))
    find = lambda c: next(e for e in evals if set(e['set']) == set(c))
    current = [j for j, p in enumerate(C_) if p['slot'] not in (BENCH_SLOT, IR_SLOT)]
    greedy = best_starters(C_, np.array([p['z']['z9'] for p in C_]), np.ones(len(C_), bool))
    compared = {'RECOMMENDED': best, 'best 9-cat (z9) lineup': find(list(greedy))}
    if legal([C_[j] for j in current]) is not None: compared['as currently set on ESPN'] = find(current)
    compared['start nobody'] = find([])

    # lineups statistically tied with the best (paired difference within 2 standard errors);
    # players in all of them are locks, the rest are toss-ups
    bw = sims_for(best['set'])['win']
    tied = []
    for e in sorted(evals, key=lambda e: -e['p_win'])[:30]:
        dd = sims_for(e['set'])['win'] - bw
        se = dd.std() / np.sqrt(S)
        if dd.mean() >= -2 * se - 1e-12: tied.append({**e, 'diff': dd.mean(), 'se': se})
    locks = set.intersection(*[set(e['set']) for e in tied])
    tossups = set.union(*[set(e['set']) for e in tied]) - locks

    # each player: best lineup with him vs without him (same random draws)
    marginal = []
    for j, p in enumerate(C_):
        w = max((e for e in evals if j in e['set']), key=lambda e: (e['p_win'], e['cats']))
        wo = max((e for e in evals if j not in e['set']), key=lambda e: (e['p_win'], e['cats']))
        marginal.append({'player': p['name'], 'start': j in best['set'], 'with': w, 'without': wo,
                         'if_out': [C_[k]['name'] for k in wo['set'] if k not in best['set']] if j in best['set'] else []})

    return dict(team=team, opp_id=opp_id, names=names, mp=mp, lo=lo, hi=hi, target=target, target_date=target_date,
                today=today, live=live, sp_dates=sp_dates, games=games, official=official, sim_days=sim_days,
                base_me=base_me, base_opp=base_opp, mine=mine, theirs=theirs, cands=C_, rest_me=rest_me,
                tot_opp=tot_opp, tdraw=tdraw, plays=plays, evals=evals, best=best, compared=compared,
                marginal=marginal, sims_for=sims_for, find=find, unknown=unknown, sims=S,
                tied=tied, locks=locks, tossups=tossups)


def fmt9(v):
    """A LINE-ordered total as the 9 category values (FG% and FT% as ratios)."""
    return [v[I['PTS']], v[I['REB']], v[I['AST']], v[I['STL']], v[I['BLK']], v[I['FG3M']], v[I['TOV']],
            v[I['FGM']] / max(v[I['FGA']], 1e-9), v[I['FTM']] / max(v[I['FTA']], 1e-9)]


def player_table(R):
    """Target-day candidates: game, status, projected per-game line, z9, recommendation."""
    best, rows = R['best'], []
    for j, p in enumerate(R['cands']):
        t, opp, home = R['games'][p['pro']][R['target']]; l = p['line']
        rows.append({'player': p['name'], 'pos': '/'.join(s for s in SLOTS[:5] if s in p['elig']),
                     'game': f"{t:%H:%M} {'vs' if home else '@'} {opp}", 'status': p['status'], 'p_play': p['p_play'],
                     'now': SLOT_OF_ID.get(p['slot'], 'BE'),
                     **dict(zip(['PTS', 'REB', 'AST', 'STL', 'BLK', '3PM', 'TO'], l[[I[c] for c in LINE[:7]]])),
                     'FG%': l[I['FGM']] / max(l[I['FGA']], 1e-9), 'FGA': l[I['FGA']],
                     'FT%': l[I['FTM']] / max(l[I['FTA']], 1e-9), 'FTA': l[I['FTA']], 'z9': p['z']['z9'],
                     'START': best['slots'][best['set'].index(j)] if j in best['set'] else '-'})
    return pd.DataFrame(rows)


def report(R):
    pct = lambda x: f'{100 * x:.1f}%'
    names, S, best = R['names'], R['sims'], R['best']
    for n in R['unknown']: print(f'warning: --avail name not on either roster: {n}')
    print(f"{names[R['team']]} vs {names[R['opp_id']]}: matchup period {R['mp']}, days {R['lo']}-{R['hi']} "
          f"({R['sp_dates'][R['lo']]:%a %b %d} - {R['sp_dates'][R['hi']]:%a %b %d})")
    if R['live']:
        r0 = compare(R['base_me'], R['base_opp'])
        print(f'score so far (live, through today): {(r0 > 0).sum()}-{(r0 < 0).sum()}-{(r0 == 0).sum()}')
        unfinished = [g for g, (sp, ok) in R['official'].items() if sp == R['today'] and not ok]
        if unfinished:
            print(f"  note: {len(unfinished)} of today's NBA games are not final; their remaining minutes are not simulated")
    left = lambda ps: sum(1 for p in ps for x in R['sim_days'] if x in R['games'].get(p['pro'], {}) and p['p_play'] > 0)
    print(f"player-games left (rostered, available): you {left(R['mine'])}, opponent {left(R['theirs'])}; {S} sims\n")

    print(f"=== {R['target_date']:%a %b %d} (scoring period {R['target']}): your players with a game")
    print(player_table(R).round(2).to_string(index=False))
    out_ = [p['name'] for p in R['mine'] if R['target'] in R['games'].get(p['pro'], {}) and p['p_play'] == 0]
    if out_: print('not counted (OUT / IR / suspended):', ', '.join(out_))
    print('no game:', ', '.join(p['name'] for p in R['mine'] if R['target'] not in R['games'].get(p['pro'], {})) or '-')

    print('\n=== Lineups compared (difference vs recommended: paired over the same sims, ± 1 standard error)')
    bw = R['sims_for'](best['set'])['win']
    rows = {}
    for k, e in R['compared'].items():
        diff = R['sims_for'](e['set'])['win'] - bw
        rows[k] = {'starters': len(e['set']), 'P(win week)': pct(e['p_win']),
                   'vs rec.': '' if e is best else f"{100 * diff.mean():+.1f} ± {100 * diff.std() / np.sqrt(S):.1f}",
                   'exp cats won': round(e['cats'], 2), **{c: pct(v) for c, v in zip(SIM_CATS, e['per_cat'])}}
    print(pd.DataFrame(rows).T.to_string())

    print('\n=== Projected final totals (mean), recommended lineup')
    print(pd.DataFrame({'you': fmt9(best['my_mean']), 'opponent': fmt9(R['tot_opp'].mean(0)),
                        'P(you win cat)': best['per_cat']}, index=SIM_CATS).round(3).T.to_string())

    print('\n=== Each player: best lineup with him starting vs without him')
    for m in R['marginal']:
        note = f"   if he sits/is ruled out: start {', '.join(m['if_out'])}" if m['if_out'] else ''
        print(f"  {m['player']:<24} with {pct(m['with']['p_win']):>6} / {m['with']['cats']:.2f} cats   without "
              f"{pct(m['without']['p_win']):>6} / {m['without']['cats']:.2f} cats{note}")

    C_ = R['cands']
    starts = sorted(zip(best['slots'], [C_[j]['name'] for j in best['set']]), key=lambda x: SLOTS.index(x[0]))
    print(f"\nSTART ({len(starts)}): " + ', '.join(f'{s} {n}' for s, n in starts))
    print('BENCH (has a game): ' + (', '.join(p['name'] for j, p in enumerate(C_) if j not in best['set']) or '-'))
    print(f"\n{len(R['tied'])} lineup(s) are within 2 standard errors of this one (see notebooks/lineup.ipynb).")
    print('  locks (in every one): ' + (', '.join(C_[j]['name'] for j in sorted(R['locks'])) or '-'))
    print('  toss-ups for the remaining spots: ' + (', '.join(C_[j]['name'] for j in sorted(R['tossups'])) or '-'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--team', type=int, required=True, help='your ESPN team_id')
    ap.add_argument('--date', help='YYYY-MM-DD, default tomorrow (US Eastern)')
    ap.add_argument('--avail', action='append', default=[], help='"Name=0.5": chance a player plays each game')
    ap.add_argument('--sims', type=int, default=10000)
    ap.add_argument('--seed', type=int, default=7)
    a = ap.parse_args(argv)
    overrides = {k.strip(): float(v) for k, v in (s.rsplit('=', 1) for s in a.avail)}
    report(analyze(a.team, a.date, overrides, a.sims, a.seed))


if __name__ == '__main__':
    main()
