"""How Docker Compose reads a project from several files, as far as this project's files need it.

The tests cannot run Docker; CI's own job has Docker read every combination of the files. This
model lets the tests ask what a project resolves to, and the fake machine of
`tests/install/stand_ins.py` decide what each `docker compose` call makes:

* each file is read as YAML with Compose's two merge tags, `!reset` (drop what the files before
  set) and `!override` (replace it instead of merging);
* each file's values are interpolated from the environment before the files are merged
  (`${NAME}`, `${NAME:-default}`, `${NAME-default}`, `${NAME:?message}`, `${NAME?message}`, and
  `$$` for a dollar sign);
* the files are merged in order: mappings key by key, and lists as Compose merges them (most are
  joined without repeats, `volumes` by the path inside the container, a command or a
  healthcheck's test replaced);
* a service that names no container gets Compose's name, PROJECT-SERVICE-1, and the network a
  service joins without naming one is the project's own, PROJECT_default.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml


class Reset:
    """A value tagged `!reset`: whatever the files before set here is dropped."""


class Override:
    """A value tagged `!override`: it replaces what the files before set, unmerged."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _Loader(yaml.SafeLoader):
    pass


def _reset(_loader: yaml.SafeLoader, _node: yaml.Node) -> Reset:
    return Reset()


def _override(loader: yaml.SafeLoader, node: yaml.Node) -> Override:
    if isinstance(node, yaml.MappingNode):
        return Override(loader.construct_mapping(node, deep=True))
    if isinstance(node, yaml.SequenceNode):
        return Override(loader.construct_sequence(node, deep=True))
    return Override(loader.construct_scalar(node))  # type: ignore[arg-type]


_Loader.add_constructor("!reset", _reset)
_Loader.add_constructor("!override", _override)


class MissingSetting(ValueError):
    """A `${NAME:?message}` whose NAME is not set (or empty): Compose refuses the project."""


def read(path: Path) -> dict[str, Any]:
    """One compose file, with its merge tags kept as `Reset` and `Override`."""
    loaded = yaml.load(path.read_text(), Loader=_Loader)  # a SafeLoader with two more tags
    return loaded if isinstance(loaded, dict) else {}


_VARIABLE = re.compile(
    r"\$(?:(?P<dollar>\$)|\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<how>:?[-?])(?P<word>[^}]*))?\}"
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def interpolate(text: str, env: Mapping[str, str]) -> str:
    """One string, with every variable in it replaced as Compose replaces it."""

    def value(match: re.Match[str]) -> str:
        if match.group("dollar"):
            return "$"
        name = match.group("name") or match.group("bare")
        how, word = match.group("how"), match.group("word") or ""
        given = env.get(name)
        unset_or_empty = given is None or given == ""
        if how == ":-":
            return word if unset_or_empty else str(given)
        if how == "-":
            return word if given is None else str(given)
        if (how == ":?" and unset_or_empty) or (how == "?" and given is None):
            raise MissingSetting(f"required variable {name} is missing a value: {word}")
        return "" if given is None else str(given)

    return _VARIABLE.sub(value, text)


def _interpolated(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return interpolate(value, env)
    if isinstance(value, dict):
        return {key: _interpolated(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolated(item, env) for item in value]
    if isinstance(value, Override):
        return Override(_interpolated(value.value, env))
    return value


# Lists Compose replaces rather than joins; `volumes` is joined by the path inside the container.
_REPLACED = {"command", "entrypoint", "test"}


def _volume_target(volume: Any) -> str:
    if isinstance(volume, dict):
        return str(volume.get("target", ""))
    parts = str(volume).split(":")
    return parts[1] if len(parts) > 1 else parts[0]


def _merged_list(key: str, base: list[Any], extra: list[Any]) -> list[Any]:
    if key in _REPLACED:
        return list(extra)
    if key == "volumes":
        kept = {_volume_target(volume): volume for volume in base}
        kept.update({_volume_target(volume): volume for volume in extra})
        return list(kept.values())
    joined = list(base)
    joined.extend(item for item in extra if item not in joined)
    return joined


def merge(base: Any, extra: Any, key: str = "") -> Any:
    """What Compose makes of EXTRA read after BASE (both already interpolated)."""
    if isinstance(extra, Override):
        return extra.value
    if isinstance(base, dict) and isinstance(extra, dict):
        merged = dict(base)
        for name, value in extra.items():
            if isinstance(value, Reset):
                merged.pop(name, None)
            elif name in merged:
                merged[name] = merge(merged[name], value, name)
            else:
                merged[name] = _without_tags(value)
        return merged
    if isinstance(base, list) and isinstance(extra, list):
        return _merged_list(key, base, extra)
    return _without_tags(extra)


def _without_tags(value: Any) -> Any:
    if isinstance(value, Override):
        return _without_tags(value.value)
    if isinstance(value, dict):
        return {
            key: _without_tags(item) for key, item in value.items() if not isinstance(item, Reset)
        }
    if isinstance(value, list):
        return [_without_tags(item) for item in value]
    return value


def resolve(files: Iterable[Path], env: Mapping[str, str]) -> dict[str, Any]:
    """The project the files make together: each file interpolated, then merged in order."""
    project: dict[str, Any] = {}
    for path in files:
        project = merge(project, _interpolated(read(path), env))
    return project


def container_name(project_name: str, service_name: str, service: Mapping[str, Any]) -> str:
    """The container a service makes: the one it names, or Compose's PROJECT-SERVICE-1."""
    named = service.get("container_name")
    return str(named) if named else f"{project_name}-{service_name}-1"


def network_names(project_name: str, project: Mapping[str, Any]) -> dict[str, str]:
    """Each network the project declares or its services join, by its name in the file: the name
    Docker knows it by (its `name:`, else PROJECT_NETWORK). External networks are left out: the
    project does not make them."""
    declared = dict(project.get("networks") or {})
    joined = (
        {"default"}
        if any(
            "network_mode" not in service and not service.get("networks")
            for service in (project.get("services") or {}).values()
        )
        else set()
    )
    for service in (project.get("services") or {}).values():
        networks = service.get("networks") or []
        joined.update(networks if isinstance(networks, list) else networks.keys())
    names = {}
    for network in sorted(joined | set(declared)):
        settings = declared.get(network) or {}
        if settings.get("external"):
            continue
        names[network] = str(settings.get("name") or f"{project_name}_{network}")
    return names
