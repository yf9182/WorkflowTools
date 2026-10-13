import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from workflow_tools.common import Failure, atomic_bytes, private_read, read_json, run, save
from workflow_tools.config import Project, source_binding
from workflow_tools.credentials import Vault
from workflow_tools.runtime import Runtime
from workflow_tools.ios import signing_metadata
from workflow_tools.cloudflare import Cloudflare, target_config
from test_ios_cloudflare import distribution, configure_cloud


def test_p12_password_profile_pair_cryptographically_verified(project,vault,distribution):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    key,cert,data,profile=distribution
    p12=pkcs12.serialize_key_and_certificates(b'test',key,cert,None,serialization.BestAvailableEncryption(b'synthetic-password'))
    refs={}
    for name,kind,raw in [('signing/p12','pkcs12',p12),('signing/password','password',b'synthetic-password'),('signing/profile','provisioning_profile',profile)]:
        path=project.home/'incoming'/name.replace('/','-')
        atomic_bytes(path,raw)
        refs.update(vault.import_file(name,kind,path)['credential_ref'])
    d=read_json(project.path); d['credential_refs'].update(refs)
    d['ios']['signing']={'p12_ref':'signing/p12','password_ref':'signing/password','profiles':{'com.example.app':'signing/profile'}}
    save(project.path,d)
    runtime=Runtime(project.root,project.home)
    result=signing_metadata(runtime.project,runtime.vault)
    assert result['profiles']['com.example.app']['type']=='app-store'
    assert len(result['certificate_sha1'])==40
    atomic_bytes(project.home/'incoming/bad-password',b'wrong-password')
    updated=vault.import_file('signing/password','password',project.home/'incoming/bad-password')
    d['credential_refs'].update(updated['credential_ref']); save(project.path,d)
    runtime=Runtime(project.root,project.home)
    with pytest.raises(Failure) as exc: signing_metadata(runtime.project,runtime.vault)
    assert exc.value.code=='P12_INVALID'


def test_fifo_credentials_rejected_without_blocking(tmp_path):
    path=tmp_path/'fifo'; os.mkfifo(path,0o600)
    with pytest.raises(Failure): private_read(path)


def test_known_values_pem_jwt_and_bearer_redacted_from_logs(tmp_path):
    secret='synthetic-long-deployment-token'
    data=(secret+'\nBearer unknownbearertoken\n-----BEGIN PRIVATE KEY-----\nTESTDATA\n-----END PRIVATE KEY-----').encode()
    log=tmp_path/'operation.log'
    result=run([sys.executable,'-c','import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())'],stdin=data,secret_values=[secret],log=log)
    assert result.returncode==0
    text=log.read_text()
    assert secret not in text and 'unknownbearertoken' not in text and 'TESTDATA' not in text
    assert 'synthetic-long-deployment-token' not in ' '.join(result.args)


def test_wrangler_refuses_implicit_resources_and_migrations(project,vault):
    p=configure_cloud(project,vault)
    path=p.root/'backend/wrangler.jsonc'
    for field,value in [('d1_databases',[{'binding':'DB'}]),('migrations',[{'tag':'v1','new_classes':['Counter']}]),('containers',[{}])]:
        path.write_text(json.dumps({'name':'my-worker',field:value}))
        with pytest.raises(Failure): target_config(p,p.target('backend','production'))


def test_pages_wrong_branch_rejected_before_writes(project,vault):
    p=configure_cloud(project,vault)
    class API:
        methods=[]
        def json(self,*a,**k):
            self.methods.append(k.get('method'))
            return {'success':True,'result':{'name':'my-web','production_branch':'other'}}
    runtime=Runtime(p.root,p.home,http=API())
    target={'kind':'pages','name':'my-web','branch':'main','environment':'production','output_directory':'dist'}
    with pytest.raises(Failure) as exc: Cloudflare(runtime).ensure_pages(target,{},p.home/'state/pages.json')
    assert exc.value.code=='TARGET_MISMATCH'
    assert runtime.http.methods==['GET']


def test_worker_tag_reconciles_lost_command_response(project,vault,monkeypatch):
    import workflow_tools.cloudflare as module
    p=configure_cloud(project,vault)
    class API:
        message=None; version=None
        def json(self,url,**kwargs):
            if '/versions/' in url: return {'success':True,'result':{'annotations':{'workers/message':self.message}}}
            return {'success':True,'result':{'deployments':[] if not self.version else [{'id':'deployment-1','versions':[{'version_id':self.version,'percentage':100}]}]}}
        def request(self,*a,**k): return b'ok',{'_workflow_status':200}
    api=API(); count=[]
    monkeypatch.setattr(module,'wrangler',lambda _:['fake-wrangler'])
    def command(args,**kwargs):
        if '--dry-run' in args: return subprocess.CompletedProcess(args,0,b'',b'')
        api.message=args[args.index('--message')+1]
        api.version='11111111-2222-3333-4444-555555555555'
        count.append(1)
        raise subprocess.TimeoutExpired(args,1)
    monkeypatch.setattr(module,'run',command)
    runtime=Runtime(p.root,p.home,http=api)
    params={'target':'backend','environment':'production','source_binding':source_binding(p)}
    with pytest.raises(Failure): module.deploy(runtime,params,'job',lambda *a,**k:None)
    assert module.deploy(runtime,params,'job',lambda *a,**k:None)['status']=='succeeded'
    assert len(count)==1


def test_analytics_refresh_explicitly_gets_new_read_job(project,monkeypatch):
    runtime=Runtime(project.root,project.home)
    monkeypatch.setattr(runtime.jobs,'launch',lambda _:None)
    a=runtime.analytics_fetch(start_date='2026-09-01',end_date='2026-09-02')
    b=runtime.analytics_fetch(start_date='2026-09-01',end_date='2026-09-02')
    c=runtime.analytics_fetch(start_date='2026-09-01',end_date='2026-09-02',refresh=True)
    assert a['job_id']==b['job_id'] and c['job_id']!=a['job_id']


def test_mcp_legacy_transport_remains_compatible(project):
    async def exercise():
        params=StdioServerParameters(command=sys.executable,args=['-m','workflow_tools.cli','--project',str(project.root),'--home',str(project.home),'mcp'])
        async with Client(params,mode='legacy') as client:
            r=await client.call_tool('workflow_doctor',{'capability':'ios_build'})
            assert r.structured_content['status']=='blocked'
    asyncio.run(exercise())
