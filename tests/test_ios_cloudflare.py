import copy
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import zipfile
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from workflow_tools.common import Failure, atomic_bytes, digest, file_sha, read_json, save
from workflow_tools.config import Project, source_binding
from workflow_tools.runtime import Runtime
from workflow_tools.ios import decode_profile, validate_profile, inspect_ipa, prepare_profiles
from workflow_tools.auth import Apple
from workflow_tools.cloudflare import read_wrangler, target_config


@pytest.fixture(scope='module')
def distribution():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Apple Distribution: Synthetic Test'),
                       x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME,'ABCDE12345')])
    now=dt.datetime.now(dt.timezone.utc)
    cert=x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(123).not_valid_before(now-dt.timedelta(days=1)).not_valid_after(now+dt.timedelta(days=365)).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]),critical=False).sign(key,hashes.SHA256())
    data={'UUID':'12345678-1234-1234-1234-123456789abc','Name':'synthetic-app-store','TeamIdentifier':['ABCDE12345'],
        'ApplicationIdentifierPrefix':['ABCDE12345'],'ExpirationDate':(now+dt.timedelta(days=300)).replace(tzinfo=None),
        'DeveloperCertificates':[cert.public_bytes(serialization.Encoding.DER)],
        'Entitlements':{'application-identifier':'ABCDE12345.com.example.app','com.apple.developer.team-identifier':'ABCDE12345','get-task-allow':False}}
    profile=pkcs7.PKCS7SignatureBuilder().set_data(plistlib.dumps(data)).add_signer(cert,key,hashes.SHA256()).sign(serialization.Encoding.DER,[pkcs7.PKCS7Options.Binary])
    return key,cert,data,profile


def test_real_cms_and_ipa_identity(distribution,tmp_path):
    key,cert,data,profile=distribution
    decoded=decode_profile(profile)
    assert decoded['UUID']==data['UUID']
    validate_profile(decoded,'ABCDE12345','com.example.app',cert.fingerprint(hashes.SHA1()).hex().upper())
    path=tmp_path/'app.ipa'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('Payload/Test.app/Info.plist',plistlib.dumps({'CFBundleIdentifier':'com.example.app','CFBundleShortVersionString':'1.2.3','CFBundleVersion':'1230'}))
        z.writestr('Payload/Test.app/embedded.mobileprovision',profile)
    identity={'bundle_id':'com.example.app','team_id':'ABCDE12345','version':'1.2.3','build_number':'1230'}
    assert inspect_ipa(path,identity)['ipa_sha256']==file_sha(path)
    with pytest.raises(Failure): inspect_ipa(path,{**identity,'bundle_id':'com.other.app'})
    with zipfile.ZipFile(path,'a') as z: z.writestr('../escape','bad')
    with pytest.raises(Failure) as exc: inspect_ipa(path,identity)
    assert exc.value.code=='IPA_INVALID'
    assert not (tmp_path/'escape').exists()


@pytest.mark.parametrize('change,code', [
    ('development','PROFILE_NOT_APP_STORE'),('adhoc','PROFILE_NOT_APP_STORE'),('expired','AUTH_EXPIRED'),
    ('team','TARGET_MISMATCH'),('bundle','TARGET_MISMATCH'),('certificate','TARGET_MISMATCH')])
def test_profile_rejects_wrong_distribution(distribution,change,code):
    data=copy.deepcopy(distribution[2])
    fingerprint=distribution[1].fingerprint(hashes.SHA1()).hex().upper()
    if change=='development': data['Entitlements']['get-task-allow']=True
    if change=='adhoc': data['ProvisionedDevices']=['synthetic-device']
    if change=='expired': data['ExpirationDate']=dt.datetime.now()-dt.timedelta(days=1)
    if change=='team': data['TeamIdentifier']=['OTHER12345']
    if change=='bundle': data['Entitlements']['application-identifier']='ABCDE12345.com.other.app'
    if change=='certificate': fingerprint='0'*40
    with pytest.raises(Failure) as exc: validate_profile(data,'ABCDE12345','com.example.app',fingerprint)
    assert exc.value.code==code


def test_asc_creates_profile_for_registered_bundle_and_exact_certificate(project, vault, distribution):
    _, cert, _, profile = distribution
    from base64 import b64encode
    class API:
        def __init__(self): self.created=[]
        def json(self, url, method="GET", headers=None, payload=None):
            if "/certificates?" in url:
                return {"data":[{"id":"cert-1","attributes":{"certificateType":"IOS_DISTRIBUTION","activated":True,
                    "certificateContent":b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()}}]}
            if "/profiles?" in url:
                return {"data":[],"included":[]}
            if "/bundleIds?" in url:
                return {"data":[{"id":"bundle-1","attributes":{"identifier":"com.example.app"}}]}
            if url.endswith("/v1/profiles") and method == "POST":
                self.created.append(payload)
                return {"data":{"id":"profile-1","attributes":{"profileType":"IOS_APP_STORE","profileState":"ACTIVE",
                    "profileContent":b64encode(profile).decode()}}}
            raise AssertionError(url)
    api=API()
    result=Apple(project,vault,api).prepare_app_store_profiles(["com.example.app"],cert.fingerprint(hashes.SHA1()).hex().upper())
    assert result["com.example.app"]["profile"]==profile
    assert result["com.example.app"]["created"] is True
    assert api.created[0]["data"]["relationships"]["certificates"]["data"]==[{"type":"certificates","id":"cert-1"}]


def test_profile_prepare_encrypts_and_registers_project_reference(project, vault, monkeypatch, distribution):
    import workflow_tools.ios as module
    class AppleStub:
        def __init__(self,*_): pass
        def prepare_app_store_profiles(self,bundles,fingerprint):
            return {bundle:{"profile":b"synthetic-profile","created":True,
                "metadata":{"uuid":"12345678-1234-1234-1234-123456789abc","expires_at":"2030-01-01T00:00:00"}}
                for bundle in bundles}
    monkeypatch.setattr(module,"Apple",AppleStub)
    monkeypatch.setattr(module,"distribution_certificate",lambda *_:(distribution[1],"A"*40))
    runtime=Runtime(project.root,project.home,http=object())
    result=prepare_profiles(runtime,["com.example.app"])
    current=read_json(project.path)
    ref=current["ios"]["signing"]["profiles"]["com.example.app"]
    assert result["project_config_updated"] is True
    assert current["credential_refs"][ref]["kind"]=="provisioning_profile"
    assert Runtime(project.root,project.home).vault.read(ref,"provisioning_profile")==b"synthetic-profile"


def make_artifact(project, content=b'synthetic-ipa'):
    path=project.artifacts/'test.ipa'; atomic_bytes(path,content)
    identity={'app_store_id':'123456789','bundle_id':'com.example.app','team_id':'ABCDE12345',
              'version':'1.2.3','build_number':'1230','ipa_sha256':file_sha(path),'certificate_sha1':'0'*40}
    artifact_id=digest(identity)
    record={**identity,'artifact_id':artifact_id,'project_root':str(project.root),'ipa':str(path)}
    save(project.artifacts/(artifact_id+'.json'),record)
    return artifact_id,record


def upload_stubs(monkeypatch,remote,unknown=False):
    import workflow_tools.ios as module
    class AppleStub:
        def __init__(self,*_): pass
        def identity(self): return {}
        def builds(self,*_): return remote['builds']
    monkeypatch.setattr(module,'Apple',AppleStub)
    monkeypatch.setattr(module,'inspect_ipa',lambda path,expected,*_: {'ipa_sha256':file_sha(path)})
    def command(args,**kwargs):
        if '--upload-app' in args:
            remote['uploads']+=1
            if unknown: raise subprocess.TimeoutExpired(args,1)
            remote['builds']=[{'id':'remote-build','attributes':{'processingState':'VALID','expired':False}}]
            return subprocess.CompletedProcess(args,0,b'{"upload-results":{"delivery-uuid":"11111111-2222-3333-4444-555555555555"}}',b'')
        return subprocess.CompletedProcess(args,0,b'{}',b'')
    monkeypatch.setattr(module,'run',command)
    return module


def test_concurrent_uploads_have_one_external_execution(project,vault,monkeypatch):
    remote={'uploads':0,'builds':[]}
    module=upload_stubs(monkeypatch,remote)
    artifact_id,_=make_artifact(project)
    runtime=Runtime(project.root,project.home)
    def invoke(n): return module.upload(runtime,{'artifact_id':artifact_id},str(n),lambda *a,**k:None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(invoke,[1,2]))
    assert remote['uploads']==1
    assert all(r['uploaded'] for r in results)
    assert list((project.home/'state/uploads').glob('*.json'))


def test_unknown_upload_never_retries_and_can_reconcile(project,vault,monkeypatch):
    remote={'uploads':0,'builds':[]}
    module=upload_stubs(monkeypatch,remote,unknown=True)
    artifact_id,_=make_artifact(project)
    runtime=Runtime(project.root,project.home)
    for _ in range(2):
        with pytest.raises(Failure) as exc: module.upload(runtime,{'artifact_id':artifact_id},'job',lambda *a,**k:None)
        assert exc.value.code=='UPLOAD_UNKNOWN'
    assert remote['uploads']==1
    remote['builds']=[{'id':'remote-build','attributes':{'processingState':'PROCESSING','expired':False}}]
    result=module.upload(runtime,{'artifact_id':artifact_id},'job',lambda *a,**k:None)
    assert result['processing_state']=='PROCESSING'
    assert remote['uploads']==1


def test_remote_existing_unowned_build_rejected(project,vault,monkeypatch):
    remote={'uploads':0,'builds':[{'id':'old','attributes':{'processingState':'VALID'}}]}
    module=upload_stubs(monkeypatch,remote)
    artifact_id,_=make_artifact(project)
    with pytest.raises(Failure) as exc: module.upload(Runtime(project.root,project.home),{'artifact_id':artifact_id},'job',lambda *a,**k:None)
    assert exc.value.code=='UPLOAD_CONFLICT' and remote['uploads']==0


def configure_cloud(project,vault):
    source=project.home/'incoming/cloudflare/token.txt'; atomic_bytes(source,b'synthetic-cloudflare-deployment-token')
    imported=vault.import_file('cloud/deploy','api_token',source)
    d=read_json(project.path); d['credential_refs'].update(imported['credential_ref'])
    backend=project.root/'backend'; backend.mkdir()
    (backend/'index.js').write_text('export default {fetch(){return new Response("ok")}}')
    (backend/'wrangler.jsonc').write_text('{"name":"my-worker","main":"index.js","compatibility_date":"2026-10-01"}')
    d['cloudflare']={'account_id':'a'*32,'api_token_ref':'cloud/deploy','targets':{'backend':{'kind':'workers','directory':'backend',
        'wrangler_config':'backend/wrangler.jsonc','default_environment':'production','environments':{'production':{'name':'my-worker',
        'healthcheck':{'url':'https://my-worker.example.com','expected_status':200,'contains':'ok'}}}}}}
    save(project.path,d)
    return Project(project.root,project.home)


def test_jsonc_keeps_urls_and_requires_matching_account(tmp_path,project,vault):
    path=tmp_path/'wrangler.jsonc'
    path.write_text('{ // comment\n"name":"test", "url":"https://example.com/a//b", /*comment*/ "nested":{"x":1,},}')
    assert read_wrangler(path)['url']=='https://example.com/a//b'
    p=configure_cloud(project,vault)
    target=p.target('backend','production')
    target_config(p,target)
    path=p.root/'backend/wrangler.jsonc'
    path.write_text('{"name":"my-worker","account_id":"'+('b'*32)+'"}')
    with pytest.raises(Failure) as exc: target_config(p,target)
    assert exc.value.code=='TARGET_MISMATCH'


def test_deploy_health_failure_resume_does_not_redeploy(project,vault,monkeypatch):
    import workflow_tools.cloudflare as module
    p=configure_cloud(project,vault)
    class API:
        version=None; healthy=False
        def json(self,url,**kwargs):
            versions=[] if not self.version else [{'id':'deployment-1','versions':[{'version_id':self.version,'percentage':100}]}]
            return {'success':True,'result':{'deployments':versions}}
        def request(self,url,**kwargs): return (b'ok' if self.healthy else b'bad'),{'_workflow_status':200}
    api=API(); calls=[]
    monkeypatch.setattr(module,'wrangler',lambda _:['fake-wrangler'])
    def command(args,**kwargs):
        if '--dry-run' not in args:
            calls.append(args)
            api.version='11111111-2222-3333-4444-555555555555'
            assert kwargs['env']['CLOUDFLARE_ACCOUNT_ID']=='a'*32
            assert kwargs['env']['CLOUDFLARE_API_TOKEN']=='synthetic-cloudflare-deployment-token'
            return subprocess.CompletedProcess(args,0,('Current Version ID: '+api.version).encode(),b'')
        assert 'CLOUDFLARE_API_TOKEN' not in kwargs['env']
        return subprocess.CompletedProcess(args,0,b'',b'')
    monkeypatch.setattr(module,'run',command)
    runtime=Runtime(p.root,p.home,http=api)
    params={'target':'backend','environment':'production','source_binding':source_binding(p)}
    with pytest.raises(Failure) as exc: module.deploy(runtime,params,'job',lambda *a,**k:None)
    assert exc.value.code=='HEALTHCHECK_FAILED'
    api.healthy=True
    assert module.deploy(runtime,params,'job',lambda *a,**k:None)['status']=='succeeded'
    assert len(calls)==1


def test_unknown_deploy_does_not_repeat_external_command(project,vault,monkeypatch):
    import workflow_tools.cloudflare as module
    p=configure_cloud(project,vault)
    class API:
        def json(self,*a,**k): return {'success':True,'result':{'deployments':[]}}
    runtime=Runtime(p.root,p.home,http=API())
    monkeypatch.setattr(module,'wrangler',lambda _:['fake-wrangler'])
    calls=[]
    def command(args,**kwargs):
        if '--dry-run' in args: return subprocess.CompletedProcess(args,0,b'',b'')
        calls.append(args); raise subprocess.TimeoutExpired(args,1)
    monkeypatch.setattr(module,'run',command)
    params={'target':'backend','environment':'production','source_binding':source_binding(p)}
    for _ in range(2):
        with pytest.raises(Failure) as exc: module.deploy(runtime,params,'job',lambda *a,**k:None)
        assert exc.value.code=='DEPLOY_UNKNOWN'
    assert len(calls)==1
