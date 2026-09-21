from __future__ import annotations
import os, shutil, uuid
from pathlib import Path
import yaml
from .catalog import copy_catalog_mcp
from .errors import ConflictError, ValidationError
from .rbac import install_rbac

class ProfileManager:
    def __init__(self, hermes_home: Path):
        self.home=Path(hermes_home).expanduser(); self.profiles=self.home/'profiles'; self.profiles.mkdir(parents=True,exist_ok=True)
    def profile_path(self,name):
        if not isinstance(name,str) or not name.startswith('ssa-') or '..' in name or '/' in name or not all(c.islower() or c.isdigit() or c=='-' for c in name):
            raise ValidationError('invalid ssa profile name')
        return self.profiles/name
    def create_profile(self, *, profile_name, display_name, model, provider='', purpose='', instructions='', skills=(), mcp_servers=(), custom_skills=(), custom_mcps=(), rbac=None):
        final=self.profile_path(profile_name); stage=self.profiles/(f'.{profile_name}.staging-{uuid.uuid4().hex}')
        if final.exists(): raise ConflictError('profile already exists')
        try:
            stage.mkdir(mode=0o700)
            (stage/'skills').mkdir(mode=0o700)
            try:
                parent_cfg=yaml.safe_load((self.home/'config.yaml').read_text()) if (self.home/'config.yaml').exists() else {}
            except yaml.YAMLError:
                parent_cfg={}
            if not isinstance(parent_cfg,dict): parent_cfg={}
            parent_model=parent_cfg.get('model') if isinstance(parent_cfg.get('model'),dict) else {}
            model_cfg={'default': model}
            if provider: model_cfg['provider']=provider
            for key in ('provider','base_url'):
                if key not in model_cfg and parent_model.get(key): model_cfg[key]=parent_model[key]
            mcp_cfg=copy_catalog_mcp(self.home, mcp_servers) if mcp_servers else {}
            for item in custom_mcps:
                mcp_cfg[item['name']]={'transport':item['transport'],'url':item['url']}
            cfg={'model': model_cfg, 'agent': {'created_by_agent_builder': True}, 'mcp_servers': mcp_cfg}
            (stage/'config.yaml').write_text(yaml.safe_dump(cfg, sort_keys=False))
            (stage/'SOUL.md').write_text(f'You are {display_name}.\n\nPurpose: {purpose}\n')
            (stage/'AGENTS.md').write_text('Operational instructions for this self-service Hermes agent.\n\n'+(instructions or ''))
            (stage/'.env').write_text('')
            os.chmod(stage/'.env',0o600)
            auth=self.home/'auth.json'
            if auth.is_file():
                shutil.copy2(auth,stage/'auth.json'); os.chmod(stage/'auth.json',0o600)
            self._sync_skills(stage/'skills', skills)
            for item in custom_skills:
                dst=stage/'skills'/item['name']; dst.mkdir(mode=0o700)
                content=item['content'].strip()
                if not content.startswith('---'):
                    content=f"---\nname: {item['name']}\ndescription: Custom skill supplied by the agent owner.\n---\n\n# {item['name']}\n\n{content}\n"
                (dst/'SKILL.md').write_text(content)
            if rbac:
                install_rbac(stage, rbac)
            self._reject_symlinks(stage)
            os.rename(stage,final)
            return final
        except BaseException:
            shutil.rmtree(stage,ignore_errors=True); raise
    def _sync_skills(self, dst_root, skills):
        dst_root=Path(dst_root); dst_root.mkdir(mode=0o700,exist_ok=True)
        skills_root=(self.home/'skills').resolve()
        for skill in skills:
            rel=Path(str(skill))
            if rel.is_absolute() or '..' in rel.parts: continue
            src=(skills_root/rel).resolve()
            if skills_root not in src.parents and src != skills_root: continue
            dst=dst_root/rel
            if src.exists() and src.is_dir(): dst.parent.mkdir(parents=True,exist_ok=True); shutil.copytree(src,dst,symlinks=False)
    def update_profile(self, profile_name, *, provider=None, model=None, purpose=None, instructions=None, skills=None, mcp_servers=None, rbac=None, cron=None, custom_skills=None, custom_mcps=None):
        p=self.profile_path(profile_name)
        if not p.exists(): raise ValidationError('profile does not exist')
        cfg_path=p/'config.yaml'
        cfg=yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
        if not isinstance(cfg,dict): cfg={}
        if model is not None:
            model_cfg=cfg.setdefault('model',{})
            if not isinstance(model_cfg,dict): model_cfg={}; cfg['model']=model_cfg
            model_cfg['default']=model
            if provider: model_cfg['provider']=provider
            elif 'provider' in model_cfg and not provider: model_cfg.pop('provider',None)
        if mcp_servers is not None:
            cfg['mcp_servers']=copy_catalog_mcp(self.home, mcp_servers) if mcp_servers else {}
        if custom_mcps:
            mcp_cfg=cfg.setdefault('mcp_servers',{})
            if not isinstance(mcp_cfg,dict):
                mcp_cfg={}; cfg['mcp_servers']=mcp_cfg
            for item in custom_mcps:
                mcp_cfg[item['name']]={'transport':item['transport'],'url':item['url']}
        if cron is not None:
            cfg['cron']=cron if isinstance(cron,dict) else {'jobs': cron}
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        if purpose is not None:
            soul=p/'SOUL.md'
            first=soul.read_text() if soul.exists() else ''
            name_line=first.splitlines()[0] if first.splitlines() else 'You are this agent.'
            soul.write_text(f'{name_line}\n\nPurpose: {purpose}\n')
        if instructions is not None:
            (p/'AGENTS.md').write_text('Operational instructions for this self-service Hermes agent.\n\n'+instructions)
        if skills is not None:
            dst_root=p/'skills'; dst_root.mkdir(mode=0o700,exist_ok=True)
            for child in list(dst_root.iterdir()):
                if child.is_dir(): shutil.rmtree(child)
                else: child.unlink()
            self._sync_skills(dst_root, skills)
        if custom_skills:
            dst_root=p/'skills'; dst_root.mkdir(mode=0o700,exist_ok=True)
            for item in custom_skills:
                dst=dst_root/item['name']; dst.mkdir(mode=0o700,exist_ok=True)
                content=item['content'].strip()
                if not content.startswith('---'):
                    content=f"---\nname: {item['name']}\ndescription: Custom skill supplied by the agent owner.\n---\n\n# {item['name']}\n\n{content}\n"
                (dst/'SKILL.md').write_text(content)
        if rbac:
            install_rbac(p, rbac)
        self._reject_symlinks(p)
        return p
    def _reject_symlinks(self,root):
        for p in Path(root).rglob('*'):
            if p.is_symlink(): raise ValidationError('symlink rejected in staged profile')
    def disable_profile(self,profile_name):
        p=self.profile_path(profile_name); (p/'.agent-builder-disabled').write_text('disabled')
    def delete_profile(self,profile_name):
        p=self.profile_path(profile_name)
        if p.exists(): os.rename(p,self.profiles/(f'.deleted-{profile_name}-{uuid.uuid4().hex[:8]}'))
