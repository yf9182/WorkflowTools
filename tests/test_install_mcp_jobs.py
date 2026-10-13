import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import time
import tomllib
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from workflow_tools.common import Failure, read_json, save
from workflow_tools.config import Project
from workflow_tools.install import install
from workflow_tools.runtime import Runtime


def test_repeat_install_preserves_user_config_skills_and_mcp(project):
    user_mcp = project.root / '.codex/config.toml'
    user_mcp.parent.mkdir()
    user_mcp.write_text('model = "keep-user-model"\n[mcp_servers.other]\ncommand="keep-other-server"\n')
    install(project.root, project.home)
    snapshot=project.path.read_bytes()
    skill=project.root/'.agents/skills/ios-testflight/SKILL.md'
    skill.write_text(skill.read_text()+'\n用户增加的要求。\n')
    result=install(project.root, project.home)
    assert project.path.read_bytes()==snapshot
    assert '用户增加的要求' in skill.read_text()
    assert '.agents/skills/ios-testflight/SKILL.md' in result['preserved_files']
    d=tomllib.loads(user_mcp.read_text())
    assert d['model']=='keep-user-model'
    assert d['mcp_servers']['other']['command']=='keep-other-server'
    assert 'workflow_tools.cli' in d['mcp_servers']['workflow_tools']['args']
    assert 'default_tools_approval_mode' not in d['mcp_servers']['workflow_tools']


def test_config_refuses_inline_secret_and_paths(project):
    d=copy.deepcopy(project.data)
    d['cloudflare']['api_token']='do-not-expose'
    save(project.path,d)
    with pytest.raises(Failure) as exc: Project(project.root,project.home)
    assert exc.value.code=='INLINE_SECRET_REFUSED'
    save(project.path,project.data)
    for p in ['../outside','/outside']:
        with pytest.raises(Failure): project.resolve(p)


def test_gate_isolation_upload_does_not_need_google_or_p12(project,vault):
    runtime=Runtime(project.root,project.home)
    assert runtime.doctor('ios_upload_testflight')['status']=='ready'
    assert runtime.doctor('ios_build')['status']=='blocked'
    d=copy.deepcopy(runtime.project.data)
    d['analytics']['sources']=['ga4']
    save(project.path,d)
    runtime=Runtime(project.root,project.home)
    assert runtime.doctor('analytics_ga4')['status']=='blocked'
    assert runtime.doctor('ios_upload_testflight')['status']=='ready'


def test_background_job_is_durable_and_resumes_without_new_job(project):
    runtime=Runtime(project.root,project.home)
    job=runtime.analytics_fetch(sources=['ga4'],start_date='2026-09-01',end_date='2026-09-02')
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        current=runtime.job_status(job['job_id'])
        if current['status'] not in ('queued','running'): break
        time.sleep(.1)
    assert current['status']=='blocked'
    assert current['blockers']
    same=runtime.analytics_fetch(sources=['ga4'],start_date='2026-09-01',end_date='2026-09-02')
    assert same['job_id']==job['job_id']
    resumed=runtime.jobs.resume(job['job_id'],launch=False)
    assert resumed['generation']==2 and resumed['job_id']==job['job_id']
    stale=runtime.jobs.execute(job['job_id'],1,runtime)
    assert stale['generation']==2 and stale['status']=='queued'
    result=runtime.jobs.execute(job['job_id'],2,runtime)
    assert result['status']=='blocked'


def test_config_change_blocks_resume(project):
    runtime=Runtime(project.root,project.home)
    record=runtime.jobs.submit('analytics_fetch',{},launch=False)
    record['status']='interrupted'
    save(runtime.jobs.path(record['job_id']),record)
    d=copy.deepcopy(project.data); d['ios']['app_store_id']='999'
    save(project.path,d)
    with pytest.raises(Failure) as exc:
        Runtime(project.root,project.home).jobs.resume(record['job_id'],launch=False)
    assert exc.value.code=='PROJECT_CHANGED'


def test_real_stdio_mcp_discovers_all_tools_and_matches_cli(project,vault):
    async def exercise():
        params=StdioServerParameters(command=sys.executable,args=['-m','workflow_tools.cli','--project',str(project.root),'--home',str(project.home),'mcp'])
        async with Client(params) as client:
            result=await client.list_tools()
            tools=result.tools if hasattr(result,'tools') else result
            names={t.name for t in tools}
            assert names=={'workflow_doctor','ios_build','ios_upload_testflight','ios_testflight_status','analytics_fetch',
                'analytics_report','cloudflare_deploy','cloudflare_status','workflow_job_status','workflow_job_resume'}
            response=await client.call_tool('workflow_doctor',{'capability':'ios_upload_testflight'})
            content=response.structured_content
            if 'result' in content: content=content['result']
            assert content==Runtime(project.root,project.home).doctor('ios_upload_testflight')
            assert 'PRIVATE KEY' not in json.dumps(content)
            response=await client.call_tool('ios_upload_testflight',{'artifact_id':'../anything'})
            content=response.structured_content
            if 'result' in content: content=content['result']
            assert content['status']=='blocked'
    asyncio.run(exercise())
