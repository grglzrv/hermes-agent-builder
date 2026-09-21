import asyncio

from agent_builder.auth import principal
from agent_builder.registry import Registry
from agent_builder.service import AgentService
from integrations.slack import _agent_builder_command, _agent_builder_mention, _agent_command, _mention_to_command, _modal_blocks, _normalize_share_target, _open_update_modal, _parse_key_values


def make_service(tmp_path):
    h = tmp_path / "h"
    (h / "profiles").mkdir(parents=True)
    (h / "skills" / "demo-skill").mkdir(parents=True)
    (h / "skills" / "demo-skill" / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n")
    (h / "config.yaml").write_text("mcp_servers:\n  demo-mcp:\n    transport: http\n    url: https://mcp.example.com/mcp\n")
    s = AgentService(Registry(tmp_path / "db.sqlite"), h, allowed_models=["m"])
    actor = principal("slack", "U1", "T1")
    s.bootstrap_admin(actor)
    s.registry.grant_global(actor.id, "agent-builder-user", actor.id)
    return s


class Recorder:
    def __init__(self):
        self.acked = False
        self.responses = []
        self.said = []

    async def ack(self, *args, **kwargs):
        self.acked = True

    async def respond(self, text, **kwargs):
        self.responses.append((text, kwargs))

    async def say(self, *args, **kwargs):
        self.said.append((args, kwargs))


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


def test_slack_create_and_list_use_member_id_principal(tmp_path):
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
    assert rows[0]["owner_id"] == "slack:U1"

    rec2 = Recorder()
    asyncio.run(_agent_builder_command(rec2.ack, rec2.respond, command('list'), service))
    assert "ssa-ops-helper" in rec2.responses[-1][0]


def test_mention_text_routes_to_agent_builder_list(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    service.create_agent(actor, {"display_name": "Mention Agent", "model": "m"})
    rec = Recorder()
    event = {
        "text": "<@UBOT> /agent-builder list",
        "user": "U1",
        "team": "T1",
        "channel": "C1",
        "ts": "123.456",
    }
    parsed = _mention_to_command(event, event["text"])
    assert parsed is not None
    assert parsed[0] == "agent-builder"
    assert parsed[1]["text"] == "list"
    asyncio.run(_agent_builder_mention(event, rec.say, service, adapter=None))
    assert rec.said
    assert "ssa-mention-agent" in rec.said[-1][1]["text"]
    assert rec.said[-1][1]["thread_ts"] == "123.456"


def test_mention_bare_agent_builder_returns_create_fallback(tmp_path):
    service = make_service(tmp_path)
    rec = Recorder()
    event = {
        "text": "<@UBOT> /agent-builder",
        "user": "U1",
        "team": "T1",
        "channel": "C1",
        "ts": "123.457",
    }
    asyncio.run(_agent_builder_mention(event, rec.say, service, adapter=None))
    assert rec.said
    assert "Agent Builder" in rec.said[-1][1]["text"]
    assert "Dashboard" in rec.said[-1][1]["text"] or "wizard" in rec.said[-1][1]["text"]


def test_slack_allowed_user_gets_builder_user_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U1")
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    # make_service bootstraps U1 as admin; use a fresh non-admin allowed user instead
    actor = principal("slack", "U2", "T1")
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U2")
    rec = Recorder()
    asyncio.run(_agent_builder_command(rec.ack, rec.respond, {**command('create name="Allowed" model=m purpose="p" instructions="i"'), "user_id": "U2", "team_id": "T1"}, service))
    assert service.registry.is_builder_user(actor.id)
    assert 'Agent created' in rec.responses[-1][0]


def test_slack_allowed_user_gets_builder_user_grant_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    service = make_service(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(service.pm.home))
    (service.pm.home / ".env").write_text("SLACK_ALLOWED_USERS=U3\n")
    actor = principal("slack", "U3", "T1")
    rec = Recorder()
    asyncio.run(_agent_builder_command(rec.ack, rec.respond, {**command('create name="Dotenv" model=m purpose="p" instructions="i"'), "user_id": "U3", "team_id": "T1"}, service))
    assert service.registry.is_builder_user(actor.id)
    assert 'Agent created' in rec.responses[-1][0]


def test_slack_modal_uses_catalog_fields(tmp_path):
    service = make_service(tmp_path)
    blocks = _modal_blocks(service)
    by_id = {b.get("block_id"): b for b in blocks}
    ids = [b.get("block_id") for b in blocks]
    assert by_id["model"]["element"]["type"] == "static_select"
    assert by_id["access_policy"].get("dispatch_action") is True
    assert by_id["access_policy"]["element"]["action_id"] == "access_policy_selected"
    assert "shared_slack_users" not in by_id
    assert by_id["private_owner"]["type"] == "section"
    assert by_id["skills"]["element"]["type"] == "multi_static_select"
    assert "custom_skill_name" not in by_id
    assert "custom_skill_text" not in by_id
    assert ids.index("skills") < ids.index("mcp_servers") < ids.index("advanced_options")
    assert by_id["mcp_servers"]["element"]["type"] == "multi_static_select"
    assert by_id["rbac_install"]["element"]["type"] == "checkboxes"
    assert by_id["rbac_fail_closed"]["element"]["type"] == "checkboxes"
    assert by_id["rbac_toolsets"]["element"]["type"] == "multi_static_select"
    assert by_id["rbac_deny"]["element"]["type"] == "multi_static_select"
    assert by_id["rbac_skills"]["element"]["type"] == "multi_static_select"
    assert by_id["rbac_bypass_sensitive_paths"]["element"]["type"] == "static_select"
    assert by_id["rbac_advanced_options"].get("dispatch_action") is True
    assert by_id["rbac_advanced_options"]["element"]["action_id"] == "rbac_advanced_options_toggle"
    assert "rbac_extends" not in by_id
    assert not by_id["instructions"].get("optional")
    assert not by_id["rbac_role"].get("optional")
    assert not by_id["rbac_toolsets"].get("optional")
    assert not by_id["rbac_skills"].get("optional")
    assert not by_id["rbac_bypass_sensitive_paths"].get("optional")
    assert not by_id["rbac_install"].get("optional")
    assert not by_id["rbac_fail_closed"].get("optional")


def test_slack_modal_share_fields_only_when_shared(tmp_path):
    service = make_service(tmp_path)
    private_by_id = {b.get("block_id"): b for b in _modal_blocks(service, current_user_id="U1")}
    assert "slack:U1" in private_by_id["private_owner"]["text"]["text"]
    by_id = {b.get("block_id"): b for b in _modal_blocks(service, include_share_fields=True, current_user_id="U1")}
    assert by_id["shared_slack_users"]["element"]["type"] == "multi_users_select"
    assert "shared_users" in by_id
    assert "private_owner" not in by_id


def test_advanced_slack_modal_is_same_main_form(tmp_path):
    service = make_service(tmp_path)
    base = {b.get("block_id"): b for b in _modal_blocks(service)}
    advanced_blocks = _modal_blocks(service, advanced=True)
    advanced = {b.get("block_id"): b for b in advanced_blocks}
    rbac_advanced = {b.get("block_id"): b for b in _modal_blocks(service, include_rbac_advanced_fields=True)}
    assert "custom_skill_name" not in base
    assert "custom_mcp_name" not in base
    assert "rbac_extends" not in base
    assert advanced["custom_skill_name"]["element"]["type"] == "plain_text_input"
    assert advanced["custom_mcp_name"]["element"]["type"] == "plain_text_input"
    assert "rbac_extends" not in advanced
    assert rbac_advanced["rbac_extends"]["element"]["type"] == "plain_text_input"
    assert rbac_advanced["rbac_user_roles"]["element"]["type"] == "plain_text_input"


def test_update_access_shared_toggle_shows_share_fields_for_private_agent(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    ag = service.create_agent(actor, {"display_name": "Private Update", "model": "m", "purpose": "old", "access_policy": "private"})
    ids = [b.get("block_id") for b in _modal_blocks(service, mode="update", actor=actor, selected_agent_key=ag["profile_name"], include_share_fields=True, current_user_id="U1")]
    assert "shared_slack_users" in ids
    assert "shared_users" in ids
    assert "private_owner" not in ids


def test_update_access_private_toggle_hides_share_fields_for_shared_agent(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    ag = service.create_agent(actor, {"display_name": "Shared Update", "model": "m", "purpose": "old", "access_policy": "shared", "shared_users": ["slack:U2"]})
    ids = [b.get("block_id") for b in _modal_blocks(service, mode="update", actor=actor, selected_agent_key=ag["profile_name"], include_share_fields=False, current_user_id="U1")]
    assert "shared_slack_users" not in ids
    assert "shared_users" not in ids
    assert "private_owner" in ids


def test_update_modal_lists_only_accessible_agents_even_for_admin(tmp_path):
    service = make_service(tmp_path)
    owner = principal("slack", "U1", "T1")
    other = principal("slack", "U2", "T1")
    service.registry.upsert_principal(other)
    service.registry.grant_global(other.id, "agent-builder-user", owner.id)
    mine = service.create_agent(owner, {"display_name": "Mine", "model": "m", "purpose": "mine"})
    theirs = service.create_agent(other, {"display_name": "Theirs", "model": "m", "purpose": "theirs"})
    blocks = _modal_blocks(service, mode="update", actor=owner, current_user_id="U1")
    by_id = {b.get("block_id"): b for b in blocks}
    opts = by_id["agent_key"]["element"]["options"]
    values = [o["value"] for o in opts]
    assert mine["profile_name"] in values
    assert theirs["profile_name"] not in values
    assert by_id["agent_key"].get("dispatch_action") is True
    assert by_id["agent_key"]["element"]["action_id"] == "agent_key_selected"
    assert by_id["purpose"]["element"].get("initial_value") == "mine"

    service.registry.share(theirs["id"], owner.id, "user", other.id)
    shared_blocks = _modal_blocks(service, mode="update", actor=owner, current_user_id="U1", selected_agent_key=theirs["profile_name"])
    shared_opts = {o["value"] for o in {b.get("block_id"): b for b in shared_blocks}["agent_key"]["element"]["options"]}
    assert theirs["profile_name"] in shared_opts


class FakeClient:
    def __init__(self):
        self.views = []

    async def views_open(self, **kwargs):
        self.views.append(kwargs)


def test_update_command_without_fields_opens_native_update_modal(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    service.create_agent(actor, {"display_name": "Updatable", "model": "m", "purpose": "old"})
    client = FakeClient()
    opened = asyncio.run(_open_update_modal(client, "TRIGGER", service, actor, current_user_id="U1"))
    assert opened is True
    view = client.views[-1]["view"]
    assert view["callback_id"] == "agent_builder_update"
    assert view["submit"]["text"] == "Update"
    ids = [b.get("block_id") for b in view["blocks"]]
    assert ids[0] == "agent_key"
    assert "display_name" not in ids
    assert ids.index("mcp_servers") < ids.index("advanced_options")


def test_updater_alias_opens_native_update_modal(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    service.create_agent(actor, {"display_name": "Alias Updatable", "model": "m", "purpose": "old"})
    client = FakeClient()
    rec = Recorder()
    asyncio.run(_agent_builder_command(rec.ack, rec.respond, {**command("updater"), "trigger_id": "TRIGGER"}, service, client=client))
    assert rec.acked is True
    assert client.views
    view = client.views[-1]["view"]
    assert view["callback_id"] == "agent_builder_update"
    assert view["submit"]["text"] == "Update"
    ids = [b.get("block_id") for b in view["blocks"]]
    assert ids[0] == "agent_key"
    assert "display_name" not in ids


def test_update_without_accessible_agents_explains_empty_update_list(tmp_path):
    service = make_service(tmp_path)
    rec = Recorder()
    asyncio.run(_agent_builder_command(rec.ack, rec.respond, {**command("update"), "trigger_id": "TRIGGER"}, service, client=FakeClient()))
    assert 'No agents are currently owned by or shared with your Slack identity.' in rec.responses[-1][0]


def test_update_mention_without_trigger_explains_slack_modal_limit(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    service.create_agent(actor, {"display_name": "Mention Updatable", "model": "m", "purpose": "old"})
    rec = Recorder()
    asyncio.run(_agent_builder_command(rec.ack, rec.respond, command("update"), service, client=None))
    assert 'cannot open a Slack modal' in rec.responses[-1][0]
    assert 'slash-command trigger' in rec.responses[-1][0]


def test_help_uses_native_slash_commands(tmp_path):
    service = make_service(tmp_path)
    rec = Recorder()
    asyncio.run(_agent_builder_command(rec.ack, rec.respond, command('help'), service))
    text = rec.responses[-1][0]
    assert '`/agent-builder`' in text
    assert '@hermes-agent /agent-builder' not in text


def test_update_without_fields_without_agents_explains_empty_list(tmp_path):
    service = make_service(tmp_path)
    rec = Recorder()
    asyncio.run(_agent_builder_command(rec.ack, rec.respond, command('update'), service))
    assert 'No agents are currently owned by or shared with your Slack identity.' in rec.responses[-1][0]


def test_agent_shorthand_passes_message_to_dispatch(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    ag = service.create_agent(actor, {"display_name": "Chat Agent", "model": "m"})
    rec = Recorder()
    asyncio.run(_agent_command(rec.ack, rec.respond, command(f'{ag["profile_name"]} hello there'), service, adapter=None))
    assert 'Inline dispatch is unavailable' in rec.responses[-1][0]


class ThreadClient:
    def __init__(self):
        self.messages = []

    async def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True, "ts": "111.222"}


class ThreadAdapter:
    def __init__(self):
        self.client = ThreadClient()
        self.synthetic = []

    def _get_client(self, channel, team_id=None):
        self.channel = channel
        self.team_id = team_id
        return self.client

    async def _handle_slack_message(self, event, payload=None):
        self.synthetic.append((event, payload))


class Source:
    def __init__(self):
        self.user_id = "U1"
        self.user_name = "tester"
        self.scope_id = "T1"
        self.chat_id = "C1"
        self.thread_id = "111.222"
        self.profile = None


class Event:
    def __init__(self):
        self.text = "hello"
        self.source = Source()


class RoutingAdapter:
    def __init__(self):
        self.events = []
        self.raw_events = []
        self.client = ThreadClient()

    def build_source(self):
        return Source()

    def _get_client(self, channel, team_id=None):
        self.channel = channel
        self.team_id = team_id
        return self.client

    async def _agent_builder_run_profile_chat(self, profile, session_name, text):
        self.profile_run = (profile, session_name, text)
        return f'profile={profile}'

    async def _handle_slack_message(self, event, payload=None):
        self.raw_events.append((event, payload))

    async def handle_message(self, event):
        self.events.append(event)


def test_agent_chat_opens_thread_binds_thread_and_dispatches_inline_message(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    ag = service.create_agent(actor, {"display_name": "Thread Agent", "model": "m"})
    adapter = ThreadAdapter()
    rec = Recorder()

    asyncio.run(_agent_command(rec.ack, rec.respond, command(f'chat {ag["profile_name"]} hello there'), service, adapter=adapter))

    assert adapter.client.messages
    assert adapter.client.messages[-1]["channel"] == "C1"
    assert "Thread Agent" in adapter.client.messages[-1]["text"]
    assert service.registry.binding(actor, "C1", "111.222") == ag["id"]
    assert adapter.synthetic[-1][0]["thread_ts"] == "111.222"
    assert adapter.synthetic[-1][0]["text"] == "hello there"
    assert "Opened thread" in rec.responses[-1][0]


def test_agent_builder_profile_router_stamps_bound_thread_profile(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    ag = service.create_agent(actor, {"display_name": "Routed Agent", "model": "m"})
    service.bind(actor, "C1", ag["profile_name"], thread="111.222")
    adapter = RoutingAdapter()

    from integrations.slack import _install_agent_profile_router
    _install_agent_profile_router(adapter, service)
    event = Event()
    asyncio.run(adapter.handle_message(event))

    assert adapter.events == [event]
    assert event.source.profile == ag["profile_name"]
    built = adapter.build_source()
    assert built.profile == ag["profile_name"]

    asyncio.run(adapter._handle_slack_message({
        "type": "message",
        "channel": "C1",
        "thread_ts": "111.222",
        "user": "U1",
        "team": "T1",
        "text": "which profile",
    }, {"team_id": "T1"}))
    assert adapter.raw_events == []
    assert adapter.profile_run == (ag["profile_name"], "slack-T1-C1-111.222", "which profile")
    assert adapter.client.messages[-1] == {"channel": "C1", "thread_ts": "111.222", "text": f"profile={ag['profile_name']}"}


def test_agent_share_accepts_profile_display_or_short_name_and_uses_slack_member_id(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    ag = service.create_agent(actor, {"display_name": "Test", "model": "m"})

    for key in (ag["profile_name"], "Test", "test"):
        rec = Recorder()
        asyncio.run(_agent_command(rec.ack, rec.respond, command(f'share {key} slack:U2'), service, adapter=None))
        assert "Shared" in rec.responses[-1][0]
        assert service.registry.get_role(ag["id"], "slack:U2") == "user"


def test_agent_share_missing_agent_reports_not_found_not_typeerror(tmp_path):
    service = make_service(tmp_path)
    rec = Recorder()
    asyncio.run(_agent_command(rec.ack, rec.respond, command('share ssa-missing slack:U2'), service, adapter=None))
    assert "agent not found" in rec.responses[-1][0]
    assert "NoneType" not in rec.responses[-1][0]


def test_update_modal_prefills_existing_rbac_rights(tmp_path):
    service = make_service(tmp_path)
    actor = principal("slack", "U1", "T1")
    ag = service.create_agent(actor, {
        "display_name": "RBAC Visible",
        "model": "m",
        "purpose": "old",
        "rbac": {
            "install": True,
            "role": "integra-assist-admin",
            "users": ["slack:U1"],
            "bootstrap_admins": ["slack:U1"],
            "toolsets": ["skill_view", "read_file"],
            "skills": ["demo-skill"],
            "deny": ["terminal"],
            "fail_closed": True,
        },
    })
    listed = service.list_agents(actor)[0]
    assert listed["rbac"]["role"] == "integra-assist-admin"
    assert listed["rbac"]["toolsets"] == ["skill_view", "read_file"]
    blocks = _modal_blocks(service, mode="update", actor=actor, selected_agent_key=ag["profile_name"], current_user_id="U1")
    by_id = {b.get("block_id"): b for b in blocks}
    assert by_id["rbac_role"]["element"]["initial_value"] == "integra-assist-admin"
    assert "slack:U1" in by_id["rbac_users"]["element"]["initial_value"]
    assert by_id["rbac_bootstrap_admins"]["element"]["initial_value"] == "slack:U1"
    assert {o["value"] for o in by_id["rbac_toolsets"]["element"]["initial_options"]} == {"skill_view", "read_file"}
    assert {o["value"] for o in by_id["rbac_skills"]["element"]["initial_options"]} == {"demo-skill"}
    assert {o["value"] for o in by_id["rbac_deny"]["element"]["initial_options"]} == {"terminal"}


def test_slack_principal_canonicalizes_team_prefixed_member_id():
    assert principal("slack", "U1", "T1").id == "slack:U1"
    assert principal("slack", "T1:U1").id == "slack:U1"
    assert _normalize_share_target(principal("slack", "U0", "T1"), "slack:T1:U2") == "slack:U2"
