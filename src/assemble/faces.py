"""Faceless b-roll check: detect faces in a stock clip's preview frames before using it.

Keywords alone can't keep faces out — stock libraries put people in almost everything, and a
stranger's face can read as the story's real person. OpenCV's YuNet detector (MIT, 230 KB ONNX,
bundled in assets/models/) runs offline on the providers' small preview images. It reliably finds
clear faces; heavily occluded ones (a face inside a helmet, lips only) can slip through, which
the human review in Phase 7 covers.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import cv2
import httpx
import numpy as np

from src.config import ROOT
from src.discover.common import FetchError, request

log = logging.getLogger("raij.assemble")

MODEL = ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
SCORE = 0.45          # tuned on real Pexels frames: catches astronaut/statue faces, no false alarms on scenes
MIN_AREA = 0.015      # faces smaller than 1.5% of the frame (distant crowds) are fine
WIDTH = 640


@lru_cache(maxsize=1)
def _detector():
    return cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), SCORE)


def face_ratio(image: np.ndarray) -> float:
    """Area of the largest detected face as a fraction of the image (0.0 = none)."""
    h, w = image.shape[:2]
    if w > WIDTH:
        image = cv2.resize(image, (WIDTH, int(h * WIDTH / w)))
        h, w = image.shape[:2]
    det = _detector()
    det.setInputSize((w, h))
    _, faces = det.detect(image)
    if faces is None:
        return 0.0
    return max(float(f[2] * f[3]) / (w * h) for f in faces)


def has_face(client: httpx.Client, urls: list[str]) -> bool:
    """True if any preview image shows a face big enough to be mistaken for someone. Unreadable
    previews count as no face (the clip still gets human review)."""
    for url in urls:
        try:
            data = request(client, "GET", url, retries=1).content
        except FetchError as exc:
            log.debug("preview %s unreadable: %s", url, exc)
            continue
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is not None and face_ratio(image) >= MIN_AREA:
            return True
    return False
