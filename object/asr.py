"""Paraformer file transcription with official temporary OSS upload protocol.
Reference: https://help.aliyun.com/zh/model-studio/get-temporary-file-url/
"""
import os
import re
import time
import uuid
from pathlib import Path
from typing import TypedDict
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent/'.env')
PARAFORMER_MODEL=os.getenv('PARAFORMER_MODEL','paraformer-v2')
BASE=os.getenv('DASHSCOPE_BASE_URL','https://dashscope.aliyuncs.com/api/v1').rstrip('/')

class TranscriptSegment(TypedDict):
    start_ms:int
    end_ms:int
    text:str
    speaker_id:int | None

def _api_key(config=None):
    key=(config or {}).get('api_key') or os.getenv('DASHSCOPE_API_KEY')
    if not key: raise RuntimeError('DASHSCOPE_API_KEY 未配置')
    return key

def _response(resp,label):
    if not resp.ok:
        # Never include signed upload URLs, authorization values or full provider bodies.
        try: code=resp.json().get('code','')
        except ValueError: code=''
        raise RuntimeError(f'{label}失败 HTTP {resp.status_code} {code}')
    return resp.json()

def upload_file_and_get_url(local_path,progress_cb=None,config=None):
    path=Path(local_path)
    if progress_cb: progress_cb('正在上传录音')
    base=(config or {}).get('base_url') or BASE
    policy=_response(requests.get(base+'/uploads',headers={'Authorization':'Bearer '+_api_key(config)},
                                 params={'action':'getPolicy','model':(config or {}).get('model') or PARAFORMER_MODEL},timeout=30),'获取上传凭证')['data']
    key=policy['upload_dir']+'/'+uuid.uuid4().hex+path.suffix.lower()
    fields={'OSSAccessKeyId':(None,policy['oss_access_key_id']),'Signature':(None,policy['signature']),
            'policy':(None,policy['policy']),'x-oss-object-acl':(None,policy['x_oss_object_acl']),
            'x-oss-forbid-overwrite':(None,policy['x_oss_forbid_overwrite']),'key':(None,key),'success_action_status':(None,'200')}
    with path.open('rb') as f:
        fields['file']=(path.name,f)
        resp=requests.post(policy['upload_host'],files=fields,timeout=180)
    if not resp.ok: raise RuntimeError(f'录音上传失败 HTTP {resp.status_code}')
    return 'oss://'+key

def _normalize_paraformer_result(data):
    segments=[]
    for transcript in data.get('transcripts',[]):
        for sent in transcript.get('sentences',[]):
            text=str(sent.get('text','')).strip()
            if not text: continue
            start,end=int(sent.get('begin_time',0)),int(sent.get('end_time',0))
            if start<0 or end<start: raise ValueError('ASR 时间范围无效')
            segments.append({'start_ms':start,'end_ms':end,'text':text,'speaker_id':sent.get('speaker_id')})
    return sorted(segments,key=lambda s:(s['start_ms'],s['end_ms']))

def semantic_segments(segments,max_gap_ms=1600,max_duration_ms=90000,max_chars=700):
    """Merge ASR sentences into editable speech turns and topic-sized chunks."""
    groups=[];current=[];start=end=size=0;speaker=None
    boundary=re.compile(r'^(?:\u9996\u5148|\u5176\u6b21|\u7136\u540e|\u63a5\u4e0b\u6765|\u4e0b\u9762|\u6700\u540e|\u603b\u7ed3(?:\u4e00\u4e0b)?|\u6362\u53e5\u8bdd\u8bf4|\u53e6\u4e00\u65b9\u9762|\u7b2c\u4e00|\u7b2c\u4e8c|\u7b2c\u4e09|\u6240\u4ee5|\u56e0\u6b64|\u90a3\u6211\u4eec|first|next|finally|in summary)',re.I)
    def flush():
        nonlocal current,size
        if not current:return
        groups.append({'start_ms':current[0]['start_ms'],'end_ms':current[-1]['end_ms'],
                       'speaker_id':current[0].get('speaker_id'),
                       'text':' '.join(item['text'] for item in current).strip(),
                       'sentences':[dict(item) for item in current]})
        current=[];size=0
    for item in segments:
        text=item['text'].strip()
        if not text:continue
        new=not current or item.get('speaker_id')!=speaker or item['start_ms']-end>max_gap_ms
        if current and (item['end_ms']-start>max_duration_ms or size+len(text)>max_chars):new=True
        # A spoken transition is meaningful once a turn contains a complete thought.
        if current and boundary.match(text) and size>40:new=True
        if new:flush();start=item['start_ms'];speaker=item.get('speaker_id')
        current.append(item);end=item['end_ms'];size+=len(text)+1
    flush()
    return groups

def transcribe_url(audio_url,*,enable_diarization=False,poll_interval=3,max_wait_seconds=1800,progress_cb=None,check_cancel=None,task_id=None,on_task=None,config=None):
    check=check_cancel or (lambda:None)
    route=config or {}; base=route.get('base_url') or BASE; model=route.get('model') or PARAFORMER_MODEL
    headers={'Authorization':'Bearer '+_api_key(route),'X-DashScope-Async':'enable','X-DashScope-OssResourceResolve':'enable'}
    check()
    if not task_id:
        submitted=_response(requests.post(base+'/services/audio/asr/transcription',headers=headers,
             json={'model':model,'input':{'file_urls':[audio_url]},'parameters':{'diarization_enabled':enable_diarization}},timeout=30),'提交转录')
        task_id=submitted.get('output',{}).get('task_id')
        if not task_id: raise RuntimeError('转录服务未返回任务编号')
        if on_task: on_task(task_id)
    deadline=time.monotonic()+max_wait_seconds
    while time.monotonic()<deadline:
        check()
        task=_response(requests.get(base+'/tasks/'+task_id,headers={'Authorization':'Bearer '+_api_key(config)},timeout=30),'查询转录')['output']
        status=task.get('task_status')
        if status=='SUCCEEDED': break
        if status in ('FAILED','CANCELED','UNKNOWN'): raise RuntimeError('转录任务失败：'+str(task.get('code',status)))
        if progress_cb: progress_cb('语音识别中：'+str(status))
        until=time.monotonic()+poll_interval
        while time.monotonic()<until:
            check(); time.sleep(min(.25,max(0,until-time.monotonic())))
    else: raise TimeoutError('转录等待超时，可重试查询已提交任务')
    results=task.get('results') or []
    if not results or not results[0].get('transcription_url'):
        raise RuntimeError('转录任务无有效结果：'+str(results[0].get('code','') if results else 'empty'))
    check()
    data=_response(requests.get(results[0]['transcription_url'],timeout=60),'下载转录')
    segments=_normalize_paraformer_result(data)
    if not segments: raise RuntimeError('录音中未识别到有效语音')
    return segments

def transcribe_local_file(local_path,*,enable_diarization=False,progress_cb=None,check_cancel=None,task_id=None,on_task=None,config=None):
    if check_cancel: check_cancel()
    url='' if task_id else upload_file_and_get_url(local_path,progress_cb,config)
    return transcribe_url(url,enable_diarization=enable_diarization,progress_cb=progress_cb,
                          check_cancel=check_cancel,task_id=task_id,on_task=on_task,config=config)

