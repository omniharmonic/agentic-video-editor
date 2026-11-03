"""Source-scoped words mapped once per output occurrence; legacy clocks remain supported."""
from __future__ import annotations
import copy
import hashlib
import math
import json
from . import editdoc
from .episode import read_json


def correction_words(words, correction):
    """A correction may replace consecutive words in one continuous output occurrence only."""
    refs = set(correction['word_ids'])
    selected = [w for w in words if w['occurrence_id'] == correction['occurrence_id']
                and w['transcript_sha256'] == correction['transcript_sha256'] and w['word_id'] in refs]
    if not selected:
        return []
    if len({w['continuity_id'] for w in selected}) != 1 or len({w['word_id'] for w in selected}) != len(selected):
        raise ValueError('Caption correction crosses disconnected output spans; select words from one continuous occurrence')
    positions = [words.index(w) for w in selected]
    if positions != list(range(positions[0], positions[-1] + 1)):
        raise ValueError('Caption correction requires consecutive occurrence words')
    return selected


def apply_correction(words,correction,limit):
    selected=correction_words(words,correction)
    if not selected:return words
    first=selected[0];last=selected[-1]
    offsets=[correction.get('start_offset_s',0),correction.get('end_offset_s',0)]
    if any(type(x) not in (int,float) or not math.isfinite(x) or not -limit<=x<=limit for x in offsets):raise ValueError(f'Caption timing offsets must be within {limit:g} seconds')
    start=first['start']+offsets[0];end=last['end']+offsets[1]
    if start<first.get('occurrence_start',0)-1e-8 or end>last.get('occurrence_end',float('inf'))+1e-8 or end<=start:raise ValueError('Caption timing must stay inside its continuous occurrence')
    first['original_word']=first['w'];first['w']=correction['text'];first['start']=start;first['end']=end;first['word_refs']=[copy.deepcopy(w['word_id']) for w in selected]
    return [w for w in words if w is first or w not in selected]


def _timing_limit(ep, doc):
    """Use the exact saved caption policy when present; retain legacy compatibility."""
    from .captions import timing_adjustment_limit
    ref=doc.get('caption_snapshot')
    if not ref:return timing_adjustment_limit(ep)
    path=ep.root/ref['file'];editdoc._safe_components(path)
    if not path.resolve().is_relative_to(ep.root.resolve()):raise ValueError('Caption snapshot leaves project')
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=ref['sha256']:raise ValueError('Caption snapshot changed')
    snapshot=json.loads(data)
    return snapshot.get('timing_adjustment_limit_s',timing_adjustment_limit(ep))


def mapped_words(ep, doc, *, identities=False):
    if not doc.get('independent_project'):
        from .captions import map_words_to_output
        path=ep.artifact('transcript.json');transcript=read_json(path)
        if not identities and not doc.get('caption_edit'):return map_words_to_output(transcript['words'],doc)
        digest=hashlib.sha256(path.read_bytes()).hexdigest();source=next((s for s in doc['sources'].values() if s.get('file')==transcript.get('source')),None)
        source_sha=doc.get('native_source_hashes',{}).get(transcript.get('source')) or (source or {}).get('sha256') or hashlib.sha256(str(transcript.get('source','')).encode()).hexdigest()
        result=map_words_to_output(transcript['words'],doc,identities={'transcript_sha256':digest,'source_sha256':source_sha})
        corrections=doc.get('caption_edit',{}).get('corrections',[])
        if corrections:
            limit=_timing_limit(ep,doc)
            for correction in corrections:result=apply_correction(result,correction,limit)
        return result
    spans = []
    for seg in sorted(doc['tracks']['dialogue'], key=lambda s: s['o0']):
        if seg.get('fill'): continue
        span = dict(seg, segment_ids=[seg['id']])
        prev = spans[-1] if spans else None
        if prev and all(prev[k] == span[k] for k in ('src', 'occurrence_id')) and abs(prev['o1'] - span['o0']) < 1e-8 and abs(prev['e0'] + prev['o1'] - prev['o0'] - span['e0']) < 1e-8:
            prev['o1'] = span['o1']; prev['segment_ids'].append(seg['id'])
        else: spans.append(span)
    result, cache = [], {}
    for span_index, span in enumerate(spans):
        src = doc['sources'][span['src']]; snapshot = src.get('transcript')
        if not snapshot: continue
        key = snapshot['sha256']
        if key not in cache:
            path = ep.root / snapshot['file']
            editdoc._safe_components(path)
            if not path.resolve().is_relative_to(ep.root.resolve()): raise ValueError('Transcript leaves project')
            if hashlib.sha256(path.read_bytes()).hexdigest() != key: raise ValueError('Pinned transcript changed')
            cache[key] = read_json(path).get('words', [])
        e0, e1 = span['e0'], span['e0'] + span['o1'] - span['o0']
        for index, word in enumerate(cache[key]):
            a = word['start'] if snapshot.get('clock')=='episode' else editdoc.episode_time(src, word['start'])
            b = word['end'] if snapshot.get('clock')=='episode' else editdoc.episode_time(src, word['end'])
            if not e0 <= (a+b)/2 < e1: continue
            start, end = span['o0'] + max(a,e0)-e0, span['o0'] + min(b,e1)-e0
            if end <= start: continue
            result.append({**word, 'source_id': src['source_id'], 'transcript_sha256': key,
                'word_id': f"{src['sha256']}:{key}:{index}", 'occurrence_id': span['occurrence_id'],
                'continuity_id': str(span_index), 'segment_ids': span['segment_ids'], 'source_start': editdoc.src_time(src,a), 'source_end': editdoc.src_time(src,b),
                'project_start': a, 'project_end': b, 'start': start, 'end': end,
                'occurrence_start':span['o0'],'occurrence_end': span['o1'], 'speaker': src['source_id'] + ':' + str(word.get('speaker') or 'unassigned')})
    corrections = doc.get('caption_edit', {}).get('corrections', [])
    if corrections:
        limit=_timing_limit(ep,doc)
        for correction in corrections:result=apply_correction(result,correction,limit)
    return result
