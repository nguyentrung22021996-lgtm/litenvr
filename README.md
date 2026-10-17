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
3. Gán `entity_id` cảm biến chuyển động cho từng camera (VD `binary_sensor.cong_motion`), nhiều entity cách nhau dấu phẩy.
4. Bấm **Kiểm tra kết nối** rồi **Lưu**.

LiteNVR tự mở WebSocket tới HA (`/api/websocket`), xác thực bằng token, đăng ký `state_changed`; khi entity chuyển sang trạng thái chuyển động (`on`/`detected`/`motion`) thì kích hoạt ghi hình + snapshot cho camera tương ứng. Tự kết nối lại khi mất mạng.

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
