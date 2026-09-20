# Agent Builder MVP implementation plan

## Architecture assessment

Hermes already provides the required edge extension surfaces:

- General plugins under `~/.hermes/plugins/<name>` provide slash commands, CLI commands, hooks, and native platform handler factories.
- Dashboard plugins provide an authenticated tab and FastAPI routes under `/api/plugins/<name>/`.
- `hermes_cli.profiles.create_profile()` and `delete_profile()` provide validated, staged, atomic profile publication and multiplex hot-rescan notification.
- `gateway.pairing.PairingStore` and dashboard `/api/pairing` endpoints provide trusted platform enrollment.
- `hermes_cli.mcp_security.validate_mcp_server_entry()` validates MCP definitions. The self-service path will be stricter and only copy administrator-catalog entries.
- Skills are filesystem packages with `SKILL.md`; the builder will list approved installed skills and copy only validated skill directories.
- One-shot `hermes chat --format stream-json` returns a session ID. The router can resume an isolated per-conversation/per-agent session without importing private gateway internals.

No Hermes core files will be modified. The platform service ships as a standalone plugin.

## Security boundaries

1. Native Telegram/Slack/Teams SDK callbacks extract immutable platform user IDs.
2. Hermes Pairing approval is checked before builder enrollment and every operation.
3. SQLite RBAC checks run before registry lookup results are disclosed and before Hermes profile execution.
4. The model never selects identities, roles, agents, capabilities, skills, MCP definitions, or approval decisions.
5. User-to-agent ACLs and agent-to-system capability grants are separate tables and policy paths.
6. Self-service MCP and capabilities come only from administrator-managed catalogs. Arbitrary MCP URLs/commands are rejected.
7. Profile names are generated as immutable `ssa-<slug>-<6-hex>` values, max 63 characters.
8. Profile writes use Hermes staged profile creation plus atomic file replacement. SQLite uses WAL, foreign keys, busy timeout, transactions, and unique constraints.
9. Secrets are represented only by credential references. Raw secret-shaped values are rejected from metadata and generated files.
10. Ordinary generated profiles have no builder plugin enabled and cannot access the registry.

## Vertical slices

### Slice 1: Registry, identity, RBAC

- SQLite migrations for principals, global roles, agents, ACLs, capabilities, integrations, bindings, wizard state, approvals, and audit events.
- Immutable normalized identities: `telegram:<id>`, `slack:<id>`, `teams:<aad-id>`.
- Roles: `agent-builder-user`, `agent-builder-owner`, and configured `hermes-admin`.
- Agent ACL roles: owner, editor, user, auditor.
- Visibility-filtered listing and deterministic `authorize(principal, action, agent)`.

### Slice 2: Profile builder and catalogs

- Model, skill, MCP, and capability catalogs.
- Transactional create: reserve registry row in `creating`; create Hermes profile; write safe config/SOUL/AGENTS/skills; activate row; audit.
- Compensating rollback removes staged/published profile and registry rows on failure.
- Update, disable, soft-delete, share/unshare, and capability approval operations.

### Slice 3: Router

- `/agent <ssa-profile> [message]` selects or invokes an agent.
- Conversation bindings map `(platform, conversation_id, principal)` to agent and Hermes session ID.
- RBAC and lifecycle state are rechecked before every execution.
- Runtime uses argv-only subprocess execution and stream-json parsing; no shell.

### Slice 4: Telegram-first wizard

- `/agent-builder` opens an inline-button menu.
- Creation wizard: display name, purpose, model, skills, MCP integrations, access, risk policy, instructions, review/create.
- Management: list/info/share/unshare/disable/delete.
- Plugin callback data is namespaced and bounded; wizard state is stored server-side.
- Slack Block Kit and Teams Adaptive Card adapters expose equivalent command-form MVP flows where configured.

### Slice 5: Dashboard

- Authenticated Agent Registry tab.
- List agents, owners, ACLs, status, capabilities, pending capability approvals, and audit events.
- Approve/reject capability requests.
- Pairing status is displayed by reading `PairingStore`; builder enrollment requires Pairing approval.

## Testing

- Unit: validation, identity normalization, RBAC matrix, registry visibility, catalogs, audit redaction.
- Creation: success, duplicate display names, invalid names/models/capabilities, filesystem and DB rollback.
- Routing: allow/deny/unknown/disabled/deleted, binding reuse and revalidation.
- Security: traversal, shell metacharacters, forged identity text, prompt RBAC bypass, capability escalation, cross-agent access, raw secret rejection.
- Concurrency: parallel creates, ACL upserts, profile name collision retry.
- Integration: real temporary Hermes homes, real plugin discovery, real SQLite, real profile creation, mocked model subprocess boundary.
- Dashboard API: authenticated host assumption plus deterministic admin role checks for mutation endpoints.

## Known V1 limits

- Pairing proves access to the Hermes gateway. Builder roles remain a separate explicit registry grant; Pairing alone never grants owner/admin.
- Telegram is the full interactive wizard. Slack and Teams are optional command/card adapters and may require tenant-specific app configuration.
- Generated profiles do not receive shared infrastructure credentials. Administrators must provision approved credential references through the capability catalog and underlying service controls.
- Runtime routing uses a bounded Hermes subprocess per turn. A future in-process public profile-routing API can replace this behind the `AgentRuntime` interface.
- Group identity expansion and cross-platform canonical-person mapping are deferred.

## Migration path

Repository and authorization interfaces isolate persistence and policy. V2 can replace SQLite with PostgreSQL, local RBAC with OpenFGA/SpiceDB, subprocess runtime with remote workers, static catalogs with an administrative service, and platform identities with a canonical corporate identity mapper without changing command contracts.
