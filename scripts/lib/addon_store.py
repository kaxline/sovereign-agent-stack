#!/usr/bin/env python3
"""The add-on data directory: <data_root>/addons/<short-id>/.

    config/settings.env   ADDON_<SHORT>_<NAME>=value, one per line (shlex-quoted;
                          lists as JSON; bools as 1/0), secrets excluded
    instances/<slug>.json non-secret instance values
    secrets/<name>        a top-level secret setting
    secrets/<slug>.<name> an instance's secret setting
    state.json            what the lifecycle recorded, e.g. {"init": {"version", "at"}}

Standard library only. Directories are 0700 and secret files 0600, set with
explicit chmods. config/ is created with the add-on directory, so a service
that mounts it finds a directory before the first configure. Every file is written to a temp name in its own directory and
renamed. No exception message, and nothing listing() returns, holds a secret
value; messages name the setting only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import unicodedata
from pathlib import Path

import addon_manifest as am

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
SETTING_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
SHORT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")


class InputError(ValueError):
    """A value, name or slug that can't be used. `.setting` is a name only."""

    def __init__(self, message: str, setting: str = ""):
        super().__init__(message)
        self.setting = setting


class Missing(RuntimeError):
    """Required settings have no value. `.setting` is the first one's name."""

    def __init__(self, message: str, setting: str = ""):
        super().__init__(message)
        self.setting = setting


# --- paths -------------------------------------------------------------------

def addon_dir(data_root: Path, manifest: dict) -> Path:
    try:
        sid = am.short_id(manifest["id"])
    except Exception:  # noqa: BLE001
        raise InputError("the manifest id has no usable short id", "id") from None
    if not (isinstance(sid, str) and SHORT_RE.match(sid)):
        raise InputError("the manifest id has no usable short id", "id")
    base = (Path(data_root) / "addons").resolve()
    target = (base / sid).resolve()
    if target == base or base not in target.parents:
        raise InputError("the add-on directory is outside data/addons", "id")
    return target


def _slug(slug) -> str:
    if not (isinstance(slug, str) and SLUG_RE.match(slug)):
        raise InputError("slug must be lowercase letters, digits and -", "slug")
    return slug


# The public name; apply_instance and remove_instance take a `slug` argument
# that shadows it, so they call _slug.
slug = _slug


def _mkdir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _mkdir_addon(d: Path) -> None:
    _mkdir(d)
    _mkdir(d / "config")


def _settings_env(d: Path) -> Path:
    return d / "config" / "settings.env"


def _write(path: Path, data: str, mode: int) -> None:
    _mkdir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- state -------------------------------------------------------------------

class StateError(RuntimeError):
    """state.json exists but isn't a JSON object. Not repaired, so a write
    can't drop the keys it holds."""


def state(manifest: dict, data_root: Path) -> dict:
    """data/addons/<id>/state.json as a dict; {} when the file is missing.
    Raises StateError when it can't be read or isn't a JSON object."""
    path = addon_dir(data_root, manifest) / "state.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        raise StateError("state.json can't be read") from None
    try:
        v = json.loads(text)
    except ValueError:
        v = None
    if not isinstance(v, dict):
        raise StateError("state.json is not a JSON object; fix or remove it")
    return v


def set_state(manifest: dict, data_root: Path, key: str, value) -> None:
    """Set one top-level key of state.json (mode 0600, written through a temp
    file and os.replace)."""
    d = addon_dir(data_root, manifest)
    current = state(manifest, data_root)
    current[key] = value
    _mkdir_addon(d)
    _write(d / "state.json", json.dumps(current, indent=2) + "\n", 0o600)


# --- schema ------------------------------------------------------------------

def _schema(manifest: dict) -> dict:
    s = manifest.get("settings")
    return s if isinstance(s, dict) else {}


def _inst_schema(manifest: dict) -> dict:
    i = manifest.get("instances")
    s = i.get("settings") if isinstance(i, dict) else None
    return s if isinstance(s, dict) else {}


def _required(spec: dict) -> bool:
    return "default" not in spec and spec.get("optional") is not True


def _check_value(name: str, spec: dict, value):
    """Return `value` if it fits the setting's type, else raise InputError
    naming the setting (never the value)."""
    t = spec.get("type")
    if t == "choice":
        ok = isinstance(value, str) and value in (spec.get("options") or [])
    elif t in ("int", "port", "bool", "list", "date"):
        ok = am._default_fits(t, value)
    else:
        ok = isinstance(value, str)
    if not ok:
        raise InputError(f"{name}: the value does not fit type {t}", name)
    strings = [value] if isinstance(value, str) else (value if isinstance(value, list) else [])
    for item in strings:
        try:
            item.encode("utf-8")
        except UnicodeEncodeError:
            raise InputError(f"{name}: the value is not valid UTF-8 text", name) from None
    return value


def _has_line_break(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in text)


def _validated(schema: dict, values) -> dict:
    if not isinstance(values, dict):
        raise InputError("values must be an object", "")
    out = {}
    for name, value in values.items():
        if not (isinstance(name, str) and SETTING_NAME_RE.match(name)) or name not in schema:
            raise InputError("unknown setting", name if isinstance(name, str) and
                             SETTING_NAME_RE.match(name) else "")
        out[name] = _check_value(name, schema[name], value)
    return out


# --- settings.env ------------------------------------------------------------

def _env_key(manifest: dict, name: str) -> str:
    return f"ADDON_{am.short_id(manifest['id'])}_{name}".upper().replace("-", "_")


def _env_text(spec: dict, value) -> str:
    t = spec["type"]
    if t == "bool":
        return "1" if value else "0"
    if t == "list":
        return json.dumps(value)
    if t in ("int", "port"):
        return str(value)
    return value


def _env_value(spec: dict, text: str):
    t = spec["type"]
    try:
        if t == "bool":
            return am.truthy(text)
        if t == "list":
            v = json.loads(text)
            return v if am._default_fits("list", v) else None
        if t in ("int", "port"):
            return int(text)
    except ValueError:
        return None
    return text


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError:
        return out
    for line in lines:
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) == 1 and "=" in parts[0]:
            key, _, val = parts[0].partition("=")
            out[key] = val
    return out


def _write_env(path: Path, env: dict[str, str]) -> None:
    _write(path, "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()), 0o644)


def _stored(manifest: dict, d: Path) -> dict:
    """Non-secret top-level values in settings.env, typed."""
    env = _read_env(_settings_env(d))
    out = {}
    for name, spec in _schema(manifest).items():
        if spec.get("type") == "secret" or not isinstance(spec, dict):
            continue
        key = _env_key(manifest, name)
        if key in env:
            v = _env_value(spec, env[key])
            if v is not None:
                out[name] = v
    return out


def _instance_values(d: Path, slug: str) -> dict:
    try:
        v = json.loads((d / "instances" / f"{slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _secret_set(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _slugs(d: Path) -> list[str]:
    try:
        names = [p.stem for p in (d / "instances").glob("*.json")]
    except OSError:
        return []
    return sorted(n for n in names if SLUG_RE.match(n))


# --- writes ------------------------------------------------------------------

def apply_settings(manifest: dict, data_root: Path, values: dict) -> dict[str, str]:
    """Validate and store top-level values. Returns {ENV: "1"|"0"} for bool
    settings with a `flag` that were given; the caller writes them to .env."""
    schema = _schema(manifest)
    clean = _validated(schema, values)
    for name, value in clean.items():
        if schema[name]["type"] != "secret" and isinstance(value, str) and _has_line_break(value):
            raise InputError(f"{name}: control characters and line breaks are not allowed", name)
    d = addon_dir(data_root, manifest)
    env = _read_env(_settings_env(d))
    flags: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for name, value in clean.items():
        spec = schema[name]
        if spec["type"] == "secret":
            secrets[name] = value
            continue
        env[_env_key(manifest, name)] = _env_text(spec, value)
        if spec["type"] == "bool" and spec.get("flag"):
            flags[spec["flag"]] = "1" if value else "0"
    _mkdir_addon(d)
    if env:
        _write_env(_settings_env(d), env)
    for name, value in secrets.items():
        _write(d / "secrets" / name, value, 0o600)
    return flags


def _validated_instance(schema: dict, values) -> dict:
    """_validated, plus: a secret value may not hold CR or LF. A hook's output
    is redacted line by line, which can't match a value split across lines."""
    clean = _validated(schema, values)
    for name, value in clean.items():
        if schema[name]["type"] == "secret" and isinstance(value, str) and ("\n" in value or "\r" in value):
            raise InputError(f"{name}: a secret may not contain a line break", name)
    return clean


def check_instance(manifest: dict, values: dict) -> None:
    """Raise InputError unless `values` is a complete new instance: every
    setting known and well-typed, and every required one given. Writes
    nothing; `connect` calls it before the hook sees the values."""
    schema = _inst_schema(manifest)
    clean = _validated_instance(schema, values)
    for name, spec in schema.items():
        if _required(spec) and name not in clean:
            raise InputError(f"{name}: required for a new instance", name)


def apply_instance(manifest: dict, data_root: Path, slug: str, values: dict) -> None:
    slug = _slug(slug)
    schema = _inst_schema(manifest)
    clean = _validated_instance(schema, values)
    d = addon_dir(data_root, manifest)
    plain = {k: v for k, v in clean.items() if schema[k]["type"] != "secret"}
    _mkdir_addon(d)
    merged = _instance_values(d, slug)
    merged.update(plain)
    _write(d / "instances" / f"{slug}.json", json.dumps(merged, indent=2) + "\n", 0o644)
    for name, value in clean.items():
        if schema[name]["type"] == "secret":
            _write(d / "secrets" / f"{slug}.{name}", value, 0o600)


def remove_instance(manifest: dict, data_root: Path, slug: str) -> None:
    slug = _slug(slug)
    d = addon_dir(data_root, manifest)
    try:
        (d / "instances" / f"{slug}.json").unlink()
    except FileNotFoundError:
        pass
    try:
        names = os.listdir(d / "secrets")
    except FileNotFoundError:
        return
    for n in names:
        if n.startswith(slug + "."):
            (d / "secrets" / n).unlink()


# --- reads -------------------------------------------------------------------

def missing(manifest: dict, data_root: Path) -> list[str]:
    d = addon_dir(data_root, manifest)
    stored = _stored(manifest, d)
    out = []
    for name, spec in _schema(manifest).items():
        if not _required(spec):
            continue
        if spec["type"] == "secret":
            if not _secret_set(d / "secrets" / name):
                out.append(name)
        elif name not in stored:
            out.append(name)
    if isinstance(manifest.get("instances"), dict):
        slugs = _slugs(d)
        if not slugs:
            # Prefixed so it can't be read as a top-level setting's name.
            out.append(f"instances:{manifest['instances'].get('name') or 'instance'}")
        for slug in slugs:
            vals = _instance_values(d, slug)
            for name, spec in _inst_schema(manifest).items():
                if not _required(spec):
                    continue
                if spec["type"] == "secret":
                    if not _secret_set(d / "secrets" / f"{slug}.{name}"):
                        out.append(f"{slug}.{name}")
                elif name not in vals:
                    out.append(f"{slug}.{name}")
    return out


def _view(schema: dict, vals: dict, secret_path) -> dict:
    out = {}
    for name, spec in schema.items():
        if spec["type"] == "secret":
            out[name] = {"set": _secret_set(secret_path(name))}
        elif name in vals:
            out[name] = vals[name]
        elif "default" in spec:
            out[name] = spec["default"]
    return out


def _schema_view(schema: dict) -> dict:
    """A copy of a settings schema for listing(). A secret's default is
    dropped (validate() refuses one anyway), so no secret value can reach it."""
    out = {}
    for name, spec in schema.items():
        spec = json.loads(json.dumps(spec))
        if isinstance(spec, dict) and spec.get("type") == "secret":
            spec.pop("default", None)
        out[name] = spec
    return out


def listing(manifest: dict, data_root: Path, environ) -> dict:
    d = addon_dir(data_root, manifest)
    builtin = manifest.get("builtin") if isinstance(manifest.get("builtin"), dict) else {}
    host = manifest.get("host") if isinstance(manifest.get("host"), dict) else {}
    instances = manifest.get("instances")
    out = {
        "id": manifest["id"],
        "shortId": am.short_id(manifest["id"]),
        "title": manifest.get("title"),
        "tagline": manifest.get("tagline"),
        "permissions": manifest.get("permissions"),
        "platform": host.get("platform") or "any",
        "enabled": am.truthy(environ.get(builtin.get("flag"))) if builtin.get("flag") else False,
        "settings": _view(_schema(manifest), _stored(manifest, d), lambda n: d / "secrets" / n),
        "instances": {},
        "missing": missing(manifest, data_root),
        "data": manifest.get("data") or {},
        "backup": manifest.get("backup") or {},
        "hooks": sorted([n for n in (*am.HOOK_NAMES, *am.INSTANCE_HOOKS) if am.hook(manifest, n)]),
        "errors": list(instances.get("errors", [])) if isinstance(instances, dict) else [],
        "schema": {
            "settings": _schema_view(_schema(manifest)),
            "instances": {"name": manifest["instances"].get("name"),
                          "settings": _schema_view(_inst_schema(manifest))}
            if isinstance(manifest.get("instances"), dict) else None,
        },
    }
    for slug in _slugs(d):
        out["instances"][slug] = _view(_inst_schema(manifest), _instance_values(d, slug),
                                       lambda n, s=slug: d / "secrets" / f"{s}.{n}")
    return out
