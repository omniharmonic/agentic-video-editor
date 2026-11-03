"""Occurrence-bound phrase editing through real GUI controls; native acts simulated."""
import copy,json,re
import pytest
from playwright.sync_api import expect
from .test_workbench_browser import browser,rig,wait_for
from .test_episode_workshop import ready,run,confirm,FakeAuthority
from ave.studio import episode_registry as R,episode_workshop as E,episode_checkpoints as C
from ave.episode import write_json
from ave.publish.safety import sha256_file


def phrase_ready(studio,tmp_path):
    eid,_,app=ready(studio,tmp_path)
    with R.resolve(studio,eid) as (ep,row):
        path=ep.artifact('transcript.json');data=R.json_file(path)
        data['words']=[{'w':f'word{i}'+('.' if i==80 else ''),'start':.3+i*.04,'end':.32+i*.04,'speaker':'S2' if i==80 else 'S1'} for i in range(81)]
        data['utterances']=[];write_json(path,data);before=sha256_file(path)
    confirm(app,eid,'speakers',{'names':{'S1':'Synthetic host','S2':'Synthetic guest'}})
    confirm(app,eid,'mapping',C.latest(studio,eid,'mapping','main')['payload']);confirm(app,eid,'bounds',{'in':.25,'out':3.75})
    a=run(studio,eid,'episode-assembly');a={**a['edit'],'artifact_id':a['artifact']['id']};confirm(app,eid,'assembly',a)
    r=run(studio,eid,'episode-rough',assembly=a,policy='gentle',restore=[]);r={**r['edit'],'artifact_id':r['artifact']['id']};confirm(app,eid,'rough',r)
    q=run(studio,eid,'episode-sequence',rough=r,show='Phrase fixture',episode_title='Exact words',number='',hosts=[],cold_open=None,opening={'enabled':False,'components':[]},closing={'enabled':False,'components':[]});confirm(app,eid,'sequence',q['review_payload'])
    f=run(studio,eid,'episode-final',rough=r,sequence=q['review_payload'],gain_db=0,caption_style='podcast',burn=False)
    confirm(app,eid,'final',{**f['edit'],'artifact_id':f['artifact']['id']})
    return eid,app,path,before


def open_editor(page,server):
    page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click();page.get_by_role('button',name='Revise sound & captions',exact=True).click();page.get_by_text('Correct caption wording',exact=True).click()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('width',[1440,390])
def test_phrase_cross_page_reopens_full_saved_range_and_restores(rig,browser,tmp_path,synthetic_final_asr,width):
    studio,server=rig;eid,app,source,before=phrase_ready(studio,tmp_path);server.app.native_authority=FakeAuthority();context=browser.new_context(viewport={'width':width,'height':1000 if width==1440 else 844});page=context.new_page();page.set_default_timeout(30000)
    try:
        open_editor(page,server);page.get_by_role('button',name=re.compile(r' word39$')).click();page.get_by_role('button',name='Include next word',exact=True).click();expect(page.get_by_text('Original: word39 word40',exact=True)).to_be_visible()
        page.get_by_role('button',name='Remove last word',exact=True).click();expect(page.get_by_text('Original: word39',exact=True)).to_be_visible();page.get_by_role('button',name='Include next word',exact=True).click();page.get_by_label('Corrected word',exact=True).fill('Joined.');page.get_by_role('button',name='Apply correction',exact=True).click();page.get_by_role('button',name='Render final programme',exact=True).click();page.get_by_text('Final cut v2',exact=False).first.wait_for()
        page.locator('.ew-caption-row summary').filter(has_text='Joined.').first.click();page.get_by_role('button',name='Edit Joined.',exact=True).click();expect(page.get_by_text('Original: word39 word40',exact=True)).to_be_visible();expect(page.get_by_label('Corrected word',exact=True)).to_have_value('Joined.')
        page.get_by_role('button',name='Restore original phrase',exact=True).click();page.get_by_role('button',name='Render final programme',exact=True).click();page.get_by_text('Final cut v3',exact=False).first.wait_for();expect(page.locator('.ew-transcript')).not_to_contain_text('Joined.');assert sha256_file(source)==before
        page.get_by_role('button',name='Revise sound & captions',exact=True).click();page.get_by_text('Correct caption wording',exact=True).click();page.get_by_role('button',name=re.compile(r' word0$')).click()
        for _ in range(49):page.get_by_role('button',name='Include next word',exact=True).click()
        expect(page.get_by_role('button',name='Include next word',exact=True)).to_be_disabled();page.get_by_role('button',name='Remove last word',exact=True).click();expect(page.get_by_role('button',name='Include next word',exact=True)).to_be_enabled()
    finally:context.close();app.close()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('boundary',['continuity_id','occurrence_id','transcript_sha256'])
def test_phrase_ignores_replaced_delayed_page_and_refuses_boundary(rig,browser,tmp_path,synthetic_final_asr,boundary):
    studio,server=rig;eid,app,_,_=phrase_ready(studio,tmp_path);context=browser.new_context();page=context.new_page();pending=[];errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.route('**/view/caption-words?session=main&page=1',lambda route:pending.append(route));open_editor(page,server)
        page.get_by_role('button',name=re.compile(r' word39$')).click();wait_for(lambda:bool(pending),page)
        page.get_by_role('button',name=re.compile(r' word0$')).click();expect(page.get_by_text('Original: word0',exact=True)).to_be_visible()
        # The obsolete request deliberately returns a stale binding; it must
        # not replace the newer selection or display a stale error there.
        stale=copy.deepcopy(E.caption_words(studio,eid,'main',1));stale['rough']['sha256']='0'*64
        pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps(stale));page.wait_for_timeout(100);expect(page.get_by_text('Original: word0',exact=True)).to_be_visible();expect(page.locator('.mw-error')).to_be_hidden()
        page.get_by_role('button',name=re.compile(r' word39$')).click();wait_for(lambda:bool(pending),page)
        changed=copy.deepcopy(E.caption_words(studio,eid,'main',1));changed['words'][0][boundary]='different-output-span'
        pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps(changed));expect(page.get_by_role('button',name='Include next word',exact=True)).to_be_disabled();assert not errors
        page.route('**/view/caption-words?session=main&page=2',lambda route:pending.append(route));page.get_by_role('button',name='Next caption words',exact=True).click();page.get_by_role('button',name=re.compile(r' word79$')).click();wait_for(lambda:bool(pending),page)
        page.get_by_role('button',name='Close',exact=True).click();pending.pop().fulfill(status=200,content_type='application/json',body=json.dumps(stale));page.wait_for_timeout(100);assert page.locator('.episode-workshop').count()==0 and not errors
    finally:
        for route in pending:route.abort()
        context.close();app.close()


@pytest.mark.legacy_ui
def test_phrase_current_changed_rough_clears_stale_controls(rig,browser,tmp_path,synthetic_final_asr):
    studio,server=rig;eid,app,_,_=phrase_ready(studio,tmp_path);context=browser.new_context();page=context.new_page()
    try:
        stale=copy.deepcopy(E.caption_words(studio,eid,'main',1));stale['rough']['sha256']='0'*64
        page.route('**/view/caption-words?session=main&page=1',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(stale)))
        open_editor(page,server);page.get_by_role('button',name=re.compile(r' word39$')).click();expect(page.locator('.mw-error')).to_contain_text('reviewed rough cut changed');assert page.get_by_label('Corrected word',exact=True).count()==0 and page.get_by_role('button',name='Apply correction',exact=True).count()==0
    finally:context.close();app.close()


@pytest.mark.legacy_ui
def test_rough_shows_nonlexical_decisions_linked_by_cut_id(rig,browser,tmp_path,synthetic_final_asr):
    from .test_episode_release import finished
    studio,server=rig;eid,_,app,_=finished(studio,tmp_path);context=browser.new_context();page=context.new_page()
    try:
        def projection(route):
            response=route.fetch();data=response.json();report=data['roughs'][-1]['report']
            # A nonlexical decision can point to a merged pause cut without
            # being duplicated in that cut's original pause-decision IDs.
            report['detected_cuts']=[{'id':'k-fixture','e0':.4,'e1':.5,'decisions':['p-fixture']}]
            report['decisions']=[{'id':'p-fixture','type':'dead_air','text':'Pause','reason':'Synthetic pause shortening','confidence':.9,'cut':'k-fixture','review':True},{'id':'n-fixture','type':'nonlexical','text':'','reason':'Synthetic sound removed inside this pause; inspect it','confidence':.8,'cut':'k-fixture','review':True}]
            route.fulfill(response=response,json=data)
        page.route('**/view?session=main',projection);page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click();page.get_by_role('button',name=re.compile(r'^Rough cut')).click()
        card=page.get_by_label('Restore cut k-fixture').locator('xpath=ancestor::div[contains(@class,"ew-decision")]');expect(card).to_contain_text('Synthetic sound removed inside this pause; inspect it');expect(card).to_contain_text('confidence 0.8')
    finally:context.close();app.close()


@pytest.mark.legacy_ui
@pytest.mark.parametrize('width',[1440,390])
def test_deep_rough_audition_reveals_player_and_returns_exact_decision(rig,browser,tmp_path,synthetic_final_asr,width):
    from .test_episode_release import finished
    studio,server=rig;eid,_,app,_=finished(studio,tmp_path);context=browser.new_context(viewport={'width':width,'height':1000 if width==1440 else 844});page=context.new_page()
    try:
        def projection(route):
            response=route.fetch();data=response.json();report=data['roughs'][-1]['report'];report['detected_cuts']=[{'id':f'k-{i}','e0':.4,'e1':.5,'decisions':[], 'comparison':{'input_clock':'assembly output','assembly_output_s':2.5,'rough_output_s':2.3}} for i in range(100)];report['decisions']=[];route.fulfill(response=response,json=data)
        page.route('**/view?session=main',projection);page.goto(server.url()+'#/workbench');page.get_by_role('button',name='Episodes',exact=True).click();page.get_by_role('button',name='Synthetic conversation',exact=True).click();page.get_by_role('button',name=re.compile(r'^Rough cut')).click()
        check=page.get_by_label('Restore cut k-99',exact=True);check.check();card=check.locator('xpath=ancestor::div[contains(@class,"ew-decision")]');main=page.locator('.ew-main');player=page.locator('.ew-player audio')
        for label,seek in [('Before · assembly',.5),('After · rough seam',.3)]:
            action=card.get_by_role('button',name=label,exact=True);action.scroll_into_view_if_needed();scroll=main.evaluate('(el)=>el.scrollTop');assert scroll>1000;action.click()
            wait_for(lambda:player.evaluate('(el,t)=>el.readyState>=2&&!el.seeking&&el.currentTime>=t',seek),page)
            assert page.locator('.ew-player').evaluate('(el)=>{const p=el.getBoundingClientRect(),m=el.closest(".ew-main").getBoundingClientRect();return p.top>=m.top-1&&p.bottom<=m.bottom+1}')
            page.get_by_role('button',name='Return to decision k-99',exact=True).click();expect(action).to_be_focused();assert abs(main.evaluate('(el)=>el.scrollTop')-scroll)<3;expect(check).to_be_checked()
    finally:context.close();app.close()
