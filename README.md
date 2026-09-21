# Hermes Agent Builder Plugin

Agent Builder is a standalone Hermes plugin for creating and managing self-service Hermes profile agents from the Hermes Dashboard and from the host CLI.

<img width="1466" height="927" alt="image" src="https://github.com/user-attachments/assets/771ff639-8e55-4b6b-8e21-d9f7e49ee4b0" />
<img width="1742" height="1282" alt="image" src="https://github.com/user-attachments/assets/106d9a22-e35f-41d6-ac83-f05428555622" />
<img width="1710" height="1276" alt="image" src="https://github.com/user-attachments/assets/6ea2e567-3bc1-4990-96e9-86eda8bdeb99" />


It provides:

- **Dashboard UI tab**: `Agent Builder` for creating, selecting, updating, and bulk-deleting generated agents.
- **Dashboard backend API**: registry, catalogs, approvals, audit log, update, delete.
- **Host CLI**: `hermes agent-builder ...` for admin operations and automation.
- **Slack and Telegram integrations**: `/agent-builder` / `/agent_builder` creation and management flows, plus `/agent chat` routing helpers.
- **Generated profile management**: creates `ssa-<slug>-<id>` profiles under the target Hermes home.
- **Registry and audit**: SQLite state in `<HERMES_HOME>/plugin-data/agent-builder/registry.db`.
- **Optional RBAC bootstrap**: can install/configure `hermes-rbac` into generated profiles when requested.

## Requirements

- Hermes Agent with plugin support and Dashboard enabled.
- Python 3.11+.
- `PyYAML>=6,<7` available in the Hermes environment. Hermes surfaces `python_dependencies` from `plugin.yaml`; install it yourself if `hermes plugins doctor` reports it missing.

## Install from GitHub

Install disabled first, inspect it, then enable:

```bash
hermes plugins install grglzrv/hermes-agent-builder --no-enable
hermes plugins list
hermes plugins enable agent-builder
```

Restart surfaces that should pick up the plugin:

```bash
# Dashboard UI/API plugin routes are mounted at dashboard startup.
systemctl --user restart hermes-dashboard.service

# Slack/Telegram native message handlers are mounted at gateway startup.
# Run this from an external shell, not from inside a gateway-delivered Hermes turn.
systemctl --user restart hermes-gateway.service
# or: hermes gateway restart
```

Open Hermes Dashboard and use the **Agent Builder** tab in the left sidebar.

## Configure

Plugin settings live under `plugins.entries.agent-builder.settings` in the active Hermes profile config. You can leave everything at defaults for the first install.

Example:

```yaml
plugins:
  enabled:
    - agent-builder
  entries:
    agent-builder:
      settings:
        require_creation_approval: true
        allowed_models:
          - openai:gpt-5.5
          - nous:Hermes-4
        admins:
          - telegram:6140241381
```

Fields:

- `require_creation_approval` — if true, new agents start as approval requests until a host admin approves them.
- `allowed_models` — optional allowlist. Empty means the plugin uses the models available to Hermes.
- `admins` — optional admin principal IDs for your deployment policy.

## Dashboard usage

The Dashboard tab lets you:

1. Click **New agent** to create a generated profile.
2. Pick model as `provider:model`, for example `openai:gpt-5.5` or `nous:Hermes-4`.
3. Attach installed skills and configured MCP servers.
4. Add optional cron JSON and RBAC settings.
5. Select one row and click **Update selected** to edit it.
6. Select one or more rows, or use the header checkbox, then click **Delete selected (N)** to bulk-delete generated agents.
7. Approve/reject pending requests from the same tab.
8. Review the audit log from **Audit log**.

Delete behavior moves the generated profile aside and marks the registry row deleted; deleted agents no longer appear in the active registry list.

## CLI usage

After enabling the plugin, Hermes registers a host CLI command:

```bash
hermes agent-builder list
hermes agent-builder requests
hermes agent-builder approve <request-id>
hermes agent-builder reject <request-id>
```

Bootstrap a trusted admin identity:

```bash
hermes agent-builder bootstrap-admin telegram <telegram-user-id>
```

Update an agent by registry id or profile name:

```bash
hermes agent-builder update ssa-support-abc123 \
  --model openai:gpt-5.5 \
  --purpose "Answer scoped operational questions for this team" \
  --instructions "Stay in scope. Cite sources. Ask before external changes." \
  --skills youtube-content,systematic-debugging \
  --mcp-servers grafana-prod \
  --access-policy shared \
  --autonomy agent \
  --shared-users telegram:123456,discord:987654
```

Delete an agent:

```bash
hermes agent-builder delete ssa-support-abc123
```

Automation can act as a specific platform principal:

```bash
hermes agent-builder update ssa-support-abc123 \
  --actor-platform telegram \
  --actor-user-id 123456 \
  --purpose "Updated purpose"
```

## Telegram usage

When the gateway is restarted with the plugin enabled:

```text
/agent-builder create
/agent-builder list
/agent-builder catalog
/agent-builder requests
/agent-builder update <ssa-profile> purpose="..." model=provider:model autonomy=agent access=shared skills=a,b mcp_servers=x,y shared_users=telegram:123
/agent-builder delete <ssa-profile>
```

## Slack usage

Configure Slack slash commands for `/agent-builder`, `/agent_builder`, and `/agent` in the Slack app manifest, then restart the gateway with the plugin enabled. Slack uses the same service/registry as Telegram and Dashboard.

```text
/agent-builder create name="Ops helper" model=nous:Hermes-4 purpose="Answer scoped operational questions"
/agent-builder list
/agent-builder catalog
/agent-builder requests
/agent-builder update <ssa-profile> purpose="..." model=provider:model autonomy=agent access=shared skills=a,b mcp_servers=x,y shared_users=slack:U123
/agent-builder delete <ssa-profile>
/agent chat <ssa-profile> [optional first message]
/agent share <ssa-profile> slack:U123
```

Telegram supports an interactive `create` wizard. Slack slash commands are single-request, so `create` takes `key=value` fields.

Agent chat helpers:

```text
/agent list
/agent chat <ssa-profile> [optional first message]
/agent share <ssa-profile> telegram:123456
```

## Principal IDs

Use `platform:id` values:

- `telegram:123456`
- `discord:987654`
- `slack:U12345`
- `teams:user@example.com`
- `dashboard:<dashboard-user>`

## Autonomy levels

- `human-approval` — drafts/recommends; humans approve sensitive, external, or irreversible actions.
- `agent` — supervised routine actions inside RBAC/tool limits; ambiguous or risky work escalates.
- `autonomous` — end-to-end execution for approved workflows inside RBAC/tool limits.

## RBAC examples

- Role: `viewer`, `dev`, `customer-admin`
- Users: `telegram:123456`, `discord:987654`, `slack:U123`
- Toolsets: `web_search`, `web_extract`, `skill_view`, `read_file`, `mcp__*`
- Skills: selected Hermes skill IDs, or leave blank to reuse selected agent skills

## Development and verification

From the plugin repo:

```bash
python3 -m py_compile \
  __init__.py \
  agent_builder/service.py \
  agent_builder/registry.py \
  agent_builder/profile_manager.py \
  agent_builder/cli.py \
  dashboard/plugin_api.py \
  integrations/telegram.py \
  integrations/slack.py

PYTHONPATH=$PWD uv run --with pytest --with pyyaml pytest -q
node --check dashboard/dist/index.js
```

## Repository layout

```text
plugin.yaml                 Native Hermes plugin manifest
__init__.py                 register(ctx): tools, CLI, Slack/Telegram, skill registration
agent_builder/              Registry, service, profile generation, CLI helpers
dashboard/manifest.json     Dashboard tab manifest
dashboard/dist/             Prebuilt Dashboard UI assets
dashboard/plugin_api.py     FastAPI routes under /api/plugins/agent-builder
integrations/telegram.py    Telegram command and routing helpers
integrations/slack.py       Slack slash-command and routing helpers
skills/agent-builder/       Bundled Hermes skill
tests/                      Plugin tests
```

## Security notes

- Runtime state is stored in `<HERMES_HOME>/plugin-data/agent-builder/`, not inside the plugin install tree.
- Secrets must stay in Hermes `.env` / secret scopes, not in plugin config or registry rows.
- Dashboard routes are protected by the Dashboard auth gate, but do not expose the dashboard publicly with untrusted plugins installed.
- Generated agents should use scoped MCP servers, skills, and RBAC roles appropriate for the target customer/team.
