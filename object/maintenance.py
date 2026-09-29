"""Safe maintenance utilities for a single-user Nota server.

Never deletes uploads, notes, SQLite data, or saved model routes. Those files
are needed to retain source traceability and recover a personal workspace.
"""
import argparse
import shutil
import sqlite3
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path


def data_root(value: str) -> Path:
    root=Path(value).expanduser().resolve()
    if not root.is_dir(): raise SystemExit(f'数据目录不存在：{root}')
    return root


def _remove(path: Path, dry_run: bool) -> bool:
    print(('would remove' if dry_run else 'removed'),path)
    if not dry_run: path.unlink()
    return True


def prune(root: Path, cache_days: int, artifact_days: int, max_mb: int, dry_run: bool) -> int:
    """Prune only replaceable cache and downloadable export artifacts.

    First apply retention periods, then remove the oldest remaining cache files
    before export artifacts when their combined size exceeds ``max_mb``.
    """
    now=time.time(); removed=0
    for dirname,days in (('cache',cache_days),('artifacts',artifact_days)):
        folder=root/dirname
        if not folder.exists(): continue
        cutoff=now-days*86400
        for path in sorted(folder.rglob('*'),key=lambda item:len(item.parts),reverse=True):
            if not path.exists() or path.stat().st_mtime>=cutoff: continue
            if path.is_dir():
                if not any(path.iterdir()):
                    print(('would remove' if dry_run else 'removed'),path)
                    if not dry_run:path.rmdir()
            else:
                removed+=_remove(path,dry_run)

    if max_mb:
        folders=[root/'cache',root/'artifacts']
        entries=[]
        for priority,folder in enumerate(folders):
            if folder.exists():
                entries.extend((priority,path,path.stat().st_mtime,path.stat().st_size)
                               for path in folder.rglob('*') if path.is_file())
        total=sum(item[3] for item in entries); limit=max_mb*1024*1024
        # Cache is expendable before exports; within either category use LRU.
        for _,path,_,size in sorted(entries,key=lambda item:(item[0],item[2])):
            if total<=limit: break
            if _remove(path,dry_run):
                total-=size; removed+=1
    return removed


def backup(root: Path, output: Path) -> Path:
    output.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination=output/f'nota-backup-{stamp}.tar.gz'
    snapshot=output/f'nota-{stamp}.sqlite3'
    source=root/'nota.sqlite3'
    if source.exists():
        with sqlite3.connect(source) as source_db, sqlite3.connect(snapshot) as backup_db:
            source_db.backup(backup_db)
    with tarfile.open(destination,'w:gz') as archive:
        if snapshot.exists(): archive.add(snapshot,arcname='nota.sqlite3')
        for name in ('uploads','artifacts','router'):
            folder=root/name
            if folder.exists(): archive.add(folder,arcname=name)
    snapshot.unlink(missing_ok=True)
    print(destination)
    return destination


def main():
    parser=argparse.ArgumentParser(description='Nota personal-server maintenance')
    parser.add_argument('--data-dir',required=True,help='NOTA_DATA_DIR directory')
    commands=parser.add_subparsers(dest='command',required=True)
    clean=commands.add_parser('clean',help='remove old cache and export artifacts only')
    clean.add_argument('--cache-days',type=int,default=14)
    clean.add_argument('--artifact-days',type=int,default=30)
    clean.add_argument('--max-cache-artifacts-mb',type=int,default=4096,
                       help='maximum combined size for cache and exports; 0 disables quota')
    clean.add_argument('--dry-run',action='store_true')
    make_backup=commands.add_parser('backup',help='create a consistent SQLite and file backup')
    make_backup.add_argument('--output',required=True)
    args=parser.parse_args(); root=data_root(args.data_dir)
    if args.command=='clean':
        if args.cache_days<1 or args.artifact_days<1 or args.max_cache_artifacts_mb<0:
            raise SystemExit('保留天数必须至少为 1，容量上限不能小于 0')
        print(f'processed {prune(root,args.cache_days,args.artifact_days,args.max_cache_artifacts_mb,args.dry_run)} files')
    else: backup(root,Path(args.output).expanduser().resolve())


if __name__=='__main__': main()
