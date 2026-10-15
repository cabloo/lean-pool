"""The installers' request validation, and text checks of what can only run as root.

The scripts are run as a non-root user, so what is observable is their request validation: a request that
passes it stops at the root check (exit 1, "must run as root" / "run with sudo"), a malformed one is
refused before that. What the scripts DO as root is run on a fake machine in test_install_run.py (the
installers, lean-pool-ctl, a whole join) and test_install_join_script.py (the join script).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parent / "site"
INSTALLERS = Path(__file__).resolve().parents[2] / "deploy" / "install"

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="request validation is observable only as non-root"
)


def run(
    script: str, *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """One of the installers by name; install.sh is given the tests' site."""
    if script == "install.sh":
        arguments = ("--site", str(SITE), *arguments)
    return subprocess.run(
        ["bash", str(INSTALLERS / script), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **(environment or {})},
    )


# --- the door ---------------------------------------------------------------------------------------------


def door(command: str) -> int:
    return run("kimina-ctl", environment={"SSH_ORIGINAL_COMMAND": command}).returncode


@pytest.mark.parametrize(
    "command",
    [
        "@proxy-demo status",
        "@proxy-demo servers",
        "@proxy-demo check",
        "@proxy-demo logs 200",
        "@proxy-demo restart",
        "@proxy-demo cache-stop",
        "@proxy-demo cache-start",
        "@proxy-demo ca",  # the authority's certificate is public
        "@proxy-demo join-open",
        "@proxy-demo join-open 90",
        "@proxy-demo join-open 1440",
        "@proxy-demo admit 192.168.1.20:18001 8",
        "@proxy-demo admit 192.168.1.20:18001 8 laptop",  # held to that name's certificate
        "@proxy-demo add-backend laptop laptop.lan:18001 8",
        "@proxy-demo add-backend laptop laptop.lan:18001 8 18201",
        "@proxy-demo add-backend lap-top-2 192.168.50.44:18001 8",
        "@proxy-demo remove-backend laptop",
        "@proxy-demo remove-backend Workstation",
        "@proxy-demo remove-backend old_box.lan",  # whatever an older list holds
        "@proxy-demo pause laptop",
        "@proxy-demo resume laptop",
        "@proxy-demo pause Workstation",
        "@proxy-demo resume old_box.lan",
        "@proxy-demo set-workers laptop 6",
        "@proxy-demo set-workers Workstation 13",
    ],
)
def test_the_door_accepts_the_pool_verbs(command):
    assert door(command) == 1  # passed validation, stopped at the root check


@pytest.mark.parametrize(
    "command",
    [
        "@proxy-demo shell",
        "@proxy-demo render",
        "@proxy-demo reload",  # not offered through the door
        "@proxy-DEMO status",
        "@proxy- status",
        "@proxy-demo add-backend laptop laptop.lan 8",  # no port
        "@proxy-demo add-backend lap/top laptop.lan:18001 8",
        # A server that is added is named by ONE DNS label in lower case: its name is the name on its certificate.
        "@proxy-demo add-backend lap.top laptop.lan:18001 8",
        "@proxy-demo add-backend workstation.lan laptop.lan:18001 8",
        "@proxy-demo add-backend Laptop laptop.lan:18001 8",
        "@proxy-demo add-backend lap_top laptop.lan:18001 8",
        "@proxy-demo add-backend -laptop laptop.lan:18001 8",
        "@proxy-demo admit laptop.lan:18001 8 Laptop",
        "@proxy-demo admit laptop.lan:18001 8 laptop.lan",
        "@proxy-demo add-backend laptop laptop.lan:18001 eight",
        "@proxy-demo add-backend laptop laptop.lan:18001 8 18201 extra",
        "@proxy-demo admit laptop.lan:18001",
        "@proxy-demo admit laptop.lan:18001 8 lap/top",
        "@proxy-demo remove-backend",
        "@proxy-demo pause",
        "@proxy-demo resume",  # one server, by name
        "@proxy-demo pause laptop desktop",
        "@proxy-demo resume laptop --no-admission",
        "@proxy-demo pause lap/top",
        "@proxy-demo resume 192.168.50.44:18001",
        "@proxy-demo pause-all",
        "@proxy-demo drain laptop",  # nothing else about pausing is offered
        "@proxy-demo set-workers laptop",
        "@proxy-demo set-workers 6",  # one server by name, and one count
        "@proxy-demo set-workers laptop six",
        "@proxy-demo set-workers laptop 6 7",
        "@proxy-demo set-workers lap/top 6",
        "@proxy-demo set-workers laptop 1000",
        "@proxy-demo set-workers laptop -1",
        "@proxy-demo set-repls 6",  # a box's own count is set on the box
        "@proxy-demo ca extra",
        "@proxy-demo join-open 0",
        "@proxy-demo join-open 1441",
        "@proxy-demo join-open ten",
        "@proxy-demo join-open 60 60",
        "@proxy-demo join-open --minutes 60",  # the door takes the number alone
        "@proxy-demo join-close",
        "@proxy-demo sign laptop",  # nothing else about joining is offered
        "@proxy-demo status; id",
        "@proxy-demo logs 501",
    ],
)
def test_the_door_refuses_everything_else_about_the_pool(command):
    assert door(command) == 2


# --- lean-pool-ctl ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("demo", "status"), 1),
        (("demo", "add-backend", "laptop", "laptop.lan:18001", "8", "18201"), 1),
        (("demo", "remove-backend", "laptop"), 1),
        (("demo", "admit", "10.0.0.5:18001", "16"), 1),
        (("demo", "admit", "10.0.0.5:18001", "16", "laptop"), 1),
        (("demo", "admit", "10.0.0.5:18001", "16", "lap top"), 2),
        (("demo", "ca"), 1),
        (("demo", "ca", "key"), 2),
        (("demo", "join-open"), 1),
        (("demo", "join-open", "--minutes", "45"), 1),
        (("demo", "join-open", "--minutes", "0"), 2),
        (("demo", "join-open", "--minutes", "1441"), 2),
        (("demo", "join-open", "45"), 2),
        (
            ("demo", "join-open", "--minutes", "45", "--token", "x"),
            2,
        ),  # a token is never taken from a caller
        (("demo", "render", "--check"), 1),
        (("demo", "render", "--no-reload"), 1),
        (("demo", "render", "--force"), 2),
        (("demo",), 2),
        (("DEMO", "status"), 2),
        (("demo", "add-backend", "laptop", "laptop.lan:18001", "0"), 2),  # zero workers
        (("demo", "add-backend", "laptop", "laptop.lan:18001", "257"), 2),
        (("demo", "add-backend", "laptop", "laptop.lan:port", "8"), 2),
        (("demo", "add-backend", "a b", "laptop.lan:18001", "8"), 2),
        (("demo", "add-backend", "lap.top", "laptop.lan:18001", "8"), 2),  # one label, no dot
        (("demo", "add-backend", "Laptop", "laptop.lan:18001", "8"), 2),
        (("demo", "add-backend", "lap_top", "laptop.lan:18001", "8"), 2),
        (("demo", "add-backend", "x" * 41, "laptop.lan:18001", "8"), 2),
        (("demo", "add-backend", "x" * 40, "laptop.lan:18001", "8"), 1),
        (("demo", "admit", "10.0.0.5:18001", "16", "Laptop"), 2),
        (("demo", "remove-backend", "Old_Box.lan"), 1),  # an older list's name can be removed
        (("demo", "remove-backend", "../servers"), 2),
        (("demo", "pause", "laptop"), 1),
        (("demo", "resume", "laptop"), 1),
        (("demo", "pause", "Old_Box.lan"), 1),  # an older list's name can be paused
        (("demo", "pause"), 2),
        (("demo", "resume", "laptop", "desktop"), 2),
        (("demo", "pause", "../servers"), 2),
        (("demo", "resume", "lap top"), 2),
        (("demo", "set-workers", "laptop", "6"), 1),
        (
            ("demo", "set-workers", "Old_Box.lan", "256"),
            1,
        ),  # an older list's name, the largest count
        (("demo", "set-workers", "laptop"), 2),
        (("demo", "set-workers", "laptop", "0"), 2),
        (("demo", "set-workers", "laptop", "257"), 2),
        (("demo", "set-workers", "laptop", "6", "7"), 2),
        (("demo", "set-workers", "../servers", "6"), 2),
        (("demo", "status", "extra"), 2),
    ],
)
def test_lean_pool_ctl_validates_before_it_asks_for_root(arguments, expected):
    assert run("lean-pool-ctl", *arguments).returncode == expected


def test_the_candidate_config_is_made_readable_before_haproxy_validates_it():
    """An install stops here without it: `mktemp -d` is root-only and HAProxy's image runs as its own
    unprivileged user ("Cannot open configuration file/directory /cfg/haproxy.cfg : Permission denied").
    A text check, because the real one needs root and Docker."""
    text = (INSTALLERS / "lean-pool-ctl").read_text()
    body = text[text.index("render_and_validate() {") : text.index("install_and_reload() {")]
    assert body.index('chmod 0755 "$work"') < body.index("haproxy -c -f /cfg/haproxy.cfg")
    assert 'chmod 0644 "$work/haproxy.cfg"' in body


def test_status_and_check_still_answer_with_the_cache_stopped_and_errors_do_not_print_their_exit_code():
    """Both seen on a real machine: with the cache stopped `status` showed nothing about the Lean servers,
    because it ran inside the cache container; and a failed admission printed "the pool is unchanged 4". Text
    checks, for the same reason as above."""
    text = (INSTALLERS / "lean-pool-ctl").read_text()
    start = text.index("inside_pool() {")
    body = text[
        start : text.index('case "$verb" in', start)
    ]  # an earlier `case` validates the arguments
    assert 'docker run --rm -i --network "container:$PROXY"' in body and "docker exec" not in body
    assert "die() { printf 'lean-pool-ctl: %s\\n' \"$1\" >&2;" in text


# --- the installer ----------------------------------------------------------------------------------------


def test_a_named_instance_gets_a_usage_agent_and_instance_one_does_not():
    def shown(*arguments):
        result = run("install.sh", *arguments, "--show-instance")
        assert result.returncode == 0, result.stderr
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    assert shown()["agent"] == "0"
    demo = shown("--instance", "demo")
    assert demo["agent"] == "1" and demo["agent_port"] == "18201"
    assert shown("--instance", "demo", "--no-agent")["agent"] == "0"


@pytest.mark.parametrize(
    ("content", "accepted"),
    [
        ("0123456789abcdef0123456789abcdef\n", True),
        ("", False),
        ("short\n", False),
        (
            "has spaces in the key 0123456789\n",
            True,
        ),  # whitespace is stripped; what remains is a key
        ("bad/characters/0123456789abcdef\n", False),
    ],
)
def test_api_key_file_is_validated_before_root_is_asked_for(tmp_path, content, accepted):
    key_file = tmp_path / "key"
    key_file.write_text(content)
    result = run("install.sh", "--instance", "demo", "--api-key-file", str(key_file))
    assert result.returncode == 1
    assert ("run with sudo" in result.stderr) is accepted, result.stderr
    assert ("must hold one key" in result.stderr) is (not accepted), result.stderr


def test_the_pools_key_is_never_an_argument_of_a_program():
    """Text check (needs root): install.sh writes the pool's key with the shell's own printf. Through
    `set_env` it would be an argument of sed, and a command line is visible to every user of the box."""
    text = (INSTALLERS / "install.sh").read_text()
    assert "set_env LEAN_SERVER_API_KEY" not in text
    assert 'printf \'LEAN_SERVER_API_KEY=%s\\n\' "$POOL_KEY" > "$DIR/kimina.env.new"' in text
    for line in text.splitlines():
        if "$POOL_KEY" in line:
            assert line.lstrip().startswith(("printf ", "[[ ", "[ -n ", "if [ -n ")), line


def test_a_missing_api_key_file_is_refused(tmp_path):
    result = run("install.sh", "--instance", "demo", "--api-key-file", str(tmp_path / "absent"))
    assert result.returncode == 1 and "no such file" in result.stderr


# --- joining is by pulling: no SSH between hosts ------------------------------------------------------------

# The installers, and the programs of the Lean server's directory they run (the compiled search step, the
# read-only check).
SCRIPTS = sorted(
    path
    for path in [*INSTALLERS.iterdir(), *(INSTALLERS.parent / "lean-server").rglob("*")]
    if path.is_file()
    and (
        path.suffix in (".sh", ".py")
        or path.name in ("kimina-ctl", "lean-pool-ctl", "kimina-firewall", "repl-native")
    )
)


def commands_of(path: Path) -> str:
    """A script's code without what is only text: comments, quoted strings and the lines of here-documents."""
    kept, here = [], None
    for line in path.read_text().splitlines():
        if here is not None:
            here = None if line.strip() == here else here
            continue
        opened = re.search(r"<<-?\s*'?\"?([A-Za-z_]+)", line)
        if opened and "<<<" not in line:
            here = opened.group(1)
        code = re.sub(
            r"'[^']*'", "''", line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
        )
        kept.append(re.sub(r'"(?:[^"\\]|\\.)*"', '""', code))
    return "\n".join(kept)


def test_no_installer_runs_ssh_or_scp():
    """A server joins by pulling, and the pool's host never reaches into another host."""
    assert len(SCRIPTS) >= 12
    callers = [
        path.name
        for path in SCRIPTS
        if re.search(r"(?<![\w./$-])(ssh|scp|sftp|rsync)(?![\w./=-])", commands_of(path))
    ]
    assert callers == []


def test_no_script_signals_a_container_through_docker():
    """Confirmed against Docker's source: `docker kill` marks a container as stopped by
    hand, WHATEVER the signal, and with `restart: unless-stopped` such a container is not started at the next
    boot nor restarted after a crash. Every reload is `kill -HUP 1` from inside the container instead. A text
    check over every script; the fake-machine run shows a daemon restart bringing the proxy and the front back."""
    for path in SCRIPTS:
        assert not re.search(r"docker\s+(kill|stop\s+-s|container\s+kill)", commands_of(path)), (
            path.name
        )
    reload = "docker exec \"$1\" sh -c 'kill -HUP 1'"
    assert (
        reload in (INSTALLERS / "pool-lib.sh").read_text()
        and reload in (INSTALLERS / "lean-pool-ctl").read_text()
    )
    control = (INSTALLERS / "lean-pool-ctl").read_text()
    assert (
        'reload_haproxy "$PROXY"'
        in control[control.index("install_and_reload() {") : control.index("listed_name_of() {")]
    )
    assert (
        'reload_haproxy "$PROXY"'
        in control[control.index("  reload)\n") : control.index("  cache-stop)\n")]
    )
    for name in (
        "pool/compose.yaml",
        "lean-server/compose.tls.yaml",
        "lean-server/compose.agent.yaml",
        "lean-server/compose.yaml",
    ):
        assert (
            "restart: unless-stopped" in (INSTALLERS.parent / name).read_text()
        )  # kept: what the reload must not defeat


def test_the_pools_verify_loop_gives_the_lean_server_five_minutes():
    """Text check (the fake-machine run shows a server that needs twenty tries ending verified): the upgrade
    recreates this box's Lean server, and install.sh itself allows such a server five minutes."""
    text = (INSTALLERS / "install-pool.sh").read_text()
    verify = text[
        text.index(
            "# --------------------------------------------------------------------------------------------- verify"
        ) :
    ]
    assert "for _ in $(seq 1 30); do" in verify and "sleep 10" in verify and "seq 1 12" not in text
    assert (
        "for _ in $(seq 1 60); do" in (INSTALLERS / "install.sh").read_text()
    )  # install.sh: 60 x 5 s for the same server


def test_text_from_a_join_request_is_made_printable_before_it_is_shown_or_sent():
    """Text check (the fake-machine run sends a request with an escape sequence and reads what comes back):
    in `join-open`, every place that shows a tool's words about a request, or writes them into a reply,
    passes them through `printable` (printable ASCII, one line, capped)."""
    control = (INSTALLERS / "lean-pool-ctl").read_text()
    assert (
        "printable()       { LC_ALL=C tr -c ' -~' ' ' | LC_ALL=C tr -s ' ' | cut -c1-\"${1:-300}\""
        in control
    )
    assert (
        'refuse_box "the signing request was not signed: $(printf \'%s\' "$signing" | printable 240)"'
        in control
    )
    assert "the server list refused $name: $(printf '%s' \"$refusal\" | printable 300)" in control
    refuse = control[
        control.index("    refuse_box() {") : control.index("    # 1. the signing request")
    ]
    assert (
        "reason=$(printf '%s' \"$1\" | printable 400)" in refuse and '--reason "$reason"' in refuse
    )
    assert 'echo "==> REFUSED: $reason.' in refuse and "$1." not in refuse
    assert "tr '\\n' ' '" not in control  # the old way: line breaks only
    for line in control.splitlines():
        if "docker logs" in line and "grep" not in line and "lines=$(" not in line:
            assert "| printable_lines" in line, line


def test_the_installer_for_instance_one_takes_no_tls_option_and_install_pool_never_names_it():
    """Text check, beside the fake-machine run that shows instance 1 untouched: install-pool.sh and
    lean-pool-ctl never name /srv/kimina (instance 1) at all, only /srv/kimina-NAME and /srv/lean-pool-NAME."""
    for name in ("install-pool.sh", "lean-pool-ctl"):
        code = commands_of(INSTALLERS / name)
        assert not re.search(r"/srv/kimina(?![-\w])", code), name
        assert "kimina-firewall.service" not in (INSTALLERS / name).read_text(), name
    assert "INSTANCE_DIR=/srv/kimina-$POOL" in (INSTALLERS / "install-pool.sh").read_text()


def test_the_authority_is_made_once_and_never_replaced():
    """Text check (the fake-machine run shows a re-run keeps it): `init` is reached only when neither file
    is there, half an authority stops the installer, and no command that touches the authority's directory
    carries --replace."""
    text = (INSTALLERS / "install-pool.sh").read_text()
    made = text[
        text.index(
            'if [ -s "$DIR/pki/ca/ca.crt" ] && [ -s "$DIR/pki/ca/ca.key" ]; then'
        ) : text.index("# The front door:")
    ]
    assert (
        made.index("keeping the pool's certificate authority")
        < made.index("it is never overwritten")
        < made.index("pki init --ca-dir /pki/ca")
    )
    assert "--replace" not in made
    assert 'install -d -o root -g root -m 0700 "$DIR/pki"' in text
    for line in text.splitlines() + (INSTALLERS / "lean-pool-ctl").read_text().splitlines():
        if "--out-dir /pki/ca" in line or "--out /pki/ca" in line:
            raise AssertionError(line)
    # A window signs with the authority mounted read-only, and for the validated name alone.
    control = (INSTALLERS / "lean-pool-ctl").read_text()
    assert (
        '-v "$DIR/pki/ca":/pki/ca:ro' in control
        and '--allow-dns "$name" 2>&1' in control
        and "--allow-ip" not in control
    )


def test_a_join_window_cleans_up_on_every_way_out():
    """Text check (the fake-machine run shows a verdict, a refusal, the time limit and Ctrl-C; a killed
    terminal or door cannot be produced there): INT, TERM, HUP and a closed pipe all exit through the EXIT
    trap, which stops the service and deletes the window's directory."""
    text = (INSTALLERS / "lean-pool-ctl").read_text()
    cleanup = text[text.index("cleanup() {") : text.index("trap cleanup EXIT")]
    assert 'docker rm -f -v "$JOIN_SERVICE"' in cleanup and 'rm -rf "$WINDOW"' in cleanup
    window = text[
        text.index("  join-open)\n") : text.index("  logs)\n")
    ]  # the branch that runs, not the validation
    for signal_name, status in (("INT", 130), ("TERM", 143), ("HUP", 129), ("PIPE", 141)):
        assert f"trap 'exit {status}' {signal_name}" in window
    assert window.index("trap 'exit 130' INT") < window.index(
        'WINDOW=$(mktemp -d "$DIR/.join.XXXXXX")'
    )
    assert window.index("JOIN_STARTED=1") < window.index('docker run -d --name "$JOIN_SERVICE"')
    # The token: 16 random bytes, made here, written to a file; printed in the one command and nowhere else.
    assert "head -c 16 /dev/urandom | base64" in window
    assert window.count('cat "$WINDOW/serve/token"') == 1 and 'echo "$token' not in window
