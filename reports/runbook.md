# Runbook — A down, chuyển sang B

**Mục tiêu:** RTO/RPO ≤ 300s. Chạy từ thư mục gốc repo khi stack đang hoạt động. Các lệnh dưới dùng cổng của lần diễn tập: A/B/edge = 18001/18002/18080.

**Chuẩn bị trước outage:** đặt URL trong terminal chạy checker/runbook; replication phải có ít nhất một snapshot thành công.

```powershell
$env:REGION_A_URL = 'http://127.0.0.1:18001'
$env:REGION_B_URL = 'http://127.0.0.1:18002'
```

Chạy hai lệnh sau ở hai terminal riêng (terminal checker cần đặt URL như trên):

```powershell
python state/replicate.py --region a --every 30 --duration 300 --backend fs
python dr/health_checker.py --interval 5 --threshold 3 --duration 300 --out reports/health-events.jsonl
```

Lệnh bước 2 tự thực hiện restore → scale → chờ ready → cutover → kiểm tra. Các bước 3–6 chỉ kiểm tra kết quả, không gọi lại failover.

| # | Bước | Lệnh | Biết xong khi | Ai | Dừng/rollback khi |
|---|---|---|---|---|---|
| 1 | Xác nhận outage | `Get-Content reports/health-events.jsonl -Tail 5`; `curl.exe -f http://127.0.0.1:18002/healthz` | Có event mới A UNHEALTHY sau 3 fail; B còn sống | On-call | A còn ready hoặc B cũng down |
| 2 | Mở incident, xác nhận failover | `python dr/runbook.py --primary a --target b --backend fs` rồi nhập `y` | Log thong_bao_incident, confirmed=true | Incident commander | Chưa xác nhận đúng sự cố: nhập N |
| 3 | Kiểm tra restore | `Get-Content reports/failover-events.jsonl -Tail 5` | 2_restore_snapshot có RPO, docs_lost, model version | Data engineer | Thiếu snapshot hoặc restore lỗi: abort |
| 4 | Kiểm tra pool và readiness | `curl.exe -f http://127.0.0.1:18002/readyz` | HTTP 200, ready=true, pool_state=full | ML platform | B chưa ready sau 60s: không cutover |
| 5 | Kiểm tra chuyển traffic | `curl.exe -f http://127.0.0.1:18080/edge/state` | active_region=b sau cache TTL 5s | On-call | Edge vẫn trỏ A: kiểm tra log và cache |
| 6 | Kiểm tra phục vụ | `Get-Content reports/runbook-run.jsonl -Tail 2`; `curl.exe -f http://127.0.0.1:18080/v1/infer` | 10 request B không lỗi, p95 < 1000ms; edge trả region=b | On-call | Còn lỗi: mở incident, đánh giá failback |
| 7 | Đo và ghi nhận | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | valid=true, warnings=[], RTO PASS, RPO có số | Incident commander | Thiếu log hoặc vượt mục tiêu: chưa đóng incident |

**Rollback:** không tự động chuyển ngược. Incident commander quyết định; Data engineer xác nhận dữ liệu. Chỉ trả traffic về A khi A ready ổn định qua 3 probe cách 5s, đủ weights/vector DB/model version và đã đồng bộ các write mới từ B. Nếu chưa đạt, tiếp tục giữ B và xử lý sự cố. Không restore snapshot A cũ làm mất dữ liệu mới của B.

**Chạy lại diễn tập Windows:** `python -m dr.drill --port-offset 10000`. Runner tự dừng các process diễn tập khi kết thúc; kiểm tra báo cáo bằng `python -X utf8 -m pytest tests/test_rto_evidence.py -v`.
