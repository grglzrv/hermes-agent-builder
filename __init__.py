from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_builder.registry import Registry
from agent_builder.service import AgentService
from agent_builder.tools import register_tools
from agent_builder.cli import handle_cli, setup_cli


def _service(ctx):
    try:
        from plugins.plugin_storage import plugin_data_dir
        data = plugin_data_dir('agent-builder')
        db_path = data / 'registry.db'
    except Exception:
        home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
        data = home / 'plugin-data' / 'agent-builder'
        data.mkdir(parents=True, exist_ok=True)
        db_path = data / 'registry.db'
    home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
    reg = Registry(db_path)
    allowed = ctx.get_config('allowed_models', None) if hasattr(ctx, 'get_config') else None
    require = bool(ctx.get_config('require_creation_approval', True)) if hasattr(ctx, 'get_config') else True
    return AgentService(reg, home, allowed_models=allowed, require_creation_approval=require)


def register(ctx):
    service = _service(ctx)
    register_tools(ctx, service)

    if hasattr(ctx, 'register_cli_command'):
        ctx.register_cli_command(
            name='agent-builder',
            help='Manage self-service Hermes profile agents',
            setup_fn=setup_cli,
            handler_fn=handle_cli,
        )

    if hasattr(ctx, 'register_telegram_handler'):
        from integrations.telegram import register_telegram
        ctx.register_telegram_handler(lambda native, adapter: register_telegram(native, adapter, service))

    if hasattr(ctx, 'register_platform_handler'):
        from integrations.slack import register_slack
        ctx.register_platform_handler('slack', lambda native, adapter: register_slack(native, adapter, service))

    try:
        skills_dir = Path(__file__).parent / 'skills'
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / 'SKILL.md'
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)
    except Exception:
        pass
