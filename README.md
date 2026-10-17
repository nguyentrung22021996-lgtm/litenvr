# LiteNVR cho ZimaOS (Tích hợp Home Assistant Motion)

Ứng dụng NVR độc lập siêu nhẹ (CPU < 1%), ghi hình trực tiếp RTSP (`-c:v copy`) và nhận tín hiệu Motion từ Home Assistant.

## Đăng nhập web
Web UI được bảo vệ bằng HTTP Basic Auth. Cấu hình qua biến môi trường:
- `NVR_USER` (mặc định `admin`)
- `NVR_PASS` (mặc định `admin`) — **nên đổi ngay**
- `NVR_AUTH=off` để tắt đăng nhập (chỉ dùng trong LAN tin cậy)

## Liên kết Home Assistant (lấy Motion bằng Token)
Trên Web UI, bấm **🛰️ Home Assistant**:
1. Nhập **Địa chỉ HA** (VD `http://homeassistant.local:8123`).
2. Dán **Long-Lived Access Token** (HA → Hồ sơ người dùng → Security → Create Token). Token được lưu trong `config.json` và chỉ dùng ở phía server.
3. Bấm **↻ Tải danh sách sensor** để LiteNVR lấy toàn bộ `binary_sensor`/`input_boolean`/`switch`/`device_tracker` từ HA. Trong ô cảm biến có thể **gõ tên hoặc chọn** từ danh sách gợi ý.
4. Mỗi cảm biến gán kèm **Nhãn sự kiện** (VD `Người quen`, `Người lạ`, hoặc tên người). Nhãn này hiển thị trên timeline/event khi cảm biến đó kích hoạt.
5. Bấm **Kiểm tra kết nối** rồi **Lưu**.

LiteNVR tự mở WebSocket tới HA (`/api/websocket`), xác thực bằng token, đăng ký `state_changed`; khi entity chuyển sang trạng thái chuyển động (`on`/`detected`/`motion`) thì kích hoạt ghi hình + snapshot cho camera tương ứng (chỉ kích hoạt khi đổi trạng thái, không lặp). Tự kết nối lại khi mất mạng.

## Xem Live / Bản ghi bị đen hoặc không phát
Nguyên nhân phổ biến: camera dùng **H.265/HEVC** — trình duyệt (Chrome/Edge/Firefox) không giải mã được.
- Mặc định LiteNVR dùng **Tự động phát hiện**: tự dò codec camera bằng `ffprobe`; nếu là H.265/HEVC sẽ tự chuyển sang H.264 cho cả Live (HLS) và khi phát bản ghi.
- Muốn chỉnh tay: **+ Quản lý Camera** → sửa camera → **Codec video**:
  - **Tự động phát hiện** (khuyến nghị).
  - **Copy**: nhẹ CPU nhất, chỉ dùng khi camera chắc chắn là H.264.
  - **Luôn chuyển sang H.264**: tương thích mọi trình duyệt, tốn CPU hơn.
- Phát bản ghi cũ quay bằng H.265: LiteNVR tự chuyển mã khi trình duyệt báo lỗi (không cần quay lại).
- Nút **🔍 Chẩn đoán** hiển thị trạng thái ffmpeg, codec dò được và log lỗi gần nhất.

**Lưu ý luồng HEVC/H.265 4K (Imou/Dahua/Hanet/EZVIZ):** live view phải chuyển mã realtime nên tốn CPU. Nếu box yếu, dùng **luồng phụ H.264** làm `sub_rtsp_url` cho live (Imou `subtype=1`; Hanet `/user:hanet;pwd:...` kèm stream phụ) và để luồng chính HEVC cho ghi hình.

**Camera Hikvision/EZVIZ (đường dẫn `.../h264/ch01/main/av_stream`) bị đen / báo lỗi:** các camera này giới hạn số phiên RTSP đồng thời (~3) và **tự khoá tạm khi có quá nhiều kết nối nhanh**, trả lỗi `500/503 SETUP failed` cho tới khi hết khoá. Cách xử lý:
- Chỉ để **một** ứng dụng kéo luồng trực tiếp; nếu đang dùng Frigate/go2rtc, trỏ LiteNVR vào **restream của go2rtc** (`rtsp://<ip-go2rtc>:8554/<ten_cam>`) thay vì kéo trực tiếp camera.
- LiteNVR đã tự tăng thời gian chờ (backoff 15–120s) khi camera trả 5XX để nhả khoá; xem nút **🔍 Chẩn đoán** (dòng "Camera đang TỪ CHỐI kết nối").
- Nếu camera vẫn 500 dù đã chờ, khởi động lại (rút điện) camera.

**Thanh thời gian:** kéo hoặc bấm trên thanh để tua; rê chuột để xem mốc giờ; vạch vàng hiển thị vị trí đang phát.

**Hình bị "vỡ"/rác khi xem qua go2rtc/restream:** thường do luồng H.264 ở profile/màu mà trình duyệt không giải mã (High 4:2:2, 4:4:4, 10-bit). LiteNVR tự dò `profile` + `pix_fmt`; nếu không phải H.264 8-bit 4:2:0 (`yuv420p`/`yuvj420p`) sẽ tự chuyển mã sang H.264 chuẩn. Có thể ép thủ công bằng **Codec video = Luôn chuyển sang H.264**. Ngoài ra LiteNVR ưu tiên TCP (`prefer_tcp`) để tránh mất gói.

## Thêm camera & nhiều giao thức
- **ONVIF tự động**: **+ Quản lý Camera** → **🔎 Tìm camera ONVIF** → chọn thiết bị → nhập tài khoản ONVIF → LiteNVR tự lấy **URL RTSP main + sub**. Hoạt động với hầu hết camera (Dahua/Imou, Hikvision/EZVIZ, Tapo, Hanet… bật ONVIF). Nếu tìm không thấy (Docker bridge chặn multicast), nhập thẳng **IP camera** vào ô bên dưới rồi bấm **Lấy luồng RTSP**.
- **Thêm hàng loạt**: **➕ Thêm hàng loạt**, mỗi dòng một camera: `mã | tên | rtsp_url | sub_url (tùy chọn)`.
- **Giao thức đầu vào**: RTSP (mặc định), HTTP MJPEG/snapshot, RTMP, HLS(m3u8) — chỉ cần dán URL, tham số `rtsp_transport` chỉ áp dụng cho RTSP.
- **Nhóm/Phòng và Nhãn**: điền khi thêm/sửa camera để phân loại; hiển thị trong danh sách và ô chọn nhanh.

## Tiết kiệm CPU / RAM
- **Live theo nhu cầu**: khi không có ai xem, LiteNVR tự bỏ nhánh HLS (nhất là với camera phải chuyển mã HEVC/H.265 → H.264). Khi mở lại live, luồng tự khởi động trong vài giây. Có thể chỉnh thời gian chờ: biến môi trường `LIVE_IDLE_STOP_SEC` (mặc định 30 giây).
- **Ghi hình lưu nguyên codec (`-c:v copy`)**: CPU gần như bằng 0; chỉ chuyển mã khi bạn thực sự xem.
- **Preset transcode nhẹ**: `ultrafast` + 2 luồng, `zerolatency`.
- **Đệm Motion trong RAM**: tắt tuỳ chọn "Đệm chuyển động trong RAM" trong **⚙️ Lưu trữ** để ghi đệm ra đĩa nếu box ít RAM.
- Với box yếu hoặc camera giới hạn phiên: dùng **luồng phụ** cho live và/hoặc **restream go2rtc**.

## Nhật ký & Lỗi
- Nút **📋 Nhật ký** hiển thị: trạng thái từng camera (chạy / lỗi / bị từ chối 5XX / chưa dò được codec) và **danh sách sự kiện** (INFO/WARN/ERR) kèm gợi ý tiếng Việt cho các lỗi RTSP thường gặp.
- Badge đỏ trên nút đếm số lỗi; có thể lọc "chỉ hiện lỗi/cảnh báo" và **📄 Sao chép** toàn bộ để gửi hỗ trợ.
- Lỗi từ trình duyệt (HLS/playback) cũng được đẩy về nhật ký này; cảnh báo ổ đĩa đầy xuất hiện tại đây.
- Nhật ký lưu trong RAM, tự xoá khi khởi động lại container.

## Dashboard giám sát
- Thẻ **CPU / RAM / Bộ nhớ** cập nhật mỗi 5 giây kèm biểu đồ mini (sparkline) theo thời gian thực.
- **CPU** hiển thị % toàn hệ thống và %CPU riêng của từng tiến trình ffmpeg (dưới mỗi camera).
- **RAM / Bộ nhớ** có thanh màu đổi theo mức (xanh → cam ≥75% → đỏ ≥90%).
- Lưới **camera trực quan**: ô camera hiện ảnh chuyển động mới nhất, chế độ (24/7 / Motion / Live), bấm để chuyển nhanh.
- Trên bản ghi liên tục, các **khoảng có chuyển động được tô màu cam** trên thanh thời gian để dễ dò lại đúng đoạn.


## Tích hợp Home Assistant (Motion Webhook - không cần token)
HA chủ động gọi webhook khi phát hiện chuyển động:
```yaml
rest_command:
  litenvr_motion:
    url: "http://<ip-zimaos>:8080/api/motion"
    method: POST
    username: "admin"
    password: "admin"
    content_type: "application/json"
    payload: '{"cam_id":"cam_cong","label":"Chuyển động (HASS)"}'
```

## Cách đưa lên GitHub để tự động đóng gói Docker Image (`ghcr.io`)
1. Tạo 1 Repository mới ở chế độ **Public** trên GitHub của bạn với tên `litenvr`.
2. Giải nén file ZIP này và tải toàn bộ các file (bao gồm thư mục `.github/workflows`) lên Repository đó.
3. Ngay khi tải lên, tab **Actions** trên GitHub sẽ tự động chạy và đóng gói ứng dụng thành Docker Image tại địa chỉ:
   `ghcr.io/<ten-github-cua-ban>/litenvr:latest`
4. Vào mục **Packages** trên GitHub -> chọn package `litenvr` -> vào **Package settings** -> đổi quyền hiển thị sang **Public**.

## Cách bấm cài 1 chạm trên ZimaOS
1. Mở giao diện ZimaOS -> nhấn dấu **(+)** góc trên bên phải -> chọn **Install a customized app** -> bấm **Import** ở góc trên.
2. Dán nội dung file `docker-compose.yml` (đã điền sẵn `ghcr.io/trankhanhduy2929-beep/litenvr:latest`) và chỉnh đường dẫn `/storage` trỏ vào ổ cứng HDD của bạn.
3. Bấm **Submit** để cài đặt. Mở ứng dụng và thêm Camera trực tiếp trên giao diện Web!
