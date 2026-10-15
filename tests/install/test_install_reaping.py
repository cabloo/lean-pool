"""Why the Lean server's container has an init process.

The Kimina Lean Server (`server/repl.py` at the pinned commit fb2393de) starts each worker as `lake env repl`
in a session of its own (line 147: `os.setsid`; line 149: `lake`), so a worker is two processes below the
server: python -> lake -> repl. To close one it kills the whole process group and then waits for its direct
child alone (lines 348 and 349: `os.killpg(..., SIGKILL)`, `await self.proc.wait()`). The repl, whose parent
has just died, is handed to the first process of the container. In the image that process is the server
itself (`CMD ["python", "-m", "server"]`), which collects only the children it started: the repl stays a
zombie, and a zombie holds a process slot under the container's `pids_limit` until the container is made
again. The count only grows, by one for each worker the server closes, and at 4096 every check is answered
500 while `/health` still passes.

With `init: true` (docker-compose.yml) Docker's init is the first process and the server its child, and an
init collects whatever is handed to it.

The mechanism needs neither Docker nor Lean, so it runs here. A process takes the first process's role with
PR_SET_CHILD_SUBREAPER (orphans below it are handed to it, as they are to PID 1 in a container), and
`sh -c 'sleep 1000 & wait'` stands in for `lake env repl`: a parent that starts one child and waits for it.
Each case runs in a Python of its own, so the test process adopts nothing. What this cannot show is Docker's
own init doing the collecting: that is checked on a real server (`status`: an init process, no zombie).

The last test is why `status` counts zombies with the host's `ps` and not with `docker top`: the kernel does
not list a zombie in its control group's `cgroup.procs`, which is where a container's PIDs are read from.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="process adoption and /proc are Linux's"
)

CLOSES = 5

FIRST_PROCESS = r'''
import ctypes
import json
import os
import signal
import subprocess
import sys
import time

role, closes = sys.argv[1], int(sys.argv[2])
PR_SET_CHILD_SUBREAPER = 36
assert ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0, os.strerror(ctypes.get_errno())


def children_of(parent, state=None):
    """The processes whose parent is `parent`, all of them or those in one state (Z: exited, collected by nobody)."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as handle:
                its_state, its_parent = handle.read().rsplit(")", 1)[1].split()[:2]
        except OSError:
            continue
        if int(its_parent) == parent and state in (None, its_state):
            found.append(int(name))
    return found


def wait_until(condition, seconds=20.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


def close_workers():
    """What the server does with every worker it closes."""
    for _ in range(closes):
        worker = subprocess.Popen(["sh", "-c", "sleep 1000 & wait"], start_new_session=True)   # repl.py:147-150
        wait_until(lambda: children_of(worker.pid))             # `lake` has started its `repl`
        os.killpg(os.getpgid(worker.pid), signal.SIGKILL)       # repl.py:348
        worker.wait()                                           # repl.py:349: the direct child, and only that one


me = os.getpid()
if role == "the server":
    # No init: the server is the first process, and what its closed workers leave is handed to it.
    close_workers()
    wait_until(lambda: len(children_of(me, "Z")) >= closes)
    report = {"closed": closes, "zombies": len(children_of(me, "Z"))}
    while True:                                                 # leave nothing behind on this machine
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                break
        except ChildProcessError:
            break
else:
    # An init: the server is its child (a fork does not inherit the adopting role), and the init collects
    # every child that ends, the ones handed to it included, until it has none left.
    server = os.fork()
    if server == 0:
        close_workers()
        os._exit(min(len(children_of(os.getpid(), "Z")), 255))  # what the server itself still holds
    collected, server_status = 0, None
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        if pid == server:
            server_status = os.waitstatus_to_exitcode(status)
        else:
            collected += 1
    report = {"closed": closes, "collected": collected, "left_with_the_server": server_status,
              "zombies": len(children_of(me, "Z"))}
print(json.dumps(report))
'''


def as_first_process(role: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", FIRST_PROCESS, role, str(CLOSES)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    said: dict[str, Any] = json.loads(result.stdout)
    return said


def test_a_server_that_is_the_first_process_keeps_one_zombie_for_every_worker_it_closes():
    assert as_first_process("the server") == {"closed": CLOSES, "zombies": CLOSES}


def test_an_init_as_the_first_process_collects_every_one_of_them():
    assert as_first_process("an init") == {
        "closed": CLOSES,
        "collected": CLOSES,
        "left_with_the_server": 0,
        "zombies": 0,
    }


def test_the_kernel_lists_no_zombie_in_its_control_groups_process_list():
    """`docker top` keeps the lines of `ps` whose PID the runtime finds in the container's cgroup.procs. A
    zombie counts against the group's `pids.max` and is not in that list, so `kimina-ctl status` counts
    zombies on the host by PID namespace instead. (Docker's half of this needs Docker, which the tests do not use.)"""
    own = Path("/proc/self/cgroup").read_text().strip().splitlines()
    if len(own) != 1 or not own[0].startswith("0::"):
        pytest.skip("this machine's control groups are not the unified hierarchy")
    listing = Path("/sys/fs/cgroup") / own[0][3:].lstrip("/") / "cgroup.procs"
    try:
        before = listing.read_text().split()
    except OSError:
        pytest.skip(f"{listing} cannot be read from here")
    if str(os.getpid()) not in before:
        pytest.skip(
            f"{listing} is not this process's own list (a control group namespace that hides it)"
        )
    child = subprocess.Popen(["true"])
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.01)
        assert Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
        assert (
            Path(f"/proc/{child.pid}/cgroup").read_text() == Path("/proc/self/cgroup").read_text()
        )  # the same group
        listed = listing.read_text().split()
        assert str(os.getpid()) in listed and str(child.pid) not in listed
    finally:
        child.wait()


# GNU awk refuses a `-v` variable named after one of its own words ("cannot use gawk builtin `namespace' as variable
# name"), and the machines these scripts run on have GNU awk while the one the tests run on has mawk, which takes
# any name. On such a machine `status` printed the zombie count empty. So the names are held
# here, for every deploy script, against GNU awk's list (its tokens table, 5.x).
GNU_AWK_WORDS = frozenset(
    [
        "BEGIN",
        "BEGINFILE",
        "END",
        "ENDFILE",
        "adump",
        "and",
        "asort",
        "asorti",
        "atan2",
        "bindtextdomain",
        "break",
        "case",
        "close",
        "compl",
        "continue",
        "cos",
        "dcgettext",
        "dcngettext",
        "default",
        "delete",
        "do",
        "else",
        "eval",
        "exit",
        "exp",
        "fflush",
        "for",
        "func",
        "function",
        "gensub",
        "getline",
        "gsub",
        "if",
        "in",
        "include",
        "index",
        "int",
        "isarray",
        "length",
        "load",
        "log",
        "lshift",
        "match",
        "mktime",
        "namespace",
        "next",
        "nextfile",
        "or",
        "patsplit",
        "print",
        "printf",
        "rand",
        "return",
        "rshift",
        "sin",
        "split",
        "sprintf",
        "sqrt",
        "srand",
        "stopme",
        "strftime",
        "strtonum",
        "sub",
        "substr",
        "switch",
        "system",
        "systime",
        "tolower",
        "toupper",
        "typeof",
        "while",
        "xor",
    ]
)


def test_no_deploy_script_hands_awk_a_variable_named_after_a_gnu_awk_word():
    import re

    deploy = Path(__file__).resolve().parents[2] / "deploy" / "install"  # the installers
    named = []
    for script in sorted(
        path for path in deploy.rglob("*") if path.is_file() and "__pycache__" not in path.parts
    ):
        try:
            text = script.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "awk" in line:
                named += [
                    (script.name, number, name)
                    for name in re.findall(r"-v\s+([A-Za-z_][A-Za-z_0-9]*)=", line)
                ]
    assert len(named) >= 5, "the scan finds the scripts' awk variables"
    assert ("kimina-ctl", "pid_namespace") in {
        (script, name) for script, _, name in named
    }  # the zombie count's
    assert [found for found in named if found[2] in GNU_AWK_WORDS] == []
