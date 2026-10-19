"""Windows webcam capture for the in-room assistant."""

import os
import time
from typing import Optional

import cv2
from PIL import Image

from src.logger import logger


class CameraCaptureWindows:
    """Capture webcam frames without using desktop screenshots."""

    def __init__(self, device_index: Optional[int] = None):
        self.device_index = device_index if device_index is not None else int(
            os.getenv("CAMERA_DEVICE_INDEX", "0")
        )
        self.camera: Optional[cv2.VideoCapture] = None
        self._last_open_attempt = 0.0

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def open(self) -> bool:
        self._last_open_attempt = time.monotonic()
        for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
            camera = cv2.VideoCapture(self.device_index, backend)
            if camera.isOpened():
                self.camera = camera
                logger.info("Camera opened: device index=%s", self.device_index)
                return True
            camera.release()

        logger.warning(
            "Camera device %s is unavailable. Check Windows camera privacy settings, "
            "CAMERA_DEVICE_INDEX, and whether another app is using the webcam.",
            self.device_index,
        )
        return False

    def capture_frame(self) -> Optional[Image.Image]:
        """Return the current webcam frame as a PIL image, or None if unavailable."""
        if self.camera is None or not self.camera.isOpened():
            if time.monotonic() - self._last_open_attempt < 5:
                return None
            if not self.open():
                return None

        ok, frame = self.camera.read()
        if not ok or frame is None:
            logger.warning("Could not read a frame from camera device %s.", self.device_index)
            self.close()
            return None

        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        max_dimension = 1280
        if max(image.size) > max_dimension:
            scale = max_dimension / max(image.size)
            image = image.resize(
                (int(image.width * scale), int(image.height * scale)),
                Image.Resampling.LANCZOS,
            )
        return image

    def close(self) -> None:
        if self.camera is not None:
            self.camera.release()
            self.camera = None
