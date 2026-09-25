from __future__ import annotations

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any

import yaml

from agent_builder.auth import principal
from agent_builder.errors import ApprovalRequired


def _actor(command: dict[str, Any]):
    return principal(
        'slack',
        str(command.get('user_id') or ''),
        scope=str(command.get('team_id') or ''),
        display_name=str(command.get('user_name') or ''),
    )



def _slack_allowed_user_ids() -> set[str]:
    allowed: set[str] = set()
    home = Path(os.getenv('HERMES_HOME') or '/root/.hermes')
    raw_values = [os.getenv('SLACK_ALLOWED_USERS') or '']
    try:
        for line in (home / '.env').read_text(errors='ignore').splitlines():
            if line.startswith('SLACK_ALLOWED_USERS='):
                raw_values.append(line.split('=', 1)[1].strip().strip('\"\''))
    except Exception:
        pass
    for raw in raw_values:
        for item in re.split(r'[,\s]+', raw):
            item = item.strip()
            if item:
                allowed.add(item)
    try:
        cfg = yaml.safe_load((home / 'config.yaml').read_text()) or {}
    except Exception:
        cfg = {}
    for root in (cfg.get('slack'), (cfg.get('platforms') or {}).get('slack')):
        if isinstance(root, dict):
            users = root.get('users') or root.get('allowed_users') or []
            if isinstance(users, str):
                users = re.split(r'[,\s]+', users)
            if isinstance(users, list):
                for item in users:
                    item = str(item or '').strip()
                    if item:
                        allowed.add(item)
    return allowed


def _ensure_slack_builder_user(service, actor) -> None:
    if getattr(actor, 'platform', '') != 'slack':
        return
    if actor.user_id not in _slack_allowed_user_ids():
        return
    try:
        service.registry.upsert_principal(actor)
        if not service.registry.is_builder_user(actor.id):
            service.registry.grant_global(actor.id, 'agent-builder-user', actor.id)
    except Exception:
        # Authorization should fail closed if registry writes fail.
        return


def _conversation(command: dict[str, Any]) -> str:
    return str(command.get('channel_id') or '')


def _thread(command: dict[str, Any]) -> str:
    return str(command.get('thread_ts') or command.get('message_ts') or '')


def _split_text(text: str) -> list[str]:
    try:
        return shlex.split(text or '')
    except ValueError:
        return (text or '').split()


def _csv(value: str) -> list[str]:
    return [x.strip() for x in str(value or '').split(',') if x.strip()]


def _resolve_agent_key(service, actor, key: str) -> str:
    wanted = str(key or '').strip()
    if not wanted:
        raise ValueError('agent profile is required')
    rows = _accessible_update_agents(service, actor)
    lowered = wanted.lower()
    for row in rows:
        if wanted in {str(row.get('id') or ''), str(row.get('profile_name') or '')}:
            return str(row.get('id') or row.get('profile_name'))
    display_matches = [
        row for row in rows
        if lowered == str(row.get('display_name') or '').strip().lower()
    ]
    if len(display_matches) == 1:
        return str(display_matches[0].get('id') or display_matches[0].get('profile_name'))
    prefix_matches = [
        row for row in rows
        if str(row.get('profile_name') or '').lower().startswith(f'ssa-{lowered}-')
    ]
    if len(prefix_matches) == 1:
        return str(prefix_matches[0].get('id') or prefix_matches[0].get('profile_name'))
    # Fall back to the raw key so the service returns its normal access/error text.
    return wanted


def _normalize_share_target(actor, value: str) -> str:
    raw = str(value or '').strip()
    if not raw:
        raise ValueError('share target is required')
    if raw.startswith('slack:'):
        # Slack share targets are always slack:<member_id>. The team/workspace
        # id (T...) from Slack URLs is not part of the Agent Builder identity.
        member = raw.split(':', 1)[1].strip()
        if ':' in member:
            member = member.split(':')[-1].strip()
        return principal('slack', member).id
    if ':' in raw:
        platform, user_id = raw.split(':', 1)
        return principal(platform, user_id).id
    return raw


def _parse_key_values(tokens: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    aliases = {
        'name': 'display_name',
        'agent_name': 'display_name',
        'autonomy': 'risk_level',
        'autonomy_level': 'risk_level',
        'access': 'access_policy',
        'instruction': 'instructions',
        'system_prompt': 'instructions',
        'systemprompt': 'instructions',
        'prompt': 'instructions',
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
        '`/agent-builder` — open the create wizard\n'
        '`/agent-builder create` — open the create wizard\n'
        '`/agent-builder advanced` — open the full advanced wizard (skills/MCP/custom skill/custom MCP/full RBAC)\n'
        '`/agent-builder create name="Ops helper" model=openai-codex:gpt-5.6-sol purpose="Answer scoped ops questions"`\n'
        '`/agent-builder list`\n'
        '`/agent-builder catalog`\n'
        '`/agent-builder requests`\n'
        '`/agent-builder update` — open the update wizard for agents you own or can access\n'
        '`/agent-builder updater` — alias for update wizard\n'
        '`/agent-builder update <ssa-profile> purpose="..." model=provider:model autonomy=agent access=shared skills=a,b mcp_servers=x,y shared_users=slack:U123`\n'
        '`/agent-builder delete <ssa-profile>`\n\n'
        '*Agent commands*\n'
        '`/agent` or `/agent list` — list agents you can use\n'
        '`/agent chat <ssa-profile>` — select an agent for this Slack channel/thread\n'
        '`/agent chat <ssa-profile> <message>` — send one message immediately using that agent profile\n'
        '`/agent <ssa-profile> <message>` — shorthand for one message\n'
        '`/agent share <ssa-profile> slack:U123` — share an agent with a Slack user'
    )


def _update_usage() -> str:
    return (
        '*Update an agent*\n'
        '`/agent-builder update` or `/agent-builder updater` opens the update wizard when native Slack modal context is available.\n'
        '`/agent-builder update <ssa-profile> key=value [key=value ...]` updates inline.\n\n'
        'Supported keys: `purpose`, `description`, `instructions`, `model`, `autonomy`/`risk_level`, '
        '`access`/`access_policy`, `skills`, `mcp_servers`, `shared_users`, `capabilities`.\n\n'
        'Examples:\n'
        '`/agent-builder update ssa-ops-helper purpose="Answer ops questions"`\n'
        '`/agent-builder update ssa-ops-helper model=openai-codex:gpt-5.6-sol autonomy=agent`\n'
        '`/agent-builder update ssa-ops-helper access=shared shared_users=slack:U123,teams:user@example.com`\n'
        '`/agent-builder update ssa-ops-helper skills=devops/hermes-operations,mcp/native-mcp mcp_servers=context7`'
    )




def _update_fallback_text(reason: str = '') -> str:
    url = _dashboard_form_url()
    parts = ['*Update an agent*']
    if reason:
        parts.append(reason)
    parts.append('Native Slack modals require a real `/agent-builder update` slash-command trigger. A normal `@hermes-agent /agent-builder update` message cannot open a Slack modal; Slack does not send a modal trigger for message events.')
    if url:
        parts.append(f'Open Dashboard Agent Builder: {url}')
    parts.append('Create or share an agent first if the update list is empty.')
    return '\n'.join(parts)

def _agent_usage() -> str:
    return (
        '*Agent commands*\n'
        '`/agent list` — list agents you can use\n'
        '`/agent chat <ssa-profile>` — bind/select an agent for this Slack channel/thread\n'
        '`/agent chat <ssa-profile> <message>` — send a message immediately using that agent profile\n'
        '`/agent <ssa-profile> <message>` — shorthand for immediate chat\n'
        '`/agent share <ssa-profile> slack:U123` — share an agent with a user\n\n'
        'After `/agent chat <ssa-profile>` selects an agent, send your next message in this Slack chat/thread to continue with that agent when routed through Agent Builder.'
    )


def _dashboard_form_url() -> str:
    base = (
        os.getenv('AGENT_BUILDER_PUBLIC_URL')
        or os.getenv('HERMES_DASHBOARD_PUBLIC_URL')
        or os.getenv('HERMES_DASHBOARD_URL')
        or ''
    ).strip().rstrip('/')
    if not base:
        return ''
    return f'{base}/api/plugins/agent-builder/telegram-form'


def _create_fallback_text() -> str:
    url = _dashboard_form_url()
    if url:
        return (
            '*Agent Builder*\n'
            f'Open the agent creation wizard: {url}\n\n'
            'Or create inline with:\n'
            '`/agent-builder create name="Ops helper" model=provider:model purpose="Answer scoped operational questions"`'
        )
    return (
        '*Agent Builder*\n'
        'Open Hermes Dashboard → Plugins → Agent Builder to create an agent.\n\n'
        'Or create inline with:\n'
        '`/agent-builder create name="Ops helper" model=provider:model purpose="Answer scoped operational questions"`\n\n'
        'To make this message include a direct wizard link, set `HERMES_DASHBOARD_PUBLIC_URL` '
        'or `AGENT_BUILDER_PUBLIC_URL` for the gateway service and restart it.'
    )


def _strip_bot_mentions(text: str) -> str:
    return re.sub(r'<@[A-Z0-9]+(?:\|[^>]+)?>', '', text or '').strip()


def _is_agent_builder_mention(text: str) -> bool:
    stripped = _strip_bot_mentions(text).lower().strip()
    return stripped.startswith('/agent-builder') or stripped.startswith('/agent_builder') or stripped.startswith('/builder') or stripped.startswith('/agent ')


def _mention_to_command(event: dict[str, Any], text: str) -> tuple[str, dict[str, Any]] | None:
    stripped = _strip_bot_mentions(text).strip()
    if not stripped:
        return None
    tokens = _split_text(stripped)
    if not tokens:
        return None
    first = tokens[0].lower()
    rest = ' '.join(tokens[1:]).strip()
    if first in {'/agent-builder', '/agent_builder', '/builder'}:
        if not rest:
            rest = 'create'
        return 'agent-builder', _event_as_command(event, rest)
    if first == '/agent':
        return 'agent', _event_as_command(event, rest)
    return None


def _event_as_command(event: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        'text': text,
        'user_id': event.get('user') or event.get('user_id') or '',
        'user_name': event.get('user_name') or '',
        'team_id': event.get('team') or event.get('team_id') or '',
        'channel_id': event.get('channel') or event.get('channel_id') or '',
        'channel_name': event.get('channel_name') or '',
        'trigger_id': event.get('ts') or '',
        'thread_ts': event.get('thread_ts') or '',
        'message_ts': event.get('ts') or '',
    }


async def _open_agent_thread(command: dict[str, Any], adapter, ag: dict[str, Any]) -> str:
    channel_id = _conversation(command)
    team_id = str(command.get('team_id') or '')
    existing_thread = _thread(command)
    if existing_thread:
        return existing_thread
    if not adapter or not hasattr(adapter, '_get_client'):
        return ''
    client = adapter._get_client(channel_id, team_id=team_id) if team_id else adapter._get_client(channel_id)
    if client is None or not hasattr(client, 'chat_postMessage'):
        return ''
    result = await client.chat_postMessage(
        channel=channel_id,
        text=f":robot_face: Chat with *{ag['display_name']}* (`{ag['profile_name']}`). Reply in this thread to continue.",
    )
    if hasattr(result, 'data') and isinstance(result.data, dict):
        return str(result.data.get('ts') or '')
    if isinstance(result, dict):
        return str(result.get('ts') or '')
    try:
        return str(result.get('ts') or '')
    except Exception:
        return ''


async def _dispatch_as_agent(command: dict[str, Any], service, adapter, actor, agent_key: str, message_text: str = ''):
    resolved_key = _resolve_agent_key(service, actor, agent_key)
    ag = service.get_agent(actor, resolved_key)
    thread_ts = await _open_agent_thread(command, adapter, ag)
    service.bind(actor, _conversation(command), resolved_key, thread=thread_ts)
    if not thread_ts:
        if not message_text:
            return f"Selected {ag['display_name']} (`{ag['profile_name']}`). Send your next message to chat with this agent."
        return f"Selected {ag['display_name']} (`{ag['profile_name']}`). Inline dispatch is unavailable; send the message as your next Slack message."
    if message_text and adapter and hasattr(adapter, '_handle_slack_message'):
        try:
            synthetic_ts = str(command.get('trigger_id') or '') or f"{thread_ts}:agent-builder"
            await adapter._handle_slack_message({
                'type': 'message',
                'text': message_text,
                'channel': _conversation(command),
                'user': command.get('user_id') or '',
                'team': command.get('team_id') or '',
                'ts': synthetic_ts,
                'thread_ts': thread_ts,
                'client_msg_id': f"agent-builder-{synthetic_ts}",
            }, {'team_id': command.get('team_id') or ''})
            return f"Opened thread for {ag['display_name']} (`{ag['profile_name']}`) and dispatched your message."
        except Exception:
            return f"Opened thread for {ag['display_name']} (`{ag['profile_name']}`). Inline dispatch failed; reply in that thread to continue."
    return f"Opened thread for {ag['display_name']} (`{ag['profile_name']}`). Reply in that thread to chat with this agent."


def _plain(value: str, limit: int = 75) -> str:
    text = str(value or '').strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _option(value: str, label: str | None = None) -> dict[str, Any] | None:
    value = str(value or '').strip()
    if not value or len(value) > 75:
        return None
    return {'text': {'type': 'plain_text', 'text': _plain(label or value)}, 'value': value}


def _catalog_options(items, *, value_key: str = '', label_key: str = '', limit: int = 100) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            value = str(item.get(value_key or 'value') or '').strip()
            label = str(item.get(label_key or 'label') or value).strip()
        else:
            value = str(item or '').strip()
            label = value
        if not value or value in seen:
            continue
        opt = _option(value, label)
        if not opt:
            continue
        out.append(opt)
        seen.add(value)
        if len(out) >= limit:
            break
    return out


def _input_block(block_id: str, label: str, *, multiline: bool = False, optional: bool = False, placeholder: str = '', hint: str = '', initial_value: str | None = None) -> dict[str, Any]:
    element: dict[str, Any] = {'type': 'plain_text_input', 'action_id': 'value'}
    if multiline:
        element['multiline'] = True
    if placeholder:
        element['placeholder'] = {'type': 'plain_text', 'text': _plain(placeholder, 150)}
    if initial_value is not None:
        element['initial_value'] = str(initial_value)[:3000]
    block: dict[str, Any] = {
        'type': 'input',
        'block_id': block_id,
        'label': {'type': 'plain_text', 'text': _plain(label)},
        'element': element,
    }
    if hint:
        block['hint'] = {'type': 'plain_text', 'text': _plain(hint, 2000)}
    if optional:
        block['optional'] = True
    return block


def _static_select_block(block_id: str, label: str, options: list[dict[str, Any]], *, optional: bool = False, placeholder: str = 'Select', initial_value: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        'type': 'input',
        'block_id': block_id,
        'label': {'type': 'plain_text', 'text': _plain(label)},
        'element': {
            'type': 'static_select',
            'action_id': 'value',
            'placeholder': {'type': 'plain_text', 'text': _plain(placeholder)},
            'options': options,
        },
    }
    if optional:
        block['optional'] = True
    if options:
        selected = next((x for x in options if str(x.get('value') or '') == str(initial_value or '')), None)
        block['element']['initial_option'] = selected or options[0]
    return block


def _multi_select_block(block_id: str, label: str, options: list[dict[str, Any]], *, optional: bool = True, placeholder: str = 'Select one or more', initial_values: list[str] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        'type': 'input',
        'block_id': block_id,
        'label': {'type': 'plain_text', 'text': _plain(label)},
        'element': {
            'type': 'multi_static_select',
            'action_id': 'value',
            'placeholder': {'type': 'plain_text', 'text': _plain(placeholder)},
            'options': options,
        },
    }
    if optional:
        block['optional'] = True
    if initial_values:
        selected = {str(x) for x in initial_values}
        block['element']['initial_options'] = [x for x in options if str(x.get('value') or '') in selected]
    return block


def _agent_options(rows) -> list[dict[str, Any]]:
    opts: list[dict[str, Any]] = []
    for r in rows or []:
        label = f"{r.get('display_name') or r.get('profile_name')} ({r.get('profile_name')})"
        opt = _option(str(r.get('profile_name') or r.get('id') or ''), label)
        if opt:
            opts.append(opt)
    return opts


def _agent_related(service, agent_id: str) -> tuple[list[str], list[str]]:
    try:
        with service.registry.connect() as c:
            skills = [str(x[0]) for x in c.execute('SELECT skill_id FROM agent_skills WHERE agent_id=? ORDER BY skill_id', (agent_id,)).fetchall()]
            mcps = [str(x[0]) for x in c.execute('SELECT integration_id FROM agent_integrations WHERE agent_id=? ORDER BY integration_id', (agent_id,)).fetchall()]
        return skills, mcps
    except Exception:
        return [], []


def _agent_instructions(service, profile_name: str) -> str:
    try:
        text = (service.pm.profiles / profile_name / 'AGENTS.md').read_text()
    except Exception:
        return ''
    prefix = 'Operational instructions for this self-service Hermes agent.\n\n'
    return text[len(prefix):] if text.startswith(prefix) else text


def _agent_form_suffix(agent: dict[str, Any] | None, selected_agent_key: str = '') -> str:
    raw = str((agent or {}).get('profile_name') or selected_agent_key or '')
    safe = re.sub(r'[^A-Za-z0-9_-]+', '-', raw).strip('-')[:80]
    return safe


def _suffix_update_block_ids(blocks: list[dict[str, Any]], suffix: str) -> list[dict[str, Any]]:
    """Force Slack to reload selected-agent fields on update.

    Slack preserves input state across views_update when block_id/action_id match.
    That is correct for ordinary Access/Advanced toggles, but wrong when the
    user switches Agent to update: Purpose, Instructions, skills, MCP, RBAC,
    etc. must be re-initialized from the newly selected agent. Suffixing every
    mutable field with the selected profile makes Slack treat them as fresh
    inputs while helper readers still resolve by base block id.
    """
    if not suffix:
        return blocks
    skip = {'agent_key'}
    for block in blocks:
        bid = block.get('block_id')
        if isinstance(bid, str) and bid and bid not in skip and not bid.endswith('__' + suffix):
            block['block_id'] = f'{bid}__{suffix}'[:255]
    return blocks


def _accessible_update_agents(service, actor) -> list[dict[str, Any]]:
    service.registry.upsert_principal(actor)
    if hasattr(service.registry, 'list_accessible_agents'):
        return service.registry.list_accessible_agents(actor.id)
    return service.registry.list_agents(actor.id, False)


def _advanced_modal_blocks(service=None, *, include_share_fields: bool | None = None, current_user_id: str = '', include_advanced_fields: bool = False, include_rbac_advanced_fields: bool = False, mode: str = 'create', actor=None, selected_agent_key: str = '') -> list[dict[str, Any]]:
    catalog: dict[str, Any] = {}
    if service is not None:
        try:
            catalog = service.catalogs()
        except Exception:
            catalog = {}
    model_options = _catalog_options(catalog.get('models') or [], value_key='value', label_key='label')
    skill_options = _catalog_options(catalog.get('skills') or [])
    mcp_options = _catalog_options(catalog.get('mcp_servers') or [])
    rbac_toolset_options = _catalog_options(catalog.get('rbac_toolsets') or [])

    selected_agent = None
    selected_skills: list[str] = []
    selected_mcps: list[str] = []
    selected_shared: list[str] = []
    agent_options: list[dict[str, Any]] = []
    if mode == 'update' and service is not None and actor is not None:
        rows = _accessible_update_agents(service, actor)
        agent_options = _agent_options(rows)
        if not selected_agent_key and rows:
            selected_agent_key = str(rows[0].get('profile_name') or rows[0].get('id') or '')
        if selected_agent_key:
            try:
                selected_agent = service.get_agent(actor, selected_agent_key)
                selected_skills, selected_mcps = _agent_related(service, selected_agent['id'])
                selected_shared = [x['principal_id'] for x in selected_agent.get('acl') or [] if x.get('role') != 'owner' and x.get('principal_type') == 'user']
                if include_share_fields is None:
                    include_share_fields = selected_agent.get('access_policy') == 'shared'
            except Exception:
                selected_agent = None

    if include_share_fields is None:
        include_share_fields = False
    initial = selected_agent or {}
    current_rbac = initial.get('rbac') or {}
    blocks: list[dict[str, Any]] = []
    if mode == 'update':
        agent_block = _static_select_block('agent_key', 'Agent to update', agent_options, placeholder='Select an existing agent you own or can access', initial_value=(initial.get('profile_name') or selected_agent_key))
        agent_block['dispatch_action'] = True
        agent_block['element']['action_id'] = 'agent_key_selected'
        blocks.append(agent_block)
    else:
        blocks.append(_input_block('display_name', 'Agent name', placeholder='Ops helper'))
    if model_options:
        blocks.append(_static_select_block('model', 'Model', model_options, placeholder='Pick an available model', initial_value=initial.get('model')))
    else:
        blocks.append(_input_block('model', 'Model', placeholder='provider:model', initial_value=initial.get('model')))
    blocks.extend([
        _input_block('purpose', 'Purpose', multiline=True, placeholder='What should this agent do?', initial_value=initial.get('purpose')),
        _input_block('instructions', 'Instructions / system prompt', multiline=True, optional=(mode == 'update'), placeholder='Detailed behavior, scope, style, constraints, and rules for this agent', hint=('Optional on update. Leave unchanged if blank.' if mode == 'update' else 'Required. This becomes extra profile instructions, effectively the agent prompt/persona on top of Hermes defaults.'), initial_value=(_agent_instructions(service, initial.get('profile_name', '')) if mode == 'update' and initial else None)),
        _multi_select_block('skills', 'Installed skills', skill_options, placeholder='Select installed skills', initial_values=selected_skills) if skill_options else _input_block('skills_text', 'Installed skills', optional=True, placeholder='Comma-separated skill ids', initial_value=','.join(selected_skills)),
        _multi_select_block('mcp_servers', 'Configured MCP servers', mcp_options, placeholder='Select MCP servers', initial_values=selected_mcps) if mcp_options else _input_block('mcp_servers_text', 'Configured MCP servers', optional=True, placeholder='Comma-separated MCP ids', initial_value=','.join(selected_mcps)),
        {
            'type': 'input',
            'block_id': 'advanced_options',
            'optional': True,
            'dispatch_action': True,
            'label': {'type': 'plain_text', 'text': 'Advanced options'},
            'element': {
                'type': 'checkboxes',
                'action_id': 'advanced_options_toggle',
                'options': [_option('show', 'Show custom skill/MCP fields')],
                **({'initial_options': [_option('show', 'Show custom skill/MCP fields')]} if include_advanced_fields else {}),
            },
        },
    ])
    if include_advanced_fields:
        blocks.extend([
            _input_block('custom_skill_name', 'Custom skill name', optional=True, placeholder='my-skill'),
            _input_block('custom_skill_text', 'Custom skill text', multiline=True, optional=True, placeholder='Optional SKILL.md content'),
            _input_block('custom_mcp_name', 'Custom MCP name', optional=True, placeholder='project-mcp'),
            _input_block('custom_mcp_url', 'Custom MCP URL', optional=True, placeholder='https://mcp.example.com/mcp'),
            _static_select_block('custom_mcp_transport', 'Custom MCP transport', [_option('http', 'HTTP'), _option('sse', 'SSE')], optional=True),
        ])
    blocks.append({
        'type': 'input',
        'block_id': 'access_policy',
        'dispatch_action': True,
        'label': {'type': 'plain_text', 'text': 'Access'},
        'element': {
            'type': 'static_select',
            'action_id': 'access_policy_selected',
            'placeholder': {'type': 'plain_text', 'text': 'Choose access'},
            'options': [_option('private', 'Private'), _option('shared', 'Shared')],
            'initial_option': _option('shared' if include_share_fields else 'private', 'Shared' if include_share_fields else 'Private'),
        },
    })
    if include_share_fields:
        blocks.extend([
            {
                'type': 'input',
                'block_id': 'shared_slack_users',
                'optional': True,
                'label': {'type': 'plain_text', 'text': 'Share with Slack members'},
                'hint': {'type': 'plain_text', 'text': 'Shown only for Access=Shared. Slack will autocomplete member IDs.'},
                'element': {'type': 'multi_users_select', 'action_id': 'value', 'placeholder': {'type': 'plain_text', 'text': 'Select Slack members'}, **({'initial_users': [x.split(':')[-1] for x in selected_shared if x.startswith('slack:')]} if selected_shared else {})},
            },
            _input_block('shared_users', 'External / other platform IDs', optional=True, placeholder='telegram:123456, discord:987654, slack:U123, teams:user@example.com', hint='Optional comma-separated platform:id values for non-Slack users.', initial_value=','.join([x for x in selected_shared if not x.startswith('slack:')])) ,
        ])
    else:
        owner_text = f'<@{current_user_id}> (`slack:{current_user_id}`)' if current_user_id else 'current Slack user who opened this form'
        blocks.append({
            'type': 'section',
            'block_id': 'private_owner',
            'text': {'type': 'mrkdwn', 'text': f'*Private owner*\n{owner_text}\nOnly this Slack user will be added to RBAC users for private access.'},
        })
    blocks.extend([
        _static_select_block('risk_level', 'Autonomy', [
            _option('human-approval', 'Human approval - manual'),
            _option('agent', 'Agent supervised - smart'),
            _option('autonomous', 'Autonomous - no approval'),
        ], initial_value=initial.get('risk_level')),
        {
            'type': 'input',
            'block_id': 'rbac_install',
            'label': {'type': 'plain_text', 'text': 'Hermes RBAC'},
            'hint': {'type': 'plain_text', 'text': 'Enable profile-level user, tool, and skill authorization.'},
            'element': {
                'type': 'checkboxes',
                'action_id': 'value',
                'options': [_option('install', 'Install/update hermes-rbac in generated profile')],
                'initial_options': [_option('install', 'Install/update hermes-rbac in generated profile')] if current_rbac.get('install', True) else [],
            },
        },
        {
            'type': 'input',
            'block_id': 'rbac_fail_closed',
            'label': {'type': 'plain_text', 'text': 'RBAC fail closed'},
            'hint': {'type': 'plain_text', 'text': 'Required. If RBAC config is invalid or a user is unknown, deny access instead of accidentally allowing actions.'},
            'element': {'type': 'checkboxes', 'action_id': 'value', 'options': [_option('fail_closed', 'Fail closed on config errors / unknown users')], 'initial_options': [_option('fail_closed', 'Fail closed on config errors / unknown users')] if current_rbac.get('fail_closed', True) else []},
        },
        _input_block('rbac_role', 'Role', placeholder='viewer', hint='Required. Main role assigned to the owner/shared users.', initial_value=current_rbac.get('role') or 'viewer'),
        _input_block('rbac_users', 'Users', optional=True, placeholder='telegram:123456, discord:987654, slack:U123, teams:user@example.com', hint='Comma-separated platform:id values assigned to the selected role. Current Slack user is always added automatically.', initial_value=','.join(current_rbac.get('users') or [])),
        _multi_select_block('rbac_deny', 'Deny toolsets', rbac_toolset_options, optional=True, placeholder='Select denied toolsets', initial_values=current_rbac.get('deny') or []) if rbac_toolset_options else _input_block('rbac_deny', 'Deny toolsets', optional=True, placeholder='terminal, write_file', hint='Comma-separated tool/toolset patterns denied even if inherited.', initial_value=','.join(current_rbac.get('deny') or [])),
        _input_block('rbac_bootstrap_admins', 'Bootstrap admins', optional=True, placeholder='telegram:123456, slack:U123', hint='Break-glass admins, usually yourself.', initial_value=','.join(current_rbac.get('bootstrap_admins') or [])),
        _multi_select_block('rbac_toolsets', 'RBAC toolsets', rbac_toolset_options, optional=False, placeholder='Select RBAC toolsets', initial_values=current_rbac.get('toolsets') or []) if rbac_toolset_options else _input_block('rbac_toolsets_text', 'RBAC toolsets', optional=True, placeholder='web_search,web_extract,skill_view', initial_value=','.join(current_rbac.get('toolsets') or [])),
        _multi_select_block('rbac_skills', 'RBAC skills', skill_options, optional=False, placeholder='Select RBAC skills', initial_values=current_rbac.get('skills') or selected_skills) if skill_options else _input_block('rbac_skills_text', 'RBAC skills', optional=True, placeholder='Comma-separated skill ids', initial_value=','.join(current_rbac.get('skills') or selected_skills)),
        _static_select_block('rbac_bypass_sensitive_paths', 'Sensitive path protection', [
            _option('protect', 'Protect sensitive paths (recommended)'),
            _option('bypass', 'Bypass sensitive path protection'),
        ], optional=False, placeholder='Choose sensitive path policy', initial_value=('bypass' if current_rbac.get('bypass_sensitive_paths') else 'protect')),
        {
            'type': 'input',
            'block_id': 'rbac_advanced_options',
            'optional': True,
            'dispatch_action': True,
            'label': {'type': 'plain_text', 'text': 'Hermes RBAC advanced options'},
            'element': {
                'type': 'checkboxes',
                'action_id': 'rbac_advanced_options_toggle',
                'options': [_option('show', 'Show advanced Hermes RBAC fields')],
                **({'initial_options': [_option('show', 'Show advanced Hermes RBAC fields')]} if include_rbac_advanced_fields else {}),
            },
        },
    ])
    if include_rbac_advanced_fields:
        blocks.extend([
            _input_block('rbac_extends', 'Extends', optional=True, placeholder='viewer', hint='Comma-separated parent roles for inheritance.', initial_value=','.join(current_rbac.get('extends') or [])),
            _input_block('rbac_default_roles', 'Default roles for unknown users', optional=True, placeholder='guest', hint='Optional wildcard * user. Example: guest', initial_value=','.join(current_rbac.get('default_roles') or [])),
            _input_block('rbac_extra_roles', 'Extra roles YAML/JSON', multiline=True, optional=True, placeholder='viewer:\n  toolsets: [web_search, skill_view]\n  skills: [youtube-content]\nguest:\n  toolsets: []\n  skills: []', initial_value=current_rbac.get('extra_roles') or None),
            _input_block('rbac_user_roles', 'Explicit user role map YAML/JSON', multiline=True, optional=True, placeholder='slack:U123: [dev, viewer]\ndiscord:456: guest', initial_value=current_rbac.get('user_roles') or None),
            _input_block('rbac_skills_text', 'Additional RBAC skills', optional=True, placeholder='research/research-paper-writing, devops/sdlc-review', hint='Optional comma-separated skills not visible in the first 100 Slack picker options.', initial_value=','.join(current_rbac.get('skills') or [])),
            _input_block('rbac_identity_persons', 'Cross-platform identities YAML/JSON', multiline=True, optional=True, placeholder='george:\n  canonical: telegram:123456\n  identities:\n    - telegram:123456\n    - slack:U123', initial_value=current_rbac.get('identity_persons') or None),
        ])
    if mode == 'update':
        return _suffix_update_block_ids(blocks, _agent_form_suffix(initial if initial else None, selected_agent_key))
    return blocks

def _basic_modal_blocks(service=None) -> list[dict[str, Any]]:
    catalog: dict[str, Any] = {}
    if service is not None:
        try:
            catalog = service.catalogs()
        except Exception:
            catalog = {}
    model_options = _catalog_options(catalog.get('models') or [], value_key='value', label_key='label')
    blocks: list[dict[str, Any]] = [
        _input_block('display_name', 'Agent name', placeholder='Ops helper'),
        _static_select_block('model', 'Model', model_options, placeholder='Pick an available model') if model_options else _input_block('model', 'Model', placeholder='provider:model'),
        _input_block('purpose', 'Purpose', multiline=True, placeholder='What should this agent do?'),
        _input_block('instructions', 'Instructions / system prompt', multiline=True, placeholder='Detailed behavior, scope, style, constraints, and rules for this agent', hint='This becomes extra profile instructions, effectively the agent prompt/persona on top of Hermes defaults.'),
        _static_select_block('access_policy', 'Access', [_option('private', 'Private'), _option('shared', 'Shared')], placeholder='Choose access'),
        _static_select_block('risk_level', 'Autonomy', [_option('human-approval', 'Human approval - manual'), _option('agent', 'Agent supervised - smart'), _option('autonomous', 'Autonomous - no approval')], placeholder='Choose approval mode'),
        {
            'type': 'input',
            'block_id': 'shared_slack_users',
            'optional': True,
            'label': {'type': 'plain_text', 'text': 'Share with Slack members'},
            'hint': {'type': 'plain_text', 'text': 'Use this when Access is Shared. Slack will autocomplete member IDs.'},
            'element': {'type': 'multi_users_select', 'action_id': 'value', 'placeholder': {'type': 'plain_text', 'text': 'Select Slack members'}},
        },
        _input_block('shared_users', 'Share with platform IDs', optional=True, placeholder='telegram:123456, discord:987654, slack:U123, teams:user@example.com', hint='Optional manual list. Used when Access is Shared.'),
        {
            'type': 'input',
            'block_id': 'rbac_install',
            'optional': True,
            'label': {'type': 'plain_text', 'text': 'hermes-rbac plugin'},
            'hint': {'type': 'plain_text', 'text': 'Enable profile-level user, tool, and skill authorization.'},
            'element': {
                'type': 'checkboxes',
                'action_id': 'value',
                'options': [_option('install', 'Install/update hermes-rbac in generated profile')],
                'initial_options': [_option('install', 'Install/update hermes-rbac in generated profile')],
            },
        },
        {
            'type': 'input',
            'block_id': 'rbac_fail_closed',
            'optional': True,
            'label': {'type': 'plain_text', 'text': 'RBAC fail closed'},
            'hint': {'type': 'plain_text', 'text': 'Recommended. If RBAC config is invalid or a user is unknown, deny access instead of accidentally allowing actions.'},
            'element': {'type': 'checkboxes', 'action_id': 'value', 'options': [_option('fail_closed', 'Fail closed on config errors / unknown users')], 'initial_options': [_option('fail_closed', 'Fail closed on config errors / unknown users')]},
        },
        _input_block('rbac_bootstrap_admins', 'RBAC bootstrap admins', optional=True, placeholder='blank = yourself; or slack:U123, teams:user@example.com', hint='Break-glass admins for the generated profile.'),
    ]
    return blocks


def _modal_blocks(service=None, *, advanced: bool = False, include_share_fields: bool | None = None, current_user_id: str = '', include_advanced_fields: bool = False, include_rbac_advanced_fields: bool = False, mode: str = 'create', actor=None, selected_agent_key: str = '') -> list[dict[str, Any]]:
    # Main /agent-builder form includes advanced options inline.
    return _advanced_modal_blocks(service, include_share_fields=include_share_fields, current_user_id=current_user_id, include_advanced_fields=(include_advanced_fields or advanced), include_rbac_advanced_fields=include_rbac_advanced_fields, mode=mode, actor=actor, selected_agent_key=selected_agent_key)


async def _open_create_modal(client, trigger_id: str, service=None, *, advanced: bool = False, current_user_id: str = '') -> bool:
    if not client or not trigger_id:
        return False
    await client.views_open(
        trigger_id=trigger_id,
        view={
            'type': 'modal',
            'callback_id': 'agent_builder_create',
            'private_metadata': current_user_id,
            'title': {'type': 'plain_text', 'text': 'Agent Builder'},
            'submit': {'type': 'plain_text', 'text': 'Create'},
            'close': {'type': 'plain_text', 'text': 'Cancel'},
            'blocks': _modal_blocks(service, advanced=advanced, include_share_fields=False, current_user_id=current_user_id, include_advanced_fields=advanced),
        },
    )
    return True


async def _open_update_modal(client, trigger_id: str, service, actor, *, selected_agent_key: str = '', current_user_id: str = '') -> bool:
    if not client or not trigger_id:
        return False
    rows = _accessible_update_agents(service, actor)
    if not rows:
        return False
    await client.views_open(
        trigger_id=trigger_id,
        view={
            'type': 'modal',
            'callback_id': 'agent_builder_update',
            'private_metadata': current_user_id,
            'title': {'type': 'plain_text', 'text': 'Update Agent'},
            'submit': {'type': 'plain_text', 'text': 'Update'},
            'close': {'type': 'plain_text', 'text': 'Cancel'},
            'blocks': _modal_blocks(service, mode='update', actor=actor, selected_agent_key=selected_agent_key, current_user_id=current_user_id),
        },
    )
    return True


def _install_agent_profile_router(adapter, service) -> None:
    if not adapter or getattr(adapter, '_agent_builder_profile_router_installed', False):
        return
    original = getattr(adapter, 'handle_message', None)
    original_build_source = getattr(adapter, 'build_source', None)
    original_slack_message = getattr(adapter, '_handle_slack_message', None)
    if callable(original_build_source):
        def routed_build_source(*args, **kwargs):
            source = original_build_source(*args, **kwargs)
            _stamp_bound_agent_profile(source, service)
            return source
        adapter.build_source = routed_build_source
    if callable(original):
        async def routed_handle_message(event, *args, **kwargs):
            _stamp_bound_agent_profile(getattr(event, 'source', None), service, getattr(event, 'text', '') or '')
            return await original(event, *args, **kwargs)
        adapter.handle_message = routed_handle_message
    if callable(original_slack_message):
        async def routed_slack_message(event, payload=None):
            handled = await _handle_bound_agent_thread_message(adapter, service, event or {}, payload or {})
            if handled:
                return None
            return await original_slack_message(event, payload)
        adapter._handle_slack_message = routed_slack_message
    adapter._agent_builder_profile_router_installed = True


async def _handle_bound_agent_thread_message(adapter, service, event: dict[str, Any], payload: dict[str, Any]) -> bool:
    if event.get('bot_id') or event.get('subtype') in {'bot_message', 'message_changed', 'message_deleted'}:
        return False
    channel = str(event.get('channel') or '')
    thread = str(event.get('thread_ts') or '')
    text = str(event.get('text') or '').strip()
    user = str(event.get('user') or '')
    team = str(event.get('team') or event.get('team_id') or payload.get('team_id') or (payload.get('team') or {}).get('id') or '')
    if not channel or not thread or not user or not text or text.startswith('/'):
        return False
    try:
        actor = principal('slack', user, scope=team)
        profile = service.route(actor, channel, text, thread=thread)
    except Exception:
        return False
    session_name = f"slack-{team or 'team'}-{channel}-{thread}"
    runner = getattr(adapter, '_agent_builder_run_profile_chat', None) or _run_profile_chat
    try:
        reply = await runner(profile, session_name, text)
    except Exception as exc:
        reply = f"Agent `{profile}` failed: {str(exc)[:300]}"
    reply = (reply or '').strip() or f"Agent `{profile}` returned no text."
    client = adapter._get_client(channel, team_id=team) if hasattr(adapter, '_get_client') else None
    if client is None or not hasattr(client, 'chat_postMessage'):
        return True
    await client.chat_postMessage(channel=channel, thread_ts=thread, text=reply[:39000])
    return True


async def _run_profile_chat(profile: str, session_name: str, text: str) -> str:
    hermes = '/root/.hermes/hermes-agent/venv/bin/hermes'
    if not Path(hermes).exists():
        hermes = 'hermes'
    proc = await asyncio.create_subprocess_exec(
        hermes,
        '--profile', profile,
        'chat',
        '-Q',
        '--continue', session_name,
        '--create-if-missing',
        '--source', 'slack-agent-builder',
        '-q', text,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f'profile {profile} chat timed out')
    stdout = out.decode(errors='replace').strip()
    stderr = err.decode(errors='replace').strip()
    if proc.returncode:
        raise RuntimeError(stderr or stdout or f'hermes exited {proc.returncode}')
    return stdout


def _stamp_bound_agent_profile(source, service, text: str = '') -> None:
    if source is None or getattr(source, 'profile', None):
        return
    try:
        actor = principal(
            'slack',
            str(getattr(source, 'user_id', '') or ''),
            scope=str(getattr(source, 'scope_id', '') or ''),
            display_name=str(getattr(source, 'user_name', '') or ''),
        )
    except Exception:
        return
    conversation = str(getattr(source, 'chat_id', '') or '')
    thread = str(getattr(source, 'thread_id', '') or '')
    if not actor.user_id or not conversation:
        return
    try:
        source.profile = service.route(actor, conversation, text, thread=thread)
    except Exception:
        return


def _modal_block_key(values: dict[str, Any], block: str) -> str | None:
    if block in values:
        return block
    prefix = f'{block}__'
    matches = [k for k in values if isinstance(k, str) and k.startswith(prefix)]
    return matches[0] if matches else None


def _modal_action(values: dict[str, Any], block: str) -> dict[str, Any]:
    key = _modal_block_key(values, block)
    bucket = values.get(key) if key else {}
    if not isinstance(bucket, dict):
        return {}
    if isinstance(bucket.get('value'), dict):
        return bucket.get('value') or {}
    # Most blocks use action_id='value'. Blocks that need interactive
    # dispatch (for example Access) use a unique action_id; Slack still stores
    # the submitted value under block_id -> action_id.
    for item in bucket.values():
        if isinstance(item, dict):
            return item
    return {}


def _modal_value(values: dict[str, Any], block: str) -> str:
    item = _modal_action(values, block)
    if 'value' in item:
        return str(item.get('value') or '').strip()
    selected = item.get('selected_option') or {}
    return str(selected.get('value') or '').strip()


def _modal_multi_values(values: dict[str, Any], block: str) -> list[str]:
    item = _modal_action(values, block)
    return [str(x.get('value') or '').strip() for x in item.get('selected_options') or [] if str(x.get('value') or '').strip()]


def _modal_selected_users(values: dict[str, Any], block: str) -> list[str]:
    item = _modal_action(values, block)
    return [str(x or '').strip() for x in item.get('selected_users') or [] if str(x or '').strip()]


def _modal_checked(values: dict[str, Any], block: str, value: str) -> bool:
    item = _modal_action(values, block)
    return value in {str(x.get('value') or '') for x in item.get('selected_options') or []}


def _modal_mapping(values: dict[str, Any], block: str, label: str) -> dict:
    raw = _modal_value(values, block)
    if not raw:
        return {}
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise ValueError(f'{label} must be valid YAML/JSON') from e
    if not isinstance(parsed, dict):
        raise ValueError(f'{label} must be a mapping')
    return parsed


def _modal_actor(body: dict[str, Any]):
    user = body.get('user') or {}
    team = body.get('team') or {}
    return principal('slack', str(user.get('id') or ''), scope=str(team.get('id') or ''), display_name=str(user.get('username') or user.get('name') or ''))


async def _handle_create_modal_submission(ack, body, service):
    values = ((body.get('view') or {}).get('state') or {}).get('values') or {}
    actor = _modal_actor(body)
    _ensure_slack_builder_user(service, actor)
    skills = _modal_multi_values(values, 'skills') or _csv(_modal_value(values, 'skills_text'))
    mcps = _modal_multi_values(values, 'mcp_servers') or _csv(_modal_value(values, 'mcp_servers_text'))
    slack_share_users = [f'slack:{u}' for u in _modal_selected_users(values, 'shared_slack_users')]
    shared_users = list(dict.fromkeys([_normalize_share_target(actor, x) for x in (slack_share_users + _csv(_modal_value(values, 'shared_users')))]))
    payload = {
        'display_name': _modal_value(values, 'display_name'),
        'model': _modal_value(values, 'model'),
        'purpose': _modal_value(values, 'purpose'),
        'instructions': _modal_value(values, 'instructions'),
        'skills': skills,
        'mcp_servers': mcps,
        'risk_level': _modal_value(values, 'risk_level') or 'human-approval',
        'access_policy': _modal_value(values, 'access_policy') or 'private',
        'shared_users': shared_users,
    }
    custom_skill_name = _modal_value(values, 'custom_skill_name')
    custom_skill_text = _modal_value(values, 'custom_skill_text')
    if custom_skill_name and custom_skill_text:
        payload['custom_skills'] = [{'name': custom_skill_name, 'content': custom_skill_text}]
    custom_mcp_name = _modal_value(values, 'custom_mcp_name')
    custom_mcp_url = _modal_value(values, 'custom_mcp_url')
    if custom_mcp_name and custom_mcp_url:
        payload['custom_mcps'] = [{
            'name': custom_mcp_name,
            'url': custom_mcp_url,
            'transport': _modal_value(values, 'custom_mcp_transport') or 'http',
        }]
    try:
        rbac_install = _modal_checked(values, 'rbac_install', 'install')
        explicit_rbac_users = _csv(_modal_value(values, 'rbac_users'))
        rbac_users = list(dict.fromkeys([actor.id] + explicit_rbac_users + shared_users))
        rbac_toolsets = _modal_multi_values(values, 'rbac_toolsets') or _csv(_modal_value(values, 'rbac_toolsets_text'))
        rbac_skills = list(dict.fromkeys(_modal_multi_values(values, 'rbac_skills') + _csv(_modal_value(values, 'rbac_skills_text')))) or skills
        bootstrap_admins = list(dict.fromkeys(_csv(_modal_value(values, 'rbac_bootstrap_admins')) or [actor.id]))
        if rbac_install:
            payload['rbac'] = {
                'install': True,
                'role': _modal_value(values, 'rbac_role') or 'viewer',
                'users': rbac_users,
                'bootstrap_admins': bootstrap_admins,
                'toolsets': rbac_toolsets or ['web_search', 'web_extract', 'skill_view'],
                'skills': rbac_skills,
                'extends': _csv(_modal_value(values, 'rbac_extends')),
                'deny': _modal_multi_values(values, 'rbac_deny') or _csv(_modal_value(values, 'rbac_deny')),
                'default_roles': _csv(_modal_value(values, 'rbac_default_roles')),
                'extra_roles': _modal_mapping(values, 'rbac_extra_roles', 'Extra roles'),
                'user_roles': _modal_mapping(values, 'rbac_user_roles', 'Explicit user role map'),
                'identity_persons': _modal_mapping(values, 'rbac_identity_persons', 'Cross-platform identities'),
                'fail_closed': _modal_checked(values, 'rbac_fail_closed', 'fail_closed'),
                'bypass_sensitive_paths': _modal_value(values, 'rbac_bypass_sensitive_paths') == 'bypass',
            }
    except ValueError as e:
        await ack(response_action='errors', errors={_modal_block_key(values, 'rbac_extra_roles') or 'rbac_extra_roles': str(e)[:200]})
        return
    errors = {_modal_block_key(values, k) or k: 'Required' for k in ('display_name', 'model', 'purpose', 'instructions') if not payload.get(k)}
    if payload.get('rbac'):
        if not _modal_value(values, 'rbac_role'):
            errors[_modal_block_key(values, 'rbac_role') or 'rbac_role'] = 'Required'
        if not (payload.get('rbac') or {}).get('toolsets'):
            errors[_modal_block_key(values, 'rbac_toolsets') or 'rbac_toolsets'] = 'Select at least one RBAC toolset'
        if not (payload.get('rbac') or {}).get('skills'):
            errors[_modal_block_key(values, 'rbac_skills') or 'rbac_skills'] = 'Select at least one RBAC skill, or enter Additional RBAC skills'
        if not _modal_value(values, 'rbac_bypass_sensitive_paths'):
            errors[_modal_block_key(values, 'rbac_bypass_sensitive_paths') or 'rbac_bypass_sensitive_paths'] = 'Required'
    if errors:
        await ack(response_action='errors', errors=errors)
        return
    try:
        ag = service.create_agent(actor, payload)
        text = f"Agent created: `{ag['profile_name']}` ({ag['status']})."
    except ApprovalRequired as pending:
        text = f"Approval required. Request: `{pending}`. Open Hermes Dashboard → Plugins → Agent Builder to approve."
    except Exception as e:
        msg = str(e)[:200]
        field = 'display_name' if msg == 'access denied' else 'model'
        await ack(response_action='errors', errors={field: msg})
        return
    await ack(response_action='update', view={
        'type': 'modal',
        'title': {'type': 'plain_text', 'text': 'Agent Builder'},
        'close': {'type': 'plain_text', 'text': 'Close'},
        'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}],
    })


async def _handle_update_modal_submission(ack, body, service):
    values = ((body.get('view') or {}).get('state') or {}).get('values') or {}
    actor = _modal_actor(body)
    _ensure_slack_builder_user(service, actor)
    agent_key = _modal_value(values, 'agent_key')
    if not agent_key:
        await ack(response_action='errors', errors={'agent_key': 'Select an agent to update'})
        return
    slack_share_users = [f'slack:{u}' for u in _modal_selected_users(values, 'shared_slack_users')]
    shared_users = list(dict.fromkeys([_normalize_share_target(actor, x) for x in (slack_share_users + _csv(_modal_value(values, 'shared_users')))]))
    payload = {
        'model': _modal_value(values, 'model'),
        'purpose': _modal_value(values, 'purpose'),
        'instructions': _modal_value(values, 'instructions'),
        'skills': _modal_multi_values(values, 'skills') or _csv(_modal_value(values, 'skills_text')),
        'mcp_servers': _modal_multi_values(values, 'mcp_servers') or _csv(_modal_value(values, 'mcp_servers_text')),
        'risk_level': _modal_value(values, 'risk_level') or 'human-approval',
        'access_policy': _modal_value(values, 'access_policy') or 'private',
        'shared_users': shared_users,
    }
    custom_skill_name = _modal_value(values, 'custom_skill_name')
    custom_skill_text = _modal_value(values, 'custom_skill_text')
    if custom_skill_name and custom_skill_text:
        payload['custom_skills'] = [{'name': custom_skill_name, 'content': custom_skill_text}]
    custom_mcp_name = _modal_value(values, 'custom_mcp_name')
    custom_mcp_url = _modal_value(values, 'custom_mcp_url')
    if custom_mcp_name and custom_mcp_url:
        payload['custom_mcps'] = [{
            'name': custom_mcp_name,
            'url': custom_mcp_url,
            'transport': _modal_value(values, 'custom_mcp_transport') or 'http',
        }]
    try:
        rbac_install = _modal_checked(values, 'rbac_install', 'install')
        explicit_rbac_users = _csv(_modal_value(values, 'rbac_users'))
        rbac_users = list(dict.fromkeys([actor.id] + explicit_rbac_users + shared_users))
        rbac_toolsets = _modal_multi_values(values, 'rbac_toolsets') or _csv(_modal_value(values, 'rbac_toolsets_text'))
        rbac_skills = list(dict.fromkeys(_modal_multi_values(values, 'rbac_skills') + _csv(_modal_value(values, 'rbac_skills_text')))) or payload['skills']
        bootstrap_admins = list(dict.fromkeys(_csv(_modal_value(values, 'rbac_bootstrap_admins')) or [actor.id]))
        if rbac_install:
            payload['rbac'] = {
                'install': True,
                'role': _modal_value(values, 'rbac_role') or 'viewer',
                'users': rbac_users,
                'bootstrap_admins': bootstrap_admins,
                'toolsets': rbac_toolsets or ['web_search', 'web_extract', 'skill_view'],
                'skills': rbac_skills,
                'extends': _csv(_modal_value(values, 'rbac_extends')),
                'deny': _modal_multi_values(values, 'rbac_deny') or _csv(_modal_value(values, 'rbac_deny')),
                'default_roles': _csv(_modal_value(values, 'rbac_default_roles')),
                'extra_roles': _modal_mapping(values, 'rbac_extra_roles', 'Extra roles'),
                'user_roles': _modal_mapping(values, 'rbac_user_roles', 'Explicit user role map'),
                'identity_persons': _modal_mapping(values, 'rbac_identity_persons', 'Cross-platform identities'),
                'fail_closed': _modal_checked(values, 'rbac_fail_closed', 'fail_closed'),
                'bypass_sensitive_paths': _modal_value(values, 'rbac_bypass_sensitive_paths') == 'bypass',
            }
    except ValueError as e:
        await ack(response_action='errors', errors={_modal_block_key(values, 'rbac_extra_roles') or 'rbac_extra_roles': str(e)[:200]})
        return
    errors = {_modal_block_key(values, k) or k: 'Required' for k in ('model', 'purpose') if not payload.get(k)}
    if payload.get('rbac'):
        if not _modal_value(values, 'rbac_role'):
            errors[_modal_block_key(values, 'rbac_role') or 'rbac_role'] = 'Required'
        if not (payload.get('rbac') or {}).get('toolsets'):
            errors[_modal_block_key(values, 'rbac_toolsets') or 'rbac_toolsets'] = 'Select at least one RBAC toolset'
        if not (payload.get('rbac') or {}).get('skills'):
            errors[_modal_block_key(values, 'rbac_skills') or 'rbac_skills'] = 'Select at least one RBAC skill, or enter Additional RBAC skills'
        if not _modal_value(values, 'rbac_bypass_sensitive_paths'):
            errors[_modal_block_key(values, 'rbac_bypass_sensitive_paths') or 'rbac_bypass_sensitive_paths'] = 'Required'
    if errors:
        await ack(response_action='errors', errors=errors)
        return
    try:
        ag = service.update_agent(actor, agent_key, payload)
        text = f"Agent updated: `{ag['profile_name']}` ({ag['status']})."
    except Exception as e:
        await ack(response_action='errors', errors={'agent_key': str(e)[:200]})
        return
    await ack(response_action='update', view={
        'type': 'modal',
        'title': {'type': 'plain_text', 'text': 'Update Agent'},
        'close': {'type': 'plain_text', 'text': 'Close'},
        'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}],
    })


async def _update_create_modal(ack, body, client, service, *, access_value: str | None = None, advanced_value: bool | None = None, rbac_advanced_value: bool | None = None, agent_key_value: str | None = None):
    await ack()
    view = (body or {}).get('view') or {}
    view_id = view.get('id')
    current_user_id = str(view.get('private_metadata') or ((body or {}).get('user') or {}).get('id') or '')
    values = ((view.get('state') or {}).get('values') or {})
    current_access = _modal_value(values, 'access_policy') or 'private'
    current_advanced = _modal_checked(values, 'advanced_options', 'show')
    current_rbac_advanced = _modal_checked(values, 'rbac_advanced_options', 'show')
    include_share = (access_value or current_access) == 'shared'
    include_advanced = current_advanced if advanced_value is None else advanced_value
    include_rbac_advanced = current_rbac_advanced if rbac_advanced_value is None else rbac_advanced_value
    if not client or not view_id:
        return
    if view.get('callback_id') == 'agent_builder_update':
        actor = _modal_actor(body or {})
        selected_agent = agent_key_value or _modal_value(values, 'agent_key')
        # When the selected agent changes, rebuild from that agent's stored
        # access policy/RBAC/profile fields. Do not carry over the previous
        # agent's Access value, otherwise Slack can show Shared/Private fields
        # for the wrong agent after using the selector.
        include_share_for_update = None if agent_key_value is not None and access_value is None else include_share
        await client.views_update(
            view_id=view_id,
            hash=view.get('hash'),
            view={
                'type': 'modal',
                'callback_id': 'agent_builder_update',
                'private_metadata': current_user_id,
                'title': {'type': 'plain_text', 'text': 'Update Agent'},
                'submit': {'type': 'plain_text', 'text': 'Update'},
                'close': {'type': 'plain_text', 'text': 'Cancel'},
                'blocks': _modal_blocks(service, mode='update', actor=actor, selected_agent_key=selected_agent, include_share_fields=include_share_for_update, current_user_id=current_user_id, include_advanced_fields=include_advanced, include_rbac_advanced_fields=include_rbac_advanced),
            },
        )
        return
    await client.views_update(
        view_id=view_id,
        hash=view.get('hash'),
        view={
            'type': 'modal',
            'callback_id': 'agent_builder_create',
            'private_metadata': current_user_id,
            'title': {'type': 'plain_text', 'text': 'Agent Builder'},
            'submit': {'type': 'plain_text', 'text': 'Create'},
            'close': {'type': 'plain_text', 'text': 'Cancel'},
            'blocks': _modal_blocks(service, include_share_fields=include_share, current_user_id=current_user_id, include_advanced_fields=include_advanced, include_rbac_advanced_fields=include_rbac_advanced),
        },
    )


async def _handle_access_policy_action(ack, body, action, client, service):
    selected = ((action or {}).get('selected_option') or {}).get('value') or ''
    await _update_create_modal(ack, body, client, service, access_value=selected)


async def _handle_update_agent_selected(ack, body, action, client, service):
    selected = ((action or {}).get('selected_option') or {}).get('value') or ''
    await _update_create_modal(ack, body, client, service, agent_key_value=selected)


async def _handle_advanced_options_action(ack, body, action, client, service):
    selected = {str(x.get('value') or '') for x in (action or {}).get('selected_options') or []}
    await _update_create_modal(ack, body, client, service, advanced_value=('show' in selected))


async def _handle_rbac_advanced_options_action(ack, body, action, client, service):
    selected = {str(x.get('value') or '') for x in (action or {}).get('selected_options') or []}
    await _update_create_modal(ack, body, client, service, rbac_advanced_value=('show' in selected))


async def _agent_builder_command(ack, respond, command, service, client=None):
    await ack()
    actor = _actor(command)
    _ensure_slack_builder_user(service, actor)
    tokens = _split_text(command.get('text') or '')
    try:
        if not tokens:
            if await _open_create_modal(client, str(command.get('trigger_id') or ''), service, current_user_id=str(command.get('user_id') or '')):
                return
            await respond(_create_fallback_text(), response_type='ephemeral')
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
        if cmd in {'advanced', 'create-advanced'}:
            if await _open_create_modal(client, str(command.get('trigger_id') or ''), service, advanced=True, current_user_id=str(command.get('user_id') or '')):
                return
            await respond('Advanced create requires native Slack modal support. Use `/agent-builder create key=value ...` as fallback.', response_type='ephemeral')
            return
        if cmd == 'create':
            if len(tokens) == 1:
                if await _open_create_modal(client, str(command.get('trigger_id') or ''), service, current_user_id=str(command.get('user_id') or '')):
                    return
                await respond(_create_fallback_text(), response_type='ephemeral')
                return
            spec = _parse_key_values(tokens[1:])
            ag = service.create_agent(actor, spec)
            await respond(_fmt_agent(ag), response_type='ephemeral')
            return
        if cmd in {'update', 'updater', 'edit'}:
            if len(tokens) == 1:
                rows = _accessible_update_agents(service, actor)
                if not rows:
                    await respond(_update_fallback_text('No agents are currently owned by or shared with your Slack identity.'), response_type='ephemeral')
                    return
                if not client or not str(command.get('trigger_id') or ''):
                    await respond(_update_fallback_text(), response_type='ephemeral')
                    return
                if await _open_update_modal(client, str(command.get('trigger_id') or ''), service, actor, current_user_id=str(command.get('user_id') or '')):
                    return
                await respond(_update_fallback_text('Could not open the Slack update modal.'), response_type='ephemeral')
                return
            if len(tokens) == 2:
                rows = _accessible_update_agents(service, actor)
                if not rows:
                    await respond(_update_fallback_text('No agents are currently owned by or shared with your Slack identity.'), response_type='ephemeral')
                    return
                if not client or not str(command.get('trigger_id') or ''):
                    await respond(_update_fallback_text(), response_type='ephemeral')
                    return
                if await _open_update_modal(client, str(command.get('trigger_id') or ''), service, actor, selected_agent_key=tokens[1], current_user_id=str(command.get('user_id') or '')):
                    return
                await respond(_update_fallback_text('Could not open the Slack update modal.'), response_type='ephemeral')
                return
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
    _ensure_slack_builder_user(service, actor)
    tokens = _split_text(command.get('text') or '')
    try:
        if not tokens or tokens[0] in {'help', 'commands'}:
            await respond(_agent_usage(), response_type='ephemeral')
            return
        if tokens[0] == 'list':
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
            agent_key = _resolve_agent_key(service, actor, tokens[1])
            target = _normalize_share_target(actor, tokens[2])
            service.share_agent(actor, agent_key, target, 'user')
            await respond(f'Shared `{tokens[1]}` with `{target}`.', response_type='ephemeral')
            return
        message_text = ' '.join(tokens[1:]).strip()
        await respond(await _dispatch_as_agent(command, service, adapter, actor, tokens[0], message_text), response_type='ephemeral')
    except Exception as e:
        await respond(f'Agent unavailable or access denied: {e}', response_type='ephemeral')


async def _agent_builder_mention(event, say, service, adapter):
    parsed = _mention_to_command(event or {}, (event or {}).get('text') or '')
    if not parsed:
        return
    kind, command = parsed
    thread_ts = (event or {}).get('thread_ts') or (event or {}).get('ts') or None

    async def ack(*args, **kwargs):
        return None

    async def respond(text, **kwargs):
        payload = {'text': text}
        if thread_ts:
            payload['thread_ts'] = thread_ts
        try:
            await say(**payload)
        except TypeError:
            await say(text)

    if kind == 'agent-builder':
        await _agent_builder_command(ack, respond, command, service)
    elif kind == 'agent':
        actor = _actor(command)
        tokens = _split_text(command.get('text') or '')
        # Keep mention-mode /agent routed through the same helper where possible.
        if not tokens or tokens[0] == 'list':
            await respond(_fmt_list(service.list_agents(actor)))
        elif tokens[0] == 'chat':
            if len(tokens) < 2:
                await respond('Usage: `/agent chat <ssa-profile> [message]`')
            else:
                text = ' '.join(tokens[2:]).strip()
                await respond(await _dispatch_as_agent(command, service, adapter, actor, tokens[1], text))
        elif tokens[0] == 'share' and len(tokens) >= 3:
            agent_key = _resolve_agent_key(service, actor, tokens[1])
            target = _normalize_share_target(actor, tokens[2])
            service.share_agent(actor, agent_key, target, 'user')
            await respond(f'Shared `{tokens[1]}` with `{target}`.')
        else:
            await respond(await _dispatch_as_agent(command, service, adapter, actor, tokens[0]))


def register_slack(app, adapter, service):
    if app is None or not hasattr(app, 'command'):
        return
    _install_agent_profile_router(adapter, service)

    if hasattr(app, 'event'):
        @app.event('app_mention')
        async def handle_agent_builder_mention(ack, event, say):
            await ack()
            await _agent_builder_mention(event, say, service, adapter)

    if hasattr(app, 'view'):
        @app.view('agent_builder_create')
        async def handle_agent_builder_create_submission(ack, body):
            await _handle_create_modal_submission(ack, body, service)

        @app.view('agent_builder_update')
        async def handle_agent_builder_update_submission(ack, body):
            await _handle_update_modal_submission(ack, body, service)

    if hasattr(app, 'action'):
        @app.action('access_policy_selected')
        async def handle_agent_builder_access_policy(ack, body, action, client):
            await _handle_access_policy_action(ack, body, action, client, service)

        @app.action('agent_key_selected')
        async def handle_agent_builder_agent_key_selected(ack, body, action, client):
            await _handle_update_agent_selected(ack, body, action, client, service)

        @app.action('advanced_options_toggle')
        async def handle_agent_builder_advanced_options(ack, body, action, client):
            await _handle_advanced_options_action(ack, body, action, client, service)

        @app.action('rbac_advanced_options_toggle')
        async def handle_agent_builder_rbac_advanced_options(ack, body, action, client):
            await _handle_rbac_advanced_options_action(ack, body, action, client, service)

    @app.command('/agent-builder')
    async def handle_agent_builder(ack, respond, command, client):
        await _agent_builder_command(ack, respond, command, service, client=client)

    @app.command('/agent_builder')
    async def handle_agent_builder_underscore(ack, respond, command, client):
        await _agent_builder_command(ack, respond, command, service, client=client)

    @app.command('/agent')
    async def handle_agent(ack, respond, command):
        await _agent_command(ack, respond, command, service, adapter)
