# Runbook — Region chính down

Phạm vi: Region A là primary, Region B là DR. Người trực chỉ chạy `dr/runbook.py` **một lần**; script này tự restore, scale, chờ readiness rồi mới cutover. Không sửa `edge/active_region` bằng tay.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `1..3 \| ForEach-Object { curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8001/readyz; curl.exe -s http://127.0.0.1:8002/healthz; Start-Sleep 5 }` | Region A fail 3 lần liên tiếp; Region B trả `alive:true` cả 3 lần | SRE on-call |
| 2 | Mở incident và xác nhận failover | `python dr/runbook.py --primary a --target b --backend fs` | Có prompt `y/N`; sau khi chọn `y`, `reports/runbook-run.jsonl` có `thong_bao_incident` | Incident Commander |
| 3 | Restore state và scale GPU pool | `Get-Content reports/failover-events.jsonl -Tail 5` | Thấy đúng thứ tự `1_verify_target`, `2_restore_snapshot`, `3_scale_pool`; restore event có `rpo_seconds`, `docs_lost`, `embed_model_version` | SRE on-call |
| 4 | Chờ Region B ready | `curl.exe -i http://127.0.0.1:8002/readyz` | HTTP 200, `ready:true`, pool `full`, vector count > 0, weights tồn tại | AI platform on-call |
| 5 | Xác minh DNS/LB cutover | `1..6 \| ForEach-Object { curl.exe -s http://127.0.0.1:8080/edge/state; Start-Sleep 1 }` | Sau tối đa một TTL, `active_region` là `b`; failover log có `5_dns_cutover` | SRE on-call |
| 6 | Verify golden signals | `1..10 \| ForEach-Object { curl.exe -s "http://127.0.0.1:8080/v1/infer?q=golden%20signal" }` | 10/10 HTTP 200; mỗi response có cả `edge_region:"b"` và `region:"b"`; runbook log có error rate 0 | AI platform on-call |
| 7 | Đo RTO/RPO và mở postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, không warning, `rto_verdict:PASS`, RPO và `docs_lost` khác null | Incident Commander |

## Điều kiện abort

- Nếu restore snapshot thất bại, model version thiếu/không khớp, vector count bằng 0, hoặc Region B không đạt `/readyz` trước timeout: **không cutover**. Giữ incident mở và khắc phục snapshot/compute trước.
- Nếu Region B có error rate > 1% hoặc p95 > 5 giây sau cutover: dừng thay đổi tiếp theo, đánh giá rollback với Incident Commander.

## Rollback / failback về Region A

Chỉ failback khi nguyên nhân ở A đã được xử lý, A trả `/readyz` 200 ba lần liên tiếp, và state phát sinh ở B đã được snapshot. Incident Commander là người duy nhất phê duyệt; SRE on-call thực thi:

```powershell
python state/snapshot.py put --region b --backend fs
python dr/failover.py --target a --backend fs --wait 60
curl.exe http://127.0.0.1:8080/edge/state
```

Nếu failback không đạt readiness hoặc golden signals, script phải abort trước DNS cutover; tiếp tục phục vụ ở B.
