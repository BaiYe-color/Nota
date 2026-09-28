"""Compatibility entry point: both old and new launch commands use Nota's service."""
from server import app
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=7860)
