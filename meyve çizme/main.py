"""
Proje 2: Kamerayla El Takibi, Fare Kontrolü ve Meyve Çizim Oyunu
Kütüphaneler: opencv-python, mediapipe

Özellikler:
1. Webcam'den canlı görüntü alma (ayna görünümü).
2. Eli tespit edip 21 eklem noktasını (landmarks) ve bağlantılarını ekrana çizme.
3. Kaç parmağın açık olduğunu gerçek zamanlı hesaplayıp ekrana yazma.
4. BONUS - Fare Kontrolü:
   - Başparmak + İşaret parmağını birleştirerek (Pinch) fare imlecini yönetme.
   - Serçe parmağı kaldırıp indirince Sol Tık / Çift Tık.
   - Orta parmağı kaldırıp indirince Sağ Tık.
   - Yumruk yaparak (0 parmak açık) ekrandaki nesneleri ve masaüstü pencerelerini/sayfalarını kaydırma.
5. OYUN MODU ([G] Tuşu - Meyve Çizim Oyunu):
   - [G] tuşuna basınca beyaz bir çizim sayfası açılır.
   - Sol üstte hedef meyve ismi ve tur bilgisi gösterilir.
   - Kullanıcı eliyle (Pinch veya İşaret Parmağı ile) fırçayı yöneterek meyveyi çizer.
   - Doğruya yakın çizerse sonraki tura geçer, yanlış çizerse "TEKRAR DENE!" uyarısı ile çizim sıfırlanır.
"""

from __future__ import annotations

import ctypes
import math
import os
import random
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import mediapipe as mp
import numpy as np


def disable_windows_background_throttling() -> None:
    """
    Windows 11'de pencere arka plana düştüğünde (masaüstüne tıklandığında)
    işletim sisteminin Python sürecini 'EcoQoS / Verimlilik Modu'na alıp
    FPS'i düşürmesini engeller ve zamanlayıcı çözünürlüğünü 1ms yapar.
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        # ABOVE_NORMAL_PRIORITY_CLASS (0x00008000)
        current_process = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(current_process, 0x00008000)

        # PROCESS_POWER_THROTTLING_STATE yapısıyla arka plan güç kısıtlamasını kapat
        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]

        throttling = PROCESS_POWER_THROTTLING_STATE()
        throttling.Version = 1
        throttling.ControlMask = 0x1  # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        throttling.StateMask = 0x0    # Kısıtlamayı devre dışı bırak (0)
        # ProcessPowerThrottling = 4
        kernel32.SetProcessInformation(
            current_process,
            4,
            ctypes.byref(throttling),
            ctypes.sizeof(throttling),
        )
    except Exception:
        pass

    try:
        # Windows uyku/zamanlayıcı hassasiyetini 15.6ms yerine 1ms yap
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


class ThreadedCamera:
    """
    Kameradan kare okuma işlemini ayrı bir arka plan iş parçacığında (thread)
    yürütür. Böylece MediaPipe çalışırken kamera sürücüsü beklemez ve masaüstünde
    FPS düşüşü yaşanmaz.
    """

    def __init__(self, index: int = 0, width: int = 960, height: int = 540) -> None:
        # Windows'ta DirectShow (CAP_DSHOW) MSMF'ye göre çok daha düşük gecikmelidir
        if sys.platform == "win32":
            self.cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not self.cap.isOpened():
                self.cap = cv2.VideoCapture(index)
        else:
            self.cap = cv2.VideoCapture(index)

        if self.cap.isOpened():
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self.cap.set(cv2.CAP_PROP_FPS, 60)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self.lock = threading.Lock()
        self.running = False
        self.frame: Optional[np.ndarray] = None
        self.success = False
        self.new_frame_event = threading.Event()

        if self.cap.isOpened():
            ok, first_frame = self.cap.read()
            if ok and first_frame is not None:
                self.success = True
                self.frame = first_frame
                self.new_frame_event.set()
            self.running = True
            self.thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.thread.start()

    def isOpened(self) -> bool:
        return self.cap.isOpened()

    def _reader_loop(self) -> None:
        while self.running:
            ok, frm = self.cap.read()
            if ok and frm is not None:
                with self.lock:
                    self.success = True
                    self.frame = frm
                self.new_frame_event.set()
            else:
                time.sleep(0.005)

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        # Yeni kare gelene kadar en fazla 35ms bekle (CPU'yu boşuna %100 yormaz)
        self.new_frame_event.wait(timeout=0.035)
        self.new_frame_event.clear()
        with self.lock:
            if not self.success or self.frame is None:
                return False, None
            return True, self.frame.copy()

    def release(self) -> None:
        self.running = False
        try:
            if hasattr(self, "thread") and self.thread.is_alive():
                self.thread.join(timeout=0.5)
        except Exception:
            pass
        self.cap.release()


# ============================================================================
# WINDOWS NATIVE MOUSE CONTROLLER (Ek paket gerektirmeden sıfır gecikmeli)
# ============================================================================
class WindowsMouseController:
    """Windows user32 API üzerinden hızlı fare kontrolü (SetCursorPos & mouse_event)."""

    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_WHEEL = 0x0800

    def __init__(self) -> None:
        self.available = sys.platform == "win32"
        self.left_Button_down = False
        if self.available:
            try:
                # DPI ölçeklendirmesinden etkilenmemek için Process DPI Awareness aktif et
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass
            self.user32 = ctypes.windll.user32
            self.screen_w = int(self.user32.GetSystemMetrics(0))
            self.screen_h = int(self.user32.GetSystemMetrics(1))
        else:
            self.user32 = None
            self.screen_w = 1920
            self.screen_h = 1080

    def move(self, x: int, y: int) -> None:
        if not self.available:
            return
        cx = max(0, min(self.screen_w - 1, int(x)))
        cy = max(0, min(self.screen_h - 1, int(y)))
        self.user32.SetCursorPos(cx, cy)

    def left_click(self, pos: Optional[Tuple[int, int]] = None) -> None:
        if not self.available:
            return
        if pos is not None:
            self.move(pos[0], pos[1])
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def double_click(self, pos: Optional[Tuple[int, int]] = None) -> None:
        if not self.available:
            return
        if pos is not None:
            self.move(pos[0], pos[1])
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.03)
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def right_click(self, pos: Optional[Tuple[int, int]] = None) -> None:
        if not self.available:
            return
        if pos is not None:
            self.move(pos[0], pos[1])
        self.user32.mouse_event(self.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(self.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)

    def start_drag(self) -> None:
        if not self.available or self.left_Button_down:
            return
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.left_Button_down = True

    def stop_drag(self) -> None:
        if not self.available or not self.left_Button_down:
            return
        self.user32.mouse_event(self.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.left_Button_down = False

    def scroll(self, delta: int) -> None:
        if not self.available or delta == 0:
            return
        self.user32.mouse_event(self.MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)


# ============================================================================
# SANAL NESNELER (Yumruk yaparak kamera ekranında kaydırılabilen kutular)
# ============================================================================
@dataclass
class DraggableObject:
    label: str
    x: int
    y: int
    w: int
    h: int
    color: Tuple[int, int, int]
    is_grabbed: bool = False
    offset_x: int = 0
    offset_y: int = 0

    def contains(self, px: int, py: int, margin: int = 25) -> bool:
        return (
            (self.x - margin) <= px <= (self.x + self.w + margin)
            and (self.y - margin) <= py <= (self.y + self.h + margin)
        )


def create_default_objects(frame_w: int = 960) -> List[DraggableObject]:
    x_pos = max(20, frame_w - 170)
    return [
        DraggableObject("Nesne 1", x_pos, 130, 140, 85, (255, 140, 50)),
        DraggableObject("Nesne 2", x_pos, 235, 140, 85, (80, 200, 120)),
        DraggableObject("Nesne 3", x_pos, 340, 140, 85, (180, 105, 255)),
    ]


# ============================================================================
# MEDIAPIPE EL TAKİP SARMALAYICI (Hem mp.solutions hem mp.tasks uyumlu)
# ============================================================================
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),          # Başparmak
    (0, 5), (5, 6), (6, 7), (7, 8),          # İşaret parmağı
    (5, 9), (9, 10), (10, 11), (11, 12),     # Orta parmak
    (9, 13), (13, 14), (14, 15), (15, 16),   # Yüzük parmağı
    (13, 17), (0, 17), (17, 18), (18, 19), (19, 20)  # Serçe parmak ve avuç
]


@dataclass
class Point3D:
    x: float
    y: float
    z: float = 0.0


@dataclass
class DetectedHand:
    landmarks: List[Point3D]
    label: str  # "Right" veya "Left"
    score: float = 1.0


class SinglePlayerHandLock:
    """
    Bir kişi oyuna başladığında sadece o kişinin elini takip eder (kilitler)
    ve kameraya giren başka elleri tamamen yok sayar.
    Aktif el kameradan tamamen çıkıp belirli bir süre (lock_timeout) görünmezse
    kilit sıfırlanır.
    """

    def __init__(self, lock_timeout: float = 1.6, max_jump_dist: float = 0.36) -> None:
        self.locked_pos: Optional[Tuple[float, float]] = None
        self.locked_label: Optional[str] = None
        self.locked_scale: float = 0.15
        self.last_seen_time: float = 0.0
        self.lock_timeout: float = lock_timeout
        self.max_jump_dist: float = max_jump_dist

    @staticmethod
    def _hand_center_and_scale(hand: DetectedHand) -> Tuple[Tuple[float, float], float]:
        lms = hand.landmarks
        cx = (lms[0].x + lms[5].x + lms[9].x + lms[17].x) * 0.25
        cy = (lms[0].y + lms[5].y + lms[9].y + lms[17].y) * 0.25
        scale = max(
            math.hypot(lms[0].x - lms[9].x, lms[0].y - lms[9].y),
            math.hypot(lms[5].x - lms[17].x, lms[5].y - lms[17].y),
            0.04,
        )
        return (cx, cy), scale

    def select_hand(self, detected_hands: List[DetectedHand]) -> Optional[DetectedHand]:
        now = time.perf_counter()

        # Kilitli el uzun süre görünmediyse kilidi serbest bırak
        if self.locked_pos is not None and (now - self.last_seen_time) > self.lock_timeout:
            self.locked_pos = None
            self.locked_label = None

        if not detected_hands:
            return None

        # Henüz kilitli bir el yoksa: Kameraya ilk giren (en yakın/büyük) eli kilitle
        if self.locked_pos is None:
            best_hand = max(
                detected_hands,
                key=lambda h: self._hand_center_and_scale(h)[1] * (0.7 + 0.3 * h.score),
            )
            center, scale = self._hand_center_and_scale(best_hand)
            self.locked_pos = center
            self.locked_label = best_hand.label
            self.locked_scale = scale
            self.last_seen_time = now
            return best_hand

        # Kilitli el varsa: Sadece kilitli elin konumuna ve el yönüne (Sağ/Sol) uyan eli kabul et,
        # sonradan kadraja giren diğer tüm elleri reddet.
        dt = max(0.0, now - self.last_seen_time)
        allowed_dist = min(0.45, self.max_jump_dist + dt * 0.18)

        best_match: Optional[DetectedHand] = None
        best_cost = float("inf")

        for hand in detected_hands:
            center, scale = self._hand_center_and_scale(hand)
            dist = math.hypot(center[0] - self.locked_pos[0], center[1] - self.locked_pos[1])

            if dist > allowed_dist:
                continue

            # Farklı el yönüne (Sağ <-> Sol) ve ani boyut değişimine ceza puanı uygula
            label_penalty = 0.0 if hand.label == self.locked_label else 0.14
            scale_diff = abs(scale - self.locked_scale) / max(0.05, self.locked_scale)
            if scale_diff > 0.65 and dist > 0.18:
                continue

            cost = dist + label_penalty + scale_diff * 0.08
            if cost < best_cost:
                best_cost = cost
                best_match = hand

        if best_match is not None:
            center, scale = self._hand_center_and_scale(best_match)
            self.locked_pos = center
            self.locked_scale = 0.7 * self.locked_scale + 0.3 * scale
            self.last_seen_time = now
            return best_match

        return None


class HandDetector:
    """MediaPipe Hands sarmalayıcısı (eski ve yeni MediaPipe sürümleriyle tam uyumlu)."""

    MODEL_URL = (
        "https://storage.googleapis.com/mediapipe-models/"
        "hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
    )

    def __init__(
        self,
        max_hands: int = 2,
        detection_conf: float = 0.7,
        tracking_conf: float = 0.7,
    ) -> None:
        self.use_solutions = hasattr(mp, "solutions") and hasattr(mp.solutions, "hands")
        self.hands = None
        self.landmarker = None
        self._timestamp_ms = 0

        if self.use_solutions:
            self.mp_hands = mp.solutions.hands
            self.hands = self.mp_hands.Hands(
                static_image_mode=False,
                max_num_hands=max_hands,
                model_complexity=1,
                min_detection_confidence=detection_conf,
                min_tracking_confidence=tracking_conf,
            )
        else:
            # MediaPipe 0.10.30+ Tasks API yedek desteği
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision

            model_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "hand_landmarker.task"
            )
            if not os.path.exists(model_path):
                print("MediaPipe HandLandmarker modeli indiriliyor...")
                urllib.request.urlretrieve(self.MODEL_URL, model_path)

            # Windows'ta Türkçe karakterli klasör yollarında ("görev" vb.) MediaPipe C++
            # dosya okuyucusunun hata vermemesi için modeli bellek tamponu olarak yüklüyoruz.
            with open(model_path, "rb") as f:
                model_bytes = f.read()

            base_options = mp_python.BaseOptions(model_asset_buffer=model_bytes)
            options = mp_vision.HandLandmarkerOptions(
                base_options=base_options,
                running_mode=mp_vision.RunningMode.VIDEO,
                num_hands=max_hands,
                min_hand_detection_confidence=detection_conf,
                min_hand_presence_confidence=detection_conf,
                min_tracking_confidence=tracking_conf,
            )
            self.landmarker = mp_vision.HandLandmarker.create_from_options(options)

    def detect(self, frame_bgr: np.ndarray) -> List[DetectedHand]:
        # MediaPipe kendi içinde görüntüyü 224x224'e küçülttüğü için, modele göndermeden
        # önce 480x270 boyutuna küçültmek doğruluğu hiç bozmadan işlemci yükünü %70 azaltır.
        h, w = frame_bgr.shape[:2]
        if w > 480:
            small_bgr = cv2.resize(frame_bgr, (480, int(h * 480 / w)), interpolation=cv2.INTER_LINEAR)
        else:
            small_bgr = frame_bgr

        rgb = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2RGB)
        detected: List[DetectedHand] = []

        if self.use_solutions:
            rgb.flags.writeable = False
            results = self.hands.process(rgb)
            rgb.flags.writeable = True

            if results.multi_hand_landmarks:
                handedness_list = results.multi_handedness or []
                for idx, hand_lms in enumerate(results.multi_hand_landmarks):
                    label = "Right"
                    score = 1.0
                    if idx < len(handedness_list):
                        cls = handedness_list[idx].classification[0]
                        label = cls.label
                        score = float(cls.score)
                    pts = [Point3D(lm.x, lm.y, lm.z) for lm in hand_lms.landmark]
                    detected.append(DetectedHand(landmarks=pts, label=label, score=score))
        else:
            rgb_contiguous = np.ascontiguousarray(rgb)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_contiguous)
            now_ms = int(time.perf_counter() * 1000)
            self._timestamp_ms = max(self._timestamp_ms + 1, now_ms)
            result = self.landmarker.detect_for_video(mp_image, self._timestamp_ms)
            if result.hand_landmarks:
                for idx, hand_lms in enumerate(result.hand_landmarks):
                    label = "Right"
                    score = 1.0
                    if idx < len(result.handedness) and result.handedness[idx]:
                        label = result.handedness[idx][0].category_name
                        score = float(result.handedness[idx][0].score)
                    pts = [Point3D(lm.x, lm.y, lm.z) for lm in hand_lms]
                    detected.append(DetectedHand(landmarks=pts, label=label, score=score))

        return detected

    def close(self) -> None:
        if self.hands is not None:
            self.hands.close()
        if self.landmarker is not None:
            self.landmarker.close()


# ============================================================================
# PARMAK SAYMA VE JEST (HAREKET) ANALİZİ
# ============================================================================
def dist2d(a: Point3D, b: Point3D) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def joint_angle_deg(a: Point3D, b: Point3D, c: Point3D) -> float:
    """b noktasındaki (a -> b -> c) eklem açısını derece cinsinden (0..180) hesaplar."""
    v1x, v1y = a.x - b.x, a.y - b.y
    v2x, v2y = c.x - b.x, c.y - b.y
    n1 = math.hypot(v1x, v1y)
    n2 = math.hypot(v2x, v2y)
    if n1 < 1e-6 or n2 < 1e-6:
        return 180.0
    cos_val = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (n1 * n2)))
    return math.degrees(math.acos(cos_val))


def count_open_fingers(hand: DetectedHand) -> List[int]:
    """
    Her parmak için [Başparmak, İşaret, Orta, Yüzük, Serçe] açık (1) / kapalı (0) döndürür.
    Hem sağ hem sol el, hem avuç içi hem el sırtı dönükken doğru çalışır.
    """
    lms = hand.landmarks
    wrist = lms[0]
    fingers: List[int] = [0, 0, 0, 0, 0]

    # --- 1. Başparmak (Landmark 4: Uç, 3: IP, 2: MCP, 5: İşaret MCP, 17: Serçe MCP) ---
    # Avuç genişliği referansı
    palm_width = max(dist2d(lms[5], lms[17]), 1e-4)
    # Başparmak ucu serçe parmak köküne (17) ve orta parmak köküne (9),
    # başparmak orta ekleminden (3 ve 2) belirgin şekilde daha uzaksa açıktır.
    thumb_tip_to_pinky = dist2d(lms[4], lms[17])
    thumb_ip_to_pinky = dist2d(lms[3], lms[17])
    thumb_mcp_to_pinky = dist2d(lms[2], lms[17])
    thumb_tip_to_index_mcp = dist2d(lms[4], lms[5])

    if (
        thumb_tip_to_pinky > thumb_ip_to_pinky * 1.07
        and thumb_tip_to_pinky > thumb_mcp_to_pinky * 1.12
        and thumb_tip_to_index_mcp > palm_width * 0.48
    ):
        fingers[0] = 1

    # --- 2. Diğer 4 Parmak (İşaret: 8, Orta: 12, Yüzük: 16, Serçe: 20) ---
    tip_ids = [8, 12, 16, 20]
    pip_ids = [6, 10, 14, 18]
    mcp_ids = [5, 9, 13, 17]

    # Yüzük ve serçe parmakları tek başına kaldırıldığında anatomik olarak tam 180 derece
    # açılamadığı için eşik değerlerini parmağa özel hassasiyetle belirliyoruz.
    pip_mults = [1.07, 1.07, 1.01, 1.03]
    mcp_mults = [1.22, 1.22, 1.12, 1.15]

    for i in range(4):
        tip = lms[tip_ids[i]]
        pip = lms[pip_ids[i]]
        mcp = lms[mcp_ids[i]]

        tip_wrist_dist = dist2d(tip, wrist)
        pip_wrist_dist = dist2d(pip, wrist)
        mcp_wrist_dist = dist2d(mcp, wrist)

        if (
            tip_wrist_dist > pip_wrist_dist * pip_mults[i]
            and tip_wrist_dist > mcp_wrist_dist * mcp_mults[i]
        ):
            if tip.y < pip.y + 0.03:
                fingers[i + 1] = 1

    return fingers


def is_fist_gesture(hand: DetectedHand, fingers: List[int], is_pinching: bool) -> bool:
    """
    Yumruk (0 parmak açık) durumunu tespit eder.
    Başparmak-işaret parmağı birleştirme (pinch) hareketinden ayırt eder.
    """
    if is_pinching:
        return False
    # 4 ana parmak kapalı olmalı
    if sum(fingers[1:]) > 0:
        return False
    lms = hand.landmarks
    wrist = lms[0]
    # İşaret ve orta parmak uçlarının avuç içine / bileğe kıvrılmış olması
    palm_len = max(dist2d(wrist, lms[9]), 1e-4)
    index_curled = dist2d(lms[8], wrist) < palm_len * 1.45
    middle_curled = dist2d(lms[12], wrist) < palm_len * 1.45
    return index_curled and middle_curled


# ============================================================================
# HAREKET VE FARE YÖNETİCİSİ (Pinch İmleç, Serçe 1x/2x Sol Tık, Orta Sağ Tık, Yumruk Kaydırma)
# ============================================================================
@dataclass
class GestureMouseState:
    enabled: bool = True
    fist_mode: str = "DRAG"  # "DRAG" (Sürükle-Bırak) veya "SCROLL" (Sayfa Kaydır)
    show_virtual_objects: bool = True

    # Pinch (Başparmak + İşaret birleştirme) durumu
    is_pinching: bool = False

    # Serçe parmak: 1 kez aç-kapat -> Tek Sol Tık, 2 kez aç-kapat -> Çift Sol Tık
    pinky_is_up: bool = False
    pinky_up_time: float = 0.0
    pinky_cycle_count: int = 0
    pinky_last_close_time: float = 0.0
    double_cycle_window: float = 0.48  # 1. kapatmadan sonra 2. açma için bekleme penceresi (sn)

    # Orta parmak: Sağ Tık durumu
    middle_lifted: bool = False
    middle_up_frames: int = 0
    middle_lift_time: float = 0.0

    last_click_time: float = 0.0
    click_anchor_screen: Optional[Tuple[int, int]] = None

    # Yumruk durumu
    is_fist: bool = False
    prev_fist_pos: Optional[Tuple[float, float]] = None

    # İmleç yumuşatma
    smooth_x: Optional[float] = None
    smooth_y: Optional[float] = None

    # Görsel bildirim (Son yapılan aksiyon)
    action_text: str = "Hazir"
    action_color: Tuple[int, int, int] = (200, 200, 200)
    action_until: float = 0.0

    # Aktif kamera alanı (kenarlara kolay ulaşmak için çerçeve marjı)
    margin_x: float = 0.16
    margin_y: float = 0.16

    def set_feedback(self, text: str, color: Tuple[int, int, int], duration: float = 1.0) -> None:
        self.action_text = text
        self.action_color = color
        self.action_until = time.perf_counter() + duration

    def map_to_screen(self, norm_x: float, norm_y: float, screen_w: int, screen_h: int) -> Tuple[int, int]:
        # Kamera içindeki aktif dikdörtgeni [0, 1] aralığına ölçekle
        nx = (norm_x - self.margin_x) / max(1e-4, (1.0 - 2.0 * self.margin_x))
        ny = (norm_y - self.margin_y) / max(1e-4, (1.0 - 2.0 * self.margin_y))
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))

        target_x = nx * screen_w
        target_y = ny * screen_h

        # Adaptif üstel yumuşatma (titremeyi önler, hızlı harekette gecikmez)
        if self.smooth_x is None or self.smooth_y is None:
            self.smooth_x = target_x
            self.smooth_y = target_y
        else:
            jump = math.hypot(target_x - self.smooth_x, target_y - self.smooth_y)
            alpha = min(0.65, max(0.18, jump / 180.0))
            self.smooth_x += alpha * (target_x - self.smooth_x)
            self.smooth_y += alpha * (target_y - self.smooth_y)

        return int(self.smooth_x), int(self.smooth_y)


def process_gestures(
    hand: Optional[DetectedHand],
    fingers: List[int],
    state: GestureMouseState,
    mouse: WindowsMouseController,
    objects: List[DraggableObject],
    frame_w: int,
    frame_h: int,
) -> dict:
    """
    El hareketlerini değerlendirir ve fare/nesne aksiyonlarını çalıştırır:
    - Başparmak + İşaret birleştirme (Pinch): Fare imlecini hareket ettirir.
    - Serçe parmağı 1 kez aç-kapat: Tek Sol Tık.
    - Serçe parmağı 2 kez aç-kapat: Çift Sol Tık.
    - Orta parmağı kaldırıp indirme: Sağ Tık.
    - Yumruk (0 parmak açık): Nesneleri/pencereleri sürükler veya sayfayı kaydırır.
    """
    now = time.perf_counter()
    info = {
        "pinch_px": None,
        "thumb_px": None,
        "index_px": None,
        "palm_px": None,
        "pinch_ratio": 1.0,
        "gesture_name": "El Yok",
    }

    # Bekleyen 1x Serçe Aç-Kapat süresi dolduysa (ve 2. kez serçe kaldırılmadıysa) -> TEK SOL TIK!
    if (
        state.pinky_cycle_count == 1
        and not state.pinky_is_up
        and (now - state.pinky_last_close_time) > state.double_cycle_window
    ):
        if state.enabled:
            mouse.left_click(state.click_anchor_screen)
        state.last_click_time = now
        state.pinky_cycle_count = 0
        state.click_anchor_screen = None
        state.set_feedback("TEK SOL TIK! (1x Serce Ac-Kapat)", (0, 255, 120), 1.1)

    if hand is None:
        # El ekrandan çıktıysa basılı tutulan sürüklemeleri ve durumları sıfırla
        state.is_pinching = False
        state.pinky_is_up = False
        state.pinky_cycle_count = 0
        state.middle_lifted = False
        state.middle_up_frames = 0
        state.click_anchor_screen = None
        state.is_fist = False
        state.prev_fist_pos = None
        mouse.stop_drag()
        for obj in objects:
            obj.is_grabbed = False
        return info

    lms = hand.landmarks
    thumb_tip = lms[4]
    index_tip = lms[8]
    palm_center = lms[9]  # Orta parmak MCP (avuç merkezi)

    thumb_px = (int(thumb_tip.x * frame_w), int(thumb_tip.y * frame_h))
    index_px = (int(index_tip.x * frame_w), int(index_tip.y * frame_h))
    pinch_mid_norm = ((thumb_tip.x + index_tip.x) * 0.5, (thumb_tip.y + index_tip.y) * 0.5)
    pinch_px = (int(pinch_mid_norm[0] * frame_w), int(pinch_mid_norm[1] * frame_h))
    palm_px = (int(palm_center.x * frame_w), int(palm_center.y * frame_h))

    info["thumb_px"] = thumb_px
    info["index_px"] = index_px
    info["pinch_px"] = pinch_px
    info["palm_px"] = palm_px

    # Avuç büyüklüğüne göre normalize edilmiş başparmak-işaret mesafesi
    palm_scale = max(dist2d(lms[0], lms[9]), dist2d(lms[5], lms[17]), 0.05)
    pinch_dist = dist2d(thumb_tip, index_tip)
    pinch_ratio = pinch_dist / palm_scale
    info["pinch_ratio"] = pinch_ratio

    # İşaret parmağı avuç içine tamamen gömülü değilse (yumruk değilse) pinch adaylığı
    index_extended_enough = dist2d(lms[8], lms[0]) > dist2d(lms[5], lms[0]) * 0.85
    other_fingers_open = sum(fingers[2:]) > 0

    # Histerezisli Pinch (Başparmak + İşaret Birleştirme) tespiti
    if state.is_pinching:
        now_pinching = (pinch_ratio < 0.45) and (index_extended_enough or other_fingers_open)
    else:
        now_pinching = (pinch_ratio < 0.33) and (index_extended_enough or other_fingers_open)

    # Yumruk (Fist) tespiti
    now_fist = is_fist_gesture(hand, fingers, now_pinching)

    # --- 1. SERÇE (1x AÇ-KAPAT: TEK TIK, 2x AÇ-KAPAT: ÇİFT TIK) & ORTA PARMAK (SAĞ TIK) ---
    # fingers = [Başparmak(0), İşaret(1), Orta(2), Yüzük(3), Serçe(4)]
    middle_open = fingers[2] == 1
    ring_open = fingers[3] == 1
    pinky_open = fingers[4] == 1

    # El tamamen açıksa (parmak sayma gösterimi: 3+ ana parmak açık ve pinch yoksa),
    # tıklama tetiklemelerini devre dışı bırak.
    full_open_palm = (not now_pinching) and (sum(fingers[1:]) >= 3)

    if full_open_palm:
        state.pinky_is_up = False
        state.pinky_cycle_count = 0
        state.middle_lifted = False
        state.middle_up_frames = 0
        state.click_anchor_screen = None
    else:
        # --------------------------------------------------------------------
        # A) SERÇE PARMAK: 1 Kez Aç-Kapat -> Tek Tık | 2 Kez Aç-Kapat -> Çift Tık
        # --------------------------------------------------------------------
        if pinky_open and not middle_open and not ring_open:
            if not state.pinky_is_up:
                state.pinky_is_up = True
                state.pinky_up_time = now
                # İlk açılışta imleç konumunu sabitle
                if state.pinky_cycle_count == 0 and state.smooth_x is not None and state.smooth_y is not None:
                    state.click_anchor_screen = (int(state.smooth_x), int(state.smooth_y))
            if state.pinky_cycle_count == 0:
                state.set_feedback("Serce Acik (1/2) -> Kapatinca Tik", (120, 255, 160), 0.25)
            else:
                state.set_feedback("Serce Acik (2/2) -> Kapatinca CIFT TIK!", (0, 255, 255), 0.3)

        elif state.pinky_is_up and not pinky_open:
            # Serçe açıldıktan sonra kapatıldı -> 1 "Aç-Kapat" döngüsü tamamlandı
            up_dur = now - state.pinky_up_time
            state.pinky_is_up = False

            if 0.02 <= up_dur <= 1.80:
                # Önceki döngünün üzerinden çok vakit geçtiyse sayacı yeniden başlat
                if state.pinky_cycle_count > 0 and (now - state.pinky_last_close_time) > 1.0:
                    state.pinky_cycle_count = 1
                else:
                    state.pinky_cycle_count += 1

                state.pinky_last_close_time = now

                if state.pinky_cycle_count >= 2:
                    # 2 kez Aç-Kapat tamamlandı -> ÇİFT SOL TIK!
                    if state.enabled:
                        mouse.double_click(state.click_anchor_screen)
                    state.last_click_time = now
                    state.pinky_cycle_count = 0
                    state.click_anchor_screen = None
                    state.set_feedback("CIFT SOL TIK! (2x Serce Ac-Kapat)", (0, 255, 255), 1.2)
                else:
                    state.set_feedback("1x Ac-Kapat (Cift tik icin 1 kez daha...)", (255, 230, 80), 0.5)
            else:
                state.pinky_cycle_count = 0
                state.click_anchor_screen = None

        # --------------------------------------------------------------------
        # B) ORTA PARMAK: Kaldırıp İndirince SAĞ TIK
        # --------------------------------------------------------------------
        if middle_open and not ring_open and not pinky_open:
            state.middle_up_frames += 1
            if not state.middle_lifted and state.middle_up_frames >= 2:
                state.middle_lifted = True
                state.middle_lift_time = now
                if state.smooth_x is not None and state.smooth_y is not None:
                    state.click_anchor_screen = (int(state.smooth_x), int(state.smooth_y))
                state.set_feedback("Orta Parmak Havada -> Indirince SAG TIK", (80, 200, 255), 0.3)
        else:
            state.middle_up_frames = 0
            if state.middle_lifted and not middle_open:
                lift_dur = now - state.middle_lift_time
                state.middle_lifted = False
                if 0.03 <= lift_dur <= 2.0 and (now - state.last_click_time) > 0.25:
                    if state.enabled:
                        mouse.right_click(state.click_anchor_screen)
                    state.last_click_time = now
                    state.set_feedback("SAG TIK! (Orta Parmak)", (0, 165, 255), 1.1)
                state.click_anchor_screen = None

    # --- 2. PINCH (BAŞPARMAK + İŞARET PARMAĞI BİRLEŞTİRME) İLE İMLEÇ YÖNETİMİ ---
    if now_pinching:
        # Yumruk sürüklemesi varsa bırak
        if state.is_fist:
            mouse.stop_drag()
            for obj in objects:
                obj.is_grabbed = False
            state.is_fist = False
            state.prev_fist_pos = None

        state.is_pinching = True
        screen_pos = state.map_to_screen(
            pinch_mid_norm[0], pinch_mid_norm[1], mouse.screen_w, mouse.screen_h
        )

        # Tıklama (serçe veya orta parmak) hareketi devam ederken imleci hedefte sabitle
        freeze_for_click = (
            state.middle_lifted
            or state.pinky_is_up
            or state.pinky_cycle_count > 0
        )
        if state.enabled and not freeze_for_click:
            mouse.move(screen_pos[0], screen_pos[1])

        info["gesture_name"] = "Imlec Kontrol (Pinch)"
    else:
        state.is_pinching = False

    # --- 2. YUMRUK (NESNE KAYDIRMA / SÜRÜKLEME) MANTIĞI ---
    if now_fist:
        state.is_fist = True
        info["gesture_name"] = f"Yumruk: Kaydirma ({state.fist_mode})"

        # A) Ekrandaki sanal nesneleri yakala ve kaydır
        grabbed_any_virtual = False
        if state.show_virtual_objects:
            for obj in reversed(objects):
                if obj.is_grabbed:
                    grabbed_any_virtual = True
                    break
            if not grabbed_any_virtual:
                for obj in reversed(objects):
                    if obj.contains(palm_px[0], palm_px[1]):
                        obj.is_grabbed = True
                        obj.offset_x = palm_px[0] - obj.x
                        obj.offset_y = palm_px[1] - obj.y
                        grabbed_any_virtual = True
                        state.set_feedback(f"{obj.label} Kaydiriliyor!", obj.color, 0.5)
                        break

            for obj in objects:
                if obj.is_grabbed:
                    obj.x = max(10, min(frame_w - obj.w - 10, palm_px[0] - obj.offset_x))
                    obj.y = max(70, min(frame_h - obj.h - 10, palm_px[1] - obj.offset_y))

        # B) Windows Fare Sürükleme (DRAG) veya Sayfa Kaydırma (SCROLL)
        if state.enabled:
            screen_pos = state.map_to_screen(
                palm_center.x, palm_center.y, mouse.screen_w, mouse.screen_h
            )
            if state.fist_mode == "DRAG":
                mouse.move(screen_pos[0], screen_pos[1])
                if not grabbed_any_virtual:
                    mouse.start_drag()
                    state.set_feedback("Yumruk: Nesne Surukleniyor", (255, 120, 255), 0.3)
            else:  # SCROLL modu
                mouse.stop_drag()
                if state.prev_fist_pos is not None:
                    dy = palm_center.y - state.prev_fist_pos[1]
                    if abs(dy) > 0.006:
                        scroll_amount = int(-dy * 1800)
                        mouse.scroll(scroll_amount)
                        direction = "Yukari" if scroll_amount > 0 else "Asagi"
                        state.set_feedback(f"Yumruk: Sayfa Kaydir ({direction})", (255, 180, 50), 0.3)
            state.prev_fist_pos = (palm_center.x, palm_center.y)
    else:
        if state.is_fist:
            state.is_fist = False
            state.prev_fist_pos = None
            mouse.stop_drag()
            for obj in objects:
                obj.is_grabbed = False

    if not now_pinching and not now_fist:
        info["gesture_name"] = f"Acik El ({sum(fingers)} Parmak)"

    return info


# ============================================================================
# GÖRSEL ÇİZİM VE ARAYÜZ (HUD) FONKSİYONLARI
# ============================================================================
FINGER_NAMES = ["Bas", "Isaret", "Orta", "Yuzuk", "Serce"]


def draw_hand_skeleton(
    frame: np.ndarray,
    hand: DetectedHand,
    fingers: List[int],
) -> None:
    """Elin 21 eklem noktasını ve bağlantı çizgilerini ekrana çizer."""
    h, w, _ = frame.shape
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in hand.landmarks]

    # Bağlantı çizgileri
    for start_idx, end_idx in HAND_CONNECTIONS:
        cv2.line(frame, pts[start_idx], pts[end_idx], (220, 220, 220), 2, cv2.LINE_AA)

    # Parmak uçları (4, 8, 12, 16, 20) açık/kapalı durumuna göre renklendirilir
    tip_indices = [4, 8, 12, 16, 20]
    for idx, (px, py) in enumerate(pts):
        if idx in tip_indices:
            f_idx = tip_indices.index(idx)
            is_open = fingers[f_idx] == 1
            color = (0, 255, 100) if is_open else (60, 70, 230)
            cv2.circle(frame, (px, py), 8, color, -1, cv2.LINE_AA)
            cv2.circle(frame, (px, py), 10, (255, 255, 255), 1, cv2.LINE_AA)
        else:
            cv2.circle(frame, (px, py), 5, (255, 190, 0), -1, cv2.LINE_AA)
            cv2.circle(frame, (px, py), 6, (30, 30, 30), 1, cv2.LINE_AA)


def draw_virtual_objects(frame: np.ndarray, objects: List[DraggableObject]) -> None:
    """Yumruk yaparak kaydırılabilen sanal kutuları çizer (sadece kutu bölgesini harmanlar)."""
    fh, fw = frame.shape[:2]
    for obj in objects:
        x1 = max(0, obj.x)
        y1 = max(0, obj.y)
        x2 = min(fw, obj.x + obj.w)
        y2 = min(fh, obj.y + obj.h)
        if x2 > x1 and y2 > y1:
            roi = frame[y1:y2, x1:x2]
            color_rect = np.full_like(roi, obj.color)
            cv2.addWeighted(color_rect, 0.55, roi, 0.45, 0, roi)

        border_col = (0, 255, 255) if obj.is_grabbed else (255, 255, 255)
        thickness = 3 if obj.is_grabbed else 2
        cv2.rectangle(
            frame,
            (obj.x, obj.y),
            (obj.x + obj.w, obj.y + obj.h),
            border_col,
            thickness,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            obj.label,
            (obj.x + 18, obj.y + 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        status_txt = "KAYDIRILIYOR" if obj.is_grabbed else "Yumrukla Tut"
        cv2.putText(
            frame,
            status_txt,
            (obj.x + 15, obj.y + 66),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (240, 240, 240),
            1,
            cv2.LINE_AA,
        )


def draw_hud(
    frame: np.ndarray,
    total_fingers: int,
    per_hand_details: List[Tuple[str, List[int]]],
    state: GestureMouseState,
    gesture_info: dict,
    fps: float,
) -> None:
    """Ekran üzerindeki bilgi panelini, parmak sayısını ve aktif jest bilgisini çizer."""
    h, w, _ = frame.shape
    now = time.perf_counter()

    # Fare kontrol aktif çerçevesi
    if state.enabled:
        mx1 = int(state.margin_x * w)
        my1 = int(state.margin_y * h)
        mx2 = int((1.0 - state.margin_x) * w)
        my2 = int((1.0 - state.margin_y) * h)
        cv2.rectangle(frame, (mx1, my1), (mx2, my2), (100, 100, 100), 1, cv2.LINE_AA)
        cv2.putText(
            frame,
            "Fare Aktif Alani",
            (mx1 + 8, my2 - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (140, 140, 140),
            1,
            cv2.LINE_AA,
        )

    # Pinch (Başparmak + İşaret) görsel çizgisi
    if gesture_info["thumb_px"] and gesture_info["index_px"]:
        t_px = gesture_info["thumb_px"]
        i_px = gesture_info["index_px"]
        p_px = gesture_info["pinch_px"]
        line_col = (0, 255, 255) if state.is_pinching else (180, 180, 180)
        cv2.line(frame, t_px, i_px, line_col, 2, cv2.LINE_AA)
        if state.is_pinching and p_px:
            cv2.circle(frame, p_px, 12, (0, 255, 255), 2, cv2.LINE_AA)
            cv2.circle(frame, p_px, 5, (0, 255, 0), -1, cv2.LINE_AA)

    # Yumruk görsel halkası
    if state.is_fist and gesture_info["palm_px"]:
        cv2.circle(frame, gesture_info["palm_px"], 28, (255, 100, 255), 3, cv2.LINE_AA)

    # Üst Yarı-Saydam Bilgi Paneli
    panel_h = 115
    sub = frame[0:panel_h, 0:w]
    black_rect = np.zeros(sub.shape, dtype=np.uint8)
    cv2.addWeighted(sub, 0.35, black_rect, 0.65, 0, frame[0:panel_h, 0:w])

    # Sol Kutu: Açık Parmak Sayısı (Büyük Gösterge)
    cv2.rectangle(frame, (15, 12), (245, 102), (0, 190, 90), -1, cv2.LINE_AA)
    cv2.putText(
        frame,
        "ACIK PARMAK",
        (28, 36),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (230, 255, 230),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        str(total_fingers),
        (105, 92),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.9,
        (255, 255, 255),
        4,
        cv2.LINE_AA,
    )

    # Orta Bölüm: Açık/Kapalı Parmak Detayı ve Hareket Durumu
    if per_hand_details:
        hand_label, f_list = per_hand_details[0]
        tr_label = "Sag El" if hand_label == "Right" else "Sol El"
        open_names = [FINGER_NAMES[i] for i, val in enumerate(f_list) if val == 1]
        detail_str = f"{tr_label}: " + (", ".join(open_names) if open_names else "Yumruk (0)")
    else:
        detail_str = "Kameraya elinizi gosterin..."

    cv2.putText(
        frame,
        detail_str,
        (265, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )

    cv2.putText(
        frame,
        f"Hareket: {gesture_info['gesture_name']}",
        (265, 68),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.60,
        (100, 235, 255),
        2,
        cv2.LINE_AA,
    )

    # Son Aksiyon Bildirimi (Sol Tık, Sağ Tık, Kaydırma vb.)
    feedback_txt = (
        state.action_text
        if now < state.action_until
        else "Pinch: Imlec | Serce: Sol/Cift Tik | Orta: Sag Tik | Yumruk: Kaydir"
    )
    feedback_col = state.action_color if now < state.action_until else (190, 190, 190)
    cv2.putText(
        frame,
        feedback_txt,
        (265, 97),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.52,
        feedback_col,
        2,
        cv2.LINE_AA,
    )

    # Sağ Üst: FPS ve Kısayollar
    cv2.putText(
        frame,
        f"FPS: {int(fps)}",
        (w - 130, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (0, 255, 180),
        2,
        cv2.LINE_AA,
    )
    mouse_status = "ACIK" if state.enabled else "KAPALI"
    cv2.putText(
        frame,
        f"[B] Fare: {mouse_status}",
        (w - 195, 64),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.50,
        (0, 255, 100) if state.enabled else (80, 80, 255),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        f"[M] Yumruk: {state.fist_mode}",
        (w - 195, 86),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.50,
        (220, 220, 220),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        "[G] Oyun [O] Kutu [R] Sifirla [Q] Cikis",
        (w - 310, 106),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.44,
        (100, 255, 220),
        1,
        cv2.LINE_AA,
    )


# ============================================================================
# MEYVE ÇİZİM OYUNU ([G] TUŞU İLE BEYAZ SAYFADA EL İLE MEYVE ÇİZME OYUNU)
# ============================================================================
@dataclass
class FruitSpec:
    name: str                  # Ekranda gösterilecek meyve adı (örn: "ELMA")
    key: str                   # Şablon anahtarı
    color: Tuple[int, int, int]  # BGR fırça rengi
    hint: str                  # Kısa çizim ipucu
    aspect_range: Tuple[float, float]  # Beklenen genişlik/yükseklik oranı aralığı


FRUIT_LIST: List[FruitSpec] = [
    FruitSpec("ELMA", "apple", (40, 40, 225), "Yuvarlak govde ve ustte kucuk sap cizin", (0.70, 1.35)),
    FruitSpec("MUZ", "banana", (0, 210, 255), "Kavisli hilal / yay seklinde cizin", (0.45, 2.20)),
    FruitSpec("PORTAKAL", "orange", (0, 140, 255), "Tam yuvarlak bir daire cizin", (0.75, 1.30)),
    FruitSpec("CILEK", "strawberry", (60, 30, 230), "Ustu genis, alti sivri ucgen/kalp gibi cizin", (0.65, 1.35)),
    FruitSpec("ARMUT", "pear", (40, 195, 85), "Ustu dar, alti genis damla seklinde cizin", (0.55, 1.15)),
    FruitSpec("KARPUZ DILIMI", "watermelon", (50, 185, 50), "Yarim daire veya ucgen dilim cizin", (0.85, 2.20)),
    FruitSpec("LIMON", "lemon", (15, 220, 250), "Yatay oval / elips seklinde cizin", (0.95, 2.10)),
    FruitSpec("KIRAZ", "cherry", (45, 25, 200), "Sapli yuvarlak kiraz seklinde cizin", (0.55, 1.45)),
]


def draw_fruit_shape(
    img: np.ndarray,
    fruit_key: str,
    center: Tuple[int, int],
    radius: int,
    color: Tuple[int, int, int],
    thickness: int = 4,
) -> None:
    """
    Verilen meyve anahtarına göre referans meyve çizimini (ikon, kılavuz veya
    değerlendirme şablonu için) img üzerine çizer.
    """
    cx, cy = center
    r = max(12, int(radius))

    if fruit_key == "apple":
        # Elma gövdesi (hafif basık daire + üst/alt çukur hissi) ve üst sap
        cv2.ellipse(img, (cx, int(cy + r * 0.08)), (int(r * 0.85), int(r * 0.78)), 0, 0, 360, color, thickness, cv2.LINE_AA)
        # Üst sap
        cv2.line(img, (cx, int(cy - r * 0.65)), (int(cx + r * 0.18), int(cy - r * 0.98)), color, thickness, cv2.LINE_AA)

    elif fruit_key == "orange":
        # Tam yuvarlak portakal + üstte küçük tepe noktası
        cv2.circle(img, (cx, cy), int(r * 0.85), color, thickness, cv2.LINE_AA)
        cv2.circle(img, (cx, int(cy - r * 0.65)), max(2, thickness), color, -1, cv2.LINE_AA)

    elif fruit_key == "banana":
        # Hilal / kavisli muz (dış ve iç yay)
        pts_outer = []
        pts_inner = []
        for deg in range(-135, 46, 10):
            rad = math.radians(deg)
            ox = int(cx + r * 0.15 + math.cos(rad) * r * 0.95)
            oy = int(cy - r * 0.10 + math.sin(rad) * r * 0.95)
            pts_outer.append([ox, oy])
        for deg in range(45, -136, -10):
            rad = math.radians(deg)
            ix = int(cx + r * 0.02 + math.cos(rad) * r * 0.58)
            iy = int(cy - r * 0.02 + math.sin(rad) * r * 0.58)
            pts_inner.append([ix, iy])
        poly = np.array(pts_outer + pts_inner, dtype=np.int32)
        cv2.polylines(img, [poly], isClosed=True, color=color, thickness=thickness, lineType=cv2.LINE_AA)

    elif fruit_key == "strawberry":
        # Üstü geniş, altı sivri çilek gövdesi
        pts = np.array([
            [cx, int(cy - r * 0.72)],
            [int(cx + r * 0.55), int(cy - r * 0.65)],
            [int(cx + r * 0.82), int(cy - r * 0.25)],
            [int(cx + r * 0.55), int(cy + r * 0.35)],
            [cx, int(cy + r * 0.88)],
            [int(cx - r * 0.55), int(cy + r * 0.35)],
            [int(cx - r * 0.82), int(cy - r * 0.25)],
            [int(cx - r * 0.55), int(cy - r * 0.65)],
        ], dtype=np.int32)
        cv2.polylines(img, [pts], isClosed=True, color=color, thickness=thickness, lineType=cv2.LINE_AA)
        # Üst yaprak/sap
        cv2.line(img, (int(cx - r * 0.30), int(cy - r * 0.75)), (int(cx + r * 0.30), int(cy - r * 0.75)), color, thickness, cv2.LINE_AA)

    elif fruit_key == "pear":
        # Üstü dar, altı geniş armut konturu
        pts = np.array([
            [cx, int(cy - r * 0.82)],
            [int(cx + r * 0.36), int(cy - r * 0.62)],
            [int(cx + r * 0.45), int(cy - r * 0.20)],
            [int(cx + r * 0.78), int(cy + r * 0.28)],
            [int(cx + r * 0.65), int(cy + r * 0.72)],
            [cx, int(cy + r * 0.88)],
            [int(cx - r * 0.65), int(cy + r * 0.72)],
            [int(cx - r * 0.78), int(cy + r * 0.28)],
            [int(cx - r * 0.45), int(cy - r * 0.20)],
            [int(cx - r * 0.36), int(cy - r * 0.62)],
        ], dtype=np.int32)
        cv2.polylines(img, [pts], isClosed=True, color=color, thickness=thickness, lineType=cv2.LINE_AA)
        cv2.line(img, (cx, int(cy - r * 0.82)), (int(cx + r * 0.12), int(cy - r * 1.0)), color, thickness, cv2.LINE_AA)

    elif fruit_key == "watermelon":
        # Yarım daire karpuz dilimi (üst düz çizgi + alt yarım çember yay)
        cv2.ellipse(img, (cx, int(cy - r * 0.25)), (int(r * 0.92), int(r * 0.88)), 0, 0, 180, color, thickness, cv2.LINE_AA)
        cv2.line(
            img,
            (int(cx - r * 0.92), int(cy - r * 0.25)),
            (int(cx + r * 0.92), int(cy - r * 0.25)),
            color,
            thickness,
            cv2.LINE_AA,
        )

    elif fruit_key == "lemon":
        # Yatay oval limon + iki uçta hafif sivrilik
        cv2.ellipse(img, (cx, cy), (int(r * 0.95), int(r * 0.62)), 0, 0, 360, color, thickness, cv2.LINE_AA)

    elif fruit_key == "cherry":
        # Alt kısımda yuvarlak kiraz + yukarı uzanan sap
        cv2.circle(img, (cx, int(cy + r * 0.30)), int(r * 0.56), color, thickness, cv2.LINE_AA)
        cv2.line(
            img,
            (cx, int(cy - r * 0.24)),
            (int(cx + r * 0.25), int(cy - r * 0.90)),
            color,
            thickness,
            cv2.LINE_AA,
        )


def _normalize_binary_drawing(mask: np.ndarray, out_size: int = 128, pad: int = 14) -> Optional[Tuple[np.ndarray, float]]:
    """
    Ikili (binary) çizim maskesindeki dolu piksellerin sınırlayıcı kutusunu (bounding box)
    bulur ve en/boy oranını koruyarak out_size x out_size boyutuna normalize eder.
    Dönüş: (normalized_mask, aspect_ratio) veya çizim yoksa None.
    """
    ys, xs = np.where(mask > 0)
    if len(xs) < 25:
        return None

    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())
    bw = max(1, x2 - x1 + 1)
    bh = max(1, y2 - y1 + 1)
    if bw < 20 and bh < 20:
        return None

    aspect = float(bw) / float(bh)
    crop = mask[y1 : y2 + 1, x1 : x2 + 1]

    target_box = out_size - 2 * pad
    scale = min(target_box / float(bw), target_box / float(bh))
    nw = max(1, int(round(bw * scale)))
    nh = max(1, int(round(bh * scale)))

    resized = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_NEAREST)
    canvas = np.zeros((out_size, out_size), dtype=np.uint8)
    ox = (out_size - nw) // 2
    oy = (out_size - nh) // 2
    canvas[oy : oy + nh, ox : ox + nw] = resized

    # Çizgi kalınlığını standartlaştır
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    canvas = cv2.dilate(canvas, kernel, iterations=1)
    return canvas, aspect


def _fill_closed_regions(binary_mask: np.ndarray) -> Tuple[np.ndarray, float]:
    """
    Kullanıcının çizdiği konturdaki küçük kopuklukları kapatır ve iç bölgesini doldurur.
    Düz çizgi veya açık karalamalarda iç alan (enclosed_ratio) ~0 çıkar; kapalı meyve
    şekillerinde ise belirgin bir iç alan oluşur.
    """
    h, w = binary_mask.shape[:2]
    # Küçük birleşim boşluklarını kapat (kullanıcı daireyi tam ucuca kapatamamış olabilir)
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    closed = cv2.morphologyEx(binary_mask, cv2.MORPH_CLOSE, close_kernel)

    # Kenarlardan taşma (floodFill) ile dış arka planı bul
    padded = np.zeros((h + 2, w + 2), dtype=np.uint8)
    padded[1 : h + 1, 1 : w + 1] = closed
    flood = padded.copy()
    ff_mask = np.zeros((h + 4, w + 4), dtype=np.uint8)
    cv2.floodFill(flood, ff_mask, (0, 0), 255)

    # FloodFill'in ulaşamadığı iç boşluklar = kapalı şeklin iç alanı
    interior = cv2.bitwise_not(flood[1 : h + 1, 1 : w + 1])
    filled = cv2.bitwise_or(closed, interior)

    interior_only = cv2.bitwise_and(interior, cv2.bitwise_not(closed))
    enclosed_ratio = float(np.count_nonzero(interior_only)) / float(h * w)
    return filled, enclosed_ratio


def evaluate_fruit_drawing(user_mask: np.ndarray, fruit: FruitSpec) -> Tuple[bool, int]:
    """
    Kullanıcının çizimini hedef meyve şablonuyla sıkı şekilde karşılaştırır:
    1. Doğrusallık (PCA) kontrolü: Düz/çapraz çizgileri anında reddeder.
    2. Kapalı Alan (Enclosed Area) kontrolü: İçi boş açık çizgileri/zikzakları reddeder.
    3. Kontur İsabeti (Precision & Recall F1) + Dolgu Alan Örtüşmesi (IoU) + Chamfer.
    """
    norm_res = _normalize_binary_drawing(user_mask, out_size=128, pad=14)
    if norm_res is None:
        return False, 0

    user_norm, user_aspect = norm_res

    ys, xs = np.where(user_norm > 0)
    drawn_pixels = len(xs)
    if drawn_pixels < 260:
        return False, 10

    # --- 1. DÜZ ÇİZGİ REDDETME (PCA Özdeğer Analizi - Yatay, Dikey ve Çapraz Düz Çizgiler) ---
    pts_float = np.column_stack((xs.astype(np.float32), ys.astype(np.float32)))
    mean_pt = np.mean(pts_float, axis=0)
    centered = pts_float - mean_pt
    cov = np.cov(centered, rowvar=False)
    eigvals = np.linalg.eigvalsh(cov)
    major_var = max(1e-4, float(eigvals[-1]))
    minor_var = max(0.0, float(eigvals[0]))
    thickness_ratio = math.sqrt(minor_var / major_var)

    # Herhangi bir açıda düz veya hafif eğri tekil çizgi ise kalınlık oranı çok düşüktür
    if thickness_ratio < 0.28:
        return False, int(round(thickness_ratio * 45))

    # --- 2. KAPALI ŞEKİL (İÇ ALAN) KONTROLÜ ---
    user_filled, user_enclosed_ratio = _fill_closed_regions(user_norm)
    # Düz çizgi, S-çizgisi veya açık karalamada iç alan oluşmaz
    if user_enclosed_ratio < 0.09:
        return False, 20

    # --- 3. EN / BOY ORANI (ASPECT RATIO) UYUMU ---
    min_ar, max_ar = fruit.aspect_range
    if min_ar <= user_aspect <= max_ar:
        aspect_score = 100.0
    else:
        diff = min(abs(user_aspect - min_ar), abs(user_aspect - max_ar))
        aspect_score = max(0.0, 100.0 - diff * 130.0)
        if aspect_score < 25.0:
            return False, int(round(aspect_score))

    # --- 4. ŞABLON KONTUR VE DOLGU KARŞILAŞTIRMASI ---
    base_tmpl = np.zeros((128, 128), dtype=np.uint8)
    draw_fruit_shape(base_tmpl, fruit.key, (64, 64), 48, (255, 255, 255), thickness=6)
    tmpl_res = _normalize_binary_drawing(base_tmpl, out_size=128, pad=14)
    if tmpl_res is None:
        return False, 0
    tmpl_norm, _ = tmpl_res

    variants = [tmpl_norm, cv2.flip(tmpl_norm, 1)]
    if fruit.key == "banana":
        variants.append(cv2.flip(tmpl_norm, 0))

    inv_user = cv2.bitwise_not(user_norm)
    dt_user = cv2.distanceTransform(inv_user, cv2.DIST_L2, 3)

    best_combined = 0.0
    for var_tmpl in variants:
        inv_tmpl = cv2.bitwise_not(var_tmpl)
        dt_tmpl = cv2.distanceTransform(inv_tmpl, cv2.DIST_L2, 3)

        user_dists = dt_tmpl[user_norm > 0]
        tmpl_dists = dt_user[var_tmpl > 0]

        # A) Sıkı Chamfer skoru
        d_u2t = float(np.mean(user_dists))
        d_t2u = float(np.mean(tmpl_dists))
        avg_dist = 0.50 * d_u2t + 0.50 * d_t2u
        chamfer_score = 100.0 * math.exp(-avg_dist / 7.5)

        # B) Kontur İsabet Oranı (Precision & Recall @ 8.5 piksel)
        precision = float(np.mean(user_dists <= 8.5))
        recall = float(np.mean(tmpl_dists <= 8.5))
        if (precision + recall) > 1e-4:
            f1_score = 100.0 * (2.0 * precision * recall) / (precision + recall)
        else:
            f1_score = 0.0

        # C) Doldurulmuş İç Alan Örtüşmesi (IoU)
        tmpl_filled, _ = _fill_closed_regions(var_tmpl)
        inter = float(np.count_nonzero(cv2.bitwise_and(user_filled, tmpl_filled)))
        union = float(np.count_nonzero(cv2.bitwise_or(user_filled, tmpl_filled)))
        iou_score = 100.0 * (inter / max(1.0, union))

        combined = 0.40 * f1_score + 0.35 * iou_score + 0.25 * chamfer_score
        if combined > best_combined:
            best_combined = combined

    final_score = int(round(0.85 * best_combined + 0.15 * aspect_score))
    final_score = max(0, min(99, final_score))
    passed = final_score >= 60
    return passed, final_score


class FruitDrawingGame:
    """
    Doğrudan başlayan Tam Ekran Beyaz Sayfa Meyve Çizim Oyunu yöneticisi.
    - İmleç Hareketi: Başparmak + İşaret parmağı birleştirerek (Pinch) fırça gezdirilir (boyamaz).
    - Boyama (Sol Tık): SADECE Serçe Parmak ile Sol Tık yapıldığında (serçe parmak kaldırıldığında
      veya serçe aç-kapat ile sol tık yapıldığında) boyar!
    """

    PASS_THRESHOLD = 60

    def __init__(self, width: int = 1280, height: int = 720) -> None:
        self.active: bool = True
        self.quit_requested: bool = False
        self.width: int = width
        self.height: int = height

        # Beyaz çizim sayfası ve ikili (binary) çizim maskesi
        self.canvas: np.ndarray = np.full((height, width, 3), 255, dtype=np.uint8)
        self.stroke_mask: np.ndarray = np.zeros((height, width), dtype=np.uint8)

        # Meyve turları
        self.fruit_order: List[FruitSpec] = list(FRUIT_LIST)
        random.shuffle(self.fruit_order)
        self.current_idx: int = 0
        self.round_num: int = 1
        self.score: int = 0

        # Fırça durumu ve yumuşatma
        self.brush_x: Optional[float] = None
        self.brush_y: Optional[float] = None
        self.prev_draw_pt: Optional[Tuple[int, int]] = None
        self.is_drawing: bool = False
        self.custom_color: Optional[Tuple[int, int, int]] = None
        self.show_guide: bool = True

        # Serçe Parmak (Sol Tık) ile boyama durumu
        self.pinky_was_open: bool = False
        self.pinky_open_start: float = 0.0
        self.paint_locked: bool = False  # Serçe aç-kapat (Sol Tık) ile boyama aç/kapat kilidi

        # Otomatik değerlendirme zamanlayıcısı
        self.has_drawn_anything: bool = False
        self.total_stroke_length: float = 0.0
        self.last_draw_time: float = 0.0
        self.auto_eval_delay: float = 2.4  # Boyamayı bıraktıktan 2.4 sn sonra otomatik kontrol

        # Sanal buton üzerinde bekleme (hover) durumu
        self.hover_btn: Optional[str] = None
        self.hover_start: float = 0.0

        # Tur sonucu / Başlangıç bildirim ekranı
        self.banner_text: str = "OYUN BASLADI!"
        self.banner_subtext: str = f"Hedef: {self.current_fruit.name} | Serce Parmak (Sol Tik) ile Boyayin"
        self.banner_color: Tuple[int, int, int] = (0, 175, 90)
        self.banner_until: float = time.perf_counter() + 1.4
        self.pending_action: Optional[str] = None  # "NEXT" veya "RESET"

    @property
    def current_fruit(self) -> FruitSpec:
        return self.fruit_order[self.current_idx % len(self.fruit_order)]

    @property
    def active_brush_color(self) -> Tuple[int, int, int]:
        return self.custom_color if self.custom_color is not None else self.current_fruit.color

    def toggle(self, width: int, height: int) -> bool:
        self.active = not self.active
        if self.active:
            self.resize_if_needed(width, height)
            self.clear_canvas()
            self.banner_text = "OYUN BASLADI!"
            self.banner_subtext = f"Hedef: {self.current_fruit.name} | Serce Parmak (Sol Tik) ile Boyayin"
            self.banner_color = (0, 175, 90)
            self.banner_until = time.perf_counter() + 1.4
            self.pending_action = None
        return self.active

    def resize_if_needed(self, width: int, height: int) -> None:
        if width != self.width or height != self.height:
            self.width = width
            self.height = height
            self.clear_canvas()

    def clear_canvas(self) -> None:
        self.canvas = np.full((self.height, self.width, 3), 255, dtype=np.uint8)
        self.stroke_mask = np.zeros((self.height, self.width), dtype=np.uint8)
        self.prev_draw_pt = None
        self.is_drawing = False
        self.pinky_was_open = False
        self.paint_locked = False
        self.has_drawn_anything = False
        self.total_stroke_length = 0.0
        self.pending_action = None

    def next_round(self) -> None:
        self.current_idx = (self.current_idx + 1) % len(self.fruit_order)
        if self.current_idx == 0:
            random.shuffle(self.fruit_order)
        self.round_num += 1
        self.custom_color = None
        self.clear_canvas()

    def trigger_evaluation(self) -> None:
        """Çizimi değerlendirir ve sonucu (Sonraki Tur / Tekrar Dene) ayarlar."""
        now = time.perf_counter()
        self.paint_locked = False
        self.is_drawing = False
        self.prev_draw_pt = None

        if self.total_stroke_length < 180:
            self.banner_text = "TEKRAR DENE!"
            self.banner_subtext = "Cizim cok kisa veya eksik! Meyveyi tam cizin."
            self.banner_color = (40, 50, 220)
            self.banner_until = now + 1.4
            self.pending_action = "RESET"
            return

        passed, sim_score = evaluate_fruit_drawing(self.stroke_mask, self.current_fruit)
        if passed:
            self.score += 10
            self.banner_text = f"DOGRU! SONRAKI TUR (%{sim_score})"
            self.banner_subtext = f"Harika {self.current_fruit.name} cizimi! Yeni meyveye geciliyor..."
            self.banner_color = (30, 185, 70)
            self.banner_until = now + 1.6
            self.pending_action = "NEXT"
        else:
            self.banner_text = f"TEKRAR DENE! (Benzerlik: %{sim_score})"
            self.banner_subtext = f"{self.current_fruit.name} sekline benzemedi (Hedef: %{self.PASS_THRESHOLD})."
            self.banner_color = (35, 45, 225)
            self.banner_until = now + 1.5
            self.pending_action = "RESET"

    def update_and_render(
        self,
        camera_frame: np.ndarray,
        hand: Optional[DetectedHand],
        fingers: List[int],
        fps: float,
    ) -> np.ndarray:
        """
        El hareketleriyle fırçayı günceller:
        - Pinch (Başparmak + İşaret) imleci gezdirir ama boyamaz.
        - SADECE Serçe Parmak ile Sol Tık yapıldığında boyar.
        """
        w, h = self.width, self.height
        now = time.perf_counter()

        # Bekleyen tur geçişi veya sıfırlama süresi dolduysa uygula
        if self.pending_action is not None and now >= self.banner_until:
            action = self.pending_action
            self.pending_action = None
            if action == "NEXT":
                self.next_round()
            elif action == "RESET":
                self.clear_canvas()

        # 1. EL İLE FIRÇA KONUMU VE SERÇE PARMAK (SOL TIK) İLE BOYAMA TESPİTİ
        brush_pt: Optional[Tuple[int, int]] = None
        drawing_now = False
        pinky_click_triggered = False

        if hand is not None and now >= self.banner_until:
            lms = hand.landmarks
            thumb_tip = lms[4]
            index_tip = lms[8]

            # İmleç konumu: Başparmak ve işaret parmağı orta noktası
            raw_nx = (thumb_tip.x + index_tip.x) * 0.5
            raw_ny = (thumb_tip.y + index_tip.y) * 0.5

            margin = 0.12
            nx = max(0.0, min(1.0, (raw_nx - margin) / (1.0 - 2.0 * margin)))
            ny = max(0.0, min(1.0, (raw_ny - margin) / (1.0 - 2.0 * margin)))
            target_x = nx * w
            target_y = ny * h

            if self.brush_x is None or self.brush_y is None:
                self.brush_x, self.brush_y = target_x, target_y
            else:
                self.brush_x += 0.42 * (target_x - self.brush_x)
                self.brush_y += 0.42 * (target_y - self.brush_y)

            brush_pt = (int(self.brush_x), int(self.brush_y))

            # fingers = [Başparmak(0), İşaret(1), Orta(2), Yüzük(3), Serçe(4)]
            middle_open = fingers[2] == 1
            ring_open = fingers[3] == 1
            pinky_open = fingers[4] == 1

            # El tamamen açıksa (3+ ana parmak açık) boyama kilidini kapat (Fırçayı kaldır)
            if sum(fingers[1:]) >= 3:
                self.pinky_was_open = False
                self.paint_locked = False
            else:
                # Serçe parmak tek başına kaldırıldıysa (orta ve yüzük kapalıyken)
                pinky_active = pinky_open and not middle_open and not ring_open
                if pinky_active and not self.pinky_was_open:
                    self.pinky_was_open = True
                    self.pinky_open_start = now
                elif not pinky_active and self.pinky_was_open:
                    # Serçe parmak açıldıktan sonra kapatıldı -> 1x Serçe Aç-Kapat (SOL TIK!)
                    up_dur = now - self.pinky_open_start
                    self.pinky_was_open = False
                    if 0.02 <= up_dur <= 0.65:
                        # Sol Tık gerçekleşti: Boyama kilidini Aç / Kapat
                        self.paint_locked = not self.paint_locked
                        pinky_click_triggered = True
                    else:
                        # Uzun süre serçeyi açık tutarak çizdiyse, serçeyi indirince boyamayı durdur
                        self.paint_locked = False

            # SADECE Serçe Parmak ile Sol Tık kilidi aktifse veya Serçe havada tutuluyorsa boya!
            drawing_now = self.paint_locked or self.pinky_was_open
        else:
            self.prev_draw_pt = None
            self.is_drawing = False
            self.pinky_was_open = False

        # Üst bar buton koordinatları (Tamamen el/serçe sol tık ile kontrol edilir)
        btn_check_rect = (w - 430, 14, w - 290, 68)
        btn_clear_rect = (w - 280, 14, w - 155, 68)
        btn_skip_rect = (w - 145, 14, w - 18, 68)

        # Fırça üst menü bölgesindeyse tuvale çizmek yerine buton etkileşimini çalıştır
        in_top_bar = brush_pt is not None and brush_pt[1] < 82

        if brush_pt is not None and in_top_bar and now >= self.banner_until:
            self.prev_draw_pt = None
            self.is_drawing = False
            bx, by = brush_pt
            current_hover = None
            if btn_check_rect[0] <= bx <= btn_check_rect[2] and btn_check_rect[1] <= by <= btn_check_rect[3]:
                current_hover = "CHECK"
            elif btn_clear_rect[0] <= bx <= btn_clear_rect[2] and btn_clear_rect[1] <= by <= btn_clear_rect[3]:
                current_hover = "CLEAR"
            elif btn_skip_rect[0] <= bx <= btn_skip_rect[2] and btn_skip_rect[1] <= by <= btn_skip_rect[3]:
                current_hover = "SKIP"

            self.hover_btn = current_hover
            # Buton üzerindeyken Serçe Parmak ile Sol Tık yapılırsa anında çalıştır
            if current_hover is not None and pinky_click_triggered:
                self.paint_locked = False
                if current_hover == "CHECK":
                    self.trigger_evaluation()
                elif current_hover == "CLEAR":
                    self.clear_canvas()
                elif current_hover == "SKIP":
                    self.next_round()
        else:
            self.hover_btn = None

        # 2. BEYAZ SAYFAYA FIRÇA İLE ÇİZİM YAPMA (SADECE SOL TIK / SERÇE AKTİFKEN)
        if brush_pt is not None and drawing_now and not in_top_bar and now >= self.banner_until:
            self.is_drawing = True
            if self.prev_draw_pt is not None:
                seg_len = math.hypot(brush_pt[0] - self.prev_draw_pt[0], brush_pt[1] - self.prev_draw_pt[1])
                if seg_len < 140:
                    cv2.line(self.canvas, self.prev_draw_pt, brush_pt, self.active_brush_color, 14, cv2.LINE_AA)
                    cv2.line(self.stroke_mask, self.prev_draw_pt, brush_pt, 255, 14, cv2.LINE_AA)
                    self.total_stroke_length += seg_len
                    self.has_drawn_anything = True
                    self.last_draw_time = now
            else:
                cv2.circle(self.canvas, brush_pt, 7, self.active_brush_color, -1, cv2.LINE_AA)
                cv2.circle(self.stroke_mask, brush_pt, 7, 255, -1, cv2.LINE_AA)
                self.last_draw_time = now
            self.prev_draw_pt = brush_pt
        else:
            self.is_drawing = False
            self.prev_draw_pt = None

        # 3. OTOMATİK DEĞERLENDİRME KONTROLÜ (Boyama kapatıldıktan sonra beklenirse)
        auto_eval_progress = 0.0
        if (
            self.has_drawn_anything
            and self.total_stroke_length >= 260
            and not self.is_drawing
            and self.pending_action is None
            and now >= self.banner_until
        ):
            idle_time = now - self.last_draw_time
            if idle_time > 0.50:
                auto_eval_progress = min(1.0, (idle_time - 0.50) / self.auto_eval_delay)
                if auto_eval_progress >= 1.0:
                    self.trigger_evaluation()

        # 4. EKRANI OLUŞTURMA (BEYAZ SAYFA + KILAVUZ + ARAYÜZ)
        view = self.canvas.copy()

        # İsteğe bağlı silik meyve kılavuz izi (kullanıcının şekli kolayca anlaması için)
        if self.show_guide:
            guide_layer = np.full_like(view, 255)
            draw_fruit_shape(
                guide_layer,
                self.current_fruit.key,
                (w // 2, (h + 70) // 2),
                int(min(w, h) * 0.27),
                (215, 215, 215),
                thickness=3,
            )
            mask_guide = np.any(guide_layer < 250, axis=2) & (self.stroke_mask == 0)
            view[mask_guide] = guide_layer[mask_guide]

        # Üst Bilgi Şeridi (Açık Gri / Beyaz Üst Panel)
        cv2.rectangle(view, (0, 0), (w, 82), (242, 244, 248), -1)
        cv2.line(view, (0, 82), (w, 82), (200, 205, 215), 2)

        # SOL ÜST: HEDEF MEYVE İSMİ VE MİNİ İKONU
        fruit = self.current_fruit
        cv2.rectangle(view, (14, 10), (410, 72), (255, 255, 255), -1)
        cv2.rectangle(view, (14, 10), (410, 72), fruit.color, 3, cv2.LINE_AA)

        # Meyve mini sembolü
        draw_fruit_shape(view, fruit.key, (48, 41), 20, fruit.color, thickness=3)

        cv2.putText(
            view,
            f"CIZ: {fruit.name}",
            (82, 42),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            (25, 25, 25),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            view,
            fruit.hint,
            (82, 64),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (90, 90, 90),
            1,
            cv2.LINE_AA,
        )

        # ORTA ÜST: TUR, SKOR VE SERÇE PARMAK BOYAMA DURUMU
        cv2.putText(
            view,
            f"Tur: {self.round_num}  |  Skor: {self.score}",
            (425, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (40, 40, 40),
            2,
            cv2.LINE_AA,
        )
        if self.is_drawing:
            brush_state_txt = "SOL TIK AKTIF: BOYUYOR (Durdurmak: Serce Ac-Kapat / El Ac)"
            brush_state_col = (0, 165, 50)
        else:
            brush_state_txt = "FIRCA BOSTA: Boyamak icin Serce Parmak ile Sol Tik Yapin"
            brush_state_col = (30, 90, 210)
        cv2.putText(
            view,
            brush_state_txt,
            (425, 63),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.44,
            brush_state_col,
            2 if self.is_drawing else 1,
            cv2.LINE_AA,
        )

        # SAĞ ÜST: KONTROL ET, TEMİZLE ve ATLA BUTONLARI (El ile tıklanabilir)
        check_bg = (40, 180, 80) if self.hover_btn == "CHECK" else (60, 160, 75)
        clear_bg = (50, 70, 220) if self.hover_btn == "CLEAR" else (80, 95, 200)
        skip_bg = (30, 140, 210) if self.hover_btn == "SKIP" else (55, 130, 185)
        cv2.rectangle(view, (btn_check_rect[0], btn_check_rect[1]), (btn_check_rect[2], btn_check_rect[3]), check_bg, -1, cv2.LINE_AA)
        cv2.rectangle(view, (btn_clear_rect[0], btn_clear_rect[1]), (btn_clear_rect[2], btn_clear_rect[3]), clear_bg, -1, cv2.LINE_AA)
        cv2.rectangle(view, (btn_skip_rect[0], btn_skip_rect[1]), (btn_skip_rect[2], btn_skip_rect[3]), skip_bg, -1, cv2.LINE_AA)

        cv2.putText(view, "KONTROL ET", (btn_check_rect[0] + 14, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.54, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(view, "Serce Sol Tik", (btn_check_rect[0] + 26, 61), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (230, 255, 230), 1, cv2.LINE_AA)

        cv2.putText(view, "TEMIZLE", (btn_clear_rect[0] + 24, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.54, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(view, "Serce Sol Tik", (btn_clear_rect[0] + 20, 61), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (230, 230, 255), 1, cv2.LINE_AA)

        cv2.putText(view, "SONRAKI", (btn_skip_rect[0] + 24, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.54, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(view, "Serce Sol Tik", (btn_skip_rect[0] + 22, 61), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (230, 245, 255), 1, cv2.LINE_AA)

        # Otomatik değerlendirme ilerleme çubuğu
        if auto_eval_progress > 0.0:
            bar_w = int((w - 40) * auto_eval_progress)
            cv2.rectangle(view, (20, 88), (w - 20, 108), (230, 230, 230), -1)
            cv2.rectangle(view, (20, 88), (20 + bar_w, 108), (0, 180, 90), -1)
            cv2.putText(
                view,
                "Cizim degerlendiriliyor... (Devam etmek icin cizmeyi surdurun)",
                (30, 103),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.44,
                (30, 30, 30),
                1,
                cv2.LINE_AA,
            )

        # SAĞ ALT: MİNİ KAMERA ÖNİZLEMESİ (Sadece aktif oyuncunun eli gösterilir)
        pip_w, pip_h = 210, 118
        pip_x1, pip_y1 = w - pip_w - 14, h - pip_h - 14
        small_cam = cv2.resize(camera_frame, (pip_w, pip_h))
        view[pip_y1 : pip_y1 + pip_h, pip_x1 : pip_x1 + pip_w] = small_cam
        cv2.rectangle(view, (pip_x1, pip_y1), (pip_x1 + pip_w, pip_y1 + pip_h), (80, 80, 80), 2)
        lock_txt = "Oyuncu Kilitli (Tek El)" if hand is not None else "El Bekleniyor..."
        cv2.putText(
            view,
            f"{lock_txt} | {int(fps)} FPS",
            (pip_x1 + 6, pip_y1 + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            (0, 255, 180) if hand is not None else (180, 220, 255),
            1,
            cv2.LINE_AA,
        )

        # SOL ALT: EL KONTROL BİLGİSİ
        cv2.putText(
            view,
            "Bas+Isaret (Pinch): Fircayi Gezdir  |  Serce Parmak (Sol Tik): Boya / Butona Tikla  |  El Ac: Fircayi Birak",
            (16, h - 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            (100, 100, 100),
            1,
            cv2.LINE_AA,
        )

        # FIRÇA İMLECİNİ ÇİZ
        if brush_pt is not None:
            if self.is_drawing:
                cv2.circle(view, brush_pt, 10, self.active_brush_color, -1, cv2.LINE_AA)
                cv2.circle(view, brush_pt, 15, (30, 30, 30), 2, cv2.LINE_AA)
            else:
                cv2.circle(view, brush_pt, 12, self.active_brush_color, 2, cv2.LINE_AA)
                cv2.circle(view, brush_pt, 3, (50, 50, 50), -1, cv2.LINE_AA)

        # SONUÇ BİLDİRİM AFİŞİ (DOĞRU -> SONRAKİ TUR / YANLIŞ -> TEKRAR DENE!)
        if now < self.banner_until and self.banner_text:
            bw, bh = 580, 110
            bx1 = (w - bw) // 2
            by1 = (h - bh) // 2
            overlay = view.copy()
            cv2.rectangle(overlay, (bx1, by1), (bx1 + bw, by1 + bh), self.banner_color, -1, cv2.LINE_AA)
            cv2.addWeighted(overlay, 0.92, view, 0.08, 0, view)
            cv2.rectangle(view, (bx1, by1), (bx1 + bw, by1 + bh), (255, 255, 255), 3, cv2.LINE_AA)

            cv2.putText(
                view,
                self.banner_text,
                (bx1 + 28, by1 + 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.92,
                (255, 255, 255),
                3,
                cv2.LINE_AA,
            )
            cv2.putText(
                view,
                self.banner_subtext,
                (bx1 + 28, by1 + 85),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (240, 240, 240),
                1,
                cv2.LINE_AA,
            )

        return view


# ============================================================================
# ANA UYGULAMA DÖNGÜSÜ (DOĞRUDAN MEYVE ÇİZİM OYUNU & TEK EL KİLİDİ)
# ============================================================================
def main() -> None:
    disable_windows_background_throttling()

    print("=" * 65)
    print("MEYVE ÇİZİM OYUNU (TEK OYUNCU EL KİLİTLİ)")
    print("- Oyun doğrudan başlar, klavye gerektirmez.")
    print("- Bir kişi oyuna başladığında el kilitlenir, başka el algılanmaz.")
    print("- Başparmak + İşaret parmağı (Pinch): Fırçayı gezdir")
    print("- Serçe Parmak (Sol Tık): Boyamayı başlat/durdur veya butonlara tıkla")
    print("- Eli tamamen aç (3+ parmak): Boyamayı bırak")
    print("=" * 65)

    cap = ThreadedCamera(index=0, width=960, height=540)
    if not cap.isOpened():
        print("HATA: Webcam açılamadı! Lütfen kameranızın bağlı olduğundan emin olun.")
        return

    detector = HandDetector(max_hands=2, detection_conf=0.7, tracking_conf=0.7)
    hand_lock = SinglePlayerHandLock(lock_timeout=1.6, max_jump_dist=0.36)
    game = FruitDrawingGame(width=1280, height=720)

    window_name = "Meyve Cizim Oyunu"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, 1280, 720)
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    prev_time = time.perf_counter()
    fps = 30.0

    try:
        while True:
            success, frame = cap.read()
            if not success or frame is None:
                continue

            # Ayna görüntüsü
            frame = cv2.flip(frame, 1)

            # 1. Kameradaki elleri tespit et ve SADECE oyuna başlayan kişinin elini seç (kilitle)
            detected_hands = detector.detect(frame)
            locked_hand = hand_lock.select_hand(detected_hands)

            primary_fingers: List[int] = [0, 0, 0, 0, 0]
            if locked_hand is not None:
                primary_fingers = count_open_fingers(locked_hand)
                draw_hand_skeleton(frame, locked_hand, primary_fingers)

            # FPS hesapla
            curr_time = time.perf_counter()
            dt = max(1e-4, curr_time - prev_time)
            fps = 0.85 * fps + 0.15 * (1.0 / dt)
            prev_time = curr_time

            # Doğrudan Meyve Çizim Oyununu güncelle ve ekrana çiz
            display_frame = game.update_and_render(
                frame,
                locked_hand,
                primary_fingers,
                fps,
            )

            cv2.imshow(window_name, display_frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                break

    finally:
        detector.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()


