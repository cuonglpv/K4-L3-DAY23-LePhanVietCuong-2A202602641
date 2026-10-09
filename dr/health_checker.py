"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import os
import pathlib
import time

import httpx

URL = {
    "a": os.environ.get("REGION_A_URL", "http://127.0.0.1:8001"),
    "b": os.environ.get("REGION_B_URL", "http://127.0.0.1:8002"),
}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Probe readiness của một region và luôn giới hạn thời gian chờ."""
    if region not in URL:
        return False, f"unknown_region={region}"
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        if response.status_code == 200:
            return True, "ready"

        # Giữ lại lý do readiness nếu upstream trả JSON như serving/app.py.
        try:
            body = response.json()
            reasons = body.get("reasons") or []
            detail = ",".join(str(reason) for reason in reasons)
        except (ValueError, TypeError, AttributeError):
            detail = ""
        reason = f"http_status={response.status_code}"
        return False, f"{reason}:{detail}" if detail else reason
    except httpx.TimeoutException as exc:
        return False, f"timeout:{type(exc).__name__}"
    except httpx.HTTPError as exc:
        return False, f"http_error:{type(exc).__name__}"
    except Exception as exc:  # Một probe lỗi không được làm chết cả checker.
        return False, f"probe_error:{type(exc).__name__}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll cả hai region và chỉ ghi log khi trạng thái thực sự thay đổi."""
    if interval <= 0:
        raise ValueError("interval must be > 0")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")
    if threshold < 1:
        raise ValueError("threshold must be >= 1")
    if duration < 0:
        raise ValueError("duration must be >= 0")

    out.parent.mkdir(parents=True, exist_ok=True)
    state = {region: "HEALTHY" for region in URL}
    consecutive_fails = {region: 0 for region in URL}
    deadline = time.monotonic() + duration
    next_poll = time.monotonic()
    emitted: list[dict] = []

    while time.monotonic() < deadline:
        for region in URL:
            ready, reason = probe(region, timeout)
            if ready:
                consecutive_fails[region] = 0
                new_state = "HEALTHY"
            else:
                consecutive_fails[region] += 1
                new_state = ("UNHEALTHY" if consecutive_fails[region] >= threshold
                             else state[region])

            if new_state != state[region]:
                now = time.time()
                record = {
                    "ts": now,
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "event": "state_change",
                    "region": region,
                    "from": state[region],
                    "to": new_state,
                    "reason": reason,
                    "consecutive_fails": consecutive_fails[region],
                    "interval_s": interval,
                    "threshold": threshold,
                }
                with out.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(json.dumps(record, ensure_ascii=False), flush=True)
                emitted.append(record)
                state[region] = new_state

        next_poll += interval
        sleep_for = min(next_poll - time.monotonic(), deadline - time.monotonic())
        if sleep_for > 0:
            time.sleep(sleep_for)

    return emitted


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
