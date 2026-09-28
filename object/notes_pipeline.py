"""CLI using the same application service as server.py.
Usage: python object/notes_pipeline.py FILE [FILE ...] --template course
"""
import argparse
import hashlib
import shutil
from pathlib import Path
from contracts import Options
from storage import Store
from service import Service
from extraction import ACCEPTED

def main():
    parser=argparse.ArgumentParser(description='Nota 笔记生成')
    parser.add_argument('files',nargs='+',type=Path)
    parser.add_argument('--template',choices=['course','revision','meeting','summary'],default='course')
    parser.add_argument('--detail',choices=['brief','normal','detailed'],default='normal')
    parser.add_argument('--no-images',action='store_true')
    parser.add_argument('--diarization',action='store_true')
    parser.add_argument('--transcript-only',action='store_true')
    parser.add_argument('--max-pages',type=int,default=0)
    args=parser.parse_args()
    store=Store(Path(__file__).parent/'data');ids=[]
    for path in args.files:
        path=path.resolve();ext=path.suffix.lower()
        if ext not in ACCEPTED or not path.is_file():parser.error('输入文件不存在或格式不支持：'+str(path))
        with path.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
        fid=hashlib.sha256((sha+ext).encode()).hexdigest();target=store.root/'uploads'/(fid+ext)
        try:store.file(fid)
        except KeyError:
            shutil.copy2(path,target);store.register_file(fid,path.name,target,sha,path.stat().st_size)
        ids.append(fid)
    options=Options(template=args.template,detail=args.detail,include_images=not args.no_images,
                    diarization=args.diarization,output_form='transcript' if args.transcript_only else 'both',max_pages=args.max_pages)
    service=Service(store)
    jid=store.new_job('generate',{'file_ids':ids,'options':options.model_dump()})
    service.run(jid);service.pool.shutdown()
    result=store.job(jid)
    print('任务：',jid,'状态：',result['status'])
    if result['result']:print('笔记：',result['result']['note_id'],'（启动 Web 后可在最近任务打开）')
    if result['error']:print(result['error'])
    raise SystemExit(0 if result['status']=='succeeded' else 2)

if __name__=='__main__':main()
