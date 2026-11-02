"""Simulated native dispatch; no AppKit sheets or OS authentication are opened."""
import ast
import json
import queue
import threading
import time
import types
from pathlib import Path
import pytest
from ave import desktop_native as native
from ave.desktop_files import NativeFileWork


def controller_method():
    tree=ast.parse(Path(native.__file__).read_text());main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
    cls=next(n for n in main.body if isinstance(n,ast.ClassDef) and n.name=='Controller')
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='episode_action');method.decorator_list=[]
    return compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),native.__file__,'exec')


@pytest.mark.parametrize('outcome',['success','cancel-before','cancel-after','close','workspace-changed','workspace-changed-during-pin'])
def test_native_episode_confirmation_is_off_main_and_bound(outcome):
    main=threading.get_ident();ui=queue.Queue();owner=NativeFileWork(ui.put);started=threading.Event();release=threading.Event();committed=threading.Event();calls=[];replies=[];titles=[];auth=[]
    request={'label':'Synthetic episode','session':'main','decision':'confirm','kind':'final','payload':{'version':1},'dependency_sha256':'a'*64,'dependencies':{}}
    def confirm(token,pin,*,cancelled):
        calls.append((threading.get_ident(),token,pin));started.set()
        if outcome=='cancel-after':committed.set()
        while not release.wait(.01):
            if cancelled() and outcome!='cancel-after':raise ValueError('Episode confirmation cancelled; no checkpoint was recorded')
        if cancelled() and outcome!='cancel-after':raise ValueError('Episode confirmation cancelled; no checkpoint was recorded')
        committed.set();return {'checkpoint':'check_fixture','state':'reviewed exact'}
    checkpoint=types.SimpleNamespace(describe=lambda token:request,confirm_native=confirm)
    session=types.SimpleNamespace(server=types.SimpleNamespace(app=types.SimpleNamespace(episode_checkpoints=checkpoint)))
    def alert(title,*_):
        assert threading.get_ident()==main
        if title=='Confirm episode decision' and outcome=='workspace-changed-during-pin':fake.session=object()
        return 'native-fixture-pin' if title=='Confirm episode decision' else True
    namespace={'__name__':'ave.desktop_native','fixture':False,'json':json,'alert':alert};exec(controller_method(),namespace)
    fake=types.SimpleNamespace(busy=False,session=session,file_work=owner,window=types.SimpleNamespace(setTitle_=lambda value:titles.append((threading.get_ident(),value))),reply=lambda *args:replies.append((threading.get_ident(),args)),unlock=lambda rid,after:auth.append(after))
    args={'token':'captured-prepared-token'}
    try:
        namespace['episode_action'](fake,'confirmEpisode',args,'request-one')
        assert calls==[] and len(auth)==1
        args['token']='changed-after-native-sheet'
        if outcome=='workspace-changed':
            fake.session=object();auth.pop()();assert calls==[] and replies==[];return
        auth.pop()()
        if outcome=='workspace-changed-during-pin':assert calls==[] and replies==[];return
        assert started.wait(1) and owner.busy and calls[0][0]!=main
        assert calls[0][1:]==('captured-prepared-token','native-fixture-pin')
        with pytest.raises(ValueError,match='native operation'):namespace['episode_action'](fake,'confirmEpisode',{'token':'other'},'request-two')
        # Dispatch has returned while verification is deliberately held; main
        # can process other UI work and the existing Cancel menu remains usable.
        heartbeat=[];ui.put(lambda:heartbeat.append(threading.get_ident()));ui.get(timeout=1)();assert heartbeat==[main]
        if outcome=='close':
            before=time.monotonic();owner.close();assert time.monotonic()-before<1
            fake.session=object()
        elif outcome in ('cancel-before','cancel-after'):
            if outcome=='cancel-after':assert committed.wait(1)
            owner.cancel();release.set();owner.thread.join(2)
        else:release.set();owner.thread.join(2)
        while not ui.empty():ui.get()()
        assert not owner.thread.is_alive()
        if outcome=='close':assert not replies and not committed.is_set()
        else:
            assert len(replies)==1 and replies[0][0]==main
            _,(rid,ok,value)=replies[0];assert rid=='request-one'
            assert ok==(outcome!='cancel-before')
            assert ('checkpoint' in value)==ok
            assert all(t[0]==main for t in titles)
            assert 'native-fixture-pin' not in json.dumps(value)
    finally:release.set();owner.close()
