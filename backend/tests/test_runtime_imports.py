import subprocess
import sys
from pathlib import Path

# Top-level modules only the dev group installs: the prod image has none of them (uv sync --no-dev),
# so a runtime import of one crashes there and nowhere else (v0.26.0: mypy_boto3_s3).
# shortcut: a hand-kept list, update it with [dependency-groups] dev in pyproject.toml.
DEV_ONLY = ("mypy_boto3_", "moto", "mypy", "pytest", "xdist", "ruff")

HIDE = f"""
import importlib.abc, sys
class Hide(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0].startswith({DEV_ONLY!r}):
            raise ModuleNotFoundError(name)
sys.meta_path.insert(0, Hide())
import app.main, app.worker
"""


def test_the_api_and_worker_import_without_dev_only_packages() -> None:
    result = subprocess.run(
        [sys.executable, "-c", HIDE],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
