from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pathlib import Path
import json, os, sys
import yaml
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from agent_builder.auth import principal
from agent_builder.errors import ApprovalRequired
from agent_builder.registry import Registry
from agent_builder.service import AgentService

router=APIRouter()


TELEGRAM_FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Create Agent</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:14px;background:var(--tg-theme-bg-color,#11151c);color:var(--tg-theme-text-color,#f5f5f5)}
h2{margin:0 0 12px}label{display:grid;gap:5px;margin:0 0 12px;font-weight:650}input,textarea,select{box-sizing:border-box;width:100%;padding:10px;border:1px solid var(--tg-theme-hint-color,#555);border-radius:8px;background:var(--tg-theme-secondary-bg-color,#1b2130);color:inherit}textarea{min-height:88px}fieldset{border:1px solid var(--tg-theme-hint-color,#555);border-radius:8px;margin:0 0 12px;padding:10px}legend{font-weight:700}.checks label{display:flex;gap:7px;align-items:center;font-weight:500;margin:6px 0}.checks input{width:auto}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.actions{display:flex;gap:8px;position:sticky;bottom:0;background:inherit;padding:12px 0 0}button{flex:1;border:0;border-radius:8px;padding:11px;font-weight:700;background:var(--tg-theme-button-color,#2f6fed);color:var(--tg-theme-button-text-color,#fff)}button.secondary{background:transparent;color:var(--tg-theme-link-color,#7aa2f7);border:1px solid currentColor}.msg{white-space:pre-wrap;padding:9px;border-radius:8px;margin-bottom:10px}.err{background:#4a1717}.ok{background:#174a2b}@media(max-width:520px){.row{grid-template-columns:1fr}}
</style></head><body>
<h2>Create new agent</h2><div id="msg"></div>
<form id="f">
<label>Agent name<input name="display_name" required placeholder="Research Assistant"></label>
<label>Purpose<textarea name="purpose" required placeholder="Describe what this agent should do"></textarea></label>
<label>Model<select name="model" id="model" required></select></label>
<fieldset><legend>Installed skills</legend><div id="skills" class="checks"></div></fieldset>
<fieldset><legend>Custom skill</legend><label>Skill name<input name="custom_skill_name" placeholder="my-skill"></label><label>Skill text<textarea name="custom_skill_text" placeholder="Write SKILL.md instructions or attach a file"></textarea></label><label>Attach skill file<input id="skill_file" type="file" accept=".md,.txt,text/markdown,text/plain"></label></fieldset>
<fieldset><legend>Configured MCP servers</legend><div id="mcps" class="checks"></div></fieldset>
<fieldset><legend>Custom MCP</legend><label>MCP name<input name="custom_mcp_name" placeholder="project-mcp"></label><label>MCP URL<input name="custom_mcp_url" type="url" placeholder="https://mcp.example.com/mcp"></label><label>Transport<select name="custom_mcp_transport"><option value="http">HTTP</option><option value="sse">SSE</option></select></label></fieldset>
<div class="row"><label>Access<select name="access_policy"><option value="private">Private</option><option value="shared">Shared</option></select></label><label>Risk<select name="risk_level"><option value="read-only">Read-only</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></label></div>
<div class="actions"><button type="button" class="secondary" id="close">Close</button><button type="submit">Create</button></div>
</form>
<script>
const tg=window.Telegram&&window.Telegram.WebApp; if(tg){tg.ready();tg.expand();}
const msg=document.getElementById('msg'); const form=document.getElementById('f');
function show(t,ok=false){msg.className='msg '+(ok?'ok':'err');msg.textContent=t}
function box(root,name){return [...root.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value)}
function checks(id,items){const root=document.getElementById(id);root.innerHTML=items.length?'':'None';items.forEach(x=>{const l=document.createElement('label');l.innerHTML='<input type="checkbox"> <span></span>';l.querySelector('input').value=x;l.querySelector('span').textContent=x;root.appendChild(l)})}
fetch('/api/plugins/agent-builder/catalogs',{credentials:'include'}).then(r=>{if(!r.ok)throw new Error('Login required. Open Hermes Dashboard once, then retry /agent-builder create.');return r.json()}).then(c=>{const m=document.getElementById('model');m.innerHTML='<option value="">Select model</option>';(c.models||[]).forEach(x=>m.add(new Option(x,x))); if(!(c.models||[]).length){m.outerHTML='<input name="model" required placeholder="provider/model">'} checks('skills',c.skills||[]);checks('mcps',c.mcp_servers||[])}).catch(e=>show(String(e.message||e)));
document.getElementById('skill_file').addEventListener('change',e=>{const file=e.target.files[0];if(!file)return;if(file.size>30000){show('Skill file must be 30 KB or smaller');return}file.text().then(t=>{form.custom_skill_text.value=t;if(!form.custom_skill_name.value)form.custom_skill_name.value=file.name.replace(/\.[^.]+$/,'').replace(/[^A-Za-z0-9_-]+/g,'-').toLowerCase()})});
document.getElementById('close').onclick=()=>tg?tg.close():history.back();
form.onsubmit=async e=>{e.preventDefault();msg.className='';msg.textContent='Submitting…';const fd=new FormData(form);const p=Object.fromEntries(fd.entries());p.skills=box(document.getElementById('skills'));p.mcp_servers=box(document.getElementById('mcps'));p.custom_skills=p.custom_skill_name&&p.custom_skill_text?[{name:p.custom_skill_name,content:p.custom_skill_text}]:[];p.custom_mcps=p.custom_mcp_name&&p.custom_mcp_url?[{name:p.custom_mcp_name,url:p.custom_mcp_url,transport:p.custom_mcp_transport}]:[];delete p.custom_skill_name;delete p.custom_skill_text;delete p.custom_mcp_name;delete p.custom_mcp_url;delete p.custom_mcp_transport;try{const r=await fetch('/api/plugins/agent-builder/agents',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify(p)});const j=await r.json();if(!r.ok)throw new Error(j.detail||JSON.stringify(j));show(j.status==='pending_approval'?'Request submitted. Host admin approves with:\n'+j.approval_command:'Agent created: '+j.agent.profile_name,true);if(tg)tg.MainButton.setText('Done'),tg.MainButton.show(),tg.MainButton.onClick(()=>tg.close())}catch(e){show(String(e.message||e))}}
</script></body></html>"""


def _config(home):
    return yaml.safe_load((home/'config.yaml').read_text()) or {}


def svc():
    home=Path(os.environ.get('HERMES_HOME') or Path.home()/'.hermes')
    cfg=_config(home)
    entry=cfg.get('plugins',{}).get('entries',{}).get('agent-builder',{})
    settings=entry.get('settings') or entry.get('config') or {}
    return AgentService(Registry(home/'plugin-data'/'agent-builder'/'registry.db'),home,
                        allowed_models=settings.get('allowed_models') or [],
                        require_creation_approval=bool(settings.get('require_creation_approval',True)))


def actor(req:Request):
    sess=getattr(req.state,'session',None)
    uid=getattr(sess,'user_id',None) or getattr(sess,'subject',None) or 'dashboard-local'
    return principal('dashboard',str(uid),getattr(sess,'org_id',''),getattr(sess,'display_name','Dashboard'))


def context(req:Request):
    s=svc(); a=actor(req); sess=getattr(req.state,'session',None)
    if getattr(sess,'provider','')=='basic':
        username=str(_config(s.pm.home).get('dashboard',{}).get('basic_auth',{}).get('username') or '')
        if username and a.user_id==username:
            s.bootstrap_admin(a); s.registry.grant_global(a.id,'agent-builder-user',a.id)
    return s,a


def _json_row(row):
    value=dict(row)
    for key in ('payload','metadata'):
        raw=value.get(key)
        if isinstance(raw,str):
            try: value[key]=json.loads(raw)
            except json.JSONDecodeError: pass
    return value


@router.get('/catalogs')
def catalogs(request:Request):
    s,_=context(request); return s.catalogs()


@router.get('/telegram-form', response_class=HTMLResponse)
def telegram_form():
    return HTMLResponse(TELEGRAM_FORM_HTML)


@router.get('/agents')
def agents(request:Request):
    s,a=context(request); return {'agents':s.list_agents(a)}


@router.post('/agents')
def create_agent(payload:dict, request:Request):
    try:
        s,a=context(request)
        try:
            agent=s.create_agent(a,payload)
            return {'status':'active','agent':agent}
        except ApprovalRequired as pending:
            return {'status':'pending_approval','request_id':str(pending),
                    'approval_command':f'hermes agent-builder approve {pending}'}
    except Exception as e:
        raise HTTPException(400,str(e))


@router.get('/agents/{agent_id}')
def agent(agent_id:str, request:Request):
    try:
        s,a=context(request); return {'agent':s.get_agent(a,agent_id)}
    except Exception as e: raise HTTPException(403,str(e))

@router.patch('/agents/{agent_id}')
def update_agent(agent_id:str, payload:dict, request:Request):
    try:
        s,a=context(request)
        return {'agent':s.update_agent(a,agent_id,payload)}
    except Exception as e: raise HTTPException(400,str(e))


@router.delete('/agents/{agent_id}')
def delete_agent(agent_id:str, request:Request):
    try:
        s,a=context(request); s.delete_agent(a,agent_id); return {'ok':True}
    except Exception as e: raise HTTPException(403,str(e))


@router.get('/requests')
def requests(request:Request):
    try:
        s,a=context(request); return {'requests':[_json_row(x) for x in s.pending_requests(a)]}
    except Exception as e: raise HTTPException(403,str(e))


@router.post('/requests/{request_id}/approve')
def approve_request(request_id:str, request:Request):
    try:
        s,a=context(request)
        req=s.approve_request(a,request_id)
        agent=s.registry.get_agent(req.get('agent_id'),include_deleted=True) if req.get('agent_id') else None
        return {'ok':True,'request':_json_row(req),'agent':_json_row(agent) if agent else None}
    except Exception as e: raise HTTPException(403,str(e))


@router.post('/requests/{request_id}/reject')
def reject_request(request_id:str, request:Request):
    try:
        s,a=context(request)
        req=s.deny_request(a,request_id)
        return {'ok':True,'request':_json_row(req)}
    except Exception as e: raise HTTPException(403,str(e))


@router.post('/agents/{agent_id}/disable')
def disable(agent_id:str, request:Request):
    try:
        s,a=context(request); s.disable_agent(a,agent_id); return {'ok':True}
    except Exception as e: raise HTTPException(403,str(e))


@router.get('/audit')
def audit(request:Request):
    try:
        s,a=context(request)
        if not s.registry.is_admin(a.id): raise PermissionError('access denied')
        return {'events':[_json_row(x) for x in s.registry.audit_list()]}
    except Exception as e: raise HTTPException(403,str(e))
