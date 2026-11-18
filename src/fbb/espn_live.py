"""Live reads from ESPN's fantasy API, for reports that need today's state
(lineups, the week's category totals, injury tags, free agents) rather than the
last `fbb.refresh` load. Uses `requests` only, so it runs on the slim analysis
install; nothing is written to the database.

    from fbb.espn_live import LiveLeague
    lg = LiveLeague(league_id=123456, season=2027)
    league = lg.league()            # teams, rosters, settings, status
    day = lg.day(lg.today())        # that day's matchups with cumulative week scores

Public leagues need no cookies. For a private league, ESPN_S2 / ESPN_SWID from
.api.env are sent when set.
"""
import json
import time

import requests

from fbb import config

SEASON_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}"
LEAGUE_URL = SEASON_URL + "/segments/0/leagues/{league_id}"
PAUSE = 0.3     # seconds between requests


class LiveLeague:
    def __init__(self, league_id=None, season=None, timeout=60):
        self.league_id = int(league_id or config.ESPN_LEAGUE_ID or 0)
        if not self.league_id:
            raise RuntimeError("no league id: pass --league or set ESPN_LEAGUE_ID in .api.env")
        self.season = season or config.current_season()
        self.url = LEAGUE_URL.format(season=self.season, league_id=self.league_id)
        self.season_url = SEASON_URL.format(season=self.season)
        self.timeout = timeout
        self.session = requests.Session()
        if config.ESPN_S2 and config.ESPN_SWID:
            self.session.cookies.update({"espn_s2": config.ESPN_S2, "SWID": config.ESPN_SWID})
        self._league = None

    def get(self, params, filt=None, url=None):
        headers = {"x-fantasy-filter": json.dumps(filt)} if filt else {}
        for attempt in range(4):   # retry dropped connections and 5xx with backoff
            last = attempt == 3
            try:
                resp = self.session.get(url or self.url, params=params, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout):
                if last:
                    raise
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code < 500 or last:
                break
            time.sleep(2 ** (attempt + 1))
        if resp.status_code in (401, 403):
            raise RuntimeError(f"ESPN {resp.status_code}: private league? set ESPN_S2 / ESPN_SWID in .api.env")
        resp.raise_for_status()
        time.sleep(PAUSE)
        return resp.json()

    def league(self):
        """Teams with today's rosters (lineup slots, injury tags, stat splits), settings, status."""
        if self._league is None:
            self._league = self.get({"view": ["mTeam", "mRoster", "mSettings", "mStatus"]})
        return self._league

    def today(self):
        """Today's scoring period (fantasy day)."""
        return self.league()["scoringPeriodId"]

    def day(self, scoring_period):
        """The matchups a fantasy day belongs to. Asked for one day, ESPN attaches
        rosterForCurrentScoringPeriod only to that day's matchups (same trick as
        fbb.ingest), and each carries cumulativeScore: the week's totals so far."""
        data = self.get({"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": scoring_period})
        return [m for m in data.get("schedule", [])
                if "rosterForCurrentScoringPeriod" in m.get("home", {})
                or "rosterForCurrentScoringPeriod" in m.get("away", {})]

    def matchup_period_of(self, scoring_period):
        day = self.day(scoring_period)
        return day[0]["matchupPeriodId"] if day else None

    def pro_schedule(self):
        """NBA teams with proGamesByScoringPeriod: {scoring period: [games]}."""
        return self.get({"view": "proTeamSchedules_wl"}, url=self.season_url)["settings"]["proTeams"]

    def free_agents(self, scoring_period, limit=300):
        """Unrostered players (free agents and players on waivers), most-owned first,
        with stat splits and injury tags."""
        filt = {"players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }}
        data = self.get({"view": "kona_player_info", "scoringPeriodId": scoring_period}, filt=filt)
        return data.get("players", [])
