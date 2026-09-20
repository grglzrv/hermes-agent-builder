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


def _validate_role_name(name: str) -> str:
    name = str(name or "").strip()
    if not name or not all(c.isalnum() or c in "_-" for c in name):
        raise ValidationError("invalid RBAC role name")
    return name


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
        "bypass_sensitive_paths": bool(spec.get("bypass_sensitive_paths")),
        "source": _normalize_source(spec.get("source")),
    }


def roles_yaml(spec: dict) -> dict:
    role = spec["role"]
    users = {u: [role] for u in spec.get("users", [])}
    return {
        "fail_closed": True,
        "bootstrap_admins": list(spec.get("bootstrap_admins", [])),
        "roles": {
            "admin": {"toolsets": ["*"], "skills": ["*"], "bypass_sensitive_paths": True},
            role: {
                "toolsets": list(spec.get("toolsets", [])),
                "skills": list(spec.get("skills", [])),
                "bypass_sensitive_paths": bool(spec.get("bypass_sensitive_paths")),
            },
        },
        "users": users,
    }


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
    identities = dest / "identities.yaml"
    if not identities.exists():
        identities.write_text("persons: {}\n")
    _enable_plugin(profile_home)
    return {"plugin_dir": str(dest), "roles_path": str(dest / "roles.yaml")}
