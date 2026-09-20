import pytest
from agent_builder.auth import principal
from agent_builder.registry import Registry
from agent_builder.service import AgentService
from agent_builder.errors import ValidationError

def home(tmp_path):
    h=tmp_path/'h'; h.mkdir(); (h/'profiles').mkdir(); (h/'skills').mkdir(); (h/'config.yaml').write_text('mcp_servers:\n  grafana-prod:\n    url: http://grafana.local\n')
    return h

def svc(tmp_path):
    h=home(tmp_path); s=AgentService(Registry(tmp_path/'db.sqlite'),h,allowed_models=['m']); a=principal('telegram','1'); s.bootstrap_admin(a); s.registry.grant_global(a.id,'agent-builder-user',a.id); return s,a,h

def test_profile_name_and_files(tmp_path):
    s,a,h=svc(tmp_path); ag=s.create_agent(a,{'display_name':'PROD2 Investigator','model':'m','mcp_servers':['grafana-prod']})
    assert ag['profile_name'].startswith('ssa-prod2-investigator-')
    p=h/'profiles'/ag['profile_name']; assert (p/'config.yaml').exists(); assert (p/'SOUL.md').exists()

def test_invalid_model_and_mcp(tmp_path):
    s,a,h=svc(tmp_path)
    with pytest.raises(ValidationError): s.create_agent(a,{'display_name':'Bad','model':'x'})
    with pytest.raises(ValidationError): s.create_agent(a,{'display_name':'Bad','model':'m','mcp_servers':['evil']})

def test_path_traversal_rejected(tmp_path):
    s,a,h=svc(tmp_path)
    with pytest.raises(ValidationError): s.pm.profile_path('ssa-../evil')
