"""Public integration evidence and native-selected configuration save."""
from pathlib import Path
import json
import os
import socket
import secrets
from .studio.connections import read, directory


def readiness(sp):
    installed=Path('/Applications/DaVinci Resolve/DaVinci Resolve.app').is_dir()
    try:
        with socket.create_connection(('127.0.0.1',49632),timeout=.15):listening=True
    except OSError:listening=False
    return {'resolve':{'installed':installed,'bridge_port_listening':listening,'bridge_verified':False,
            'note':'A local listener is present; protocol/Resolve connection remains unverified.' if listening else 'Open Resolve, then choose Workspace → Scripts → resolve_bridge once per launch.'},
        'canva':{'connected':False,'note':'No Canva account has been observed by this app. Open Canva to sign in; your external agent’s Canva connector requires its own consent.'},
        'mcp':{'configured_is_connected':False,'observed':read(directory(sp)/'agent-observed.json'),
            'note':'Save the exact workspace configuration for your agent. A saved file is not an observed MCP connection.'}}


def save_agent_config(target,config):
    target=Path(target)
    if target.is_symlink() or not target.parent.is_dir():raise ValueError('Choose a regular configuration file')
    if target.exists() and target.stat().st_size>1000000:raise ValueError('Configuration file is too large')
    raw=json.dumps(config,indent=2).encode()+b'\n';backup=None
    if target.exists():
        backup=target.with_name(target.name+'.backup-'+secrets.token_hex(6))
        with backup.open('xb') as f:f.write(target.read_bytes())
        backup.chmod(0o600)
    temp=target.with_name('.'+target.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        with temp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        temp.chmod(0o600);os.replace(temp,target)
    finally:temp.unlink(missing_ok=True)
    return {'saved':True,'backup_created':backup is not None,'connected':False}


def fixture_connections(accounts):
    from .platforms.connection_verify import PRODUCTS
    if isinstance(accounts,dict):accounts=accounts.get('accounts',[])
    rows=[]
    for i,a in enumerate(accounts):
        product=next((k for k,v in PRODUCTS.items() if v[0]==a['platform']),None)
        rows.append({'id':a['id'],'label':a.get('label',a['id']),'platform':a['platform'],'mode':'manual' if i==0 else a.get('mode','graph'),'public':{},'revision':'sample',
            'product':product,'token_family':PRODUCTS.get(product,('','Not selected'))[1],'stored':['token'] if i>0 else [],'credential_state':'partial_update' if i==1 else 'stored' if i>1 else 'unknown',
            'evidence':{'state':'permission_denied','checked_at':'2026-10-29T12:00:00Z','source':'synthetic example','grants':[],'roles':[],'tasks':[],'limitations':['Identity and grants have not been verified for this sample.']} if i==1 else None,
            'verified':False,'live_enabled':False,'live_tested':False,'paused':False,'manual_available':True,'secret_fields':['token']})
    return {'accounts':rows,'products':{k:{'platform':v[0],'family':v[1],'version':v[2]} for k,v in PRODUCTS.items()},'host':{'revision':'sample','config':{},'stored':[],'state':'not_configured'},'paused':False,'sender':{},
        'service':{'state':'stale','reason':'No recent check. The Mac may be asleep or background execution may be disabled. Repair or restart this workspace’s service.','revision':'sample','mode':'rehearsal','interval':60,'workspace':'Sample studio','executable':'/Applications/AVE Studio.app/Contents/MacOS/AVE Studio (fictional)','last_check':1793275200,'loaded':True,'running':False},
        'tools':{'resolve':{'installed':False,'bridge_verified':False,'note':'Sample: no active bridge observed.'},'canva':{'connected':False,'note':'No account observed.'},'mcp':{'observed':{},'note':'Generated configuration is not a connection.'}},'native':False,'fixtures':True,'manual_note':'Local profiles and manual packages need no credential, media host or sender.'}
