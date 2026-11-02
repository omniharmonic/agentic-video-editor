"""Persisted visual plans use real disk state, independent from publishing."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from ave.studio import feed_plan, store
from ave.studio.events import AGENT
from ave.studio.service import open_studio
from ave.studio.ui.api import StudioApp, map_error


def draft_studio(bare):
    studio = open_studio(bare.root)
    for title in ('Morning light', 'Coffee in the studio', 'Making things', 'Evening walk'):
        studio.create_post(idea=title)
    return studio


def keys(plan):
    return [c['key'] for c in plan['cells']]


def move(studio, plan, key=None, before=None, account=''):
    return feed_plan.move(studio, account=account, expected_rev=plan['rev'], snapshot=plan['snapshot'], key=key or plan['cells'][0]['key'], before=before)


def authoritative_bytes(sp):
    return {str(p.relative_to(sp.root)): p.read_bytes() for folder in (sp.posts_dir, sp.publish, sp.schedule_dir, sp.ledger_dir, sp.accounts_dir)
            for p in folder.rglob('*') if p.is_file()}


def test_accountless_order_survives_reopen_without_changing_posts_or_schedule(bare):
    studio = draft_studio(bare)
    initial = feed_plan.read(studio)
    assert len(initial['cells']) == 4
    assert not (bare.work / 'feed-plans').exists()  # GET has no side effects.
    before = authoritative_bytes(bare)
    saved = move(studio, initial)
    assert keys(saved) == keys(initial)[1:] + keys(initial)[:1]
    assert saved['rev'] == 1
    assert keys(feed_plan.read(open_studio(bare.root))) == keys(saved)
    assert authoritative_bytes(bare) == before


def test_stale_tabs_cannot_overwrite_and_simultaneous_writes_have_one_winner(bare):
    studio = draft_studio(bare)
    plan = feed_plan.read(studio)
    gate = threading.Barrier(2)
    def writer(index):
        gate.wait()
        try:
            return move(open_studio(bare.root), plan, key=keys(plan)[index])['rev']
        except feed_plan.PlanConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(writer, [0, 1]))
    assert sorted(map(str, result)) == ['1', 'conflict']
    with pytest.raises(feed_plan.PlanConflict):
        move(studio, plan)


def test_changed_content_or_new_draft_invalidates_snapshot(bare):
    studio = draft_studio(bare)
    initial = feed_plan.read(studio)
    studio.create_post(idea='A new draft in another window')
    with pytest.raises(feed_plan.PlanConflict):
        move(studio, initial)
    current = feed_plan.read(studio)
    p = store.load_post(bare, current['cells'][0]['post'])
    p['copy']['base'] = 'Changed caption'
    store.save_post(bare, p, expected_rev=p['rev'], actor=AGENT, op='edit')
    with pytest.raises(feed_plan.PlanConflict):
        move(studio, current)


def test_published_tiles_are_locked_and_account_scopes_are_independent(built):
    from ave.publish import queue
    from ave.studio import state
    post = store.load_post(built.sp, built.posts['in_review'])
    item = state.load_queue_item(built.sp, post['variants'][0])
    item['status'] = 'published'  # Synthetic history only; no publisher is called.
    queue.save(built.sp, item)
    studio = open_studio(built.sp.root, clock=built.clock)
    local = feed_plan.read(studio)
    locked = [c for c in local['cells'] if c['locked']]
    assert locked
    with pytest.raises(feed_plan.PlanConflict, match='keep their place'):
        move(studio, local, key=locked[0]['key'])
    account = feed_plan.read(studio, 'ig-main')
    original = keys(account)
    assert len([c for c in account['cells'] if not c['locked']]) >= 2
    changed = move(studio, account, account='ig-main')
    assert keys(changed) != original
    assert keys(feed_plan.read(studio)) == keys(local)
    assert [c['key'] for c in changed['cells'] if c['locked']] == [c['key'] for c in account['cells'] if c['locked']]


def test_api_validates_and_returns_real_saved_plan(bare):
    studio = draft_studio(bare)
    app = StudioApp(studio)
    status, initial = app.get('feed-plan', {})
    assert status == 200
    status, updated = app.post('feed-plan', {'account':'', 'expected_rev':initial['rev'], 'snapshot':initial['snapshot'], 'key':keys(initial)[0], 'before':None})
    assert status == 200 and updated['rev'] == 1
    with pytest.raises(feed_plan.PlanConflict) as stale:
        app.post('feed-plan', {'expected_rev':0, 'snapshot':initial['snapshot'], 'key':keys(initial)[0]})
    assert map_error(stale.value).status == 409
    for payload in ({'expected_rev':True, 'snapshot':updated['snapshot'], 'key':keys(updated)[0]},
                    {'expected_rev':1, 'key':keys(updated)[0]},
                    {'account':'../outside', 'expected_rev':1, 'snapshot':updated['snapshot'], 'key':keys(updated)[0]}):
        with pytest.raises(Exception) as invalid:
            app.post('feed-plan', payload)
        assert map_error(invalid.value).status == 400


def test_symlink_sidecars_are_refused(bare, tmp_path):
    studio = draft_studio(bare)
    initial = feed_plan.read(studio)
    folder = bare.work / 'feed-plans'
    folder.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    (folder / 'local.lock').symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        move(studio, initial)
    assert outside.read_text() == 'untouched'


def test_fixture_plan_is_explicitly_read_only():
    from ave.studio.service import FixtureStudio
    app = StudioApp(FixtureStudio())
    status, result = app.get('feed-plan', {})
    assert status == 200 and result['read_only'] is True
    with pytest.raises(Exception) as error:
        app.post('feed-plan', {'expected_rev':0, 'snapshot':result['snapshot'], 'key':result['cells'][0]['key']})
    assert map_error(error.value).status == 501


def test_assigning_a_variant_preserves_existing_local_order(bare):
    from studio_helpers import write_account
    studio = draft_studio(bare)
    initial = feed_plan.read(studio)
    saved = move(studio, initial)
    original = keys(saved)
    p = studio.post(saved['cells'][1]['post'])
    write_account(bare, 'ig-new', 'instagram')
    studio.add_variant(p['id'], account='ig-new', platform='instagram', post_type='feed', expected_rev=p['rev'])
    assert keys(feed_plan.read(studio)) == original


def test_publishing_transition_rejects_stale_move_and_removes_tile_from_mutable_plan(built):
    from ave.publish import queue
    from ave.studio import state
    studio = open_studio(built.sp.root, clock=built.clock)
    initial = feed_plan.read(studio)
    post = store.load_post(built.sp, built.posts['in_review'])
    item = state.load_queue_item(built.sp, post['variants'][0])
    item['status'] = 'publishing'  # Synthetic in-flight state, without an external send.
    queue.save(built.sp, item)
    with pytest.raises(feed_plan.PlanConflict):
        move(studio, initial)
    current = feed_plan.read(studio)
    cell = next(c for c in current['cells'] if c['post'] == post['id'])
    assert cell['locked'] and cell['status'] == 'publishing'
    with pytest.raises(feed_plan.PlanConflict):
        move(studio, current, key=cell['key'])


def test_fixture_flag_cannot_mutate_a_backed_studio(bare):
    studio = draft_studio(bare)
    app = StudioApp(studio, fixtures=True)
    _, initial = app.get('feed-plan', {})
    assert initial['read_only']
    with pytest.raises(Exception) as refused:
        app.post('feed-plan', {'expected_rev':0,'snapshot':initial['snapshot'],'key':keys(initial)[0]})
    assert map_error(refused.value).status == 501
    assert not (bare.work / 'feed-plans').exists()


@pytest.mark.parametrize('account', ['', 'ig-first', 'ig-second'])
def test_all_dropped_and_archived_failed_assignments_never_become_unassigned_drafts(bare, account):
    from studio_helpers import write_account
    studio = open_studio(bare.root)
    for aid in ('ig-first', 'ig-second'):
        write_account(bare, aid, 'instagram')
    unassigned = studio.create_post(idea='A genuine unassigned draft')
    retired = []
    for kind in ('dropped', 'failed'):
        p = studio.create_post(idea=f'Archived {kind} post')
        p = studio.add_variant(p['id'], account='ig-first', platform='instagram', post_type='feed', expected_rev=p['rev'])
        raw = store.load_post(bare, p['id'])
        if kind == 'dropped':
            raw['variants'][0]['dropped'] = True
        else:
            raw['variants'][0]['result'] = {'state':'failed'}
        saved = store.save_post(bare, raw, expected_rev=raw['rev'], actor=AGENT, op='synthetic-archive')
        assert saved['stage'] == 'archived'
        retired.append(saved['id'])
    plan = feed_plan.read(studio, account)
    assert [c['post'] for c in plan['cells']] == [unassigned['id']]
    assert plan['cells'][0]['unassigned'] is True
    assert not any(c['post'] in retired for c in plan['cells'])


def test_archived_published_variant_remains_locked_in_own_account_and_local_history(built):
    from ave.publish import queue
    from ave.studio import state
    studio = open_studio(built.sp.root, clock=built.clock)
    post = store.load_post(built.sp, built.posts['in_review'])
    v = post['variants'][0]
    item = state.load_queue_item(built.sp, v)
    item['status'] = 'published'
    queue.save(built.sp, item)
    v['result'] = {'state':'published','url':'https://example.invalid/synthetic-published'}
    saved = store.save_post(built.sp, post, expected_rev=post['rev'], actor=AGENT, op='synthetic-published')
    assert saved['stage'] == 'archived'
    for account in ('', v['account']):
        plan = feed_plan.read(studio, account)
        cell = next(c for c in plan['cells'] if c['post'] == post['id'])
        assert cell['locked'] and cell['status'] == 'published' and not cell['unassigned']
        with pytest.raises(feed_plan.PlanConflict):
            move(studio, plan, key=cell['key'], account=account)
    other = next(a['id'] for a in built.accounts if a['id'] != v['account'])
    assert not any(c['post'] == post['id'] for c in feed_plan.read(studio, other)['cells'])
