# Fantasy basketball league database

ESPN fantasy basketball league data → Postgres (Docker) → SQL tables for notebooks and analysis.
Data comes from ESPN's fantasy API through [espn_api](https://github.com/cwendt94/espn-api).
Layout follows `cfb-project`: raw JSON tables, numbered `sql/build` scripts that rebuild `core.*`, and a `src/fbb` package.

DISCLAIMER: This whole repo was made using AI.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set a password
cp .api.env.example .api.env    # league id, and cookies for a private league
docker compose up -d            # Postgres 16 on localhost:5433
```

**Private leagues** need two cookies from a logged-in browser session on fantasy.espn.com
(dev tools → Application → Cookies): `espn_s2` and `SWID` (keep the `{}` braces). Put them in `.api.env`.
They expire every few months; refresh them if loads start returning 401.

## Loading data

```bash
python -m fbb.refresh --history     # first time: every season the league has existed
python -m fbb.refresh               # daily: current season (new days + last 3 refetched)
python -m fbb.refresh --skip-load   # rebuild tables only
```

Seasons are named by the year they end (2026-27 = **2027**). Full detail (daily box scores,
transactions, player pool) is available from 2019; earlier seasons have league, schedule and draft only.
A full season is ~350 requests (one per day for box scores, one per day for transactions).

## Tables

| Table | Grain |
|---|---|
| `core.seasons` | season: league name, scoring type, team count, playoff settings |
| `core.scoring_items` | season × stat: categories or point values |
| `core.members` | ESPN user (manager) |
| `core.teams` | season × fantasy team: record, PF/PA, seed, final rank, moves, FAAB |
| `core.team_owners` | season × team × manager |
| `core.rosters` | roster as of the latest load |
| `core.matchups` / `core.team_matchups` | matchup / team × matchup (W/L, points, cats) |
| `core.matchup_categories` | team × matchup × category (category leagues) |
| `core.team_days` | team × day fantasy points |
| `core.player_days` | **team × player × day**: lineup slot, starter flag, fantasy points, stat line |
| `core.players` / `core.player_seasons` | NBA player / player × season (owner, % owned, ADP, ranks) |
| `core.player_stat_splits` | player × season × split (total, last 7/15/30, projected) |
| `core.draft_picks` | pick: round, team, player, auction bid, keeper |
| `core.transactions` / `core.transaction_items` | adds, drops, waivers (with FAAB bids), trades, lineup moves |

### NBA game logs

Every player's game log for every NBA game comes from stats.nba.com (`nba_api`), one request per season per
game type. Backfill with `python -m fbb.ingest_nba 2022 2027`; `fbb.refresh` keeps the current season up to date.

| Table | Grain |
|---|---|
| `core.player_games` | NBA player × game: `season_type` (Regular Season / Play-In / Playoffs), opponent, home/away, minutes, box score, `espn_player_id`, ESPN `scoring_period` |
| `ref.player_xwalk` | NBA player id ↔ ESPN player id, with `match_method` (manual / name / name_team) |
| `ref.player_xwalk_manual` | your hand fixes, kept across rebuilds |
| `core.xwalk_unmatched` | NBA players with no ESPN match, most minutes first |

To fix an unmatched player: `INSERT INTO ref.player_xwalk_manual VALUES (<nba id>, <espn id>, 'note');`
then `python -m fbb.refresh --skip-load`.

Stat lines are `jsonb` keyed by stat name (`stats->>'PTS'`); `ref.*` holds the ESPN id lookups.

```python
from fbb.db import read_query
read_query("standings.sql", season=2027)
```

## Analysis

### Trade evaluator: `analysis/trade_eval.py`

Compares a trade offer with keeping the current roster, using two horizons: the rest of this season
and the next five seasons.

```bash
python analysis/trade_eval.py --team 4 \
    --give "Cameron Boozer" "Jaylen Brown" \
    --get "Trae Young" "James Harden" "Dillon Brooks" \
    --targets PTS AST STL FG3M FT \
    --avail "Stephen Curry=0.6" \
    --alt "Jaylen Brown > Trae Young"
```

`--targets` are the categories you build for (the rest are punted). `--avail` scales a player's
games-played rate (for example, for an injury). `--alt` adds another scenario, written
`"Give A, B > Get C / Drop D"`. Run `--help` for the rest.

How it values players:

1. **Value:** per-game z-scores in each of the 9 categories, measured against the season's top 156
   players (12 teams × 13 roster spots). FG% and FT% are scored by impact: makes above the pool's
   rate on the player's attempts. Turnovers count against. `z9` sums all nine categories. `zT` sums
   only the target categories, so `z9 − zT` is what a player gives you in categories you punt.
2. **Talent:** this season plus the previous two, weighted 1 / 0.5 / 0.25 per game. Older seasons
   are first adjusted to the player's current age.
3. **Aging curve:** the average year-over-year change in each stat by age, from players with at
   least 20 games in both seasons, smoothed over 3 ages. Also gives expected games played by age.
4. **Dynasty:** value above replacement (players ranked 157-180) × expected share of games,
   projected 5 seasons ahead.
5. **Rest of season:** a Monte Carlo simulation of the whole league over the real NBA schedule.
   Every day each team starts its best legal lineup. Your team ranks players by
   `0.7·zT + 0.3·z9`, every other team by `z9`. The simulated weeks play out the real H2H schedule
   and the 6-team playoff bracket. Output: expected categories won, P(win week), P(playoffs) and
   P(title). Teams the trade doesn't touch reuse the same random draws, so differences between
   scenarios come from the trade, not from noise.

Player ages come from stats.nba.com and are cached in `data/nba_player_seasons.csv`
(`--refresh-ages` refetches them). A worked example is in
[`docs/trade_2026-11-16_boozer_brown.md`](docs/trade_2026-11-16_boozer_brown.md).

### Notebooks

| Notebook | What it answers |
|---|---|
| `bench_losses.ipynb` | Weeks lost because of benched players who could legally have started |
| `losers.ipynb` | Quick look at the bench-losses query |
| `trade_market.ipynb` | Trades that raise my playoff odds and that the other manager would plausibly accept |
| `siakam_trade.ipynb` | Fact-checks on my roster, and whether to trade Pascal Siakam to fix FT% |
| `position_market.ipynb` | By position: who would crack my top 10 in build value (`zT`), plotted against 9-cat value (`z9`) |
