"""The installer scripts against the host's own tools, where those differ from the ones the tests run on.

Seen on Ubuntu with uutils coreutils:
`install: invalid group: '99'`. That `install` looks a numeric group up in the host's group database and
refuses a number with no entry; GNU's, which this suite runs on, accepts any number. A container image's
group (99 for HAProxy's, 10001 for the pool's) has no entry on the host. So files for a container's group are
made by `install_for_gid` / `install_dir_for_gid`: `install` for root, then `chown`, which takes any number.

Text checks, because the difference cannot be reproduced with the tools installed here."""

import re
from pathlib import Path

INSTALLERS = Path(__file__).resolve().parents[2] / "deploy" / "install"
SCRIPTS = (
    "install.sh",
    "install-pool.sh",
    "pool-lib.sh",
    "lean-pool-ctl",
    "join-box.sh",
    "kimina-ctl",
    "kimina-firewall",
)


def code_lines(script: str):
    for number, line in enumerate((INSTALLERS / script).read_text().splitlines(), start=1):
        yield number, line.split(" #", 1)[0]


def test_no_script_asks_install_for_a_group_held_in_a_variable():
    """The one exception names a group by NAME, read from the host itself (`id -gn` of the door's user)."""
    offenders = [
        f"{script}:{number}: {code.strip()}"
        for script in SCRIPTS
        for number, code in code_lines(script)
        if re.search(r"\binstall\b.*\s-g\s+\"?\$", code) and "id -gn" not in code
    ]
    assert offenders == []


def test_the_helpers_install_for_root_and_hand_over_with_chown():
    for script in ("pool-lib.sh", "lean-pool-ctl"):
        text = (INSTALLERS / script).read_text()
        for helper in ("install_for_gid() {", "install_dir_for_gid() {"):
            start = text.index(helper)
            body = text[start : text.index("\n}\n", start)]
            assert "-g root" in body and 'chown root:"$' in body and "chmod" in body, (
                script,
                helper,
            )
            assert body.index("-g root") < body.index("chown root:")


def test_every_container_group_file_goes_through_a_helper():
    """The seven places the first version called `install -g <number>`: three directories and one file per
    join window, the pool's two certificate bundles and their directory, a box's bundle and its directory.
    An eighth since pools ship their image: the directory of the Lean image the pool's host exports for joining boxes."""
    uses = {
        script: sum(
            len(re.findall(r"\binstall(?:_dir)?_for_gid\s+0", code))
            for _, code in code_lines(script)
        )
        for script in ("install-pool.sh", "pool-lib.sh", "lean-pool-ctl")
    }
    assert uses == {"install-pool.sh": 2, "pool-lib.sh": 3, "lean-pool-ctl": 3}
