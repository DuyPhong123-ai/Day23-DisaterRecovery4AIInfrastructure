"""Generate reports from actual completed drill logs: python -m dr.write_reports."""
import datetime
import json
import pathlib

from tools.measure_rto import measure


def rows(path):
    return [(i, json.loads(line)) for i, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1)
            if line.strip()]


def main():
    report = pathlib.Path("reports")
    config = json.loads((report / "drill-config.json").read_text(encoding="utf-8"))
    m = measure(report / "drill-2-withdr.jsonl", "chaos/chaos-events.jsonl",
                report / "health-events.jsonl", report / "failover-events.jsonl", 300)
    if not m["valid"] or m["warnings"] or m["rto_verdict"] != "PASS":
        raise SystemExit(f"DR drill is not clean: {m}")
    traffic = rows("reports/drill-2-withdr.jsonl")
    lo, hi = traffic[0][1]["ts"], traffic[-1][1]["ts"]
    kill = next((i, e) for i, e in rows("chaos/chaos-events.jsonl")
                if e.get("action") == "kill" and lo <= e["ts"] <= hi)
    t0 = kill[1]["ts"]
    failed = next((i, e) for i, e in traffic if e["ts"] >= t0 and not e["ok"])
    recovered = next((i, e) for i, e in traffic if e["ts"] > failed[1]["ts"] and e["ok"])
    health = next((i, e) for i, e in rows("reports/health-events.jsonl")
                  if e["region"] == "a" and e["to"] == "UNHEALTHY" and e["ts"] >= t0)
    steps = {e["step"]: (i, e) for i, e in rows("reports/failover-events.jsonl") if e["ts"] >= t0}
    notices = next((i, e) for i, e in rows("reports/runbook-run.jsonl") if e["step"] == 2 and e["ts"] >= t0)
    restore = steps["2_restore_snapshot"][1]
    rto, rpo = m["rto_measured_s"], m["rpo_at_restore_s"]
    floor = m["health_check_config"]["detect_floor_s"]
    detect_extra = health[1]["ts"] - t0 - floor
    orchestration = steps["1_verify_target"][1]["ts"] - health[1]["ts"]
    restore_time = restore["ts"] - steps["1_verify_target"][1]["ts"]
    warm = steps["4_wait_ready"][1]["ts"] - restore["ts"]
    ttl = recovered[1]["ts"] - steps["4_wait_ready"][1]["ts"]
    evidence = "# RTO/RPO Evidence — Lab 23\n\n"
    evidence += (f"Diễn tập thực tế trên {config['platform']}, backend filesystem. A bị tạm dừng bằng "
                 f"{config['suspension']} (kết nối treo đến timeout). Không sửa timestamp hoặc công cụ đo. "
                 f"Cổng A/B/edge: {'/'.join(map(str, config['ports']))}; xem `reports/drill-config.json`.\n\n")
    baseline = json.loads((report / "measure-drill-1.json").read_text())
    base_rows = rows("reports/drill-1-nodr.jsonl")
    base_kill = next((i, e) for i, e in rows("chaos/chaos-events.jsonl")
                     if e.get("action") == "kill" and base_rows[0][1]["ts"] <= e["ts"] <= base_rows[-1][1]["ts"])
    base_fail = next((i, e) for i, e in base_rows if e["ts"] >= base_kill[1]["ts"] and not e["ok"])
    evidence += f"## Drill 1 — baseline\n\n{baseline['requests_failed']} request lỗi; RTO = **{baseline['rto_verdict']}** trong cửa sổ 40 giây. Không có request thành công sau lỗi đến cuối log.\n\n"
    evidence += "| Mốc | Giá trị | Evidence |\n|---|---|---|\n"
    evidence += f"| Outage | {base_kill[1]['iso']} UTC | `chaos/chaos-events.jsonl:{base_kill[0]}` |\n"
    evidence += f"| User thấy lỗi đầu tiên | +{base_fail[1]['ts'] - base_kill[1]['ts']:.3f}s | `reports/drill-1-nodr.jsonl:{base_fail[0]}` |\n"
    evidence += "| Kết quả / số request lỗi | NO_RECOVERY | `reports/measure-drill-1.json` |\n\n"
    evidence += "## Drill 2 — timeline\n\n| Mốc | Từ outage | Evidence |\n|---|---:|---|\n"
    milestones = [("Outage", kill, "chaos/chaos-events.jsonl"),
                  ("User thấy lỗi", failed, "reports/drill-2-withdr.jsonl"),
                  ("Health checker phát hiện", health, "reports/health-events.jsonl"),
                  ("Snapshot restore xong", steps["2_restore_snapshot"], "reports/failover-events.jsonl"),
                  ("B ready", steps["4_wait_ready"], "reports/failover-events.jsonl"),
                  ("Cutover", steps["5_dns_cutover"], "reports/failover-events.jsonl"),
                  ("User được B phục vụ", recovered, "reports/drill-2-withdr.jsonl")]
    for name, (line, event), path in milestones:
        evidence += f"| {name} | {event['ts'] - t0:.3f}s | `{path}:{line}` |\n"
    evidence += f"\nRTO đo được: **{rto}s / mục tiêu 300s — PASS**. RPO: **{rpo}s, {m['docs_lost']} document chưa có trong bản restore / mục tiêu 300s — {'PASS' if rpo <= 300 else 'FAIL'}**. Phiên bản embedding: `{restore['embed_model_version']}`. Tổng {m['requests_failed']} request lỗi sau outage. Nguồn: `reports/measure-drill-2.json`, `reports/failover-events.jsonl:{steps['2_restore_snapshot'][0]}`.\n\n"
    evidence += "## Phân rã RTO (các khoảng không chồng lấn)\n\n| Thành phần | Giây | Evidence / cách đo |\n|---|---:|---|\n"
    parts = [("Detection budget theo rubric: 5 × 3", floor, f"`reports/health-events.jsonl:{health[0]}`"),
             ("Phần phát hiện vượt budget: pha polling, timeout, HTTP overhead", detect_extra,
              f"`reports/health-events.jsonl:{health[0]}` − `chaos/chaos-events.jsonl:{kill[0]}` − 15s"),
             ("Runbook xác nhận, thông báo và verify B", orchestration, f"`reports/runbook-run.jsonl:{notices[0]}` → verify target"),
             ("Snapshot restore + đo RPO", restore_time, f"`reports/failover-events.jsonl:{steps['2_restore_snapshot'][0]}` − verify target"),
             ("Scale + GPU warm-up + xác nhận state", warm, f"`reports/failover-events.jsonl:{steps['4_wait_ready'][0]}` − restore"),
             ("Cutover + cache TTL + chu kỳ request", ttl, f"`reports/drill-2-withdr.jsonl:{recovered[0]}` − ready")]
    for name, seconds, source in parts:
        evidence += f"| {name} | {seconds:.3f}s | {source} |\n"
    total = sum(p[1] for p in parts)
    evidence += f"\nTổng timestamp: {total:.3f}s, làm tròn một chữ số = {rto}s. `waited_s` riêng cho readiness = {steps['4_wait_ready'][1]['waited_s']}s. Khoảng cuối bao gồm cache và lấy mẫu request, không phải phép đo TTL thuần.\n\n"
    evidence += "Rubric gọi interval × threshold là detect floor. Chính xác hơn, đây là budget danh nghĩa: ba probe cách nhau 5 giây chỉ trải 10 giây giữa probe đầu và cuối; độ trễ từ outage còn phụ thuộc pha polling, timeout và thời gian thực hiện probe. Checker ngủ 5 giây sau mỗi vòng, nên timeout cộng vào chu kỳ thực. Runbook xác nhận độc lập trước cutover.\n"
    (report / "rto-evidence.md").write_text(evidence, encoding="utf-8")
    post = "# Postmortem — DR Drill Lab 23\n\n## Timeline\n\n| UTC | Sự kiện | Evidence |\n|---|---|---|\n"
    for name, (line, event), path in milestones:
        iso = datetime.datetime.fromtimestamp(event["ts"], datetime.timezone.utc).isoformat()
        post += f"| {iso} | {name} | `{path}:{line}` |\n"
    post += f"\nThông báo/confirm tự động cho lần chấm điểm: `reports/runbook-run.jsonl:{notices[0]}`; trễ {notices[1]['notification_delay_s']}s. Mặc định vận hành thực tế vẫn hỏi y/N.\n\n"
    post += f"## RTO/RPO và gap analysis\n\nRTO mục tiêu 300s, thực tế {rto}s; gap (thực tế − mục tiêu) = {rto - 300:.1f}s. RPO mục tiêu 300s, thực tế {rpo}s / {m['docs_lost']} document; gap = {rpo - 300:.2f}s. Baseline không có recovery; sau DR, traffic phục hồi ở B. Phân rã đầy đủ tại `reports/rto-evidence.md`.\n\n"
    post += "## Root cause — 5 whys\n\n1. Inference lỗi vì A không trả lời khi process bị tạm dừng.\n2. Người dùng vẫn vào A vì edge cache con trỏ active region.\n3. Không thể đổi sang B ngay vì B khởi đầu thiếu vector DB/weights và pool ở warm.\n4. Cần restore vì thiết kế active-passive dùng snapshot định kỳ, chưa đồng bộ liên tục.\n5. Baseline không phục hồi vì chưa có quy trình điều phối phát hiện → restore → scale → readiness → cutover. Snapshot, model version và readiness gate giải quyết khoảng trống đó.\n\n"
    post += "## Action items\n\n| Action item | Owner | Deadline | Tác động dự kiến |\n|---|---|---|---|\n| Dùng persistent HTTP client và đo lại chu kỳ polling | SRE | 2026-10-12 | Giảm overhead HTTP; phải đo trước khi cam kết số giây |\n| Thử snapshot mỗi 10s và kiểm tra backup nhất quán | Data engineer | 2026-10-13 | Giảm cửa sổ lag danh nghĩa từ 30s xuống 10s; tăng I/O |\n| Thử B có state sẵn và pool full | ML platform | 2026-10-16 | Có thể giảm warm-up 6s; tăng chi phí standby |\n| Diễn tập failback với đồng bộ dữ liệu từ B về A | Incident commander | 2026-10-16 | Tránh mất write mới ở B và flapping |\n\n"
    post += f"## Câu hỏi bắt buộc\n\n1. Budget detection = 5 × 3 = 15s, chiếm {floor / rto * 100:.1f}% RTO. Đây là quy ước của rubric; detection thực tế được đo từ health log. Muốn RTO 300s, phải dành budget cho restore/warm-up/cache: interval ≤ (300 − budget còn lại)/3, không dùng hết 100s cho interval.\n2. Interval 1s cho budget 3s, giảm danh nghĩa 12s; không đảm bảo RTO giảm đúng 12s vì timeout/pha polling. Probe tăng 5 lần, dễ gặp ba lỗi ngắn liên tiếp hơn; giữ threshold và yêu cầu xác nhận để hạn chế flapping.\n3. Nếu A mất dữ liệu vĩnh viễn, {m['docs_lost']} document này không có trong snapshot đã restore và cần nhập lại từ nguồn bền vững nếu có. Outage 6 giờ không tự đồng nghĩa mất 6 giờ dữ liệu; RPO phản ánh khoảng trống trước snapshot. Lab vẫn đọc được file SQLite của A để đo docs_lost; thực tế cần watermark/audit log độc lập nếu A mất hoàn toàn.\n\n"
    post += "## Giới hạn và bài học\n\nĐây là local mock inference, không đo GPU thật hoặc DNS thật. Windows suspension được ghi rõ trong drill-config và chaos log. Ingest và replication là process riêng, vẫn truy cập SQLite trên máy local khi serving A treo: docs_lost được đo tại thời điểm restore, gồm cả write sau outage, không mô phỏng mất toàn bộ ổ đĩa region A. Health checker chạy ngoài serving process nên vẫn phát hiện khi A treo. Muốn chứng minh RTO 5 phút, dùng raw loadgen + chaos log, công cụ measure_rto và báo cáo evidence, không lấy số mẫu từ GUIDE. Replication hiện sao chép SQLite trực tiếp; production cần snapshot nhất quán/SQLite backup API hoặc database-native backup và object-store độc lập. Không auto-failback.\n"
    (report / "postmortem.md").write_text(post, encoding="utf-8")


if __name__ == "__main__":
    main()
