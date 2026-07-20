#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
HỆ THỐNG CHỤP ẢNH THỰC NGHIỆM
Thiết bị: Raspberry Pi 4 + Raspberry Pi Camera Module V2
Giao diện: Tkinter
Camera API: Picamera2

Chức năng:
- Hiển thị livestream camera.
- MANUAL: chụp ảnh ngay.
- AUTO: bật/tắt chế độ tự động chụp mỗi 60 giây.
- Lưu ảnh theo cấu trúc:
      dataset/
          YYYY-MM-DD/
              1.jpg
              2.jpg
              ...
- EXIT: xác nhận và tắt Raspberry Pi an toàn.
"""

from __future__ import annotations

import os
import re
import subprocess
import logging
import threading
import sys
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageDraw, ImageFont, ImageTk
from picamera2 import Picamera2
import cv2
import serial
from serial.tools import list_ports


# ============================================================
# CẤU HÌNH HỆ THỐNG
# ============================================================

APP_TITLE = "Hệ thống chụp ảnh"
INSTITUTION_NAME = "HỆ THỐNG THU THẬP DỮ LIỆU HÌNH ẢNH"
#DEVICE_TEXT = "Raspberry Pi 4 • Camera Module V2"

BASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = BASE_DIR / "dataset"
LOGO_PATH = BASE_DIR / "logo.png"
LOG_PATH = BASE_DIR / "power_control.log"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

PREVIEW_SIZE = (640, 640)
CAPTURE_SIZE = (1280,1280)  # Độ phân giải tối đa phổ biến của Camera V2
AUTO_INTERVAL_MS = 60_000
PREVIEW_INTERVAL_MS = 200    # Xấp xỉ 5 FPS, giảm tải CPU so với 25 FPS

# Arduino Nano gửi dữ liệu dạng: ADC:950
SERIAL_BAUD_RATE = 9600
SERIAL_POLL_MS = 250

# Với mạch quang trở hiện tại: ADC càng cao thì ánh sáng càng yếu.
SLEEP_ADC_THRESHOLD = 900
WAKE_ADC_THRESHOLD = 800

# Ngưỡng phân nhóm ảnh theo mức ánh sáng.
# 0-349   : cao
# 350-849 : trung bình
# 850-1023: thấp
HIGH_LIGHT_MAX_ADC = 349
MEDIUM_LIGHT_MAX_ADC = 849

LIGHT_FOLDER_HIGH = "cao"
LIGHT_FOLDER_MEDIUM = "trung_binh"
LIGHT_FOLDER_LOW = "thap"
LIGHT_FOLDER_NAMES = (
    LIGHT_FOLDER_HIGH,
    LIGHT_FOLDER_MEDIUM,
    LIGHT_FOLDER_LOW,
)

# Tắt/bật hoàn toàn đầu ra HDMI bằng wlr-randr.
# Khi HDMI tắt, VNC dùng đầu ra này cũng sẽ ngắt.
HDMI_OUTPUT_CONTROL = True
WLR_RANDR_PATH = "/usr/bin/wlr-randr"
HDMI_OUTPUT_NAME = "HDMI-A-1"
HDMI_COMMAND_TIMEOUT_SECONDS = 10

# Chỉ chuyển trạng thái khi điều kiện được duy trì đủ lâu.
# Giá trị kiểm tra nhanh. Sau khi thử ổn định, đổi lại 60 và 10.
SLEEP_CONFIRM_SECONDS = 5
WAKE_CONFIRM_SECONDS = 3

# Tự bật chế độ chụp AUTO sau khi camera khởi tạo.
AUTO_START_CAPTURE = False

WINDOW_BG = "#eef2f5"
SIDEBAR_BG = "#172a3a"
CARD_BG = "#ffffff"
TEXT_PRIMARY = "#17324d"
TEXT_SECONDARY = "#5e7184"
ACCENT = "#1f6f8b"
SUCCESS = "#2b8a66"
WARNING = "#cc7a00"
DANGER = "#b33a3a"
BORDER = "#d8e1e8"


# ============================================================
# HÀM HỖ TRỢ
# ============================================================

def create_default_logo(path: Path) -> None:
    """Tạo logo minh họa tối giản nếu người dùng chưa cung cấp logo.png."""
    if path.exists():
        return

    size = 360
    image = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)

    # Nền logo
    draw.rounded_rectangle(
        (18, 18, size - 18, size - 18),
        radius=56,
        fill=(239, 246, 249, 255),
        outline=(31, 111, 139, 255),
        width=10,
    )

    # Thân camera
    draw.rounded_rectangle(
        (65, 112, 295, 260),
        radius=28,
        fill=(23, 50, 77, 255),
    )
    draw.rounded_rectangle(
        (105, 82, 185, 124),
        radius=12,
        fill=(31, 111, 139, 255),
    )

    # Ống kính
    draw.ellipse((123, 128, 237, 242), fill=(238, 244, 247, 255))
    draw.ellipse((143, 148, 217, 222), fill=(31, 111, 139, 255))
    draw.ellipse((162, 167, 198, 203), fill=(15, 31, 45, 255))
    draw.ellipse((171, 173, 184, 186), fill=(255, 255, 255, 210))

    # Đèn trạng thái
    draw.ellipse((260, 128, 278, 146), fill=(62, 196, 126, 255))

    image.save(path)


def get_font(size: int, bold: bool = False):
    """Tải font phổ biến trên Raspberry Pi; dùng font mặc định nếu không có."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold else
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for font_path in candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size)

    return ImageFont.load_default()


def numeric_image_index(filename: str) -> int | None:
    """Lấy số thứ tự từ tên kiểu 1.jpg, 2.jpg, ..."""
    match = re.fullmatch(r"(\d+)\.jpg", filename, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


# ============================================================
# ỨNG DỤNG CHÍNH
# ============================================================

class CameraCaptureApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.configure(bg=WINDOW_BG)
        self.root.minsize(900, 540)

        # Mở toàn màn hình; nhấn F11 để bật/tắt, Esc để thoát toàn màn hình.
        self.fullscreen = True
        self.root.attributes("-fullscreen", self.fullscreen)
        self.root.bind("<F11>", self.toggle_fullscreen)
        self.root.bind("<Escape>", self.leave_fullscreen)
        self.root.protocol("WM_DELETE_WINDOW", self.request_shutdown)

        self.camera: Picamera2 | None = None
        self.preview_after_id: str | None = None
        self.auto_after_id: str | None = None
        self.auto_enabled = False
        self.latest_preview_photo = None
        self.is_closing = False

        # Trạng thái camera và chế độ tiết kiệm năng lượng.
        self.camera_running = False
        self.low_power_mode = False
        self.dark_since: float | None = None
        self.light_since: float | None = None
        self.latest_adc: int | None = None

        # Kết nối Arduino qua USB serial.
        self.serial_connection: serial.Serial | None = None
        self.serial_after_id: str | None = None

        # Trạng thái đầu ra HDMI.
        self.hdmi_output_enabled = True
        self.hdmi_command_running = False

        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        create_default_logo(LOGO_PATH)

        self._build_ui()
        self._set_status("Đang khởi tạo camera...", WARNING)
        self.root.after(300, self._initialize_camera)
        self.root.after(1000, self._initialize_serial)

    # --------------------------------------------------------
    # XÂY DỰNG GIAO DIỆN
    # --------------------------------------------------------

    def _build_ui(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=0)
        self.root.grid_columnconfigure(1, weight=1)

        # -------------------- CỘT 1: ĐIỀU KHIỂN --------------------
        self.sidebar = tk.Frame(
            self.root,
            bg=SIDEBAR_BG,
            width=285,
            padx=18,
            pady=12,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)

        logo_image = Image.open(LOGO_PATH).convert("RGBA")
        logo_image.thumbnail((105, 105), Image.Resampling.LANCZOS)
        self.logo_photo = ImageTk.PhotoImage(logo_image)

        tk.Label(
            self.sidebar,
            image=self.logo_photo,
            bg=SIDEBAR_BG,
        ).grid(row=0, column=0, pady=(0, 8))

        tk.Label(
            self.sidebar,
            text=INSTITUTION_NAME,
            bg=SIDEBAR_BG,
            fg="white",
            font=("DejaVu Sans", 13, "bold"),
            wraplength=235,
            justify="center",
        ).grid(row=1, column=0, pady=(0, 5))

        self.adc_state_label = tk.Label(
            self.sidebar,
            text="ARDUINO: ĐANG KẾT NỐI\nADC: --",
            bg=SIDEBAR_BG,
            fg="#ffd166",
            font=("DejaVu Sans", 10, "bold"),
            wraplength=245,
            justify="center",
        )
        self.adc_state_label.grid(row=2, column=0, pady=(2, 7))

       # tk.Label(
        #    self.sidebar,
         #   text=DEVICE_TEXT,
         #   bg=SIDEBAR_BG,
         #   fg="#b8cad7",
         #   font=("DejaVu Sans", 10),
       # ).grid(row=2, column=0, pady=(0, 28))

        separator = tk.Frame(self.sidebar, bg="#365164", height=1)
        separator.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        self.manual_button = self._create_button(
            parent=self.sidebar,
            text="MANUAL\nChụp ảnh thủ công",
            command=self.capture_manual,
            bg=ACCENT,
        )
        self.manual_button.grid(row=4, column=0, sticky="ew", pady=8)

        self.auto_button = self._create_button(
            parent=self.sidebar,
            text="AUTO\nBật chụp mỗi 1 phút",
            command=self.toggle_auto,
            bg=SUCCESS,
        )
        self.auto_button.grid(row=5, column=0, sticky="ew", pady=8)

        self.exit_button = self._create_button(
            parent=self.sidebar,
            text="EXIT\nTắt hệ thống",
            command=self.request_shutdown,
            bg=DANGER,
        )
        self.exit_button.grid(row=6, column=0, sticky="ew", pady=8)

        self.sidebar.grid_rowconfigure(7, weight=1)

        self.auto_state_label = tk.Label(
            self.sidebar,
            text="AUTO: TẮT",
            bg=SIDEBAR_BG,
            fg="#b8cad7",
            font=("DejaVu Sans", 11, "bold"),
        )
        self.auto_state_label.grid(row=8, column=0, pady=(20, 7))

        self.clock_label = tk.Label(
            self.sidebar,
            text="",
            bg=SIDEBAR_BG,
            fg="white",
            font=("DejaVu Sans", 11),
        )
        self.clock_label.grid(row=9, column=0)

        tk.Label(
            self.sidebar,
            text="F11: toàn màn hình • Esc: cửa sổ",
            bg=SIDEBAR_BG,
            fg="#829aaa",
            font=("DejaVu Sans", 9),
        ).grid(row=10, column=0, pady=(5, 0))

        # -------------------- CỘT 2: LIVESTREAM --------------------
        self.content = tk.Frame(self.root, bg=WINDOW_BG, padx=14, pady=10)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(1, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.content, bg=WINDOW_BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.grid_columnconfigure(0, weight=1)

        tk.Label(
            header,
            text="LIVESTREAM CAMERA",
            bg=WINDOW_BG,
            fg=TEXT_PRIMARY,
            font=("DejaVu Sans", 18, "bold"),
        ).grid(row=0, column=0, sticky="w")

        tk.Label(
            header,
            text="Giám sát và thu thập dữ liệu hình ảnh thực nghiệm",
            bg=WINDOW_BG,
            fg=TEXT_SECONDARY,
            font=("DejaVu Sans", 12),
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        camera_card = tk.Frame(
            self.content,
            bg=CARD_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=10,
            pady=10,
        )
        camera_card.grid(row=1, column=0, sticky="nsew")
        camera_card.grid_rowconfigure(0, weight=1)
        camera_card.grid_columnconfigure(0, weight=1)

        self.preview_label = tk.Label(
            camera_card,
            bg="#111820",
            fg="white",
            text="ĐANG KHỞI TẠO CAMERA...",
            font=("DejaVu Sans", 14, "bold"),
        )
        self.preview_label.grid(row=0, column=0, sticky="nsew")

        footer = tk.Frame(self.content, bg=WINDOW_BG)
        footer.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        footer.grid_columnconfigure(0, weight=1)

        self.status_label = tk.Label(
            footer,
            text="",
            bg=WINDOW_BG,
            fg=TEXT_SECONDARY,
            font=("DejaVu Sans", 10, "bold"),
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")

        self.counter_label = tk.Label(
            footer,
            text="Số ảnh hôm nay: 0",
            bg=WINDOW_BG,
            fg=TEXT_SECONDARY,
            font=("DejaVu Sans", 14),
            anchor="e",
        )
        self.counter_label.grid(row=0, column=1, sticky="e")

        self._update_clock()
        self._update_counter()

    def _create_button(self, parent, text, command, bg):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg="white",
            activebackground=bg,
            activeforeground="white",
            relief="flat",
            bd=0,
            cursor="hand2",
            font=("DejaVu Sans", 12, "bold"),
            padx=10,
            pady=9,
        )

    # --------------------------------------------------------
    # ĐIỀU KHIỂN ĐẦU RA HDMI
    # --------------------------------------------------------

    def _set_hdmi_output(self, enabled: bool) -> None:
        """Bật/tắt HDMI trong luồng nền để không chặn Tkinter."""
        if not HDMI_OUTPUT_CONTROL or self.is_closing:
            return

        if self.hdmi_command_running:
            logging.warning("Bỏ qua lệnh HDMI vì lệnh trước vẫn đang chạy.")
            return

        if self.hdmi_output_enabled == enabled:
            return

        self.hdmi_command_running = True

        threading.Thread(
            target=self._run_hdmi_command,
            args=(enabled,),
            daemon=True,
        ).start()

    def _find_wayland_environment(self) -> dict[str, str]:
        """Tạo môi trường Wayland dùng được cho chương trình tự khởi động."""
        env = os.environ.copy()

        # Tài khoản pi thường có UID 1000. Ưu tiên runtime hiện tại,
        # sau đó tìm socket Wayland thực tế trong /run/user/.
        candidate_runtime_dirs = []

        current_runtime = env.get("XDG_RUNTIME_DIR")
        if current_runtime:
            candidate_runtime_dirs.append(Path(current_runtime))

        candidate_runtime_dirs.append(Path(f"/run/user/{os.getuid()}"))
        candidate_runtime_dirs.append(Path("/run/user/1000"))

        # Loại bỏ đường dẫn trùng nhau nhưng giữ thứ tự ưu tiên.
        unique_runtime_dirs = []
        for runtime_dir in candidate_runtime_dirs:
            if runtime_dir not in unique_runtime_dirs:
                unique_runtime_dirs.append(runtime_dir)

        for runtime_dir in unique_runtime_dirs:
            if not runtime_dir.exists():
                continue

            wayland_sockets = sorted(runtime_dir.glob("wayland-*"))
            for socket_path in wayland_sockets:
                # Bỏ qua file khóa wayland-*.lock.
                if socket_path.name.endswith(".lock"):
                    continue

                env["XDG_RUNTIME_DIR"] = str(runtime_dir)
                env["WAYLAND_DISPLAY"] = socket_path.name
                env.setdefault(
                    "DBUS_SESSION_BUS_ADDRESS",
                    f"unix:path={runtime_dir}/bus",
                )
                return env

        # Giữ giá trị dự phòng để thông báo lỗi có ý nghĩa.
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        env.setdefault(
            "DBUS_SESSION_BUS_ADDRESS",
            "unix:path=/run/user/1000/bus",
        )
        return env

    def _run_hdmi_command(self, enabled: bool) -> None:
        """Thực thi wlr-randr với môi trường Wayland được xác định rõ."""
        action = "--on" if enabled else "--off"
        action_text = "bật" if enabled else "tắt"

        command = [
            WLR_RANDR_PATH,
            "--output",
            HDMI_OUTPUT_NAME,
            action,
        ]

        env = self._find_wayland_environment()

        logging.info(
            "HDMI command requested: %s | XDG_RUNTIME_DIR=%s | "
            "WAYLAND_DISPLAY=%s",
            " ".join(command),
            env.get("XDG_RUNTIME_DIR"),
            env.get("WAYLAND_DISPLAY"),
        )

        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=HDMI_COMMAND_TIMEOUT_SECONDS,
                env=env,
            )

            if result.returncode == 0:
                self.hdmi_output_enabled = enabled
                logging.info(
                    "Đã %s đầu ra HDMI bằng lệnh: %s",
                    action_text,
                    " ".join(command),
                )
            else:
                error_text = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"mã lỗi {result.returncode}"
                )
                logging.error(
                    "Không thể %s HDMI: %s",
                    action_text,
                    error_text,
                )
                self.root.after(
                    0,
                    lambda: self._set_status(
                        f"Không thể {action_text} HDMI: {error_text}",
                        WARNING,
                    ),
                )

        except FileNotFoundError:
            logging.error("Không tìm thấy wlr-randr tại %s", WLR_RANDR_PATH)
            self.root.after(
                0,
                lambda: self._set_status(
                    "Không tìm thấy wlr-randr.",
                    WARNING,
                ),
            )

        except subprocess.TimeoutExpired:
            logging.error("Lệnh %s HDMI bị quá thời gian.", action_text)
            self.root.after(
                0,
                lambda: self._set_status(
                    f"Lệnh {action_text} HDMI bị quá thời gian.",
                    WARNING,
                ),
            )

        except Exception as exc:
            logging.exception("Lỗi ngoài dự kiến khi điều khiển HDMI.")
            self.root.after(
                0,
                lambda: self._set_status(
                    f"Lỗi điều khiển HDMI: {exc}",
                    WARNING,
                ),
            )

        finally:
            self.hdmi_command_running = False

    # --------------------------------------------------------
    # ARDUINO / ADC / TIẾT KIỆM NĂNG LƯỢNG
    # --------------------------------------------------------

    def _find_arduino_port(self) -> str | None:
        """Tự tìm cổng USB serial của Arduino Nano."""
        # Ưu tiên tên ổn định trong /dev/serial/by-id/.
        by_id = Path("/dev/serial/by-id")
        if by_id.exists():
            devices = sorted(by_id.iterdir())
            if devices:
                return str(devices[0])

        # Dự phòng: tìm ttyUSB hoặc ttyACM.
        for port in list_ports.comports():
            if port.device.startswith(("/dev/ttyUSB", "/dev/ttyACM")):
                return port.device

        return None

    def _initialize_serial(self) -> None:
        """Kết nối Arduino và bắt đầu đọc ADC."""
        if self.is_closing or self.serial_connection is not None:
            return

        port = self._find_arduino_port()
        if port is None:
            self._set_status(
                "Chưa tìm thấy Arduino; sẽ thử lại sau 5 giây.",
                WARNING,
            )
            self.serial_after_id = self.root.after(
                5000,
                self._initialize_serial,
            )
            return

        try:
            self.serial_connection = serial.Serial(
                port=port,
                baudrate=SERIAL_BAUD_RATE,
                timeout=0,
            )
            self.serial_connection.reset_input_buffer()
            self._set_status(f"Đã kết nối Arduino tại {port}.", SUCCESS)
            self.adc_state_label.configure(
                text=f"ARDUINO: {port}\nADC: đang chờ dữ liệu",
                fg="#ffd166",
            )
            logging.info("Connected to Arduino at %s", port)
            self.serial_after_id = self.root.after(
                SERIAL_POLL_MS,
                self._poll_serial,
            )

        except serial.SerialException as exc:
            self.serial_connection = None
            self._set_status(f"Lỗi kết nối Arduino: {exc}", WARNING)
            self.adc_state_label.configure(
                text=f"LỖI ARDUINO\n{exc}",
                fg="#ff8a8a",
            )
            logging.exception("Arduino connection error")
            self.serial_after_id = self.root.after(
                5000,
                self._initialize_serial,
            )

    def _poll_serial(self) -> None:
        """Đọc các dòng ADC mà không chặn vòng lặp Tkinter."""
        self.serial_after_id = None

        if self.is_closing:
            return

        connection = self.serial_connection
        if connection is None:
            self.serial_after_id = self.root.after(
                5000,
                self._initialize_serial,
            )
            return

        try:
            while connection.in_waiting:
                line = (
                    connection.readline()
                    .decode("utf-8", errors="ignore")
                    .strip()
                )

                if not line:
                    continue

                logging.info("SERIAL RAW: %s", line)

                # Chấp nhận: ADC:950, ADC = 950, Gia tri ADC: 950, hoặc 950.
                numbers = re.findall(r"\d{1,4}", line)
                if not numbers:
                    continue

                adc_value = int(numbers[-1])
                if 0 <= adc_value <= 1023:
                    self.latest_adc = adc_value
                    self._process_adc(adc_value)

        except (serial.SerialException, OSError) as exc:
            self._set_status(f"Mất kết nối Arduino: {exc}", WARNING)
            try:
                connection.close()
            except Exception:
                pass
            self.serial_connection = None
            self.serial_after_id = self.root.after(
                5000,
                self._initialize_serial,
            )
            return

        self.serial_after_id = self.root.after(
            SERIAL_POLL_MS,
            self._poll_serial,
        )

    def _process_adc(self, adc_value: int) -> None:
        """Áp dụng vùng trễ ADC và hiển thị giá trị/đếm ngược trên giao diện."""
        now = time.monotonic()

        if not self.low_power_mode:
            if adc_value >= SLEEP_ADC_THRESHOLD:
                if self.dark_since is None:
                    self.dark_since = now

                elapsed = now - self.dark_since
                remaining = max(0, SLEEP_CONFIRM_SECONDS - elapsed)
                self.adc_state_label.configure(
                    text=(
                        f"ADC: {adc_value} | TỐI\n"
                        f"Ngủ sau: {remaining:.1f} giây"
                    ),
                    fg="#ffb347",
                )
                logging.info(
                    "ADC=%s DARK sleep_remaining=%.1f",
                    adc_value,
                    remaining,
                )

                if elapsed >= SLEEP_CONFIRM_SECONDS:
                    self.dark_since = None
                    self._enter_low_power_mode()
            else:
                self.dark_since = None
                if adc_value <= WAKE_ADC_THRESHOLD:
                    level_text = "SÁNG/HOẠT ĐỘNG"
                    color = "#6de0a3"
                else:
                    level_text = "VÙNG TRỄ"
                    color = "#ffd166"

                self.adc_state_label.configure(
                    text=f"ADC: {adc_value} | {level_text}",
                    fg=color,
                )
                logging.info("ADC=%s NORMAL", adc_value)

        else:
            if adc_value <= WAKE_ADC_THRESHOLD:
                if self.light_since is None:
                    self.light_since = now

                elapsed = now - self.light_since
                remaining = max(0, WAKE_CONFIRM_SECONDS - elapsed)
                self.adc_state_label.configure(
                    text=(
                        f"ADC: {adc_value} | ÁNH SÁNG TRỞ LẠI\n"
                        f"Thức sau: {remaining:.1f} giây"
                    ),
                    fg="#6de0a3",
                )
                logging.info(
                    "ADC=%s LIGHT wake_remaining=%.1f",
                    adc_value,
                    remaining,
                )

                if elapsed >= WAKE_CONFIRM_SECONDS:
                    self.light_since = None
                    self._leave_low_power_mode()
            else:
                self.light_since = None
                self.adc_state_label.configure(
                    text=f"ADC: {adc_value} | ĐANG TIẾT KIỆM",
                    fg="#ffb347",
                )
                logging.info("ADC=%s LOW_POWER", adc_value)

    def _enter_low_power_mode(self) -> None:
        """Dừng camera, livestream và lịch chụp nhưng giữ Pi/VNC hoạt động."""
        if self.low_power_mode or self.is_closing:
            return

        self.low_power_mode = True

        if self.preview_after_id is not None:
            try:
                self.root.after_cancel(self.preview_after_id)
            except tk.TclError:
                pass
            self.preview_after_id = None

        # Chỉ hủy lần chụp đã lên lịch; vẫn giữ auto_enabled để khôi phục sau.
        if self.auto_after_id is not None:
            try:
                self.root.after_cancel(self.auto_after_id)
            except tk.TclError:
                pass
            self.auto_after_id = None

        if self.camera is not None and self.camera_running:
            try:
                self.camera.stop()
                self.camera_running = False
            except Exception as exc:
                self._set_status(f"Lỗi dừng camera: {exc}", DANGER)

        self.latest_preview_photo = None
        self.preview_label.configure(
            image="",
            text=(
                "CHẾ ĐỘ TIẾT KIỆM NĂNG LƯỢNG\n\n"
                f"ADC: {self.latest_adc}\n"
                "Camera và livestream đã tạm dừng"
            ),
            bg="#000000",
            fg="#ffffff",
        )
        self.manual_button.configure(state="disabled")
        self._set_status(
            f"Đã vào chế độ tiết kiệm năng lượng, ADC={self.latest_adc}.",
            WARNING,
        )
        logging.info("ENTER LOW POWER ADC=%s", self.latest_adc)

        # Cho giao diện 0,8 giây để hoàn tất cập nhật rồi tắt HDMI.
        self.root.after(800, lambda: self._set_hdmi_output(False))

    def _leave_low_power_mode(self) -> None:
        """Bật HDMI, khởi động lại camera và khôi phục hoạt động."""
        if not self.low_power_mode or self.is_closing:
            return

        # Bật lại HDMI trước khi khôi phục livestream.
        self._set_hdmi_output(True)

        if self.camera is None:
            self._set_status("Không thể thức: camera chưa được khởi tạo.", DANGER)
            return

        try:
            if not self.camera_running:
                self.camera.start()
                self.camera_running = True

            # Đợi camera ổn định mà không chặn giao diện.
            self.root.after(600, self._finish_wake_up)

        except Exception as exc:
            self._set_status(f"Không thể khởi động lại camera: {exc}", DANGER)

    def _finish_wake_up(self) -> None:
        if self.is_closing or not self.low_power_mode:
            return

        self.low_power_mode = False
        self.manual_button.configure(state="normal")
        self.preview_label.configure(
            image="",
            text="ĐANG KHÔI PHỤC CAMERA...",
            bg="#111820",
            fg="white",
        )

        self._schedule_preview()

        # Nếu AUTO đang bật trước khi ngủ, tiếp tục chu kỳ chụp.
        if self.auto_enabled:
            self._schedule_next_auto_capture()

        self._set_status(
            f"Đã khôi phục hoạt động, ADC={self.latest_adc}.",
            SUCCESS,
        )
        logging.info("LEAVE LOW POWER ADC=%s", self.latest_adc)

    # --------------------------------------------------------
    # CAMERA
    # --------------------------------------------------------

    def _initialize_camera(self) -> None:
        try:
            self.camera = Picamera2()

            # Hai luồng:
            # - main: ảnh chụp độ phân giải cao
            # - lores: livestream nhẹ hơn cho giao diện
            config = self.camera.create_still_configuration(
                main={
                    "size": CAPTURE_SIZE,
                    "format": "RGB888",
                },
                lores={
                    "size": PREVIEW_SIZE,
                    "format": "YUV420",
                },
                display="lores",
                buffer_count=3,
            )

            self.camera.configure(config)
            self.camera.start()
            self.camera_running = True
            time.sleep(0.7)

            self._set_status("Camera đã sẵn sàng.", SUCCESS)
            self._schedule_preview()

            if AUTO_START_CAPTURE:
                self.root.after(800, self.start_auto)

        except Exception as exc:
            self.camera = None
            self.preview_label.configure(
                image="",
                text=(
                    "KHÔNG THỂ KHỞI TẠO CAMERA\n\n"
                    f"{exc}\n\n"
                    "Kiểm tra cáp CSI và chạy: rpicam-hello"
                ),
            )
            self._set_status("Lỗi khởi tạo camera.", DANGER)
            messagebox.showerror(
                "Lỗi camera",
                "Không thể khởi tạo Raspberry Pi Camera.\n\n"
                f"Chi tiết: {exc}",
            )

    def _schedule_preview(self) -> None:
        if self.is_closing or self.low_power_mode or not self.camera_running:
            return
        self._update_preview()
        self.preview_after_id = self.root.after(
            PREVIEW_INTERVAL_MS,
            self._schedule_preview,
        )

    def _update_preview(self) -> None:
        if self.camera is None:
            return

        try:
            frame_yuv = self.camera.capture_array("lores")

            frame_rgb = cv2.cvtColor(
                frame_yuv,
                cv2.COLOR_YUV2RGB_I420
            )

            image = Image.fromarray(frame_rgb)

            area_width = max(self.preview_label.winfo_width(), 640)
            area_height = max(self.preview_label.winfo_height(), 480)

            image.thumbnail(
                (area_width - 4, area_height - 4),
                Image.Resampling.LANCZOS,
            )

            self.latest_preview_photo = ImageTk.PhotoImage(image=image)
            self.preview_label.configure(
                image=self.latest_preview_photo,
                text="",
            )

        except Exception as exc:
            self._set_status(f"Lỗi livestream: {exc}", DANGER)

    # --------------------------------------------------------
    # LƯU ẢNH
    # --------------------------------------------------------

    def _today_folder(self) -> Path:
        """Tạo thư mục ngày và ba thư mục mức ánh sáng."""
        folder = DATASET_DIR / datetime.now().strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)

        for folder_name in LIGHT_FOLDER_NAMES:
            (folder / folder_name).mkdir(parents=True, exist_ok=True)

        return folder

    def _classify_light_level(self, adc_value: int) -> tuple[str, str]:
        """Trả về tên thư mục và nhãn hiển thị từ giá trị ADC."""
        if not 0 <= adc_value <= 1023:
            raise ValueError(f"Giá trị ADC không hợp lệ: {adc_value}")

        if adc_value <= HIGH_LIGHT_MAX_ADC:
            return LIGHT_FOLDER_HIGH, "cao"

        if adc_value <= MEDIUM_LIGHT_MAX_ADC:
            return LIGHT_FOLDER_MEDIUM, "trung bình"

        return LIGHT_FOLDER_LOW, "thấp"

    def _next_image_path(self, adc_value: int) -> tuple[Path, str]:
        """Tạo đường dẫn ảnh trong thư mục tương ứng mức ánh sáng."""
        date_folder = self._today_folder()
        folder_name, light_label = self._classify_light_level(adc_value)
        light_folder = date_folder / folder_name

        indices = []
        for path in light_folder.iterdir():
            if not path.is_file():
                continue

            index = numeric_image_index(path.name)
            if index is not None:
                indices.append(index)

        next_index = max(indices, default=0) + 1
        return light_folder / f"{next_index}.jpg", light_label

    def capture_manual(self) -> None:
        self.capture_image(source="MANUAL")

    def capture_image(self, source: str) -> None:
        if self.low_power_mode or not self.camera_running:
            self._set_status(
                "Không chụp ảnh khi đang ở chế độ tiết kiệm năng lượng.",
                WARNING,
            )
            return

        if self.camera is None:
            messagebox.showwarning(
                "Camera chưa sẵn sàng",
                "Camera chưa được khởi tạo hoặc đang gặp lỗi.",
            )
            return

        # Không lưu ảnh khi chưa nhận được ADC, nhằm tránh gán sai thư mục.
        adc_value = self.latest_adc
        if adc_value is None:
            self._set_status(
                f"{source}: Chưa nhận được ADC từ Arduino, bỏ qua lần chụp.",
                WARNING,
            )
            logging.warning("%s capture skipped: ADC is unavailable", source)
            return

        try:
            image_path, light_label = self._next_image_path(adc_value)

            # Chụp trực tiếp từ luồng main độ phân giải cao.
            self.camera.capture_file(str(image_path), name="main")

            self._flash_preview_border()
            self._update_counter()
            self._set_status(
                (
                    f"{source}: Đã lưu {image_path.relative_to(BASE_DIR)} "
                    f"| ADC={adc_value} | mức {light_label}"
                ),
                SUCCESS,
            )
            logging.info(
                "%s CAPTURE path=%s ADC=%s level=%s",
                source,
                image_path,
                adc_value,
                light_label,
            )

        except Exception as exc:
            self._set_status(f"Lỗi lưu ảnh: {exc}", DANGER)
            logging.exception("Capture failed")
            messagebox.showerror(
                "Không thể lưu ảnh",
                f"Đã xảy ra lỗi khi chụp ảnh:\n\n{exc}",
            )

    def _flash_preview_border(self) -> None:
        original_bg = self.preview_label.cget("bg")
        self.preview_label.configure(bg="#d9f4e7")
        self.root.after(
            180,
            lambda: self.preview_label.configure(bg=original_bg),
        )

    # --------------------------------------------------------
    # CHẾ ĐỘ AUTO
    # --------------------------------------------------------

    def toggle_auto(self) -> None:
        if self.auto_enabled:
            self.stop_auto()
        else:
            self.start_auto()

    def start_auto(self) -> None:
        if self.camera is None:
            messagebox.showwarning(
                "Camera chưa sẵn sàng",
                "Không thể bật AUTO khi camera chưa hoạt động.",
            )
            return

        self.auto_enabled = True
        self.auto_button.configure(
            text="AUTO\nDừng chụp tự động",
            bg=WARNING,
            activebackground=WARNING,
        )
        self.auto_state_label.configure(
            text="AUTO: ĐANG BẬT",
            fg="#6de0a3",
        )
        self._set_status(
            "Đã bật AUTO: hệ thống chụp một ảnh sau mỗi 60 giây.",
            SUCCESS,
        )

        # Lần chụp đầu tiên diễn ra ngay khi bật AUTO.
        self.capture_image(source="AUTO")
        self._schedule_next_auto_capture()

    def stop_auto(self) -> None:
        self.auto_enabled = False

        if self.auto_after_id is not None:
            self.root.after_cancel(self.auto_after_id)
            self.auto_after_id = None

        self.auto_button.configure(
            text="AUTO\nBật chụp mỗi 1 phút",
            bg=SUCCESS,
            activebackground=SUCCESS,
        )
        self.auto_state_label.configure(
            text="AUTO: TẮT",
            fg="#b8cad7",
        )
        self._set_status("Đã dừng chế độ chụp tự động.", TEXT_SECONDARY)

    def _schedule_next_auto_capture(self) -> None:
        if (
            not self.auto_enabled
            or self.is_closing
            or self.low_power_mode
        ):
            return

        self.auto_after_id = self.root.after(
            AUTO_INTERVAL_MS,
            self._auto_capture_cycle,
        )

    def _auto_capture_cycle(self) -> None:
        self.auto_after_id = None

        if (
            not self.auto_enabled
            or self.is_closing
            or self.low_power_mode
        ):
            return

        self.capture_image(source="AUTO")
        self._schedule_next_auto_capture()

    # --------------------------------------------------------
    # TRẠNG THÁI GIAO DIỆN
    # --------------------------------------------------------

    def _set_status(self, text: str, color: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.status_label.configure(
            text=f"[{timestamp}] {text}",
            fg=color,
        )

    def _update_counter(self) -> None:
        # Gọi _today_folder() để ba thư mục được tạo ngay khi ứng dụng chạy.
        folder = self._today_folder()

        count = sum(
            1
            for path in folder.rglob("*.jpg")
            if path.is_file() and numeric_image_index(path.name) is not None
        )

        self.counter_label.configure(text=f"Số ảnh hôm nay: {count}")

    def _update_clock(self) -> None:
        self.clock_label.configure(
            text=datetime.now().strftime("%d/%m/%Y  •  %H:%M:%S")
        )
        self.root.after(1000, self._update_clock)

    # --------------------------------------------------------
    # CỬA SỔ VÀ TẮT MÁY
    # --------------------------------------------------------

    def toggle_fullscreen(self, _event=None) -> None:
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def leave_fullscreen(self, _event=None) -> None:
        self.fullscreen = False
        self.root.attributes("-fullscreen", False)

    def request_shutdown(self) -> None:
        confirmed = messagebox.askyesno(
            "Xác nhận tắt hệ thống",
            "Bạn có chắc muốn thoát chương trình và tắt Raspberry Pi không?",
            icon="warning",
        )

        if not confirmed:
            return

        self._shutdown_application(poweroff=True)

    def _shutdown_application(self, poweroff: bool) -> None:
        if self.is_closing:
            return

        self.is_closing = True
        self.auto_enabled = False
        self._set_status("Đang đóng camera và tắt hệ thống...", WARNING)
        self.root.update_idletasks()

        if self.preview_after_id is not None:
            try:
                self.root.after_cancel(self.preview_after_id)
            except tk.TclError:
                pass

        if self.auto_after_id is not None:
            try:
                self.root.after_cancel(self.auto_after_id)
            except tk.TclError:
                pass

        if self.serial_after_id is not None:
            try:
                self.root.after_cancel(self.serial_after_id)
            except tk.TclError:
                pass
            self.serial_after_id = None

        if self.serial_connection is not None:
            try:
                self.serial_connection.close()
            except Exception:
                pass
            self.serial_connection = None

        if self.camera is not None:
            try:
                if self.camera_running:
                    self.camera.stop()
                    self.camera_running = False
            except Exception:
                pass
            try:
                self.camera.close()
            except Exception:
                pass

        if poweroff:
            try:
                # Cần cấu hình sudoers theo file hướng dẫn đi kèm.
                subprocess.Popen(
                    ["sudo", "-n", "/usr/bin/systemctl", "poweroff"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                messagebox.showerror(
                    "Không thể tắt Raspberry Pi",
                    "Chương trình đã đóng camera nhưng không gọi được "
                    f"lệnh poweroff.\n\n{exc}",
                )

        self.root.after(300, self.root.destroy)


def main() -> int:
    try:
        root = tk.Tk()
        CameraCaptureApp(root)
        root.mainloop()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Lỗi nghiêm trọng: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
