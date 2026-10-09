# Postmortem - DR Drill Lab 23

Drill duoc thuc hien ngay 2026-10-09. Day la postmortem blameless: loi duoc quy ve thiet ke va quy trinh, khong quy ve ca nhan.

## 1. Timeline

| ISO time (UTC) | Su kien | Evidence |
|---|---|---|
| 2026-10-09T06:58:30Z | Region A bi netblock | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T06:58:32Z | Request dau tien sau t_outage bi loi | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T06:58:44Z | Health checker chuyen A sang `UNHEALTHY` | `reports/health-events.jsonl:2` |
| 2026-10-09T06:58:48Z | Operator/runbook xac nhan incident va bat dau failover | `reports/runbook-run.jsonl:2` |
| 2026-10-09T06:58:48Z | Snapshot restore hoan tat: RPO 8.01s, mat 4 docs | `reports/failover-events.jsonl:2` |
| 2026-10-09T06:58:55Z | DNS pointer cutover sang Region B | `reports/failover-events.jsonl:5` |
| 2026-10-09T06:59:00Z | Edge het TTL, xac nhan `edge_active_region:b` | `reports/runbook-run.jsonl:5` |
| 2026-10-09T06:59:01Z | Request dau tien thanh cong end-to-end tu Region B | `reports/drill-2-withdr.jsonl:38` |
| 2026-10-09T06:59:06Z | 10 golden requests PASS, p95 892.67ms <= 5000ms | `reports/runbook-run.jsonl:6` |

## 2. RTO/RPO so voi muc tieu va gap analysis

- RTO muc tieu: 300s; do duoc: **31.5s**; gap/headroom: **268.5s**. Ket qua PASS, Region B phuc vu recovery va measurement khong co warning tai `reports/measure-drill-2.json:20`.
- RPO muc tieu theo replication schedule: **<=30s** (`reports/replication.jsonl:1`); do duoc: **8.01s**, tuong ung **4 documents** chua co trong snapshot; headroom: **21.99s**.
- Buoc ton nhieu thoi gian nhat la health-check detection: 14.8s, chiem khoang 47.0% RTO. Runbook confirmation/preflight ton 3.6s; GPU warm-up ton 6.3s.
- Baseline khong co DR ket thuc voi `NO_RECOVERY` va 9 request fail, chung minh recovery khong tu xay ra (`reports/measure-drill-1.json:25`).

## 3. Root cause - 5 whys

1. Vi sao user gap loi? Edge van route toi Region A sau khi tien trinh A dung.
2. Vi sao traffic chua doi ngay? Thiet ke yeu cau ba readiness failure lien tiep de chong flapping.
3. Vi sao chua the route sang B khi outage vua xay ra? B ban dau chi warm, khong co weights va vector data.
4. Vi sao B can restore trong incident? State duoc replicate theo snapshot dinh ky thay vi active-active; snapshot gan nhat cham hon primary 8.01s.
5. Vi sao RTO con 31.5s du restore rat nhanh? Detection 14.8s, xac nhan/preflight 3.6s, GPU warm-up 6.3s va DNS/TTL 6.9s nam noi tiep tren critical path.

Root cause he thong la standby chua san sang phuc vu va cac buoc detect/confirm/warm-up dang tuan tu. Chaos chi kich hoat dieu kien de quan sat van de nay.

## 4. Action items

| # | Action item | Owner | Deadline | Tac dong du kien |
|---|---|---|---|---|
| 1 | Pre-restore model weights/vector snapshot o B va check `embed_model_version` sau moi replication | AI Platform | 2026-10-16 | Drill nay restore chi ton khoang 0.016s; loi ich chinh la giam rui ro restore/compatibility, khong claim tiet kiem 3.6s preflight |
| 2 | Thu health interval 2s, threshold 3 kem hysteresis/circuit breaker va do false-positive rate | SRE | 2026-10-23 | Detect floor tu 15s xuong 6s, tiet kiem toi da 9s |
| 3 | Duy tri Region B o pool state `full` va continuously ready, khong chi `warm` | Capacity owner | 2026-10-23 | Loai bo warm-up khoang 6.3s tren critical path |
| 4 | Them replication sau cutover va quy trinh failback co kiem tra state B sang A | Data Platform | 2026-10-30 | Giu RPO failback trong 30s va tranh mat write o B |

## 5. Ba cau hoi bat buoc

1. `interval x threshold = 5s x 3 = 15.0s`. Detection thuc te la 14.8s do outage xay ra gan bien poll, khoang 47.0% RTO 31.5s.
2. Neu interval la 1s va van giu threshold 3, detect floor con 3s; ly thuyet RTO giam khoang 12s, xuong gan 19.5s. Doi lai la tai probe tang 5 lan, nhay hon voi network jitter va tang nguy co false positive/flapping; can hysteresis va circuit breaker.
3. `docs_lost = 4` nghia la bon documents/tickets da ingest o primary nhung chua co trong snapshot restore. Voi khach hang, cau tra loi dua tren bon documents do co the thieu hoac cu; can replay tu durable event log hoac doi soat va re-ingest truoc khi tuyen bo phuc hoi du lieu hoan toan.
