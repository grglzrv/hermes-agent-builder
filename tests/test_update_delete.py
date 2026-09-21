import json

import yaml

from agent_builder.auth import principal
from agent_builder.cli import main
from agent_builder.registry import Registry
from agent_builder.service import AgentService


def make_home(tmp_path):
    h = tmp_path / "h"
    (h / "profiles").mkdir(parents=True)
    (h / "skills" / "systematic-debugging").mkdir(parents=True)
    (h / "skills" / "systematic-debugging" / "SKILL.md").write_text("---\nname: systematic-debugging\n---\n")
    (h / "config.yaml").write_text("mcp_servers:\n  grafana-prod:\n    url: http://grafana.local\n")
    return h


def seed(tmp_path):
    h = make_home(tmp_path)
    svc = AgentService(Registry(h / "plugin-data" / "agent-builder" / "registry.db"), h, allowed_models=["m"])
    actor = principal("telegram", "1")
    svc.bootstrap_admin(actor)
    svc.registry.grant_global(actor.id, "agent-builder-user", actor.id)
    ag = svc.create_agent(actor, {"display_name": "CLI Agent", "model": "m"})
    return h, ag


def actor_args(h):
    return ["--home", str(h), "--actor-platform", "telegram", "--actor-user-id", "1"]


def test_cli_update_and_delete_existing_agent(tmp_path, capsys):
    h, ag = seed(tmp_path)

    main([
        "update", *actor_args(h), ag["profile_name"],
        "--purpose", "updated by cli",
        "--skills", "systematic-debugging",
        "--mcp-servers", "grafana-prod",
        "--access-policy", "shared",
        "--shared-users", "discord:456, slack:U123",
        "--cron-json", '{"jobs":[{"name":"daily","schedule":"0 9 * * *","prompt":"report"}]}',
    ])
    out = json.loads(capsys.readouterr().out)
    assert out["updated"] == ag["id"]

    svc = AgentService(Registry(h / "plugin-data" / "agent-builder" / "registry.db"), h, allowed_models=["m"])
    owner = principal("telegram", "1")
    updated = svc.get_agent(owner, ag["id"])
    assert updated["access_policy"] == "shared"
    acl = {x["principal_id"]: x["role"] for x in updated["acl"]}
    assert acl["discord:456"] == "user"
    assert acl["slack:U123"] == "user"
    cfg = yaml.safe_load((h / "profiles" / ag["profile_name"] / "config.yaml").read_text())
    assert cfg["cron"]["jobs"][0]["name"] == "daily"
    assert cfg["mcp_servers"]["grafana-prod"]["url"] == "http://grafana.local"
    assert (h / "profiles" / ag["profile_name"] / "skills" / "systematic-debugging").exists()

    main(["delete", *actor_args(h), ag["profile_name"]])
    deleted = json.loads(capsys.readouterr().out)
    assert deleted["deleted"] == ag["id"]
    assert svc.registry.get_agent(ag["id"], include_deleted=True)["status"] == "deleted"
    assert not (h / "profiles" / ag["profile_name"]).exists()


def test_access_policy_is_effective_from_acl_and_private_clears_shares(tmp_path):
    h, ag = seed(tmp_path)
    svc = AgentService(Registry(h / "plugin-data" / "agent-builder" / "registry.db"), h, allowed_models=["m"])
    owner = principal("telegram", "1")

    # A stale stored shared flag without non-owner ACL entries should render as private.
    svc.registry.update_agent(ag["id"], {"access_policy": "shared"})
    current = svc.get_agent(owner, ag["id"])
    assert current["access_policy"] == "private"
    assert current["shared_users"] == []

    # Shared policy becomes shared only when non-owner user ACL entries exist.
    svc.update_agent(owner, ag["id"], {"access_policy": "shared", "shared_users": ["slack:U123"]})
    shared = svc.get_agent(owner, ag["id"])
    assert shared["access_policy"] == "shared"
    assert shared["shared_users"] == ["slack:U123"]

    # Switching back to private with no shared_users field revokes old shares.
    svc.update_agent(owner, ag["id"], {"access_policy": "private"})
    private = svc.get_agent(owner, ag["id"])
    assert private["access_policy"] == "private"
    assert private["shared_users"] == []
    assert {x["role"] for x in private["acl"]} == {"owner"}
