"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
import os

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {r: os.environ.get(f"REGION_{r.upper()}_URL", f"http://127.0.0.1:{8001 + i}")
       for i, r in enumerate("ab")}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """TODO: append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    print(json.dumps(record), flush=True)
    return record


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """TODO: 5 bước ở trên, đúng thứ tự."""
    if target not in URL or backend not in ("fs", "minio") or wait <= 0:
        raise ValueError("Invalid target, backend or wait")
    primary = "b" if target == "a" else "a"
    current_step = "1_verify_target"
    try:
        before = state_of(target)
        emit(step=current_step, target=target, state=before)
        current_step = "2_restore_snapshot"
        started = time.monotonic()
        meta = snapshot.get(target, backend)
        loss = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                            pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        emit(step=current_step, target=target, **meta, **loss,
             elapsed_s=round(time.monotonic() - started, 4))
        current_step = "3_scale_pool"
        pool = pathlib.Path(f"state/region-{target}/pool_state")
        pool.parent.mkdir(parents=True, exist_ok=True)
        pool.write_text("full", encoding="utf-8")
        emit(step=current_step, target=target, pool_state="full")
        current_step = "4_wait_ready"
        started = time.monotonic()
        deadline = started + wait
        reason = "not ready"
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{URL[target]}/readyz",
                                     timeout=min(2, max(0.01, deadline - time.monotonic())))
                if response.status_code == 200 and response.json().get("ready") is True:
                    break
                reason = f"HTTP {response.status_code}: {response.text}"
            except (httpx.HTTPError, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        else:
            emit(step=current_step, target=target, ok=False, reason=reason,
                 waited_s=round(time.monotonic() - started, 4))
            return {"ok": False, "target": target, "cutover": False, "reason": reason}
        after = state_of(target)
        emit(step=current_step, target=target, ok=True,
             waited_s=round(time.monotonic() - started, 4), state=after)
        current_step = "5_dns_cutover"
        active = pathlib.Path("edge/active_region")
        temporary = active.with_suffix(".tmp")
        temporary.write_text(target, encoding="utf-8")
        temporary.replace(active)
        emit(step=current_step, target=target, ok=True)
        return {"ok": True, "target": target, "cutover": True, "state": after,
                "snapshot": meta, **loss}
    except (httpx.HTTPError, OSError, ValueError, SystemExit) as exc:
        emit(step=current_step, target=target, ok=False, reason=str(exc))
        return {"ok": False, "target": target, "cutover": False, "reason": str(exc)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    result = failover(a.target, a.backend, a.wait)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)
