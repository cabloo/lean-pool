"""An instance whose image is pulled rather than built (`INSTANCE_IMAGE`), and the site an install reads.

The REAL installer runs here on the fake machine of `stand_ins.py` (read its docstring first). Its Docker
records a pull and a tag, and refuses a pull when a test says so (STUB_PULL_FAILS). The site is the example
the repository ships, `deploy/install/site.example/`: one instance, `upstream`, from the upstream Kimina image.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from stand_ins import INSTALLERS, Machine, rewritten_installers, tools_run_from_source

pytestmark = [
    pytest.mark.skipif(os.geteuid() == 0, reason="the stand-ins play root for a user who is not"),
    pytest.mark.skipif(
        not tools_run_from_source(), reason="the lean-pool tools do not start with this Python"
    ),
]

EXAMPLE = INSTALLERS / "site.example"
REFERENCE = "projectnumina/kimina-lean-server:2.0.0"
IMAGE = "leanpool/kimina:kimina-2.0.0"
LEAN = "lean-server-upstream-kimina-1"
AGENT = "lean-server-upstream-agent-1"
INSTANCE = "/srv/kimina-upstream"


@pytest.fixture(scope="module")
def deploy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return rewritten_installers(tmp_path_factory.mktemp("deploy"))


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A copy of the example site, which a test may change."""
    return Path(shutil.copytree(EXAMPLE, tmp_path / "site"))


def shown(script: Path, *arguments: str, fake_root: Path | None = None) -> dict[str, str]:
    """What `--show-instance` prints, as a dictionary; it needs no root and changes nothing. Installers
    rewritten for a fake machine are given its root."""
    result = subprocess.run(
        ["bash", str(script), *arguments, "--show-instance"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "FAKE_ROOT": str(fake_root)} if fake_root else None,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def refused(script: Path, *arguments: str) -> str:
    """The message a refused `--show-instance` gives."""
    result = subprocess.run(
        ["bash", str(script), *arguments, "--show-instance"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    return result.stderr


def installed(machine: Machine, site: Path, **environment: str) -> subprocess.CompletedProcess[str]:
    result = machine.bash(
        "install.sh", "--site", str(site), "--instance", "upstream", "--no-door", **environment
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "INSTALLED AND VERIFIED" in result.stdout
    return result


def pulls(calls: list[str]) -> list[str]:
    """The calls that pull, tag or build a Lean server's image (the pool's own image is built either way)."""
    return [
        call
        for call in calls
        if call.startswith(("docker pull ", "docker tag "))
        or (call.startswith("docker build ") and "/kimina:" in call)
    ]


# --- the example site --------------------------------------------------------------------------------------------


def test_the_example_site_names_an_image_to_pull_and_no_recipe() -> None:
    said = shown(INSTALLERS / "install.sh", "--site", str(EXAMPLE), "--instance", "upstream")
    assert said["pull"] == REFERENCE
    assert said["image"] == IMAGE and said["dockerfile"] == ""
    assert said["container"] == LEAN and said["dir"] == INSTANCE and said["port"] == "18001"


def test_an_instance_names_an_image_to_pull_or_a_recipe_never_both(site: Path) -> None:
    with (site / "instances" / "upstream.env").open("a") as values:
        values.write("INSTANCE_DOCKERFILE=Dockerfile\n")
    message = refused(INSTALLERS / "install.sh", "--site", str(site), "--instance", "upstream")
    assert "cannot go with INSTANCE_DOCKERFILE or INSTANCE_BUILD_FILES" in message


@pytest.mark.parametrize("name", ["default", "main"])
def test_no_instance_takes_a_name_of_the_instance_with_no_name(site: Path, name: str) -> None:
    """`default` is the file of the instance with no name, and `main` the name of its control group."""
    shutil.copy(site / "instances" / "upstream.env", site / "instances" / f"{name}.env")
    message = refused(INSTALLERS / "install.sh", "--site", str(site), "--instance", name)
    assert "--instance takes 1-16 lower-case letters and digits" in message


@pytest.mark.parametrize(
    "reference",
    ["Kimina:latest", "projectnumina/kimina lean", "../kimina:1", "projectnumina/kimina;id", ""],
)
def test_an_image_reference_is_held_to_its_form(site: Path, reference: str) -> None:
    values = site / "instances" / "upstream.env"
    values.write_text(
        values.read_text().replace(f"INSTANCE_IMAGE={REFERENCE}", f"INSTANCE_IMAGE={reference}")
    )
    message = refused(INSTALLERS / "install.sh", "--site", str(site), "--instance", "upstream")
    assert "INSTANCE_IMAGE" in message or "INSTANCE_DOCKERFILE" in message


# --- an install that pulls -----------------------------------------------------------------------------------------


def test_the_image_is_pulled_once_and_tagged_and_nothing_is_built(
    tmp_path: Path, deploy: Path, site: Path
) -> None:
    machine = Machine(tmp_path / "host", deploy)
    result = installed(machine, site)
    assert f"Pulling {REFERENCE} as {IMAGE}: nothing is built" in result.stdout
    assert pulls(machine.calls) == [f"docker pull {REFERENCE}", f"docker tag {REFERENCE} {IMAGE}"]
    assert machine.path(f"{INSTANCE}/image.built").is_file()
    assert machine.container_id(LEAN) != ""
    # A second run finds the image under its tag, pulls nothing, and makes nothing again.
    lean, recreated, mark = machine.container_id(LEAN), machine.recreated(), machine.mark()
    again = installed(machine, site)
    assert (
        f"The image {IMAGE} is already pulled from {REFERENCE}: not pulling it again"
        in again.stdout
    )
    assert pulls(machine.calls_since(mark)) == []
    assert machine.container_id(LEAN) == lean and machine.recreated() == recreated


def test_another_reference_is_pulled_and_the_server_is_made_again_once(
    tmp_path: Path, deploy: Path, site: Path
) -> None:
    machine = Machine(tmp_path / "host", deploy)
    installed(machine, site)
    lean, recreated, mark = machine.container_id(LEAN), machine.recreated(), machine.mark()
    newer = "projectnumina/kimina-lean-server:2.0.1"
    values = site / "instances" / "upstream.env"
    values.write_text(values.read_text().replace(REFERENCE, newer))
    installed(machine, site)
    assert pulls(machine.calls_since(mark)) == [
        f"docker pull {newer}",
        f"docker tag {newer} {IMAGE}",
    ]
    # The usage agent too, once: the instance's values are in the installer's version it states.
    assert machine.recreated() == [*recreated, LEAN, AGENT] and machine.container_id(LEAN) not in (
        "",
        lean,
    )


def test_a_pull_that_fails_stops_the_install_with_no_server_started(
    tmp_path: Path, deploy: Path, site: Path
) -> None:
    machine = Machine(tmp_path / "host", deploy)
    result = machine.bash(
        "install.sh",
        "--site",
        str(site),
        "--instance",
        "upstream",
        "--no-door",
        STUB_PULL_FAILS="1",
    )
    assert result.returncode == 1
    assert f"could not pull {REFERENCE} (INSTANCE_IMAGE)" in result.stderr
    assert machine.container_id(LEAN) == ""
    assert not machine.path(f"{INSTANCE}/image.built").exists()


# --- which site ----------------------------------------------------------------------------------------------------


def test_without_site_the_installer_reads_the_site_beside_it_and_site_overrides_it(
    tmp_path: Path, deploy: Path
) -> None:
    """A join bundle carries its site as `site/` beside the installer; a host names its own with --site."""
    assert (deploy / "site" / "site.env").is_file()
    beside = shown(deploy / "install.sh", "--instance", "demo", fake_root=tmp_path)
    assert (
        beside["image"] == "acme/kimina:lean-demo-1"
        and beside["container"] == "lean-server-demo-kimina-1"
    )
    given = shown(
        deploy / "install.sh", "--site", str(EXAMPLE), "--instance", "upstream", fake_root=tmp_path
    )
    assert given["image"] == IMAGE and given["container"] == LEAN


def test_with_no_site_anywhere_the_installer_asks_for_one(tmp_path: Path) -> None:
    installers = Path(
        shutil.copytree(INSTALLERS, tmp_path / "install", ignore=shutil.ignore_patterns("site"))
    )
    assert "--site DIR is needed" in refused(installers / "install.sh", "--instance", "upstream")
    assert "site.env is missing" in refused(
        installers / "install.sh", "--site", str(tmp_path), "--instance", "upstream"
    )
