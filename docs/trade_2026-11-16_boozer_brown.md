# Trade evaluation: Boozer + J. Brown for T. Young + Harden + D. Brooks

**Date:** 2026-11-16 (2027 season, start of week 5). **Team:** Ligma Sugma (team 4), 2-2.
**Offer from:** TWIN FADE AWAY (team 1).
**My build:** win PTS, AST, 3PM, STL, FT% (punt REB, BLK, FG%, TO).
**Decision: decline.**

Reproduce (about 1.5 minutes; output saved to `data/trade_eval_2026-11-16.txt`):

```bash
python analysis/trade_eval.py --team 4 \
    --give "Cameron Boozer" "Jaylen Brown" \
    --get "Trae Young" "James Harden" "Dillon Brooks" \
    --targets PTS AST STL FG3M FT --avail "Stephen Curry=0.6" \
    --alt "Jaylen Brown > Trae Young" \
    --alt "Jaylen Brown > Trae Young, James Harden / Klay Thompson" \
    --alt "Cameron Boozer > Trae Young" --sims 1000
```

Data as of the 2026-11-16 load: rosters from today, NBA game logs through 2026-11-15.

## Summary

The offer swaps a 19-year-old for a package of older players, so its case would have to be a
win-now upgrade. The full-league simulation doesn't show a reliable one. My playoff odds go
from 24.3% to 27.5% with projected talent, and fall from 41.6% to 30.8% on this season's form.
My own weekly results barely change (4.42 to 4.43 expected category wins). Most of the gain
in the talent projection comes from weakening the trade partner, who is competing with me for
a playoff spot, not from my roster getting better.

Over five years the package is worth less in 9-category value (21.0 vs 24.5). It leads only
on the five target categories, mostly because of Trae Young. That lead doesn't show up as
more weekly wins, because the roster already has more guards than lineup spots.

## 1. Player value (per-game z-scores)

The z-scores compare each player with the top 156 players this season (12 teams × 13).
FG% and FT% are scored by impact: shooting percentage relative to the pool, weighted by
attempts. "Target" means the sum over PTS, AST, STL, 3PM and FT%. Projected talent blends
this season with the previous two seasons. Prior seasons are adjusted to the player's
current age, and recent games count more.

| Player | Age | 9-cat rank now | Target rank now | Talent z (9-cat) | Talent z (target) |
|---|---|---|---|---|---|
| Cameron Boozer (give) | 19 | 87 | 69 | -0.69 | 0.48 |
| Jaylen Brown (give) | 30 | 68 | 79 | 0.35 | 1.80 |
| Trae Young (get) | 28 | 88 | **16** | 0.51 | **6.04** |
| James Harden (get) | 37 | 57 | 24 | 1.81 | 5.48 |
| Dillon Brooks (get) | 30 | 187 | 129 | -2.63 | -0.56 |

Replacement level, meaning the average of players ranked 157-180, is -3.51 (9-cat) and -3.05 (target).
Brooks is close to replacement level.

## 2. Aging curve (built from this league's data)

The curve uses the delta method on 1,700+ consecutive player-season pairs from 2019 to 2026.
Both seasons in a pair have at least 20 games played.

| Age | 19 | 20 | 21 | 22 | 23-26 | 27-30 | 31-35 | 36 | 37 |
|---|---|---|---|---|---|---|---|---|---|
| Change in 9-cat z next season | +1.14 | +0.95 | +0.88 | +0.67 | about 0 | -0.4 to -0.6 | -0.8 to -1.1 | -1.04 | -1.32 |
| Share playing 20+ games next season | | 0.89 | 0.87 | 0.87 | 0.78-0.81 | 0.78-0.80 | 0.73-0.78 | 0.60 | 0.58 |

The curve shows how players who stay in the league change. Players who leave the league
are counted separately: they count as zero games in the expected-games estimate.

## 3. Dynasty projection

Each player's talent is moved forward along the aging curve. Value is value above replacement
multiplied by the expected share of games played. The rank equivalents are against the
2026 season.

| Rank (9-cat / target) | 2027 | 2028 | 2029 | 2030 | 2031 |
|---|---|---|---|---|---|
| Boozer | 84 / 69 | 48 / 53 | 31 / 38 | 21 / 29 | **15 / 21** |
| Brown | 53 / 47 | 68 / 53 | 87 / 59 | 110 / 71 | 138 / 89 |
| Trae Young | 50 / **9** | 54 / 9 | 63 / 11 | 77 / 13 | 91 / 14 |
| Harden | 28 / 13 | 44 / 15 | 69 / 20 | 101 / 29 | 138 / 36 |
| Brooks | 154 / 99 | 177 / 110 | 202 / 120 | 242 / 150 | 271 / 178 |

| Total value above replacement | 1 yr | 3 yr | 5 yr | 5 yr, 0.85 discount |
|---|---|---|---|---|
| 9-cat: get / give | 6.39 / 4.45 | 15.69 / 14.48 | 20.97 / **24.49** | 16.82 / **18.02** |
| Target: get / give | **12.53** / 5.58 | **34.99** / 17.73 | **52.73** / 29.67 | **40.40** / 21.92 |

How to read these totals:

- **Harden's later years are too optimistic.** The data has only 13, 9 and 5 players at ages
  37, 38 and 39, and expected games stop dropping after age 37. If he retires after 2028, the
  package's 5-year 9-cat total falls to about 16.7, and its target total to about 40.
- **Most of the package's target-category value is Trae Young.** He holds a top-15 target
  rank through 2031.
- **Five years cuts off at Boozer's age 23.** He is still improving then, and his prime comes
  after the window ends.
- **Boozer is ahead of comparable rookies.** Among rookies aged 21 or younger from 2020 to
  2024 who played 22+ minutes a game, the average 9-cat z went from -1.75 in year 1 to +0.19
  in year 4. Boozer's year 1 is -0.69, close to Scottie Barnes, Evan Mobley and Cade
  Cunningham. The range of outcomes is wide: Haliburton and LaMelo at the top, KJ Martin at
  the bottom.

## 4. Rest-of-season simulation (whole league, weeks 5-17 + playoffs)

Each run simulates 1,000 seasons for all 12 teams over the real NBA schedule:

- **Lineups.** Every day, every team starts its best legal lineup (PG SG SF PF C UT UT) from
  the players who play that day. My team chooses mainly on the five target categories. Other
  teams choose on all 9.
- **Availability and stats.** Each player's availability is drawn at random from his
  games-played rate. Weekly stats are drawn around each player's projected per-game line.
- **Matchups and playoffs.** Weekly totals decide the real remaining H2H schedule (weeks 5-17)
  and then a 6-team bracket in weeks 18-20. The top 2 seeds get byes, and the higher seed
  advances on a tie.
- **Seeding.** Teams are seeded by record, then total category wins.
- **The trade partner changes too.** In each scenario TWIN FADE AWAY gets my players. A team
  left with an open roster spot adds the best free agent.
- **Fair comparisons.** A team the trade doesn't touch has the same simulated weeks in every
  scenario, so the differences between scenarios come from the trade, not from random noise.

| Projected talent | Exp. cat wins / week | P(win week) | Final wins | P(playoffs) | P(title) |
|---|---|---|---|---|---|
| As is | 4.42 | 48.2% | 8.3 | 24.3% | 0.7% |
| **Offer** (drop Jenkins) | 4.43 | 48.7% | 8.3 | **27.5%** | 1.9% |
| Brown for Trae | 4.33 | 45.3% | 7.9 | 17.7% | 0.5% |
| Brown for Trae + Harden (drop Klay) | 4.36 | 45.7% | 7.9 | 19.6% | 0.7% |
| Boozer for Trae | 4.48 | 50.3% | 8.5 | 30.9% | 1.6% |

| This season's form only | Exp. cat wins / week | P(win week) | Final wins | P(playoffs) | P(title) |
|---|---|---|---|---|---|
| As is | 4.64 | 53.2% | 8.9 | 41.6% | 2.5% |
| **Offer** (drop Tre Jones) | 4.51 | 49.5% | 8.4 | **30.8%** | 2.0% |
| Brown for Trae | 4.54 | 50.3% | 8.5 | 31.3% | 0.8% |
| Brown for Trae + Harden (drop Klay) | 4.48 | 48.4% | 8.3 | 25.1% | 0.5% |
| Boozer for Trae | 4.64 | 54.2% | 9.0 | 43.6% | 2.5% |

Trade partner (TWIN FADE AWAY) playoff odds: with projected talent, 72.9% as is and 45.7%
after the offer. On this season's form, 17.1% and 15.5%.

Category win probability against my actual opponents, projected talent:

| Category | PTS | AST | 3PM | STL | FT% | REB | BLK | TO | FG% |
|---|---|---|---|---|---|---|---|---|---|
| As is | 71.3% | 76.4% | 84.2% | 47.8% | 54.4% | 17.3% | 14.1% | 39.8% | 36.6% |
| Offer | 74.7% | 84.8% | 90.0% | 49.1% | 57.5% | 9.8% | 9.8% | 33.9% | 33.4% |

Where I stand as is (projected talent): 9th in projected final wins (8.3), with a 24% chance
of making the playoffs. Ant's Unwanted Children (11.7 wins, 42% title odds), Screwston Sweats
and Team Youngbois are the favorites.

What drives the result:

- **My own weekly results barely move.** Expected category wins per week go from 4.42 to
  4.43 with projected talent, and drop from 4.64 to 4.51 on form. The offer adds AST, 3PM and
  FT%, which I already win most weeks. STL, the target category I lose most (48%), stays
  about the same. I give up the REB, BLK and TO I was still winning sometimes.
- **The talent projection's playoff gain mostly comes from hurting a rival.** With projected
  talent TWIN FADE AWAY is a playoff team (73%), and the trade cuts them to 46%. That gain
  depends on how good Harden and Trae really are now. On form, TWIN FADE AWAY is already out
  of the race (17%), so the trade doesn't help me that way, and my own odds drop 11 points.
- **There are more guards than lineup spots.** Trae, Harden and Brooks each get about 2.6
  starts a week. Those starts come away from Robinson, Quickley, Nembhard, VanVleet,
  Tre Jones and Ajay Mitchell, who already produce target stats.
- **Brown's scoring is hard to replace.** Every Brown-for-guards version lowers PTS and the
  playoff odds in both projections.
- **The two projections disagree mostly about Harden at 37.** The talent projection blends
  in his 2026 season (23.6 points, 8.0 assists). On form he's at 16.9 and 5.8.

**Calibration.** A simulated league-average team week has 546 PTS, 187 REB, 127 AST and
59 3PM. Actual weeks 1-4 averaged 598, 208, 137 and 64. The simulation runs about 9% low for
every team because it doesn't model streaming or pickups. Since the bias applies to every
team, head-to-head results are much less affected than the raw totals.

## Assumptions and limits

- The simulation assumes Stephen Curry (OUT as of 11/16) plays 60% of his normal games. That's an
  assumption, not data, and it is the same in every scenario.
- The 10 rostered players with no games this season, all listed OUT (Ingram, Butler, Sharpe,
  Lively and others), are treated as out for the season. Use `--avail "Name=x"` to bring one back.
- Regular-season weeks are 7 days. Playoff round 1 (week 18) is 14 days because it includes
  the All-Star break, which makes the season end on ESPN's last scoring period (146).
- ESPN lists some NBA Cup knockout games in December as TBD, so each team is missing about
  2 games. That affects every team equally.
- No free-agent streaming during the season, and no future trades. Divisions are ignored in
  seeding, and the H2H tiebreaker is approximated by total category wins.
- With 1,000 seasons, P(playoffs) has a standard error of about ±1.4 points for one scenario.
  Differences between scenarios are more precise than that, because teams the trade doesn't
  touch have the same simulated weeks in every scenario.
- Lineup choices put most of the weight on the five target categories. The other four count at 0.3.

## What to do instead

Keep Boozer. If Brown is traded, the data points to a forward or wing who gets steals, not
another guard.
