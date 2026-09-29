"""Nota local API: persistent jobs, sources, notes and revision history."""
import asyncio
import difflib
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path,PureWindowsPath
from urllib.parse import urlsplit
from fastapi import FastAPI,File,UploadFile,HTTPException,Request
from fastapi.responses import FileResponse,StreamingResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from contracts import GenerateRequest,Options,EditRequest,ReviseRequest,RestoreRequest,RetryRequest,TranscriptEditRequest,RouterSettingsRequest
from storage import Store,Conflict,TERMINAL
from service import Service
from extraction import ACCEPTED,AUDIO
from rendering import safe_html,source_label,chapter_preview
from engine import PIPELINE_VERSION
from router import Router

ROOT=Path(__file__).parent

def _allowed_hosts():
    """Read trusted Host headers without weakening the localhost default."""
    configured=os.getenv('NOTA_ALLOWED_HOSTS','').strip()
    if configured:
        return [host.strip() for host in configured.split(',') if host.strip()]
    return ['127.0.0.1','localhost','testserver']

def create_app(data_root=None):
    store=Store(data_root or os.getenv('NOTA_DATA_DIR',ROOT/'data'))
    router=Router(store.root)
    service=Service(store,workers=max(1,int(os.getenv('NOTA_WORKERS','2')),),router=router)
    @asynccontextmanager
    async def lifespan(app):
        loop=asyncio.get_running_loop();previous_handler=loop.get_exception_handler()
        def local_disconnect_handler(loop,context):
            if isinstance(context.get('exception'),(ConnectionResetError,BrokenPipeError)): return
            if previous_handler: previous_handler(loop,context)
            else: loop.default_exception_handler(context)
        loop.set_exception_handler(local_disconnect_handler)
        try:
            with store.runtime_lock():
                store.recover()
                yield
                for job in store.jobs():
                    if job['status'] in ('queued','running'): store.cancel(job['id'])
                service.pool.shutdown(wait=False,cancel_futures=True)
        finally: loop.set_exception_handler(previous_handler)
    app=FastAPI(title='Nota',lifespan=lifespan)
    app.state.store=store; app.state.service=service; app.state.router=router
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=_allowed_hosts())

    @app.middleware('http')
    async def same_origin(request,call_next):
        origin=request.headers.get('origin')
        if request.method not in ('GET','HEAD','OPTIONS') and origin and urlsplit(origin).netloc!=request.url.netloc:
            return JSONResponse({'detail':'不允许跨站修改请求'},status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.exception_handler(KeyError)
    async def missing(request,exc): return JSONResponse({'detail':str(exc)},status_code=404)
    @app.exception_handler(ValueError)
    async def invalid(request,exc): return JSONResponse({'detail':str(exc)},status_code=400)
    @app.exception_handler(Conflict)
    async def conflict(request,exc): return JSONResponse({'detail':str(exc)},status_code=409)

    @app.get('/api/health')
    def health():
        try:
            probe=store.root/'.healthcheck'
            probe.write_text('ok',encoding='ascii'); probe.unlink(missing_ok=True)
            storage='ok'
        except OSError:
            storage='unavailable'
        return {'status':'ok' if storage=='ok' else 'degraded','version':PIPELINE_VERSION,
                'storage':storage,'configured':router.public()}

    @app.post('/api/upload')
    async def upload(files:list[UploadFile]=File(...)):
        if not 1<=len(files)<=30: raise HTTPException(400,'每次上传1到30个文件')
        for f in files:
            name=f.filename or ''
            if not name or PureWindowsPath(name).name!=name or '/' in name or ':' in name or '\\' in name:
                raise HTTPException(400,'文件名无效')
            if Path(name).suffix.lower() not in ACCEPTED: raise HTTPException(400,'不支持的文件类型：'+Path(name).suffix)
        uploaded=[]; total=0; max_size=int(os.getenv('NOTA_MAX_UPLOAD_MB','200'))*1024*1024
        for f in files:
            tmp=store.root/'uploads'/(uuid.uuid4().hex+'.part')
            h=hashlib.sha256(); size=0
            try:
                with tmp.open('wb') as out:
                    while chunk:=await f.read(1024*1024):
                        size+=len(chunk);total+=len(chunk)
                        if size>max_size or total>max_size*2: raise HTTPException(413,'上传超过大小限制')
                        h.update(chunk);out.write(chunk)
                if not size: raise HTTPException(400,'不能上传空文件')
                sha=h.hexdigest(); ext=Path(f.filename).suffix.lower()
                fid=hashlib.sha256((sha+ext).encode()).hexdigest()
                target=store.root/'uploads'/(fid+ext)
                try: saved=store.file(fid)
                except KeyError:
                    os.replace(tmp,target)
                    try: store.register_file(fid,f.filename,target,sha,size)
                    except Exception:
                        # Concurrent identical uploads can race at the unique index.
                        store.file(fid)
                    saved=store.file(fid)
                uploaded.append({'id':fid,'name':saved['name'],'size':size})
            finally:
                tmp.unlink(missing_ok=True); await f.close()
        return {'files':uploaded}

    @app.post('/api/generate')
    def generate(request:GenerateRequest):
        if len(set(request.file_ids))!=len(request.file_ids): raise ValueError('请移除重复文件')
        files=[store.file(fid) for fid in request.file_ids]
        roles={item.file_id:item.role for item in request.materials}
        content_files=[file for file in files if roles.get(file['id'],'primary')!='style_reference']
        if request.options.output_form=='transcript' and any(Path(f['path']).suffix not in AUDIO for f in content_files):
            raise ValueError('只要逐字稿仅支持纯音频输入')
        if any(Path(f['path']).suffix.lower() in AUDIO for f in content_files) and not router.has_asr():
            raise ValueError('检测到录音材料，但尚未配置 ASR。请打开“模型设置”填写 DashScope ASR 的 API Key。')
        return {'job_id':service.submit('generate',request.model_dump())}

    @app.get('/api/jobs')
    def jobs(): return store.jobs()
    @app.get('/api/jobs/{jid}')
    def job(jid:str): return store.job(jid)
    @app.post('/api/jobs/{jid}/cancel')
    def cancel(jid:str): store.job(jid);store.cancel(jid);return {'ok':True}
    @app.post('/api/jobs/{jid}/retry')
    def retry(jid:str,request:RetryRequest=RetryRequest()): return {'job_id':service.retry(jid,request.skip_review_questions,request.degraded_mode)}

    @app.get('/api/stream/{jid}')
    async def stream(jid:str,request:Request,after:int=0):
        store.job(jid)
        try: after=max(after,int(request.headers.get('last-event-id','0')))
        except ValueError: raise HTTPException(400,'事件游标无效')
        async def events():
            cursor=after; idle=0
            while not await request.is_disconnected():
                entries=await asyncio.to_thread(store.events,jid,cursor)
                for e in entries:
                    cursor=e['seq']
                    yield f"id: {cursor}\ndata: {json.dumps(e,ensure_ascii=False)}\n\n"
                current=await asyncio.to_thread(store.job,jid)
                if current['status'] in TERMINAL and not entries:
                    yield 'event: end\ndata: {}\n\n';return
                idle+=1
                if idle%40==0: yield ': heartbeat\n\n'
                await asyncio.sleep(.25)
        return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.get('/api/files/{fid}')
    def original(fid:str):
        f=store.file(fid); path=Path(f['path']).resolve()
        if not path.is_relative_to(store.root/'uploads'): raise HTTPException(400,'文件路径无效')
        return FileResponse(path,filename=f['name'],content_disposition_type='inline')

    @app.get('/api/artifacts/{name}')
    def artifact(name:str):
        if Path(name).name!=name or PureWindowsPath(name).name!=name: raise HTTPException(400,'文件名无效')
        path=(store.root/'artifacts'/name).resolve()
        if not path.is_relative_to(store.root/'artifacts') or not path.is_file(): raise HTTPException(404,'产物不存在')
        return FileResponse(path,filename=name,content_disposition_type='inline' if path.suffix=='.png' else 'attachment')

    @app.get('/api/notes/{nid}')
    def note(nid:str,version:int|None=None):
        note=store.note(nid,version)
        for ch in note['data']['chapters']:
            ch['html']=chapter_preview(ch,note['data']['sources'],note['data']['options']['include_images'],
                                       note['data'].get('knowledge',{}).get('visuals',[]))
        for b in note['data']['sources']: b['label']=source_label(b)
        return note

    @app.put('/api/notes/{nid}/chapters/{cid}')
    def edit(nid:str,cid:str,request:EditRequest):
        return {'version':service.edit(nid,cid,request.expected_version,request.markdown)}

    @app.post('/api/notes/{nid}/chapters/{cid}/revise')
    def revise(nid:str,cid:str,request:ReviseRequest):
        note=store.note(nid)
        if note['version']!=request.expected_version: raise Conflict('版本已改变，请刷新')
        if not any(ch['id']==cid for ch in note['data']['chapters']): raise KeyError('章节不存在')
        return {'job_id':service.submit('revise',{'note_id':nid,'chapter_id':cid,**request.model_dump()})}

    @app.get('/api/notes/{nid}/history')
    def history(nid:str): store.note(nid);return store.history(nid)
    @app.get('/api/notes/{nid}/diff')
    def diff(nid:str,before:int,after:int):
        from rendering import note_markdown
        a=note_markdown(store.note(nid,before)['data']).splitlines()
        b=note_markdown(store.note(nid,after)['data']).splitlines()
        return {'diff':'\n'.join(difflib.unified_diff(a,b,fromfile=f'v{before}',tofile=f'v{after}',lineterm=''))}
    @app.post('/api/notes/{nid}/restore')
    def restore(nid:str,request:RestoreRequest): return {'version':service.restore(nid,request.expected_version,request.version)}

    @app.put('/api/notes/{nid}/transcript')
    def transcript_edit(nid:str,request:TranscriptEditRequest):
        return {'version':service.edit_transcript(nid,request.expected_version,[item.model_dump() for item in request.segments])}

    @app.post('/api/notes/{nid}/transcript/regenerate')
    def transcript_regenerate(nid:str,expected_version:int):
        return {'job_id':service.regenerate_from_transcript(nid,expected_version)}
    @app.post('/api/notes/{nid}/export')
    def export(nid:str,format:str='zip',version:int|None=None,include_sources:bool=False):
        if format not in ('zip','md','html','pdf','transcript'): raise ValueError('导出格式无效')
        note=store.note(nid,version)
        return {'job_id':service.submit('export',{'note_id':nid,'version':note['version'],'format':format,'include_sources':include_sources})}

    @app.get('/api/preferences')
    def preferences(): return store.preferences()
    @app.put('/api/preferences')
    def preferences_save(options:Options): return store.preferences(options.model_dump())

    @app.get('/api/model-settings')
    def model_settings(): return router.public()
    @app.put('/api/model-settings')
    def model_settings_save(settings:RouterSettingsRequest):
        return router.save(settings.model_dump())

    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    @app.get('/')
    def index(): return FileResponse(ROOT/'static'/'index.html',headers={'Cache-Control':'no-store'})
    return app

app=create_app()
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host=os.getenv('NOTA_HOST','127.0.0.1'),port=int(os.getenv('NOTA_PORT','7860')),
                proxy_headers=os.getenv('NOTA_PROXY_HEADERS','0')=='1')
