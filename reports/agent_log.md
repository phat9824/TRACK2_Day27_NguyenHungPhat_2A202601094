# AI Agent Decision Log

Nguyễn Hùng Phát — 2A202601094. Agent dùng: Claude Code.

Ghi lại các quyết định đáng kể thôi, không copy hội thoại. Nguyên tắc tôi tự đặt
cho lab này: mọi thứ agent nói đã sửa xong đều phải chạy lại lệnh để tự nhìn thấy
kết quả, không tin mô tả của nó.

## 1. Contract validator: type nghiêm, freshness cần mốc thời gian rõ ràng

- **Giả thuyết:** `pd.to_numeric(..., errors="coerce")` đang giấu type drift — một
  schema hỏng bị biến thành một đống null và trôi tiếp xuống dưới. Còn freshness
  thì không thể tính nếu không biết lấy mốc nào làm "bây giờ".
- **Yêu cầu cho agent:** thêm type validation, freshness và severity → action vào
  `src/contract_validator.py`.
- **Agent đề xuất:** predicate riêng cho từng kiểu, không coerce ngầm; freshness so
  với `reference_time` (ưu tiên tham số, sau đó tới `freshness.reference_time` trong
  contract); map `severity -> action` kèm hàm `determine_action`.
- **Bằng chứng:** `pytest tests_public -q` → `10 passed`. Inject `duplicate_pk` →
  `critical unique order_id: duplicate_rows=6`, action `QUARANTINE`.
- **Kết luận:** Accept, nhưng sửa một chỗ.
- **Sửa chỗ nào và tại sao:** bản đầu agent để freshness mặc định lấy
  `datetime.now()`. Chạy thử thì `test_healthy_contract_passes_starter_checks` đỏ
  ngay, vì fixture của bài đề ngày 2026-08-28 nên lúc nào chạy nó cũng "cũ" cả.
  Giờ nếu không ai đưa mốc thời gian thì bỏ qua freshness, còn `run_baseline.py`
  truyền `reference_time=now` một cách tường minh. Pipeline thật vẫn bắt được
  `stale_kb` (190 phút > 60 phút), mà một fixture cũ thì không bị phán là hỏng chỉ
  vì hôm nay mới chạy test.

## 2. Contract của KB thực ra không kiểm tra gì cả

- **Giả thuyết:** `stale_kb` không bị bắt vì lý do cấu trúc, không phải vì ngưỡng
  đặt sai.
- **Yêu cầu cho agent:** giải thích tại sao `make baseline` không phản ứng gì với
  `inject_fault.py stale_kb`.
- **Agent trả lời:** `kb_contract.yaml` khai rule dưới `fields:`, validator chỉ đọc
  `columns:`, và bản thân `run_baseline.py` cũng chưa bao giờ validate KB.
- **Bằng chứng:** trước — `stale_kb` không sinh check fail nào. Sau khi đọc cả hai
  key và nối KB vào baseline — `warning freshness published_at: age_minutes=190.10;
  max_delay_minutes=60.00`, action `WARN`.
- **Kết luận:** Accept.
- **Tại sao:** đây là kiểm chứng cơ chế chứ không phải vá triệu chứng. Một contract
  parse được nhưng không match gì thì tệ hơn không có contract, vì nó cho cảm giác
  an toàn mà không có độ phủ. Đã ghi thành action item: cảnh báo theo số check
  *được chạy*, chứ không chỉ số check fail.

## 3. Bác đề xuất đầu tiên về seasonality

- **Giả thuyết ban đầu của tôi:** chỉ cần đổi sang baseline robust (MAD) là đủ để
  một ngày cuối tuần bình thường không bị báo động.
- **Yêu cầu cho agent:** làm `method="auto"` xử lý được tính mùa vụ.
- **Agent đề xuất:** bỏ z-score dùng MAD, và ưu tiên `same_segment_history` nếu
  caller có truyền.
- **Bằng chứng:** tôi viết check trước khi tin. History 4 tuần (ngày thường ~600,
  cuối tuần ~250), giá trị hiện tại 250 vào thứ Bảy. MAD vẫn kêu — vì 8 giá trị
  cuối tuần trong 28 điểm thì median rơi vào ~598 và MAD chỉ ~10, nên một thứ Bảy
  hoàn toàn bình thường ăn score **33.5**. Đề xuất sai, và chỉ có bài test mới lộ ra.
- **Kết luận:** Reject, rồi làm lại.
- **Làm lại thế nào:** thêm regime check kiểu k-nearest-neighbour. Nếu một giá trị
  rơi vào giữa một cụm hàng xóm gần và chặt trong lịch sử, thì đó là mức mà metric
  vẫn đạt tới đều đặn, không phải sự cố. Một cú tụt lẻ loi trong quá khứ không giả
  được thành "cụm", vì khi không đủ hàng xóm gần thì k phải với sang các giá trị xa
  và cụm không còn chặt nữa. Kết quả: cuối tuần bình thường `False`, tụt còn 20
  `True`, `volume_drop` còn 150 `True` (score 10.29).

## 4. Cái floor, sau khi tự tạo ra một false positive

- **Giả thuyết:** chỉ còn mỗi trường hợp MAD = 0 là chưa xử lý.
- **Yêu cầu cho agent:** xử lý history mà phần lớn giá trị giống hệt nhau.
- **Agent đề xuất:** dùng "practical scale" bằng 1% khi MAD = 0.
- **Bằng chứng:** vẫn kêu với một dao động 0.1% trên metric cỡ hàng trăm. Thay bằng
  cascade ước lượng độ phân tán (MAD → IQR → std) *cộng thêm* một sàn bằng 1% mức
  của chính metric, áp cho mọi score robust.
- **Kết luận:** Accept bản sửa.
- **Tại sao:** hóa ra có hai bug nấp trong cùng một nhánh: không có ước lượng scale,
  và không có ngưỡng "lệch bao nhiêu thì mới đáng quan tâm". Không ai nên bị gọi
  dậy vì metric phẳng lì đổi 0.5%, và `inf` thì không phải một con số ai triage được.

## 5. dbt: viết unit test trước, sửa model sau

- **Giả thuyết:** khách hàng có nhiều dòng SCD đang active sẽ làm phồng doanh thu
  qua cái join trong `fct_daily_revenue`, mà SQL không báo lỗi gì.
- **Yêu cầu cho agent:** viết test nhỏ nhất phơi được lỗi này ra.
- **Agent đề xuất:** một unit test với đúng một order 100.0 và hai version active
  của cùng một customer; rồi dùng `row_number()` theo `valid_from desc` để rút
  dimension về một dòng mỗi customer trước khi join.
- **Bằng chứng:** tôi chạy unit test đó trên **model gốc chưa sửa** trước đã:
  `FAIL 1 duplicate_active_customer_versions_do_not_inflate_revenue`, còn ba unit
  test kia vẫn pass — tức là nó phơi đúng một lỗi chứ không phải fixture viết sai.
  Sau khi sửa model: `dbt build` → `PASS=28, ERROR=0`.
- **Kết luận:** Accept.
- **Tại sao:** một cái test chưa từng được nhìn thấy fail thì chưa chứng minh được
  gì. Cho nó chạy trên model hỏng mới biến nó thành bằng chứng.

## 6. Chỉnh lại baseline cho cảnh báo volume

- **Giả thuyết ban đầu:** cứ so với cùng thứ trong tuần là chuẩn nhất.
- **Yêu cầu cho agent:** baseline đang khỏe mà vẫn báo động, detector sai chỗ nào?
- **Agent đề xuất:** nới ngưỡng.
- **Bằng chứng:** không đồng ý — nới ngưỡng là đánh đổi bằng việc bỏ sót sự cố thật.
  Nguyên nhân thật ra là: `data/incoming/orders.csv` luôn phát đúng 600 dòng mỗi
  ngày, trong khi history thì có mùa vụ. Chạy vào thứ Bảy thì "600 so với các thứ
  Bảy (~250)" là **artifact của fixture**, không phải anomaly. Kiểm lại trên full
  history: 600 khỏe → `False` (0.17), `volume_drop` 150 → `True` (10.29), một cuối
  tuần thật 250 → `False`.
- **Kết luận:** sửa lại — cảnh báo chạy trên full history 28 ngày, còn so sánh cùng
  thứ giữ lại nhưng dán nhãn diagnostic.
- **Tại sao:** nới ngưỡng thì đổi một false positive lấy nhiều false negative. Sửa
  baseline thì hết false positive mà vẫn giữ nguyên độ nhạy. Còn câu "tổng thể bình
  thường, nhưng cao so với một thứ Bảy" thì vẫn đáng cho người trực nhìn thấy, nên
  tôi giữ lại chứ không xóa.

## 7. Drift phân phối và RAG mà không thêm dependency

- **Giả thuyết:** so sánh mean ratio thì mù với mọi dịch chuyển giữ nguyên trung bình.
- **Yêu cầu cho agent:** cải thiện `detect_distribution_shift`.
- **Agent đề xuất:** `scipy.stats.ks_2samp`.
- **Bằng chứng:** `scipy` không nằm trong `requirements.txt` — nó chỉ có mặt vì
  Great Expectations kéo theo. Import thẳng như vậy thì module sẽ chết trên bất kỳ
  môi trường nào cài đúng theo file requirements. Nên tôi viết thống kê KS và
  p-value tiệm cận bằng numpy, ghép thêm PSI. Kiểm: hai phân phối giống nhau
  `False`; cùng mean nhưng phương sai gấp 6 `True`; tách đôi quanh cùng mean `True`;
  case mean lệch 20x của bài `True`.
- **Kết luận:** Accept bản sửa.
- **Tại sao:** một dependency không khai báo là một lần deploy hỏng đang chờ môi
  trường sạch. Cùng lý do đó mà `approximate_embedding_norms` dùng L2 norm của
  vector term-frequency: không cần tải model nào, và khi thay bằng norm thật từ
  `model.encode(...)` thì mọi detector phía dưới không phải sửa một dòng.

## Tổng kết kiểm chứng

| Kiểm tra | Lệnh | Kết quả |
|---|---|---|
| Test công khai + test tôi thêm | `pytest tests_public -q` | 32 passed |
| dbt | `dbt build` | PASS=28, ERROR=0 |
| Unit test chạy trên model gốc | `dbt test --select fct_daily_revenue` | FAIL 1 (đúng như mong đợi) |
| Great Expectations | `python gx/validate_orders.py` | 25 expectation, 0 fail, action PASS |
| Dashboard | `streamlit.testing.v1.AppTest` | render được, không exception |
| `duplicate_pk` | `make baseline` | QUARANTINE, burn 30.30x, PAGE critical |
| `volume_drop` | `make baseline` | anomaly True, score 10.29 |
| `stale_kb` | `make baseline` | KB freshness 190.1 phút > 60, WARN |
