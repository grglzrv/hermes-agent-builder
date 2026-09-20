from __future__ import annotations

from pathlib import Path
import os
import yaml

from .errors import ValidationError


def list_skills(home: Path):
    out = []
    for p in (home / "skills").rglob("SKILL.md") if (home / "skills").exists() else []:
        if any(x.startswith(".") for x in p.relative_to(home / "skills").parts):
            continue
        out.append(str(p.parent.relative_to(home / "skills")))
    return sorted(set(out))


def list_mcps(home: Path):
    try:
        cfg = yaml.safe_load((home / "config.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    servers = cfg.get("mcp_servers") or cfg.get("mcp", {}).get("servers") or {}
    return sorted(k for k, v in servers.items() if isinstance(k, str) and isinstance(v, dict))


def copy_catalog_mcp(source_home: Path, names):
    cfg = yaml.safe_load((source_home / "config.yaml").read_text()) or {}
    servers = cfg.get("mcp_servers") or cfg.get("mcp", {}).get("servers") or {}
    unknown = set(names) - set(servers)
    if unknown:
        raise ValidationError("MCP not in administrator catalog: " + ", ".join(sorted(unknown)))
    return {n: servers[n] for n in names}


def _read_config(home: Path) -> dict:
    try:
        return yaml.safe_load((home / "config.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _provider_choice(provider: str, model: str, label: str = "", authenticated=True) -> dict:
    value = f"{provider}:{model}" if provider else model
    return {
        "value": value,
        "provider": provider,
        "model": model,
        "label": label or value,
        "authenticated": bool(authenticated),
    }


def list_models(home: Path, *, include_unauthenticated: bool = False, max_per_provider: int = 200):
    """Return selectable provider:model choices for providers this profile can use.

    Uses Hermes' live/cached model catalog when available, falling back to the
    current profile config. Values are provider-qualified so a generated profile
    can persist both `model.provider` and `model.default` without guessing.
    """
    choices = []
    seen = set()
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        try:
            from hermes_cli.models import cached_provider_model_ids as _cached_provider_model_ids
            from hermes_cli.models import list_available_providers as _list_available_providers
            from hermes_cli.models import provider_label as _provider_label
        except Exception:
            _cached_provider_model_ids = _list_available_providers = _provider_label = None
        if _list_available_providers is not None and _cached_provider_model_ids is not None:
            for provider in _list_available_providers():
                pid = str(provider.get("id") or "").strip()
                if not pid:
                    continue
                authed = bool(provider.get("authenticated"))
                if not authed and not include_unauthenticated:
                    continue
                try:
                    models = _cached_provider_model_ids(pid)[:max_per_provider]
                except Exception:
                    models = []
                for model in models:
                    key = (pid, model)
                    if key in seen:
                        continue
                    seen.add(key)
                    label = f"{provider.get('label') or (_provider_label(pid) if _provider_label else pid)} — {model}"
                    choices.append(_provider_choice(pid, model, label, authed))
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home

    cfg = _read_config(home)
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    current_model = str(model_cfg.get("default") or "").strip()
    current_provider = str(model_cfg.get("provider") or "").strip()
    if current_model:
        provider = current_provider or "configured"
        key = (provider, current_model)
        if key not in seen:
            choices.insert(0, _provider_choice(provider, current_model, f"Current — {provider}:{current_model}", True))
    return choices


def parse_model_choice(value: str, valid_choices=()):
    raw = str(value or "").strip()
    if not raw:
        raise ValidationError("model is required")
    allowed = {c["value"] if isinstance(c, dict) else str(c) for c in (valid_choices or [])}
    allowed_providers = {str(c.get("provider") or "").strip() for c in (valid_choices or []) if isinstance(c, dict)}
    allowed_providers.discard("")
    if ":" in raw:
        provider, model = raw.split(":", 1)
        provider = provider.strip()
        model = model.strip()
        if provider and model:
            if allowed and raw not in allowed and provider not in allowed_providers:
                raise ValidationError("unsupported model provider")
            return provider, model, raw
    if allowed and raw not in allowed:
        raise ValidationError("unsupported model")
    return "", raw, raw


def list_rbac_toolsets():
    return [
        "web_search", "web_extract", "skill_view", "read_file", "search_files",
        "terminal", "execute_code", "write_file", "patch", "browser_exec", "mcp__*",
    ]
