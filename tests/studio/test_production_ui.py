"""Real browser journeys over mutable fictional work. No paid or live services."""
import threading
import time
from pathlib import Path
import pytest
from ave.studio import campaigns, production as P, store
from ave.studio.service import open_studio
from ave.studio.project import StudioProject
from ave.studio.ui.server import StudioServer
from studio_helpers import add_asset, FakeClock, write_account


@pytest.fixture(scope='module')
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b=p.chromium.launch();yield b;b.close()


def build(tmp_path):
    from PIL import Image,ImageDraw,ImageFont
    sp=StudioProject(tmp_path/'production-sample');sp.create(title='Field Notes · Sample')
    studio=open_studio(sp.root,clock=FakeClock('2026-10-29T12:00:00+00:00'))
    write_account(sp,'author','instagram')
    c=campaigns.new(sp,'The quiet season',goal='Invite readers into the making of the book',start='2026-10-26',end='2026-11-15',themes=['Making','Reading','Place'])
    person=P.save_config(studio,'people',{'name':'Sofia Chen','local_role_label':'Writer'},expected_rev=0)['items'][0]
    reviewer=P.save_config(studio,'people',{'name':'Maren Okafor','local_role_label':'Editor'},expected_rev=1)['items'][-1]
    titles=['Cover reveal: The quiet season','Inside the writing room','A passage about belonging','The landscape behind chapter two','Reading notes for launch week','A first look at the printed pages','Author conversation: finding the story','Three ways to slow down','A letter to early readers','Behind the cover illustration','Launch day reading','Bookshop event invitation']
    created=[]
    for i,title in enumerate(titles):
        media=[]
        if i!=8:
            im=Image.new('RGB',(640,480),[(219,224,206),(219,207,186),(185,206,214)][i%3]);d=ImageDraw.Draw(im)
            if i%4==0:
                d.rectangle((0,320,640,480),fill='#9e8066');d.polygon([(225,60),(424,78),(424,400),(225,380)],fill='#2b5149');d.polygon([(424,78),(448,66),(448,390),(424,400)],fill='#ebe7d6');d.text((249,130),'THE QUIET\n\nSEASON',fill='#ece5c9',font_size=27);d.line((250,275,390,275),fill='#c0b279',width=3)
            elif i%4==1:
                d.rectangle((50,20,360,300),fill='#57716b');d.rectangle((65,35,345,285),fill='#b8ced2');d.line((205,35,205,285),fill='#f0eee7',width=12);d.line((65,155,345,155),fill='#f0eee7',width=12);d.rectangle((0,325,640,480),fill='#8e6b4f');d.polygon([(130,350),(270,335),(300,450),(150,460)],fill='#eee9d9');d.ellipse((415,320,500,380),fill='#ece7d8');d.rectangle((417,350,499,420),fill='#ece7d8');d.ellipse((425,330,490,366),fill='#725038')
            elif i%4==2:
                d.polygon([(80,100),(318,75),(320,395),(80,420)],fill='#f8f2df');d.polygon([(320,75),(565,100),(565,420),(320,395)],fill='#eae1ce');
                for y in range(135,350,24): d.line((120,y,275,y-8),fill='#a29785',width=3);d.line((360,y-8,520,y),fill='#a29785',width=3)
            else:
                d.rectangle((0,0,640,270),fill='#c4d9de');d.ellipse((420,45,495,120),fill='#f2e2ba');d.polygon([(0,260),(120,150),(310,300),(460,210),(640,285),(640,480),(0,480)],fill='#7f9581');d.polygon([(0,340),(220,245),(430,375),(640,320),(640,480),(0,480)],fill='#49695a');d.line((350,480,240,330),fill='#c8b795',width=35)
            path=sp.work/f'scene-{i}.jpg';im.save(path);a=add_asset(sp,path,alt=title);media=[{'asset':a['id'],'slot':0,'role':'image','alt':title}]
            if i==2:media.append({**media[0],'slot':1})
        if i==6:
            import subprocess,json,hashlib
            video=sp.work/'author-conversation.mp4'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-loop','1','-i',str(path),'-t','1','-vf','scale=320:240','-pix_fmt','yuv420p',str(video)],check=True)
            digest=hashlib.sha256(video.read_bytes()).hexdigest()
            rec={**a,'id':'a_'+digest[:12],'sha256':digest,'file':sp.rel(video),'kind':'video','ext':'.mp4','bytes':video.stat().st_size,'w':320,'h':240,'duration':1,'fps':25}
            with (sp.library_dir/'assets.jsonl').open('a') as f:f.write(json.dumps(rec)+'\n')
            media=[{'asset':rec['id'],'slot':0,'role':'video','alt':title},{**media[0],'slot':1,'role':'cover'}]
        p=studio.create_post(idea=title,copy={'base':title+'\nNotes from the making of a book.'},media=media,campaign=c['id'])
        if i in (2,6,7):p=studio.add_variant(p['id'],account='author',platform='instagram',post_type={2:'carousel',6:'reel',7:'story'}[i],expected_rev=p['rev'])
        status=['idea','writing','design','review','ready','blocked'][i%6]
        fields={'owner':person['id'] if i%3 else reviewer['id'],'reviewer':reviewer['id'],'status':status,'blocked_reason':'Awaiting author photo' if status=='blocked' else '', 'priority':'high' if i%4==0 else 'normal','start_date':f'2026-10-{26+i%4:02}','due_date':f'2026-11-{2+i%9:02}', 'brief':{'audience':'Thoughtful readers','objective':'Invite readers into the story'},'checklist':[{'id':'task_copy','title':'Review the words','owner':person['id'],'done':status=='ready','due_date':None}]}
        if i==0:fields['due_date']='2026-10-28'
        if i==8:fields.update(start_date=None,due_date=None)
        p=P.update(studio,p['id'],fields,expected_rev=p['rev']);created.append(p)
    p=created[3];P.update(studio,p['id'],{'depends_on':[created[4]['id']]},expected_rev=p['rev'])
    campaign=campaigns.load(sp,c['id']);campaigns.edit(sp,c['id'],{'milestones':[{'id':'first_reading','title':'First public reading','date':'2026-11-06','done':False}]},expected_rev=campaign['rev'])
    raw=store.load_post(sp,created[6]['id']);raw['variants'][0]['schedule']={'local':'2026-11-09T09:00','tz':'UTC','utc':'2026-11-09T09:00:00Z'}
    from ave.studio.events import AGENT
    store.save_post(sp,raw,expected_rev=raw['rev'],actor=AGENT,op='synthetic.schedule')
    raw=store.load_post(sp,created[7]['id']);raw['variants'][0]['result']={'state':'published','url':'https://example.invalid/sample-post','at':'2026-10-28T09:00:00Z'}
    store.save_post(sp,raw,expected_rev=raw['rev'],actor=AGENT,op='synthetic.receipt')
    return studio


@pytest.fixture
def rig(tmp_path):
    studio=build(tmp_path);server=StudioServer(studio,pin='424242')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield studio,server
    server.shutdown();thread.join(5);server.server_close()


def wait_for(fn,page):
    end=time.time()+6
    while time.time()<end:
        if fn():return
        page.wait_for_timeout(50)
    assert fn()


@pytest.mark.legacy_ui
def test_production_browser_journey(rig,browser):
    studio,server=rig;page=browser.new_page(viewport={'width':1440,'height':960});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(server.url()+'#/pipeline');page.locator('.production-table tr[data-post]').first.wait_for()
        page.get_by_role('button',name='+ New item',exact=True).first.click();page.get_by_label('Item title',exact=True).fill('Launch checklist');page.get_by_role('button',name='Create item',exact=True).click()
        page.get_by_role('button',name='Close inspector',exact=True).wait_for();page.get_by_role('button',name='Close inspector',exact=True).click()
        p=next(p for p in store.list_posts(studio.sp) if (p.get('idea') or {}).get('text')=='Launch checklist')
        page.get_by_label('Owner for Launch checklist',exact=True).select_option(label='Sofia Chen')
        wait_for(lambda:store.load_post(studio.sp,p['id']).get('production',{}).get('owner'),page)
        page.get_by_label('Due date for Launch checklist',exact=True).fill('2026-11-06');page.keyboard.press('Tab')
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['due_date']=='2026-11-06',page)
        page.get_by_role('button',name='Board',exact=True).click()
        card=page.locator(f'.production-card[data-post="{p["id"]}"]');target=page.locator('.production-lane[data-stage="writing"]')
        card.drag_to(target)
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['status']=='writing',page)
        page.get_by_role('button',name='Launch checklist',exact=True).click();page.get_by_role('button',name='Tasks',exact=True).click()
        page.get_by_label('Task title',exact=True).fill('Proof the launch note');page.get_by_role('button',name='Add task',exact=True).click()
        page.get_by_label('Complete Proof the launch note',exact=True).check()
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['checklist'][0]['done'],page)
        page.get_by_role('button',name='Discussion',exact=True).click();page.get_by_label('Comment',exact=True).fill('Keep the language warm.');page.get_by_role('button',name='Add comment',exact=True).click()
        page.get_by_text('Keep the language warm.',exact=True).first.wait_for()
        page.get_by_label('Reply to',exact=True).select_option(label='Keep the language warm.');page.get_by_label('Comment',exact=True).fill('The final line is simpler now.');page.get_by_role('button',name='Add comment',exact=True).click()
        page.get_by_text('The final line is simpler now.',exact=True).first.wait_for();page.get_by_role('button',name='Resolve thread',exact=True).first.click()
        wait_for(lambda:P.comments(studio,p['id'])[0]['resolved'],page)
        page.get_by_role('button',name='Close inspector',exact=True).click();page.get_by_role('button',name='Timeline',exact=True).click();page.locator('.prod-time-bar').first.wait_for()
        page.reload();page.locator('.production-table').wait_for();assert store.load_post(studio.sp,p['id'])['production']['due_date']=='2026-11-06'
        assert not errors
    finally:page.close()


def test_production_api_conflict_media_and_no_queue_follow(rig):
    from studio.test_api import Client
    studio,server=rig;c=Client(server)
    p=store.list_posts(studio.sp)[0]
    status,_,body=c.req('POST',f'/api/studio/posts/{p["id"]}/production',{'expected_rev':p['rev'],'fields':{'priority':'urgent'}})
    assert status==200
    status,_,body=c.req('POST',f'/api/studio/posts/{p["id"]}/production',{'expected_rev':p['rev'],'fields':{'priority':'low'}})
    assert status==409 and __import__('json').loads(body)['latest']['rev']==p['rev']+1
    status,_,_=c.req('POST',f'/api/studio/posts/{p["id"]}/production',{'expected_rev':p['rev']+1,'fields':{'priority':'low'},'approve':True})
    assert status==400
    projected=P.project(studio,store.load_post(studio.sp,p['id']))
    if projected['thumbnail']['url']:
        assert c.req('GET',projected['thumbnail']['url'])[0]==200


@pytest.mark.legacy_ui
def test_keyboard_edit_conflict_recovery_and_large_inventory(rig,browser):
    studio,server=rig
    for i in range(63):studio.create_post(idea=f'Writing idea {i:02}')
    page=browser.new_page(viewport={'width':1440,'height':960})
    try:
        page.goto(server.url()+'#/pipeline');page.locator('.production-table tr[data-post]').first.wait_for()
        assert page.locator('.production-table tr[data-post]').count()==75
        title='Inside the writing room';p=next(p for p in store.list_posts(studio.sp) if (p.get('idea') or {}).get('text')==title)
        owner=page.get_by_label('Owner for '+title,exact=True);owner.focus();page.keyboard.press('ArrowDown');page.keyboard.press('Tab')
        wait_for(lambda:store.load_post(studio.sp,p['id'])['rev']>p['rev'],page)
        # Change the disk while a real user has an unsaved field. Polling must not erase it.
        control=page.get_by_label('Title for '+title,exact=True);control.fill('My preserved title')
        current=store.load_post(studio.sp,p['id']);P.update(studio,p['id'],{'priority':'urgent'},expected_rev=current['rev'])
        page.keyboard.press('Tab');page.get_by_role('button',name='Apply my fields to latest',exact=True).wait_for();page.get_by_role('button',name='Apply my fields to latest',exact=True).click()
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['title']=='My preserved title',page)
        assert store.load_post(studio.sp,p['id'])['production']['priority']=='urgent'
        page.get_by_role('button',name='Board',exact=True).click();stage=page.get_by_label('Move My preserved title to stage',exact=True);stage.focus();page.keyboard.press('ArrowDown');page.keyboard.press('Tab')
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['status']=='design',page)
        page.get_by_role('button',name='My preserved title',exact=True).click();page.get_by_role('button',name='Close inspector',exact=True).click();assert page.locator(':focus').count()==1
        page.get_by_role('searchbox',name='Search pipeline',exact=True).fill('Writing idea 0');assert page.locator('.production-card').count()==10
    finally:page.close()


@pytest.fixture
def review_rig(tmp_path):
    """Small disk-backed review regression workspace, with no publishing writes."""
    from ave.episode import write_json
    sp=StudioProject(tmp_path/'review-work');sp.create(title='Review regressions')
    studio=open_studio(sp.root,clock=FakeClock('2026-10-29T12:00:00+00:00'))
    for account in ['ig-main','ig-other','ig-none']:write_account(sp,account,'instagram')
    c=campaigns.new(sp,'Review campaign',accounts=['ig-main','ig-other','ig-none'],per_week={'ig-main':2,'ig-other':7})
    person=P.save_config(studio,'people',{'name':'Editor'},expected_rev=0)['items'][0]
    write_json(sp.work/'content'/'writing.json',{'files':[{'file':f'chapter{i}.md','title':f'Chapter {i}'} for i in range(1,4)]})
    sources=[{'source_id':'chapter1.md','label':'Selected passage','locator':{'start':10,'end':20}},{'source_id':'chapter2.md','label':'Context'}]
    p=studio.create_post(idea='Review evidence',campaign=c['id'])
    p=P.update(studio,p['id'],{'sources':sources,'checklist':[{'id':'check','title':'Check text','done':False}]},expected_rev=p['rev'])
    server=StudioServer(studio,pin='424242');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield studio,server,p,c,person,sources
    server.shutdown();thread.join(5);server.server_close()


@pytest.mark.legacy_ui
def test_review_brief_preserves_sources_and_draft_through_stage_and_poll(review_rig,browser):
    studio,server,p,c,person,sources=review_rig
    page=browser.new_page(viewport={'width':1440,'height':960})
    try:
        page.goto(server.url()+'&poll=200#/pipeline');page.get_by_role('button',name='Open',exact=True).click()
        page.get_by_label('Objective',exact=True).fill('First saved objective')
        page.get_by_role('button',name='Save brief',exact=True).click()
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['brief'].get('objective')=='First saved objective',page)
        assert store.load_post(studio.sp,p['id'])['production']['sources']==sources
        page.get_by_label('Objective',exact=True).fill('Unsaved objective survives')
        page.get_by_label('Message',exact=True).fill('Keep this message too')
        page.get_by_label('Inspector owner',exact=True).select_option(person['id'])
        page.get_by_label('Production due date',exact=True).fill('2026-11-12')
        page.locator('.production-inspector').get_by_label('Move Review evidence to stage',exact=True).select_option('writing')
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['status']=='writing',page)
        assert page.get_by_label('Objective',exact=True).input_value()=='Unsaved objective survives'
        # Force a real polling refresh after focus leaves the inspector.
        page.get_by_role('heading',name='Content production',exact=True).click()
        with page.expect_response(lambda r:'/api/studio/production' in r.url and r.request.method=='GET'):
            studio.create_post(idea='Arrived during editing')
        page.get_by_label('Title for Arrived during editing',exact=True).wait_for()
        assert page.get_by_label('Objective',exact=True).input_value()=='Unsaved objective survives'
        assert page.get_by_label('Message',exact=True).input_value()=='Keep this message too'
        assert page.get_by_label('Inspector owner',exact=True).input_value()==person['id']
        assert page.get_by_label('Production due date',exact=True).input_value()=='2026-11-12'
        page.get_by_role('button',name='Save brief',exact=True).click()
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['brief'].get('objective')=='Unsaved objective survives',page)
        saved=store.load_post(studio.sp,p['id'])['production']
        assert saved['owner']==person['id'] and saved['due_date']=='2026-11-12' and saved['sources']==sources
        # Deliberately replacing the first source still retains the remaining references.
        page.get_by_label('Source material',exact=True).select_option('chapter3.md')
        page.get_by_role('button',name='Save brief',exact=True).click()
        wait_for(lambda:store.load_post(studio.sp,p['id'])['production']['sources'][0]['source_id']=='chapter3.md',page)
        assert store.load_post(studio.sp,p['id'])['production']['sources'][1:]==sources[1:]
    finally:page.close()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('case,label',[('valid','Approved'),('stale','Approval stale'),('expired','Approval expired'),('uncertain','Delivery uncertain')])
def test_review_release_labels_follow_authoritative_flags(review_rig,browser,monkeypatch,case,label):
    from ave.studio import state,policy
    from ave.studio.events import AGENT
    studio,server,p,c,person,sources=review_rig
    p=studio.add_variant(p['id'],account='ig-main',platform='instagram',post_type='feed',expected_rev=p['rev'])
    raw=store.load_post(studio.sp,p['id']);raw['variants'][0]['schedule']={'utc':'2026-11-09T09:00:00Z','local':'2026-11-09T09:00','tz':'UTC'}
    store.save_post(studio.sp,raw,expected_rev=raw['rev'],actor=AGENT,op='synthetic.schedule')
    item={'id':'synthetic-read','status':'approved','approvals':[{'by':'fixture','at':studio.now().isoformat()}],'results':{}}
    if case=='uncertain':item.update(status='failed',results={'ig-main':{'state':'sending'}})
    monkeypatch.setattr(state,'load_queue_item',lambda *a:item)
    monkeypatch.setattr(state,'stale_approval',lambda *a:['synthetic mismatch'] if case=='stale' else [])
    monkeypatch.setattr(policy,'approval_expired',lambda *a:case=='expired')
    derived=studio.post(p['id'])['variants'][0]
    if case!='valid':assert {'stale':'approval_stale','expired':'approval_expired','uncertain':'uncertain'}[case] in derived['flags']
    page=browser.new_page()
    try:
        page.goto(server.url()+'#/pipeline');page.get_by_role('button',name='Open',exact=True).click()
        text=page.locator('.prod-release').inner_text()
        assert label in text and '2026-11-09T09:00' in text
        if case!='valid':assert 'ig-main: Approved' not in text
        if case=='uncertain':assert 'Sending' not in text
        page.get_by_role('button',name='Close inspector',exact=True).click()
        if case!='valid':assert label in page.locator('.prod-publication').inner_text()
    finally:page.close()


@pytest.mark.legacy_ui
def test_review_workflow_order_retirement_and_mixed_views(review_rig,browser):
    studio,server,p,c,person,sources=review_rig;page=browser.new_page(viewport={'width':1440,'height':960})
    order=['blocked','writing','idea','review','ready']
    try:
        page.goto(server.url()+'#/pipeline');page.get_by_role('button',name='Workflows',exact=True).click()
        page.get_by_label('Existing workflow',exact=True).select_option('wf_default')
        page.get_by_label('Workflow stages',exact=True).fill('\n'.join(f'{id}: {id.title()}' for id in order))
        page.get_by_role('button',name='Save',exact=True).click();page.locator('.production-dialog').wait_for(state='detached')
        page.reload();page.get_by_role('button',name='Board',exact=True).click()
        assert page.locator('.production-lane').evaluate_all('(xs)=>xs.map(x=>x.dataset.stage)')==order
        page.get_by_role('button',name='Table',exact=True).click()
        assert [t.split()[1].lower() for t in page.locator('.prod-group th > button:first-child').all_text_contents()]==order
        assert [s['id'] for s in P.config(studio.sp,'workflows')['items'][0]['stages']]==order
        wf=P.save_config(studio,'workflows',{'name':'Specialist','stages':[{'id':'ready','label':'Ready'},{'id':'capture','label':'Capture'},{'id':'blocked','label':'Blocked'}],'readiness':{'checklist':True,'dependencies':True}},expected_rev=1)['items'][-1]
        other=studio.create_post(idea='Specialist work');P.update(studio,other['id'],{'workflow':wf['id'],'status':'capture'},expected_rev=other['rev'])
        page.reload();page.get_by_role('button',name='Board',exact=True).click()
        assert page.locator('.production-lane').evaluate_all('(xs)=>xs.map(x=>x.dataset.stage)')==order+['capture']
        page.get_by_role('searchbox',name='Search pipeline',exact=True).fill('Specialist work')
        assert page.locator('.production-lane').evaluate_all('(xs)=>xs.map(x=>x.dataset.stage)')==['ready','capture','blocked']
    finally:page.close()
