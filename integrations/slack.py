from __future__ import annotations

import shlex
from typing import Any

from agent_builder.auth import principal
from agent_builder.errors import ApprovalRequired


def _actor(command: dict[str, Any]):
    return principal(
        'slack',
        str(command.get('user_id') or ''),
        scope=str(command.get('team_id') or ''),
        display_name=str(command.get('user_name') or ''),
    )


def _conversation(command: dict[str, Any]) -> str:
    return str(command.get('channel_id') or '')


def _split_text(text: str) -> list[str]:
    try:
        return shlex.split(text or '')
    except ValueError:
        return (text or '').split()


def _csv(value: str) -> list[str]:
    return [x.strip() for x in str(value or '').split(',') if x.strip()]


def _parse_key_values(tokens: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    aliases = {
        'name': 'display_name',
        'agent_name': 'display_name',
        'autonomy': 'risk_level',
        'autonomy_level': 'risk_level',
        'access': 'access_policy',
        'mcp': 'mcp_servers',
        'mcps': 'mcp_servers',
    }
    list_fields = {'skills', 'mcp_servers', 'shared_users', 'capabilities'}
    for token in tokens:
        if '=' not in token:
            raise ValueError('fields must be key=value, e.g. name="Ops helper" model=nous:Hermes-4')
        key, value = token.split('=', 1)
        key = aliases.get(key.strip().replace('-', '_'), key.strip().replace('-', '_'))
        value = value.strip()
        if key in list_fields:
            payload[key] = _csv(value)
        elif key in {
            'display_name', 'model', 'purpose', 'description', 'instructions',
            'access_policy', 'risk_level',
        }:
            payload[key] = value
        else:
            raise ValueError(f'unsupported field: {key}')
    return payload


def _catalog_text(service) -> str:
    c = service.catalogs()
    models = [m['value'] if isinstance(m, dict) else str(m) for m in c['models']]
    return (
        '*Catalog*\n'
        'Models: ' + (', '.join(models[:20]) or '(no connected providers found)') + '\n'
        'Skills: ' + (', '.join(c['skills'][:20]) or '(none)') + '\n'
        'MCP: ' + (', '.join(c['mcp_servers']) or '(none)') + '\n'
        'Autonomy: ' + ', '.join(c.get('autonomy_levels') or ['human-approval', 'agent', 'autonomous']) + '\n'
        'RBAC toolsets: ' + (', '.join((c.get('rbac_toolsets') or [])[:20]) or '(none)')
    )


def _fmt_list(rows) -> str:
    if not rows:
        return 'No authorized SSA agents.'
    return '\n'.join(
        f"• `{r['profile_name']}` — {r['display_name']} — owner `{r['owner_id']}` — {r['status']}"
        for r in rows
    )


def _fmt_requests(rows) -> str:
    if not rows:
        return 'No pending approval requests.'
    return '\n'.join(
        f"• `{r['id']}` — {r['request_type']} — agent `{r.get('agent_id') or ''}` — requested by `{r['requested_by']}`"
        for r in rows
    )


def _fmt_agent(a) -> str:
    return (
        '*Agent created*\n'
        f"Name: {a['display_name']}\n"
        f"Profile: `{a['profile_name']}`\n"
        f"Owner: `{a['owner_id']}`\n"
        f"Access: {a['access_policy']}\n"
        f"Status: {a['status']}"
    )


def _usage() -> str:
    return (
        '*Agent Builder commands*\n'
        '`/agent-builder create name="Ops helper" model=nous:Hermes-4 purpose="Answer scoped operational questions"`\n'
        '`/agent-builder list`\n'
        '`/agent-builder catalog`\n'
        '`/agent-builder requests`\n'
        '`/agent-builder update <ssa-profile> purpose="..." model=provider:model autonomy=agent access=shared skills=a,b mcp_servers=x,y shared_users=slack:U123`\n'
        '`/agent-builder delete <ssa-profile>`\n'
        '`/agent chat <ssa-profile> [optional first message]`\n'
        '`/agent share <ssa-profile> slack:U123`'
    )


async def _dispatch_as_agent(command: dict[str, Any], service, adapter, actor, agent_key: str, message_text: str = ''):
    ag = service.bind(actor, _conversation(command), agent_key)
    if not message_text:
        return f"Selected {ag['display_name']} (`{ag['profile_name']}`). Send your next message to chat with this agent."
    if not adapter or not hasattr(adapter, '_build_message_event') or not hasattr(adapter, 'handle_message'):
        return f"Selected {ag['display_name']} (`{ag['profile_name']}`). Inline dispatch is unavailable; send the message as your next Slack message."
    try:
        event = {
            'type': 'message',
            'text': message_text,
            'channel': _conversation(command),
            'user': command.get('user_id') or '',
            'team': command.get('team_id') or '',
            'ts': command.get('trigger_id') or '',
        }
        msg_event = await adapter._build_message_event(
            event,
            text=message_text,
            original_text=message_text,
            command_probe_text=message_text,
            is_command_text=False,
            channel_id=_conversation(command),
            team_id=str(command.get('team_id') or ''),
            ts=str(command.get('trigger_id') or ''),
            user_id=str(command.get('user_id') or ''),
            thread_ts='',
            is_dm=str(command.get('channel_name') or '').startswith('directmessage'),
            media_urls=[],
            media_types=[],
            channel_context=None,
        )
        msg_event.source.profile = ag['profile_name']
        await adapter.handle_message(msg_event)
        return f"Dispatched to {ag['display_name']} (`{ag['profile_name']}`)."
    except Exception:
        return f"Selected {ag['display_name']} (`{ag['profile_name']}`). Inline dispatch failed; send the message again normally."


async def _agent_builder_command(ack, respond, command, service):
    await ack()
    actor = _actor(command)
    tokens = _split_text(command.get('text') or '')
    try:
        if not tokens:
            await respond(_usage(), response_type='ephemeral')
            return
        cmd = tokens[0].lower()
        if cmd in {'list', 'agents'}:
            await respond(_fmt_list(service.list_agents(actor)), response_type='ephemeral')
            return
        if cmd == 'catalog':
            await respond(_catalog_text(service), response_type='ephemeral')
            return
        if cmd == 'requests':
            await respond(_fmt_requests(service.pending_requests(actor)), response_type='ephemeral')
            return
        if cmd == 'create':
            if len(tokens) == 1:
                await respond('Create requires fields in Slack. Example:\n`/agent-builder create name="Ops helper" model=nous:Hermes-4 purpose="Answer scoped operational questions"`', response_type='ephemeral')
                return
            spec = _parse_key_values(tokens[1:])
            ag = service.create_agent(actor, spec)
            await respond(_fmt_agent(ag), response_type='ephemeral')
            return
        if cmd == 'update' and len(tokens) >= 3:
            payload = _parse_key_values(tokens[2:])
            ag = service.update_agent(actor, tokens[1], payload)
            await respond(f"Updated {ag['display_name']} (`{ag['profile_name']}`).", response_type='ephemeral')
            return
        if cmd == 'delete' and len(tokens) == 2:
            ag = service.get_agent(actor, tokens[1])
            service.delete_agent(actor, tokens[1])
            await respond(f"Deleted {ag['display_name']} (`{ag['profile_name']}`).", response_type='ephemeral')
            return
        await respond(_usage(), response_type='ephemeral')
    except ApprovalRequired as pending:
        await respond(f'Approval required. Request: `{pending}`. Open Hermes Dashboard / Agent Builder to approve.', response_type='ephemeral')
    except Exception as e:
        await respond(f'Error: {e}', response_type='ephemeral')


async def _agent_command(ack, respond, command, service, adapter):
    await ack()
    actor = _actor(command)
    tokens = _split_text(command.get('text') or '')
    try:
        if not tokens or tokens[0] == 'list':
            await respond(_fmt_list(service.list_agents(actor)), response_type='ephemeral')
            return
        if tokens[0] == 'chat':
            if len(tokens) < 2:
                await respond('Usage: `/agent chat <ssa-profile> [message]`', response_type='ephemeral')
                return
            text = ' '.join(tokens[2:]).strip()
            await respond(await _dispatch_as_agent(command, service, adapter, actor, tokens[1], text), response_type='ephemeral')
            return
        if tokens[0] == 'share' and len(tokens) >= 3:
            service.share_agent(actor, tokens[1], tokens[2], 'user')
            await respond('Shared.', response_type='ephemeral')
            return
        await respond(await _dispatch_as_agent(command, service, adapter, actor, tokens[0]), response_type='ephemeral')
    except Exception as e:
        await respond(f'Agent unavailable or access denied: {e}', response_type='ephemeral')


def register_slack(app, adapter, service):
    if app is None or not hasattr(app, 'command'):
        return

    @app.command('/agent-builder')
    async def handle_agent_builder(ack, respond, command):
        await _agent_builder_command(ack, respond, command, service)

    @app.command('/agent_builder')
    async def handle_agent_builder_underscore(ack, respond, command):
        await _agent_builder_command(ack, respond, command, service)

    @app.command('/agent')
    async def handle_agent(ack, respond, command):
        await _agent_command(ack, respond, command, service, adapter)
