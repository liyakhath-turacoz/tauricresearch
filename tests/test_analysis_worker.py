"""The worker uses a fake CLI. These process checks never contact an AI provider."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import analysis_worker

pytestmark = pytest.mark.unit


def test_worker_refuses_missing_parent():
    with pytest.raises(RuntimeError):
        analysis_worker.guard_parent(-1)


def test_worker_only_accepts_cli_main(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analysis_worker.py", str(os.getpid()), "-m", "other.module"])
    with pytest.raises(SystemExit, match="Usage"):
        analysis_worker.main()


def test_worker_forwards_options_without_starting_its_own_analysis(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sys, "argv", ["analysis_worker.py", "1234", "-m", "cli.main", "--ticker", "NVDA"]
    )
    monkeypatch.setattr(analysis_worker, "guard_parent", lambda pid: calls.append(pid))
    monkeypatch.setattr(
        analysis_worker.runpy, "run_module", lambda *a, **kw: calls.append((a, kw, list(sys.argv)))
    )
    analysis_worker.main()
    assert calls[0] == 1234
    assert calls[1][2] == ["cli.main", "--ticker", "NVDA"]


@pytest.mark.parametrize("mode", ["parent_exit", "stop_redirector"])
def test_native_parent_and_redirector_cleanup(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    ready = tmp_path / "ready.json"
    heartbeat = tmp_path / "heartbeat"
    child_code = """
import os,sys,time,json
from pathlib import Path
import analysis_worker
parent,ready,heartbeat=sys.argv[1:]
def fake_cli(*a,**kw):
    Path(ready).write_text(json.dumps({'pid':os.getpid()}))
    while True:
        with open(heartbeat,'a') as stream:stream.write('.')
        time.sleep(.03)
analysis_worker.runpy.run_module=fake_cli
sys.argv=['analysis_worker.py',parent,'-m','cli.main']
analysis_worker.main()
"""
    parent_code = """
import subprocess,sys,os,time,json
from pathlib import Path
root,child_code,ready,heartbeat,mode=sys.argv[1:]
child=subprocess.Popen([sys.executable,'-c',child_code,str(os.getpid()),ready,heartbeat],cwd=root)
deadline=time.monotonic()+5
while not Path(ready).exists():
    if child.poll() is not None:raise SystemExit('Worker exited before readiness')
    if time.monotonic()>deadline:raise SystemExit('Worker did not become ready')
    time.sleep(.02)
time.sleep(.1)
if mode=='stop_redirector':
    child.terminate()
    child.wait(timeout=2)
    time.sleep(.2)
    first=Path(heartbeat).stat().st_size
    time.sleep(.2)
    stopped=Path(heartbeat).stat().st_size==first
    print(json.dumps({'stopped':stopped}),flush=True)
else:
    print(json.dumps({'parent_exited':True}),flush=True)
"""
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            parent_code,
            str(root),
            child_code,
            str(ready),
            str(heartbeat),
            mode,
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        output, error = parent.communicate(timeout=8)
        assert parent.returncode == 0, error
        result = json.loads(output)
        assert result.get("stopped", result.get("parent_exited"))
        before = heartbeat.stat().st_size
        import time

        time.sleep(0.2)
        assert heartbeat.stat().st_size == before
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        if ready.exists():
            try:
                os.kill(json.loads(ready.read_text())["pid"], signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
