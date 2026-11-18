"""analysis/matchup_plan.py: lineup, simulation and category logic on synthetic data. No database."""
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from fbb import config

sys.path.insert(0, str(config.ROOT / 'analysis'))
import matchup_plan as mp  # noqa: E402

SLOTS = ['PG', 'SG', 'SF', 'PF', 'C', 'UT', 'UT']
NORM = pd.DataFrame([  # league weekly means / spreads, roughly this league's
    {'stat_name': k, 'mean': a, 'sd': b} for k, (a, b) in {
        'PTS': (605, 117), 'REB': (209, 42), 'AST': (140, 34), 'STL': (38, 9.3), 'BLK': (23, 7.9),
        '3PM': (63, 17), 'TO': (72, 16), 'FG%': (0.475, 0.029), 'FT%': (0.798, 0.044),
        'FGM': (218, 43), 'FGA': (458, 83), 'FTM': (106, 27), 'FTA': (132, 33)}.items()])


def line(pts=15, reb=5, ast=3, stl=1, blk=0.5, tpm=1.5, to=1.5, fgm=5.5, fga=12, ftm=2, fta=2.5):
    return np.array([pts, reb, ast, stl, blk, tpm, to, fgm, fga, ftm, fta], float)


def player(pid, name, pos, has, ln=None, p=0.95, slot='BE', fteam=4):
    return {'player_id': pid, 'name': name, 'pos': set(pos), 'has': np.array(has, bool), 'line': line() if ln is None else ln,
            'p': p, 'slot': slot, 'fteam': fteam, 'status': 'ACTIVE', 'roster_status': 'ONTEAM', 'pro_team': 'X',
            'pct_owned': 1.0, 'mpg': 25.0}


# ---------------------------------------------------------------- lineups
def test_best_lineup_respects_positions():
    elig = np.array([[s == 'UT' or s in pos for s in SLOTS] for pos in (['C'], ['C'], ['C'], ['PG'])])
    ri, ci = mp.best_lineup(np.array([3.0, 2.0, 1.0, 0.5]), elig, False)
    assert sorted(ri) == [0, 1, 2, 3]                           # one C at C, two at UT, the PG at PG
    assert {SLOTS[c] for c in ci} == {'C', 'UT', 'PG'}


def test_best_lineup_bench_negative_leaves_slots_open():
    elig = np.ones((3, len(SLOTS)), bool)
    ri, _ = mp.best_lineup(np.array([1.0, -0.5, 0.2]), elig, True)
    assert sorted(ri) == [0, 2]
    ri, _ = mp.best_lineup(np.array([1.0, -0.5, 0.2]), elig, False)
    assert sorted(ri) == [0, 1, 2]                              # default: anyone who can play starts


def test_best_lineup_too_many_players_keeps_best():
    elig = np.array([[s in ('PG', 'UT') for s in SLOTS]] * 4)  # four PGs, three PG-legal slots
    ri, _ = mp.best_lineup(np.array([1.0, 4.0, 2.0, 3.0]), elig, False)
    assert sorted(ri) == [1, 2, 3]


# ---------------------------------------------------------------- categories
def test_compare_turnovers_and_percentages():
    A = np.array([line(pts=100, to=10, fgm=40, fga=100, ftm=8, fta=10)])
    B = np.array([line(pts=90, to=12, fgm=45, fga=90, ftm=8, fta=10)])
    res, mar = mp.compare(A, B)
    r = dict(zip(mp.CATS, res[0]))
    assert r['PTS'] == 1 and r['TO'] == 1                       # fewer turnovers wins
    assert r['FG%'] == -1 and r['FT%'] == 0
    assert mar[0, mp.CATS.index('TO')] == 2
    assert mar[0, mp.CATS.index('FG%')] == pytest.approx(0.4 - 0.5)


def test_summarize_counts_ties_as_half():
    res = np.array([[1, 1, 1, 1, 1, -1, -1, -1, -1], [1, 1, 1, 1, 0, -1, -1, -1, -1]])
    s = mp.summarize(res)
    assert s['P(win)'] == 0.5 and s['P(tie)'] == 0.5 and s['exp_cats'] == pytest.approx((5 + 4.5) / 2)


def test_leverage_ignores_decided_categories():
    rng = np.random.default_rng(0)
    n = 4000
    mar = np.column_stack([rng.normal(200, 10, n)] * 4 + [rng.normal(-200, 10, n)] * 4 + [rng.normal(0, 5, n)])
    res = np.sign(mar)
    lev = mp.leverage(res, mar, floors=np.full(9, 0.5))
    assert lev[8] > 100 * lev[:8].max()                         # 4-4 elsewhere: the coin-flip cat decides it


# ---------------------------------------------------------------- projections
def test_project_blends_seasons_and_caps_makes():
    r = {f'cur_{c}': 10.0 for c in mp.NBA_COLS} | {f'prev_{c}': 20.0 for c in mp.NBA_COLS}
    r |= {'cur_wsum': 6.0, 'prev_gp': 60}
    out = mp.project(r)
    assert out[mp.I['PTS']] == pytest.approx(15.0)              # 6 games of this season vs 6-game prior
    r_none = {f'cur_{c}': np.nan for c in mp.NBA_COLS} | {f'prev_{c}': np.nan for c in mp.NBA_COLS}
    assert mp.project(r_none | {'cur_wsum': np.nan}) is None
    bad = {f'cur_{c}': 1.0 for c in mp.NBA_COLS} | {'cur_fgm': 5.0, 'cur_fga': 4.0, 'cur_wsum': 3.0}
    bad |= {f'prev_{c}': np.nan for c in mp.NBA_COLS}
    out = mp.project(bad)
    assert out[mp.I['FGM']] <= out[mp.I['FGA']]


def test_play_prob():
    base = {'player_name': 'A', 'injury_status': 'ACTIVE', 'lineup_slot': 'BE', 'team_last10': 10, 'gp_last10': 10, 'cur_gp': 10}
    assert mp.play_prob(base, {}) == pytest.approx(0.95)
    assert mp.play_prob(base | {'injury_status': 'OUT'}, {}) == 0
    assert mp.play_prob(base | {'lineup_slot': 'IR', 'injury_status': 'ACTIVE'}, {}) == 0
    assert mp.play_prob(base | {'gp_last10': 3}, {}) < 0.5      # DNPs lately
    assert mp.play_prob(base | {'injury_status': 'OUT'}, {'A': 0.4}) == 0.4


# ---------------------------------------------------------------- simulation
def test_simulate_starts_fit_slots_and_add_actuals():
    D = 3
    ps = [player(i, f'p{i}', ['PG'], [1, 1, 0]) for i in range(10)]
    team = mp.Team(ps, SLOTS, D)
    val = np.tile(np.arange(10, dtype=float)[:, None], (1, D))
    actual = line() * 0 + 7
    tot, starts = mp.simulate(team, val, np.zeros(D, bool), 300, 1, actual)
    assert starts.sum(1).max() <= 2 * 3                         # PG + 2 UT, two game days
    assert (tot >= 7).all()
    assert starts[:, 9].mean() > starts[:, 0].mean()            # higher value starts more
    tot2, _ = mp.simulate(team, val, np.zeros(D, bool), 300, 1, actual)
    assert np.array_equal(tot, tot2)                            # reproducible


def fake_week(me, opp, actual_me, actual_opp, D):
    W = SimpleNamespace(
        a=SimpleNamespace(team=4), opp=6, slots=SLOTS, roster_spots=16, today_sp=1, first_add=1,
        sim_sps=list(range(1, D + 1)), stale=[], week_sps=list(range(1, D + 1)),
        actual={4: actual_me, 6: actual_opp}, floors=np.array([0.5] * 7 + [0.0006, 0.0009]))
    W.w_gen, W.ctx_gen = mp.generic_weights(NORM)
    W.pool = {p['name']: p for p in me + opp}
    W.roster = lambda t: [dict(p) for p in W.pool.values() if p['fteam'] == t]
    W.label = lambda sp: f'day {sp}'
    return W


def test_tuning_benches_a_bricklayer_when_only_fg_pct_is_open():
    """Eight categories are decided 4-4; FG% is close and decides the week. The tuned plan should sit
    a high-volume poor shooter, whose points no longer matter, and win FG% more often."""
    D = 2
    good = line(pts=10, reb=3, ast=2, stl=0.5, blk=0.3, tpm=1, to=1, fgm=4.5, fga=8, ftm=1, fta=1.2)
    brick = line(pts=22, reb=4, ast=3, stl=1, blk=0.3, tpm=3, to=2, fgm=7, fga=21, ftm=4, fta=5)
    me = [player(i, f'g{i}', ['PG', 'SG', 'SF', 'PF', 'C'], [1, 1], good) for i in range(6)]
    me.append(player(99, 'Brick', ['SG'], [1, 1], brick))
    opp = [player(100 + i, f'o{i}', ['PG', 'SG', 'SF', 'PF', 'C'], [1, 1], good, fteam=6) for i in range(7)]
    # me: far ahead in PTS REB AST 3PM, far behind in STL BLK TO FT%; FG% about even
    act_me = np.array([700, 220, 150, 15, 5, 80, 90, 300, 625, 60, 100], float)
    act_opp = np.array([400, 120, 80, 45, 40, 30, 40, 300, 630, 85, 100], float)
    W = fake_week(me, opp, act_me, act_opp, D)
    P = mp.Planner(W, 1500, 3).tune()
    brick_i = P.me.names.index('Brick')
    assert P.lev is not None
    assert P.tuned['V'][brick_i].max() <= 0                      # tuned value says sit him
    assert P.base['starts'][:, brick_i].mean() > 1.5             # 9-cat default starts him
    assert P.tuned['starts'][:, brick_i].mean() < 0.1
    fg = mp.CATS.index('FG%')
    assert P.tuned['cats'][fg] > P.base['cats'][fg] + 0.05
    assert P.tuned['P(win)'] >= P.base['P(win)']


def test_waiver_scan_fills_an_empty_day():
    """My team has nobody playing on day 2; a free agent who plays then should be the add."""
    D = 2
    me = [player(i, f'm{i}', ['PG', 'SG', 'SF', 'PF', 'C'], [1, 0]) for i in range(14)]
    me += [player(50, 'Benchwarmer', ['C'], [0, 0], line(pts=2, reb=1, ast=0, fgm=1, fga=3)),
           player(51, 'Bench2', ['C'], [0, 0], line(pts=3, reb=1, ast=0, fgm=1, fga=3))]
    opp = [player(100 + i, f'o{i}', ['PG', 'SG', 'SF', 'PF', 'C'], [1, 0], fteam=6) for i in range(9)]  # an even fight on day 1
    fa = [player(200, 'Streamer', ['SF'], [0, 1], line(pts=14, reb=6), fteam=None),
          player(201, 'Idle FA', ['SF'], [0, 0], line(pts=20, reb=8), fteam=None),
          player(202, 'Day-one FA', ['SF'], [1, 0], line(pts=8, reb=3), fteam=None)]
    for f in fa:
        f['roster_status'] = 'FREEAGENT'
    zero = np.zeros(11)
    W = fake_week(me, opp + fa, zero, zero, D)
    P = mp.Planner(W, 800, 5).tune()
    adds, log, _ = P.scan(max_adds=1, drop_pool=2, keep=set(), force_drop=set(), n_full=5)
    assert adds and adds[0]['add'] == 'Streamer'
    assert adds[0]['drop'] in ('Benchwarmer', 'Bench2')        # the players with no games go
    assert adds[0]['dP(win)'] > 0
    assert 'Idle FA' not in {r['add'] for r in log[0][2]}       # no games left: never listed
