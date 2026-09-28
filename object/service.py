"""One application service for web, CLI, retries and revisions."""
import copy
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contracts import Options
from engine import Context,Cancelled,atomic_json,MODELS,PIPELINE_VERSION
from extraction import extract,AUDIO
from generation import generate,quality,write_chapter
from materials import resolve_materials,apply_materials,style_sample
from storage import Conflict

class Service:
    def __init__(self,store,workers=2,router=None):
        self.store=store; self.router=router
        self.submit_lock=threading.Lock()
        self.pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='nota')

    def submit(self,kind,payload):
        payload=copy.deepcopy(payload)
        if self.router and kind in ('generate','revise','regenerate') and 'model_config_id' not in payload:
            payload['model_config_id']=self.router.snapshot_id()
        with self.submit_lock:
            if self.store.active_count()>=12:
                raise ValueError('任务队列已满，请等待或取消现有任务')
            jid=self.store.new_job(kind,payload)
            self.pool.submit(self.run,jid)
            return jid

    def run(self,jid):
        ctx=None
        try:
            job=self.store.job(jid); payload=job['payload']
            config=self.router.load(payload.get('model_config_id')) if self.router else None
            ctx=Context(self.store,jid,config)
            ctx.check(); self.store.set_job(jid,'running')
            if job['kind']=='generate':
                files=[self.store.file(fid) for fid in payload['file_ids']]
                options=Options.model_validate(payload['options'])
                materials=resolve_materials(files,payload.get('materials',[]),options)
                enriched=apply_materials(files,materials)
                content_files=[file for file in enriched if file['material_role']!='style_reference']
                if not content_files: raise ValueError('至少需要一份主要内容或补充资料')
                style_parts=[]
                for file in enriched:
                    if file['material_role']=='style_reference':
                        sample=style_sample(file)
                        if sample: style_parts.append(f"风格样本《{file['name']}》：\n{sample}")
                        else: ctx.warn(file['name']+' 无法提取可用的风格文本，已跳过')
                if style_parts and len(options.style)<2000:
                    addition='\n\n'.join(style_parts)
                    options=options.model_copy(update={'style':(options.style+'\n\n'+addition).strip()[:2000]})
                sources,counts=extract(ctx,content_files,options)
                for item in materials:
                    if item['role']=='style_reference':
                        counts.append({'document':item['name'],'total':1,'processed':1,'unit':'风格参考'})
                transcript='\n\n'.join(f"[{b['start_ms']//60000:02d}:{b['start_ms']//1000%60:02d}] {b['text']}" for b in sources if b['kind']=='audio')
                if options.output_form=='transcript': cards,knowledge,chapters=[],{},[]
                else: cards,knowledge,chapters=generate(ctx,sources,options)
                if chapters and not any(ch['status']=='ready' for ch in chapters):
                    raise RuntimeError('全部章节生成失败；已保留解析和卡片缓存，可重试')
                title_file=next((file for file in content_files if file['material_role']=='primary'),content_files[0])
                data={'title':Path(title_file['name']).stem+(' · 多资料笔记' if len(content_files)>1 else ''),
                      'options':options.model_dump(),'sources':sources,'counts':counts,'cards':cards,'knowledge':knowledge,'chapters':chapters,
                      'materials':materials,'transcript':transcript,'warnings':ctx.warnings,'metrics':ctx.metrics,'pipeline_version':PIPELINE_VERSION,'models':ctx.models}
                data['quality']=quality(data)
                data['metrics_summary']={'seconds':round(time.monotonic()-ctx.started,2),
                    'input_tokens':sum(m.get('input_tokens',0) for m in ctx.metrics),
                    'output_tokens':sum(m.get('output_tokens',0) for m in ctx.metrics),
                    'cache_hits':sum(bool(m.get('cache_hit')) for m in ctx.metrics),
                    'model_calls':sum('input_tokens' in m for m in ctx.metrics),
                    'models_used':sorted({m['model'] for m in ctx.metrics if 'input_tokens' in m})}
                ctx.check()
                nid=self.store.create_note(jid,data)
                status='partial' if ctx.warnings or data['quality']['failed_chapters'] or data['quality']['issues'] or data['quality']['uncertain_sources'] else 'succeeded'
                result={'note_id':nid,'version':1,'quality':data['quality']}
            elif job['kind']=='revise':
                note=self.store.note(payload['note_id']); expected=payload['expected_version']
                if note['version']!=expected: raise Conflict('修订期间笔记已改变，请刷新后重试')
                data=copy.deepcopy(note['data']); options=Options.model_validate(data['options'])
                chapter=next(c for c in data['chapters'] if c['id']==payload['chapter_id'])
                ctx.progress(.2,'结合原始资料修订所选章节')
                if data.get('knowledge',{}).get('strategy')=='learning_units':
                    from learning import write_unit
                    revised=write_unit(ctx,chapter,data['sources'],options,payload['instruction'],chapter['markdown'])
                    retained=[v for v in data['knowledge']['visuals'] if v['id'] not in chapter.get('visual_ids',[])]
                    data['knowledge']['visuals']=retained+revised['visuals']
                else:
                    revised=write_chapter(ctx,chapter,data['cards'],data['sources'],options,payload['instruction'],chapter['markdown'],chapter.get('conversation',[]),knowledge=data.get('knowledge'))
                data['chapters']=[revised if c['id']==chapter['id'] else c for c in data['chapters']]
                data['quality']=quality(data); ctx.check()
                version=self.store.save_revision(note['id'],expected,data,'对话修订：'+payload['instruction'][:80])
                result={'note_id':note['id'],'version':version,'quality':data['quality']}
                status='partial' if revised['issues'] else 'succeeded'
            elif job['kind']=='regenerate':
                note=self.store.note(payload['note_id']); expected=payload['expected_version']
                if note['version']!=expected: raise Conflict('重新生成前笔记已变化，请刷新后重试')
                data=copy.deepcopy(note['data']); options=Options.model_validate(data['options'])
                if options.output_form=='transcript':
                    raise ValueError('当前产出仅为逐字稿，不能生成笔记正文')
                ctx.progress(.15,'根据校对后的逐字稿重建笔记')
                cards,knowledge,chapters=generate(ctx,data['sources'],options)
                if chapters and not any(ch['status']=='ready' for ch in chapters):
                    raise RuntimeError('校对稿未能生成任何笔记章节')
                data.update(cards=cards,knowledge=knowledge,chapters=chapters,warnings=ctx.warnings,
                            metrics=ctx.metrics,pipeline_version=PIPELINE_VERSION,models=ctx.models)
                data['quality']=quality(data); ctx.check()
                version=self.store.save_revision(note['id'],expected,data,'根据校对逐字稿重新生成笔记')
                result={'note_id':note['id'],'version':version,'quality':data['quality']}
                status='partial' if ctx.warnings or data['quality']['issues'] else 'succeeded'
            elif job['kind']=='export':
                from rendering import export_note
                ctx.progress(.1,'导出固定版本')
                note=self.store.note(payload['note_id'],payload['version'])
                artifact,warnings=export_note(self.store,note,payload['format'],payload.get('include_sources',False))
                ctx.check(); result={'artifact':artifact,'note_id':note['id'],'version':note['version']}; status='succeeded'
                if warnings:
                    result['warnings']=warnings
                    status='partial'
            else: raise ValueError('未知任务类型')
            ctx.progress(1,'完成' if status=='succeeded' else '已生成，有待核对内容')
            self.store.set_job(jid,status,result=result)
        except Cancelled as exc:
            self.store.set_job(jid,'cancelled',error=str(exc))
        except Conflict as exc:
            self.store.set_job(jid,'failed',error=str(exc))
        except Exception as exc:
            message=str(exc)[:240] if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__+'：处理失败，请检查配置后重试'
            self.store.set_job(jid,'failed',error=message)
        finally:
            if ctx is not None:
                report={'seconds':round(time.monotonic()-ctx.started,3),'steps':ctx.metrics,'warnings':ctx.warnings}
                atomic_json(self.store.root/'artifacts'/f'{jid}-metrics.json',report)
                self.store.emit(jid,'metrics',**report)

    def retry(self,jid,skip_review_questions=False,degraded_mode=False):
        job=self.store.job(jid)
        if job['status'] not in ('failed','partial','cancelled','interrupted'):
            raise ValueError('只能重试失败、中断、取消或部分完成的任务')
        payload=copy.deepcopy(job['payload'])
        if skip_review_questions:
            if job['kind']!='generate': raise ValueError('跳过复习问题只适用于笔记生成任务')
            payload.setdefault('options',{})['include_review_questions']=False
        if degraded_mode:
            if job['kind']!='generate': raise ValueError('容错生成只适用于笔记生成任务')
            options=payload.setdefault('options',{})
            options['degraded_mode']=True
            options['include_review_questions']=False
            options['include_images']=False
        return self.submit(job['kind'],payload)

    def edit_transcript(self,nid,expected,segments):
        note=self.store.note(nid)
        if note['version']!=expected: raise Conflict('校对期间笔记已变化，请刷新后重试')
        data=copy.deepcopy(note['data']); by_id={item['id']:item for item in data['sources'] if item['kind']=='audio'}
        for item in segments:
            source=by_id.get(item['id'])
            if source is None: raise ValueError('校对内容包含未知录音片段')
            source['text']=item['text'].strip()
        if not by_id: raise ValueError('这份笔记没有可校对的录音片段')
        data['transcript']='\n\n'.join(f"[{s['start_ms']//60000:02d}:{s['start_ms']//1000%60:02d}] {s['text']}" for s in data['sources'] if s['kind']=='audio')
        data['transcript_corrected']=True
        data.setdefault('warnings',[]).append('逐字稿已校对；请使用“根据校对稿重新生成笔记”更新正文。')
        data['quality']=quality(data)
        return self.store.save_revision(nid,expected,data,'校对录音转写')

    def regenerate_from_transcript(self,nid,expected):
        return self.submit('regenerate',{'note_id':nid,'expected_version':expected})

    def edit(self,nid,cid,expected,markdown):
        note=self.store.note(nid)
        data=copy.deepcopy(note['data']); chapter=next((c for c in data['chapters'] if c['id']==cid),None)
        if chapter is None: raise KeyError('章节不存在')
        if not markdown.strip(): raise ValueError('正文不能为空')
        chapter.pop('document',None)
        chapter.update(markdown=markdown,questions=[],edited=True,status='ready',issues=[{'message':'本节经过手动编辑，尚未重新自动核对','source_ids':chapter['source_ids']}])
        data['quality']=quality(data)
        return self.store.save_revision(nid,expected,data,'手动编辑：'+chapter['title'])

    def restore(self,nid,expected,version):
        old=self.store.note(nid,version)
        return self.store.save_revision(nid,expected,old['data'],f'恢复版本 {version}')
