"""Whole-source, bounded local manuscript extraction with stable section locators."""
from __future__ import annotations
import hashlib
import re
import zipfile
import xml.etree.ElementTree as ET
from . import production
from .workbench import safe, read, write

MAX_BYTES=32*1024*1024
MAX_TEXT=2_000_000
MAX_PAGES=500
MAX_SECTIONS=20000
EXTS=('.txt','.md','.markdown','.docx','.pdf')

def extract(path, *, with_status=False):
    if path.stat().st_size>MAX_BYTES: raise ValueError('Manuscripts support originals up to 32 MiB; choose an explicit chapter file')
    suffix=path.suffix.lower();pages=[];page_status=[];warnings=[]
    if suffix in ('.txt','.md','.markdown'):
        try: pages=[(None,path.read_bytes().decode('utf-8'))]
        except UnicodeDecodeError: raise ValueError('Writing must be UTF-8') from None
    elif suffix=='.docx':
        with zipfile.ZipFile(path) as z:
            infos=z.infolist()
            if len(infos)>2000 or sum(i.file_size for i in infos)>64*1024*1024 or any(i.file_size>32*1024*1024 or (i.compress_size and i.file_size/i.compress_size>200) for i in infos): raise ValueError('DOCX archive expansion budget exceeded; export a chapter as UTF-8')
            names=[i.filename for i in infos]
            if any(n.startswith('/') or '\\' in n or '..' in n.split('/') for n in names): raise ValueError('DOCX contains unsafe archive member names')
            if any(n.startswith(('word/footnotes','word/endnotes','word/header','word/footer','word/comments','word/glossary')) for n in names): raise ValueError('DOCX notes, headers, footers, comments or glossary require explicit export to UTF-8; these parts are not silently omitted')
            if len(names)!=len(set(names)) or 'word/document.xml' not in names: raise ValueError('DOCX has missing or duplicate document entries')
            xml=z.read('word/document.xml')
            try: xml=xml.decode('utf-8-sig')
            except UnicodeDecodeError: raise ValueError('DOCX XML must be UTF-8; export a UTF-8 text copy') from None
            declaration=re.search(r'<\?xml[^>]*encoding=[\"\']([^\"\']+)',xml,re.I)
            if '\x00' in xml or (declaration and declaration.group(1).lower() not in ('utf-8','utf8')): raise ValueError('DOCX XML must be UTF-8; export a UTF-8 text copy')
            if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper(): raise ValueError('DOCX entities are unsupported')
            root=ET.fromstring(xml)
            ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            if any(True for _ in root.iter(ns+'txbxContent')): raise ValueError('DOCX text boxes require explicit export to UTF-8; no silent omission')
            body=root.find(ns+'body')
            if root.tag!=ns+'document' or body is None: raise ValueError('DOCX main document body is missing')
            unsupported={'altChunk','drawing','pict','object','sym','fldSimple','instrText','delText','ins','del','moveFrom','moveTo','contentPart','footnoteReference','endnoteReference','commentReference','subDoc','numPr'}
            for node in body.iter():
                if node.tag in {ns+t for t in unsupported} or (node.text and node.text.strip() and node.tag!=ns+'t'):
                    raise ValueError('DOCX unsupported textual structure '+node.tag.rsplit('}',1)[-1]+': export an explicit UTF-8 copy; no content was silently omitted')
            separators={ns+'tab':'\t',ns+'br':'\n',ns+'cr':'\n',ns+'noBreakHyphen':'\u2011',ns+'softHyphen':'\u00ad'}
            pages=[(None,'\n\n'.join(''.join((t.text or '') if t.tag==ns+'t' else separators.get(t.tag,'') for t in p.iter()) for p in body.iter(ns+'p')))]
    elif suffix=='.pdf':
        from pypdf import PdfReader, filters
        # The dedicated parser worker also has a wall-time alarm. Bound decoded
        # stream allocation before get_contents/extract_text, not only afterwards.
        for name in ("ZLIB_MAX_OUTPUT_LENGTH","LZW_MAX_OUTPUT_LENGTH","RUN_LENGTH_MAX_OUTPUT_LENGTH","MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH","MAX_DECLARED_STREAM_LENGTH"):
            setattr(filters,name,16*1024*1024)
        pdf=PdfReader(path,strict=True)
        if pdf.is_encrypted: raise ValueError('Encrypted PDF: save an unlocked text-bearing copy')
        if not 1<=len(pdf.pages)<=MAX_PAGES: raise ValueError('PDF supports 1–500 pages; select an explicit chapter file')
        total=0
        for n,page in enumerate(pdf.pages,1):
            content=page.get_contents()
            if content is not None and len(content.get_data())>16*1024*1024: raise ValueError('PDF page expansion exceeds 16 MiB')
            text=page.extract_text() or '';total+=len(text)
            if total>MAX_TEXT: raise ValueError('Extracted text exceeds 2 million characters; select an explicit chapter file')
            pages.append((n,text))
            status='text_extracted' if text.strip() else 'no_selectable_text'
            page_status.append({'page':n,'status':status,'characters':len(text)})
            if not text.strip(): warnings.append(f'PDF page {n}: no selectable text; may be blank or require OCR. No OCR was performed.')
        if not any(t.strip() for _,t in pages): raise ValueError('OCR required: this PDF contains no selectable text. Original retained; import a text-bearing copy. No OCR was performed.')
    else: raise ValueError('Use UTF-8, DOCX or a text-bearing PDF')
    if sum(len(t) for _,t in pages)>MAX_TEXT: raise ValueError('Extracted text exceeds 2 million characters; choose an explicit chapter file')
    rows=[];offset=0
    canonical_text='\n\n'.join(text for _,text in pages)
    for page,text in pages:
        for match in re.finditer(r'[^\n]+(?:\n(?!\s*\n)[^\n]+)*',text):
            # A bounded section may be selected without returning the whole book.
            matched=match.group();body=matched.strip();leading=len(matched)-len(matched.lstrip())
            if not body: continue
            for start in range(0,len(body),8000):
                part=body[start:start+8000]
                rows.append({'id':f'section_{len(rows)+1:05d}','page':page,'start':offset+match.start()+leading+start,'end':offset+match.start()+leading+start+len(part),'text':part})
        offset+=len(text)+2
    if not rows or len(rows)>MAX_SECTIONS: raise ValueError('No text sections, or section budget exceeded')
    result={'sections':rows,'text':canonical_text,'page_status':page_status,'warnings':warnings,'extraction_complete':not warnings,'locator_basis':'original UTF-8 text' if suffix in ('.txt','.md','.markdown') else 'canonical extracted text'}
    return result if with_status else rows

def execute(studio,source):
    sp=studio.sp;path=safe(sp,sp.root/source['file'])
    from ..publish.safety import sha256_file
    if sha256_file(path)!=source['sha256']: raise ValueError('Manuscript original checksum changed')
    extracted=extract(path,with_status=True);rows=extracted['sections'];directory=sp.work/'production'/'manuscripts'/source['source_id']
    for i in range(0,len(rows),25): write(sp,directory/f'page-{i//25}.json',{'sections':rows[i:i+25]})
    metadata={k:extracted[k] for k in ('page_status','warnings','extraction_complete','locator_basis')}
    from . import desk
    desk._atomic_bytes(safe(sp,directory/'extracted.txt'),extracted['text'].encode('utf-8'))
    write(sp,directory/'index.json',{**metadata,'v':1,'parser':'manuscript.v1','source_sha256':source['sha256'],'sections':len(rows),'pages':(len(rows)+24)//25,'characters':sum(len(r['text']) for r in rows)})
    from .workbench import source_index
    idx=source_index(sp)
    for s in idx['sources']:
        if s['source_id']==source['source_id']: s.update(ready=True,sections=len(rows),pages=(len(rows)+24)//25,extraction_complete=extracted['extraction_complete'])
    write(sp,sp.work/'production'/'sources.json',idx)
    return {'source_id':source['source_id'],'sections':len(rows),'pages':(len(rows)+24)//25,'parser':'manuscript.v1',**metadata,'note':('Extracted selectable text retained; some PDF pages need attention.' if extracted['warnings'] else 'Supported text extraction retained. Browse bounded section pages; no text was silently truncated.')}

def page(sp,sid,n=0):
    from .workbench import recording
    src=recording(sp,sid)
    if not src or src.get('kind')!='document' or not src.get('ready'): raise ValueError('Choose an extracted manuscript')
    if type(n) is not int or not 0<=n<src['pages']: raise ValueError('Section page outside manuscript')
    result=read(sp,sp.work/'production'/'manuscripts'/sid/f'page-{n}.json')
    meta=read(sp,sp.work/'production'/'manuscripts'/sid/'index.json')
    return {**result,**{k:meta.get(k) for k in ('page_status','warnings','extraction_complete','locator_basis')},'page':n,'pages':src['pages'],'source_id':sid,'source_sha256':src['sha256']}

def section(sp,sid,section_id):
    if not isinstance(section_id,str) or not re.fullmatch(r'section_[0-9]{5}',section_id): raise ValueError('Invalid section locator')
    n=int(section_id[8:])-1
    return next((r for r in page(sp,sid,n//25)['sections'] if r['id']==section_id),None)
