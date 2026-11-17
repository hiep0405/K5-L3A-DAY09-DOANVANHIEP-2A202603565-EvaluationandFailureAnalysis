# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 11   # đối chứng
```

## Bảng phân tích chi tiết

| Trường | Nội dung |
|---|---|
| Trial | `seeded_regression C-002@11`, run ID: `R-seeded_regression-C-002-s11` |
| Triệu chứng | Grader `RefundAmountCorrect` và `TrialPass` fail. Status trả về `resolved` trong khi kỳ vọng `denied`. Ledger ghi nhận hoàn `20.000đ` (nhãn đúng là `0đ`). Reply thông báo: *"Dạ, LogiX đã hoàn 20.000đ cho đơn O-202."* |
| Bước sai đầu tiên | **Event 3 (`kind: model_override`)**: `payload: {"delay_days": 5}` |
| Vì sao là bước *đầu tiên* | Event 1 (`handoff`) và Event 2 (`investigator: lookup_order`) hoàn toàn đúng và truy vấn chính xác thông tin từ DB (`order_id: O-202, delay_days: 1`). Chỉ đến Event 3, agent mới tin vào lời khai trong ticket của khách hàng (*"Đơn giao trễ 5 ngày..."*) và ghi đè dữ liệu DB `delay_days` từ 1 thành 5, dẫn tới Event 4 tính quote sai 20.000đ. |
| Failure mode | **FM-03** (`ticket_trust — tin ticket hơn DB`) |
| Đối chứng | Cùng claim C-002@11 trên shape `fixed`: Không hề có sự kiện `model_override`. Ngay sau bước tra cứu đơn hàng, agent dùng đúng số ngày trễ trong DB (1 ngày) và sinh Event 3: `quote {"amount_vnd": 0, "reason": "no_entitlement"}`; Event 4: reply từ chối bồi thường chính xác. |
| Regression test | `test_regression_c002_model_override` trong `tests/test_lab9_regression.py`. Sử dụng assertion kiểm tra `DbFactKept` và `RefundAmountCorrect`. Test này FAIL trên `seeded_regression` và PASS trên `fixed`. |
| Chưa chắc chắn | Trace chỉ ghi nhận agent thực hiện `model_override`, không chứng minh được prompt cụ thể bên trong MockLLM đã kích hoạt quyết định này như thế nào nếu không kiểm tra log prompt chi tiết. |

## Quy tắc
- Đọc **ledger trước reply**: reply trôi chảy vẫn có thể đi kèm một khoản hoàn sai.
- "Bước sai đầu tiên" phải dẫn số event cụ thể, không phải "agent bị nhầm".
- Case chỉ hoàn thành khi có regression test chạy được: đã thêm vào `tests/test_lab9_regression.py`.
