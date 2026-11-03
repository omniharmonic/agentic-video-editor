"""Coverage preserves exact bounds while separating edge precision from omissions."""
import copy
from ave import editdoc
from ave.studio.episode_workshop import assembly_coverage


def coverage(short_gap=False):
    doc={'duration':.999,'trim':{'in':.0004,'out':1.0004},'sources':{'a':{'file':'raw/a.wav'}},'tracks':{'dialogue':[{'src':'a','e0':.001,'o0':0,'o1':.999}]}}
    if short_gap:doc['duration']=.979;doc['tracks']['dialogue']=[{'src':'a','e0':.001,'o0':0,'o1':.019},{'src':'a','e0':.04,'o0':.019,'o1':.979}]
    before=copy.deepcopy(doc);result=assembly_coverage(doc,{'raw/a.wav':{'duration':2}});assert doc==before
    return result


def test_edge_rounding_keeps_exact_numbers_and_internal_gap():
    c=coverage();assert c['boundary_precision_seconds']==editdoc.EPS
    assert len(c['boundary_rounding'])==2 and c['confirmed_bounds']=={'in':.0004,'out':1.0004}
    assert c['omitted_inside_seconds']==sum(x['end']-x['start'] for x in c['excluded_inside_bounds'])
    c=coverage(True);assert len(c['boundary_rounding'])==2
    actual=[g for g in c['excluded_inside_bounds'] if g not in c['boundary_rounding']]
    assert len(actual)==1 and actual[0]['start']==.02 and actual[0]['end']==.04
