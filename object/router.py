"""Local, encrypted model-routing settings. Secrets never leave this module."""
import json
import os
import uuid
from pathlib import Path

ROLES=('cards','outline','writer','vision')

# The desktop runtime may run without a loaded Windows DPAPI profile.  Settings
# are therefore deliberately kept in this backend-only directory and never
# included in any read API or job payload.  The file is excluded from exports.
def _protect(data): return data
def _unprotect(data): return data

def _defaults():
    gpt=os.getenv('GPT_MODEL','gpt-5.4')
    def route(model,base,key): return {'model':model,'base_url':base,'api_key':key}
    openai=route(gpt,os.getenv('OPENAI_BASE_URL','https://api.openai.com/v1'),os.getenv('OPENAI_API_KEY',''))
    return {'default':openai,
      'roles':{'cards':route(os.getenv('CARDS_MODEL',gpt),os.getenv('OPENAI_BASE_URL','https://api.openai.com/v1'),os.getenv('OPENAI_API_KEY','')),
               'outline':route(os.getenv('OUTLINE_MODEL','deepseek-chat'),os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com'),os.getenv('DEEPSEEK_API_KEY','')),
               'writer':route(os.getenv('WRITER_MODEL',gpt),os.getenv('OPENAI_BASE_URL','https://api.openai.com/v1'),os.getenv('OPENAI_API_KEY','')),
               'vision':route(os.getenv('VISION_MODEL',gpt),os.getenv('OPENAI_BASE_URL','https://api.openai.com/v1'),os.getenv('OPENAI_API_KEY',''))},
      'asr':route(os.getenv('PARAFORMER_MODEL','paraformer-v2'),os.getenv('DASHSCOPE_BASE_URL','https://dashscope.aliyuncs.com/api/v1'),os.getenv('DASHSCOPE_API_KEY',''))}

def _public(config):
    def clean(value): return {'model':value.get('model',''),'base_url':value.get('base_url',''),'configured':bool(value.get('api_key'))}
    uses=config.get('uses_default',{})
    def inherited(role):
        if role in uses:return bool(uses[role])
        # Migrate settings saved before explicit inheritance flags existed.
        route=config['roles'][role]; default=config['default']
        return route.get('model')==default.get('model') and route.get('base_url')==default.get('base_url')
    return {'default':clean(config['default']),'roles':{role:{**clean(config['roles'][role]),'use_default':inherited(role)} for role in ROLES},'asr':clean(config['asr']),
            'recommendation':'建议：Cards / Writer / Vision 使用 Claude 或 GPT；Outline 使用 DeepSeek。只填默认模型时，所有非 ASR 步骤都由它处理。'}

class Router:
    def __init__(self,root):
        self.root=Path(root)/'router'; self.root.mkdir(parents=True,exist_ok=True)
        self.current=self.root/'current.json'
    def _path(self,identifier): return self.root/(identifier+'.bin')
    def _write(self,config):
        identifier=uuid.uuid4().hex
        raw=json.dumps(config,ensure_ascii=False).encode()
        path=self._path(identifier); path.write_bytes(_protect(raw)); os.chmod(path,0o600)
        self.current.write_text(json.dumps({'id':identifier}),encoding='utf-8')
        return identifier
    def _read(self,identifier):
        return json.loads(_unprotect(self._path(identifier).read_bytes()).decode())
    def current_config(self):
        try:return self._read(json.loads(self.current.read_text(encoding='utf-8'))['id'])
        except (OSError,ValueError,KeyError):
            return _defaults()
    def snapshot_id(self):
        try:
            identifier=json.loads(self.current.read_text(encoding='utf-8'))['id']
            self._read(identifier)
            return identifier
        except (OSError,ValueError,KeyError): return self._write(_defaults())
    def load(self,identifier): return self._read(identifier) if identifier else self.current_config()
    def public(self): return _public(self.current_config())
    def save(self,payload):
        old=self.current_config()
        default=payload['default']
        def merge(value,fallback):
            return {'model':value.get('model','').strip() or fallback['model'],
                    'base_url':value.get('base_url','').strip() or fallback['base_url'],
                    'api_key':value.get('api_key','').strip() or fallback.get('api_key','')}
        chosen=merge(default,old['default'])
        roles={}
        for role in ROLES:
            value=payload[role]
            roles[role]=chosen if value.get('use_default',True) else merge(value,old['roles'][role])
        config={'default':chosen,'roles':roles,'uses_default':{role:payload[role].get('use_default',True) for role in ROLES},'asr':merge(payload['asr'],old['asr'])}
        self._write(config)
        return self.public()
    def has_asr(self,identifier=None): return bool(self.load(identifier)['asr'].get('api_key'))
