"""Studios share session memory, but never inherit the episode editing checklist."""
import json
from pathlib import Path

import pytest

from ave import cli, progress, state
from ave.episode import Episode
from ave.studio.project import StudioProject
from ave.studio.service import Studio


def run(capsys, *args):
    rc = cli.main([str(a) for a in args])
    return rc, capsys.readouterr().out


def jrun(capsys, *args):
    rc, out = run(capsys, *args)
    assert rc == 0
    return json.loads(out)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_init_resume_and_generated_instructions_are_studio_aware(tmp_path, capsys):
    root = tmp_path / 'studio'
    result = jrun(capsys, 'studio', 'init', root, '--name', 'A shared practice')
    assert 'error' not in result['project_files']
    memory = (root / 'PROGRESS.md').read_text()
    instructions = (root / 'AGENTS.md').read_text()
    assert 'content studio' in instructions and 'ave-studio' in instructions
    assert 'video-editing project' not in instructions
    assert 'Probe footage' not in memory and 'Assembly edit' not in memory
    assert 'Add source material or an idea' in memory
    before = snapshot(root)
    rc, text = run(capsys, 'resume', root)
    assert rc == 0 and 'Studio: A shared practice' in text
    assert 'NEXT: Add source material or an idea' in text
    assert 'studio resume' in text and 'unpause' in text
    assert snapshot(root) == before
    status = jrun(capsys, 'status', root)
    assert status['kind'] == 'studio' and status['next']['stage'] == 'source'
    assert status['next']['skill'] == 'ave-studio-ideas'
    assert not status['next']['human_gate']
    assert 'ave ingest ' not in status['next']['command']


def test_studio_log_question_resolve_learn_and_preserve_human_notes(tmp_path, capsys):
    root = tmp_path / 'studio'
    jrun(capsys, 'studio', 'init', root)
    memory = root / 'PROGRESS.md'
    memory.write_text('A note from Imani.\n\n' + memory.read_text() + '\n## Human notes\nLeave room for silence.\n')
    question = jrun(capsys, 'log', root, 'Which passage should open the post?', '--kind', 'question')
    assert question['question'] == 1
    assert 'Which passage' in run(capsys, 'resume', root)[1]
    assert jrun(capsys, 'log', root, 'Imani selected the quiet opening.', '--resolve', '1')['resolved'] == 1
    assert jrun(capsys, 'learn', root, 'Review the complete thought before making a short.', '--skill', 'ave-shorts')['id'] == 'F1'
    report = jrun(capsys, 'progress', root)
    assert not report['open_questions']
    assert report['next']['stage'] == 'source'
    assert jrun(capsys, 'learn', root, '--list')['proposed']
    text = memory.read_text()
    assert text.startswith('A note from Imani.')
    assert '## Human notes\nLeave room for silence.' in text
    assert 'Imani selected the quiet opening.' in text
    assert progress.START in text and progress.END in text
    assert not jrun(capsys, 'progress', root)['checklist_changed']


def test_studio_inventory_follows_posts_sources_accounts_and_review(tmp_path, capsys):
    root = tmp_path / 'studio'
    sp = StudioProject(root)
    sp.create(title='A working show')
    post = Studio(sp).create_post(idea='A grounded thought')
    status = jrun(capsys, 'status', root)
    assert status['counts']['ideas'] == 1 and status['next']['stage'] == 'ideas'
    Studio(sp).edit_post(post['id'], {'copy.base':'A complete first draft.'}, expected_rev=post['rev'])
    status = jrun(capsys, 'status', root)
    assert status['counts']['drafts'] == 1 and status['next']['stage'] == 'drafts'
    assert not status['next']['human_gate']
    assert 'Probe footage' not in progress.checklist_block(sp)


def test_ordinary_episode_status_and_assemble_resolution_remain_episode_only(tmp_path, capsys):
    root = tmp_path / 'episode'
    jrun(capsys, 'init-episode', root)
    status = jrun(capsys, 'status', root)
    assert status['next']['stage'] == 'ingest'
    assert 'Probe footage' in run(capsys, 'resume', root)[1]
    studio = StudioProject(tmp_path / 'studio')
    studio.create()
    with pytest.raises(SystemExit, match='not an episode'):
        cli.main(['assemble', str(studio.root)])


def test_workspace_bare_studio_name_and_read_only_resume_without_memory(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AVE_WORKSPACE', str(tmp_path))
    sp = StudioProject(tmp_path / 'studio')
    sp.create()
    before = snapshot(sp.root)
    assert run(capsys, 'resume', 'studio')[0] == 0
    assert snapshot(sp.root) == before
    assert not (sp.root / 'PROGRESS.md').exists()
    assert jrun(capsys, 'status', 'studio')['kind'] == 'studio'


def test_review_and_feedback_are_surfaced_without_crossing_gates(tmp_path, capsys):
    from studio_helpers import add_asset, make_png, queue_variant, write_account
    from ave.studio import store
    from ave.studio.events import AGENT
    from ave.studio.model import new_post, new_variant
    sp = StudioProject(tmp_path / 'studio')
    sp.create()
    write_account(sp, 'ig-main', 'instagram')
    asset = add_asset(sp, make_png(sp.work / 'card.png'))
    post = new_post(idea='A complete thought', copy={'base':'A complete thought.'})
    variant = new_variant(account='ig-main', platform='instagram', post_type='feed')
    post['variants'] = [variant]
    queue_variant(sp, post, variant, asset)
    saved = store.save_post(sp, post, expected_rev=None, actor=AGENT, op='create')
    before = snapshot(sp.root)
    report = jrun(capsys, 'status', sp.root)
    assert report['counts']['review'] == 1 and report['counts']['accounts'] == 1
    assert report['next']['stage'] == 'review' and report['next']['human_gate']
    assert snapshot(sp.root) == before
    Studio(sp).add_feedback(saved['id'], 'rework_copy', note='Shorten the opening', by='Imani')
    before = snapshot(sp.root)
    report = jrun(capsys, 'status', sp.root)
    assert report['counts']['feedback'] == 1 and report['next']['stage'] == 'feedback'
    assert not report['next']['human_gate']
    assert snapshot(sp.root) == before
    assert 'feedback next' in report['next']['command']


def test_source_inventory_and_paused_state_are_not_episode_progress(tmp_path, capsys):
    from ave.episode import write_json
    from ave.studio import dispatch
    sp = StudioProject(tmp_path / 'studio')
    sp.create()
    write_json(sp.work / 'content/writing.json', {'files':[{'file':'essay.md','passages':[{'text':'We listen.'}]}]})
    report = jrun(capsys, 'status', sp.root)
    assert report['counts']['sources'] == 1 and report['next']['stage'] == 'ideas'
    dispatch.pause(sp, by='test')
    before = snapshot(sp.root)
    report = jrun(capsys, 'status', sp.root)
    assert report['paused'] and report['next']['human_gate']
    assert report['next']['stage'] == 'paused'
    assert 'PAUSED' in run(capsys, 'resume', sp.root)[1]
    assert snapshot(sp.root) == before


def test_refresh_corrects_legacy_studio_checklist_but_keeps_human_sections(tmp_path, capsys):
    sp = StudioProject(tmp_path / 'studio')
    sp.create()
    text = '# Existing studio memory\n\n## Pipeline checklist\n\n' + progress.START + '\n- [ ] Probe footage\n' + progress.END
    text += '\n\n## Decisions\n\nImani asked for a slower pace.\n'
    (sp.root / 'PROGRESS.md').write_text(text)
    report = jrun(capsys, 'progress', sp.root)
    assert report['checklist_changed']
    after = (sp.root / 'PROGRESS.md').read_text()
    assert 'Probe footage' not in after
    assert 'Imani asked for a slower pace.' in after
    assert after.startswith('# Existing studio memory')


def test_expired_approval_still_requires_review_in_orientation(tmp_path, monkeypatch):
    from ave.studio import progress as studio_progress
    sp = StudioProject(tmp_path / 'studio')
    sp.create()
    original = studio_progress.Studio
    class ExpiredStudio(original):
        def posts(self):
            return [{'stage':'active','variants':[{'status':'approved','flags':['approval_expired']}]}]
    monkeypatch.setattr(studio_progress, 'Studio', ExpiredStudio)
    report = studio_progress.status(sp)
    assert report['counts']['review'] == 1
    assert report['next']['stage'] == 'review' and report['next']['human_gate']
