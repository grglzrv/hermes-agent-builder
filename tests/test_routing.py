import pytest
from agent_builder.auth import principal
from agent_builder.registry import Registry
from agent_builder.service import AgentService
from agent_builder.errors import AuthorizationError, NotFoundError

def setup(tmp_path):
    h=tmp_path/'h'; h.mkdir(); (h/'profiles').mkdir(); (h/'skills').mkdir(); (h/'config.yaml').write_text('mcp_servers: {}\n')
    s=AgentService(Registry(tmp_path/'db'),h,allowed_models=['m']); o=principal('slack','U1','T1'); u=principal('slack','U2','T1'); s.bootstrap_admin(o); s.registry.grant_global(o.id,'agent-builder-user',o.id); ag=s.create_agent(o,{'display_name':'A One','model':'m'}); return s,o,u,ag

def test_binding_revalidates_access(tmp_path):
    s,o,u,ag=setup(tmp_path)
    s.bind(o,'C1',ag['id']); assert s.route(o,'C1','hi')==ag['profile_name']
    with pytest.raises(NotFoundError): s.route(u,'C1','hi')
    s.registry.bind(u,'C1',ag['id'])
    with pytest.raises(AuthorizationError): s.route(u,'C1','hi')
    s.share_agent(o,ag['id'],u.id,'user'); assert s.route(u,'C1','hi')==ag['profile_name']
