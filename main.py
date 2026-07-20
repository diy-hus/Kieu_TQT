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
import sys
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageDraw, ImageFont, ImageTk
from picamera2 import Picamera2
import cv2


# ============================================================
# CẤU HÌNH HỆ THỐNG
# ============================================================

APP_TITLE = "Hệ thống chụp ảnh"
INSTITUTION_NAME = "HỆ THỐNG THU THẬP DỮ LIỆU HÌNH ẢNH"
#DEVICE_TEXT = "Raspberry Pi 4 • Camera Module V2"

BASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = BASE_DIR / "dataset"
LOGO_PATH = BASE_DIR / "logo.png"

PREVIEW_SIZE = (640, 640)
CAPTURE_SIZE = (1280,1280)  # Độ phân giải tối đa phổ biến của Camera V2
AUTO_INTERVAL_MS = 60_000
PREVIEW_INTERVAL_MS = 40     # Xấp xỉ 25 FPS cho giao diện

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
        self.root.minsize(1100, 700)

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

        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        create_default_logo(LOGO_PATH)

        self._build_ui()
        self._set_status("Đang khởi tạo camera...", WARNING)
        self.root.after(300, self._initialize_camera)

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
            width=310,
            padx=28,
            pady=28,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)

        logo_image = Image.open(LOGO_PATH).convert("RGBA")
        logo_image.thumbnail((150, 150), Image.Resampling.LANCZOS)
        self.logo_photo = ImageTk.PhotoImage(logo_image)

        tk.Label(
            self.sidebar,
            image=self.logo_photo,
            bg=SIDEBAR_BG,
        ).grid(row=0, column=0, pady=(2, 18))

        tk.Label(
            self.sidebar,
            text=INSTITUTION_NAME,
            bg=SIDEBAR_BG,
            fg="white",
            font=("DejaVu Sans", 15, "bold"),
            wraplength=245,
            justify="center",
        ).grid(row=1, column=0, pady=(0, 7))

       # tk.Label(
        #    self.sidebar,
         #   text=DEVICE_TEXT,
         #   bg=SIDEBAR_BG,
         #   fg="#b8cad7",
         #   font=("DejaVu Sans", 10),
       # ).grid(row=2, column=0, pady=(0, 28))

        separator = tk.Frame(self.sidebar, bg="#365164", height=1)
        separator.grid(row=3, column=0, sticky="ew", pady=(0, 26))

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
        ).grid(row=10, column=0, pady=(16, 0))

        # -------------------- CỘT 2: LIVESTREAM --------------------
        self.content = tk.Frame(self.root, bg=WINDOW_BG, padx=24, pady=22)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_rowconfigure(1, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.content, bg=WINDOW_BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.grid_columnconfigure(0, weight=1)

        tk.Label(
            header,
            text="LIVESTREAM CAMERA",
            bg=WINDOW_BG,
            fg=TEXT_PRIMARY,
            font=("DejaVu Sans", 20, "bold"),
        ).grid(row=0, column=0, sticky="w")

        tk.Label(
            header,
            text="Giám sát và thu thập dữ liệu hình ảnh thực nghiệm",
            bg=WINDOW_BG,
            fg=TEXT_SECONDARY,
            font=("DejaVu Sans", 15),
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
        footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
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
            padx=14,
            pady=15,
        )

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
            time.sleep(0.7)

            self._set_status("Camera đã sẵn sàng.", SUCCESS)
            self._schedule_preview()

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
        if self.is_closing:
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
        folder = DATASET_DIR / datetime.now().strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _next_image_path(self) -> Path:
        folder = self._today_folder()

        indices = []
        for path in folder.iterdir():
            if not path.is_file():
                continue
            index = numeric_image_index(path.name)
            if index is not None:
                indices.append(index)

        next_index = max(indices, default=0) + 1
        return folder / f"{next_index}.jpg"

    def capture_manual(self) -> None:
        self.capture_image(source="MANUAL")

    def capture_image(self, source: str) -> None:
        if self.camera is None:
            messagebox.showwarning(
                "Camera chưa sẵn sàng",
                "Camera chưa được khởi tạo hoặc đang gặp lỗi.",
            )
            return

        try:
            image_path = self._next_image_path()

            # Chụp trực tiếp từ luồng main độ phân giải cao.
            self.camera.capture_file(str(image_path), name="main")

            self._flash_preview_border()
            self._update_counter()
            self._set_status(
                f"{source}: Đã lưu {image_path.relative_to(BASE_DIR)}",
                SUCCESS,
            )

        except Exception as exc:
            self._set_status(f"Lỗi lưu ảnh: {exc}", DANGER)
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
        if not self.auto_enabled or self.is_closing:
            return

        self.auto_after_id = self.root.after(
            AUTO_INTERVAL_MS,
            self._auto_capture_cycle,
        )

    def _auto_capture_cycle(self) -> None:
        self.auto_after_id = None

        if not self.auto_enabled or self.is_closing:
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
        folder = DATASET_DIR / datetime.now().strftime("%Y-%m-%d")
        count = 0

        if folder.exists():
            count = sum(
                1
                for path in folder.iterdir()
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

        if self.camera is not None:
            try:
                self.camera.stop()
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
