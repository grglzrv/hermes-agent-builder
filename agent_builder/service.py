from __future__ import annotations
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from .auth import authorize, principal
from .catalog import list_mcps, list_models, list_rbac_toolsets, list_skills, parse_model_choice
from .errors import ApprovalRequired, AuthorizationError, NotFoundError, ValidationError
from .profile_manager import ProfileManager
from .rbac import normalize_rbac_spec, read_rbac_spec
from .validators import display_name, slug, text, selections

AUTONOMY_LEVELS={'human-approval','autonomous','agent'}
LEGACY_RISK_MAP={'read-only':'human-approval','low':'human-approval','medium':'human-approval','high':'autonomous'}
SHAREABLE_PLATFORMS=['telegram','discord','slack','teams']

def normalize_share_users(values):
    if values is None: return None
    if isinstance(values,str):
        values=[x.strip() for x in values.replace('\n',',').split(',') if x.strip()]
    out=[]
    for raw in values or []:
        parts=str(raw).strip().split(':',1)
        if len(parts)!=2 or parts[0].lower() not in SHAREABLE_PLATFORMS or not parts[1].strip():
            raise ValidationError('shared users must be platform:id entries such as telegram:123, discord:456, slack:U123, teams:user@example.com')
        out.append(principal(parts[0],parts[1]).id)
    return list(dict.fromkeys(out))

AUTONOMY_CHOICES=[
    {'value':'human-approval','label':'Human approval - manual','description':'Manual approval for actions.'},
    {'value':'agent','label':'Agent supervised - smart','description':'Smart approval for scoped actions.'},
    {'value':'autonomous','label':'Autonomous - no approval','description':'No approval required inside configured limits.'},
]

def normalize_autonomy(value):
    raw=str(value or 'human-approval').strip().lower().replace(' ', '-')
    raw=LEGACY_RISK_MAP.get(raw, raw)
    if raw not in AUTONOMY_LEVELS: raise ValidationError('invalid autonomy level')
    return raw

class AgentService:
    def __init__(self, registry, hermes_home: Path, *, allowed_models=None, require_creation_approval=False):
        self.registry=registry; self.pm=ProfileManager(Path(hermes_home)); self.allowed_models=None if allowed_models is None else set(allowed_models); self.require_creation_approval=require_creation_approval
    def bootstrap_admin(self, actor):
        self.registry.upsert_principal(actor); self.registry.grant_global(actor.id,'hermes-admin',actor.id); self.registry.grant_global(actor.id,'agent-builder-owner',actor.id)
    def catalogs(self):
        models=list_models(self.pm.home)
        if self.allowed_models:
            models=[m for m in models if m['value'] in self.allowed_models or m['model'] in self.allowed_models]
            present={m['value'] for m in models} | {m['model'] for m in models}
            models.extend({'value': m, 'provider': '', 'model': m, 'label': m, 'authenticated': True} for m in sorted(self.allowed_models - present))
        return {'skills': list_skills(self.pm.home), 'mcp_servers': list_mcps(self.pm.home), 'models': models, 'model_values': [m['value'] for m in models], 'autonomy_levels': [c['value'] for c in AUTONOMY_CHOICES], 'autonomy_choices': AUTONOMY_CHOICES, 'shareable_platforms': SHAREABLE_PLATFORMS, 'share_user_example': 'telegram:123456, discord:987654, slack:U123, teams:user@example.com', 'rbac_toolsets': list_rbac_toolsets(), 'rbac_plugin': {'repo': 'https://github.com/TTomas78/hermes-rbac'}}
    def create_agent(self, actor, spec):
        self.registry.upsert_principal(actor); authorize(self.registry,actor,'agent.create')
        name=display_name(spec.get('display_name') or spec.get('name') or '')
        cats=self.catalogs(); model_provider, model, model_value=parse_model_choice(spec.get('model') or '', cats['models'])
        if self.allowed_models and model_value not in self.allowed_models and model not in self.allowed_models: raise ValidationError('unsupported model')
        valid_skills=list_skills(self.pm.home); valid_mcps=list_mcps(self.pm.home)
        skills=selections(spec.get('skills') or [], valid_skills, 'skill')
        mcps=selections(spec.get('mcp_servers') or [], valid_mcps, 'MCP server')
        custom_skills=[]
        for item in list(spec.get('custom_skills') or [])[:8]:
            if not isinstance(item,dict): raise ValidationError('custom skill must be an object')
            custom_skills.append({'name':slug(item.get('name') or ''),'content':text(item.get('content',''),30000)})
        custom_mcps=[]
        for item in list(spec.get('custom_mcps') or [])[:8]:
            if not isinstance(item,dict): raise ValidationError('custom MCP must be an object')
            name=slug(item.get('name') or '')
            url=text(item.get('url',''),2000); parsed=urlsplit(url)
            if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.username or parsed.password:
                raise ValidationError('custom MCP URL must be an http(s) URL without embedded credentials')
            transport=str(item.get('transport') or 'http').lower()
            if transport not in {'http','sse'}: raise ValidationError('custom MCP transport must be http or sse')
            custom_mcps.append({'name':name,'url':url,'transport':transport})
        spec=dict(spec); spec['custom_skills']=custom_skills; spec['custom_mcps']=custom_mcps
        rbac=normalize_rbac_spec(spec.get('rbac'), selected_skills=skills)
        risk=normalize_autonomy(spec.get('risk_level') or spec.get('autonomy_level'))
        aid='agt_'+uuid.uuid4().hex[:20]; prof=f"ssa-{slug(name)}"
        row={'id':aid,'profile_name':prof,'display_name':name,'description':text(spec.get('description',''),1000),'purpose':text(spec.get('purpose',''),2000),'owner_id':actor.id,'owner_platform':actor.platform,'model':model_value,'status':'creating','risk_level':risk,'access_policy':spec.get('access_policy') or 'private','approval_required': bool(spec.get('approval_required')),'created_by':actor.id}
        need_approval=self.require_creation_approval or row['approval_required']
        if need_approval:
            row['status']='pending_approval'; self.registry.insert_agent(row,skills,mcps,spec.get('capabilities') or [])
            rid=self.registry.request_approval(aid,'agent.create',{'profile_name':prof,'spec':{k:v for k,v in spec.items() if 'secret' not in k.lower()}},actor.id)
            self.registry.audit(actor,'agent.create','pending',aid,metadata={'approval_request_id':rid})
            raise ApprovalRequired(rid)
        self.registry.insert_agent(row,skills,mcps,spec.get('capabilities') or [])
        try:
            self.pm.create_profile(profile_name=prof,display_name=name,provider=model_provider,model=model,purpose=row['purpose'],instructions=text(spec.get('instructions',''),4000),skills=skills,mcp_servers=mcps,custom_skills=custom_skills,custom_mcps=custom_mcps,rbac=rbac)
            self.registry.update_status(aid,'active')
            shared=normalize_share_users(spec.get('shared_users'))
            if shared is not None:
                for pid in shared:
                    self.registry.share(aid,pid,'user',actor.id)
            self.registry.audit(actor,'agent.create','allow',aid,metadata={'profile_name':prof,'shared_users':shared or []})
        except BaseException as e:
            self.registry.update_status(aid,'error'); self.registry.audit(actor,'agent.create','error',aid,metadata={'error':str(e)[:300]}); raise
        return self.get_agent(actor,aid)
    def approve_request(self, actor, request_id):
        authorize(self.registry,actor,'agent.manage_capabilities',None) if not self.registry.is_admin(actor.id) else True
        req=self.registry.decide(request_id,'approved',actor.id)
        if req['request_type']=='agent.create':
            ag=self.registry.get_agent(req['agent_id'],include_deleted=True)
            spec=json.loads(req.get('payload') or '{}').get('spec') or {}
            provider, model, _value=parse_model_choice(ag['model'], self.catalogs()['models'])
            rbac=normalize_rbac_spec(spec.get('rbac'), selected_skills=spec.get('skills') or [])
            try:
                self.pm.create_profile(profile_name=ag['profile_name'],display_name=ag['display_name'],
                                       provider=provider,model=model,purpose=ag['purpose'],
                                       instructions=text(spec.get('instructions',''),4000),
                                       skills=spec.get('skills') or (),
                                       mcp_servers=spec.get('mcp_servers') or (),
                                       custom_skills=spec.get('custom_skills') or (),
                                       custom_mcps=spec.get('custom_mcps') or (),
                                       rbac=rbac)
                self.registry.update_status(ag['id'],'active')
            except BaseException:
                self.registry.update_status(ag['id'],'error')
                raise
        self.registry.audit(actor,'approval.approve','allow',req.get('agent_id'),metadata={'request_id':request_id})
        return req
    def deny_request(self, actor, request_id):
        if not self.registry.is_admin(actor.id): raise AuthorizationError('access denied')
        req=self.registry.decide(request_id,'denied',actor.id); self.registry.audit(actor,'approval.deny','allow',req.get('agent_id'),metadata={'request_id':request_id}); return req
    def pending_requests(self, actor):
        self.registry.upsert_principal(actor)
        if not self.registry.is_admin(actor.id): raise AuthorizationError('access denied')
        return self.registry.pending()
    def _attach_runtime_state(self, row):
        try:
            row=dict(row)
            profile_name=row.get('profile_name')
            if profile_name:
                row['rbac']=read_rbac_spec(self.pm.profile_path(profile_name))
            acl=self.registry.acl(row['id']) if row.get('id') else []
            row['acl']=acl
            non_owner=[x for x in acl if x.get('role')!='owner' and x.get('principal_type')=='user']
            row['access_policy']='shared' if non_owner else 'private'
            row['shared_users']=[x.get('principal_id') for x in non_owner if x.get('principal_id')]
        except Exception:
            row=dict(row)
            row.setdefault('rbac',None)
            row.setdefault('acl',[])
            row.setdefault('shared_users',[])
        return row
    def list_agents(self, actor):
        self.registry.upsert_principal(actor)
        return [self._attach_runtime_state(row) for row in self.registry.list_agents(actor.id, self.registry.is_admin(actor.id))]
    def get_agent(self, actor, key):
        ag=self.registry.get_agent(key)
        if not ag: raise NotFoundError('agent not found')
        authorize(self.registry,actor,'agent.read',ag['id']); ag=self._attach_runtime_state(ag); return ag
    def update_agent(self, actor, key, spec):
        ag=self.registry.get_agent(key)
        if not ag: raise NotFoundError('agent not found')
        authorize(self.registry,actor,'agent.edit',ag['id'])
        cats=self.catalogs(); fields={}; profile_updates={}
        if 'model' in spec:
            provider, model, value=parse_model_choice(spec.get('model') or '', cats['models'])
            if self.allowed_models and value not in self.allowed_models and model not in self.allowed_models: raise ValidationError('unsupported model')
            fields['model']=value; profile_updates.update({'provider':provider,'model':model})
        if 'purpose' in spec:
            fields['purpose']=text(spec.get('purpose',''),2000); profile_updates['purpose']=fields['purpose']
        if 'description' in spec: fields['description']=text(spec.get('description',''),1000)
        if 'instructions' in spec: profile_updates['instructions']=text(spec.get('instructions',''),4000)
        if 'risk_level' in spec or 'autonomy_level' in spec:
            risk=normalize_autonomy(spec.get('risk_level') if 'risk_level' in spec else spec.get('autonomy_level'))
            fields['risk_level']=risk
        valid_skills=list_skills(self.pm.home); valid_mcps=list_mcps(self.pm.home)
        skills=None; mcps=None; shared=None
        if 'access_policy' in spec:
            ap=spec.get('access_policy') or 'private'
            if ap not in {'private','shared'}: raise ValidationError('invalid access policy')
            fields['access_policy']=ap
            if ap == 'private' and 'shared_users' not in spec:
                shared=[]
        if 'shared_users' in spec:
            shared=normalize_share_users(spec.get('shared_users'))
        if 'skills' in spec:
            skills=selections(spec.get('skills') or [], valid_skills, 'skill'); profile_updates['skills']=skills
        if 'mcp_servers' in spec:
            mcps=selections(spec.get('mcp_servers') or [], valid_mcps, 'MCP server'); profile_updates['mcp_servers']=mcps
        if 'cron' in spec: profile_updates['cron']=spec.get('cron') or {}
        if 'custom_skills' in spec:
            custom_skills=[]
            for item in list(spec.get('custom_skills') or [])[:8]:
                if not isinstance(item,dict): raise ValidationError('custom skill must be an object')
                custom_skills.append({'name':slug(item.get('name') or ''),'content':text(item.get('content',''),30000)})
            if custom_skills: profile_updates['custom_skills']=custom_skills
        if 'custom_mcps' in spec:
            custom_mcps=[]
            for item in list(spec.get('custom_mcps') or [])[:8]:
                if not isinstance(item,dict): raise ValidationError('custom MCP must be an object')
                name=slug(item.get('name') or '')
                url=text(item.get('url',''),2000); parsed=urlsplit(url)
                if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.username or parsed.password:
                    raise ValidationError('custom MCP URL must be an http(s) URL without embedded credentials')
                transport=str(item.get('transport') or 'http').lower()
                if transport not in {'http','sse'}: raise ValidationError('custom MCP transport must be http or sse')
                custom_mcps.append({'name':name,'url':url,'transport':transport})
            if custom_mcps: profile_updates['custom_mcps']=custom_mcps
        if 'rbac' in spec:
            profile_updates['rbac']=normalize_rbac_spec(spec.get('rbac'), selected_skills=skills if skills is not None else [])
            if profile_updates['rbac'] is None: profile_updates.pop('rbac')
        self.pm.update_profile(ag['profile_name'], **profile_updates)
        self.registry.update_agent(ag['id'], fields, skills=skills, integrations=mcps, capabilities=spec.get('capabilities') if 'capabilities' in spec else None)
        if shared is not None:
            self.registry.replace_user_shares(ag['id'],shared,'user',actor.id)
        self.registry.audit(actor,'agent.update','allow',ag['id'],metadata={'fields':sorted(spec.keys())})
        return self.get_agent(actor,ag['id'])
    def share_agent(self, actor, key, principal_id, role='user'):
        ag=self.registry.get_agent(key)
        if not ag: raise NotFoundError('agent not found')
        authorize(self.registry,actor,'agent.share',ag['id']); self.registry.share(ag['id'],principal_id,role,actor.id); self.registry.audit(actor,'agent.share','allow',ag['id'],metadata={'principal_id':principal_id,'role':role})
    def disable_agent(self, actor, key):
        ag=self.registry.get_agent(key); authorize(self.registry,actor,'agent.delete',ag['id']); self.pm.disable_profile(ag['profile_name']); self.registry.update_status(ag['id'],'disabled'); self.registry.audit(actor,'agent.disable','allow',ag['id'])
    def delete_agent(self, actor, key):
        ag=self.registry.get_agent(key); authorize(self.registry,actor,'agent.delete',ag['id']); self.pm.delete_profile(ag['profile_name']); self.registry.update_status(ag['id'],'deleted'); self.registry.audit(actor,'agent.delete','allow',ag['id'])
    def bind(self, actor, conversation, key, thread=''):
        ag=self.registry.get_agent(key); authorize(self.registry,actor,'agent.invoke',ag['id']); self.registry.bind(actor,conversation,ag['id'],thread); self.registry.audit(actor,'agent.bind','allow',ag['id']); return ag
    def unbind(self, actor, conversation, thread=''):
        self.registry.unbind(actor,conversation,thread); self.registry.audit(actor,'agent.unbind','allow')
    def route(self, actor, conversation, message, thread=''):
        aid=self.registry.binding(actor,conversation,thread)
        if not aid: raise NotFoundError('no agent selected')
        ag=self.registry.get_agent(aid); authorize(self.registry,actor,'agent.invoke',aid); self.registry.audit(actor,'agent.invoke','allow',aid); return ag['profile_name']
