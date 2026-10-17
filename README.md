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
- **Restream nội bộ (go2rtc)**: bản Docker đã kèm `go2rtc`; LiteNVR tự tạo cấu hình và đẩy mọi nguồn qua restream `rtsp://127.0.0.1:8554`, nhờ đó **mỗi camera chỉ tốn 1 phiên RTSP** dù có ghi hình + xem live + chụp snapshot (tránh lỗi "đủ số phiên" của Hikvision/EZVIZ). Các stream bạn tự thêm trong `go2rtc.yaml` được **giữ nguyên** khi LiteNVR cập nhật cấu hình. Tắt bằng biến môi trường `GO2RTC=off`.
- **Live nhẹ CPU (LIVE_MAX_HEIGHT)**: với camera 2K/4K phải chuyển mã (H.265/HEVC), live được hạ về tối đa 720p — giảm tải mã hoá, băng thông (dễ xem qua Wi-Fi/4G) và tải máy khách; **bản ghi vẫn giữ nguyên độ phân giải gốc**. Đổi bằng biến môi trường `LIVE_MAX_HEIGHT` (`0` = giữ nguyên, `1080` = nét hơn nhưng nặng hơn). Lưu ý: camera HEVC vẫn tốn CPU để *giải mã* — cách tiết kiệm nhất là dùng luồng H.264 (xem bên dưới).
- **Dùng luồng phụ cho Live**: điền "Luồng phụ" trong phần sửa camera (vd Imou/Dahua `subtype=1`, Hikvision `/Streaming/Channels/102`) — live thường là H.264 nên **không cần chuyển mã** (CPU gần như 0).
- **Live theo nhu cầu**: khi không có ai xem, LiteNVR tự bỏ nhánh HLS (nhất là với camera phải chuyển mã HEVC/H.265 → H.264). Khi mở lại live, luồng tự khởi động trong vài giây. Có thể chỉnh thời gian chờ: biến môi trường `LIVE_IDLE_STOP_SEC` (mặc định 30 giây).
- **Ghi hình lưu nguyên codec (`-c:v copy`)**: CPU gần như bằng 0; chỉ chuyển mã khi bạn thực sự xem.
- **Preset transcode nhẹ**: `ultrafast` + 2 luồng, `zerolatency`.
- **Đệm Motion trong RAM**: tắt tuỳ chọn "Đệm chuyển động trong RAM" trong **⚙️ Lưu trữ** để ghi đệm ra đĩa nếu box ít RAM.
- Với box yếu hoặc camera giới hạn phiên: dùng **luồng phụ** cho live và/hoặc **restream go2rtc**.

## Nhật ký & Lỗi
- Nút **📋 Nhật ký** hiển thị: trạng thái từng camera (chạy / lỗi / bị từ chối 5XX / chưa dò được codec) và **danh sách sự kiện** (INFO/WARN/ERR) kèm gợi ý tiếng Việt cho các lỗi RTSP thường gặp.
- Badge đỏ trên nút đếm số lỗi; có thể lọc "chỉ hiện lỗi/cảnh báo" và **📄 Sao chép** toàn bộ để gửi hỗ trợ.
- Lỗi từ trình duyệt (HLS/playback) cũng được đẩy về nhật ký này; cảnh báo ổ đĩa đầy xuất hiện tại đây.
- Trạng thái **restream nội bộ (go2rtc)** hiển thị ngay đầu hộp Nhật ký kèm **CPU load và RAM** của máy; file ghi rỗng/hỏng được tự phát hiện và xoá (file 0 byte) kèm cảnh báo.
- Nhật ký lưu trong RAM, tự xoá khi khởi động lại container.

## Dashboard giám sát
- Thẻ **CPU / RAM / Bộ nhớ** cập nhật mỗi 5 giây kèm biểu đồ mini (sparkline) theo thời gian thực.
- **CPU** hiển thị % toàn hệ thống và %CPU riêng của từng tiến trình ffmpeg (dưới mỗi camera).
- **RAM / Bộ nhớ** có thanh màu đổi theo mức (xanh → cam ≥75% → đỏ ≥90%).
- Lưới **camera trực quan**: ô camera hiện ảnh chuyển động mới nhất, chế độ (24/7 / Motion / Live / Lịch), bấm để chuyển nhanh.
- Trên bản ghi liên tục, các **khoảng có chuyển động được tô màu cam** trên thanh thời gian để dễ dò lại đúng đoạn.

## Tính năng NVR nâng cao
- **⊞ Lưới nhiều camera**: bấm **⊞ Lưới** để xem 4 camera cùng lúc (bấm tiếp để chuyển 9 ô, bấm lần nữa để tắt). Bấm vào một ô để quay về chế độ 1 camera. Live trong lưới tự khởi động và tự dừng khi bạn thoát lưới (tiết kiệm CPU).
- **🔒 Khoá bản ghi**: di con trỏ tới đoạn cần giữ rồi bấm **🔒 Khoá đoạn** — file này **không bao giờ bị xoá tự động** (theo ngày lưu, giới hạn GB hay % ổ cứng). Bấm **🔓 Mở khoá** để cho phép xoá lại.
- **⬇️ Xuất clip**: chọn *Từ giờ → Đến giờ* dưới thanh thời gian rồi bấm **Tải đoạn** để tải một file MP4 gồm toàn bộ các đoạn ghi trong khoảng đó. Nút **⬇️** trên mỗi sự kiện tải nhanh clip quanh sự kiện (mặc định 10s trước + 60s sau). Bật **chuyển H.264** nếu muốn gửi clip cho thiết bị/tài khoản không phát được H.265.
- **📅 Lịch ghi hình**: trong phần sửa camera, bật **Lịch ghi hình** và thêm các khung giờ (Thứ, Từ, Đến, chế độ 24/7 hay Motion). Ngoài mọi khung giờ, camera **không ghi** (vẫn xem live được). Hỗ trợ khung giờ qua đêm (VD 22:00 → 06:00). Ví dụ: ngày thường chỉ Motion 8h–17h, ban đêm 24/7.
- **🎮 PTZ (ONVIF)**: bấm **🎮 PTZ** để hiện bàn điều hướng xoay/zoom (dùng tài khoản trong URL RTSP; camera cần hỗ trợ ONVIF). Chọn preset có sẵn trong danh sách và bấm **📍 Đến preset**. Nếu camera dùng cổng ONVIF khác 80, thêm `onvif_port` trong `config.json`.
- **🔔 Thông báo Telegram/Webhook**: bấm **🔔 Thông báo**, dán **Bot Token** (tạo bot qua @BotFather), **Chat ID** và bật thông báo — mỗi khi có chuyển động, LiteNVR gửi tin kèm **ảnh snapshot** (tuỳ chọn). Ô **Webhook URL** cho phép đẩy JSON sang Node-RED/HA/n8n. Nút **📨 Gửi thử** để kiểm tra; tin nhắn giới hạn 1 tin/3 giây mỗi camera để tránh dội.
- **💾 Sao lưu / Khôi phục cấu hình**: trong **⚙️ Lưu trữ**, bấm **Xuất cấu hình** để tải file JSON (camera, lịch, HA, thông báo) và **Nhập cấu hình** để khôi phục sau khi chạy container mới / khôi phục nguyên trạng.


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

### Mẫu riêng cho ZimaOS (kèm giới hạn CPU/RAM)
Nếu chạy ZimaOS và muốn cấu hình "chuẩn" như bản đang chạy (cổng **9895**, ổ `/DATA/NVR`, giới hạn **1 CPU / 4GB RAM**, tmpfs cho HLS + buffer), dùng file **`docker-compose.zimaos.yml`**:
- `GO2RTC=off`: chọn khi bạn **đã có go2rtc trong Frigate** và trỏ luồng qua đó (bớt 1 tầng relay, nhẹ hơn). Không có go2rtc ngoài thì đổi thành `GO2RTC=auto`.
- `LIVE_MAX_HEIGHT=720`: live H.265/HEVC được hạ về 720p cho nhẹ CPU (bản ghi vẫn giữ nguyên gốc). Đặt `0` để giữ nguyên.
- Giới hạn `cpus: "1.00"` phù hợp box yếu; muốn live mượt hơn với nhiều camera HEVC thì nâng lên `"2.00"`.

Cả 2 file đều import được theo cách ở trên; nhớ sửa `source` của volume cho đúng ổ cứng của bạn.
