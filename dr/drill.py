"""Run both real local drills, including Windows process suspension.

python -m dr.drill
Only children launched by this runner may be suspended or terminated.
Existing generated data/logs are archived under run/archive-* before seeding.
"""
import ctypes
import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

from chaos import kill_region as chaos
from dr import runbook
from state.seed_vectors import seed
from tools.measure_rto import measure


def main(offset=0):
    root = pathlib.Path(__file__).resolve().parents[1]
    os.chdir(root)
    ports = [p + offset for p in (8001, 8002, 8080)]
    urls = {r: f"http://127.0.0.1:{ports[i]}" for i, r in enumerate("ab")}
    chaos.URL = urls
    runbook.URL = urls
    runbook.hc.URL = urls
    runbook.fo.URL = urls
    env_urls = {f"REGION_{r.upper()}_URL": url for r, url in urls.items()}
    for port in ports:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"Port {port} occupied; no processes were changed")
    archive = root / "run" / f"archive-{time.time_ns()}"
    generated = [pathlib.Path(f"state/region-{r}") for r in "ab"]
    generated += [pathlib.Path("state/_replica"), pathlib.Path("chaos/chaos-events.jsonl")]
    generated += list(pathlib.Path("reports").glob("*.jsonl"))
    generated += list(pathlib.Path("reports").glob("*.json"))
    for source in generated:
        if source.exists():
            destination = archive / source
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
    seed("a", 200, 2)
    seed("b", 0, 0)
    pathlib.Path("edge/active_region").write_text("a")
    processes, handles = [], []
    suspended = False

    def launch(name, args, env=None):
        log = open(f"run/{name}.log", "w", encoding="utf-8")
        handles.append(log)
        process = subprocess.Popen([sys.executable, *args], stdout=log, stderr=subprocess.STDOUT,
                                   env={**os.environ, **env_urls, "WARMUP_SECONDS": "6",
                                        "EDGE_TTL_SECONDS": "5", **(env or {})})
        processes.append(process)
        return process

    def toggle_suspend(process, resume=False):
        if os.name == "nt":
            function = getattr(ctypes.WinDLL("ntdll"), "NtResumeProcess" if resume else "NtSuspendProcess")
            function.argtypes = [ctypes.c_void_p]
            function.restype = ctypes.c_long
            status = function(int(process._handle))
            if status != 0:
                raise OSError(f"Process suspend/resume failed: NTSTATUS {status}")
        else:
            import signal
            os.kill(process.pid, signal.SIGCONT if resume else signal.SIGSTOP)

    def outage(process):
        nonlocal suspended
        if process.poll() is not None or not chaos.is_alive("b"):
            raise RuntimeError("Owned primary not running or standby not alive; abort")
        chaos.event(action="kill", region="a", mode="netblock", backend="bare", mock=True,
                    other_region="b", other_alive=True, forced_both=False,
                    platform=sys.platform,
                    method="NtSuspendProcess" if os.name == "nt" else "SIGSTOP",
                    note="Real suspension of runner-owned Region A; t_outage_start")
        toggle_suspend(process)
        suspended = True

    try:
        a = launch("region-a", ["-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1",
                                "--port", str(ports[0]), "--log-level", "warning"], {"REGION": "a"})
        launch("region-b", ["-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1",
                            "--port", str(ports[1]), "--log-level", "warning"], {"REGION": "b"})
        edge = launch("edge", ["-m", "uvicorn", "edge.proxy:app", "--host", "127.0.0.1",
                               "--port", str(ports[2]), "--log-level", "warning"])
        import httpx
        for _ in range(30):
            if any(p.poll() is not None for p in (a, processes[1], edge)):
                raise RuntimeError("Service exited; inspect run/*.log")
            try:
                if chaos.is_ready("a") and chaos.is_alive("b") and httpx.get(
                        f"http://127.0.0.1:{ports[2]}/v1/infer", timeout=3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Services did not start")
        # Establish B's warm state while the process is running.
        print("Standby before recovery:", httpx.get(f"{urls['b']}/v1/state").json(), flush=True)
        pathlib.Path("reports/drill-config.json").write_text(json.dumps({
            "platform": sys.platform, "ports": ports, "region_urls": urls,
            "suspension": "NtSuspendProcess" if os.name == "nt" else "SIGSTOP",
            "warmup_seconds": 6, "edge_ttl_seconds": 5, "port_offset": offset}, indent=2))
        baseline = launch("baseline", ["loadgen/traffic.py", "--duration", "40", "--rps", "2",
                                        "--url", f"http://127.0.0.1:{ports[2]}/v1/infer",
                                        "--out", "reports/drill-1-nodr.jsonl"])
        time.sleep(8)
        outage(a)
        if baseline.wait(timeout=50) != 0:
            raise RuntimeError("Baseline traffic failed")
        toggle_suspend(a, resume=True)
        suspended = False
        chaos.event(action="restore", region="a", method="NtResumeProcess" if os.name == "nt" else "SIGCONT")
        print("Baseline complete: no recovery. Starting DR drill.", flush=True)
        ingest = launch("ingest", ["state/ingest.py", "--region", "a", "--rate", "0.5", "--duration", "120"])
        replicate = launch("replicate", ["state/replicate.py", "--every", "30", "--duration", "120", "--backend", "fs"])
        time.sleep(5)
        if ingest.poll() is not None or replicate.poll() is not None or not pathlib.Path(
                "state/_replica/dr-artifacts/MANIFEST.json").exists():
            raise RuntimeError("Ingest/replication did not start")
        traffic = launch("withdr", ["loadgen/traffic.py", "--duration", "100", "--rps", "2",
                                    "--url", f"http://127.0.0.1:{ports[2]}/v1/infer",
                                    "--out", "reports/drill-2-withdr.jsonl"])
        health = launch("health", ["dr/health_checker.py", "--interval", "5", "--threshold", "3",
                                   "--duration", "100", "--out", "reports/health-events.jsonl"])
        time.sleep(12)
        outage(a)
        result = runbook.run("a", "b", "fs", auto=True)
        if not result["ok"]:
            raise RuntimeError(f"Runbook failed: {result}")
        for process in (traffic, health):
            if process.wait(timeout=110) != 0:
                raise RuntimeError("Traffic or health checker exited with errors")
        for number, name in ((1, "drill-1-nodr"), (2, "drill-2-withdr")):
            measured = measure(f"reports/{name}.jsonl", "chaos/chaos-events.jsonl",
                               "reports/health-events.jsonl", "reports/failover-events.jsonl", 300)
            pathlib.Path(f"reports/measure-drill-{number}.json").write_text(
                json.dumps(measured, indent=2), encoding="utf-8")
            print(json.dumps(measured, indent=2), flush=True)
    finally:
        if suspended:
            toggle_suspend(a, resume=True)
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-offset", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.port_offset <= 57455:
        parser.error("port-offset must keep ports within 1..65535")
    main(args.port_offset)
