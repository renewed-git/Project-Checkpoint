import sys
import ctypes
# Per-monitor DPI awareness MUST be set before pyautogui/uiautomation are imported
# (pyautogui sets a weaker mode on import, after which this call is refused).
if sys.platform.startswith("win"):
    try:
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # per-monitor v2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
import cv2
import mediapipe as mp
import pyautogui
import numpy as np
import time
import math
import subprocess
import ctypes
import sys
import os
import traceback
import faulthandler
if os.environ.get("GAZE_DEBUG") == "1":  # crash tracer (off by default: Windows COM triggers false alarms)
    faulthandler.enable(all_threads=True)
from gaze_overlay import GazeOverlay
from collections import deque

# =====================================================================
# GAZE CURSOR V10 - Windows - FACE POINTER
# You steer a blue ball with your HEAD (no eye tracking, no calibration):
#   slow head movement = precise, quick movement = covers distance (pointer acceleration)
#   hold still = the ball holds still (no jitter)
# + LOOK-AND-PRESS: hands stay on the keyboard. Hold Right Alt = cursor jumps to the ball
#   (steering gets finer while held), release = click. Right Alt + key = hotkeys, any app.
# + on-screen ball, face mesh, Muse Spark open-1026, optional hands-free dwell/blink
# =====================================================================

pyautogui.FAILSAFE = True  # used by pyautogui.click(); we also run our own corner check
pyautogui.PAUSE = 0
IS_WINDOWS = sys.platform.startswith("win")

# --- Displays ---
MAIN_DISPLAY_ONLY = os.environ.get("GAZE_ALL_MONITORS") != "1"  # set GAZE_ALL_MONITORS=1 to roam all screens
MULTI_MONITOR = not MAIN_DISPLAY_ONLY

# --- Face pointer ---
GAIN_SLOW = 900         # px per unit of tan(head angle) when you move slowly: ~3 deg = ~47 px (precise)
GAIN_FAST = 3200        # when you move quickly: ~10 deg flick = ~560 px
SPEED_LO = 0.08         # head speed (tan/s) at or below which movement is fully precise...
SPEED_HI = 0.60         # ...and at or above which it is fully fast
STILL_SPEED = 0.015     # head speed below this = holding still: the ball doesn't move (Right Alt + 9/0)
AIM_PRECISION = 0.5     # while holding Right Alt, steering is this much finer
HEAD_MIN_CUTOFF = 0.8   # head-pose smoothing (Hz): lower = smoother, more lag (Right Alt + 5/6)
HEAD_BETA = 2.0         # how fast the smoothing lets quick moves through
POINTER_SIGN_X = 1      # Right Alt + F flips left/right
POINTER_SIGN_Y = 1      # Right Alt + H flips up/down

# --- Clicking ---
CLICK_KEY = os.environ.get("GAZE_CLICK_KEY", "right alt")  # hold = aim, release = click (any app)
CLICK_KEY_LABEL = CLICK_KEY.title()                        # e.g. "Right Alt" (or set GAZE_CLICK_KEY="caps lock")
SCAN_CODES = {"caps lock": {58}}                           # hardware code: matches on every keyboard layout
AIM_STEADY_FOR_MUSE = 0.6
CLICK_COOLDOWN = 1.0
DWELL_ENABLED = False   # hands-free mode only (Right Alt + D)
DWELL_TIME = 1.2
DWELL_RADIUS = 40
BLINK_ENABLED = False   # hands-free mode only (Right Alt + B)
BLINK_THRESHOLD = 0.19  # only until your own open-eye level is learned
BLINK_RATIO = 0.5       # blink = eyes under half as open as YOUR normal
BLINK_CONSEC_FRAMES = 3
SHOW_MESH = True

MONITORS = []  # list of (x, y, w, h); filled by get_desktop_bounds()


def get_desktop_bounds():
    """Returns (x, y, w, h) of the area the gaze cursor maps onto, plus primary width.
    Virtual desktop = bounding box of ALL monitors. x/y can be negative when a
    monitor sits left of / above the primary one."""
    prim_w, prim_h = pyautogui.size()
    MONITORS[:] = [(0, 0, prim_w, prim_h)]
    if not MULTI_MONITOR:
        return 0, 0, prim_w, prim_h, prim_w
    try:
        from screeninfo import get_monitors  # pip install screeninfo
        mons = get_monitors()
        MONITORS[:] = [(m.x, m.y, m.width, m.height) for m in mons]
        x0 = min(m.x for m in mons)
        y0 = min(m.y for m in mons)
        x1 = max(m.x + m.width for m in mons)
        y1 = max(m.y + m.height for m in mons)
        print(f"[DISPLAYS] {len(mons)} monitor(s): " +
              ", ".join(f"{m.width}x{m.height}@({m.x},{m.y})" for m in mons))
        return x0, y0, x1 - x0, y1 - y0, prim_w
    except Exception:
        if IS_WINDOWS:  # fallback: Windows virtual-screen metrics, no extra package needed
            gsm = ctypes.windll.user32.GetSystemMetrics
            return gsm(76), gsm(77), gsm(78), gsm(79), prim_w
        return 0, 0, prim_w, prim_h, prim_w


DESK_X, DESK_Y, DESK_W, DESK_H, PRIMARY_W = get_desktop_bounds()
PRIMARY_H = pyautogui.size()[1]
EDGE_MARGIN = 3  # keep the gaze cursor off the exact edges/corners
print(f"[DESKTOP] ball can move over {DESK_W}x{DESK_H} at ({DESK_X},{DESK_Y})")


NOSE, CHIN, L_CHEEK, R_CHEEK = 1, 152, 234, 454

# --- Muse Spark semantic layer (Baseten serverless, background thread) ---
MUSE_ENABLED = True
MUSE_TRIGGER = 0.5  # ask Muse once dwell is 50% complete
try:
    from muse_client import MuseOffloader
    muse = MuseOffloader()
    print(f"[MUSE] ready: {muse.model}")  # always meta-models/muse-spark-open-1026
except Exception as e:
    muse = None
    print(f"[MUSE] disabled ({type(e).__name__}: {e}) - cursor still works")

try:
    import keyboard as kbhook  # pip install keyboard  (global keyboard hook, no admin needed)
except ImportError:
    kbhook = None


class GazeClickKey:
    """Keyboard listeners on just the keys we use, so everything works whatever app is focused:
      - Right Alt: hold = aim, release = click (+Shift held = right-click). Swallowed.
      - Esc while holding Right Alt = cancel the click.
      - Right Alt + a command key (Q quit, R re-center, V ball, ...) = that
        command, no click. The letter is swallowed, so nothing gets typed in your app.
    (The camera-window keys stopped working once the on-screen marker was added: its window
    pumps the same Windows message queue and ate the key presses. This hook doesn't care.)"""

    COMMANDS = set("qrcsvhfdbmik567890")

    def __init__(self, key_name):
        self.held = False
        self.cancel = None          # None | "esc" | "command"
        self.events = deque()
        self.commands = deque()
        key_name = key_name.strip().lower()
        self.alt_names = {key_name} | ({"right alt", "alt gr"} if key_name == "right alt" else set())
        self.scan_codes = SCAN_CODES.get(key_name, set())
        self.seen_once = False
        self.last_seen = 0.0        # last key event while held (auto-repeat keeps this fresh)
        self.warned_altgr = False
        suppress = os.environ.get("GAZE_NO_SUPPRESS") != "1"  # GAZE_NO_SUPPRESS=1: don't swallow keys
        # Listen ONLY to the keys we use (not every key on the PC): the click key, Esc and the
        # command letters. Our answer decides per key: swallow (False) or pass to your app (True),
        # so the letters type normally unless the click key is held.
        for k in sorted(self.alt_names) + ["esc"] + sorted(self.COMMANDS):
            try:
                kbhook.hook_key(k, self._on_event, suppress=suppress)
            except ValueError:
                pass  # e.g. "alt gr" doesn't exist on this keyboard layout

    def _on_event(self, e):
        try:
            return self._handle(e)
        except Exception:
            traceback.print_exc()
            return True  # never block typing because of a bug here

    def _handle(self, e):
        """Return False = swallow the key, True = let it through to your app."""
        name = (e.name or "").lower()
        if name == "alt gr" and not self.warned_altgr and "right alt" in self.alt_names:
            self.warned_altgr = True
            print("[CLICK] Your keyboard layout uses Right Alt as AltGr - apps may still see an Alt press.\n"
                  "        Fix: quit, run  $env:GAZE_CLICK_KEY=\"right ctrl\"  and start again.")
        if self.held:
            self.last_seen = time.monotonic()
        if name in self.alt_names or getattr(e, "scan_code", None) in self.scan_codes:
            if not self.seen_once and e.event_type == "down":
                self.seen_once = True
                print(f"[CLICK] {CLICK_KEY_LABEL} detected - the click key works")
            if e.event_type == "down":
                if not self.held:  # ignore auto-repeat while held
                    self.held, self.cancel = True, None
                    self.last_seen = time.monotonic()
                    self.events.append("aim")
            elif self.held:
                self.held = False
                if self.cancel:
                    self.events.append("cancel_" + self.cancel)
                else:
                    self.events.append("right" if kbhook.is_pressed("shift") else "click")
            return False
        if self.held:
            if name == "esc":
                if e.event_type == "down":
                    self.cancel = "esc"
                return False
            if name in self.COMMANDS:
                if e.event_type == "down":
                    self.cancel = "command"
                    self.commands.append(name)
                return False
        return True

    def poll(self):
        # The key-up can be lost (screen lock, Ctrl+Alt+Del, an admin window in front). Without
        # this, "held" would stay on and swallow q/r/c... typed in other apps.
        if self.held and time.monotonic() - self.last_seen > 1.5:
            self.held = False
            self.events.append("cancel_lost")
        out = []
        while self.events:
            out.append(self.events.popleft())
        return out

    def poll_command(self):
        return self.commands.popleft() if self.commands else None


mp_face_mesh = mp.solutions.face_mesh
mp_drawing = mp.solutions.drawing_utils
mp_styles = mp.solutions.drawing_styles
face_mesh = mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

LEFT_EYE_H = [263, 362]
LEFT_EYE_V = [386, 374]
RIGHT_EYE_H = [33, 133]
RIGHT_EYE_V = [159, 145]


class OneEuroFilter:
    """Speed-adaptive low-pass filter (Casiez et al. 2012), the standard for pointer jitter."""

    def __init__(self, min_cutoff, beta, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self, value=None):
        self.x_prev = value
        self.dx_prev = 0.0
        self.t_prev = None

    @staticmethod
    def _alpha(dt, cutoff):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.x_prev is None or self.t_prev is None:
            self.x_prev, self.t_prev = x, t
            return x
        dt = max(t - self.t_prev, 1e-4)
        dx = (x - self.x_prev) / dt
        a_d = self._alpha(dt, self.d_cutoff)
        dx_hat = a_d * dx + (1 - a_d) * self.dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(dt, cutoff)
        x_hat = a * x + (1 - a) * self.x_prev
        self.x_prev, self.dx_prev, self.t_prev = x_hat, dx_hat, t
        return x_hat


FOREHEAD = 10  # top-center of the forehead (CHIN = 152 is bottom-center)


def get_head_yaw_pitch(lm, w=640, h=480):
    """TRUE 3D head direction from MediaPipe's per-landmark depth (z).
    Builds the face's own axes (ear-to-ear and chin-to-forehead), takes their
    cross product as the direction the face points, and returns
    (tan(yaw), tan(pitch)). These follow head ROTATION and barely change when
    the body/neck slides or leans, and tan() matches how a turn sweeps across
    a flat screen."""
    def P(i):
        return np.array([lm[i].x * w, lm[i].y * h, lm[i].z * w])

    right = P(R_CHEEK) - P(L_CHEEK)      # across the face
    up = P(FOREHEAD) - P(CHIN)           # chin -> forehead
    fwd = np.cross(right, up)            # out of the face
    n = np.linalg.norm(fwd)
    if n < 1e-6:
        return 0.0, 0.0
    fwd /= n
    if fwd[2] > 0:                       # make it point toward the camera (-z)
        fwd = -fwd
    depth = max(-fwd[2], 1e-3)
    return float(fwd[0] / depth), float(fwd[1] / depth)


def eye_aspect_ratio(landmarks, h_ids, v_ids):
    h_dist = abs(landmarks[h_ids[0]].x - landmarks[h_ids[1]].x)
    v_dist = abs(landmarks[v_ids[0]].y - landmarks[v_ids[1]].y)
    return v_dist / h_dist if h_dist != 0 else 0.3


FAILSAFE_POINTS = list(getattr(pyautogui, "FAILSAFE_POINTS", [(0, 0)]))


def user_hit_failsafe():
    """Emergency stop: the user slammed the REAL mouse into a corner of the primary screen."""
    return tuple(pyautogui.position()) in [tuple(p) for p in FAILSAFE_POINTS]


def clamp_to_display(x, y, margin=0):
    """Keep a point ON a real screen: past an edge it sits on the border instead of vanishing
    (also handles gaps between monitors of different sizes). margin keeps it that far inside."""
    best, best_d = None, None
    for (mx, my, mw, mh) in MONITORS:
        cx = min(max(x, mx + margin), mx + mw - 1 - margin)
        cy = min(max(y, my + margin), my + mh - 1 - margin)
        d = (cx - x) ** 2 + (cy - y) ** 2
        if best_d is None or d < best_d:
            best, best_d = (cx, cy), d
    return best if best is not None else (x, y)


def safe_point(x, y):
    """Clamp onto a real screen and stay clear of failsafe corners, which can sit
    in the MIDDLE of a multi-monitor desktop (e.g. the primary's top-right corner)."""
    x, y = clamp_to_display(x, y)
    x = min(max(x, DESK_X + EDGE_MARGIN), DESK_X + DESK_W - 1 - EDGE_MARGIN)
    y = min(max(y, DESK_Y + EDGE_MARGIN), DESK_Y + DESK_H - 1 - EDGE_MARGIN)
    for fx, fy in FAILSAFE_POINTS:
        if abs(x - fx) <= EDGE_MARGIN and abs(y - fy) <= EDGE_MARGIN:
            y = fy + EDGE_MARGIN + 1 if fy <= DESK_Y + DESK_H / 2 else fy - EDGE_MARGIN - 1
    return int(x), int(y)


def move_cursor(x, y):
    # SetCursorPos accepts virtual-desktop coords (incl. negative = left/upper monitors)
    if IS_WINDOWS:
        ctypes.windll.user32.SetCursorPos(x, y)
    else:
        pyautogui.moveTo(x, y, duration=0)


def draw_face_mesh(frame, face_landmarks):
    mp_drawing.draw_landmarks(
        image=frame, landmark_list=face_landmarks,
        connections=mp_face_mesh.FACEMESH_TESSELATION, landmark_drawing_spec=None,
        connection_drawing_spec=mp_styles.get_default_face_mesh_tesselation_style())
    mp_drawing.draw_landmarks(
        image=frame, landmark_list=face_landmarks,
        connections=mp_face_mesh.FACEMESH_CONTOURS, landmark_drawing_spec=None,
        connection_drawing_spec=mp_styles.get_default_face_mesh_contours_style())
    mp_drawing.draw_landmarks(
        image=frame, landmark_list=face_landmarks,
        connections=mp_face_mesh.FACEMESH_IRISES, landmark_drawing_spec=None,
        connection_drawing_spec=mp_styles.get_default_face_mesh_iris_connections_style())


def draw_head_direction(frame, lm, yaw, pitch, w, h, yaw0, pitch0):
    """Arrow from the nose showing head orientation relative to the last re-center."""
    nx, ny = int(lm[NOSE].x * w), int(lm[NOSE].y * h)
    ex = int(nx + (yaw - yaw0) * 200)
    ey = int(ny + (pitch - pitch0) * 200)
    cv2.arrowedLine(frame, (nx, ny), (ex, ey), (0, 165, 255), 2, tipLength=0.25)


def toggle_osk():
    global osk_open
    if not osk_open:
        print("[OSK] Opening Windows On-Screen Keyboard...")
        subprocess.Popen("start osk", shell=True)
        osk_open = True
    else:
        print("[OSK] Closing...")
        subprocess.run("taskkill /f /im osk.exe", shell=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        osk_open = False


# DirectShow = 1s startup on Windows vs 10s default
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_FPS, 30)

if not cap.isOpened():
    print("ERROR: Camera in use. Close Teams/Zoom/Chrome and retry.")
    exit()

primary_center = (PRIMARY_W // 2, PRIMARY_H // 2)
ball_x, ball_y = float(primary_center[0]), float(primary_center[1])   # the blue ball YOU steer
filt_yaw = OneEuroFilter(HEAD_MIN_CUTOFF, HEAD_BETA)
filt_pitch = OneEuroFilter(HEAD_MIN_CUTOFF, HEAD_BETA)
prev_head = None          # (yaw, pitch, t) of the last frame, for relative movement
neutral = None            # head pose at the last re-center (for the arrow)
ear_hist = deque(maxlen=300)
blink_counter = 0
last_click = 0.0
dwell_start = time.time()
dwell_anchor = (ball_x, ball_y)
dwell_progress = 0.0
aiming = False
aim_steady_since, aim_anchor = 0.0, (ball_x, ball_y)
osk_open = False
head_speed = 0.0

overlay = GazeOverlay(DESK_X, DESK_Y, DESK_W, DESK_H)
if kbhook is None:
    print("[CLICK] the 'keyboard' package is missing: run  pip install keyboard  - clicking is off")
    clicker = None
else:
    clicker = GazeClickKey(CLICK_KEY)


def eyes_are_closed(ear):
    """A real blink, relative to how open YOUR eyes normally are (looking down isn't a blink)."""
    if len(ear_hist) >= 30:
        closed = ear < BLINK_RATIO * float(np.percentile(ear_hist, 80))
    else:
        closed = ear < BLINK_THRESHOLD
    if not closed:
        ear_hist.append(ear)
    return closed


def pointer_gain(speed):
    """Pointer acceleration, like a mouse: slow head movement = precise, fast = covers distance."""
    if speed <= SPEED_LO:
        return GAIN_SLOW
    if speed >= SPEED_HI:
        return GAIN_FAST
    f = (speed - SPEED_LO) / (SPEED_HI - SPEED_LO)
    return GAIN_SLOW + f * (GAIN_FAST - GAIN_SLOW)


CAM_TITLE = 'Gaze Cursor V10 - face pointer | Right Alt aim/click | Right Alt+Q quit'
_cam_styled = False


def make_camera_window_unfocusable():
    """Pressing Alt while the camera window had focus put Windows into menu mode, which froze the
    whole program; plain keys typed into it acted as commands (holding Q quit). A window that can't
    take focus never receives keys - all control goes through the Right Alt listeners instead."""
    global _cam_styled
    _cam_styled = True
    if not IS_WINDOWS:
        return
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, CAM_TITLE)
    if not hwnd:
        return
    GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
    if u.GetForegroundWindow() == hwnd and _prev_foreground:
        u.SetForegroundWindow(_prev_foreground)  # give focus back to the app you started from


_prev_foreground = ctypes.windll.user32.GetForegroundWindow() if IS_WINDOWS else None


def recenter():
    global ball_x, ball_y, prev_head, neutral
    ball_x, ball_y = float(primary_center[0]), float(primary_center[1])
    prev_head = None
    neutral = None
    print("[CENTER] ball moved to the middle of your main screen - point your face there and carry on")


print("=" * 70)
print("GAZE CURSOR V10 - FACE POINTER (no eye tracking, no calibration)")
print("Move your HEAD to steer the blue ball - slow = precise, quick = far.")
print("ANY APP: hold RIGHT ALT = cursor jumps to the ball (finer steering while held),")
print("         release = click | +Shift = right-click | Esc while holding = cancel")
print("HOTKEYS (any app): RIGHT ALT + Q quit | R or C re-center ball | V ball on/off")
print("                   RIGHT ALT + 7/8 slower/faster | 9/0 steadier/snappier | 5/6 smoother/quicker")
print("                   RIGHT ALT + F flip left/right | H flip up/down | D dwell | B blink | M mesh | I Muse | K Win keyboard")
print("Real mouse to a primary-screen corner = Emergency stop")
print("=" * 70)

cam_fails = 0
try:
    while True:
        ret, frame = cap.read()
        if not ret:  # camera stopped: keep hotkeys/failsafe alive instead of spinning forever
            cam_fails += 1
            if cam_fails == 30:
                print("[CAMERA] no frames - is another app using the webcam? (Right Alt + Q quits)")
            if user_hit_failsafe() or (clicker and clicker.poll_command() == "q") or cam_fails > 300:
                break
            if clicker:
                clicker.poll()
            overlay.update()
            cv2.waitKey(30)
            continue
        cam_fails = 0

        if user_hit_failsafe():
            print("[FAILSAFE] Mouse in corner - stopping.")
            break

        frame = cv2.flip(frame, 1)
        h, w, _ = frame.shape
        result = face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        now = time.time()
        dwell_progress = 0.0
        eyes_closed = False

        if result.multi_face_landmarks:
            face = result.multi_face_landmarks[0]
            lm = face.landmark
            ear = (eye_aspect_ratio(lm, LEFT_EYE_H, LEFT_EYE_V) + eye_aspect_ratio(lm, RIGHT_EYE_H, RIGHT_EYE_V)) / 2.0
            eyes_closed = eyes_are_closed(ear)

            # ---------------- FACE POINTER: head movement steers the ball ----------------
            yaw_raw, pitch_raw = get_head_yaw_pitch(lm, w, h)
            yaw, pitch = filt_yaw(yaw_raw, now), filt_pitch(pitch_raw, now)
            if neutral is None:
                neutral = (yaw, pitch)
            if prev_head is not None:
                dt = max(now - prev_head[2], 1e-3)
                dyaw, dpitch = yaw - prev_head[0], pitch - prev_head[1]
                head_speed = math.hypot(dyaw, dpitch) / dt
                if head_speed >= STILL_SPEED:            # below this = holding still: ball stays put
                    g = pointer_gain(head_speed) * (AIM_PRECISION if aiming else 1.0)
                    ball_x += POINTER_SIGN_X * g * dyaw
                    ball_y += POINTER_SIGN_Y * g * dpitch
                    ball_x, ball_y = clamp_to_display(ball_x, ball_y)   # push on the border, never vanish
            prev_head = (yaw, pitch, now)

            if SHOW_MESH:
                draw_face_mesh(frame, face)
                draw_head_direction(frame, lm, yaw, pitch, w, h, neutral[0], neutral[1])
        else:
            prev_head = None   # face lost: no jump when it comes back...
            filt_yaw.reset()   # ...and don't let the smoothing replay the head turn that happened meanwhile
            filt_pitch.reset()
            cv2.putText(frame, "No face detected", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

        # ---------------- Right Alt: aim + click (works with or without a face this frame) ----------------
        try:
            for ev in (clicker.poll() if clicker else []):
                if ev == "aim":
                    aiming = True
                    aim_anchor, aim_steady_since = (ball_x, ball_y), now
                elif ev in ("click", "right") and aiming:
                    aiming = False
                    cx, cy = safe_point(ball_x, ball_y)   # click exactly where the ball is
                    move_cursor(cx, cy)
                    pyautogui.click(button="right" if ev == "right" else "left")
                    print(f"[INPUT] {'Right-click' if ev == 'right' else 'Click'} at ({cx},{cy})")
                    last_click = now
                elif ev.startswith("cancel"):
                    aiming = False
                    if ev == "cancel_esc":
                        print("[INPUT] Click cancelled (Esc)")

            if aiming:
                move_cursor(*safe_point(ball_x, ball_y))   # real cursor sits on the ball
                if muse and MUSE_ENABLED:
                    if math.hypot(ball_x - aim_anchor[0], ball_y - aim_anchor[1]) > DWELL_RADIUS:
                        aim_anchor, aim_steady_since = (ball_x, ball_y), now
                    elif now - aim_steady_since > AIM_STEADY_FOR_MUSE:
                        muse.maybe_trigger(*safe_point(ball_x, ball_y), {"pointer": "face", "ball": [int(ball_x), int(ball_y)]})
            elif DWELL_ENABLED or BLINK_ENABLED:          # hands-free mode: real cursor follows the ball
                move_cursor(*safe_point(ball_x, ball_y))

            # ---- hands-free: blink click ----
            if BLINK_ENABLED and not aiming and result.multi_face_landmarks:
                if eyes_closed:
                    blink_counter += 1
                else:
                    if blink_counter >= BLINK_CONSEC_FRAMES and now - last_click > CLICK_COOLDOWN:
                        pyautogui.click()
                        print("[INPUT] Blink Click")
                        last_click = now
                        dwell_start, dwell_anchor = now, (ball_x, ball_y)
                    blink_counter = 0

            # ---- hands-free: dwell click ----
            if DWELL_ENABLED and not aiming:
                if math.hypot(ball_x - dwell_anchor[0], ball_y - dwell_anchor[1]) > DWELL_RADIUS:
                    dwell_anchor, dwell_start = (ball_x, ball_y), now
                else:
                    dwell_progress = min((now - dwell_start) / DWELL_TIME, 1.0)
                    if now - dwell_start >= DWELL_TIME:
                        if now - last_click > CLICK_COOLDOWN:
                            pyautogui.click()
                            print("[INPUT] Dwell Click")
                            last_click = now
                        dwell_anchor, dwell_start = (ball_x, ball_y), now
        except pyautogui.FailSafeException:
            raise
        except Exception:
            print("[ERROR] while aiming/clicking - aim cancelled, still running:")
            traceback.print_exc()
            aiming = False

        # ---------------- camera-window text ----------------
        cv2.putText(frame, f'BALL:{int(ball_x)},{int(ball_y)}  head speed:{head_speed:.2f}',
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.putText(frame, f'speed x{GAIN_SLOW / 900:.2f}  still<{STILL_SPEED:.3f}  smooth {HEAD_MIN_CUTOFF:.2f}Hz',
                    (10, 115), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)
        if muse and MUSE_ENABLED:
            tgt = (muse.last or {}).get("target", "-")
            cv2.putText(frame, f'MUSE[{muse.status}]: {tgt}', (10, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 1)
        mode_txt = "AIMING - fine steering, release Right Alt to click" if aiming else \
            ("HANDS-FREE (dwell/blink)" if (DWELL_ENABLED or BLINK_ENABLED) else "Steer the ball with your head - hold Right Alt to aim")
        cv2.putText(frame, mode_txt, (10, 165), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (0, 200, 255) if aiming else (180, 180, 180), 2 if aiming else 1)
        cv2.rectangle(frame, (10, 70), (210, 90), (50, 50, 50), -1)
        cv2.rectangle(frame, (10, 70), (10 + int(200 * dwell_progress), 90), (0, 255, 0), -1)

        # ---------------- the blue ball on screen ----------------
        overlay.update(gaze=clamp_to_display(ball_x, ball_y, margin=16))   # always the blue ball, never orange

        cv2.imshow(CAM_TITLE, frame)
        cv2.waitKey(1)                   # keeps the window drawn; its keys are ignored on purpose
        if not _cam_styled:
            make_camera_window_unfocusable()
        _cmd = clicker.poll_command() if clicker else None
        key = ord(_cmd) if _cmd else 255  # commands come ONLY from Right Alt + key

        if key == ord('q'):
            break
        elif key in (ord('r'), ord('c')):
            recenter()
        elif key == ord('v'):
            overlay.toggle()
        elif key in (ord('7'), ord('8')):
            k = 1.2 if key == ord('8') else 1 / 1.2
            GAIN_SLOW *= k
            GAIN_FAST *= k
            print(f"[TUNE] pointer speed x{GAIN_SLOW / 900:.2f}")
        elif key in (ord('9'), ord('0')):
            STILL_SPEED = max(0.003, min(0.08, STILL_SPEED * (1.3 if key == ord('9') else 1 / 1.3)))
            print(f"[TUNE] hold-still threshold = {STILL_SPEED:.3f} ({'steadier' if key == ord('9') else 'snappier'})")
        elif key in (ord('5'), ord('6')):
            HEAD_MIN_CUTOFF = max(0.2, min(3.0, HEAD_MIN_CUTOFF * (1 / 1.3 if key == ord('5') else 1.3)))
            filt_yaw.min_cutoff = filt_pitch.min_cutoff = HEAD_MIN_CUTOFF
            print(f"[TUNE] smoothing {HEAD_MIN_CUTOFF:.2f} Hz ({'smoother' if key == ord('5') else 'quicker'})")
        elif key == ord('f'):
            POINTER_SIGN_X = -POINTER_SIGN_X
            print("[TUNE] left/right flipped")
        elif key == ord('h'):
            POINTER_SIGN_Y = -POINTER_SIGN_Y
            print("[TUNE] up/down flipped")
        elif key == ord('d'):
            DWELL_ENABLED = not DWELL_ENABLED
            dwell_start, dwell_anchor = time.time(), (ball_x, ball_y)
            print(f"[TOGGLE] Hands-free dwell-click = {DWELL_ENABLED}")
        elif key == ord('b'):
            BLINK_ENABLED = not BLINK_ENABLED
            print(f"[TOGGLE] Hands-free blink-click = {BLINK_ENABLED}")
        elif key == ord('m'):
            SHOW_MESH = not SHOW_MESH
            print(f"[TOGGLE] Face mesh = {SHOW_MESH}")
        elif key == ord('i'):
            MUSE_ENABLED = not MUSE_ENABLED
            print(f"[TOGGLE] Muse = {MUSE_ENABLED}")
        elif key == ord('k'):
            toggle_osk()
except pyautogui.FailSafeException:
    print("[FAILSAFE] Mouse in a corner of the main screen - stopping.")
except KeyboardInterrupt:
    print("[STOP] Ctrl+C")
except Exception:
    print("[ERROR] unexpected error - shutting down cleanly:")
    traceback.print_exc()
finally:
    overlay.close()
    if kbhook is not None:
        kbhook.unhook_all()
    cap.release()
    cv2.destroyAllWindows()
    if osk_open:
        subprocess.run("taskkill /f /im osk.exe", shell=True)
