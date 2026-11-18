"""requirements-analysis.txt (slim cloud install) must pin the same versions as requirements.txt."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pins(name):
    """{normalized package name: version} for every `pkg==version` line, ignoring markers and comments."""
    out = {}
    for line in (ROOT / name).read_text(encoding="utf-8-sig").splitlines():
        m = re.match(r"\s*([A-Za-z0-9_.-]+)==([^\s;#]+)", line)
        if m:
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return out


def test_analysis_pins_match_full_requirements():
    full, slim = pins("requirements.txt"), pins("requirements-analysis.txt")
    # packages pinned only for non-Windows platforms (absent from the Windows-built requirements.txt)
    platform_only = {"pexpect", "ptyprocess"}
    assert set(slim) - set(full) <= platform_only, "add these to requirements.txt"
    mismatched = {k: (v, full[k]) for k, v in slim.items() if k in full and v != full[k]}
    assert not mismatched, f"versions differ (analysis, full): {mismatched}"


def test_analysis_covers_read_path():
    slim = pins("requirements-analysis.txt")
    for pkg in ("pandas", "numpy", "scipy", "sqlalchemy", "psycopg2-binary", "requests", "python-dotenv", "pytest"):
        assert pkg in slim, pkg
