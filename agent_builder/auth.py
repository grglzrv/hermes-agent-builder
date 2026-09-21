from dataclasses import dataclass
from .errors import AuthorizationError, ValidationError

@dataclass(frozen=True)
class Principal:
    platform: str
    user_id: str
    scope: str = ""
    display_name: str = ""
    @property
    def id(self):
        # Slack member IDs are already workspace-scoped by Slack and are the
        # user-facing IDs people copy from Slack. Do not include the workspace
        # team id from URLs like /client/T.../C... in Agent Builder principals.
        if self.platform == "slack":
            return f"slack:{self.user_id}"
        return f"{self.platform}:{self.scope + ':' if self.scope else ''}{self.user_id}"

def principal(platform, user_id, scope="", display_name=""):
    platform=str(platform).strip().lower(); user_id=str(user_id).strip(); scope=str(scope).strip()
    if platform == "slack" and ":" in user_id:
        # Accept/canonicalize accidental slack:<team_id>:<member_id> inputs.
        user_id = user_id.split(":")[-1].strip()
    if platform not in {"telegram","discord","slack","teams","dashboard","local"} or not user_id or len(user_id)>256:
        raise ValidationError("invalid trusted platform identity")
    return Principal(platform,user_id,scope,display_name)

ROLE_ACTIONS={
 "owner":{"agent.read","agent.invoke","agent.edit","agent.share","agent.delete","agent.manage_capabilities"},
 "editor":{"agent.read","agent.invoke","agent.edit"},
 "user":{"agent.read","agent.invoke"},
 "auditor":{"agent.read"},
}
def authorize(registry, actor, action, agent_id=None):
    if registry.is_admin(actor.id): return True
    if agent_id is None:
        if action in {"agent.create","agent.list"} and registry.is_builder_user(actor.id): return True
        raise AuthorizationError("access denied")
    row=registry.get_agent(agent_id, include_deleted=True)
    if not row: raise AuthorizationError("agent unavailable")
    if action=="agent.invoke" and row["status"]!="active": raise AuthorizationError("agent unavailable")
    role=registry.get_role(agent_id,actor.id)
    if action not in ROLE_ACTIONS.get(role,set()): raise AuthorizationError("access denied")
    return True
