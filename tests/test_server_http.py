"""Start the MCP server as a subprocess through main.py (the AgentCore entry point) and
exercise every tool over streamable HTTP with the MCP client."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    data_dir = tempfile.mkdtemp()
    # Pre-load the sample manifest so tools have data; analyze_pending is called over MCP below.
    sys.path.insert(0, str(ROOT))
    from agent import pipeline
    from agent.store import Store
    from perception import labels_mode
    acts, _ = labels_mode.read_manifest(ROOT / "data" / "sample_manifest.csv")
    pipeline.ingest_actions(Store(data_dir=data_dir), acts)

    port = _free_port()
    env = dict(os.environ, DATA_DIR=data_dir, PORT=str(port), LLM_ENABLED="false", PYTHONPATH=str(ROOT))
    env.pop("FINDINGS_BUCKET", None)
    # Literal arguments, no shell. executable= runs this test's own interpreter, and its folder
    # goes first on PATH so a virtual environment is still detected.
    env["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), env.get("PATH", "")])
    proc = subprocess.Popen(["python", "main.py"], executable=sys.executable, cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        if proc.poll() is not None:
            raise RuntimeError(proc.stdout.read())
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("server did not start")
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _call(url, name, args=None):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(name, args or {})
            assert not res.isError, res.content
            return json.loads(res.content[0].text)


async def _tools(url):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return [t.name for t in (await s.list_tools()).tools]


def test_tool_list(server):
    names = asyncio.run(_tools(server))
    assert set(names) == {"list_filters", "get_shift_summary", "list_deviations", "get_worker_scorecard",
                          "get_station_trend", "get_clip_evidence", "get_procedure", "analyze_pending",
                          "get_step_profile", "compare_task_allocation"}


def test_analyze_then_query(server):
    first = asyncio.run(_call(server, "analyze_pending"))
    assert first["units_analyzed"] == 1792 and first["deviations"] > 0
    assert asyncio.run(_call(server, "analyze_pending"))["units_analyzed"] == 0

    filters = asyncio.run(_call(server, "list_filters"))
    assert filters["latest_date"] == "2026-11-14" and filters["stations"] == ["ST-01", "ST-02", "ST-03", "ST-04"]

    summary = asyncio.run(_call(server, "get_shift_summary", {"date": "", "shift": ""}))
    assert summary["date"] == "2026-11-14" and summary["units"] > 0
    assert summary["by_station"] and summary["narrative"]
    assert summary["narrative_source"] == "rules"  # LLM disabled in this test

    devs = asyncio.run(_call(server, "list_deviations", {"severity": "high", "limit": 5}))
    assert devs["returned"] <= 5 and all(d["severity"] == "high" for d in devs["deviations"])

    ev = asyncio.run(_call(server, "get_clip_evidence", {"deviation_id": devs["deviations"][0]["deviation_id"]}))
    assert ev["clip_available"] is False and ev["clip_ref"].startswith("clips/")

    sc = asyncio.run(_call(server, "get_worker_scorecard", {"worker": "W-104"}))
    assert sc["units"] > 0 and sc["fatigue_indicators"] >= 10

    tr = asyncio.run(_call(server, "get_station_trend", {"station": "ST-02", "days": 14}))
    assert tr["days"] == 14 and all("compliance_pct" in p for p in tr["series"])

    proc = asyncio.run(_call(server, "get_procedure"))
    assert proc["procedure_id"] == "assembly-box-v1" and len(proc["steps"]) == 9

    prof = asyncio.run(_call(server, "get_step_profile"))
    assert len(prof["steps"]) == 9 and prof["robot_data_is_placeholder"] is True
    assert prof["steps"][3]["robot_cycle_seconds"] == 28.0 and prof["steps"][0]["robot_cycle_seconds"] is None

    alloc = asyncio.run(_call(server, "compare_task_allocation"))
    assert len(alloc["scenarios"]) == 5 and "caution" in alloc
    custom = asyncio.run(_call(server, "compare_task_allocation", {"robot_steps": [4, 6], "parallel": True}))
    assert [r["robot_steps"] for r in custom["scenarios"]] == [[], [4, 6]]
    bad = asyncio.run(_call(server, "compare_task_allocation", {"robot_steps": [1]}))
    assert "error" in bad and bad["robot_steps_available"] == [2, 4, 6, 8]

    missing = asyncio.run(_call(server, "get_clip_evidence", {"deviation_id": "dev-nope"}))
    assert "error" in missing
