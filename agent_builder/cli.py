from __future__ import annotations
import argparse
import getpass
import json
import os
from pathlib import Path
from .auth import principal
from .registry import Registry
from .service import AgentService


def _csv(value):
    if value is None: return None
    return [x.strip() for x in str(value).split(',') if x.strip()]


def _json(value, label):
    if value is None: return None
    try: return json.loads(value)
    except json.JSONDecodeError as e: raise SystemExit(f'{label} must be valid JSON: {e}') from e


def _home(args=None) -> Path:
    explicit=getattr(args,'home',None) if args is not None else None
    if explicit: return Path(explicit).expanduser()
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')


def _service(args=None) -> AgentService:
    home=_home(args)
    return AgentService(Registry(home/'plugin-data'/'agent-builder'/'registry.db'),home)


def _local_admin(service: AgentService):
    actor=principal('local',getpass.getuser(),display_name=getpass.getuser())
    service.registry.upsert_principal(actor)
    if not service.registry.is_admin(actor.id):
        raise PermissionError(f'{actor.id} is not a Hermes administrator; bootstrap it explicitly first')
    return actor


def _actor_from_flags(args, service):
    if getattr(args,'actor_user_id',None):
        return principal(args.actor_platform,args.actor_user_id,getattr(args,'actor_scope','') or '')
    return _local_admin(service)


def _add_actor_flags(parser):
    parser.add_argument('--home', default='', help='Hermes home to manage (defaults to active HERMES_HOME)')
    parser.add_argument('--actor-platform', default='local', choices=['local','dashboard','telegram','discord','slack','teams'], help='Platform identity performing the action')
    parser.add_argument('--actor-user-id', default='', help='User id performing the action; omitted means local host admin')
    parser.add_argument('--actor-scope', default='', help='Optional workspace/team/chat scope for the actor')


def setup_cli(parser):
    sub=parser.add_subparsers(dest='agent_builder_command',required=True)
    sub.add_parser('list',help='List registered agents')
    sub.add_parser('requests',help='Print pending approval requests as JSON Lines')
    approve=sub.add_parser('approve',help='Approve a pending request (Hermes host admins only)')
    approve.add_argument('request_id')
    reject=sub.add_parser('reject',help='Reject a pending request (Hermes host admins only)')
    reject.add_argument('request_id')
    bootstrap=sub.add_parser('bootstrap-admin',help='Grant a trusted platform identity Hermes admin rights')
    bootstrap.add_argument('platform')
    bootstrap.add_argument('user_id')
    bootstrap.add_argument('--scope',default='')
    update=sub.add_parser('update',help='Update a generated agent by id or profile name')
    _add_actor_flags(update)
    update.add_argument('agent')
    update.add_argument('--model', help='provider:model, for example openai:gpt-5.5 or nous:Hermes-4')
    update.add_argument('--purpose')
    update.add_argument('--description')
    update.add_argument('--instructions')
    update.add_argument('--skills', help='Comma-separated skill ids. Example: youtube-content,systematic-debugging')
    update.add_argument('--mcp-servers', help='Comma-separated MCP server ids. Example: confluence,grafana-prod')
    update.add_argument('--access-policy', choices=['private','shared'])
    update.add_argument('--autonomy', dest='risk_level', choices=['human-approval','agent','autonomous'])
    update.add_argument('--shared-users', help='Comma-separated platform:id users. Empty string clears non-owner shares.')
    update.add_argument('--cron-json', help='Cron config JSON')
    update.add_argument('--rbac-json', help='hermes-rbac config JSON')
    update.add_argument('--capabilities', help='Comma-separated capability ids')
    update.add_argument('--set-json', help='Raw JSON object merged last into the update payload')
    delete=sub.add_parser('delete',help='Delete a generated agent by id or profile name')
    _add_actor_flags(delete)
    delete.add_argument('agent')


def _json_row(row):
    if row is None: return None
    value=dict(row)
    for key in ('payload','metadata'):
        raw=value.get(key)
        if isinstance(raw,str):
            try: value[key]=json.loads(raw)
            except json.JSONDecodeError: pass
    return value


def _update_payload(args):
    payload={}
    for key in ('model','purpose','description','instructions','access_policy','risk_level'):
        value=getattr(args,key,None)
        if value is not None: payload[key]=value
    if args.skills is not None: payload['skills']=_csv(args.skills)
    if args.mcp_servers is not None: payload['mcp_servers']=_csv(args.mcp_servers)
    if args.shared_users is not None: payload['shared_users']=_csv(args.shared_users)
    if args.capabilities is not None: payload['capabilities']=_csv(args.capabilities)
    cron=_json(args.cron_json,'--cron-json')
    rbac=_json(args.rbac_json,'--rbac-json')
    raw=_json(args.set_json,'--set-json')
    if cron is not None: payload['cron']=cron
    if rbac is not None: payload['rbac']=rbac
    if raw is not None:
        if not isinstance(raw,dict): raise SystemExit('--set-json must be a JSON object')
        payload.update(raw)
    return payload


def handle_cli(args):
    service=_service(args)
    command=args.agent_builder_command
    if command=='bootstrap-admin':
        actor=principal(args.platform,args.user_id,args.scope)
        service.bootstrap_admin(actor)
        service.registry.grant_global(actor.id,'agent-builder-user',actor.id)
        print(json.dumps({'ok':True,'principal':actor.id,'roles':['hermes-admin','agent-builder-owner','agent-builder-user']},separators=(',',':')))
        return 0
    actor=_actor_from_flags(args, service)
    if command=='list':
        for row in service.list_agents(actor): print(json.dumps(_json_row(row),separators=(',',':')))
        return 0
    if command=='requests':
        for row in service.pending_requests(actor): print(json.dumps(_json_row(row),separators=(',',':')))
        return 0
    if command=='approve':
        req=service.approve_request(actor,args.request_id)
        agent=service.registry.get_agent(req['agent_id'],include_deleted=True)
        print(json.dumps({'ok':True,'request':_json_row(req),'agent':_json_row(agent)},separators=(',',':')))
        return 0
    if command=='reject':
        req=service.deny_request(actor,args.request_id)
        print(json.dumps({'ok':True,'request':_json_row(req)},separators=(',',':')))
        return 0
    if command=='update':
        ag=service.update_agent(actor,args.agent,_update_payload(args))
        print(json.dumps({'ok':True,'updated':ag['id'],'profile_name':ag['profile_name'],'status':ag['status'],'access_policy':ag['access_policy']},separators=(',',':')))
        return 0
    if command=='delete':
        ag=service.get_agent(actor,args.agent)
        service.delete_agent(actor,args.agent)
        print(json.dumps({'ok':True,'deleted':ag['id'],'profile_name':ag['profile_name']},separators=(',',':')))
        return 0
    raise SystemExit(f'unsupported command: {command}')


def main(argv=None):
    parser=argparse.ArgumentParser(prog='hermes agent-builder')
    setup_cli(parser)
    return handle_cli(parser.parse_args(argv))


if __name__=='__main__':
    raise SystemExit(main())
