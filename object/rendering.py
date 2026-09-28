"""Sanitized rich preview and immutable export artifacts."""
import base64
import html
import re
import zipfile
from pathlib import Path
import bleach
import markdown

TAGS=set(bleach.sanitizer.ALLOWED_TAGS)|{'p','h1','h2','h3','h4','pre','code','blockquote','hr','br','table','thead','tbody','tr','th','td','del'}

def safe_html(text,preserve_internal_tokens=False):
    # Internal placement tokens are a transport detail. Only the structured
    # chapter renderer keeps visual tokens long enough to replace them.
    if not preserve_internal_tokens:
        text=re.sub(r'\[\[(?:VISUAL|FORMULA):[^\]]*\]\]','',text,flags=re.I)
    formulas={}
    def protect(match):
        token='NOTAMATHBLOCK'+str(len(formulas))+'TOKEN'
        formulas[token]=match.group();return token
    pieces=re.split(r'(```[\s\S]*?```|`[^`\n]*`)',text)
    for i in range(0,len(pieces),2):
        pieces[i]=re.sub(r'\$\$[\s\S]+?\$\$|\$(?!\s)[^$\n]+?\$',protect,pieces[i])
    rendered=bleach.clean(markdown.markdown(''.join(pieces),extensions=['tables','fenced_code','sane_lists']),tags=TAGS,
                        attributes={'a':['href','title'],'code':['class']},protocols=['https','http'],strip=True)
    for token,formula in formulas.items():rendered=rendered.replace(token,html.escape(formula))
    return rendered

def source_label(b):
    location=f"第 {b['page']} 页/单元" if b.get('page') is not None else f"{b['start_ms']//60000:02d}:{b['start_ms']//1000%60:02d}"
    return b['document_name']+' · '+location

def visual_caption(source,visual=None):
    lines=[re.sub(r'^#+\s*','',line).strip() for line in source.get('text','').splitlines() if line.strip()]
    caption=((visual or {}).get('title') or (lines[0] if lines else source_label(source))).replace('|',' ')
    caption=re.sub(r'^(?:Figure|Table|图|表)\s*\d*\s*[:：.-]?\s*','',caption,flags=re.I)
    caption=re.sub(r'^(?:illustrates|shows|compares)\s+','',caption,flags=re.I)
    caption=re.sub(r'\s*,?\s*(?:focusing on|measured by|by visualizing|with emphasis on)\b.*$','',caption,flags=re.I)
    caption=re.sub(r'\s+',' ',caption).strip(' .:：-')
    if len(caption)>100:
        caption=caption[:100].rsplit(' ',1)[0].rstrip(' ,，:：')
    return caption or '来源图表'

def issue_markdown(issues):
    messages=[re.sub(r'\s+',' ',i.get('message','')).strip()[:500] for i in issues]
    return '### 核对提示\n\n'+'\n'.join('- '+message for message in messages if message)

def reader_text(markdown):
    markdown=re.sub(r'(?ms)^#{1,4}\s*(?:核对提示|审校提示|修改建议)\s*$.*?(?=^#{1,4}\s|\Z)','',markdown)
    markdown=re.sub(r'(?m)^[-*]\s*(?:建议改为|应改为|请修改).*$','',markdown)
    markdown=re.sub(r'(?m)(?:待核对项|待确认项)[：:].*$','',markdown)
    markdown=re.sub(r'[（(]\s*(?:cl|co|fo|ex|vi)-[0-9a-f]+\s*[）)]','',markdown,flags=re.I)
    markdown=re.sub(r'(?<!VISUAL:)\b(?:cl|co|fo|ex|vi)-[0-9a-f]+\b','',markdown,flags=re.I)
    return re.sub(r'\n{3,}','\n\n',markdown).strip()

def chapter_markdown(chapter):
    if chapter.get('document'):
        from learning import document_markdown
        return document_markdown(chapter['document'])
    return chapter['markdown']

def reader_chapter(chapter):
    text=chapter_markdown(chapter)
    # Structured warning blocks are evidence, not internal editing comments.
    return text if chapter.get('document') else reader_text(re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text))

def chapter_preview(chapter,sources,include_images=True,visuals=()):
    from engine import digest
    from urllib.parse import quote
    text=chapter_markdown(chapter)
    rendered=safe_html(text,preserve_internal_tokens=True)
    by_id={s['id']:s for s in sources};used_assets=set()
    def image_for(source,caption):
        if not include_images:return ''
        if source.get('asset') and source.get('method') in {'layout-region','slide-region','vision-region'}:
            used_assets.add(source['asset'])
            escaped=html.escape(caption)
            return '<figure><img loading="lazy" alt="'+escaped+'" src="/api/artifacts/'+quote(source['asset'],safe='')+'"><figcaption>'+escaped+'</figcaption></figure>'
        return '<aside class="figure-unavailable">配图未能加载：'+html.escape(caption)+'</aside>'
    for block in chapter.get('document',{}).get('blocks',[]):
        if block['kind']!='figure':continue
        source=by_id.get(block['source_id'],{})
        token='[[VISUAL:vi-'+digest(block['source_id'])[:12]+']]'
        image=image_for(source,block.get('caption','来源图表'))
        rendered=rendered.replace('<p>'+token+'</p>',image).replace(token,image)
    # Legacy chapters use visual IDs directly in Markdown.  Render only the
    # selected evidence at that marker; never append every source-page asset.
    for visual in visuals:
        if visual.get('id') not in chapter.get('visual_ids',[]):continue
        source=by_id.get(visual.get('source_id'),{})
        if source.get('asset') in used_assets:continue
        token='[[VISUAL:'+visual['id']+']]'
        image=image_for(source,visual_caption(source,visual))
        rendered=rendered.replace('<p>'+token+'</p>',image).replace(token,image)
    return re.sub(r'<p>\s*\[\[(?:VISUAL|FORMULA):[^\]]*\]\]\s*</p>|\[\[(?:VISUAL|FORMULA):[^\]]*\]\]','',rendered,flags=re.I)

def note_markdown(data,include_sources=False):
    parts=['# '+data['title']]
    by_id={s['id']:s for s in data['sources']};seen=set();figure_no=0
    visual_by_id={item['id']:item for item in data.get('knowledge',{}).get('visuals',[])}
    def figure(visual):
        nonlocal figure_no
        source=by_id.get(visual.get('source_id'))
        if not source or source.get('asset_role')=='source_page' or not source.get('asset') or source['asset'] in seen:return ''
        seen.add(source['asset']);figure_no+=1;caption=visual_caption(source,visual)
        return f"![图 {figure_no}：{caption}](images/{source['asset']})"
    for ch in data['chapters']:
        body=reader_chapter(ch) if ch['status']=='ready' else '> 本节生成失败，请重试。'
        selected=[]
        if data['options']['include_images']:
            selected=[visual_by_id[vid] for vid in ch.get('visual_ids',[]) if vid in visual_by_id]
            if not selected:selected=[item for item in visual_by_id.values() if item.get('source_id') in ch.get('source_ids',[])]
        for visual in selected:
            token='[[VISUAL:'+visual['id']+']]'
            if token in body:body=body.replace(token,figure(visual),1)
        body=re.sub(r'\[\[VISUAL:[\w-]+\]\]','',body)
        # If an older draft has no claim-level placeholders, keep one best fallback
        # visual instead of dumping every candidate at the end of the chapter.
        fallback=[item for item in selected if by_id.get(item.get('source_id'),{}).get('asset') not in seen][:1]
        remaining=[] if data.get('knowledge',{}).get('strategy')=='learning_units' else [figure(item) for item in fallback]
        if remaining:body+='\n\n'+'\n\n'.join(value for value in remaining if value)
        parts+=['## '+ch['title'],body]
        if include_sources:
            labels=list(dict.fromkeys(source_label(by_id[s]) for s in ch['source_ids'] if s in by_id))
            parts+=['来源：'+'；'.join(labels)]
        if data['options']['include_images'] and not visual_by_id and data.get('knowledge',{}).get('strategy')!='learning_units':
            # Legacy notes may not have VisualEvidence records.
            legacy=[]
            for sid in ch['source_ids']:
                b=by_id.get(sid,{});asset=b.get('asset')
                if asset and asset not in seen:
                    legacy_visual={'source_id':sid,'title':visual_caption(b)};value=figure(legacy_visual)
                    if value:legacy.append(value)
            if legacy:parts+=legacy
    if not data['chapters']:
        parts+=['## 逐字稿',data.get('transcript','')]
    return '\n\n'.join(parts)+'\n'

def note_html(data,store,interactive=False,include_sources=False):
    body='<h1>'+html.escape(data['title'])+'</h1>'
    sources={b['id']:b for b in data['sources']}; seen=set();figure_no=0
    visual_by_id={item['id']:item for item in data.get('knowledge',{}).get('visuals',[])}
    def figure(visual):
        nonlocal figure_no
        source=sources.get(visual.get('source_id'));key=(source or {}).get('asset')
        if not key or source.get('asset_role')=='source_page' or key in seen:return ''
        path=store.root/'artifacts'/key
        if not path.is_file():return ''
        seen.add(key);figure_no+=1;caption=f"图 {figure_no}：{visual_caption(source,visual)}"
        return '<figure><img alt="'+html.escape(caption)+'" src="data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()+'"><figcaption>'+html.escape(caption)+'</figcaption></figure>'
    for c in data['chapters']:
        chapter=reader_chapter(c) if c['status']=='ready' else '本节生成失败，请重试。'
        selected=[]
        if data['options']['include_images']:
            selected=[visual_by_id[vid] for vid in c.get('visual_ids',[]) if vid in visual_by_id]
            if not selected:selected=[item for item in visual_by_id.values() if item.get('source_id') in c.get('source_ids',[])]
        rendered=[];cursor=0
        pattern=re.compile(r'\[\[VISUAL:([\w-]+)\]\]')
        for match in pattern.finditer(chapter):
            rendered.append(safe_html(chapter[cursor:match.start()]))
            visual=visual_by_id.get(match.group(1))
            if visual in selected:rendered.append(figure(visual))
            cursor=match.end()
        rendered.append(safe_html(chapter[cursor:]))
        fallback=[item for item in selected if sources.get(item.get('source_id'),{}).get('asset') not in seen][:1]
        remaining=[] if data.get('knowledge',{}).get('strategy')=='learning_units' else [figure(item) for item in fallback]
        body+='<h2>'+html.escape(c['title'])+'</h2>'+''.join(rendered+remaining)
        if include_sources:
            labels=list(dict.fromkeys(source_label(sources[s]) for s in c['source_ids'] if s in sources))
            body+='<p class="source">来源：'+html.escape('；'.join(labels))+'</p>'
        if data['options']['include_images'] and not visual_by_id and data.get('knowledge',{}).get('strategy')!='learning_units':
            for sid in c['source_ids']:
                source=sources.get(sid,{})
                if source.get('asset') not in seen:
                    body+=figure({'source_id':sid,'title':visual_caption(source)})
    if not data['chapters']: body+=safe_html(data.get('transcript',''))
    math_assets=standalone_math_assets() if interactive else ''
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>'+html.escape(data['title'])+'</title><style>body{font-family:sans-serif;max-width:850px;margin:40px auto;padding:24px;line-height:1.75;color:#202934}img{display:block;max-width:100%;max-height:680px;margin:auto}figure{margin:24px 0;padding:14px;background:#f6f7f4;border:1px solid #dde2d8;border-radius:8px}figcaption{margin-top:10px;color:#536057;font-size:14px}.issues{margin:16px 0;padding:10px 14px;background:#fff8e6;border-left:3px solid #a80}.issues summary{cursor:pointer;font-weight:600}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px}pre{white-space:pre-wrap;background:#f4f5f6;padding:15px}.source{font-size:12px;color:#667}blockquote{border-left:3px solid #a80;padding-left:12px}</style><body>'+body+math_assets+'</body></html>'

def export_note(store,note,format,include_sources=False):
    import uuid
    data=note['data']; name=f"{note['id']}-v{note['version']}-{uuid.uuid4().hex[:8]}"
    root=store.root/'artifacts';warnings=[]
    if format=='md':
        path=root/(name+'.md'); path.write_text(note_markdown(data,include_sources),encoding='utf-8')
    elif format=='transcript':
        path=root/(name+'-transcript.md'); path.write_text('# 逐字稿\n\n'+data.get('transcript',''),encoding='utf-8')
    elif format=='html':
        path=root/(name+'.html'); path.write_text(note_html(data,store,interactive=True,include_sources=include_sources),encoding='utf-8')
    elif format=='zip':
        path=root/(name+'.zip')
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('notes.md',note_markdown(data,include_sources))
            z.writestr('transcript.md',data.get('transcript',''))
            for key in sorted({b['asset'] for b in data['sources'] if b.get('asset')}):
                if data['options']['include_images']: z.write(root/key,'images/'+key)
    elif format=='pdf':
        import shutil, subprocess, tempfile
        from export import _find_pandoc
        pandoc=_find_pandoc()
        if pandoc and shutil.which('xelatex'):
            path=root/(name+'.pdf')
            # Pandoc ignores raw HTML/TeX from model or user markdown. Math is still supported.
            with tempfile.TemporaryDirectory(prefix='nota-pdf-',dir=root) as tmp:
                tmp=Path(tmp)
                text=note_markdown(data,include_sources)
                guard_math(text)
                for b in data['sources']:
                    if b.get('asset'):
                        text=text.replace('images/'+b['asset'],(root/b['asset']).as_posix())
                md=tmp/'notes.md';md.write_text(text,encoding='utf-8')
                result=subprocess.run([pandoc,str(md),'-f','markdown-raw_tex-raw_html-tex_math_single_backslash-tex_math_double_backslash','--pdf-engine=xelatex','--pdf-engine-opt=-no-shell-escape',
                    '-V','CJKmainfont=Microsoft YaHei','-V','papersize:a4','-V','geometry:margin=2cm',
                    '-V',r'header-includes=\usepackage{graphicx,float}\floatplacement{figure}{htbp}\setkeys{Gin}{width=0.78\linewidth,height=0.40\textheight,keepaspectratio}',
                    '-o',str(path)],cwd=tmp,capture_output=True,timeout=180)
                if result.returncode:
                    path.unlink(missing_ok=True)
                    raise RuntimeError('PDF 排版失败，请下载 HTML/Markdown；检查本机 LaTeX 与字体依赖')
            warnings=validate_pdf_artifact(path)
            return path.name,warnings
        if shutil.which('xelatex'):
            from pdf_renderer import compile_pdf
            path=root/(name+'.pdf')
            assets={'images/'+b['asset']:root/b['asset'] for b in data['sources'] if b.get('asset')}
            compile_pdf(note_markdown(data,include_sources),assets,path)
            warnings=validate_pdf_artifact(path)
            return path.name,warnings
        if '$' in note_markdown(data,include_sources):
            raise RuntimeError('数学 PDF 需要 XeLaTeX，请先下载网页或 Markdown')
        import pymupdf
        path=root/(name+'.pdf')
        story=pymupdf.Story(html=note_html(data,store,include_sources=include_sources),user_css='body{font-family:sans-serif;font-size:10pt} img{max-width:450px} pre{white-space:pre-wrap}')
        writer=pymupdf.DocumentWriter(str(path))
        page=pymupdf.paper_rect('a4'); area=page+(36,36,-36,-36)
        try:
            more=True
            while more:
                device=writer.begin_page(page)
                more,_=story.place(area)
                story.draw(device); writer.end_page()
        finally: writer.close()
        warnings=validate_pdf_artifact(path)
    else: raise ValueError('不支持的导出格式')
    return path.name,warnings

def validate_pdf_artifact(path):
    import pymupdf
    path=Path(path)
    if not path.is_file() or path.stat().st_size==0:
        raise RuntimeError('PDF 成品检查未通过：未生成可下载的文件')
    try:
        with pymupdf.open(path) as document:
            if document.page_count<1: raise RuntimeError('PDF 成品检查未通过：文件没有页面')
            # Layout heuristics are advisory: figure-heavy pages are legitimate.
            # Sample only the first and last page to avoid a second full-document pass.
            warnings=[]
            for index in sorted({0,document.page_count-1}):
                page=document[index];text=re.sub(r'\s+','',page.get_text())
                images=page.get_images(full=True);number=index+1
                if images and len(text)<80:warnings.append(f'第 {number} 页以图片或公式为主，请下载后检查版式')
                elif not text:warnings.append(f'第 {number} 页未提取到文字，请下载后检查版式')
            return warnings
    except RuntimeError: raise
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise RuntimeError('PDF 成品检查未通过：文件无法打开') from exc


def guard_math(text):
    """Restrict TeX math to display commands; never allow IO or macro execution."""
    allowed=set("frac dfrac tfrac sqrt sum prod int iint iiint oint lim limits infty alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi rho sigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega cdot times div pm mp le leq ge geq ne neq approx sim simeq equiv cong propto in notin subset subseteq supset supseteq cup cap setminus emptyset varnothing forall exists neg land lor lnot to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow mapsto lbrace rbrace langle rangle vert Vert mid colon ldots cdots vdots ddots quad qquad text textrm mathrm mathbf mathit mathbb mathcal mathsf mathtt operatorname overline underline hat bar vec dot ddot tilde widehat widetilde left right big Big bigg Bigg sin cos tan cot sec csc log ln exp min max arg det gcd bmod mod pmod begin end underset overset underbrace overbrace hline Join bowtie".split())
    for math in re.findall(r'\$+(.+?)\$+',text,re.S):
        if '^^' in math: raise ValueError('PDF 中含不支持的数学控制字符，请使用网页导出')
        for command in re.findall(r'\\([A-Za-z]+)',math):
            if command not in allowed: raise ValueError('PDF 中含不支持的数学命令：'+command)
        for env in re.findall(r'\\(?:begin|end)\s*\{([^}]+)\}',math):
            if env not in {'matrix','pmatrix','bmatrix','Bmatrix','vmatrix','Vmatrix','cases','aligned','gathered','array'}:
                raise ValueError('不支持的数学环境')


def standalone_math_assets():
    root=Path(__file__).parent/'static'/'vendor'/'katex'
    css=(root/'katex.min.css').read_text(encoding='utf-8')
    def font(match):
        relative=match.group(1).strip('"\'')
        path=(root/relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():return match.group()
        mime='font/woff2' if path.suffix=='.woff2' else ('font/woff' if path.suffix=='.woff' else 'font/ttf')
        return 'url(data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode()+')'
    css=re.sub(r'url\(([^)]+)\)',font,css)
    js=(root/'katex.min.js').read_text(encoding='utf-8')+'\n'+(root/'contrib'/'auto-render.min.js').read_text(encoding='utf-8')
    js=js.replace('</script','<\\/script')
    init="renderMathInElement(document.body,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],throwOnError:false,trust:false});"
    return '<style>'+css+'</style><script>'+js+'</script><script>'+init+'</script>'
