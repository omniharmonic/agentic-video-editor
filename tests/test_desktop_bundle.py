"""Run against an explicitly selected frozen bundle; ordinary suite tests sources."""
from pathlib import Path
import json
import os
import subprocess
import pytest


def test_packaging_entry_never_analyzes_checkout():
    root=Path(__file__).resolve().parents[1]
    spec=(root/'packaging/macos/ave-studio.spec').read_text()
    assert "pathex=[mcp_root, os.path.join(mcp_root, 'src')]" in spec and "target_arch='arm64'" in spec
    assert "'LocalAuthentication'" in spec
    assert "copy(runtime,data)" in (root/'scripts/build_desktop.py').read_text()
    source=(root/'src/ave/desktop_worker.py').read_text()
    assert "os.environ['AVE_AGENT'] = '1'" in source
    assert '--mcp' in source and '--production-worker' in source


def test_actual_frozen_diagnostics_and_mcp(tmp_path):
    executable=os.environ.get('AVE_TEST_BUNDLE_EXECUTABLE')
    if not executable:pytest.skip('Set AVE_TEST_BUNDLE_EXECUTABLE to the actual standalone binary')
    env=dict(os.environ,PATH='/usr/bin:/bin',XDG_DATA_HOME=str(tmp_path/'data'),XDG_CONFIG_HOME=str(tmp_path/'config'))
    result=subprocess.run([executable,'--diagnostics-json',str(tmp_path/'diagnostics.json')],cwd=tmp_path,env=env,capture_output=True,timeout=120)
    assert result.returncode==0,result.stderr
    data=json.loads((tmp_path/'diagnostics.json').read_text())
    assert data['frozen'] and data['ok'] and data['architecture']=='arm64'
    assert 'AVE Studio.app' in data['resources'] and '/src/' not in data['resources']
    from ave.studio.project import StudioProject
    sp=StudioProject(tmp_path/'studio');sp.create()
    request={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}}
    result=subprocess.run([executable,'--mcp','--studio',str(sp.root)],input=json.dumps(request)+'\n',text=True,capture_output=True,env=env,cwd=tmp_path,timeout=60)
    assert result.returncode==0,result.stderr
    reply=json.loads(result.stdout)
    assert reply['result']['serverInfo']['name']=='ave-studio'
