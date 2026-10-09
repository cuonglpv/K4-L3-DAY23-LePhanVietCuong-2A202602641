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
import os
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {
    "a": os.environ.get("REGION_A_URL", "http://127.0.0.1:8001"),
    "b": os.environ.get("REGION_B_URL", "http://127.0.0.1:8002"),
}
LOG = pathlib.Path(os.environ.get("FAILOVER_LOG", "reports/failover-events.jsonl"))


def emit(**kw):
    """Append một event JSONL có timestamp và đồng thời in ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rec = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def state_of(region: str, timeout: float = 2.0) -> dict:
    """Đọc state của region; tách helper để test/drill có thể mô phỏng an toàn."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=timeout)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Thực hiện năm bước failover, tuyệt đối không cutover trước readiness."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unsupported backend: {backend}")
    if wait < 0:
        raise ValueError("wait must be >= 0")

    result = {"ok": False, "target": target, "backend": backend, "steps": []}

    # 1. Region đích phải tồn tại và trả được state trước khi đụng tới dữ liệu.
    try:
        initial_state = state_of(target)
        emit(step="1_verify_target", ok=True, target=target, state=initial_state)
        result["steps"].append("1_verify_target")
        result["initial_state"] = initial_state
    except Exception as exc:
        emit(step="1_verify_target", ok=False, target=target,
             error=type(exc).__name__, reason=str(exc))
        result.update(error="verify_target_failed", reason=str(exc))
        return result

    # 2. Restore cả vector DB lẫn model weights, rồi đo RPO trên dữ liệu thật.
    try:
        restored = snapshot.get(target, backend)
        source = "b" if target == "a" else "a"
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{source}/vectors.sqlite"),
                           pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        restore_event = {
            **restored,
            **rpo,
            "embed_model_version": restored.get("embed_model_version"),
        }
        emit(step="2_restore_snapshot", ok=True, target=target, **restore_event)
        result["steps"].append("2_restore_snapshot")
        result.update(restore_event)
    except (Exception, SystemExit) as exc:
        # snapshot.get dùng SystemExit khi chưa có snapshot; chuyển thành kết quả có cấu trúc.
        emit(step="2_restore_snapshot", ok=False, target=target,
             error=type(exc).__name__, reason=str(exc))
        result.update(error="restore_snapshot_failed", reason=str(exc))
        return result

    # 3. Scale pool. Python write_text tạo UTF-8 không BOM trên Windows.
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    try:
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full", encoding="utf-8")
        emit(step="3_scale_pool", ok=True, target=target, pool_state="full")
        result["steps"].append("3_scale_pool")
    except OSError as exc:
        emit(step="3_scale_pool", ok=False, target=target,
             error=type(exc).__name__, reason=str(exc))
        result.update(error="scale_pool_failed", reason=str(exc))
        return result

    # 4. Chờ readiness thật. Timeout ở đây là điều kiện abort, không được đổi DNS.
    started_wait = time.monotonic()
    deadline = started_wait + wait
    last_reason = "not_probed"
    ready_payload = None
    while True:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, max(wait, 0.1)))
            if response.status_code == 200:
                ready_payload = response.json()
                break
            try:
                body = response.json()
                last_reason = ",".join(body.get("reasons") or []) or f"status={response.status_code}"
            except (ValueError, TypeError, AttributeError):
                last_reason = f"status={response.status_code}"
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            emit(step="4_wait_ready", ok=False, target=target,
                 waited_s=round(time.monotonic() - started_wait, 3), reason=last_reason)
            result["steps"].append("4_wait_ready")
            result.update(error="wait_ready_timeout", reason=last_reason)
            return result
        time.sleep(min(0.5, remaining))

    waited = round(time.monotonic() - started_wait, 3)
    emit(step="4_wait_ready", ok=True, target=target, waited_s=waited,
         ready=ready_payload)
    result["steps"].append("4_wait_ready")

    try:
        final_state = state_of(target)
    except Exception:
        final_state = ready_payload or {}

    # 5. Chỉ ghi pointer sau khi target ready. Replace giúp reader không thấy file dở dang.
    active = pathlib.Path("edge/active_region")
    pending = active.with_name(f".{active.name}.tmp")
    try:
        active.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(target, encoding="utf-8")
        pending.replace(active)
        emit(step="5_dns_cutover", ok=True, target=target, active_region=target)
        result["steps"].append("5_dns_cutover")
    except OSError as exc:
        emit(step="5_dns_cutover", ok=False, target=target,
             error=type(exc).__name__, reason=str(exc))
        result.update(error="dns_cutover_failed", reason=str(exc), state=final_state)
        return result

    result.update(ok=True, state=final_state, waited_ready_s=waited,
                  active_region=target)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
