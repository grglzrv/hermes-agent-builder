from __future__ import annotations
import asyncio
from agent_builder.auth import principal
from agent_builder.errors import ApprovalRequired, AuthorizationError, NotFoundError, ValidationError

WIZ={}
FIELDS=['display_name','purpose','model']

def _chat(update): return str(update.effective_chat.id)
def _user(update):
    u=update.effective_user; return principal('telegram',str(u.id),display_name=(u.full_name or u.username or ''))
async def _send(update,text,reply_markup=None): await update.effective_message.reply_text(text, reply_markup=reply_markup)

def _split_chat_args(args):
    if not args:
        return '', ''
    if args[0] == 'chat':
        args=args[1:]
    if not args:
        return '', ''
    return args[0], ' '.join(args[1:]).strip()

async def _dispatch_as_agent(update, service, adapter, actor, agent_key, message_text=''):
    ag=service.bind(actor,_chat(update),agent_key)
    if not message_text:
        await _send(update,f"Selected {ag['display_name']} ({ag['profile_name']}). Send your next message to chat with this agent.")
        return ag
    if not adapter or not hasattr(adapter,'_build_message_event') or not hasattr(adapter,'handle_message'):
        await _send(update,f"Selected {ag['display_name']} ({ag['profile_name']}). I could not inject the inline message through this Telegram adapter; send it as your next message.")
        return ag
    try:
        from gateway.platforms.event import MessageType
        event=adapter._build_message_event(update.effective_message, MessageType.TEXT, getattr(update,'update_id',None))
        event.text=message_text
        event.source.profile=ag['profile_name']
        await adapter.handle_message(event)
    except Exception:
        await _send(update,f"Selected {ag['display_name']} ({ag['profile_name']}). Inline dispatch failed; send the message again normally.")
        raise
    return ag

async def _dispatch_bound_text(update, service, adapter):
    actor=_user(update)
    try:
        profile=service.route(actor,_chat(update),update.effective_message.text or '')
    except Exception:
        return False
    if not adapter or not hasattr(adapter,'_build_message_event') or not hasattr(adapter,'handle_message'):
        return False
    from gateway.platforms.event import MessageType
    event=adapter._build_message_event(update.effective_message, MessageType.TEXT, getattr(update,'update_id',None))
    event.source.profile=profile
    await adapter.handle_message(event)
    return True

def _catalog_text(service):
    c=service.catalogs(); models=[m['value'] if isinstance(m,dict) else str(m) for m in c['models']]
    return 'Catalog\nModels: '+(', '.join(models[:20]) or '(no connected providers found)')+'\nSkills: '+(', '.join(c['skills'][:20]) or '(none)')+'\nMCP: '+(', '.join(c['mcp_servers']) or '(none)')+'\nAutonomy: '+', '.join(c.get('autonomy_levels') or ['human-approval','agent','autonomous'])+'\nRBAC toolsets: '+(', '.join((c.get('rbac_toolsets') or [])[:20]) or '(none)')

def register_telegram(app, adapter, service):
    try:
        from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, filters
    except Exception:
        return
    app.add_handler(CommandHandler(['agent_builder','agent-builder'], lambda u,c: _cmd(u,c,service,adapter)), group=-10)
    app.add_handler(CommandHandler(['agent'], lambda u,c: _agent(u,c,service,adapter)), group=-10)
    app.add_handler(CallbackQueryHandler(lambda u,c: _callback(u,c,service), pattern=r'^ab:'), group=-10)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: _wizard_text(u,c,service,adapter)), group=-10)

async def _cmd(update, context, service, adapter=None):
    actor=_user(update); args=list(getattr(context,'args',[]) or [])
    try:
        if args and args[0] in {'list','agents'}:
            rows=service.list_agents(actor); await _send(update, _fmt_list(rows), _agent_keyboard(rows)); return
        if args and args[0]=='chat':
            key,msg=_split_chat_args(args)
            if key:
                await _dispatch_as_agent(update,service,adapter,actor,key,msg); return
            rows=[r for r in service.list_agents(actor) if r.get('status')=='active']
            await _send(update, 'Pick an agent to chat with:', _agent_keyboard(rows, prefix='ab:chat:')); return
        if args and args[0]=='catalog': await _send(update,_catalog_text(service)); return
        if args and args[0]=='requests': await _send(update,_fmt_requests(service.pending_requests(actor))); return
        if args and args[0]=='update' and len(args)>=3:
            payload=_parse_update_args(args[2:])
            ag=service.update_agent(actor,args[1],payload)
            await _send(update,f"Updated {ag['display_name']} ({ag['profile_name']}).")
            return
        if args and args[0]=='delete' and len(args)==2:
            ag=service.get_agent(actor,args[1])
            if service.registry.binding(actor,_chat(update))==ag['id']:
                service.unbind(actor,_chat(update))
            service.delete_agent(actor,args[1])
            await _send(update,'Deleted. Main Hermes agent selected.')
            return
        if (args and args[0] in {'create','start'}) or not args:
            WIZ[_chat(update)]={'actor':actor,'step':0,'spec':{}}
            await _send(update,'Agent Builder wizard\nAgent name? Prefix generated as ssa- automatically.'); return
        if args and args[0]=='cancel': WIZ.pop(_chat(update),None); await _send(update,'Cancelled.'); return
        await _send(update,'Commands: /agent_builder create | list | chat | catalog | update <ssa-profile> purpose="..." model=provider:model autonomy=agent access=shared skills=a,b mcp_servers=x,y shared_users=telegram:123 | delete <ssa-profile> | cancel')
    except Exception as e: await _send(update,f'Error: {e}')


def _parse_update_args(tokens):
    payload={}
    aliases={'autonomy':'risk_level','access':'access_policy','mcp':'mcp_servers','mcps':'mcp_servers'}
    list_fields={'skills','mcp_servers','shared_users','capabilities'}
    for token in tokens:
        if '=' not in token:
            raise ValueError('update fields must be key=value, e.g. purpose="Read docs" model=openai:gpt-5.5')
        key,value=token.split('=',1)
        key=aliases.get(key.strip().replace('-','_'),key.strip().replace('-','_'))
        value=value.strip()
        if key in list_fields:
            payload[key]=[x.strip() for x in value.split(',') if x.strip()]
        elif key in {'model','purpose','description','instructions','access_policy','risk_level'}:
            payload[key]=value
        else:
            raise ValueError(f'unsupported update field: {key}')
    if not payload:
        raise ValueError('provide at least one key=value field')
    return payload

async def _wizard_text(update, context, service, adapter=None):
    cid=_chat(update)
    if cid not in WIZ:
        if await _dispatch_bound_text(update,service,adapter):
            try:
                from telegram.ext import ApplicationHandlerStop
                raise ApplicationHandlerStop
            except ImportError:
                return
        return
    st=WIZ[cid]; spec=st['spec']; step=st['step']; val=update.effective_message.text.strip()
    try:
        if step < len(FIELDS):
            spec[FIELDS[step]]=val; st['step']+=1
            if st['step']<len(FIELDS):
                if FIELDS[st['step']]=='model':
                    await _send(update,'Pick or type model from connected providers. Example: openai:gpt-5.5 or nous:Hermes-4', _model_keyboard(service)); return
                prompt={'purpose':'Purpose? Example: Answer scoped operational questions for this team or customer.','model':'Model? Pick a button or type provider:model, e.g. openai:gpt-5.5 or nous:Hermes-4'}[FIELDS[st['step']]]; await _send(update,prompt); return
            await _send(update,_catalog_text(service)+'\nSend comma-separated skills, or - for none. Example: youtube-content,systematic-debugging'); return
        if step==len(FIELDS):
            spec['skills']=[] if val=='-' else [x.strip() for x in val.split(',') if x.strip()]; st['step']+=1; await _send(update,'Send comma-separated MCP servers, or - for none. Example: confluence,grafana-prod'); return
        if step==len(FIELDS)+1:
            spec['mcp_servers']=[] if val=='-' else [x.strip() for x in val.split(',') if x.strip()]; st['step']+=1; await _send(update,'Access policy: private or shared? Example: shared for a team agent, private for owner-only.'); return
        if step==len(FIELDS)+2:
            spec['access_policy']='shared' if val.lower().startswith('shared') else 'private'; st['step']+=1; await _send(update,'Autonomy? human-approval = drafts/requires approval; agent = supervised routine actions; autonomous = end-to-end scoped workflows.', _choice_keyboard('ab:autonomy:', service.catalogs().get('autonomy_levels') or ['human-approval','agent','autonomous'])); return
        if step==len(FIELDS)+3:
            autonomy=val.lower().replace(' ','-')
            spec['risk_level']=autonomy if autonomy in {'human-approval','autonomous','agent'} else 'human-approval'
            st['step']+=1; await _send(update,'Install hermes-rbac plugin? yes or no. Recommended yes for shared/customer agents.'); return
        if step==len(FIELDS)+4:
            if val.lower().startswith(('y','install','true','1')):
                spec['rbac']={'install':True}; st['step']+=1; await _send(update,'RBAC role name? Examples: viewer, dev, customer-admin'); return
            WIZ.pop(cid,None)
            try:
                ag=service.create_agent(st['actor'],spec); await _send(update,_fmt_agent(ag))
            except ApprovalRequired as ar: await _send(update,f'Approval required. Request: {ar}. Open Hermes Dashboard Pairing/Agent Builder tab to approve.')
            return
        if step==len(FIELDS)+5:
            spec.setdefault('rbac',{})['role']=val or 'viewer'; st['step']+=1; await _send(update,'RBAC users? comma-separated platform:id, or - for none. Example: telegram:123, discord:456'); return
        if step==len(FIELDS)+6:
            spec.setdefault('rbac',{})['users']=[] if val=='-' else [x.strip() for x in val.split(',') if x.strip()]; st['step']+=1; await _send(update,'Bootstrap admins? comma-separated platform:id, or - for none. Example: telegram:123456 for yourself.'); return
        if step==len(FIELDS)+7:
            spec.setdefault('rbac',{})['bootstrap_admins']=[] if val=='-' else [x.strip() for x in val.split(',') if x.strip()]; st['step']+=1; await _send(update,'RBAC toolsets? comma-separated from catalog, or - for defaults. Example: web_search,web_extract,skill_view,read_file,mcp__*. Choices: '+', '.join((service.catalogs().get('rbac_toolsets') or [])[:20])); return
        if step==len(FIELDS)+8:
            if val!='-': spec.setdefault('rbac',{})['toolsets']=[x.strip() for x in val.split(',') if x.strip()]
            st['step']+=1; await _send(update,'RBAC skills? comma-separated skill choices, or - to reuse selected agent skills. '+', '.join((service.catalogs().get('skills') or [])[:20])); return
        if step==len(FIELDS)+9:
            if val!='-': spec.setdefault('rbac',{})['skills']=[x.strip() for x in val.split(',') if x.strip()]
            st['step']+=1; await _send(update,'Bypass sensitive path protection? yes or no'); return
        if step==len(FIELDS)+10:
            spec.setdefault('rbac',{})['bypass_sensitive_paths']=val.lower().startswith(('y','true','1'))
            WIZ.pop(cid,None)
            try:
                ag=service.create_agent(st['actor'],spec); await _send(update,_fmt_agent(ag))
            except ApprovalRequired as ar: await _send(update,f'Approval required. Request: {ar}. Open Hermes Dashboard Pairing/Agent Builder tab to approve.')
    except Exception as e:
        WIZ.pop(cid,None); await _send(update,f'Wizard failed: {e}')

async def _agent(update, context, service, adapter):
    actor=_user(update); args=list(getattr(context,'args',[]) or [])
    if not args: await _send(update,'Usage: /agent list | /agent chat [agent] [message] | /agent share <ssa-profile> <platform:id>'); return
    try:
        if args[0]=='list':
            rows=service.list_agents(actor); await _send(update,_fmt_list(rows),_agent_keyboard(rows)); return
        if args[0]=='chat':
            key,msg=_split_chat_args(args)
            if key:
                await _dispatch_as_agent(update,service,adapter,actor,key,msg); return
            rows=[r for r in service.list_agents(actor) if r.get('status')=='active']
            await _send(update,'Pick an agent to chat with:',_agent_keyboard(rows,prefix='ab:chat:')); return
        if args[0]=='share' and len(args)>=3:
            service.share_agent(actor,args[1],args[2],'user'); await _send(update,'Shared.'); return
        ag=await _dispatch_as_agent(update,service,adapter,actor,args[0])
    except Exception: await _send(update,'Agent unavailable or access denied.')

async def _callback(update, context, service):
    q=update.callback_query
    await q.answer()
    data=q.data or ''
    actor=_user(update)
    if data.startswith('ab:chat:'):
        key=data.split(':',2)[2]
        try:
            ag=service.bind(actor,_chat(update),key)
            await q.edit_message_text(f"Selected {ag['display_name']} ({ag['profile_name']}). Send your next message to chat with this agent.")
        except Exception:
            await q.edit_message_text('Agent unavailable or access denied.')
    elif data.startswith('ab:model:'):
        cid=_chat(update)
        if cid in WIZ:
            WIZ[cid]['spec']['model']=data.split(':',2)[2]
            WIZ[cid]['step']+=1
            await q.edit_message_text(_catalog_text(service)+'\nSend comma-separated skills, or - for none.')
    elif data.startswith('ab:autonomy:'):
        cid=_chat(update)
        if cid in WIZ:
            WIZ[cid]['spec']['risk_level']=data.split(':',2)[2]
            WIZ[cid]['step']+=1
            await q.edit_message_text('Install hermes-rbac plugin? yes or no. Recommended yes for shared/customer agents.')

def _agent_keyboard(rows, prefix='ab:chat:'):
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return None
    buttons=[[InlineKeyboardButton(r['display_name'][:40], callback_data=prefix+r['id'])] for r in rows[:20]]
    return InlineKeyboardMarkup(buttons) if buttons else None

def _model_keyboard(service):
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return None
    models=service.catalogs().get('models',[])[:20]
    buttons=[[InlineKeyboardButton((m.get('label') or m.get('value'))[:60], callback_data='ab:model:'+m['value'])] for m in models if isinstance(m,dict)]
    return InlineKeyboardMarkup(buttons) if buttons else None

def _choice_keyboard(prefix, values):
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return None
    buttons=[[InlineKeyboardButton(str(v)[:60], callback_data=prefix+str(v))] for v in values[:20]]
    return InlineKeyboardMarkup(buttons) if buttons else None

def _fmt_list(rows):
    if not rows: return 'No authorized SSA agents.'
    return '\n'.join([f"{r['profile_name']} — {r['display_name']} — owner {r['owner_id']} — {r['status']}" for r in rows])
def _fmt_requests(rows):
    if not rows: return 'No pending approval requests.'
    return '\n'.join([f"{r['id']} — {r['request_type']} — agent {r.get('agent_id') or ''} — requested by {r['requested_by']}" for r in rows])
def _fmt_agent(a):
    return f"Agent created\nName: {a['display_name']}\nProfile: {a['profile_name']}\nOwner: {a['owner_id']}\nAccess: {a['access_policy']}\nStatus: {a['status']}"
