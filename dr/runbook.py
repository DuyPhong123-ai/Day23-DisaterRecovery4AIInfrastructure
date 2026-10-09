"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc
import math

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = fo.URL


def step(n, name, **kw):
    """TODO: ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {**kw, "ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "step": n, "name": name}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """TODO: auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """TODO: 7 bước ở trên."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("Primary and target must be distinct valid regions")
    started = time.monotonic()
    observed_after = time.time()
    # Independent confirmation: three failures, with the first probe after 5s.
    for _ in range(3):
        time.sleep(5)
        ready, reason = hc.probe(primary, 2)
        try:
            other = httpx.get(f"{URL[target]}/healthz", timeout=2)
            other_alive = other.status_code == 200
        except httpx.HTTPError:
            other_alive = False
        if ready or not other_alive:
            step(1, "xac_nhan_outage", ok=False, reason="primary ready or target not alive")
            return {"ok": False, "reason": "outage not confirmed or target unavailable"}
    step(1, "xac_nhan_outage", ok=True, primary=primary, target=target, reason=reason,
         interval_s=5, threshold=3)
    if not confirm(auto, f"Confirm failover {primary} -> {target}?"):
        step(2, "thong_bao_incident", ok=False, confirmed=False)
        return {"ok": False, "reason": "operator declined"}
    # Match only a recent outage, never borrow an old drill's timestamp.
    outage = None
    events = pathlib.Path("chaos/chaos-events.jsonl")
    if events.exists():
        kills = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
        outage = next((e["ts"] for e in reversed(kills) if e.get("action") == "kill"
                       and e.get("region") == primary and observed_after - 60 <= e["ts"] <= observed_after), None)
    step(2, "thong_bao_incident", confirmed=True, auto=auto, t_outage=outage,
         notification_delay_s=None if outage is None else round(time.time() - outage, 3))
    result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=result["ok"], result=result)
    if not result["ok"]:
        return result
    step(4, "verify_state_replica", state=result["state"],
         vector_count=result["state"].get("count"), weights=result["state"].get("weights"),
         embed_model_version=result["snapshot"]["embed_model_version"])
    step(5, "dns_cutover", ok=result["cutover"], target=target)
    latencies, errors = [], 0
    with httpx.Client(timeout=3) as client:
        for _ in range(10):
            t0 = time.monotonic()
            try:
                response = client.get(f"{URL[target]}/v1/infer")
                errors += response.status_code != 200 or response.json().get("region") != target
            except (httpx.HTTPError, ValueError):
                errors += 1
            latencies.append((time.monotonic() - t0) * 1000)
    p95 = sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1]
    signals = {"requests": 10, "p95_ms": round(p95, 2), "error_rate": errors / 10}
    signals_ok = errors == 0 and p95 < 1000
    step(6, "verify_golden_signals", ok=signals_ok, **signals)
    step(7, "post_incident", ok=signals_ok, elapsed_s=round(time.monotonic() - started, 3),
         measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {**result, "ok": signals_ok, "golden_signals": signals}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)
