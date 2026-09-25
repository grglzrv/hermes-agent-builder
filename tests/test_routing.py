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


def test_admin_cannot_route_or_bind_unshared_agent_by_admin_bypass(tmp_path):
    h=tmp_path/'h2'; h.mkdir(); (h/'profiles').mkdir(); (h/'skills').mkdir(); (h/'config.yaml').write_text('mcp_servers: {}\n')
    s=AgentService(Registry(tmp_path/'db2'),h,allowed_models=['m'])
    owner=principal('slack','OWNER','T1')
    admin=principal('slack','ADMIN','T1')
    s.bootstrap_admin(owner)
    s.bootstrap_admin(admin)
    ag=s.create_agent(owner,{'display_name':'Owner Only','model':'m'})
    with pytest.raises(AuthorizationError):
        s.bind(admin,'C1',ag['id'])
    s.registry.bind(admin,'C1',ag['id'])
    with pytest.raises(AuthorizationError):
        s.route(admin,'C1','hi')
    s.share_agent(owner,ag['id'],admin.id,'user')
    s.bind(admin,'C1',ag['id'])
    assert s.route(admin,'C1','hi')==ag['profile_name']


def test_approval_path_grants_shared_users_acl(tmp_path):
    from agent_builder.errors import ApprovalRequired
    h=tmp_path/'h3'; h.mkdir(); (h/'profiles').mkdir(); (h/'skills').mkdir(); (h/'config.yaml').write_text('mcp_servers: {}\n')
    s=AgentService(Registry(tmp_path/'db3'),h,allowed_models=['m'],require_creation_approval=True)
    admin=principal('slack','ADMIN','T1')
    owner=principal('slack','OWNER','T1')
    shared=principal('slack','SHARED','T1')
    s.bootstrap_admin(admin)
    s.registry.upsert_principal(owner); s.registry.grant_global(owner.id,'agent-builder-user',admin.id)
    try:
        s.create_agent(owner,{'display_name':'Shared Approval','model':'m','shared_users':['slack:SHARED']})
    except ApprovalRequired as pending:
        rid=str(pending)
    else:
        raise AssertionError('expected approval')
    s.approve_request(admin,rid)
    rows=s.registry.list_accessible_agents(shared.id)
    assert [r['profile_name'] for r in rows]==['ssa-shared-approval']
    assert s.registry.get_role(rows[0]['id'], shared.id)=='user'
