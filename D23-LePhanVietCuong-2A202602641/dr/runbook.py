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
  5 dns_cutover              — poll Edge qua /edge/state tới khi TTL hết và Edge thực
                              sự resolve sang region đích.
  6 verify_golden_signals    — 10 request end-to-end qua Edge: route đúng region,
                              p95 latency <= 5s và error rate bằng 0.
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import os
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {
    "a": os.environ.get("REGION_A_URL", "http://127.0.0.1:8001"),
    "b": os.environ.get("REGION_B_URL", "http://127.0.0.1:8002"),
}
EDGE_URL = os.environ.get("EDGE_URL", "http://127.0.0.1:8080")
EDGE_CUTOVER_WAIT_SECONDS = float(os.environ.get("EDGE_CUTOVER_WAIT_SECONDS", "15"))
GOLDEN_P95_LIMIT_MS = float(os.environ.get("GOLDEN_P95_LIMIT_MS", "5000"))


def step(n, name, **kw):
    """Ghi một mốc timeline của runbook."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rec = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Auto mode dùng cho CI; vận hành thường phải xác nhận rõ ràng."""
    if auto:
        return True
    try:
        answer = input(f"{msg} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def _latest_outage(primary: str) -> dict | None:
    events = pathlib.Path("chaos/chaos-events.jsonl")
    if not events.exists():
        return None
    latest = None
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == primary:
            latest = event
    return latest


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, int(0.95 * len(ordered) + 0.999999) - 1)
    return round(ordered[index], 2)


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực thi runbook bảy bước và trả kết quả có cấu trúc."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different known regions")

    started = time.time()
    result = {"ok": False, "primary": primary, "target": target, "backend": backend}

    # 1. Ba lần fail liên tiếp, cách nhau 5 giây, cùng nhịp với health checker chuẩn.
    observations = []
    primary_failures = 0
    for attempt in range(1, 4):
        primary_ready, primary_reason = probe(primary, timeout=2.0)
        target_ready, target_reason = probe(target, timeout=2.0)
        observations.append({
            "attempt": attempt,
            "primary_ready": primary_ready,
            "primary_reason": primary_reason,
            "target_ready": target_ready,
            "target_reason": target_reason,
        })
        primary_failures = primary_failures + 1 if not primary_ready else 0
        if attempt < 3:
            time.sleep(5.0)

    outage_confirmed = primary_failures >= 3
    step(1, "xac_nhan_outage", ok=outage_confirmed,
         consecutive_primary_failures=primary_failures, observations=observations)
    if not outage_confirmed:
        result.update(error="outage_not_confirmed", observations=observations)
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started, 2),
             reason="outage_not_confirmed")
        return result

    # 2. Timestamp incident luôn được tạo sau khi outage đã được xác nhận.
    outage = _latest_outage(primary)
    outage_ts = outage.get("ts") if outage else None
    if not confirm(auto, f"Đã xác nhận region-{primary} outage. Failover sang region-{target}?"):
        step(2, "thong_bao_incident", ok=False, outage_ts=outage_ts,
             reason="operator_declined")
        result.update(error="operator_declined", outage_ts=outage_ts)
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started, 2),
             reason="operator_declined")
        return result
    incident = step(2, "thong_bao_incident", ok=True, outage_ts=outage_ts,
                    notification_delay_s=(None if outage_ts is None
                                          else round(time.time() - outage_ts, 2)))

    # 3. Hàm failover được gọi đúng một lần; các bước sau chỉ đọc kết quả này.
    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=failover_result.get("ok", False),
         failover_steps=failover_result.get("steps", []),
         error=failover_result.get("error"))

    state = failover_result.get("state") or {}
    replica_ok = bool(failover_result.get("ok") and state.get("weights")
                      and state.get("count", 0) > 0)
    step(4, "verify_state_replica", ok=replica_ok,
         count=state.get("count"), weights=state.get("weights"),
         rpo_seconds=failover_result.get("rpo_seconds"),
         docs_lost=failover_result.get("docs_lost"),
         embed_model_version=failover_result.get("embed_model_version"))

    # 5. File pointer da doi chua du: doi Edge het TTL va tu resolve sang target.
    cutover_requested = bool(failover_result.get("ok")
                             and failover_result.get("active_region") == target)
    cutover_ok = False
    edge_state = {}
    edge_reason = failover_result.get("reason") or "cutover_not_requested"
    edge_attempts = 0
    edge_wait_started = time.monotonic()
    edge_deadline = edge_wait_started + EDGE_CUTOVER_WAIT_SECONDS
    while cutover_requested:
        edge_attempts += 1
        try:
            response = httpx.get(f"{EDGE_URL}/edge/state", timeout=2.0)
            if response.status_code == 200:
                edge_state = response.json()
                if edge_state.get("active_region") == target:
                    cutover_ok = True
                    edge_reason = "edge_routes_to_target"
                    break
                edge_reason = f"edge_active_region={edge_state.get('active_region')}"
            else:
                edge_reason = f"edge_state_status={response.status_code}"
        except Exception as exc:
            edge_reason = f"{type(exc).__name__}: {exc}"

        remaining = edge_deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.5, remaining))

    step(5, "dns_cutover", ok=cutover_ok,
         requested_region=failover_result.get("active_region"),
         edge_active_region=edge_state.get("active_region"),
         edge_url=EDGE_URL, attempts=edge_attempts,
         waited_s=round(time.monotonic() - edge_wait_started, 3),
         reason=edge_reason)

    if not (failover_result.get("ok") and replica_ok and cutover_ok):
        elapsed = round(time.time() - started, 2)
        step(6, "verify_golden_signals", ok=False, requests=0,
             reason="failover_incomplete")
        step(7, "post_incident", ok=False, elapsed_s=elapsed,
             measure_command=("python tools/measure_rto.py --loadgen "
                              "reports/drill-2-withdr.jsonl --target-rto 300"))
        result.update(error=failover_result.get("error", "failover_incomplete"),
                      failover=failover_result, elapsed_s=elapsed,
                      incident_ts=incident["ts"])
        return result

    # 6. Mười request end-to-end qua Edge; xác minh cả route và upstream region.
    latencies = []
    failures = 0
    samples = []
    for request_no in range(1, 11):
        t0 = time.perf_counter()
        try:
            response = httpx.get(f"{EDGE_URL}/v1/infer",
                                 params={"q": "golden signal"}, timeout=6.0)
            latency_ms = (time.perf_counter() - t0) * 1000
            body = response.json()
            edge_region = body.get("edge_region")
            served_by = body.get("region")
            ok = (response.status_code == 200 and edge_region == target
                  and served_by == target)
            if not ok:
                failures += 1
            samples.append({"request": request_no, "ok": ok,
                            "status": response.status_code,
                            "edge_region": edge_region,
                            "region": served_by,
                            "latency_ms": round(latency_ms, 2)})
            latencies.append(latency_ms)
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            failures += 1
            latencies.append(latency_ms)
            samples.append({"request": request_no, "ok": False,
                            "error": type(exc).__name__,
                            "latency_ms": round(latency_ms, 2)})

    error_rate = round(failures / 10, 3)
    p95_latency_ms = _p95(latencies)
    latency_ok = (p95_latency_ms is not None
                  and p95_latency_ms <= GOLDEN_P95_LIMIT_MS)
    golden_ok = failures == 0 and latency_ok
    step(6, "verify_golden_signals", ok=golden_ok, requests=10,
         errors=failures, error_rate=error_rate,
         p95_latency_ms=p95_latency_ms,
         p95_limit_ms=GOLDEN_P95_LIMIT_MS, latency_ok=latency_ok,
         edge_url=EDGE_URL, samples=samples)

    elapsed = round(time.time() - started, 2)
    overall_ok = golden_ok
    measure_command = ("python tools/measure_rto.py --loadgen "
                       "reports/drill-2-withdr.jsonl --target-rto 300")
    step(7, "post_incident", ok=overall_ok, elapsed_s=elapsed,
         measure_command=measure_command)
    result.update(ok=overall_ok, failover=failover_result,
                  golden_signals={"requests": 10, "errors": failures,
                                  "error_rate": error_rate,
                                  "p95_latency_ms": p95_latency_ms,
                                  "p95_limit_ms": GOLDEN_P95_LIMIT_MS,
                                  "latency_ok": latency_ok},
                  elapsed_s=elapsed, incident_ts=incident["ts"],
                  outage_ts=outage_ts, measure_command=measure_command)
    if not overall_ok:
        result["error"] = "golden_signals_failed"
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
