import gzip
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts import reader_intake as intake
from scripts.reader_quality import NearIndex
from scripts.reader_bulk import Bulk, DriveSource
from scripts.reader_eval import metrics, validate_gold, prediction, prepare, report
from scripts.reader_local import Held, infer, budget_for
from reader_colors import RATING_COLORS, _processing_gate


def test_html_body_cleanup_and_json_detection():
    article = '<html><nav>MENU secret</nav><article><h1>Printed research</h1>' + ''.join('<p>This synthetic article explains durable checkpoints and cautious source preservation in a document library. It contains substantive sentences for extraction.</p>' for _ in range(8)) + '</article><script>badscript()</script></html>'
    for name,payload in [('a.html',article.encode()),('a.json',json.dumps({'content':article,'tags':['rating-blue']}).encode())]:
        doc = intake.parse_document(name,payload)
        assert 'badscript' not in doc['content']
        assert 'MENU secret' not in doc['content']
        assert 'durable checkpoints' in doc['content']


def test_extraction_failure_is_deferred_and_source_preserved(tmp_path):
    source = tmp_path/'source.zip'
    with zipfile.ZipFile(source,'w') as z:
        z.writestr('bad.html','<html><script>x()</script></html>')
        z.writestr('good.json',json.dumps({'content':'kept','tags':['rating-blue']}))
    before = source.read_bytes()
    engine = intake.Intake(tmp_path/'out')
    result = engine.run(source)
    assert result['stats']['deferred_extraction']==1
    assert result['stats']['canonical_new']==1
    assert source.read_bytes()==before
    with gzip.open(engine.work/'issues.jsonl.gz','rt') as f:
        assert json.loads(next(f))['reason']=='HTML_EXTRACTION_FAILED'
    engine.close()


@pytest.mark.parametrize('tags',[['rating-blue'],['남색'],['🏆'],['rating-blue','rating-gold'],['rating-white'],[], {'opaque':{'name':'rating-blue'}}, [{'name':'rating-gold'}]])
def test_shared_authoritative_color_policy(tags):
    doc = intake.parse_document('d.json',json.dumps({'content':'body','tags':tags}).encode())
    c, exportable, _ = _processing_gate({'readwise_tags':tags})
    assert doc['rating']==c
    assert doc['admission']==('selected' if exportable else 'held')


def test_near_candidates_persist_and_never_merge(tmp_path):
    db = sqlite3.connect(tmp_path/'near.sqlite')
    base = 'The library preserves every original source. A checkpoint lets long batch jobs resume safely. ' * 10
    n=NearIndex(db)
    assert n.add('one',base)==[]
    db.commit();db.close()
    db=sqlite3.connect(tmp_path/'near.sqlite');n=NearIndex(db)
    matches=n.add('two',base+'!' )
    assert matches[0]['body_sha256']=='one'
    assert n.add('short','tiny')==[]
    assert n.add('unrelated','가나다라마바사 아자차카타파하 '*20)==[]
    assert db.execute('select count(*) from near_v1').fetchone()[0]==3
    db.close()


def test_archive_failure_rolls_back_and_can_rerun(tmp_path):
    source = tmp_path/'a.zip'
    with zipfile.ZipFile(source,'w') as z:
        z.writestr('one.json',json.dumps({'content':'one','id':'one'}))
        z.writestr('two.json',json.dumps({'content':'two','id':'two'}))
    engine=intake.Intake(tmp_path/'out'); real=intake.parse_document
    def fail(name,payload):
        if name=='two.json':raise RuntimeError('INTERRUPTED')
        return real(name,payload)
    with patch.object(intake,'parse_document',side_effect=fail),pytest.raises(RuntimeError):engine.run(source)
    assert engine.db.execute('select count(*) from docs').fetchone()[0]==0
    assert engine.run(source)['stats']['canonical_new']==2
    engine.close()


def test_drive_list_pagination():
    d=object.__new__(intake.Drive)
    with patch.object(d,'api',side_effect=[{'files':[{'id':'a'}],'nextPageToken':'next'},{'files':[{'id':'b'}]}]) as api:
        assert len(d.list('q','id'))==2
        assert api.call_args_list[1].args[1]['pageToken']=='next'


class FakeSource:
    def page(self,folder,token):
        if folder=='root' and token is None:
            return {'files':[{'id':'sub','mimeType':'application/vnd.google-apps.folder'}, {'id':'a','version':'1','mimeType':'text/plain'}],'nextPageToken':'two'}
        if folder=='root':
            return {'files':[{'id':'b','version':'1','mimeType':'text/plain'}]}
        return {'files':[{'id':'a','version':'1','mimeType':'text/plain'}]}
    def download(self,metadata,destination):destination.write_text('raw '+metadata['id'])


def test_bulk_pagination_resume_versions_and_idempotency(tmp_path):
    b=Bulk(tmp_path,['root']);seen=[]
    def process(key,meta,raw):seen.append(key);assert raw.read_text().startswith('raw ')
    first=b.run(FakeSource(),process,max_items=1)
    assert first['discovery_complete'] is False
    b.close();b=Bulk(tmp_path,['root'])
    assert b.run(FakeSource(),process)['completed']==1
    assert b.run(FakeSource(),process)['completed']==0
    assert len(set(seen))==2
    assert len(list(tmp_path.glob('*.source')))==2
    b.close()


def test_bulk_retry_and_failed_processing_retains_source(tmp_path):
    b=Bulk(tmp_path,['root'],sleep=lambda _:None);source=FakeSource()
    real=source.download
    with patch.object(source,'download',side_effect=[TimeoutError(),None]):
        # The retry policy retries transient failures exactly up to its bound.
        n=[]
        b._retry(lambda: n.append(1) or (source.download({},tmp_path/'x')))
        assert len(n)==2
    def fail(*args):raise ValueError('PRIVATE BODY MUST NOT APPEAR')
    assert b.run(source,fail)['held']==2
    assert len(list(tmp_path.glob('*.source')))==2
    assert b.db.execute("select reason from jobs limit 1").fetchone()[0]=='PROCESSING_FAILED'
    b.retry_held();assert b.run(source,lambda *args:None)['completed']==2
    b.close()


def test_bulk_crash_replays_running_with_stable_key(tmp_path):
    b=Bulk(tmp_path,['root']);source=FakeSource()
    def crash(*args):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):b.run(source,crash)
    b.close();b=Bulk(tmp_path,['root'])
    assert b.run(source,lambda *args:None)['completed']==2
    b.close()


@pytest.mark.parametrize('workers',[0,2,99])
def test_bulk_concurrency_bound(tmp_path,workers):
    with pytest.raises(ValueError):Bulk(tmp_path,['root'],workers=workers)


def test_metrics_hold_counts_as_miss_and_invalid_is_held():
    m=metrics([('blue','{"color":"blue"}'),('blue','{"color":"held"}'),('red','{"color":"blue"}'),('gold','invalid')])
    assert m['accuracy']==.25 and m['held']==2
    assert m['per_color']['blue']=={'tp':1,'fp':1,'fn':1,'support':2,'precision':.5,'recall':.5}
    assert m['per_color']['gold']['fn']==1
    assert prediction('{"color":"invented"}')=='held'


def test_gold_missing_or_synthetic_not_accepted(tmp_path):
    for value in [{},{'schema':'synthetic','cases':[{}]*50},{'schema':'reader-human-gold-v1','cases':[{}]*50}]:
        with pytest.raises(ValueError):validate_gold(value)
    gold=tmp_path/'gold.json';gold.write_text('{}')
    with pytest.raises(ValueError):prepare(gold,tmp_path/'no-rubric',tmp_path/'config.json')
    assert not (tmp_path/'config.json').exists()


def test_gold_duplicate_body_and_unconfirmed_review_are_rejected():
    # Test data deliberately cannot pass as independent human gold.
    cases=[{'id':str(i),'text':'synthetic','text_sha256':hashlib.sha256(b'synthetic').hexdigest(),'expected':RATING_COLORS[i%9],'confirmation':{'method':'model-generated'}} for i in range(50)]
    with pytest.raises(ValueError):validate_gold({'schema':'reader-human-gold-v1','cases':cases})


def test_local_missing_gearshift_and_oversized_prompt_fail_before_calls(tmp_path):
    from scripts.reader_local import GearShift
    with pytest.raises(Held,match='GEARSHIFT_NOT_CONNECTED'):GearShift(tmp_path)
    with pytest.raises(Held,match='PROMPT_EXCEEDS_CONTEXT'):infer('ollama','x','x'*5000,amando_root=tmp_path,work=tmp_path)


def test_local_denial_prevents_inference(tmp_path):
    from scripts import reader_local as local
    with patch.object(local,'GearShift') as gear,patch.object(local,'budget_for',return_value={}),patch.object(local,'ollama') as http:
        gear.return_value.check.side_effect=Held('GEARSHIFT_HELD')
        with pytest.raises(Held):local.infer('ollama','x','short',amando_root=tmp_path,work=tmp_path)
        http.assert_not_called()


def test_drive_adapter_read_only_query_and_reject_shared(tmp_path):
    from unittest.mock import Mock
    d=Mock();d.api.return_value={'version':'1','shared':True}
    s=DriveSource(d)
    with pytest.raises(ValueError,match='SOURCE_SHARED_OR_CHANGED'):s.download({'id':'x','version':'1'},tmp_path/'x')
    assert d.api.call_args.args[0]=='files/x'
    with pytest.raises(ValueError):s.page("unsafe'",None)


def test_intake_near_candidates_keep_both_originals(tmp_path):
    body='The original archive records durable checkpoints and conservative document decisions. '*10
    zpath=tmp_path/'a.zip'
    with zipfile.ZipFile(zpath,'w') as z:
        for i,text in enumerate([body,body+'!',body]):
            z.writestr(str(i)+'.json',json.dumps({'id':str(i),'content':text,'tags':['rating-blue']}))
    engine=intake.Intake(tmp_path/'out');result=engine.run(zpath)
    assert result['stats']['canonical_new']==2
    assert result['stats']['duplicate_body']==1
    with gzip.open(engine.work/result['shards'][0],'rt') as f:rows=[json.loads(x) for x in f]
    assert rows[1]['metadata']['near_duplicate_candidates']
    assert rows[0]['content']==body and rows[1]['content']==body+'!'
    engine.close()


def test_bulk_new_scan_detects_new_version(tmp_path):
    b=Bulk(tmp_path,['root']);s=FakeSource()
    b.run(s,lambda *a:None)
    with patch.object(s,'page',return_value={'files':[{'id':'a','version':'2','mimeType':'text/plain'}]}):
        b.start_scan();assert b.run(s,lambda *a:None)['completed']==1
    assert b.db.execute('select count(*) from jobs').fetchone()[0]==3
    b.close()


def test_promptfoo_export_report(tmp_path):
    p=tmp_path/'synthetic-results.json'
    p.write_text(json.dumps({'results':{'results':[
        {'provider':{'id':'local','label':'ollama'},'vars':{'expected':'blue'},'response':{'output':'{"color":"blue"}'}},
        {'provider':{'id':'local','label':'mlx'},'vars':{'expected':'blue'},'response':{'output':'{"color":"held"}'}}
    ]}}))
    r=report(p)
    assert r['ollama']['accuracy']==1 and r['mlx']['held']==1


def test_live_gearshift_denial_closes_stream_cooperatively(tmp_path):
    import io
    from scripts import reader_local as local
    from itertools import count
    clock=count()
    stream=io.BytesIO(b'{"response":"piece"}\n{"done":true,"eval_count":1}\n')
    with patch.object(local,'GearShift') as gear,patch.object(local,'budget_for',return_value={}),patch.object(local,'ollama',return_value=stream),patch.object(local.time,'monotonic',side_effect=clock.__next__):
        gear.return_value.check.side_effect=[{},Held('GEARSHIFT_HELD')]
        with pytest.raises(Held):local.infer('ollama','owned','short',amando_root=tmp_path,work=tmp_path)
        assert stream.closed


def test_gold_config_does_not_expand_tag_arrays_or_leak_labels(tmp_path):
    # Mocking provenance validation tests config plumbing only, never gold truth.
    c={'id':'fixture','text':'synthetic body','expected':'gold','reader_tags':['rating-blue','rating-red']}
    rubric=tmp_path/'rubric.txt';rubric.write_text('SYNTHETIC rubric for plumbing test only')
    gold=tmp_path/'synthetic.json';gold.write_text('{}')
    out=tmp_path/'config.json'
    with patch('scripts.reader_eval.validate_gold',return_value=[c]):prepare(gold,rubric,out)
    cfg=json.loads(out.read_text())
    assert isinstance(cfg['tests'][0]['vars']['tags'],str)
    assert '{{expected}}' not in cfg['prompts'][0] and '{{tags}}' not in cfg['prompts'][0]
    assert cfg['evaluateOptions']['maxConcurrency']==1 and cfg['sharing'] is False


def test_manual_workflow_has_no_secrets_models_or_public_outputs():
    import yaml
    root=Path(__file__).resolve().parents[1]
    path=root/'.github/workflows/reader-quality-check.yml'
    config=yaml.safe_load(path.read_text())
    assert set(config['on'])=={'workflow_dispatch'}
    assert config['permissions']=={'contents':'read'}
    text=path.read_text()
    assert 'secrets.' not in text and 'upload-artifact@' not in text
    assert '--backend mlx' not in text and '--backend ollama' not in text
    assert config['jobs']['validate']['runs-on']=='ubuntu-24.04'
