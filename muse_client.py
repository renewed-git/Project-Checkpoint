"""
muse_client.py - threaded, stateless Muse Spark (open-1026) offloader for Gaze Cursor V4.

Uses Baseten's serverless (OpenAI-compatible) Model API, so there is nothing to
deploy and no GPU idling on your $100 credits. Keys are read from env vars only.

Self-test (run from the project folder with gaze-env active):
    python muse_client.py
"""
import os
import io
import json
import time
import base64
import threading

from openai import OpenAI
from PIL import ImageGrab

BASE_URL = "https://inference.baseten.co/v1"
MUSE_MODEL = "meta-models/muse-spark-open-1026"  # locked for V4
# Which env var holds the key that is allowed to call MUSE_MODEL.
# Your self-test showed only BASETEN_API_KEY can call muse-spark-open-1026.
MUSE_KEY_VAR = os.environ.get("MUSE_KEY_VAR", "BASETEN_API_KEY")  # b10_ key = the one that works

CROP_SIZE = 300          # px square around the cursor
REQUEST_TIMEOUT = 10.0   # seconds; the webcam loop never waits on this
MIN_INTERVAL = 1.5       # seconds between requests (debounce)
SAME_SPOT_RADIUS = 60    # px; don't re-ask about the spot we just asked about

SYSTEM_PROMPT = (
    "You are the semantic layer of a gaze-controlled cursor for users with motor disabilities. "
    "The image is a 300x300 screenshot centered on where the user is looking. "
    "Identify the single UI element at or nearest the CENTER. "
    'Reply with ONLY compact JSON: {"target": "<short name, e.g. \'Send button\' or \'key: E\'>", '
    '"kind": "button|key|text_field|link|icon|other", '
    '"suggestion": "<if the user is typing text, a likely next 1-3 words, else empty>"}'
)


def capture_crop_b64(x, y, size=CROP_SIZE):
    """Screenshot a square around (x, y) in VIRTUAL-desktop coords (works on any monitor)."""
    half = size // 2
    bbox = (int(x) - half, int(y) - half, int(x) + half, int(y) + half)
    img = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


MAX_TOKENS = 1500   # reasoning models spend tokens "thinking" before the answer
_reasoning_ok = True  # flips to False if the endpoint rejects reasoning_effort


def ask_muse(client, messages, max_tokens=MAX_TOKENS):
    """Chat call that keeps reasoning short and always returns the visible answer text.
    Returns (text, finish_reason)."""
    global _reasoning_ok
    kwargs = dict(model=MUSE_MODEL, max_tokens=max_tokens, messages=messages)
    if _reasoning_ok:
        try:
            r = client.chat.completions.create(reasoning_effort="low", **kwargs)
        except Exception as e:
            if "reasoning" not in str(getattr(e, "body", e)).lower():
                raise
            _reasoning_ok = False
            r = client.chat.completions.create(**kwargs)
    else:
        r = client.chat.completions.create(**kwargs)
    choice = r.choices[0]
    return (choice.message.content or "").strip(), choice.finish_reason


def _parse(text):
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"target": text[:40], "kind": "other", "suggestion": ""}


class MuseOffloader:
    """Call maybe_trigger() every frame; it fires at most one background request at a time.
    Read .last (dict) / .status (str) from the main loop for the overlay."""

    def __init__(self, model=MUSE_MODEL, key_var=MUSE_KEY_VAR):
        key = os.environ.get(key_var, "")
        if not key:
            raise RuntimeError(f"env var {key_var} is empty - set it in PowerShell first")
        self.client = OpenAI(base_url=BASE_URL, api_key=key, timeout=REQUEST_TIMEOUT, max_retries=0)
        self.model = model
        self.busy = False
        self.last = None          # parsed JSON from the latest successful reply
        self.status = "idle"
        self.latency = 0.0
        self._last_time = 0.0
        self._last_xy = None
        self._lock = threading.Lock()

    def maybe_trigger(self, x, y, gaze_state):
        """gaze_state: dict of the stateless per-frame features, e.g. pupil/yaw/pitch."""
        now = time.time()
        if self.busy or now - self._last_time < MIN_INTERVAL:
            return False
        if self._last_xy and abs(x - self._last_xy[0]) < SAME_SPOT_RADIUS \
                and abs(y - self._last_xy[1]) < SAME_SPOT_RADIUS:
            return False
        with self._lock:
            if self.busy:
                return False
            self.busy = True
        self._last_time, self._last_xy = now, (x, y)
        threading.Thread(target=self._worker, args=(x, y, dict(gaze_state)), daemon=True).start()
        return True

    def _worker(self, x, y, gaze_state):
        t0 = time.time()
        self.status = "asking..."
        try:
            crop = capture_crop_b64(x, y)
            text, finish = ask_muse(self.client, [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Gaze state: " + json.dumps(gaze_state)},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{crop}"}},
                    ]},
                ])
            if not text:
                raise RuntimeError(f"empty reply (finish_reason={finish}) - raise MAX_TOKENS")
            self.last = _parse(text)
            self.latency = time.time() - t0
            self.status = f"ok {self.latency:.1f}s"
            print(f"[MUSE {self.latency:.1f}s] {self.last}")
        except Exception as e:
            self.status = f"error: {type(e).__name__}"
            print(f"[MUSE ERROR] {type(e).__name__}: {getattr(e, 'body', e)}")
        finally:
            self.busy = False


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    print(f"Model: {MUSE_MODEL}\n")
    working = None
    for var in ("HACKATHON_KEY", "BASETEN_API_KEY"):
        k = os.environ.get(var, "")
        if not k:
            print(f"{var}: not set")
            continue
        c = OpenAI(base_url=BASE_URL, api_key=k, timeout=30, max_retries=0)
        try:
            text, finish = ask_muse(c, [{"role": "user", "content": "Say OK"}])
            print(f"{var}: WORKS -> {text!r} (finish_reason={finish}, "
                  f"reasoning_effort={'low' if _reasoning_ok else 'unsupported'})")
            working = working or var
        except Exception as e:
            print(f"{var}: FAILS -> {type(e).__name__} {getattr(e, 'body', '')}")

    if not working:
        print("\nNeither key can call muse-spark-open-1026. Paste the FAILS lines above to get help.")
        raise SystemExit(1)

    print(f"\nUsing {working}. Image test: hover your mouse over a button, starting in 3s...")
    time.sleep(3)
    import pyautogui
    mx, my = pyautogui.position()
    m = MuseOffloader(key_var=working)
    m.maybe_trigger(mx, my, {"test": True})
    while m.busy:
        time.sleep(0.1)
    print("Result:", m.last, "|", m.status)
    if working != MUSE_KEY_VAR:
        print(f'\nSet this once so the cursor uses it:  [System.Environment]::SetEnvironmentVariable("MUSE_KEY_VAR", "{working}", "User")')
