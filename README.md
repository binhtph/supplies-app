# Hass Dashboard - Trợ Lý Quản Lý Thiết Bị & Phương Tiện

Ứng dụng web toàn diện giúp bạn quản lý, theo dõi tuổi thọ và chi phí thay thế của các thiết bị (vật tư) trong nhà cũng như lịch trình bảo dưỡng phương tiện (xe cộ). Hệ thống được tích hợp sâu với **Home Assistant** qua giao thức MQTT.

## 🚀 Tính Năng Nổi Bật

- **Giao diện Cực Nhanh (SPA):** Trải nghiệm không độ trễ với công nghệ HTMX (Không tải lại trang).
- **Thiết Kế Đẳng Cấp:** Phong cách iOS hiện đại, giao diện hiển thị xuất sắc trên mọi kích thước màn hình (Di động / PC) nhờ Tailwind CSS Tĩnh.
- **Quản Lý Vật Tư:** Theo dõi ngày dùng, ước tính ngày thay mới, tính toán chi phí trọn đời cho mọi vật dụng trong nhà.
- **Quản Lý Phương Tiện:** Tự động tính toán ODO (số km đã chạy), số km còn lại của các phụ tùng xe (nhớt, vỏ xe, v.v.).
- **Tích hợp Home Assistant (MQTT):** Tự động phát sóng trạng thái của thiết bị & phương tiện lên hệ thống smarthome Home Assistant để tự động hóa cảnh báo.
- **Lưu trữ An Toàn:** Backup dữ liệu trực tiếp và logs hệ thống đầy đủ.

## 🛠️ Công Nghệ Sử Dụng

- **Backend:** Python (FastAPI), Uvicorn
- **Frontend:** HTML5, Jinja2 Templates, HTMX, Tailwind CSS v3 (Static), FontAwesome 6
- **Database:** SQLite (`app.db`)
- **IoT / Smarthome:** Paho-MQTT

## 📦 Hướng Dẫn Cài Đặt (Local & Docker)

### Cách 1: Chạy trực tiếp (Python)
1. Cài đặt các thư viện yêu cầu:
   ```bash
   pip install -r requirements.txt
   ```
2. Khởi động máy chủ:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 3003
   ```
3. Truy cập vào: `http://localhost:3003`

### Cách 2: Chạy thông qua Docker (Linux / VPS)
Dự án đã có sẵn `Dockerfile`. Bạn chỉ cần:
```bash
docker build -t hass-dashboard .
docker run -d -p 3003:3003 -v $(pwd)/data:/app/data --name hass-dashboard hass-dashboard
```

## 📂 Cấu Trúc Thư Mục
- `main.py`: Logic Backend và APIs.
- `templates/`: Giao diện ứng dụng.
- `static/`: Chứa file CSS tĩnh siêu nhẹ.
- `data/`: Cơ sở dữ liệu SQLite và thư mục ảnh (Avatar).

---
*Phát triển bởi [Bạn] & Antigravity IDE.*
