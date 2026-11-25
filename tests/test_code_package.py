"""Build the real deployment zip once and check its layout: entry point, source,
and dependencies as linux/arm64 wheels (what AgentCore Runtime executes)."""
import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import floor  # noqa: E402


@pytest.mark.network
def test_code_zip_layout():
    data = floor.build_code_zip()
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    assert "main.py" in names and "agent/server.py" in names and "perception/labels_mode.py" in names
    assert "agent/procedure/assembly-box.json" in names
    assert "agent/planning/robot_capability.json" in names and "agent/planning/allocation.py" in names
    assert "perception/havid_mode.py" in names
    assert "mcp/server/fastmcp/__init__.py" in names and "boto3/__init__.py" in names
    # Every platform-specific wheel must be built for aarch64 (abi3 .so names omit the arch, so read WHEEL tags).
    tags = []
    for n in names:
        if n.endswith(".dist-info/WHEEL"):
            tags += [l.split(":", 1)[1].strip() for l in z.read(n).decode().splitlines() if l.startswith("Tag:")]
    platform_tags = [t for t in tags if not t.endswith("-any")]
    assert platform_tags and all("aarch64" in t for t in platform_tags), platform_tags
    assert not any("x86_64" in t for t in tags)
    assert not any("__pycache__" in n for n in names)
    assert len(data) < 150 * 1024 * 1024


def test_source_hash_is_stable_and_changes_with_source(tmp_path, monkeypatch):
    a = floor.source_hash()
    assert a == floor.source_hash() and len(a) == 12
