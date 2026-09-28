"""Read-only environment check. No model calls or credential output."""
import importlib.util
import os
import shutil
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent/'.env')
if __name__=='__main__':
    for name in ('fastapi','uvicorn','openai','pymupdf','pptx','docx','requests','markdown','bleach'):
        print(name, 'OK' if importlib.util.find_spec(name) else 'MISSING')
    for name in ('OPENAI_API_KEY','DEEPSEEK_API_KEY','DASHSCOPE_API_KEY'):
        print(name,'configured' if os.getenv(name) else 'missing')
    from export import _find_pandoc
    print('PDF engine', ('pandoc + xelatex' if _find_pandoc() else 'xelatex (built-in renderer)') if shutil.which('xelatex') else 'basic fallback only')
