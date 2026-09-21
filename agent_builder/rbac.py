from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

from .errors import ValidationError

RBAC_REPO = "https://github.com/TTomas78/hermes-rbac.git"
RBAC_PLUGIN = "hermes-rbac"

def _normalize_source(source: str | None) -> str:
    source = str(source or RBAC_REPO).strip()
    if source.startswith("https://github.com/") and "/tree/" in source:
        owner_repo = source[len("https://github.com/"):].split("/tree/", 1)[0]
        return f"https://github.com/{owner_repo}.git"
    if source.startswith("https://github.com/") and not source.endswith(".git") and source.count("/") >= 4:
        return source.rstrip("/") + ".git"
    return source


def _safe_list(values):
    if values is None:
        return []
    if isinstance(values, str):
        return [x.strip() for x in values.split(",") if x.strip()]
    return [str(x).strip() for x in values if str(x).strip()]


def _safe_mapping(value, label):
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = yaml.safe_load(value) or {}
        except yaml.YAMLError as e:
            raise ValidationError(f"{label} must be valid YAML/JSON") from e
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a mapping")
    return value


def _validate_role_name(name: str) -> str:
    name = str(name or "").strip()
    if not name or not all(c.isalnum() or c in "_-" for c in name):
        raise ValidationError("invalid RBAC role name")
    return name


def _validate_glob_item(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValidationError(f"invalid {label}")
    return value


def _validate_user_key(key: str) -> str:
    key = str(key or "").strip()
    if key == "*":
        return key
    if ":" not in key:
        raise ValidationError("RBAC user must look like platform:user_id")
    platform, user_id = key.split(":", 1)
    if platform not in {"discord", "telegram", "slack", "teams", "whatsapp", "local", "dashboard"} or not user_id:
        raise ValidationError("invalid RBAC user key")
    return key


def _normalize_extra_roles(value) -> dict:
    roles = {}
    for raw_name, raw_spec in _safe_mapping(value, "RBAC extra_roles").items():
        name = _validate_role_name(raw_name)
        if raw_spec is None:
            raw_spec = {}
        if not isinstance(raw_spec, dict):
            raise ValidationError("RBAC extra role specs must be mappings")
        roles[name] = {
            "extends": [_validate_role_name(x) for x in _safe_list(raw_spec.get("extends"))],
            "toolsets": [_validate_glob_item(x, "RBAC toolset") for x in _safe_list(raw_spec.get("toolsets"))],
            "skills": [_validate_glob_item(x, "RBAC skill") for x in _safe_list(raw_spec.get("skills"))],
            "deny": [_validate_glob_item(x, "RBAC deny") for x in _safe_list(raw_spec.get("deny"))],
            "bypass_sensitive_paths": bool(raw_spec.get("bypass_sensitive_paths")),
        }
    return roles


def _normalize_identity_persons(value) -> dict:
    persons = {}
    for raw_id, raw_spec in _safe_mapping(value, "RBAC identity_persons").items():
        person_id = _validate_role_name(raw_id)
        if not isinstance(raw_spec, dict):
            raise ValidationError("RBAC identity person specs must be mappings")
        canonical = _validate_user_key(str(raw_spec.get("canonical") or ""))
        identities = [_validate_user_key(x) for x in _safe_list(raw_spec.get("identities"))]
        if canonical not in identities:
            raise ValidationError("RBAC identity canonical must be listed in identities")
        persons[person_id] = {"canonical": canonical, "identities": identities}
    return persons


def _normalize_user_roles(value) -> dict:
    users = {}
    for raw_user, raw_roles in _safe_mapping(value, "RBAC user_roles").items():
        user = _validate_user_key(raw_user)
        roles = [_validate_role_name(x) for x in _safe_list(raw_roles)]
        if not roles:
            raise ValidationError("RBAC user_roles entries must include at least one role")
        users[user] = roles
    return users


def normalize_rbac_spec(spec: dict | None, *, selected_skills=()) -> dict | None:
    spec = spec or {}
    if not spec.get("install"):
        return None
    role = _validate_role_name(spec.get("role") or "viewer")
    users = [_validate_user_key(u) for u in _safe_list(spec.get("users"))]
    bootstrap_admins = [_validate_user_key(u) for u in _safe_list(spec.get("bootstrap_admins"))]
    toolsets = _safe_list(spec.get("toolsets")) or ["web_search", "web_extract", "skill_view"]
    skills = _safe_list(spec.get("skills"))
    if not skills:
        skills = list(selected_skills or [])
    if not users and bootstrap_admins:
        users = list(bootstrap_admins)
    return {
        "role": role,
        "users": users,
        "bootstrap_admins": bootstrap_admins,
        "toolsets": toolsets,
        "skills": skills,
        "extends": [_validate_role_name(x) for x in _safe_list(spec.get("extends"))],
        "deny": [_validate_glob_item(x, "RBAC deny") for x in _safe_list(spec.get("deny"))],
        "default_roles": [_validate_role_name(x) for x in _safe_list(spec.get("default_roles"))],
        "user_roles": _normalize_user_roles(spec.get("user_roles")),
        "extra_roles": _normalize_extra_roles(spec.get("extra_roles")),
        "identity_persons": _normalize_identity_persons(spec.get("identity_persons")),
        "fail_closed": bool(spec.get("fail_closed", True)),
        "bypass_sensitive_paths": bool(spec.get("bypass_sensitive_paths")),
        "source": _normalize_source(spec.get("source")),
    }


def roles_yaml(spec: dict) -> dict:
    role = spec["role"]
    users = {u: [role] for u in spec.get("users", [])}
    if spec.get("default_roles"):
        users["*"] = list(spec.get("default_roles", []))
    users.update(spec.get("user_roles") or {})
    roles = {
        "admin": {"toolsets": ["*"], "skills": ["*"], "bypass_sensitive_paths": True},
    }
    for name, role_spec in (spec.get("extra_roles") or {}).items():
        entry = {
            "toolsets": list(role_spec.get("toolsets", [])),
            "skills": list(role_spec.get("skills", [])),
            "bypass_sensitive_paths": bool(role_spec.get("bypass_sensitive_paths")),
        }
        if role_spec.get("extends"):
            entry["extends"] = list(role_spec["extends"])
        if role_spec.get("deny"):
            entry["deny"] = list(role_spec["deny"])
        roles[name] = entry
    selected = {
        "toolsets": list(spec.get("toolsets", [])),
        "skills": list(spec.get("skills", [])),
        "bypass_sensitive_paths": bool(spec.get("bypass_sensitive_paths")),
    }
    if spec.get("extends"):
        selected["extends"] = list(spec["extends"])
    if spec.get("deny"):
        selected["deny"] = list(spec["deny"])
    roles[role] = selected
    return {
        "fail_closed": bool(spec.get("fail_closed", True)),
        "bootstrap_admins": list(spec.get("bootstrap_admins", [])),
        "roles": roles,
        "users": users,
    }


def identities_yaml(spec: dict) -> dict:
    return {"persons": dict(spec.get("identity_persons") or {})}


def _copy_or_clone_plugin(source: str, dest: Path):
    if dest.exists():
        shutil.rmtree(dest)
    source_path = Path(source).expanduser()
    if source_path.exists():
        shutil.copytree(source_path, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        return
    subprocess.run(["git", "clone", "--depth=1", source, str(dest)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _enable_plugin(profile_home: Path):
    cfg_path = profile_home / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    if not isinstance(cfg, dict):
        cfg = {}
    plugins = cfg.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
        cfg["plugins"] = plugins
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        enabled = []
    if RBAC_PLUGIN not in enabled:
        enabled.append(RBAC_PLUGIN)
    plugins["enabled"] = enabled
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))


def install_rbac(profile_home: Path, spec: dict) -> dict:
    profile_home = Path(profile_home)
    plugins_dir = profile_home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    dest = plugins_dir / RBAC_PLUGIN
    _copy_or_clone_plugin(spec.get("source") or RBAC_REPO, dest)
    (dest / "roles.yaml").write_text(yaml.safe_dump(roles_yaml(spec), sort_keys=False, allow_unicode=True))
    (dest / "identities.yaml").write_text(yaml.safe_dump(identities_yaml(spec), sort_keys=False, allow_unicode=True))
    _enable_plugin(profile_home)
    return {"plugin_dir": str(dest), "roles_path": str(dest / "roles.yaml")}
