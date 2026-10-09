"""Focused tests for runbook coordination and health recovery."""
import json

from dr import health_checker as hc
from dr import runbook as rb
from dr import failover as fo


def test_runbook_calls_failover_once_and_uses_returned_state(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "LOG", tmp_path / "runbook.jsonl")
    monkeypatch.setattr(rb.time, "sleep", lambda _: None)
    monkeypatch.setattr(rb.hc, "probe", lambda *args: (False, "timeout"))
    calls = []
    result = {"ok": True, "target": "b", "cutover": True,
              "state": {"region": "b", "weights": True, "count": 200, "pool_state": "full"},
              "snapshot": {"embed_model_version": "test-v1"}}
    monkeypatch.setattr(rb.fo, "failover", lambda *args, **kwargs: calls.append(args) or result)

    class Response:
        status_code = 200

        def json(self):
            return {"region": "b"}

    class Client:
        def __init__(self, **kwargs):
            self.requests = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            self.requests += 1
            return Response()

    monkeypatch.setattr(rb.httpx, "get", lambda *args, **kwargs: Response())
    monkeypatch.setattr(rb.httpx, "Client", Client)
    actual = rb.run("a", "b", "fs", auto=True)
    assert actual["ok"]
    assert len(calls) == 1
    assert actual["golden_signals"]["requests"] == 10
    events = [json.loads(line) for line in rb.LOG.read_text().splitlines()]
    assert [e["step"] for e in events] == list(range(1, 8))
    assert events[3]["vector_count"] == 200
    assert events[3]["state"]["count"] == 200
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)


def test_declined_confirmation_never_calls_failover(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "LOG", tmp_path / "runbook.jsonl")
    monkeypatch.setattr(rb.time, "sleep", lambda _: None)
    monkeypatch.setattr(rb.hc, "probe", lambda *args: (False, "timeout"))
    monkeypatch.setattr(rb.httpx, "get", lambda *args, **kwargs: type("Response", (), {"status_code": 200})())
    monkeypatch.setattr(rb, "confirm", lambda *args: False)
    monkeypatch.setattr(rb.fo, "failover", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("failover called despite declined confirmation")))
    assert not rb.run("a", "b", "fs", auto=False)["ok"]


def test_readiness_rejects_liveness_only_response(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"alive": True}

    monkeypatch.setattr(hc.httpx, "get", lambda *args, **kwargs: Response())
    assert hc.probe("a", 1)[0] is False


def test_health_checker_resets_failures_and_logs_recovery(monkeypatch, tmp_path):
    sequence = iter([False, True, False, False, False, True])
    monkeypatch.setattr(hc, "probe", lambda region, timeout: (
        next(sequence, True) if region == "a" else True, "mock"))
    log = tmp_path / "health.jsonl"
    hc.run(interval=0.001, timeout=0.01, threshold=3, duration=0.05, out=log)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["to"] for e in events] == ["UNHEALTHY", "HEALTHY"]
    assert events[0]["consecutive_fails"] == 3
    assert events[1]["consecutive_fails"] == 0


def test_successful_failover_orders_restore_readiness_and_cutover(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "edge").mkdir()
    active = tmp_path / "edge/active_region"
    active.write_text("a")
    monkeypatch.setattr(fo, "LOG", tmp_path / "failover.jsonl")
    monkeypatch.setattr(fo, "state_of", lambda region: {
        "region": region, "count": 200, "weights": True, "pool_state": "full"})
    monkeypatch.setattr(fo.snapshot, "get", lambda *args: {"embed_model_version": "v1"})
    monkeypatch.setattr(fo.snapshot, "rpo", lambda *args: {"rpo_seconds": 2, "docs_lost": 1})

    class Ready:
        status_code = 200

        def json(self):
            assert active.read_text() == "a", "cutover happened before readiness"
            assert (tmp_path / "state/region-b/pool_state").read_text() == "full"
            return {"ready": True}

    monkeypatch.setattr(fo.httpx, "get", lambda *args, **kwargs: Ready())
    result = fo.failover("b", "fs", wait=1)
    assert result["ok"] and active.read_text() == "b"
    assert [json.loads(line)["step"] for line in fo.LOG.read_text().splitlines()] == [
        "1_verify_target", "2_restore_snapshot", "3_scale_pool", "4_wait_ready", "5_dns_cutover"]
