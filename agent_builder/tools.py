import json
from .auth import principal

def _ok(**kw): return json.dumps({'ok':True, **kw})
def _err(e): return json.dumps({'ok':False,'error':type(e).__name__,'message':str(e)})

def register_tools(ctx, service):
    def actor(args): return principal(args.get('platform','telegram'), args.get('user_id','unknown'), args.get('scope',''))
    schemas=[('agent_builder_list','List authorized SSA agents'),('agent_builder_get','Get one authorized SSA agent'),('agent_builder_catalogs','List allowed models skills and MCPs')]
    for name,desc in schemas:
        ctx.register_tool(name=name,toolset='agent_builder',description=desc,schema={'name':name,'description':desc,'parameters':{'type':'object','properties':{}}},handler=lambda args,n=name,**kw: _dispatch(n,args,service,actor))

def _dispatch(name,args,service,actor):
    try:
        if name=='agent_builder_catalogs': return _ok(catalogs=service.catalogs())
        a=actor(args)
        if name=='agent_builder_list': return _ok(agents=service.list_agents(a))
        if name=='agent_builder_get': return _ok(agent=service.get_agent(a,args.get('agent_id') or args.get('profile_name')))
    except Exception as e: return _err(e)
