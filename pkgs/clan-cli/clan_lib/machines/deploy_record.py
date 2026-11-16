"""Who deployed what to a NixOS machine.

An update leaves a record next to the generation it made. The next update
refuses to run when the machine has something that the local checkout does
not contain, because it would revert it.
"""

import getpass
import json
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from clan_lib.cmd import Log, RunOpts, run
from clan_lib.errors import ClanError
from clan_lib.flake import Flake

if TYPE_CHECKING:
    from clan_lib.ssh.host import Host

log = logging.getLogger(__name__)

PROFILES = "/nix/var/nix/profiles"
# `test` makes no generation, and its system is gone after a reboot
TEST_RECORD = "/run/clan-test-deploy.json"

_GENERATION = (
    f"gen=$(readlink {PROFILES}/system); gen=${{gen#system-}}; gen=${{gen%-link}}"
)

_READ = f"""
readlink -f /run/current-system
{_GENERATION}
echo "$gen"
ls {PROFILES}/system-*-deploy.json 2>/dev/null | wc -l
cat "{PROFILES}/system-$gen-deploy.json" 2>/dev/null || echo null
cat {TEST_RECORD} 2>/dev/null || echo null
"""

# $0: the system the record is for. After a failed update the profile may
# still be at an older generation, which is not ours to label.
_WRITE = f"""
set -eu
[ "$(readlink -f {PROFILES}/system)" = "$0" ] || exit 0
{_GENERATION}
cat > {PROFILES}/.deploy.tmp
mv {PROFILES}/.deploy.tmp "{PROFILES}/system-$gen-deploy.json"
for f in {PROFILES}/system-*-deploy.json; do
  [ -L "${{f%-deploy.json}}-link" ] || rm -f "$f"
done
"""


def action() -> str:
    return os.environ.get("CLAN_DEPLOY_ACTION", "switch")


def _git(flake: Flake, *args: str) -> str | None:
    if not flake.is_local:
        return None
    res = run(
        ["git", "-C", str(flake.path), *args],
        RunOpts(check=False, log=Log.NONE),
    )
    return res.stdout.strip() if res.returncode == 0 else None


def _source(flake: Flake) -> dict[str, Any]:
    return {
        "rev": _git(flake, "rev-parse", "HEAD"),
        "branch": _git(flake, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(_git(flake, "status", "--porcelain")),
        "user": _git(flake, "config", "user.name") or getpass.getuser(),
    }


def _blocked(flake: Flake, src: dict[str, Any], out: str) -> str | None:
    current, generation, count, *records = out.strip().split("\n")
    record, test = (json.loads(r) for r in records)
    if test and test["system"] == current:
        record = test
    if not record:
        # no record on the whole machine: its first update with records
        if count.strip() == "0":
            return None
        return f"generation {generation} has no deploy record: it was deployed by other means"

    by = f"{record['user']}, {record['action']} of {str(record['rev'])[:10]} ({record['branch']}), {record['time']}"
    # the same commit again, e.g. after a failed switch
    if record["rev"] == src["rev"] and not record["dirty"] and not src["dirty"]:
        return None
    # after a `boot` the old system runs until the reboot
    if record["system"] != current and record["action"] != "boot":
        return f"it runs {current}, which is not the system of its record ({by}): switched without a new generation"
    if record["dirty"]:
        return f"its record is from a dirty tree ({by})"
    if (
        src["rev"]
        and record["rev"]
        and _git(flake, "merge-base", "--is-ancestor", record["rev"], "HEAD") is None
    ):
        return f"it runs a commit that is not in HEAD ({by})"
    return None


def check(flake: Flake, host: "Host", machine_name: str) -> None:
    if action() == "dry-activate":
        return
    res = host.run(
        ["sh", "-c", _READ],
        RunOpts(check=False, log=Log.NONE, prefix=machine_name, trace=False),
    )
    if res.returncode != 0:
        return
    try:
        why = _blocked(flake, _source(flake), res.stdout)
    except (ValueError, KeyError):
        why = "its deploy record is unreadable"
    if why is None:
        return
    if os.environ.get("CLAN_DEPLOY_FORCE") == "1":
        log.warning(f"{machine_name}: forced: {why}")
        return
    msg = f"Not updating '{machine_name}': {why}. Pass --force to deploy over it."
    raise ClanError(msg)


def write(flake: Flake, host: "Host", machine_name: str, system: str) -> None:
    if action() == "dry-activate":
        return
    record = {
        **_source(flake),
        "action": action(),
        "time": datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "system": system,
    }
    host.run(
        ["sh", "-c", f"cat > {TEST_RECORD}" if action() == "test" else _WRITE, system],
        RunOpts(
            input=(json.dumps(record) + "\n").encode(),
            check=False,
            log=Log.STDERR,
            trace=False,
            prefix=machine_name,
        ),
    )
