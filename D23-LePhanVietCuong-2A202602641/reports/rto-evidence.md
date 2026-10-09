# RTO/RPO Evidence - Lab 23

Tat ca timestamp trong bang la UTC va lay tu log cua drill ngay 2026-10-09.

## 1. Drill 1 - khong co DR

| Chi so | Gia tri | Cach do | Evidence |
|---|---:|---|---|
| t_outage | 2026-10-09T04:47:07Z | chaos netblock Region A | `chaos/chaos-events.jsonl:1` |
| Request fail dau tien | +2.1s | request `ok:false` dau tien sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thanh cong sau do | Khong co | `request_thanh_cong_dau_tien:null` | `reports/measure-drill-1.json:16` |
| RTO | `NO_RECOVERY` | verdict do tu cua so loadgen va chaos | `reports/measure-drill-1.json:25` |

## 2. Drill 2 - co DR

| Moc | +giay tu t_outage | Cach do | Evidence |
|---|---:|---|---|
| t_outage | 0.0s | `action:kill`, `mode:netblock`, Region A | `chaos/chaos-events.jsonl:3` |
| User thay loi dau tien sau t_outage | 2.1s | dong `ok:false` dau tien co timestamp sau t_outage | `reports/drill-2-withdr.jsonl:25` |
| Health checker phat hien | 14.8s | Region A chuyen `UNHEALTHY` sau 3 fail | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 18.4s | `2_restore_snapshot`, co RPO that | `reports/failover-events.jsonl:2` |
| Region B ready | 24.7s | `4_wait_ready`, waited 6.266s | `reports/failover-events.jsonl:4` |
| DNS cutover | 25.1s | `5_dns_cutover`, active Region B | `reports/failover-events.jsonl:5` |
| **Request thanh cong dau tien tu B / RTO** | **31.5s** | request dau tien `ok:true`, `edge_region:b`, `served_by:b` sau loi | `reports/drill-2-withdr.jsonl:38` |

| Chi so | Do duoc | Muc tieu | Verdict |
|---|---:|---:|---|
| RTO - Inference API | 31.5s | 300s | PASS |
| RPO - Vector DB | 8.01s / 4 documents | <=30s theo replication interval (`reports/replication.jsonl:1`) | PASS |

Nguon tong hop may doc duoc: `reports/measure-drill-2.json:20`. Restore dung embedding model `embed-model=vi-e5-base@v3`, duoc ghi cung RPO tai `reports/failover-events.jsonl:2`. Runbook xac minh Edge het TTL va route sang B tai `reports/runbook-run.jsonl:5`; 10 golden requests end-to-end co p95 892.67ms <= 5000ms tai `reports/runbook-run.jsonl:6`.

## 3. RTO gom nhung gi

| Thanh phan | Giay | Nguon timestamp | Cach giam |
|---|---:|---|---|
| Health-check detection | 14.8s | outage den `reports/health-events.jsonl:2`; cau hinh 5.0s x 3, detect floor 15.0s | Giam interval nhung giu threshold/hysteresis |
| Runbook confirm + verify target | 3.6s | detect den `reports/failover-events.jsonl:2` | Alert tich hop nut confirm va preflight song song |
| Snapshot restore | 0.0s (khoang 0.016s) | `reports/failover-events.jsonl:1` den `reports/failover-events.jsonl:2` | Duy tri snapshot nho, gan region dich |
| GPU pool warm-up | 6.3s | `waited_s` tai `reports/failover-events.jsonl:4` | Giu capacity warm hoac full |
| DNS cutover + TTL | 6.9s | ready den cutover den request B tai `reports/drill-2-withdr.jsonl:38` | Giam TTL hoac dung global LB health routing |

Delta chua lam tron cong lai thanh 31.5s. Co 13 request fail trong drill 2; ket qua tong hop khong co warning tai `reports/measure-drill-2.json:4`.
