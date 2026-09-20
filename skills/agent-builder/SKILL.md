---
name: agent-builder
description: "Use when creating or managing self-service Hermes profile agents. Enforces registry RBAC."
---

# Agent Builder

Use deterministic registry/RBAC tools. Never decide access in the model. Users can only see agents returned by `agent_builder_list`.

## Operator notes

- Model choices are provider-qualified (`provider:model`) and should come from `agent_builder_catalogs` / dashboard `/catalogs`, not free text.
- To route a conversation to a specific agent, use `/agent chat`, `/agent chat <profile-or-id>`, or `/agent chat <profile-or-id> <message>`. The Telegram integration stamps the selected profile on dispatched events so the turn opens under that generated agent profile; do not ask the model to impersonate another profile without a registry binding.
- Existing agents can be updated with `PATCH /agents/{id}` / `AgentService.update_agent`: model, purpose, instructions, skills, MCP servers, cron config, capabilities, access/risk, and hermes-rbac can be changed after creation. Setting `rbac.install=true` installs or overwrites that profile’s `plugins/hermes-rbac/roles.yaml` and enables `hermes-rbac` in the profile config.
