"""bootstrap.sql's up-front password check (ZB-08), read as text.

CI runs bootstrap.sql only with all three passwords set, so nothing there would notice a later edit
that puts a role change above the check or drops part of it, and with it brings back PASSWORD ''
(which clears a role's password). These tests read the file instead.
"""

import re

import pytest

from tests.conftest import API_DIR

BOOTSTRAP = API_DIR / "migrations" / "bootstrap.sql"
PASSWORDS = {
    "migrate_password": "ZIF_MIGRATE_PASSWORD",
    "app_password": "ZIF_APP_PASSWORD",
    "worker_password": "ZIF_WORKER_PASSWORD",
}
# Anything that creates, changes or drops a role (ROLE, USER and GROUP are synonyms) or a grant,
# or sets a password. CREATE ROLE also counts inside the SELECT '...' \gexec that builds it.
CHANGE = re.compile(
    r"\b((?:CREATE|ALTER|DROP)\s+(?:ROLE|USER|GROUP)|GRANT|REVOKE)\b|(\\password)\b",
    re.IGNORECASE,
)


def without_comments(source: str) -> str:
    """The source with every -- comment blanked out, so offsets stay those of the file."""
    return re.sub(r"--[^\n]*", lambda m: " " * len(m.group()), source)


def problems(source: str) -> list[str]:
    """Everything wrong with the check in this bootstrap.sql source; empty when it holds."""
    code = without_comments(source)
    found: list[str] = []
    getenvs = dict(re.findall(r"^\\getenv (\w+) (\w+)$", code, re.MULTILINE))
    if getenvs != PASSWORDS:
        found.append(f"the \\getenv lines read {getenvs}, not {PASSWORDS}")
    stop_ends = []
    for var in PASSWORDS:
        x = re.escape(var)
        parts = {
            "getenv": rf"^\\getenv {x} \w+$",
            "default": rf"^\\set {x}_missing true$",
            "gset": rf"^\\if :\{{\?{x}\}}\n"
            rf"SELECT :'{x}' = '' AS {x}_missing \\gset\n"
            r"\\endif$",
            "stop": rf"^\\if :{x}_missing\n"
            r"\\set ON_ERROR_STOP on\n"
            r"DO \$\$ BEGIN RAISE EXCEPTION '[^'\n]+'; END \$\$;\n"
            r"\\endif$",
        }
        spans = {}
        for name, pattern in parts.items():
            matches = list(re.finditer(pattern, code, re.MULTILINE))
            if len(matches) != 1:
                found.append(f"{var}: {len(matches)} {name} blocks, not 1")
            else:
                spans[name] = matches[0].span()
        if len(spans) == len(parts):
            starts = [spans[name][0] for name in parts]
            if starts != sorted(starts):
                found.append(f"{var}: not in the order getenv, default, gset, stop")
            stop_ends.append(spans["stop"][1])
    first_change = CHANGE.search(code)
    if first_change is None:
        found.append("no CREATE ROLE, ALTER ROLE, GRANT or REVOKE at all")
    elif stop_ends and max(stop_ends) > first_change.start():
        found.append(f"{first_change.group(0)!r} comes before the last stop block ends")
    # Every password the file sends is one the check covers, and is sent after the check.
    for match in re.finditer(r"PASSWORD :'(\w+)'", code):
        if match.group(1) not in PASSWORDS:
            found.append(f"PASSWORD :'{match.group(1)}' is not checked")
        if stop_ends and match.start() < max(stop_ends):
            found.append(f"PASSWORD :'{match.group(1)}' is sent before the last stop block ends")
    return found


# ZB-08 FENCE. Wrong impl: a CREATE ROLE, ALTER ROLE, GRANT or REVOKE above the check, or a
# password without its missing-by-default flag, its \gset or its stop block.
def test_bootstrap_checks_every_password_before_it_changes_anything() -> None:
    assert problems(BOOTSTRAP.read_text()) == []


SOURCE = BOOTSTRAP.read_text()
MIGRATE_ALTER = "ALTER ROLE ziftbook_migrate PASSWORD :'migrate_password';\n"
APP_GSET = (
    "\\if :{?app_password}\nSELECT :'app_password' = '' AS app_password_missing \\gset\n\\endif\n"
)
WORKER_STOP = (
    "\\if :worker_password_missing\n\\set ON_ERROR_STOP on\n"
    "DO $$ BEGIN RAISE EXCEPTION 'ZIF_WORKER_PASSWORD is unset or empty: ziftbook_worker needs a"
    " password'; END $$;\n\\endif\n"
)
GETENVS_END = "\\getenv worker_password ZIF_WORKER_PASSWORD\n"


def moved_above_the_check(source: str, statement: str) -> str:
    return source.replace(statement, "").replace(GETENVS_END, GETENVS_END + statement)


# The fence fails on each way of breaking the check; each edit must apply to today's file.
@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(moved_above_the_check(SOURCE, MIGRATE_ALTER), id="alter-above-the-check"),
        pytest.param(
            SOURCE.replace(
                GETENVS_END, GETENVS_END + 'GRANT CONNECT ON DATABASE :"DBNAME" TO x;\n'
            ),
            id="grant-above-the-check",
        ),
        pytest.param(
            SOURCE.replace(MIGRATE_ALTER, "").replace(
                GETENVS_END, GETENVS_END + MIGRATE_ALTER.replace("ALTER ROLE", "ALTER USER")
            ),
            id="alter-user-above-the-check",
        ),
        pytest.param(SOURCE.replace("\\set app_password_missing true\n", ""), id="no-default"),
        pytest.param(SOURCE.replace(APP_GSET, ""), id="no-gset"),
        pytest.param(SOURCE.replace(WORKER_STOP, ""), id="no-stop"),
        pytest.param(
            SOURCE.replace(
                "\\set ON_ERROR_STOP on\nDO $$ BEGIN RAISE EXCEPTION 'ZIF_APP",
                "DO $$ BEGIN RAISE EXCEPTION 'ZIF_APP",
            ),
            id="stop-without-on-error-stop",
        ),
    ],
)
def test_the_fence_catches_a_broken_check(broken: str) -> None:
    assert broken != SOURCE
    assert problems(broken) != []
