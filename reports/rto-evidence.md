# Evidence — Lab 23

Tôi chạy hai lần diễn tập trên Windows, backend filesystem, cổng A/B/edge là 18001/18002/18080. A được tạm dừng để mô phỏng server treo.

## 1. Baseline — chưa có DR

| Mốc | Kết quả | Evidence |
|---|---|---|
| A bắt đầu treo | Mốc 0 | `chaos/chaos-events.jsonl:1` |
| User thấy lỗi đầu tiên | +2.226s | `reports/drill-1-nodr.jsonl:8` |
| Phục hồi | Không phục hồi trong cửa sổ 40s; 10 request lỗi | `reports/measure-drill-1.json:25`, `reports/measure-drill-1.json:28` |

## 2. Có DR — phục hồi sang B

| Mốc | +giây từ outage | Evidence |
|---|---:|---|
| t_outage | 0.000s | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi | +3.006s | `reports/drill-2-withdr.jsonl:11` |
| Health check phát hiện | +25.613s | `reports/health-events.jsonl:2` |
| Runbook xác nhận và thông báo | +29.384s | `reports/runbook-run.jsonl:2` |
| Restore snapshot xong | +30.679s | `reports/failover-events.jsonl:2` |
| B ready | +39.512s | `reports/failover-events.jsonl:4` |
| Chuyển traffic sang B | +39.515s | `reports/failover-events.jsonl:5` |
| Request đầu tiên thành công ở B | +42.154s | `reports/drill-2-withdr.jsonl:24` |

| Chỉ số | Đo được | Mục tiêu | Kết quả | Evidence |
|---|---|---|---|---|
| RTO | 42.2s | ≤ 300s | PASS | `reports/drill-2-withdr.jsonl:24` |
| RPO | 18.02s / 9 document chưa được restore | ≤ 300s | PASS | `reports/failover-events.jsonl:2` |
| Golden signals của B | 10 request, p95 = 16ms, error rate = 0% | p95 < 1000ms, không lỗi | PASS | `reports/runbook-run.jsonl:6` |

## 3. Phân rã RTO

| Thành phần | Giây | Evidence / cách tính |
|---|---:|---|
| Detection budget: interval × threshold = 5 × 3 | 15.000s | `reports/health-events.jsonl:2` |
| Phần phát hiện vượt budget: pha polling, timeout | 10.613s | `reports/health-events.jsonl:2` − `chaos/chaos-events.jsonl:3` − 15s |
| Xác nhận sự cố và kiểm tra B | 5.033s | `reports/failover-events.jsonl:1` − `reports/health-events.jsonl:2` |
| Restore và đo RPO | 0.033s | `reports/failover-events.jsonl:2` − `reports/failover-events.jsonl:1` |
| Scale pool và chờ B ready | 8.833s | `reports/failover-events.jsonl:4` − `reports/failover-events.jsonl:2` |
| Cutover, cache TTL và chờ request tiếp theo | 2.642s | `reports/drill-2-withdr.jsonl:24` − `reports/failover-events.jsonl:4` |

Tổng = **42.154s**, làm tròn thành **42.2s** theo công cụ đo. Budget 15s là quy ước của lab; thời gian phát hiện thực tế còn phụ thuộc pha polling và timeout. Khoảng cuối không phải TTL thuần.
