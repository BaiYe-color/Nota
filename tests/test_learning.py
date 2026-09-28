import copy
from pathlib import Path
import pytest
from contracts import Options
from learning import validate_plan,validate_document,document_markdown,generate_slides
from rendering import note_markdown,note_html
from storage import Store
from engine import Context
from extraction import extract,validate_slide


def sources():
    return [{'id':'s1','document_id':'d','document_name':'slides.pdf','page':1,'text':'A definition','kind':'text','asset':None,
             'evidence_asset':'page.png','method':'slide-layout','material_type':'lecture_slides'},
            {'id':'s2','document_id':'d','document_name':'slides.pdf','page':2,'text':'A worked example','kind':'text','asset':None,
             'method':'slide-layout','material_type':'lecture_slides'}]


def test_plan_rejects_lost_reordered_and_duplicate_pages():
    good={'units':[{'title':'One','objective':'Learn','source_ids':['s1','s2']}],'excluded':[]}
    assert validate_plan(good,sources())==good
    for refs in [['s1'],['s2','s1'],['s1','s1','s2']]:
        bad=copy.deepcopy(good);bad['units'][0]['source_ids']=refs
        with pytest.raises(ValueError):validate_plan(bad,sources())


def test_document_rejects_fake_math_unknown_evidence_and_page_images():
    base={'kind':'formula','basis':'source','source_ids':['s1'],'latex':'formula-ffn'}
    with pytest.raises(ValueError):validate_document({'blocks':[base]},sources(),[])
    base.update(latex='x=1');assert validate_document({'blocks':[base]},sources(),[])['blocks']
    base['source_ids']=['invented']
    with pytest.raises(ValueError):validate_document({'blocks':[base]},sources(),[])
    with pytest.raises(ValueError):validate_document({'blocks':[{'kind':'figure','basis':'source','source_ids':['s1'],
        'source_id':'s1','caption':'Whole slide','text':'Look'}]},sources(),[])


def test_document_normalizes_structured_review_questions():
    value={'blocks':[{'kind':'paragraph','basis':'source','source_ids':['s1'],'text':'Definition'}],
           'questions':[{'question':'什么是定义？','answer':'忽略答案字段'},'还需要哪些条件？',{'unknown':'drop'}]}
    assert validate_document(value,sources(),[])['questions']==['什么是定义？','还需要哪些条件？']

def test_fallback_document_keeps_source_traceability():
    from learning import fallback_document
    document=fallback_document(sources())
    assert document['questions']==[] and document['blocks'][0]['source_ids']==['s1']


def test_document_drops_redundant_inline_asset_tokens():
    document=validate_document({'blocks':[{'kind':'paragraph','basis':'source','source_ids':['s1'],
        'text':'倒排记录按 docID 排序。 ![不要发布](slide.png) [[VISUAL:vi-old]]'}]},sources(),[])
    assert document['blocks'][0]['text']=='倒排记录按 docID 排序。'


def test_document_binds_model_id_mistakes_to_current_unit_evidence():
    page=sources()[0]
    crop={**page,'id':'crop','asset':'crop.png','method':'layout-region'}
    document=validate_document({'blocks':[
        {'kind':'paragraph','basis':'source','source_ids':['s1'],'text':'定义来自本页。'},
        {'kind':'figure','basis':'source','source_ids':['made-up-id'],'source_id':'crop',
         'caption':'结构图','text':'观察箭头的方向。'},
    ]},[page,crop],[crop])
    assert document['blocks'][0]['source_ids']==['s1']
    assert set(document['blocks'][1]['source_ids'])=={'s1','crop'}


def test_structured_document_is_authoritative_and_warnings_survive(tmp_path):
    document=validate_document({'blocks':[{'kind':'warning','basis':'source','source_ids':['s1'],
                                         'text':'原课件的行列名称冲突。待核对项：请对照矩阵。'},
        {'kind':'table','basis':'derived','source_ids':['s1'],'columns':['term','docID'],'rows':[['home','1, 2']]}]},sources(),[])
    ch={'title':'Unit','status':'ready','source_ids':['s1'],'markdown':'STALE','document':document}
    data={'title':'Lesson','chapters':[ch],'sources':sources(),'options':{'include_images':True},
          'knowledge':{'strategy':'learning_units','visuals':[]}}
    md=note_markdown(data);page=note_html(data,Store(tmp_path))
    assert 'STALE' not in md+page and '来源辨析' in md and '来源辨析' in page
    assert '待核对项' in md and '待核对项' in page
    assert 'page.png' not in md+page and '<table>' in page and '推导说明' in md


def test_scanned_page_uses_layout_crops_regardless_of_writing_type(tmp_path,monkeypatch):
    import pymupdf
    p=tmp_path/'slide.pdf';doc=pymupdf.open();doc.new_page(width=600,height=400);doc.save(p);doc.close()
    store=Store(tmp_path/'data');ctx=Context(store,store.new_job('generate',{}))
    def fake(*args,**kwargs):
        return kwargs['validate']({'text':'Definition and process','regions':[
            {'role':'text','bbox':[.1,.1,.9,.3],'text':'Definition'},
            {'role':'diagram','bbox':[.1,.4,.7,.8],'text':'A -> B'}]})
    monkeypatch.setattr(ctx,'llm',fake)
    # The writing style may be personal notes, but scan layout still needs
    # selective crops instead of an exported full-page screenshot.
    blocks,_=extract(ctx,[{'id':'a'*64,'sha':'scan','name':'slides.pdf','path':str(p),'material_type':'personal_notes'}],Options())
    page=next(b for b in blocks if b['kind']=='text');fig=next(b for b in blocks if b['kind']=='visual')
    assert page['asset'] is None and page['evidence_asset'] and len(page['page_layout'])==2
    assert fig['method']=='layout-region' and fig['asset']!=page['evidence_asset'] and fig['bbox']==[.1,.4,.7,.8]


def test_handwritten_page_stays_source_evidence_not_a_full_page_figure(tmp_path,monkeypatch):
    import pymupdf
    p=tmp_path/'handwritten.pdf';doc=pymupdf.open();doc.new_page(width=600,height=400);doc.save(p);doc.close()
    store=Store(tmp_path/'data');ctx=Context(store,store.new_job('generate',{}))
    monkeypatch.setattr(ctx,'llm',lambda role,system,data,image,validate: validate({'text':'手写定义','uncertain':False}))
    blocks,_=extract(ctx,[{'id':'b'*64,'sha':'hand','name':'handwritten.pdf','path':str(p),'material_type':'personal_notes'}],Options(handwritten=True))
    page=blocks[0]
    assert page['asset'] is None and page['evidence_asset'] and page['method']=='vision'


def test_slide_validator_accepts_provider_layout_aliases():
    result=validate_slide({'blocks':[
        {'type':'heading','coordinates':[100,50,900,180],'content':'布尔检索'},
        {'kind':'flowchart','position':{'x':100,'y':250,'width':600,'height':400},
         'description':'文档 → 分词 → 倒排表'},
    ]})
    assert result['regions'][0]=={'role':'title','bbox':[.1,.05,.9,.18],'text':'布尔检索'}
    assert result['regions'][1]['role']=='diagram'
    assert result['regions'][1]['bbox']==[.1,.25,.7,.65]


def test_learning_pipeline_preserves_steps_without_screenshots():
    class Ctx:
        def progress(self,*args):pass
        def llm(self,role,system,data,validate):
            if role=='outline' and isinstance(data,list):value={'units':[{'title':'Process','objective':'Build an index','source_ids':['s1','s2']}],'excluded':[]}
            elif role=='outline':value={'issues':[]}
            else:value={'blocks':[{'kind':'steps','basis':'source','source_ids':['s1','s2'],'items':['Tokenize','Sort','Merge']}],'questions':[]}
            return validate(validate(value))
    cards,knowledge,chapters=generate_slides(Ctx(),sources(),Options())
    assert knowledge['strategy']=='learning_units' and len(chapters)==1
    assert '1. Tokenize' in chapters[0]['markdown'] and '3. Merge' in chapters[0]['markdown']
    assert chapters[0]['document']['blocks'][0]['source_ids']==['s1','s2']


def test_learning_pipeline_falls_back_when_outline_excludes_every_page():
    class Ctx:
        def progress(self,*args):pass
        def warn(self,message):self.warning=message
        def llm(self,role,system,data,validate):
            if role=='outline' and isinstance(data,list):
                return validate({'units':[],'excluded':[{'source_id':'s1','reason':'mistakenly excluded'},
                                                        {'source_id':'s2','reason':'mistakenly excluded'}]})
            elif role=='outline':return validate({'issues':[]})
            return validate({'blocks':[{'kind':'paragraph','basis':'source','source_ids':['s1','s2'],'text':'已恢复内容。'}],
                             'questions':[]})
    ctx=Ctx();cards,knowledge,chapters=generate_slides(ctx,sources(),Options(handwritten=True))
    assert len(cards)==len(chapters)==1 and chapters[0]['source_ids']==['s1','s2']
    assert '自动分组' in ctx.warning and knowledge['units'][0]['title']=='学习单元 1'


def test_table_rejects_ragged_rows():
    with pytest.raises(ValueError):validate_document({'blocks':[{'kind':'table','basis':'source',
        'source_ids':['s1'],'columns':['term','docID'],'rows':[['home']]}]},sources(),[])


def test_preview_inserts_only_selected_crop():
    from rendering import chapter_preview
    source={**sources()[0],'id':'crop','asset':'crop.png','method':'slide-region'}
    document=validate_document({'blocks':[{'kind':'figure','basis':'source','source_ids':['crop'],
        'source_id':'crop','caption':'The process','text':'Follow the arrows.'}]},[source],[source])
    ch={'document':document,'markdown':'STALE'}
    html=chapter_preview(ch,[source]);assert html.count('<img')==1 and '[[VISUAL:' not in html
    assert '<img' not in chapter_preview(ch,[source],False)


def test_preview_never_leaks_internal_visual_tokens_when_asset_is_missing():
    from rendering import chapter_preview,safe_html
    document={'schema_version':1,'blocks':[{'kind':'figure','basis':'source','source_ids':['s1'],
        'source_id':'missing','caption':'流程图','text':'观察箭头。'}],'questions':[]}
    rendered=chapter_preview({'document':document},sources())
    assert '[[VISUAL:' not in rendered and '配图未能加载：流程图' in rendered
    assert '[[VISUAL:' not in safe_html('正文 [[VISUAL:vi-old]]')


def test_legacy_preview_places_only_the_selected_visual_at_its_marker():
    from rendering import chapter_preview
    crop={**sources()[0],'id':'crop','asset':'crop.png','method':'vision-region'}
    visual={'id':'vi-crop','source_id':'crop','title':'流程图'}
    chapter={'markdown':'说明。\n\n[[VISUAL:vi-crop]]\n\n结论。','visual_ids':['vi-crop']}
    html=chapter_preview(chapter,[crop],True,[visual])
    assert html.count('<img')==1 and html.index('说明') < html.index('<figure') < html.index('结论')


def test_document_titles_distinguish_sections_from_supporting_labels():
    document=validate_document({'blocks':[
        {'kind':'paragraph','basis':'source','source_ids':['s1'],'title':'核心机制','title_role':'section','text':'定义。'},
        {'kind':'steps','basis':'source','source_ids':['s1'],'title':'操作步骤','title_role':'label','items':['第一步']},
    ]},sources(),[])
    markdown=document_markdown(document)
    assert '### 核心机制' in markdown and '**操作步骤**' in markdown
    legacy=document_markdown({'blocks':[{'kind':'table','basis':'source','title':'旧表格','columns':['A'],'rows':[['1']]}]})
    assert '**旧表格**' in legacy


def test_manual_edit_exits_structured_projection(tmp_path):
    from service import Service
    store=Store(tmp_path);jid=store.new_job('generate',{})
    doc={'schema_version':1,'blocks':[{'kind':'paragraph','basis':'source','source_ids':['s1'],'text':'Original'}],'questions':[]}
    data={'title':'Lesson','chapters':[{'id':'ch1','title':'Unit','card_ids':['u1'],'status':'ready','issues':[],
          'source_ids':['s1'],'document':doc,'markdown':'Original'}],'sources':sources(),
          'cards':[{'id':'u1','importance':3,'source_ids':['s1']}],'options':{'include_images':True},
          'knowledge':{'strategy':'learning_units','visuals':[]},'warnings':[]}
    nid=store.create_note(jid,data);service=Service(store,workers=1)
    try:
        service.edit(nid,'ch1',1,'Manually corrected')
        updated=store.note(nid)['data']
        assert 'document' not in updated['chapters'][0]
        assert 'Manually corrected' in note_markdown(updated) and 'Original' not in note_markdown(updated)
    finally:service.pool.shutdown(wait=True)


def test_unavailable_audit_preserves_draft_and_marks_it_unverified():
    from learning import write_unit
    class Ctx:
        def warn(self,message):self.warning=message
        def llm(self,role,system,data,validate):
            if role=='outline':raise RuntimeError('HTTP 402')
            return validate({'blocks':[{'kind':'paragraph','basis':'source','source_ids':['s1'],'text':'Definition'}]})
    ctx=Ctx();ch=write_unit(ctx,{'id':'ch','title':'Unit','objective':'Understand','source_ids':['s1'],'card_ids':['u']},sources(),Options())
    assert ch['document']['blocks'][0]['text']=='Definition'
    assert ch['audit_status']=='unavailable' and '402' in ch['issues'][0]['message']


def test_cancel_during_audit_is_not_downgraded_to_warning():
    from learning import write_unit
    from engine import Cancelled
    class Ctx:
        def llm(self,role,system,data,validate):
            if role=='outline':raise Cancelled('cancelled')
            return validate({'blocks':[{'kind':'paragraph','basis':'source','source_ids':['s1'],'text':'Definition'}]})
    with pytest.raises(Cancelled):write_unit(Ctx(),{'title':'Unit','objective':'Understand','source_ids':['s1']},sources(),Options())
