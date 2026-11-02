"""Built-in synthetic native acceptance workspace. Never opens a caller path."""
from pathlib import Path
import tempfile
from PIL import Image, ImageDraw


def create():
    # Isolate synthetic signatures from the real user's local approval key.
    # This key cannot unlock NativeAuthority or access any caller workspace.
    import os, secrets
    os.environ['AVE_APPROVAL_KEY'] = secrets.token_hex(32)
    from .publish.safety import reset_approval_key_cache
    reset_approval_key_cache()
    from .studio.project import StudioProject
    from .studio.service import open_studio
    from .studio import model, store, authoring, manual_export
    from .studio.events import AGENT
    from . import library
    temporary = tempfile.TemporaryDirectory(prefix='ave-native-fixture-')
    root = Path(temporary.name)
    sp = StudioProject(root / 'studio'); sp.create(title='Synthetic native acceptance',tz='UTC')
    studio = open_studio(sp.root,publishers={},host=None); studio.native_authoring=True
    src=root/'synthetic.png';im=Image.new('RGB',(1080,1350),'#ede4d1')
    d=ImageDraw.Draw(im);d.rectangle((120,160,960,1120),fill='#506e63');d.text((180,600),'SYNTHETIC NATIVE ACCEPTANCE',fill='white');im.save(src)
    asset=library.add_file(sp,src,source={'type':'render'},by='synthetic-fixture',alt='A generated green rectangle on cream')
    account=authoring.profile_save(studio,{'platform':'instagram','profile':{'display_name':'Synthetic Studio','handle':'synthetic_example','bio':'Fictional acceptance fixture'}},AGENT)
    post=model.new_post(idea='Synthetic package acceptance',copy={'base':'A synthetic package for native Save and cancellation checks.'},media=[{'asset':asset['id'],'role':'image','slot':1,'alt':'Generated green rectangle'}])
    post['variants']=[model.new_variant(account=account['id'],platform='instagram',post_type='feed')]
    post=store.save_post(sp,post,expected_rev=None,actor=AGENT,op='fixture')
    manual_export.build(sp,post['id'],draft=True,expected_rev=post['rev'])
    return studio,temporary
