"""Slide-specific learning units and validated publication blocks.
Markdown is a compatibility projection, never the source of truth for generated units.
"""
import re
from engine import digest,Cancelled

PLAN = """把课堂材料或手写学习笔记按原教学顺序组织为连续学习单元，例子及后续步骤不能拆散。每单元围绕一个学习目标，通常2至8页。封面、目录、重复章节页可排除但必须说明原因。只要存在可读的教学内容，至少建立一个学习单元；绝不能把所有页面都排除。不要把所有页面切成单页单元，不要重新按关键词打散课堂顺序。返回 {units:[{title,objective,source_ids:[输入ID]}],excluded:[{source_id,reason}]}。每个输入ID恰好分配或排除一次。"""
WRITE = """根据学习单元和原始来源生成可独立学习的结构化笔记，不输出整篇Markdown。恢复定义→机制→贯穿例子→步骤→结果的关系，按材料实际需要选择，不套固定栏目。连续演示必须使用同一组数据；保留全部关键条件。区分原文与推导：推导标derived，来源矛盾用warning指出双方及依据，不能盲目照抄或静默修正。课堂提问可保留；给解答时要给必要过程并标derived。不为了凑完整解释而编造前提或过度保证。避免重复标题与结论。
返回 {blocks:[{kind:paragraph|steps|table|formula|figure|code|warning,title:可选标题,title_role:section|label|none,source_ids:[输入ID],basis:source|derived,text:文字}],questions:[复习问题]}。
每个单元最多 3 个 title_role=section 的正式小节标题；只有概念、机制或应用主题才用 section。例子、步骤、表格、公式、图片、推导说明、易错点、来源辨析应使用 label 或 none，绝不能伪装为同级小节。
paragraph/warning使用text。steps使用items:[按顺序的步骤]。table使用columns:[列名],rows:[[单元格]]，保留行列语义。formula使用latex:实际表达式，禁止formula-name或ID代替公式。code使用text:可读伪代码。figure使用source_id:候选图片ID,caption:完整简短中文标题,text:说明观察什么及支持什么结论。只能引用candidates中图片，不能插整页课件、装饰或纯文字截图，没有必要就不插图。普通 text、items、rows、title、caption 中禁止出现 `![`、`<img`、HTML 标签、`[[VISUAL:` 或 `[[FORMULA:`；这些不是 Markdown 输出格式。需要配图时新建一个 figure 块，需要公式时新建一个 formula 块。不输出任何图片/HTML/资源链接或内部ID在正文。每块必须有来源。每个关键定义和演示过程至少覆盖一次。"""
AUDIT = """核对学习单元是否可用于学习。检查行列/集合语义、否定、数值、条件、例题每一步和结果、是否把上界估计说成保证、是否无依据补全、是否遗漏关键定义或步骤。不把来源中的明显矛盾当真理。只报告实质问题，不做文风建议。返回 {issues:[{message:具体错误及修正依据,source_ids:[来源ID]}]}。"""


def validate_plan(value,sources):
    allowed={s['id'] for s in sources};assigned=[]
    order={s['id']:i for i,s in enumerate(sources)};last=-1
    for unit in value['units']:
        refs=unit['source_ids']
        if not unit.get('title') or not unit.get('objective') or not refs or not set(refs)<=allowed:raise ValueError('学习单元来源无效')
        positions=[order[x] for x in refs]
        if positions!=sorted(positions) or min(positions)<=last:raise ValueError('学习单元必须保持教学顺序')
        last=max(positions);assigned.extend(refs)
    for item in value.get('excluded',[]):
        if item.get('source_id') not in allowed or not item.get('reason'):raise ValueError('排除页需要来源与理由')
        assigned.append(item['source_id'])
    if len(assigned)!=len(set(assigned)) or set(assigned)!=allowed:raise ValueError('页面必须恰好分配或有理由排除一次')
    if not value['units']:raise ValueError('学习单元为空')
    return value


def validate_document(value,sources,candidates,include_review_questions=True):
    allowed={s['id'] for s in sources};images={s['id'] for s in candidates};seen=set()
    # A visual crop is derived from its page, so an otherwise valid figure may
    # cite its crop alone.  The application completes that citation with the
    # page evidence; ordinary prose must still name an ID from this unit.
    page_sources=[s['id'] for s in sources if not s.get('asset')]
    page_for_visual={}
    for visual in candidates:
        match=next((page['id'] for page in sources if not page.get('asset') and
                    page.get('document_id')==visual.get('document_id') and page.get('page')==visual.get('page')),None)
        if match:page_for_visual[visual['id']]=match
    def clean_inline_artifacts(item):
        """Remove only display tokens that carry no source content.

        Some OpenAI-compatible Claude gateways occasionally add a Markdown image
        token after otherwise valid structured prose.  The token is redundant:
        real figures and formulas have their own typed block.  Keeping it would
        make a valid note fail its whole job and can leak an internal ID.
        """
        if isinstance(item,str):
            item=re.sub(r'!\[[^\]]*\]\([^)]*\)','',item)
            return re.sub(r'\[\[(?:VISUAL|FORMULA):[^\]]*\]\]','',item,flags=re.I).strip()
        if isinstance(item,list): return [clean_inline_artifacts(x) for x in item]
        return item
    blocks=[]
    for raw in value.get('blocks',[]):
        block=dict(raw) if isinstance(raw,dict) else raw
        if isinstance(block,dict):
            for field in ('text','caption','title','items','rows'):
                if field in block:block[field]=clean_inline_artifacts(block[field])
            raw_refs=block.get('source_ids',[])
            raw_refs=raw_refs if isinstance(raw_refs,list) else []
            invalid_refs=[ref for ref in raw_refs if ref not in allowed]
            refs=[ref for ref in raw_refs if ref in allowed]
            if block.get('kind')=='figure' and block.get('source_id') in images:
                sid=block['source_id']
                refs=list(dict.fromkeys(ref for ref in [*refs,sid,page_for_visual.get(sid)] if ref))
            elif invalid_refs:
                raise ValueError('内容块引用了本单元外的来源 '+str(invalid_refs[:3])+
                                 '；请逐字使用输入 sources 中的 id，且不要使用其他单元或候选图片的 ID。允许的页面来源 ID：'+
                                 ', '.join(page_sources[:12]))
            block['source_ids']=refs
        blocks.append(block)
    if not isinstance(blocks,list) or not blocks or len(blocks)>45:raise ValueError('笔记块数量无效')
    for block in blocks:
        if not isinstance(block,dict):raise ValueError('内容块必须是对象')
        kind=block.get('kind');refs=block.get('source_ids',[])
        for field in ('title','text','caption'):
            if field in block and not isinstance(block[field],str):raise ValueError('内容字段必须是文字')
        for field in ('text','caption','title','items','rows'):
            matched=re.search(r'!\[|</?[A-Za-z][A-Za-z0-9]*(?:\s[^>]*|/?)>|\[\[(?:VISUAL|FORMULA):',str(block.get(field,'')))
            if matched:
                raise ValueError(f'内容块的 {field} 含有禁止的图片/公式标记“{matched.group()[:30]}”。删除该标记；图片改为 kind=figure，公式改为 kind=formula。')
        if kind not in {'paragraph','steps','table','formula','figure','code','warning'}:raise ValueError('未知内容块类型')
        title_role=block.get('title_role')
        if title_role is not None and title_role not in {'section','label','none'}:raise ValueError('标题类型无效')
        if not block.get('title'):block['title_role']='none'
        elif title_role is None:
            # Existing notes predate title roles. Preserve substantive prose
            # headings, while rendering supporting blocks as quiet labels.
            block['title_role']='section' if kind=='paragraph' else 'label'
        if not refs or not set(refs)<=allowed:raise ValueError('内容块必须引用本单元来源')
        if block.get('basis') not in {'source','derived'}:raise ValueError('必须区分原文与推导')
        if kind=='steps' and (not isinstance(block.get('items'),list) or not block.get('items') or not all(isinstance(x,str) for x in block['items'])):raise ValueError('步骤为空')
        if kind=='table':
            cols=block.get('columns',[]);rows=block.get('rows',[])
            if not isinstance(cols,list) or not isinstance(rows,list) or not cols or not rows or any(not isinstance(row,list) or len(row)!=len(cols) for row in rows):raise ValueError('表格行列不一致')
        if kind=='formula':
            latex=block.get('latex','').strip()
            if not latex or re.search(r'formula[-_]|\[\[|^fm-',latex,re.I):raise ValueError('公式必须是实际数学表达式')
            from rendering import guard_math
            guard_math('$$'+latex+'$$')
        if kind=='figure':
            sid=block.get('source_id')
            if sid not in images or sid not in refs or sid in seen:raise ValueError('图片必须是本单元独立候选且只出现一次')
            if not block.get('caption') or not block.get('text'):raise ValueError('配图必须有标题和读图说明')
            seen.add(sid)
        if kind in {'paragraph','warning','code'} and not block.get('text'):raise ValueError('文字内容为空')
    raw_questions=value.get('questions',[]) if include_review_questions else []
    # Some compatible models return {question: ..., answer: ...}; retain only
    # the question text so this optional field cannot discard a whole unit.
    questions=[]
    if isinstance(raw_questions,list):
        for question in raw_questions:
            if isinstance(question,str): text=question
            elif isinstance(question,dict):
                text=next((question.get(key) for key in ('question','prompt','text','title')
                           if isinstance(question.get(key),str)), '')
            else: text=''
            if text and text.strip():questions.append(text.strip())
    return {'schema_version':1,'blocks':blocks,'questions':questions[:6]}


def document_markdown(document):
    parts=[]
    for b in document['blocks']:
        if b.get('title'):
            # Saved documents from before title_role keep a sensible hierarchy.
            title_role=b.get('title_role') or ('section' if b.get('kind')=='paragraph' else 'label')
            if title_role=='section':parts.append('### '+b['title'])
            elif title_role=='label':parts.append('**'+b['title']+'**')
        if b['basis']=='derived':parts.append('**推导说明**')
        kind=b['kind']
        if kind=='table':
            cell=lambda x:str(x).replace('|',r'\|').replace('\n',' ')
            rows=[b['columns'],['---']*len(b['columns']),*b['rows']]
            parts.append('\n'.join('| '+' | '.join(cell(x) for x in row)+' |' for row in rows))
        elif kind=='steps':parts.append('\n'.join(f'{i}. {text}' for i,text in enumerate(b['items'],1)))
        elif kind=='formula':parts.append('$$\n'+b['latex']+'\n$$')
        elif kind=='figure':parts.extend([b['text'],'[[VISUAL:vi-'+digest(b['source_id'])[:12]+']]'])
        elif kind=='code':parts.append('```text\n'+b['text'].replace('```','')+'\n```')
        elif kind=='warning':parts.append('> 来源辨析：'+b['text'].replace('\n','\n> '))
        else:parts.append(b['text'])
    return '\n\n'.join(parts)

def fallback_document(pages):
    """Last-resort, source-grounded prose for a unit whose rich schema failed."""
    blocks=[]
    for page in pages:
        text=re.sub(r'!\[[^\]]*\]\([^)]*\)|\[\[(?:VISUAL|FORMULA):[^\]]*\]\]|<[^>]+>','',str(page.get('text',''))).strip()
        if text:
            blocks.append({'kind':'paragraph','basis':'source','source_ids':[page['id']],
                           'title_role':'none','text':text[:3500]})
    if not blocks:
        blocks=[{'kind':'paragraph','basis':'source','source_ids':[p['id'] for p in pages],
                 'title_role':'none','text':'来源未提取到可整理文字，请查看原始页面。'}]
    return {'schema_version':1,'blocks':blocks,'questions':[]}


def write_unit(ctx,unit,sources,options,instruction=None,previous=None):
    pages=[s for s in sources if s['id'] in unit['source_ids'] and not s.get('asset')]
    page_keys={(s['document_id'],s.get('page')) for s in pages}
    candidates=[s for s in sources if s.get('asset') and s.get('method') in {'layout-region','slide-region','vision-region'} and (s['document_id'],s.get('page')) in page_keys]
    evidence=pages+candidates
    payload={'unit':{k:unit[k] for k in ('title','objective') if k in unit},'sources':[{'id':s['id'],'page':s.get('page'),'text':s['text'],'regions':s.get('page_layout',[])} for s in evidence],
             'candidates':[{'id':s['id'],'description':s['text']} for s in candidates],
             'detail':options.detail,'instructions':options.instructions,'style':options.style}
    if instruction:payload.update(revision_request=instruction,previous=previous)
    validate=lambda value:validate_document(value,evidence,candidates,options.include_review_questions)
    writer_prompt=WRITE if options.include_review_questions else WRITE+'\n本次用户选择跳过复习问题：questions 必须返回空数组 []，只生成学习笔记。'
    fallback_issue=None
    try:
        document=ctx.llm('writer',writer_prompt,payload,validate=validate)
    except Cancelled: raise
    except (RuntimeError,ValueError,KeyError,TypeError) as exc:
        document=fallback_document(pages)
        fallback_issue={'message':'已跳过配图、公式、表格和复习题等增强项，按解析来源生成基础正文：'+str(exc)[:160],
                        'source_ids':[s['id'] for s in pages]}
    def check(value):
        issues=value['issues'] if isinstance(value,dict) else value;allowed={s['id'] for s in evidence}
        if not isinstance(issues,list) or any(not x.get('message') or not set(x.get('source_ids',[]))<=allowed for x in issues):raise ValueError('审校来源无效')
        return {'issues':issues}
    audit_status='passed'
    try:
        if fallback_issue: raise RuntimeError(fallback_issue['message'])
        issues=ctx.llm('outline',AUDIT,{'document':document,'sources':payload['sources']},validate=check)['issues']
        if issues:
            document=ctx.llm('writer',writer_prompt,{**payload,'draft':document,'corrections':issues},validate=validate)
            issues=ctx.llm('outline',AUDIT,{'document':document,'sources':payload['sources']},validate=check)['issues']
        if issues:audit_status='issues'
    except Cancelled:raise
    except (RuntimeError,ValueError,KeyError,TypeError) as exc:
        audit_status='unavailable'
        issues=[fallback_issue] if fallback_issue else [{'message':'本单元审校未完成：'+str(exc)[:240],'source_ids':[s['id'] for s in pages]}]
        ctx.warn(issues[0]['message'])
    visuals=[{'id':'vi-'+digest(b['source_id'])[:12],'source_id':b['source_id'],'title':b['caption'],'description':b['text'],'claim_ids':[]} for b in document['blocks'] if b['kind']=='figure']
    return {**unit,'document':document,'markdown':document_markdown(document),'questions':document['questions'],
            'source_ids':[s['id'] for s in evidence],'visual_ids':[v['id'] for v in visuals], 'visuals':visuals,
            'issues':issues,'audit_status':audit_status,'status':'ready','edited':bool(instruction),'conversation':[]}


def generate_slides(ctx,sources,options):
    pages=[s for s in sources if not s.get('asset') or s.get('asset_role')=='source_page']
    payload=[{'id':s['id'],'page':s.get('page'),'document':s['document_name'],
              'text':s['text'][:2500]} for s in pages]
    try:
        plan=ctx.llm('outline',PLAN,payload,validate=lambda v:validate_plan(v,pages))
    except Cancelled: raise
    except (ValueError,KeyError,TypeError) as exc:
        # OCR can be good while an outline model misclassifies every handwritten
        # page as a cover or empty page.  Keep source order and continue with
        # bounded deterministic units instead of failing the whole note.
        ctx.warn('学习单元规划未通过校验，已按来源顺序自动分组：'+str(exc)[:120])
        groups=[pages[i:i+4] for i in range(0,len(pages),4)]
        plan={'units':[{'title':'学习单元 '+str(index+1),'objective':'理解本单元中的核心概念、定义与例子。',
                        'source_ids':[page['id'] for page in group]} for index,group in enumerate(groups)],'excluded':[]}
    chapters=[];cards=[];visuals=[]
    for i,unit in enumerate(plan['units']):
        ctx.progress(.64+.32*i/len(plan['units']),f'组织学习单元 {i+1}/{len(plan["units"])}')
        cid='unit-'+digest(unit)[:12]
        card={'id':cid,'title':unit['title'],'points':[unit['objective']],'source_ids':unit['source_ids'],
              'importance':3,'claim_ids':[],'formulas':[],'examples':[]}
        chapter=write_unit(ctx,{'id':'ch-'+cid,'card_ids':[cid],**unit},sources,options)
        chapters.append(chapter);cards.append(card);visuals.extend(chapter['visuals'])
    knowledge={'schema_version':2,'strategy':'learning_units','units':plan['units'],'excluded':plan.get('excluded',[]),
               'claims':[],'concepts':[],'formulas':[],'experiments':[],'visuals':visuals,'conflicts':[]}
    return cards,knowledge,chapters
