from pathlib import Path
import uuid

def pytest_configure(config):
    # Each run uses a fresh workspace directory, avoiding global temp permissions.
    if not config.option.basetemp:
        config.option.basetemp=str(Path(__file__).resolve().parents[1]/'work'/('pytest-'+uuid.uuid4().hex))
