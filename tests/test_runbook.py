import time

from dr import runbook as rb


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def _prepare_runbook(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "LOG", tmp_path / "runbook.jsonl")
    monkeypatch.setattr(rb, "EDGE_URL", "http://edge.test")
    monkeypatch.setattr(rb.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        rb, "probe",
        lambda region, timeout: (False, "mocked_primary_down")
        if region == "a" else (False, "mocked_standby_not_ready"),
    )
    monkeypatch.setattr(rb, "_latest_outage", lambda _primary: {"ts": time.time() - 1})
    monkeypatch.setattr(
        rb.fo, "failover",
        lambda target, backend, wait: {
            "ok": True,
            "target": target,
            "backend": backend,
            "steps": ["1_verify_target", "2_restore_snapshot", "3_scale_pool",
                      "4_wait_ready", "5_dns_cutover"],
            "state": {"weights": True, "count": 10},
            "active_region": target,
            "rpo_seconds": 1.0,
            "docs_lost": 0,
            "embed_model_version": "test",
        },
    )


def test_runbook_verifies_edge_and_sends_golden_requests_through_edge(monkeypatch, tmp_path):
    _prepare_runbook(monkeypatch, tmp_path)
    calls = []
    edge_states = iter(["a", "b"])

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/edge/state"):
            return FakeResponse({"active_region": next(edge_states)})
        assert url == "http://edge.test/v1/infer"
        return FakeResponse({"edge_region": "b", "region": "b"})

    monkeypatch.setattr(rb.httpx, "get", fake_get)
    result = rb.run("a", "b", "fs", auto=True)

    assert result["ok"] is True
    assert calls.count("http://edge.test/edge/state") == 2
    assert calls.count("http://edge.test/v1/infer") == 10
    assert result["golden_signals"]["latency_ok"] is True


def test_runbook_fails_when_golden_p95_exceeds_limit(monkeypatch, tmp_path):
    _prepare_runbook(monkeypatch, tmp_path)
    monkeypatch.setattr(rb, "_p95", lambda _values: rb.GOLDEN_P95_LIMIT_MS + 0.01)

    def fake_get(url, **kwargs):
        if url.endswith("/edge/state"):
            return FakeResponse({"active_region": "b"})
        return FakeResponse({"edge_region": "b", "region": "b"})

    monkeypatch.setattr(rb.httpx, "get", fake_get)
    result = rb.run("a", "b", "fs", auto=True)

    assert result["ok"] is False
    assert result["error"] == "golden_signals_failed"
    assert result["golden_signals"]["latency_ok"] is False
