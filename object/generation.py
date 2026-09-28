"""Bounded knowledge extraction, coverage planning and source-grounded writing."""
import re
import unicodedata
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from contracts import Card,Claim,Concept,Formula,Experiment,VisualEvidence,KnowledgeIR,ConceptEdge,Chapter
from engine import Cancelled,digest

CARDS='''把材料转换为简洁的结构化知识IR与兼容知识卡片。保留限定条件、否定、数字、公式、例子和图表结论，不得编造。根据material_type理解结构：论文重视问题、方法、实验和局限，课堂PPT需恢复标题与要点关系，教材重视概念依赖与例题，会议材料必须区分决定、建议、行动项和待确认事项。supplementary只用于补充或交叉验证，不能盖过primary主线。
把可独立核验的陈述拆成claims，并标记claim_type：fact/definition/mechanism/result/limitation/decision/proposal/action；模型为了帮助理解而推导但来源未直接陈述的内容只能标interpretation或extension，confidence必须降低。conditions保存数据集、模型版本、适用范围、否定和前提。公式、实验、视觉对象必须引用它支持的claim_ids和source_ids。相同概念可合并，但条件不同的实验结果不得合并。每个来源ID必须被某张卡片引用。
保持输出简洁但必须覆盖所有重要条件和结论：每条claim只表达一个可核验事实，statement通常不超过200字；同一事实只能出现一次；卡片不得复制整段原文。论文中出现明确数据集、指标和结果时必须生成experiment。作者单位、版权、致谢和完整参考文献只建立importance=1的简短检索卡，不得复制书目全文。
返回 {"cards":[{"id":"card-temp","title":"概念标题","points":["要点"],"source_ids":["输入ID"],"importance":3,"formulas":[],"examples":[],"claim_ids":["claim-temp"]}],"claims":[{"id":"claim-temp","statement":"可独立核验的陈述","claim_type":"fact","source_ids":["输入ID"],"conditions":[],"confidence":1.0,"importance":3}],"concepts":[{"id":"concept-temp","name":"概念","definition":"定义","claim_ids":["claim-temp"],"depends_on":[]}],"formulas":[{"id":"formula-temp","latex":"公式","meaning":"含义","variables":["变量：含义"],"source_ids":["输入ID"],"claim_ids":["claim-temp"]}],"experiments":[{"id":"experiment-temp","name":"实验","dataset":"数据集","setup":["条件"],"metrics":["指标"],"results":["结果"],"source_ids":["输入ID"],"claim_ids":["claim-temp"]}],"visuals":[{"id":"visual-temp","source_id":"输入ID","title":"图表标题","visual_type":"figure","description":"图表说明","claim_ids":["claim-temp"]}]}。所有引用ID只能来自本次输入或本次返回对象。'''
CARDS += '''
公式是可选证据，不是待填空字段。cards.formulas 始终返回 []；只在顶层 formulas 中记录来源能精确确认的公式。无法逐字符确认表达式、变量、上下标或运算符时，顶层 formulas 必须为 []，正文仍照常提取。latex 只能是实际可渲染的 LaTeX，例如 "R \\Join S" 或 "\\frac{a}{b}"；严禁输出 formula、formula-1、fm-1、equation-1、公式名称、公式编号、自然语言说明或猜测的表达式。'''
# Keep the schema example syntactically meaningful; a generic value such as
# "公式" encourages weaker models to satisfy the field with a placeholder.
CARDS=CARDS.replace('"latex":"公式"','"latex":"\\\\frac{a}{b}"')
OUTLINE='''根据完整知识卡片和全局概念图，一次性组织一份有学习路径的笔记大纲。每张卡片恰好分配一次，必须覆盖全部ID，每章最多10张卡片；引用同一声明的卡片必须进入同一章，保证核心声明只有一个主章节。遵守 prerequisite 顺序，合并训练配置、复杂度、架构等重叠主题；长论文通常控制在6至9章。章节标题必须具体且互不重复，禁止使用“概览”“其他”“（1）/（2）”等机械拆分。短材料可只有一节。返回 {"chapters":[{"id":"临时ID","title":"无Markdown符号的标题","card_ids":["输入ID"]}]}。'''
PAPER_OUTLINE='''根据论文的知识卡片和概念图组织一份论文阅读笔记大纲。每张卡片恰好分配一次，必须覆盖全部ID，每章最多10张卡片；同一声明不得被分到多个章节。按证据关系而非论文页序组织，并仅在材料确有对应证据时依次使用这些主题：研究问题与贡献、方法与核心机制、模型架构/训练设定、实验设计与结果、结论与局限。不要为凑齐模板建立空章节，也不要把实验数字拆散到方法章节。实验章节必须同时容纳数据集、对比对象、指标、设置和结果；局限须保留适用条件。章节标题要具体且互不重复，禁止“概览”“其他”“第X部分”。返回 {"chapters":[{"id":"临时ID","title":"无Markdown符号的具体标题","card_ids":["输入ID"]}]}。'''
GRAPH='''根据完整概念列表建立稀疏的全局概念图。只返回理解材料确有帮助的强关系；不要仅因词语相似连边。prerequisite 的方向是“先理解的概念→依赖它的概念”，part_of 是“局部→整体”，contrasts 和 related 用于明确对比或紧密关联。每个概念最多给出2条出边，prerequisite 不得形成环。所有ID必须来自输入。返回 {"edges":[{"source_id":"概念ID","target_id":"概念ID","relation":"prerequisite|part_of|contrasts|related","reason":"简短依据"}]}。'''
WRITE='''根据本章知识卡、类型化知识IR和来源写一节真正便于学习的笔记。开头直接说本节最重要的结论，随后按概念依赖解释机制；实验要把数据集、模型、指标、设置与结果成组表述。不要沿论文页序复述，也不要每节套用“为什么重要/怎样工作/易错点”等固定栏目。公式只能在独立行写成 [[FORMULA:公式ID]]，不得自行重抄 LaTeX；相关图表在最能帮助理解的段落后独立写 [[VISUAL:视觉ID]]，并在前文说明读者应观察什么以及图支持什么结论。只能使用 knowledge_ir 中存在的 ID，每个视觉ID最多一次。
若 paper_mode 为 true，这是论文阅读笔记：每节必须服务于其章节角色；先明确作者要解决的问题与贡献，再解释方法；实验结论必须和数据集、比较对象、指标、设置成组出现，缺项就明确“来源未给出”，不得用常识补全。把作者主张、实验观察与补充解释分开；不能把论文图表仅作为装饰，也不能把实验数字写成脱离条件的绝对结论。结论与局限必须保留适用范围，不把论文的相关性观察写成因果保证。
严格遵守材料类型写作指引、输出目标、详略和用户风格，保留限定条件、否定和数字，不引入无来源事实。primary决定主线；supplementary只补充解释、例子或交叉验证，并在发生冲突时明确说明。knowledge_ir.conflicts 中的冲突不得擅自选择一方，应连同数据集、模型、指标和实验设置标成“待核对”。表格数据只摘录支撑结论的关键行，完整书目与作者单位不写入正文。确有帮助的常识解释必须标注“补充解释”，无法核对才标注“待核对”。不要写一级/二级标题，不要插图片/HTML/外部链接，不要输出审校意见或修改建议。不要重复章节标题、空泛开场、来源原文或其他章节。用Markdown，子标题可用###。返回 {"markdown":"正文","questions":["检验理解而非死记数字的复习问题"]}。'''
WRITE += '\n每章最多使用 3 个 ### 三级标题，且仅用于真正的概念、机制或应用主题；其余转折、例子、步骤、实验条件和补充说明直接写入段落，或用 **短标签**，不能伪装成同级标题。'
AUDIT='''你是严格但克制的事实核对者。只报告会误导读者的明确问题：数字/列对应错误、否定或条件改变、关键结论遗漏、无依据断言。不要报告措辞偏好、作者单位是否完整、来源已明确支持的表述，也不要把“可进一步补充”当错误。最多3项，每项用1至2句说明错误并给出可直接采用的修改；没有实质错误返回空数组。返回 {"issues":[{"message":"简短问题与修改建议","source_ids":["输入来源ID"]}]}。'''
REPAIR='''依据核对意见修订这一节笔记。只能使用给定来源；逐项检查核对意见，如果意见与来源冲突，以来源为准。修正数字、否定、条件和归属，保留原有结构与正确内容，不扩写其他章节，不添加新主题，不写修订说明。返回 {"markdown":"修订后的完整正文","questions":["保留或改进后的复习问题"]}。'''
TEMPLATES={'course':'课程笔记：概念、机制、例子、易错点','revision':'考前速记：高密度要点、关键公式、易混淆点',
           'meeting':'会议纪要：议题、讨论、已明确的决议、负责人和截止时间，区分建议与已决定事项',
           'summary':'总结纪要：结论优先、依据、后续行动'}
DETAIL={'brief':'精简，每节约150-350中文字，去除重复铺垫','normal':'适中，每节约350-700中文字','detailed':'详细，每节约600-1200中文字，保留必要步骤和例子'}
MATERIAL_GUIDANCE={'paper':'按研究问题与贡献→方法机制→训练/实现设定→实验设计与结果→结论和局限组织。任何实验结果都必须与数据集、对比对象、指标和设置同组呈现；缺失信息明确说明，不能从常识补全。','lecture_slides':'恢复标题与要点的层级和讲授逻辑，补足必要连接，但不要把每页逐条抄写。','personal_notes':'保留作者自己的重点、疑问和缩写，整理跳跃内容但不要抹平成通用教材。','textbook':'按先修概念、定义、推导、例题和适用边界组织。','exercise':'先说明题目目标与已知条件，再给关键步骤、答案检查和可迁移方法。','meeting':'明确分开已决定事项、建议、行动项和待确认事项；行动项写负责人、截止时间和证据。','general':'按结论、依据、机制和边界组织，避免来源顺序复述。'}

def pack(items,limit=10000,text=lambda x:x['text']):
    batches=[]; batch=[]; size=0
    for item in items:
        n=len(text(item))
        if batch and size+n>limit:
            batches.append(batch); batch=[]; size=0
        batch.append(item); size+=n
    if batch: batches.append(batch)
    return batches

def _sid(prefix,*value):
    return prefix+'-'+digest(value)[:12]

def _metadata_like(text):
    lower=text.lower()
    return len(re.findall(r'\[\d+\]',text))>=4 or lower.count(' et al')>=3 or ('references' in lower and len(text)>500)

_FORMULA_PLACEHOLDER=re.compile(r'^\s*(?:formula|fm)(?:[-_−]\w+)?\s*$',re.I)

def _formula_latex(value):
    """Normalize and validate a formula from the trusted knowledge IR."""
    latex=str(value or '').strip().strip('$').strip()
    if _FORMULA_PLACEHOLDER.fullmatch(latex):
        raise ValueError('公式必须是实际 LaTeX，不能使用 formula-1 等占位符')
    if not latex:
        raise ValueError('公式不能为空')
    from rendering import guard_math
    guard_math('$$'+latex+'$$')
    return latex

def card_validator(sources):
    """Validate full IR while accepting legacy card-only model responses."""
    allowed={b['id'] for b in sources}; by_source={b['id']:b for b in sources}
    formula_repair_requested=False
    def validate(data):
        nonlocal formula_repair_requested
        normalized=[]
        for value in data['cards']:
            value=dict(value)
            value['points']=[str(item.get('statement') or item.get('text') or item) if isinstance(item,dict) else str(item)
                             for item in value.get('points',[])]
            value['formulas']=[str(item.get('latex') or item.get('formula') or item) if isinstance(item,dict) else str(item)
                               for item in value.get('formulas',[])]
            value['examples']=[str(item.get('text') or item.get('example') or item) if isinstance(item,dict) else str(item)
                               for item in value.get('examples',[])]
            normalized.append(value)
        cards=[Card.model_validate(c).model_dump() for c in normalized]
        if not cards: raise ValueError('卡片不能为空')
        for card in cards:
            if not set(card['source_ids'])<=allowed: raise ValueError('卡片包含未知来源ID')

        raw_claims=data.get('claims') or []
        derived={}
        if not raw_claims:
            for n,card in enumerate(cards):
                derived[n]=[]
                for p,point in enumerate(card['points']):
                    temp=f'legacy-claim-{n}-{p}';derived[n].append(temp)
                    raw_claims.append({'id':temp,'statement':point,'claim_type':'fact','source_ids':card['source_ids'],
                                       'conditions':[],'confidence':1.0,'importance':card['importance']})
        claims=[]; claim_map={}
        for raw in raw_claims:
            claim=Claim.model_validate(raw).model_dump()
            if claim['id'] in claim_map: raise ValueError('声明ID重复')
            if not set(claim['source_ids'])<=allowed: raise ValueError('声明包含未知来源ID')
            stable=_sid('cl',claim['statement'],claim['claim_type'],claim['conditions'],claim['context'])
            claim_map[claim['id']]=stable;claim['id']=stable;claims.append(claim)

        claim_ids=set(claim_map)
        for n,card in enumerate(cards):
            refs=card['claim_ids'] or derived.get(n,[])
            refs=[ref for ref in refs if ref in claim_ids]
            if not refs:
                refs=[raw['id'] for raw in raw_claims if set(raw['source_ids'])&set(card['source_ids'])]
            card['claim_ids']=list(dict.fromkeys(claim_map[ref] for ref in refs))

        raw_concepts=data.get('concepts') or []
        if not raw_concepts:
            raw_concepts=[{'id':f'legacy-concept-{n}','name':card['title'],'definition':'',
                           'claim_ids':[ref for ref in (derived.get(n,[]) or [])],'depends_on':[]}
                          for n,card in enumerate(cards)]
        concept_models=[Concept.model_validate(value).model_dump() for value in raw_concepts]
        concept_ids={c['id'] for c in concept_models}
        if len(concept_ids)!=len(concept_models): raise ValueError('概念ID重复')
        concept_map={c['id']:_sid('co',c['name'],c['definition']) for c in concept_models}
        concepts=[]
        for concept in concept_models:
            concept['claim_ids']=[claim_map[x] for x in concept['claim_ids'] if x in claim_map]
            concept['depends_on']=[concept_map[x] for x in concept['depends_on'] if x in concept_map]
            concept['id']=concept_map[concept['id']];concepts.append(concept)

        formulas=[]
        raw_formulas=data.get('formulas') or []
        if not raw_formulas:
            for n,card in enumerate(cards):
                for p,value in enumerate(card['formulas']):
                    raw_formulas.append({'id':f'legacy-formula-{n}-{p}','latex':value,'meaning':'','variables':[],
                                         'source_ids':card['source_ids'],'claim_ids':derived.get(n,[])})
        for raw in raw_formulas:
            formula=Formula.model_validate(raw).model_dump()
            if not set(formula['source_ids'])<=allowed: raise ValueError('公式引用未知来源ID')
            try:
                formula['latex']=_formula_latex(formula['latex'])
            except ValueError as exc:
                if not formula_repair_requested:
                    formula_repair_requested=True
                    raise ValueError('公式字段无效：'+str(exc)+
                                     '。请重新生成完整 JSON：公式 latex 必须是可渲染的实际 LaTeX；'
                                     '若无法从来源确认公式，请从 formulas 和 cards.formulas 中删除该项。')
                # The repair attempt still failed. Formula rendering is optional
                # enrichment, so preserve its grounded claims and continue.
                continue
            formula['claim_ids']=[claim_map[x] for x in formula['claim_ids'] if x in claim_map]
            formula['id']=_sid('fm',formula['latex'],formula['meaning'],formula['variables']);formulas.append(formula)

        experiments=[]
        for raw in data.get('experiments',[]):
            experiment=Experiment.model_validate(raw).model_dump()
            if not set(experiment['source_ids'])<=allowed: raise ValueError('实验引用未知来源ID')
            experiment['claim_ids']=[claim_map[x] for x in experiment['claim_ids'] if x in claim_map]
            experiment['id']=_sid('ex',experiment['name'],experiment['dataset'],experiment['setup'],experiment['metrics'],experiment['results'])
            experiments.append(experiment)
        if not experiments:
            for source in sources:
                linked=[claim for claim in claims if source['id'] in claim['source_ids'] and
                        (claim['claim_type']=='result' or (source.get('material_type')=='paper' and
                         re.search(r'\d|BLEU|F1|MRR|PPL|accuracy|准确率|性能|结果',claim['statement'],re.I)))]
                if not linked: continue
                results=[claim['statement'] for claim in linked]
                conditions=list(dict.fromkeys(value for claim in linked for value in claim['conditions']))
                experiments.append({'id':_sid('ex','来源实验结果',source['id'],results),'name':'来源实验结果',
                                    'dataset':'','setup':conditions,'metrics':[],'results':results,
                                    'source_ids':[source['id']],'claim_ids':[claim['id'] for claim in linked]})
        _enrich_claim_contexts(claims,experiments)

        visuals=[]
        raw_visuals=data.get('visuals') or []
        if not raw_visuals:
            for source in sources:
                if source.get('asset'):
                    linked=[claim['id'] for claim in claims if source['id'] in claim['source_ids']]
                    raw_visuals.append({'id':'legacy-visual-'+source['id'],'source_id':source['id'],
                                        'title':source['text'].splitlines()[0][:120] or '来源图片',
                                        'visual_type':'table' if source.get('kind')=='table' else 'image',
                                        'description':source['text'][:800],'claim_ids':linked})
        stable_claims={claim['id'] for claim in claims}
        for raw in raw_visuals:
            visual=VisualEvidence.model_validate(raw).model_dump()
            if visual['source_id'] not in allowed: raise ValueError('视觉对象引用未知来源ID')
            refs=[]
            for ref in visual['claim_ids']:
                if ref in claim_map: refs.append(claim_map[ref])
                elif ref in stable_claims: refs.append(ref)
            visual['claim_ids']=refs
            visual['description']=visual['description'][:600]
            visual['id']=_sid('vi',visual['source_id'],visual['title']);visuals.append(visual)
        ir=KnowledgeIR(claims=claims,concepts=concepts,formulas=formulas,experiments=experiments,visuals=visuals).model_dump()
        return {'cards':cards,'knowledge':ir}
    return validate

def _merge_entities(target,values,list_fields):
    for value in values:
        current=target.get(value['id'])
        if current:
            for field in list_fields:
                current[field]=list(dict.fromkeys(current.get(field,[])+value.get(field,[])))
        else: target[value['id']]=value

def _fold_text(value):
    """Normalize presentation differences without removing factual content."""
    value=unicodedata.normalize('NFKC',str(value)).casefold()
    return re.sub(r'[^\w\u4e00-\u9fff]+','',value)

def _numbers(value):
    return tuple(sorted(re.findall(r'(?<![\w.])-?\d+(?:\.\d+)?%?',unicodedata.normalize('NFKC',str(value)))))

def _negations(value):
    text=str(value).casefold()
    markers=('不','未','无','非','不能','没有','not','no ','without','never','cannot',"n't")
    return tuple(marker for marker in markers if marker in text)

def _similarity(left,right):
    a=_fold_text(left);b=_fold_text(right)
    if not a or not b: return 0.0
    if a==b: return 1.0
    ratio=SequenceMatcher(None,a,b).ratio()
    def grams(value):
        return {value[i:i+2] for i in range(max(1,len(value)-1))}
    ga,gb=grams(a),grams(b)
    jaccard=len(ga&gb)/max(1,len(ga|gb))
    stop={'a','an','the','is','are','was','were','on','in','of','to','for','and','or'}
    def tokens(value):
        normalized=unicodedata.normalize('NFKC',str(value)).casefold()
        words={word for word in re.findall(r'[a-z]+|\d+(?:\.\d+)?%?',normalized) if word not in stop}
        chinese=''.join(re.findall(r'[\u4e00-\u9fff]',normalized))
        words.update(chinese[i:i+2] for i in range(max(0,len(chinese)-1)))
        return words
    ta,tb=tokens(left),tokens(right)
    token_jaccard=len(ta&tb)/max(1,len(ta|tb))
    return max(ratio,jaccard,token_jaccard)

def _same_claim(left,right):
    if left['claim_type']!=right['claim_type']: return False
    combined_left=left['statement']+' '+' '.join(left.get('conditions',[]))
    combined_right=right['statement']+' '+' '.join(right.get('conditions',[]))
    if _numbers(combined_left)!=_numbers(combined_right): return False
    if _negations(combined_left)!=_negations(combined_right): return False
    left_conditions=sorted(_fold_text(x) for x in left.get('conditions',[]) if _fold_text(x))
    right_conditions=sorted(_fold_text(x) for x in right.get('conditions',[]) if _fold_text(x))
    # A missing or different condition can change the meaning of an experimental result.
    if left_conditions!=right_conditions: return False
    for field in ('datasets','models','metrics','setup'):
        a=sorted(_fold_text(x) for x in left.get('context',{}).get(field,[]) if _fold_text(x))
        b=sorted(_fold_text(x) for x in right.get('context',{}).get(field,[]) if _fold_text(x))
        if a!=b: return False
    return _similarity(left['statement'],right['statement'])>=.72

def _enrich_claim_contexts(claims,experiments):
    metric_pattern=r'\b(?:BLEU|F1|MRR(?:@\d+)?|PPL|accuracy|precision|recall|AUC)\b|准确率|精确率|召回率|困惑度'
    by_claim={claim['id']:claim for claim in claims}
    for claim in claims:
        context=claim.setdefault('context',{})
        for field in ('datasets','models','metrics','setup','values'):
            context[field]=list(dict.fromkeys(context.get(field,[])))
        for condition in claim.get('conditions',[]):
            lower=condition.casefold()
            if re.search(r'dataset|data set|corpus|testset|devset|数据集|语料',lower):
                _append_unique(context['datasets'],[condition])
            elif re.search(r'model|version|\bv\d|\bbase\b|\bbig\b|\blarge\b|模型|版本',lower):
                _append_unique(context['models'],[condition])
            else:
                _append_unique(context['setup'],[condition])
        _append_unique(context['values'],_numbers(claim['statement']))
        _append_unique(context['metrics'],re.findall(metric_pattern,claim['statement'],re.I))
        _append_unique(context['models'],re.findall(r'\b[A-Za-z][\w-]*(?:\s*\((?:base|big|large|small)\)|[- ]v?\d+(?:\.\d+)*)',claim['statement'],re.I))
        context['negated']=bool(context.get('negated') or _negations(claim['statement']))
    for experiment in experiments:
        for claim_id in experiment.get('claim_ids',[]):
            claim=by_claim.get(claim_id)
            if not claim: continue
            context=claim['context']
            if experiment.get('dataset'): _append_unique(context['datasets'],[experiment['dataset']])
            _append_unique(context['metrics'],experiment.get('metrics',[]))
            _append_unique(context['setup'],experiment.get('setup',[]))

def _scope_signature(claim):
    context=claim.get('context',{})
    result=[]
    for field in ('datasets','models','metrics','setup'):
        values=[]
        for value in context.get(field,[]):
            value=re.sub(r'-?\d+(?:\.\d+)?%?','#',str(value))
            if _fold_text(value): values.append(_fold_text(value))
        result.append(tuple(sorted(values)))
    return tuple(result)

def _claim_conflicts(claims):
    conflicts=[]
    for index,left in enumerate(claims):
        if left['claim_type'] not in ('fact','result','decision'): continue
        for right in claims[index+1:]:
            if left['claim_type']!=right['claim_type'] or _scope_signature(left)!=_scope_signature(right): continue
            def basis(value):
                value=re.sub(r'-?\d+(?:\.\d+)?(?:e[+-]?\d+)?%?','#',value,flags=re.I)
                value=re.sub(r'\b(?:not|no|never|without|cannot)\b|不|未|无|非|没有','',value,flags=re.I)
                return value
            a,b=basis(left['statement']),basis(right['statement'])
            # Conflict review must compare the same subject, model and metric. A broad
            # semantic resemblance would confuse legitimate base/big or method baselines.
            if _fold_text(a)!=_fold_text(b) and _similarity(a,b)<.96: continue
            left_values=tuple(left.get('context',{}).get('values',[])) or _numbers(left['statement'])
            right_values=tuple(right.get('context',{}).get('values',[])) or _numbers(right['statement'])
            left_neg=bool(left.get('context',{}).get('negated') or _negations(left['statement']))
            right_neg=bool(right.get('context',{}).get('negated') or _negations(right['statement']))
            if left_neg!=right_neg:
                kind='polarity_mismatch';field='negated';detail='肯定/否定含义不一致'
            elif left_values!=right_values:
                kind='numeric_mismatch';field='values';detail='同一限定条件下的数值不一致'
            else: continue
            ids=[left['id'],right['id']]
            conflicts.append({'id':_sid('cf',kind,ids),'kind':kind,'claim_ids':ids,
                              'source_ids':list(dict.fromkeys(left['source_ids']+right['source_ids'])),
                              'field':field,'message':detail+'：'+left['statement']+' / '+right['statement']})
    return conflicts

def _append_unique(target,values,similar=False):
    for value in values:
        if value in target: continue
        if similar and any(_numbers(value)==_numbers(old) and _negations(value)==_negations(old)
                           and _similarity(value,old)>=.9 for old in target):
            continue
        target.append(value)

def normalize_knowledge(cards,knowledge):
    """Conservatively merge repeated cross-batch entities and repair all references."""
    claims=[];claim_map={}
    for claim in knowledge.get('claims',[]):
        current=next((item for item in claims if _same_claim(item,claim)),None)
        if current:
            claim_map[claim['id']]=current['id']
            _append_unique(current['source_ids'],claim.get('source_ids',[]))
            current['confidence']=max(current['confidence'],claim['confidence'])
            current['importance']=max(current['importance'],claim['importance'])
        else:
            claim=dict(claim);claim['source_ids']=list(dict.fromkeys(claim['source_ids']))
            claims.append(claim);claim_map[claim['id']]=claim['id']

    def remap_claims(values):
        return list(dict.fromkeys(claim_map[value] for value in values if value in claim_map))

    concepts=[];concept_map={}
    for concept in knowledge.get('concepts',[]):
        current=next((item for item in concepts
                      if _fold_text(item['name'])==_fold_text(concept['name'])
                      or (_similarity(item['name'],concept['name'])>=.94
                          and (not item['definition'] or not concept['definition']
                               or _similarity(item['definition'],concept['definition'])>=.8))),None)
        if current:
            concept_map[concept['id']]=current['id']
            _append_unique(current['claim_ids'],remap_claims(concept.get('claim_ids',[])))
            if len(concept.get('definition',''))>len(current.get('definition','')):
                current['definition']=concept['definition']
        else:
            concept=dict(concept);concept['claim_ids']=remap_claims(concept.get('claim_ids',[]))
            concepts.append(concept);concept_map[concept['id']]=concept['id']
    for concept in concepts:
        concept['depends_on']=list(dict.fromkeys(concept_map[value] for value in concept.get('depends_on',[])
                                                 if value in concept_map and concept_map[value]!=concept['id']))

    entities={}
    for key in ('formulas','experiments','visuals'):
        values=[]
        for item in knowledge.get(key,[]):
            item=dict(item);item['claim_ids']=remap_claims(item.get('claim_ids',[]))
            values.append(item)
        entities[key]=values

    normalized_cards=[]
    for card in cards:
        card=dict(card);card['claim_ids']=remap_claims(card.get('claim_ids',[]))
        current=next((item for item in normalized_cards
                      if (_fold_text(item['title'])==_fold_text(card['title']))
                      or (set(item['claim_ids'])&set(card['claim_ids'])
                          and _similarity(item['title'],card['title'])>=.72)),None)
        if current:
            _append_unique(current['points'],card.get('points',[]),similar=True)
            for field in ('source_ids','formulas','examples','claim_ids'):
                _append_unique(current[field],card.get(field,[]),similar=field in ('formulas','examples'))
            current['importance']=max(current['importance'],card['importance'])
        else:
            normalized_cards.append(card)
    for card in normalized_cards:
        card['id']='k-'+digest([card['title'],sorted(card['claim_ids']),card['points']])[:12]
    # A claim may be mentioned by several cards, but only one card owns its main
    # chapter assignment. Other cards can still keep their source-backed prose.
    owner={}
    for card in sorted(normalized_cards,key=lambda item:-item['importance']):
        unique=[]
        for claim_id in card['claim_ids']:
            if claim_id not in owner:
                owner[claim_id]=card['id'];unique.append(claim_id)
        card['claim_ids']=unique

    normalized=KnowledgeIR(schema_version=knowledge.get('schema_version',1),claims=claims,concepts=concepts,
                           formulas=entities['formulas'],experiments=entities['experiments'],
                           visuals=entities['visuals'],conflicts=_claim_conflicts(claims)).model_dump()
    return normalized_cards,normalized

def build_concept_graph(ctx,knowledge):
    concepts=knowledge.get('concepts',[]);known={item['id'] for item in concepts}
    edges=[]
    for concept in concepts:
        for dependency in concept.get('depends_on',[]):
            if dependency in known and dependency!=concept['id']:
                edges.append({'source_id':dependency,'target_id':concept['id'],
                              'relation':'prerequisite','reason':'提取阶段明确的概念依赖'})
    for index,left in enumerate(concepts):
        for right in concepts[index+1:]:
            if set(left.get('claim_ids',[]))&set(right.get('claim_ids',[])):
                edges.append({'source_id':left['id'],'target_id':right['id'],
                              'relation':'related','reason':'共享声明证据'})
    if len(concepts)>=4:
        claims={item['id']:item['statement'] for item in knowledge.get('claims',[])}
        payload=[{'id':item['id'],'name':item['name'],'definition':item.get('definition','')[:240],
                  'claims':[claims[x] for x in item.get('claim_ids',[]) if x in claims][:2]}
                 for item in concepts]
        def validate(data):
            result=[]
            for raw in data.get('edges',[]):
                edge=ConceptEdge.model_validate(raw).model_dump()
                if edge['source_id'] not in known or edge['target_id'] not in known or edge['source_id']==edge['target_id']:
                    raise ValueError('概念图包含未知或自引用ID')
                result.append(edge)
            return {'edges':result}
        try: edges.extend(ctx.llm('outline',GRAPH,payload,validate=validate)['edges'])
        except Cancelled: raise
        except Exception: ctx.warn('全局概念关系提取失败，已保留确定性关系')
    result=[];seen=set();prerequisites={key:set() for key in known}
    def reaches(start,target):
        pending=[start];visited=set()
        while pending:
            node=pending.pop()
            if node==target:return True
            if node in visited:continue
            visited.add(node);pending.extend(prerequisites.get(node,()))
        return False
    for edge in edges:
        key=(edge['source_id'],edge['target_id'],edge['relation'])
        if key in seen:continue
        if edge['relation']=='prerequisite':
            if reaches(edge['target_id'],edge['source_id']):continue
            prerequisites[edge['source_id']].add(edge['target_id'])
        seen.add(key);result.append(edge)
    knowledge=dict(knowledge);knowledge['concept_edges']=result
    return KnowledgeIR(**knowledge).model_dump()

def make_knowledge(ctx,blocks):
    batches=pack(blocks);all_cards=[]
    stores={key:{} for key in ('claims','concepts','formulas','experiments','visuals')}
    merge_fields={'claims':['source_ids','conditions'],'concepts':['claim_ids','depends_on'],
                  'formulas':['source_ids','claim_ids'],'experiments':['source_ids','claim_ids'],
                  'visuals':['claim_ids']}
    for i,batch in enumerate(batches):
        ctx.progress(.46+.16*i/len(batches),f'提取知识 {i+1}/{len(batches)}')
        data=ctx.llm('cards',CARDS,[{'id':b['id'],'text':b['text'],'kind':b.get('kind','text'),
                                    'has_asset':bool(b.get('asset')),'material_type':b.get('material_type','general'),
                                    'material_role':b.get('material_role','primary')} for b in batch],validate=card_validator(batch))
        assigned={s for c in data['cards'] for s in c['source_ids']}
        for block in batch:
            if block['id'] in assigned: continue
            ctx.warn('知识提取遗漏一个来源，已保留原文声明供核对：'+block['id'])
            metadata=_metadata_like(block['text']);statement=block['text'][:800]
            importance=1 if metadata else 3;title='参考资料元信息' if metadata else '待整理的原文'
            claim={'id':_sid('cl',statement,'fact',[]),'statement':statement,'claim_type':'fact',
                   'source_ids':[block['id']],'conditions':[],'confidence':.7,'importance':importance}
            concept={'id':_sid('co',title,''),'name':title,'definition':'',
                     'claim_ids':[claim['id']],'depends_on':[]}
            data['knowledge']['claims'].append(claim);data['knowledge']['concepts'].append(concept)
            data['cards'].append({'id':'fallback','title':title,'points':[statement],
                                  'source_ids':[block['id']],'importance':importance,'formulas':[],
                                  'examples':[],'claim_ids':[claim['id']]})
        all_cards.extend(data['cards'])
        for key in stores:
            _merge_entities(stores[key],data['knowledge'][key],merge_fields[key])
    merged={}
    for card in all_cards:
        key=digest([card['title'],card['points'],card['formulas'],card['examples']])
        if key in merged:
            merged[key]['source_ids']=list(dict.fromkeys(merged[key]['source_ids']+card['source_ids']))
            merged[key]['claim_ids']=list(dict.fromkeys(merged[key]['claim_ids']+card['claim_ids']))
        else:
            card['id']='k-'+key[:12];merged[key]=card
    knowledge=KnowledgeIR(**{key:list(values.values()) for key,values in stores.items()}).model_dump()
    return normalize_knowledge(list(merged.values()),knowledge)

def make_cards(ctx,blocks):
    return make_knowledge(ctx,blocks)[0]

def _fallback_plan(cards):
    parent={card['id']:card['id'] for card in cards}
    def root(value):
        while parent[value]!=value:
            parent[value]=parent[parent[value]];value=parent[value]
        return value
    def union(a,b):
        a,b=root(a),root(b)
        if a!=b:parent[b]=a
    owners={}
    for card in cards:
        for claim in card.get('claim_ids',[]):
            if claim in owners:union(card['id'],owners[claim])
            else:owners[claim]=card['id']
    groups=[];by_root={}
    for card in cards:by_root.setdefault(root(card['id']),[]).append(card)
    pending=[]
    for component in by_root.values():
        if pending and len(pending)+len(component)>10:groups.append(pending);pending=[]
        pending.extend(component)
        if len(pending)>=10:groups.append(pending);pending=[]
    if pending:groups.append(pending)
    return [{'id':'','title':group[0]['title'],'card_ids':[card['id'] for card in group]} for group in groups]

def plan(ctx,cards,knowledge=None,material_type='general'):
    knowledge=knowledge or {};allowed={c['id'] for c in cards}
    skeleton=[{'id':c['id'],'title':c['title'],'importance':c['importance'],
               'claim_ids':c.get('claim_ids',[])} for c in cards]
    payload={'cards':skeleton,
             'concepts':[{'id':c['id'],'name':c['name'],'claim_ids':c.get('claim_ids',[])}
                         for c in knowledge.get('concepts',[])],
             'concept_edges':knowledge.get('concept_edges',[])}
    def validate(data):
        chapters=[Chapter.model_validate(c).model_dump() for c in data['chapters']]
        ids=[cid for ch in chapters for cid in ch['card_ids']]
        if set(ids)!=allowed or len(ids)!=len(set(ids)): raise ValueError('章节必须恰好覆盖全部卡片一次')
        if any(len(ch['card_ids'])>10 for ch in chapters): raise ValueError('每节最多10张卡片')
        chapter_by_card={card_id:index for index,ch in enumerate(chapters) for card_id in ch['card_ids']}
        locations={}
        for card in cards:
            for claim in card.get('claim_ids',[]): locations.setdefault(claim,set()).add(chapter_by_card[card['id']])
        if any(len(value)>1 for value in locations.values()): raise ValueError('同一声明不能分配到多个主章节')
        return {'chapters':chapters}
    outline_prompt=PAPER_OUTLINE if material_type=='paper' else OUTLINE
    try: chapters=ctx.llm('outline',outline_prompt,payload,validate=validate)['chapters']
    except Cancelled: raise
    except Exception:
        ctx.warn('全局大纲模型未通过检查，已按声明归属建立完整大纲')
        chapters=_fallback_plan(cards)
    for ch in chapters:ch['id']='ch-'+digest(ch['card_ids'])[:12]
    return chapters

def validate_written(data,max_chars=60000,include_review_questions=True):
    md=data['markdown']
    if not isinstance(md,str) or not md.strip(): raise ValueError('章节正文为空')
    if len(md)>max_chars: raise ValueError(f'正文超过本节上限 {max_chars} 字符，请压缩并删除跨章节重复')
    # Model cannot inject local/remote resources. Images are bound to source assets by the application.
    md=re.sub(r'!\[[^\]]*\]\([^)]*\)','',md)
    md=re.sub(r'<[^>]*>','',md)
    if re.search(r'\$\$\s*(?:formula|fm)[-_\w]*\s*\$\$',md,re.I):
        raise ValueError('公式占位符不能作为公式；请使用 knowledge_ir 中的 [[FORMULA:公式ID]] 或写出实际 LaTeX')
    if md.count('```')%2: raise ValueError('代码块未闭合')
    raw_questions=data.get('questions',[]) if include_review_questions else []
    # Compatible gateways sometimes encode optional questions as objects.
    # Keep their prompt, but do not let that optional shape fail a chapter.
    questions=[]
    if isinstance(raw_questions,list):
        for question in raw_questions:
            if isinstance(question,str): text=question
            elif isinstance(question,dict):
                text=next((question.get(key) for key in ('question','prompt','text','title')
                           if isinstance(question.get(key),str)), '')
            else: text=''
            if text and text.strip():questions.append(text.strip())
    return {'markdown':md.strip(),'questions':questions[:8]}

def compact_subheadings(markdown,max_sections=3):
    """Keep only substantive subheadings; render the rest as quiet labels."""
    sections=0;inside_fence=False;lines=[]
    for line in markdown.splitlines():
        if line.strip().startswith('```'):inside_fence=not inside_fence
        match=None if inside_fence else re.match(r'^(#{3,4})\s+(.+?)\s*#*\s*$',line)
        if match:
            title=match.group(2).strip()
            if len(match.group(1))==3 and sections<max_sections:
                sections+=1;lines.append('### '+title)
            else: lines.append('**'+title+'**')
        else: lines.append(line)
    return '\n'.join(lines)

def clean_final_markdown(markdown,formulas,visuals):
    """Resolve trusted IR tokens and remove internal review prose from reader output."""
    formula_by_id={item['id']:item for item in formulas};visual_ids={item['id'] for item in visuals}
    def formula(match):
        item=formula_by_id.get(match.group(1))
        if not item:return ''
        # This is a defense-in-depth check for notes created before IR validation
        # was tightened. Never surface an internal placeholder as a math block.
        try: latex=_formula_latex(item.get('latex'))
        except ValueError: return ''
        return '\n$$'+latex+'$$\n'
    markdown=re.sub(r'\[\[FORMULA:([\w-]+)\]\]',formula,markdown)
    markdown=re.sub(r'\[\[VISUAL:([\w-]+)\]\]',lambda m:m.group() if m.group(1) in visual_ids else '',markdown)
    markdown=re.sub(r'(?ms)^#{1,4}\s*(?:核对提示|审校提示|修改建议)\s*$.*?(?=^#{1,4}\s|\Z)','',markdown)
    markdown=re.sub(r'(?m)^[-*]\s*(?:建议改为|应改为|请修改).*$','',markdown)
    markdown=re.sub(r'(?m)(?:待核对项|待确认项)[：:].*$','',markdown)
    markdown=re.sub(r'[（(]\s*(?:cl|co|fo|ex|vi)-[0-9a-f]+\s*[）)]','',markdown,flags=re.I)
    markdown=re.sub(r'(?<!VISUAL:)\b(?:cl|co|fo|ex|vi)-[0-9a-f]+\b','',markdown,flags=re.I)
    markdown=compact_subheadings(markdown)
    return re.sub(r'\n{3,}','\n\n',markdown).strip()

def finalize_chapters(chapters,knowledge):
    """Remove repeated prose globally while preserving numbers and the first full explanation."""
    seen=[];removed=0
    for chapter in chapters:
        if chapter.get('status')!='ready':continue
        blocks=re.split(r'\n\s*\n',chapter['markdown']);kept=[]
        for block in blocks:
            plain=re.sub(r'[#*_`>|\[\]()]','',block);normalized=_fold_text(plain)
            duplicate=False
            if len(normalized)>=25 and not block.lstrip().startswith(('|','$$','[[VISUAL:')):
                nums=_numbers(plain)
                duplicate=any(nums==old_nums and _similarity(plain,old)>=.9 for old,old_nums in seen)
                if not duplicate:seen.append((plain,nums))
            if duplicate:removed+=1
            else:kept.append(block)
        chapter['markdown']='\n\n'.join(kept).strip();chapter['deduplicated_blocks']=removed
    return chapters,removed

def write_chapter(ctx,ch,cards,blocks,options,instruction=None,previous=None,conversation=None,outline=None,knowledge=None):
    subset=[c for c in cards if c['id'] in ch['card_ids']]
    source_ids=list(dict.fromkeys(s for c in subset for s in c['source_ids']))
    sources=[b for b in blocks if b['id'] in source_ids]
    # The evidence budget is enforced before any remote call; large chapters are split by the caller.
    max_chars={'brief':1300,'normal':3000,'detailed':5000}[options.detail]
    knowledge=knowledge or {}
    claim_ids={claim for card in subset for claim in card.get('claim_ids',[])}
    selected_claims=[item for item in knowledge.get('claims',[]) if item['id'] in claim_ids]
    if not selected_claims and not claim_ids:
        selected_claims=[item for item in knowledge.get('claims',[]) if set(item['source_ids'])&set(source_ids)]
    selected_claim_ids={item['id'] for item in selected_claims}
    selected_concepts=[item for item in knowledge.get('concepts',[]) if set(item.get('claim_ids',[]))&selected_claim_ids]
    selected_concept_ids={item['id'] for item in selected_concepts}
    chapter_ir={
        'claims':selected_claims,
        'concepts':selected_concepts,
        'concept_edges':[item for item in knowledge.get('concept_edges',[])
                         if item['source_id'] in selected_concept_ids and item['target_id'] in selected_concept_ids],
        'formulas':[item for item in knowledge.get('formulas',[]) if set(item.get('claim_ids',[]))&selected_claim_ids or set(item.get('source_ids',[]))&set(source_ids)],
        'experiments':[item for item in knowledge.get('experiments',[]) if set(item.get('claim_ids',[]))&selected_claim_ids or set(item.get('source_ids',[]))&set(source_ids)],
        'visuals':[item for item in knowledge.get('visuals',[]) if item.get('source_id') in source_ids],
        'conflicts':[item for item in knowledge.get('conflicts',[]) if set(item.get('claim_ids',[]))&selected_claim_ids],
    }
    compact_cards=[{'id':card['id'],'title':card['title'],'importance':card['importance'],
                    'claim_ids':card.get('claim_ids',[]),'source_ids':card['source_ids']} for card in subset]
    data={'chapter':ch['title'],'target':TEMPLATES[options.template],'detail':DETAIL[options.detail],
          'hard_character_limit':max_chars,
          'material_guidance':[MATERIAL_GUIDANCE[k] for k in dict.fromkeys(s.get('material_type','general') for s in sources) if k in MATERIAL_GUIDANCE],
          'style':options.style,'instructions':options.instructions,'cards':compact_cards,
          'knowledge_ir':chapter_ir,
          'sources':[{'id':s['id'],'text':s['text'],'material_type':s.get('material_type','general'),
                      'material_role':s.get('material_role','primary')} for s in sources],
          # Titles are enough to establish scope. Supplying other chapters' full points makes
          # weaker models repeat those points despite the negative instruction.
          'other_chapter_topics_do_not_repeat':[c['title'] for c in cards if c['id'] not in ch['card_ids'] and c['importance']>1]}
    primary_types={s.get('material_type') for s in blocks if s.get('material_role','primary')=='primary'}
    data['paper_mode']=primary_types=={'paper'}
    if instruction:
        data.update({'revision_request':instruction,'previous':previous,'conversation':(conversation or [])[-6:]})
    if len(str(data))>60000: raise ValueError('本章节证据过多，请拆分后重试')
    writer_prompt=WRITE if options.include_review_questions else WRITE+'\n本次用户选择跳过复习问题：questions 必须返回空数组 []，只生成笔记正文。'
    fallback_issue=None
    try:
        written=ctx.llm('writer',writer_prompt,data,validate=lambda value:validate_written(value,max_chars,options.include_review_questions))
    except Cancelled: raise
    except (RuntimeError,ValueError,KeyError,TypeError) as exc:
        text='\n\n'.join(str(source.get('text','')).strip()[:3500] for source in sources if source.get('text')).strip()
        written={'markdown':text or '来源未提取到可整理文字，请查看原始资料。','questions':[]}
        fallback_issue={'message':'已跳过公式、配图、复习题和自动审校等增强项，按解析来源生成基础正文：'+str(exc)[:160],
                        'source_ids':source_ids}
    written['markdown']=clean_final_markdown(written['markdown'],chapter_ir['formulas'],chapter_ir['visuals'])
    ctx.check()
    def audit_validate(value):
        issues=value['issues']
        if not isinstance(issues,list): raise ValueError('核对结果必须为列表')
        for issue in issues:
            if not isinstance(issue.get('message'),str) or not set(issue.get('source_ids',[]))<=set(source_ids):
                raise ValueError('核对引用无效')
        useful=[]
        for issue in issues:
            message=issue['message']
            if any(mark in message for mark in ('无需修改','无须修改','无实质错误')):
                continue
            if not any(action in message for action in ('建议','应改','改为','删除','补充','遗漏')):
                continue
            useful.append(issue)
        return {'issues':useful[:3]}
    def audit(markdown):
        return ctx.llm('outline',AUDIT,{'draft':markdown,'sources':data['sources']},validate=audit_validate)['issues']
    try:
        if fallback_issue: raise RuntimeError(fallback_issue['message'])
        issues=audit(written['markdown'])
        if issues:
            repair_data={'chapter':ch['title'],'draft':written['markdown'],'issues':issues,
                         'sources':data['sources'],'hard_character_limit':max_chars}
            repair_prompt=REPAIR if options.include_review_questions else REPAIR+'\n本次用户选择跳过复习问题：questions 必须返回空数组 []。'
            written=ctx.llm('writer',repair_prompt,repair_data,validate=lambda value:validate_written(value,max_chars,options.include_review_questions))
            written['markdown']=clean_final_markdown(written['markdown'],chapter_ir['formulas'],chapter_ir['visuals'])
            ctx.check()
            issues=audit(written['markdown'])
    except Cancelled: raise
    except Exception:
        issues=[fallback_issue] if fallback_issue else [{'message':'本节自动内容核对未完成，请对照来源检查','source_ids':source_ids}]
    if '待核对' in written['markdown'] and not issues:
        issues=[{'message':'正文含待核对内容，请结合来源确认后修订','source_ids':source_ids}]
    return {**ch,**written,'source_ids':source_ids,'visual_ids':[item['id'] for item in chapter_ir['visuals']],
            'issues':issues,'status':'ready','edited':bool(instruction),
            'conversation':(conversation or [])+([{'instruction':instruction}] if instruction else [])}

def generate(ctx,blocks,options):
    if blocks and (options.handwritten or all(b.get('material_type')=='lecture_slides' for b in blocks)):
        from learning import generate_slides
        return generate_slides(ctx,blocks,options)
    cards,knowledge=make_knowledge(ctx,blocks)
    ctx.progress(.63,'建立全局概念关系')
    knowledge=build_concept_graph(ctx,knowledge)
    ctx.progress(.64,'安排章节并检查知识覆盖')
    content_cards=[c for c in cards if c['importance']>1]
    if not content_cards: content_cards=cards
    if options.template in ('meeting','summary') and len(content_cards)<=8 and sum(len(b['text']) for b in blocks)<5000:
        chapters=[{'id':'','title':'会议纪要' if options.template=='meeting' else '总结纪要','card_ids':[c['id'] for c in content_cards]}]
    else:
        primary_types={b.get('material_type') for b in blocks if b.get('material_role','primary')=='primary'}
        material_type='paper' if primary_types=={'paper'} else 'general'
        chapters=plan(ctx,content_cards,knowledge,material_type=material_type)
    # Bound by evidence length too, not merely card count.
    bounded=[]
    by_id={c['id']:c for c in cards}; by_source={b['id']:b for b in blocks}
    for ch in chapters:
        groups=pack([by_id[c] for c in ch['card_ids']],24000,lambda c:' '.join(by_source[s]['text'] for s in c['source_ids']))
        for n,group in enumerate(groups):
            ids=[c['id'] for c in group]
            title=ch['title'] if n==0 else group[0]['title']
            bounded.append({'id':'ch-'+digest(ids)[:12],'title':title,'card_ids':ids})
    results=[None]*len(bounded)
    def one(ch):
        try: return write_chapter(ctx,ch,cards,blocks,options,outline=[{'title':c['title'],'card_ids':c['card_ids']} for c in bounded],knowledge=knowledge)
        except Cancelled: raise
        except Exception as exc:
            ctx.warn(ch['title']+' 生成失败：'+type(exc).__name__)
            return {**ch,'markdown':'','questions':[],'source_ids':list(dict.fromkeys(s for c in cards if c['id'] in ch['card_ids'] for s in c['source_ids'])),
                    'issues':[{'message':'章节生成失败，请重试','source_ids':[]}],'status':'failed','edited':False,'conversation':[]}
    with ThreadPoolExecutor(max_workers=3,thread_name_prefix='chapter') as pool:
        pending={pool.submit(one,ch):i for i,ch in enumerate(bounded)}
        for done,future in enumerate(as_completed(pending),1):
            results[pending[future]]=future.result()
            ctx.progress(.67+.29*done/max(1,len(bounded)),f'撰写与核对 {done}/{len(bounded)}')
    results,removed=finalize_chapters(results,knowledge)
    if removed:ctx.warn(f'终稿全局去重移除 {removed} 个重复段落')
    return cards,knowledge,results

def quality(data):
    cards=data.get('cards',[]); chapters=data.get('chapters',[]); blocks=data['sources']
    used={cid for ch in chapters if ch['status']=='ready' for cid in ch['card_ids']}
    card_sources={s for c in cards for s in c['source_ids']}
    claims=data.get('knowledge',{}).get('claims',[])
    conflicts=data.get('knowledge',{}).get('conflicts',[])
    used_claims={claim for card in cards if card['id'] in used for claim in card.get('claim_ids',[])}
    paragraphs=[];duplicates=0;internal=0;unresolved=0
    visual_ids={item['id'] for item in data.get('knowledge',{}).get('visuals',[])}
    for chapter in chapters:
        text=chapter.get('markdown','')
        internal+=len(re.findall(r'(?im)^#{1,4}\s*(?:核对提示|审校提示|修改建议)',text))
        unresolved+=len(re.findall(r'\[\[FORMULA:',text))
        unresolved+=sum(token not in visual_ids for token in re.findall(r'\[\[VISUAL:([\w-]+)\]\]',text))
        for paragraph in re.split(r'\n\s*\n',text):
            plain=re.sub(r'[#*_`>|\[\]()]','',paragraph)
            if len(_fold_text(plain))<25:continue
            if any(_numbers(plain)==nums and _similarity(plain,old)>=.9 for old,nums in paragraphs):duplicates+=1
            else:paragraphs.append((plain,_numbers(plain)))
    artifact_quality={'paragraph_count':len(paragraphs)+duplicates,'duplicate_paragraphs':duplicates,
                      'duplicate_ratio':round(duplicates/max(1,len(paragraphs)+duplicates),4),
                      'internal_review_leaks':internal,'unresolved_ir_tokens':unresolved,
                      'visual_placements':sum(len(re.findall(r'\[\[VISUAL:',c.get('markdown',''))) for c in chapters)}
    return {'source_count':len(blocks),'card_count':len(cards),'covered_cards':len(used),
            'claim_count':len(claims),'covered_claims':len(used_claims),'conflict_count':len(conflicts),
            'conflicts':conflicts,
            'missing_claims':[claim['id'] for claim in claims if claim['id'] not in used_claims and claim['importance']>1],
            'missing_cards':[c['id'] for c in cards if c['id'] not in used and c['importance']>1],
            'omitted_metadata':[{'id':c['id'],'title':c['title'],'reason':'纯元信息，保留在来源中，不单独写入正文'} for c in cards if c['id'] not in used and c['importance']==1],
            'unrepresented_sources':[b['id'] for b in blocks if b['id'] not in card_sources] if cards else [],
            'failed_chapters':[c['id'] for c in chapters if c['status']!='ready'],
            'issues':[{'chapter':c['title'],**issue} for c in chapters for issue in c.get('issues',[])],
            'uncertain_sources':[b['id'] for b in blocks if b.get('uncertain')],
            'warnings':data.get('warnings',[]),
            'artifact_quality':artifact_quality,
            'meaning':'覆盖统计只验证引用分配，不代表语义无遗漏；自动核对结果需结合来源判断。'}
