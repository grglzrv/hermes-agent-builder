import asyncio
import sys
import types

from agent_builder.auth import principal
from agent_builder.registry import Registry
from agent_builder.service import AgentService
from integrations.telegram import _dispatch_as_agent, _dispatch_bound_text, _split_chat_args


def make_service(tmp_path):
    h = tmp_path / "h"
    (h / "profiles").mkdir(parents=True)
    (h / "skills").mkdir()
    (h / "config.yaml").write_text("mcp_servers: {}\n")
    s = AgentService(Registry(tmp_path / "db.sqlite"), h, allowed_models=["m"])
    actor = principal("telegram", "1")
    s.bootstrap_admin(actor)
    s.registry.grant_global(actor.id, "agent-builder-user", actor.id)
    ag = s.create_agent(actor, {"display_name": "Chat Agent", "model": "m"})
    return s, actor, ag


class User:
    id = "1"
    full_name = "Tester"
    username = "tester"


class Chat:
    id = "C1"


class Message:
    def __init__(self, text):
        self.text = text
        self.chat = Chat()
        self.from_user = User()
        self.message_id = "M1"
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)


class Update:
    def __init__(self, text):
        self.effective_user = User()
        self.effective_chat = Chat()
        self.effective_message = Message(text)
        self.update_id = 7


class Adapter:
    def __init__(self):
        self.events = []

    def _build_message_event(self, message, msg_type, update_id=None):
        source = types.SimpleNamespace(profile=None)
        return types.SimpleNamespace(text=message.text, source=source, message_type=msg_type, update_id=update_id)

    async def handle_message(self, event):
        self.events.append(event)


def install_gateway_stub(monkeypatch):
    gateway = types.ModuleType("gateway")
    platforms = types.ModuleType("gateway.platforms")
    event = types.ModuleType("gateway.platforms.event")
    event.MessageType = types.SimpleNamespace(TEXT="text")
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.event", event)


def test_split_chat_args():
    assert _split_chat_args(["chat", "ssa-x", "hello", "there"]) == ("ssa-x", "hello there")
    assert _split_chat_args(["ssa-x"]) == ("ssa-x", "")
    assert _split_chat_args(["chat"]) == ("", "")
    assert _split_chat_args(["select", "ssa-x"]) == ("select", "ssa-x")


def test_agent_chat_inline_message_dispatches_to_profile(tmp_path, monkeypatch):
    install_gateway_stub(monkeypatch)
    service, actor, ag = make_service(tmp_path)
    adapter = Adapter()
    update = Update("/agent chat ignored")
    asyncio.run(_dispatch_as_agent(update, service, adapter, actor, ag["id"], "hello from inline"))
    assert len(adapter.events) == 1
    assert adapter.events[0].text == "hello from inline"
    assert adapter.events[0].source.profile == ag["profile_name"]


def test_bound_plain_text_dispatches_to_profile(tmp_path, monkeypatch):
    install_gateway_stub(monkeypatch)
    service, actor, ag = make_service(tmp_path)
    service.bind(actor, "C1", ag["id"])
    adapter = Adapter()
    update = Update("normal follow-up")
    assert asyncio.run(_dispatch_bound_text(update, service, adapter)) is True
    assert adapter.events[0].text == "normal follow-up"
    assert adapter.events[0].source.profile == ag["profile_name"]
