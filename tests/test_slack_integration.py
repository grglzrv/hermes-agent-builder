import asyncio

from agent_builder.auth import principal
from agent_builder.registry import Registry
from agent_builder.service import AgentService
from integrations.slack import _agent_builder_command, _parse_key_values


def make_service(tmp_path):
    h = tmp_path / "h"
    (h / "profiles").mkdir(parents=True)
    (h / "skills").mkdir()
    (h / "config.yaml").write_text("mcp_servers: {}\n")
    s = AgentService(Registry(tmp_path / "db.sqlite"), h, allowed_models=["m"])
    actor = principal("slack", "U1", "T1")
    s.bootstrap_admin(actor)
    s.registry.grant_global(actor.id, "agent-builder-user", actor.id)
    return s


class Recorder:
    def __init__(self):
        self.acked = False
        self.responses = []

    async def ack(self, *args, **kwargs):
        self.acked = True

    async def respond(self, text, **kwargs):
        self.responses.append((text, kwargs))


def command(text, user="U1", team="T1", channel="C1"):
    return {
        "text": text,
        "user_id": user,
        "user_name": "tester",
        "team_id": team,
        "channel_id": channel,
    }


def test_parse_key_values_keeps_quoted_values_and_aliases():
    payload = _parse_key_values([
        'name=Ops helper',
        'model=m',
        'autonomy=agent',
        'access=shared',
        'skills=a,b',
        'shared_users=slack:U2,telegram:3',
    ])
    assert payload["display_name"] == "Ops helper"
    assert payload["risk_level"] == "agent"
    assert payload["access_policy"] == "shared"
    assert payload["skills"] == ["a", "b"]
    assert payload["shared_users"] == ["slack:U2", "telegram:3"]


def test_slack_create_and_list_use_scoped_slack_principal(tmp_path):
    service = make_service(tmp_path)
    rec = Recorder()
    asyncio.run(_agent_builder_command(
        rec.ack,
        rec.respond,
        command('create name="Ops helper" model=m purpose="Answer scoped questions"'),
        service,
    ))
    assert rec.acked is True
    assert "Agent created" in rec.responses[-1][0]
    rows = service.list_agents(principal("slack", "U1", "T1"))
    assert rows[0]["owner_id"] == "slack:T1:U1"

    rec2 = Recorder()
    asyncio.run(_agent_builder_command(rec2.ack, rec2.respond, command('list'), service))
    assert "ssa-ops-helper" in rec2.responses[-1][0]
