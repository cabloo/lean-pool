"""``docs/tls.md``, "Setting it up": the recipe is followed here, command by command.

This project's own commands are run exactly as the page gives them, in two directories that
stand for the pool box and a Lean server box, and what the page says to carry from one machine
to the other is carried by the test. What needs Docker, root or a Lean server is not run. A
command the test does not know fails it, so the page cannot grow a step that nothing follows.
"""

from __future__ import annotations

import re
import shlex
import shutil
import ssl
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
import yaml
from cryptography import x509

from leanpool.admit.cli import main as admit_main
from leanpool.haproxy.cli import main as haproxy_main
from leanpool.pki.cli import main as pki_main

PROJECT = Path(__file__).parent.parent
PAGE = (PROJECT / "docs" / "tls.md").read_text(encoding="utf-8")
RECIPE = PAGE[PAGE.index("## Setting it up") : PAGE.index("## Changing a pool that uses TLS")]

Command = Callable[[Sequence[str], Mapping[str, str]], int]
COMMANDS: dict[str, Command] = {"leanpool-pki": pki_main, "leanpool-haproxy-config": haproxy_main}
# Run where there is Docker, root, a Lean server or a pool to ask: not here.
NOT_RUN = ("gid=", "sudo ", "docker ", "curl ", "uv run leanpool-admit ")
_STEP = re.compile(r"^### (\d)\. On the (pool box|Lean server box): .*$", re.MULTILINE)
_BLOCK = re.compile(r"^```sh\n(.*?)^```", re.MULTILINE | re.DOTALL)
_JOINED = re.compile(r"\(umask 077 && cat (\S+) (\S+) > (\S+)\)")


def steps() -> list[tuple[int, str, list[str]]]:
    """Each numbered step: its number, its machine, and the commands of its code blocks."""
    found = []
    headings = list(_STEP.finditer(RECIPE))
    for heading, following in zip(headings, [*headings[1:], None], strict=True):
        text = RECIPE[heading.end() : following.start() if following else len(RECIPE)]
        commands = [
            command.strip()
            for block in _BLOCK.findall(text)
            for command in block.replace("\\\n", " ").splitlines()
            if command.strip()
        ]
        found.append((int(heading.group(1)), heading.group(2), commands))
    return found


def follow(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Do, in the current directory, what one command of the page does."""
    if command.startswith(NOT_RUN):
        return
    words = shlex.split(command)
    if words[:2] == ["uv", "run"] and words[2] in COMMANDS:
        target = Path(words[words.index(">") + 1]) if ">" in words else None
        arguments = words[3 : words.index(">")] if ">" in words else words[3:]
        capsys.readouterr()
        assert COMMANDS[words[2]](arguments, {}) == 0, command
        if target is not None:
            target.write_text(capsys.readouterr().out)
    elif words[0] == "cp" and len(words) == 3:
        shutil.copy(words[1], words[2])
    elif words[0] == "mv" and len(words) == 3:
        Path(words[1]).rename(words[2])
    elif words[:2] == ["mkdir", "-p"] and len(words) == 3:
        Path(words[2]).mkdir(parents=True, exist_ok=True)
    elif joined := _JOINED.fullmatch(command):
        first, second, target_name = joined.groups()
        Path(target_name).touch(mode=0o600)
        Path(target_name).write_bytes(Path(first).read_bytes() + Path(second).read_bytes())
    else:
        pytest.fail(f"the recipe has a command this test does not follow: {command}")


def test_the_recipe_can_be_followed_and_leaves_what_the_compose_files_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pool, box = tmp_path / "pool", tmp_path / "lean-server"
    machines = {"pool box": pool, "Lean server box": box}
    # Where the recipe starts: a pool that works in plain HTTP.
    (pool / "haproxy").mkdir(parents=True)
    (pool / "servers").write_text("lean-a 192.0.2.10:8000 4\n")
    box.mkdir()

    for number, machine, commands in steps():
        monkeypatch.chdir(machines[machine])
        for command in commands:
            follow(command, capsys)
        if number == 2:  # "Take the `.csr` file to the pool box, into `deploy/pool`."
            shutil.copy(box / "tls" / "lean-a.csr", pool / "lean-a.csr")
        if number == 3:  # "Take `lean-a.crt` and a copy of `pki/ca/ca.crt` back to the box"
            shutil.copy(pool / "lean-a.crt", box / "tls" / "lean-a.crt")
            shutil.copy(pool / "pki" / "ca" / "ca.crt", box / "tls" / "ca.crt")

    # Steps 1 to 6 are done on a machine of the pool; step 7 is the clients'.
    assert [number for number, _machine, _commands in steps()] == [1, 2, 3, 4, 5, 6]
    # The pool box: what deploy/pool/compose.tls.yaml mounts, and a configuration that uses it.
    (pool_mount,) = yaml.safe_load((PROJECT / "deploy/pool/compose.tls.yaml").read_text())[
        "services"
    ]["haproxy"]["volumes"]
    assert pool_mount == "./pki/proxy:/etc/leanpool/tls:ro"
    assert {"front.pem", "proxy-client.pem", "ca.crt"} <= {
        path.name for path in (pool / "pki" / "proxy").iterdir()
    }
    proxy_config = (pool / "haproxy" / "haproxy.cfg").read_text()
    assert not (pool / "haproxy" / "haproxy.cfg.new").exists()
    assert "bind :18100 ssl crt /etc/leanpool/tls/front.pem" in proxy_config
    assert "verifyhost lean-a" in proxy_config
    assert "crt /etc/leanpool/tls/proxy-client.pem" in proxy_config
    # The Lean server box: what deploy/lean-server/compose.tls.yaml mounts.
    front = yaml.safe_load((PROJECT / "deploy/lean-server/compose.tls.yaml").read_text())[
        "services"
    ]["front"]
    assert front["volumes"] == [
        "./front:/usr/local/etc/haproxy:ro",
        "./tls:/etc/leanpool/tls:ro",
    ]
    front_config = (box / "front" / "haproxy.cfg").read_text()
    assert "crt /etc/leanpool/tls/box.pem" in front_config
    assert "ca-file /etc/leanpool/tls/ca.crt" in front_config
    assert "-m str lean-pool-proxy" in front_config
    # The box's certificate is its key's, is for its name, and was signed by the pool's authority.
    box_pem = box / "tls" / "box.pem"
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(box_pem)
    assert box_pem.stat().st_mode & 0o077 == 0
    certificate = x509.load_pem_x509_certificate(box_pem.read_bytes())
    authority = x509.load_pem_x509_certificate((box / "tls" / "ca.crt").read_bytes())
    certificate.verify_directly_issued_by(authority)
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["lean-a"]


def test_the_admission_command_of_the_recipe_names_options_and_files_that_exist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    (command,) = [
        shlex.split(command)
        for _number, _machine, commands in steps()
        for command in commands
        if command.startswith("uv run leanpool-admit ")
    ]
    with pytest.raises(SystemExit):
        admit_main(["--help"], {})
    known = set(re.findall(r"--[a-z][a-z-]+", capsys.readouterr().out))

    assert {word for word in command if word.startswith("--")} <= known
    # In `deploy/pool`, where the step is done, the cases it names are the starter cases.
    cases = (PROJECT / "deploy" / "pool" / command[command.index("--cases") + 1]).resolve()
    assert cases == PROJECT / "examples" / "admission-cases"
    assert command[command.index("--tls-server-name") + 1] == "lean-a"


def test_nothing_the_recipe_runs_as_root_or_in_docker_is_one_of_this_projects_commands() -> None:
    not_followed = [
        command
        for _number, _machine, commands in steps()
        for command in commands
        if command.startswith(NOT_RUN) and not command.startswith("uv run leanpool-admit ")
    ]

    assert not_followed
    assert not any("leanpool-" in command for command in not_followed)
