import mss
import mss.tools
import numpy as np
from PIL import Image
import io
import win32gui
import win32con
import win32ui
from typing import Optional, Tuple, Union
import logging

_logger = logging.getLogger(__name__)


def grab_screen_image(monitor: int = 1, max_edge: int = 1280) -> Optional[Image.Image]:
    """Grab one screenshot as a PIL image, downscaled for the model.

    Creates its own mss instance per call on purpose: mss handles are bound to the
    thread that made them, and this runs on an arbitrary asyncio.to_thread worker.
    Unlike ScreenCaptureWindows.capture_frame this never dedupes - an unchanged
    screen is still a valid answer to "what am I looking at?".

    Args:
        monitor: mss monitor index. 1 is the primary display, 0 is all monitors joined.
        max_edge: longest edge of the returned image, in pixels.
    """
    try:
        with mss.mss() as sct:
            if monitor >= len(sct.monitors):
                _logger.warning(
                    "Monitor %s not present (%s available); using the primary display.",
                    monitor,
                    len(sct.monitors) - 1,
                )
                monitor = 1
            shot = sct.grab(sct.monitors[monitor])
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        if max(image.size) > max_edge:
            scale = max_edge / max(image.size)
            image = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        return image
    except Exception as e:
        _logger.error("Screen capture failed: %s", e)
        return None


class ScreenCaptureWindows:
    """Windows用のスクリーンキャプチャクラス"""

    def __init__(self, monitor: int = 1):
        """
        初期化
        Args:
            monitor (int): キャプチャするモニターの番号（デフォルト: 1 = プライマリモニター）
        """
        self.monitor = monitor
        self.sct = mss.mss()
        self.previous_frame = None
        self.logger = logging.getLogger(__name__)

    def __enter__(self):
        """コンテキストマネージャーのエントリーポイント"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """コンテキストマネージャーの終了処理"""
        self.sct.close()

    def get_window_rect(self, window_title: str) -> Optional[Tuple[int, int, int, int]]:
        """
        指定したウィンドウの位置とサイズを取得
        Args:
            window_title (str): ウィンドウのタイトル
        Returns:
            Optional[Tuple[int, int, int, int]]: (left, top, right, bottom) または None
        """
        try:
            hwnd = win32gui.FindWindow(None, window_title)
            if hwnd:
                rect = win32gui.GetWindowRect(hwnd)
                return rect
            return None
        except Exception as e:
            self.logger.error(f"ウィンドウの位置取得に失敗: {e}")
            return None

    def capture_frame(self) -> Optional[bytes]:
        """
        画面をキャプチャしてバイト列として返す
        Returns:
            Optional[bytes]: 画像データ（PNG形式）またはNone
        """
        try:
            # プライマリモニターをキャプチャ
            monitor = self.sct.monitors[self.monitor]
            screenshot = self.sct.grab(monitor)
            
            # PILイメージに変換
            img = Image.frombytes('RGB', screenshot.size, screenshot.rgb)
            
            # 画像サイズを調整（必要に応じて）
            max_dimension = 1920
            if img.width > max_dimension or img.height > max_dimension:
                ratio = max_dimension / max(img.width, img.height)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            # PNG形式でバイト列に変換
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='PNG', optimize=True)
            img_byte_arr = img_byte_arr.getvalue()

            # 前のフレームと同じ場合はNoneを返す
            if img_byte_arr == self.previous_frame:
                return None
            
            self.previous_frame = img_byte_arr
            return img_byte_arr

        except Exception as e:
            self.logger.error(f"画面キャプチャ中にエラーが発生: {e}")
            return None

    def capture_window(self, window_title: str) -> Optional[bytes]:
        """
        特定のウィンドウをキャプチャ
        Args:
            window_title (str): キャプチャするウィンドウのタイトル
        Returns:
            Optional[bytes]: 画像データ（PNG形式）またはNone
        """
        try:
            hwnd = win32gui.FindWindow(None, window_title)
            if not hwnd:
                return None

            # ウィンドウの位置とサイズを取得
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width = right - left
            height = bottom - top

            # デバイスコンテキストを取得
            hwndDC = win32gui.GetWindowDC(hwnd)
            mfcDC = win32ui.CreateDCFromHandle(hwndDC)
            saveDC = mfcDC.CreateCompatibleDC()

            # ビットマップを作成
            saveBitMap = win32ui.CreateBitmap()
            saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
            saveDC.SelectObject(saveBitMap)

            # ウィンドウの内容をビットマップにコピー
            result = saveDC.BitBlt((0, 0), (width, height), mfcDC, (0, 0), win32con.SRCCOPY)

            # ビットマップデータを取得
            bmpinfo = saveBitMap.GetInfo()
            bmpstr = saveBitMap.GetBitmapBits(True)

            # PILイメージに変換
            img = Image.frombuffer(
                'RGB',
                (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                bmpstr, 'raw', 'BGRX', 0, 1)

            # クリーンアップ
            win32gui.DeleteObject(saveBitMap.GetHandle())
            saveDC.DeleteDC()
            mfcDC.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwndDC)

            # PNG形式でバイト列に変換
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='PNG')
            return img_byte_arr.getvalue()

        except Exception as e:
            self.logger.error(f"ウィンドウキャプチャ中にエラーが発生: {e}")
            return None
