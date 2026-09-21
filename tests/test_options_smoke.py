import yaml

from agent_builder.auth import principal
from agent_builder.registry import Registry
from agent_builder.service import AgentService
from agent_builder.rbac import normalize_rbac_spec


def make_home(tmp_path):
    h = tmp_path / "h"
    (h / "profiles").mkdir(parents=True)
    (h / "skills" / "youtube-content").mkdir(parents=True)
    (h / "skills" / "youtube-content" / "SKILL.md").write_text("---\nname: youtube-content\n---\n")
    (h / "skills" / "systematic-debugging").mkdir(parents=True)
    (h / "skills" / "systematic-debugging" / "SKILL.md").write_text("---\nname: systematic-debugging\n---\n")
    (h / "config.yaml").write_text("mcp_servers:\n  grafana-prod:\n    url: http://grafana.local\n")
    return h


def rbac_source(tmp_path):
    src = tmp_path / "hermes-rbac-src"
    src.mkdir()
    (src / "plugin.yaml").write_text("name: hermes-rbac\nhooks:\n  - pre_gateway_dispatch\n  - pre_tool_call\n")
    (src / "__init__.py").write_text("def register(ctx): pass\n")
    (src / "roles.yaml.example").write_text("fail_closed: true\n")
    (src / "identities.yaml.example").write_text("persons: {}\n")
    return src


def service(tmp_path):
    h = make_home(tmp_path)
    s = AgentService(Registry(tmp_path / "db.sqlite"), h, allowed_models=["openai:gpt-5.5", "nous:Hermes-4"])
    a = principal("telegram", "1")
    s.bootstrap_admin(a)
    s.registry.grant_global(a.id, "agent-builder-user", a.id)
    return s, a, h


def test_catalog_exposes_choice_lists(tmp_path):
    s, _a, _h = service(tmp_path)
    c = s.catalogs()
    assert {m["value"] for m in c["models"]} >= {"openai:gpt-5.5", "nous:Hermes-4"}
    assert "youtube-content" in c["skills"]
    assert "grafana-prod" in c["mcp_servers"]
    assert c["autonomy_levels"] == ["human-approval", "agent", "autonomous"]
    assert {x["value"] for x in c["autonomy_choices"]} == {"human-approval", "agent", "autonomous"}
    assert all(x.get("description") for x in c["autonomy_choices"])
    assert c["shareable_platforms"] == ["telegram", "discord", "slack", "teams"]
    assert "discord:987654" in c["share_user_example"]
    assert {"web_search", "skill_view"} <= set(c["rbac_toolsets"])


def test_rbac_source_accepts_github_tree_url():
    spec = normalize_rbac_spec({"install": True, "source": "https://github.com/TTomas78/hermes-rbac/tree/master"})
    assert spec is not None
    assert spec["source"] == "https://github.com/TTomas78/hermes-rbac.git"


def test_create_agent_with_all_options_and_rbac_install(tmp_path):
    s, a, h = service(tmp_path)
    src = rbac_source(tmp_path)
    ag = s.create_agent(a, {
        "display_name": "Ops Helper",
        "description": "Keeps an eye on prod",
        "purpose": "answer observability questions",
        "instructions": "Stay read only.",
        "model": "openai:gpt-5.5",
        "skills": ["youtube-content"],
        "mcp_servers": ["grafana-prod"],
        "access_policy": "shared",
        "shared_users": ["discord:456", "slack:U123", "teams:user@example.com"],
        "risk_level": "human-approval",
        "rbac": {
            "install": True,
            "source": str(src),
            "role": "viewer",
            "users": ["telegram:2"],
            "bootstrap_admins": ["telegram:1"],
            "toolsets": ["web_search", "web_extract", "skill_view"],
            "skills": ["youtube-content"],
        },
    })
    profile = h / "profiles" / ag["profile_name"]
    cfg = yaml.safe_load((profile / "config.yaml").read_text())
    assert cfg["model"] == {"default": "gpt-5.5", "provider": "openai"}
    assert ag["risk_level"] == "human-approval"
    assert cfg["plugins"]["enabled"] == ["hermes-rbac"]
    roles = yaml.safe_load((profile / "plugins" / "hermes-rbac" / "roles.yaml").read_text())
    assert roles["bootstrap_admins"] == ["telegram:1"]
    assert roles["users"]["telegram:2"] == ["viewer"]
    assert roles["roles"]["viewer"]["skills"] == ["youtube-content"]
    assert (profile / "skills" / "youtube-content" / "SKILL.md").exists()
    acl = {x["principal_id"]: x["role"] for x in s.get_agent(a, ag["id"])["acl"]}
    assert acl["discord:456"] == "user"
    assert acl["slack:U123"] == "user"
    assert acl["teams:user@example.com"] == "user"


def test_rbac_full_config_writes_inheritance_default_and_identities(tmp_path):
    s, a, h = service(tmp_path)
    src = rbac_source(tmp_path)
    ag = s.create_agent(a, {
        "display_name": "Full RBAC Agent",
        "model": "openai:gpt-5.5",
        "rbac": {
            "install": True,
            "source": str(src),
            "role": "dev",
            "extends": ["viewer"],
            "deny": ["terminal"],
            "users": ["slack:U123"],
            "bootstrap_admins": ["telegram:1"],
            "toolsets": ["terminal", "web_search"],
            "skills": ["youtube-content"],
            "default_roles": ["guest"],
            "extra_roles": {
                "viewer": {"toolsets": ["web_search", "skill_view"], "skills": ["youtube-content"]},
                "guest": {"toolsets": [], "skills": []},
            },
            "identity_persons": {
                "george": {
                    "canonical": "telegram:1",
                    "identities": ["telegram:1", "slack:U123"],
                }
            },
        },
    })
    plugin = h / "profiles" / ag["profile_name"] / "plugins" / "hermes-rbac"
    roles = yaml.safe_load((plugin / "roles.yaml").read_text())
    assert roles["roles"]["dev"]["extends"] == ["viewer"]
    assert roles["roles"]["dev"]["deny"] == ["terminal"]
    assert roles["roles"]["viewer"]["toolsets"] == ["web_search", "skill_view"]
    assert roles["users"]["slack:U123"] == ["dev"]
    assert roles["users"]["*"] == ["guest"]
    identities = yaml.safe_load((plugin / "identities.yaml").read_text())
    assert identities["persons"]["george"]["canonical"] == "telegram:1"
    assert identities["persons"]["george"]["identities"] == ["telegram:1", "slack:U123"]


def test_rbac_full_dashboard_yaml_fields_are_accepted(tmp_path):
    s, a, h = service(tmp_path)
    src = rbac_source(tmp_path)
    ag = s.create_agent(a, {
        "display_name": "Dashboard RBAC Agent",
        "model": "openai:gpt-5.5",
        "rbac": {
            "install": True,
            "source": str(src),
            "role": "dev",
            "users": "slack:U123",
            "extends": "viewer",
            "default_roles": "guest",
            "extra_roles": "viewer:\n  toolsets: [web_search, skill_view]\n  skills: [youtube-content]\nguest:\n  toolsets: []\n  skills: []\n",
            "identity_persons": "george:\n  canonical: telegram:1\n  identities:\n    - telegram:1\n    - slack:U123\n",
        },
    })
    plugin = h / "profiles" / ag["profile_name"] / "plugins" / "hermes-rbac"
    roles = yaml.safe_load((plugin / "roles.yaml").read_text())
    assert roles["roles"]["dev"]["extends"] == ["viewer"]
    assert roles["users"]["*"] == ["guest"]
    identities = yaml.safe_load((plugin / "identities.yaml").read_text())
    assert identities["persons"]["george"]["identities"] == ["telegram:1", "slack:U123"]


def test_select_chat_binding_returns_agent(tmp_path):
    s, a, _h = service(tmp_path)
    ag = s.create_agent(a, {"display_name": "Chat Target", "model": "nous:Hermes-4"})
    selected = s.bind(a, "chat-1", ag["id"])
    assert selected["id"] == ag["id"]
    assert s.route(a, "chat-1", "hello") == ag["profile_name"]


def test_update_existing_agent_adds_rbac_and_changes_options(tmp_path):
    s, a, h = service(tmp_path)
    src = rbac_source(tmp_path)
    ag = s.create_agent(a, {"display_name": "Mutable Agent", "model": "openai:gpt-5.5", "skills": ["youtube-content"]})
    updated = s.update_agent(a, ag["id"], {
        "model": "nous:Hermes-4",
        "risk_level": "autonomous",
        "purpose": "new purpose",
        "instructions": "new instructions",
        "skills": ["systematic-debugging"],
        "mcp_servers": ["grafana-prod"],
        "shared_users": ["telegram:3", "discord:456"],
        "cron": {"jobs": [{"name": "daily", "schedule": "0 9 * * *", "prompt": "report"}]},
        "rbac": {
            "install": True,
            "source": str(src),
            "role": "dev",
            "users": ["telegram:3"],
            "toolsets": ["web_search", "skill_view", "read_file"],
            "skills": ["systematic-debugging"],
        },
    })
    assert updated["model"] == "nous:Hermes-4"
    assert updated["risk_level"] == "autonomous"
    profile = h / "profiles" / ag["profile_name"]
    cfg = yaml.safe_load((profile / "config.yaml").read_text())
    assert cfg["model"] == {"default": "Hermes-4", "provider": "nous"}
    assert cfg["mcp_servers"]["grafana-prod"]["url"] == "http://grafana.local"
    assert cfg["cron"]["jobs"][0]["name"] == "daily"
    assert cfg["plugins"]["enabled"] == ["hermes-rbac"]
    assert not (profile / "skills" / "youtube-content").exists()
    assert (profile / "skills" / "systematic-debugging" / "SKILL.md").exists()
    roles = yaml.safe_load((profile / "plugins" / "hermes-rbac" / "roles.yaml").read_text())
    assert roles["users"]["telegram:3"] == ["dev"]
    assert roles["roles"]["dev"]["toolsets"] == ["web_search", "skill_view", "read_file"]
    acl = {x["principal_id"]: x["role"] for x in s.get_agent(a, ag["id"])["acl"]}
    assert acl["telegram:3"] == "user"
    assert acl["discord:456"] == "user"
