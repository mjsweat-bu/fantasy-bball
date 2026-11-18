"""Project paths and settings, in one place.

Secrets stay in .env (database) and .api.env (ESPN league + cookies) at the project
root; both are git-ignored and loaded here, so scripts and notebooks can run from
any working directory.
"""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".api.env")

# folders
SQL_BUILD_DIR = ROOT / "sql" / "build"          # pipeline scripts, run in numeric order
SQL_ANALYSIS_DIR = ROOT / "sql" / "analysis"    # ad-hoc and report queries

# database (Postgres in Docker; see docker-compose.yml)
DB_CONTAINER = "fbb-postgres"
DB_CONTAINER_SQL_DIR = "/sql"                   # ./sql is mounted here in the container
DB_HOST = os.environ.get("POSTGRES_HOST", "localhost")
DB_PORT = int(os.environ.get("POSTGRES_PORT", "5433"))
DB_NAME = os.environ.get("POSTGRES_DB")
DB_USER = os.environ.get("POSTGRES_USER")
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD")

# hosted database (Neon, etc.): a full connection URL, e.g.
#   postgresql://user:pass@ep-xxx.us-east-2.aws.neon.tech/fantasy-bball?sslmode=require
# When set, it takes priority over the POSTGRES_* settings above.
DATABASE_URL = os.environ.get("DATABASE_URL") or None

# ESPN fantasy league
ESPN_LEAGUE_ID = int(os.environ["ESPN_LEAGUE_ID"]) if os.environ.get("ESPN_LEAGUE_ID") else None
ESPN_S2 = os.environ.get("ESPN_S2") or None
ESPN_SWID = os.environ.get("ESPN_SWID") or None


def current_season(today=None):
    """ESPN names an NBA season by the year it ends: the 2026-27 season is 2027.
    Fantasy leagues open in late summer, so from August on it's next year's season."""
    today = today or date.today()
    return today.year + 1 if today.month >= 8 else today.year


def nba_season_label(season):
    """ESPN-style season (end year) -> stats.nba.com label: 2027 -> '2026-27'."""
    return f"{season - 1}-{season % 100:02d}"

