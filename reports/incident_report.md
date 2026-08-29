# Incident Report

**Sự cố:** Doanh thu trên CEO dashboard tụt 77% mà không ai báo lỗi, kèm theo support agent trả lời sai chính sách refund
**Ngày:** 2026-08-29 (UTC)
**Người viết:** Nguyễn Hùng Phát — 2A202601094
**Trạng thái:** Đã khôi phục, còn 6 action item

## Severity

P1.

Hai thứ khách hàng nhìn thấy được đều sai cùng lúc: số doanh thu báo cáo cho ban lãnh đạo thiếu 77%, và support agent tư vấn refund theo bản policy đã cũ 3 tiếng. Vấn đề là không có gì đỏ cả — `dbt build` chạy xong `PASS=28, ERROR=0`. Nghĩa là thời gian phát hiện phụ thuộc hoàn toàn vào lớp observability, chứ không có job nào fail để mà cảnh báo.

## Summary

Batch hôm đó chỉ vào 150 dòng order thay vì 600, mất 75%. Nhưng 150 dòng vào được thì dòng nào cũng hợp lệ, nên không có contract check nào fail, không `not_null` nào fail, không dbt test nào fail. `fct_daily_revenue` được build lại rất "đúng" từ một input thiếu, và ra **$4,308.42 trên 66 completed order**, trong khi batch khỏe mạnh là **$18,961.04 trên 290 order**. Thiếu 77.3%.

Song song đó, timestamp `published_at` của knowledge base bị lùi 3 tiếng, làm RAG index phục vụ bản refund policy đã bị thay thế.

Điều đáng nói không phải là hai cái lỗi này, mà là loại lỗi: **dữ liệu thiếu thì validation theo dòng không nhìn thấy được**. Mọi cổng kiểm tra trong pipeline đều đang hỏi "dòng này có đúng không?", không cổng nào hỏi "đã đủ dòng chưa?".

## Detection

| | Mất volume | KB cũ |
|---|---|---|
| Tín hiệu | `row_count_anomaly` | freshness trên `kb_documents.published_at` |
| Detector | `auto:all_history:mad`, score 10.29 (ngưỡng 3.0) | 190.1 phút so với giới hạn 60 phút |
| Thấy lần đầu | baseline run ngay sau khi ingest | cùng run đó |
| Do đâu báo | `scripts/run_baseline.py` | `scripts/run_baseline.py` |
| Pipeline quyết định | `WARN` | `WARN` |

Phần quan trọng hơn là danh sách những thứ **không** kêu:

- `dbt build` — `PASS=28, WARN=0, ERROR=0`
- contract của orders — 0/19 check fail
- `unique` / `not_null` / `accepted_values` / `range` — xanh hết
- drift phân phối `amount` — im lặng, và im lặng đúng: 150 dòng còn lại là mẫu đại diện tốt, số tiền chưa bao giờ sai, chỉ số lượng sai

## Root Cause

**Lỗi 1 — ingest thiếu.** Extract phía upstream trả về 150 dòng đầu rồi kết thúc, không báo lỗi. Loader coi file ngắn là file đủ: nó không có contract về volume kỳ vọng, không đối chiếu row count với manifest, không kiểm tra watermark liên tục. Với nó thì một file bị cắt và một ngày ít đơn trông y hệt nhau.

**Lỗi 2 — timestamp KB đi lùi.** `published_at` của mọi document lùi 3 tiếng, nên document "mới nhất" mà RAG index coi là hiện hành lại cũ hơn policy đang thực sự có hiệu lực. Chỗ này còn có một lớp nữa: `kb_contract.yaml` khai rule dưới key `fields:` chứ không phải `columns:`, mà validator ban đầu chỉ đọc `columns:`. Tức là KB có vẻ ngoài của việc được contract bảo vệ, nhưng thực tế 0 rule nào được chạy.

## Evidence

1. **Anomaly volume trên baseline robust.** `detect_metric(150, history_28d, method="auto")` → `is_anomaly=True`, score 10.29, method `auto:all_history:mad`. History 28 ngày có tính mùa vụ (ngày thường ~600, cuối tuần ~250), detector so 150 với đúng cụm mà nó thuộc về, nên một cuối tuần bình thường ~250 vẫn im còn 150 thì không.

2. **Mart được build sạch từ input bẩn.** `dbt build` → `PASS=28`. Query `select sum(daily_revenue), sum(completed_order_rows) from fct_daily_revenue` ra **4308.42 / 66**, so với **18961.04 / 290** ở batch khỏe.

3. **Contract im lặng là do thiết kế, không phải do hỏng.** 19 check, 0 fail: cả 150 dòng đều thỏa mọi rule. Đây chính là bằng chứng cho việc validation theo dòng không diễn tả được một invariant về volume.

4. **KB quá hạn freshness.** `age_minutes=190.10; max_delay_minutes=60.00`, severity `warning` → action `warn`.

5. **Error budget.** Không có critical breach nào nên SLI contract vẫn 100%, và policy burn-rate không page — đúng theo luật đang cấu hình. Nhưng nó cũng có nghĩa là sự cố volume này hoàn toàn vô hình với SLO hiện tại. Đã ghi thành action item bên dưới.

## Blast Radius

Mức dataset, theo `data/baseline/lineage_graph.json`:

```text
raw_orders (150/600 dòng)
-> stg_orders
-> fct_daily_revenue        [thiếu 77.3%]
-> ceo_revenue_dashboard    [khách hàng thấy: báo cáo cho ban lãnh đạo]

kb_documents (cũ 3 tiếng)
-> kb_active_docs
-> rag_index
-> support_agent            [khách hàng thấy: tư vấn sai refund policy]
```

Mức cột — tức là *con số nào* sai, chứ không chỉ bảng nào bị chạm:

```text
raw_orders.amount
-> stg_orders.amount_usd
-> fct_daily_revenue.daily_revenue
-> ceo_revenue_dashboard.revenue
```

Hai consumer bị ảnh hưởng, cả hai đều là mặt khách hàng nhìn thấy. `stg_customers` và mọi thứ dưới nó không bị đụng, nên không cần rebuild customer dimension.

## Mitigation

1. Giữ batch lại, không promote. Pipeline chỉ ra `WARN`, tôi nâng lên hold thủ công sau khi xác nhận anomaly volume.
2. Gắn ghi chú lên CEO dashboard: "dữ liệu chưa đủ, đừng đọc số doanh thu hôm nay". Thà để trống còn hơn để một con số trông rất hợp lý nhưng sai.
3. Chạy lại ingest từ nguồn (`make reset`) để lấy đủ 600 dòng.
4. Publish lại KB để `published_at` phản ánh đúng thời điểm publish thật.

## Recovery

`make reset && make baseline && make dbt`:

- orders về **600** dòng, anomaly `False` (score 0.17)
- contract orders 0/19 fail, contract KB 0/14 fail
- KB freshness **10.0 phút**, nằm trong giới hạn 60
- `dbt build` → `PASS=28`
- `fct_daily_revenue` → **18961.04 / 290 dòng**
- pipeline `PASS`, error budget 100%

## Verification

- [x] Contract healthy — 0/19 check của orders và 0/14 của KB đang fail
- [x] dbt tests healthy — `PASS=28, ERROR=0`, gồm cả 4 unit test
- [x] Anomaly về vùng bình thường — score 10.29 → 0.17
- [x] SLO healthy / hiểu rõ budget — 100% budget, burn 0.00x cả hai cửa sổ, không page
- [x] Downstream verified — doanh thu và số dòng completed **khớp chính xác** với baseline khỏe, chứ không phải "nhìn cũng hợp lý"

## Prevention / Action Items

| Action | Owner | Deadline | Why |
|---|---|---|---|
| Thêm contract về volume kỳ vọng cho `orders` (row count nằm trong dải của cùng thứ trong tuần) và fail batch nếu thấp hơn | commerce-data | 2026-09-05 | Đây là cổng duy nhất có thể *chặn* batch này ngay, thay vì cảnh báo sau khi đã build xong |
| Đối chiếu row count mỗi batch với manifest của nguồn trước khi promote | commerce-data | 2026-09-12 | Biến một lần cắt file im lặng thành lỗi cứng, xác định, ngay tại biên |
| Đưa `row_count_anomaly` vào SLI contract để sự cố volume có tiêu tốn error budget | commerce-data | 2026-09-12 | Một P1 mà budget vẫn 100% nghĩa là SLI đang đo sai thứ |
| Nâng severity freshness của KB từ `warning` lên `critical` | support-ai | 2026-09-05 | Policy refund cũ là thông tin sai mà khách hàng đọc trực tiếp; `warn` là đánh giá nhẹ tay. Lần này tôi chưa đổi để giữ nguyên contract fixture như đề giao, ghi lại đây như một câu hỏi mở |
| Kiểm tra `published_at` phải tăng đơn điệu theo `doc_id` | support-ai | 2026-09-19 | Freshness bắt được triệu chứng, monotonic mới bắt được cái regression gây ra nó |
| Cảnh báo theo tỉ lệ check *được chạy*, không chỉ check fail | commerce-data | 2026-09-19 | KB chạy 0 rule suốt một thời gian mà vẫn trông khỏe. Zero check không bao giờ được phép hiển thị như pass |

## Rút ra được gì

`SUCCESS` là câu nói về pipeline, không phải về dữ liệu. Ba lớp bảo vệ độc lập cùng báo khỏe trên một batch thiếu 75%, đơn giản vì lớp nào cũng chỉ soi những dòng đang có mặt. Completeness, freshness và volume là các invariant riêng, tách khỏi validity, và chúng cần cổng riêng của mình.

Bài học thứ hai âm thầm hơn: một contract parse được nhưng không match gì cả thì nguy hiểm hơn là không có contract, vì nó cho ta sự yên tâm mà không cho ta độ phủ.
