# Postmortem — Lab 23

Tôi mô phỏng A treo và phục hồi dịch vụ sang B. Baseline không tự phục hồi; lần có DR phục hồi sau **42.2s**. Báo cáo tập trung vào khoảng trống của hệ thống và cách cải thiện.

## 1. Timeline

Ngày 09/10/2026, giờ Việt Nam/Thái Lan (UTC+7); làm tròn đến mili giây.

| Thời gian | Sự kiện | Evidence |
|---|---|---|
| 12:43:54.483 | A bắt đầu treo | `chaos/chaos-events.jsonl:3` |
| 12:43:57.489 | User thấy lỗi đầu tiên | `reports/drill-2-withdr.jsonl:11` |
| 12:44:20.097 | Health checker báo A UNHEALTHY | `reports/health-events.jsonl:2` |
| 12:44:23.867 | Runbook xác nhận và thông báo | `reports/runbook-run.jsonl:2` |
| 12:44:25.162 | Restore snapshot xong | `reports/failover-events.jsonl:2` |
| 12:44:33.996 | B ready | `reports/failover-events.jsonl:4` |
| 12:44:33.998 | Chuyển traffic sang B | `reports/failover-events.jsonl:5` |
| 12:44:36.637 | Request đầu tiên thành công ở B | `reports/drill-2-withdr.jsonl:24` |

Lần chấm điểm dùng --auto; vận hành mặc định vẫn yêu cầu xác nhận y/N.

## 2. Gap analysis

| Chỉ số | Mục tiêu | Đo được | Gap: đo được − mục tiêu |
|---|---|---|---|
| RTO | 300s | 42.2s | −257.8s, đạt |
| RPO | 300s | 18.02s / 9 document | −281.98s, đạt |

Phát hiện outage tốn **25.613s**, lớn nhất trong RTO. Snapshot restore chỉ khoảng **0.033s**; tôi ưu tiên cải thiện polling và timeout trước. B có 10/10 request thành công, p95 **16ms** (`reports/runbook-run.jsonl:6`). Các khoảng đo được đối chiếu trong bảng phân rã của báo cáo evidence.

## 3. Root cause — 5 whys

1. User bị lỗi vì A ngừng trả lời request.
2. Request vẫn đến A vì edge chưa đổi active region và còn cache.
3. Không thể chuyển ngay sang B vì B thiếu dữ liệu/weights và pool chưa full.
4. B cần restore và warm-up vì lab dùng mô hình active-passive, backup định kỳ.
5. Baseline không phục hồi vì thiếu quy trình phát hiện và điều phối failover. Cách khắc phục là health check độc lập, snapshot đầy đủ và chỉ cutover sau readiness.

## 4. Action items

| Action item | Owner | Deadline | Mục đích |
|---|---|---|---|
| Tái sử dụng HTTP client, đo lại polling/timeout | SRE | 12/10/2026 | Giảm thời gian phát hiện |
| Thử snapshot mỗi 10s, kiểm tra tính nhất quán | Data engineer | 13/10/2026 | Giảm replication lag |
| Thử B có state sẵn và pool full | ML platform | 16/10/2026 | Giảm thời gian restore/warm-up |
| Diễn tập failback, đối chiếu write mới ở B | Incident commander | 16/10/2026 | Tránh mất dữ liệu khi trả về A |

## 5. Bài học

- Budget detection 5 × 3 = **15s**, khoảng **35.5% RTO**; thực tế còn timeout và pha polling. RTO 300s phải dành thêm thời gian cho restore, warm-up và cache.
- Giảm interval xuống 1s làm budget còn 3s, giảm danh nghĩa 12s nhưng tăng số probe 5 lần; cần cân nhắc báo động do lỗi ngắn và giữ bước xác nhận.
- Nếu A mất dữ liệu vĩnh viễn, 9 document chưa restore cần nhập lại từ nguồn bền vững nếu có; outage 6 giờ không đồng nghĩa mất 6 giờ dữ liệu.

**Giới hạn:** lab tạm dừng serving A, còn ingest/replication vẫn đọc ghi SQLite local. RPO được đo tại lúc restore; đây chưa phải mô phỏng mất toàn bộ dữ liệu của một region. GPU và DNS cũng được mô phỏng.
