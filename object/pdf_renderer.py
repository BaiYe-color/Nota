"""Small deterministic Markdown-to-TeX renderer used when Pandoc is unavailable.
Only application-generated TeX plus allowlisted math reaches XeLaTeX.
"""
import html
import os
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
import markdown

def cjk_font():
    return os.getenv('NOTA_CJK_FONT') or ('Microsoft YaHei' if os.name == 'nt' else 'Noto Sans CJK SC')

class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.root={'tag':'root','children':[],'attrs':{}};self.stack=[self.root]
    def handle_starttag(self,tag,attrs):
        node={'tag':tag,'attrs':dict(attrs),'children':[]};self.stack[-1]['children'].append(node)
        if tag not in {'img','br','hr','input','meta','link'}:self.stack.append(node)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i]['tag']==tag:self.stack=self.stack[:i];break
    def handle_data(self,data):self.stack[-1]['children'].append(data)

def escape(text):
    special={'\\':r'\textbackslash{}','{':r'\{','}':r'\}','$':r'\$','&':r'\&','#':r'\#','%':r'\%','_':r'\_','~':r'\textasciitilde{}','^':r'\textasciicircum{}'}
    return ''.join(special.get(c,c) for c in text)

def latex_document(text,assets):
    from rendering import guard_math
    guard_math(text)
    math={}
    def protect(m):
        key='NOTAMATHTOKEN'+str(len(math))+'END';math[key]=m.group();return key
    protected=re.sub(r'\$\$[\s\S]+?\$\$|\$(?!\s)[^$\n]+?\$',protect,text)
    tree=Tree();tree.feed(markdown.markdown(protected,extensions=['tables','fenced_code','sane_lists']))
    def render(node):
        if isinstance(node,str):
            parts=re.split(r'(NOTAMATHTOKEN\d+END)',node)
            return ''.join(math[p] if p in math else escape(p) for p in parts)
        tag=node['tag'];children=node['children'];body=''.join(render(c) for c in children)
        if tag in ('root','thead','tbody'):return body
        if tag in ('h1','h2','h3','h4'):
            command={'h1':'section','h2':'subsection','h3':'subsubsection','h4':'paragraph'}[tag]
            return '\n\\'+command+'*{'+body+'}\n'
        if tag=='p':return '\n\n'+body+'\n\n'
        if tag in ('strong','b'):return r'\textbf{'+body+'}'
        if tag in ('em','i'):return r'\emph{'+body+'}'
        if tag=='code':return r'{\ttfamily '+body+'}'
        if tag=='pre':return '\n\\begin{quote}\\small '+body.replace('\n','\\\\\n')+'\n\\end{quote}\n'
        if tag in ('ul','ol'):
            env='itemize' if tag=='ul' else 'enumerate';return '\n\\begin{'+env+'}\n'+body+'\n\\end{'+env+'}\n'
        if tag=='li':return '\n\\item '+body
        if tag=='blockquote':return '\n\\begin{quote}'+body+'\\end{quote}\n'
        if tag=='br':return '\\\\\n'
        if tag=='hr':return '\n\\noindent\\rule{\\linewidth}{0.4pt}\n'
        if tag=='a':return body # Offline export preserves link text without introducing external resources.
        if tag=='img':
            path=assets.get(node['attrs'].get('src'))
            if not path:return ''
            return '\n\\begin{center}\\includegraphics[width=.78\\linewidth,height=.40\\textheight,keepaspectratio]{\\detokenize{'+Path(path).as_posix()+'}}\\end{center}\n'
        if tag=='table':
            rows=[]
            def visit(n):
                if isinstance(n,str):return
                if n['tag']=='tr':rows.append(n)
                else:
                    for child in n['children']:visit(child)
            visit(node)
            cols=max((len([c for c in r['children'] if isinstance(c,dict) and c['tag'] in ('td','th')]) for r in rows),default=1)
            spec='|'+'|'.join('p{'+str(round(.88/cols,3))+r'\linewidth}' for _ in range(cols))+'|'
            out='\n\\begin{longtable}{'+spec+'}\\hline\n'
            for row in rows:
                cells=[render(c) for c in row['children'] if isinstance(c,dict) and c['tag'] in ('td','th')]
                out+=' & '.join(cells)+r' \\ \hline'+'\n'
            return out+'\\end{longtable}\n'
        if tag in ('td','th'):return body.strip()
        return body
    preamble=r'''\documentclass[11pt]{article}
\usepackage[margin=2cm]{geometry}
\usepackage{fontspec,xeCJK,amsmath,amssymb,graphicx,longtable,array}
\setmainfont{TeX Gyre Termes}
\setCJKmainfont{'''+cjk_font()+r'''}
\setCJKmonofont{'''+cjk_font()+r'''}
\setlength{\parindent}{0pt}
\setlength{\parskip}{5pt}
\sloppy
\begin{document}
'''
    return preamble+render(tree.root)+'\n\\end{document}\n'

def compile_pdf(text,assets,path):
    executable=shutil.which('xelatex')
    if not executable:raise RuntimeError('数学 PDF 需要 XeLaTeX，请先下载网页或 Markdown')
    with tempfile.TemporaryDirectory(prefix='nota-tex-',dir=path.parent) as scratch:
        scratch=Path(scratch);(scratch/'note.tex').write_text(latex_document(text,assets),encoding='utf-8')
        result=subprocess.run([executable,'-no-shell-escape','-interaction=nonstopmode','-halt-on-error','note.tex'],cwd=scratch,capture_output=True,timeout=180)
        if result.returncode:
            # Keep a diagnostic beside the export, without leaking it over the API.
            (path.with_suffix('.log')).write_bytes(result.stdout[-12000:])
            raise RuntimeError('PDF 排版失败；请查看对应导出日志或使用完整网页导出')
        shutil.copyfile(scratch/'note.pdf',path)
