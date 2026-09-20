import pytest
from agent_builder.auth import principal, authorize
from agent_builder.registry import Registry
from agent_builder.service import AgentService
from agent_builder.errors import AuthorizationError

def make(tmp_path):
    home=tmp_path/'home'; home.mkdir(); (home/'profiles').mkdir(); (home/'skills').mkdir(); (home/'config.yaml').write_text('mcp_servers: {}\\n')
    reg=Registry(tmp_path/'agents.db'); svc=AgentService(reg,home,allowed_models=['gpt-test'])
    owner=principal('telegram','1'); other=principal('telegram','2'); admin=principal('telegram','9')
    svc.bootstrap_admin(owner); reg.grant_global(owner.id,'agent-builder-user',owner.id); svc.bootstrap_admin(admin)
    ag=svc.create_agent(owner,{'display_name':'Pay Agent','model':'gpt-test','risk_level':'read-only'})
    return svc,owner,other,admin,ag

def test_owner_shared_admin_permissions(tmp_path):
    svc,owner,other,admin,ag=make(tmp_path)
    assert svc.get_agent(owner,ag['id'])['id']==ag['id']
    with pytest.raises(AuthorizationError): svc.get_agent(other,ag['id'])
    svc.share_agent(owner,ag['id'],other.id,'user')
    assert svc.get_agent(other,ag['id'])['id']==ag['id']
    with pytest.raises(AuthorizationError): svc.disable_agent(other,ag['id'])
    svc.disable_agent(admin,ag['id']); assert svc.registry.get_agent(ag['id'],True)['status']=='disabled'

def test_private_discovery(tmp_path):
    svc,owner,other,admin,ag=make(tmp_path)
    assert [x['id'] for x in svc.list_agents(other)]==[]
    assert ag['id'] in [x['id'] for x in svc.list_agents(admin)]
