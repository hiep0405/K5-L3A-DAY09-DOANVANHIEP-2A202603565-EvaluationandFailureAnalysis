# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5`

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | Đạt (κ=0.757 ≥ 0.6, P=0.857 ≥ 0.8, R=0.800 ≥ 0.7) | dùng |
| helpful | 100 | 0.375 | 1.000 | 0.300 | Không đạt (κ=0.375 < 0.6, R=0.300 < 0.7) | loại |

## ≥5 case audit thủ công

| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú & Đề xuất sửa rubric |
|---|---|---|---|---|---|
| 1 | H-008 | PASS | FAIL | Người đúng | Câu trả lời *"Em đã hoàn tiền cho anh rồi nha, anh check giúp em"* quá suồng sã trong giao tiếp dịch vụ chính thức. Judge nhầm lẫn giữa thân thiện và suồng sã. Cần sửa rubric judge: bổ sung quy tắc cấm xưng hô thân mật tự phát (`anh/em`, `nha`). |
| 2 | H-017 | PASS | FAIL | Người đúng | *"Dạ, bạn ơi LogiX đã hoàn 25.000đ cho đơn O-606 rồi nha"* dùng đại từ *"bạn ơi"* và kết thúc bằng *"nha"*. Rubric cần quy định rõ phong cách giao tiếp doanh nghiệp (CSKH tiêu chuẩn xưng LogiX - quý khách/anh/chị). |
| 3 | H-029 | PASS | FAIL | Người đúng | *"Dạ, vui lòng chờ. Hệ thống đang xử lý, không cần nhắn thêm"* có sắc thái cộc cằn, gạt bỏ khách hàng. Cần thêm tiêu chí vào rubric: cấm các mệnh lệnh tiêu cực gây khó chịu cho người dùng. |
| 4 | H-042 | FAIL | PASS | Người đúng | *"Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ"* là câu trả lời chuẩn mực, súc tích. Judge phạt sai vì cho rằng câu quá ngắn. Cần nới rubric: câu ngắn gọn đủ ý không bị tính là lỗi tone. |
| 5 | H-051 | FAIL | PASS | Người đúng | *"Dạ, LogiX đã refund 90.000đ cho đơn O-202 ạ"* dùng từ mượn *"refund"* phổ biến trong thương mại điện tử. Judge quá khắt khe về từ ngữ mượn. Rubric nên cho phép các thuật ngữ quen thuộc (refund, ship, voucher). |

## Kết luận
- **Tiêu chí bị loại và lý do**: Tiêu chí `helpful` bị **loại bỏ hoàn toàn** khỏi bộ kiểm định release gate vì có Recall cực kỳ thấp (chỉ đạt 0.300, ma trận nhầm lẫn: `tp=9, fp=0, fn=21, tn=70`). Judge này bỏ lọt tới 21/30 ca không hữu ích (70% lỗi bị lọt lưới). Chỉ số Cohen's $\kappa = 0.375$ thấp hơn nhiều so với ngưỡng tối thiểu 0.6. Nếu dùng `helpful` làm gate, agent có thể đưa ra câu trả lời vô nghĩa hoặc không hỗ trợ người dùng mà hệ thống vẫn tự động cho qua.
- **Rủi ro còn lại**: Dù `tone_ok` đạt chuẩn trên tập replay công khai, việc đánh giá vẫn phụ thuộc vào bộ phân phối câu mẫu hiện tại. Trong môi trường thực tế, người dùng có nhiều cách diễn đạt và khiếu nại phức tạp hơn, do đó cần liên tục thu thập mẫu hội thoại thật của khách hàng để tạo tập held-out mới nhằm tái hiệu chuẩn (re-calibration) định kỳ.
