"""
gaze_overlay.py - see where you're pointing, without moving the mouse.

Three tiny see-through, click-through, always-on-top windows:
  - the blue ball (where you're pointing)
  - an orange ring at the cursor while you hold the click key
  - a green box + name around what will be clicked
Each marker is its OWN small window that moves around. (A single full-screen layer with
markers drawn inside it left orange/green trails behind on some PCs, because Windows
didn't repaint the spots the markers had just left.)
Clicks pass straight through. Uses tkinter (built into Python).
"""
import sys

IS_WINDOWS = sys.platform.startswith("win")
KEY = "#010203"   # this exact colour is made fully transparent
OFF = -32000      # parking spot off-screen = hidden (no show/hide calls that could grab focus)
LABEL_H = 22      # room above the green box for the target's name


class _Marker:
    """One small borderless window holding one canvas."""

    def __init__(self, tk, root, w, h):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=KEY)
        if IS_WINDOWS:
            self.win.attributes("-transparentcolor", KEY)
        self.cv = tk.Canvas(self.win, width=w, height=h, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack()
        self.geom = None
        self.move(OFF, OFF, w, h)

    def move(self, x, y, w, h):
        g = f"{int(w)}x{int(h)}+{int(x)}+{int(y)}"
        if g != self.geom:            # only touch the window when it actually moves/resizes
            self.geom = g
            self.win.geometry(g)
            self.cv.configure(width=int(w), height=int(h))

    def hide(self):
        if self.geom is None or not self.geom.endswith(f"+{OFF}+{OFF}"):
            self.geom = f"1x1+{OFF}+{OFF}"
            self.win.geometry(self.geom)


class GazeOverlay:
    def __init__(self, x, y, w, h):
        self.ok = False
        self.visible = True
        prev = None
        try:
            import tkinter as tk
            if IS_WINDOWS:
                import ctypes
                prev = ctypes.windll.user32.GetForegroundWindow()
            self.root = tk.Tk()
            self.root.withdraw()                      # invisible parent; the markers are its children
            r = self.r = 11
            self.ball = _Marker(tk, self.root, 2 * r + 4, 2 * r + 4)
            self.ball.cv.create_oval(2, 2, 2 * r + 2, 2 * r + 2, outline="#ffffff", fill="#1e88e5", width=2)
            ar = r - 4
            self.ring = _Marker(tk, self.root, 2 * ar + 8, 2 * ar + 8)
            self.ring.cv.create_oval(4, 4, 2 * ar + 4, 2 * ar + 4, outline="#ff9800", width=4)
            self.box = _Marker(tk, self.root, 10, 10)
            self.box_rect = self.box.cv.create_rectangle(0, 0, 0, 0, outline="#00e676", width=4)
            self.box_text = self.box.cv.create_text(2, 2, text="", fill="#00e676", anchor="nw",
                                                    font=("Segoe UI", 11, "bold"))
            self.root.update_idletasks()
            for m in (self.ball, self.ring, self.box):
                self._click_through(m.win)
            self.root.update()
            if prev:  # creating windows can take focus: hand it back to the app you were in
                ctypes.windll.user32.SetForegroundWindow(prev)
            self.ok = True
        except Exception as e:
            print(f"[OVERLAY] off ({type(e).__name__}: {e})")

    @staticmethod
    def _click_through(win):
        """Mouse clicks pass through, and the marker never takes keyboard focus."""
        if not IS_WINDOWS:
            return
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
        WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x80, 0x08000000, 0x8
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE,
                         ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST)

    def update(self, gaze=None, aim=None, target=None):
        """gaze=(x,y) blue ball; aim=(x,y) orange ring while aiming; target = snapped control (green box)."""
        if not self.ok:
            return
        try:
            show = self.visible
            if show and gaze is not None and aim is None:
                s = 2 * self.r + 4
                self.ball.move(gaze[0] - s // 2, gaze[1] - s // 2, s, s)
            else:
                self.ball.hide()
            if show and aim is not None:
                s = 2 * (self.r - 4) + 8
                self.ring.move(aim[0] - s // 2, aim[1] - s // 2, s, s)
            else:
                self.ring.hide()
            if show and aim is not None and target is not None:
                pad = 4
                bw = (target.right - target.left) + 2 * pad
                bh = (target.bottom - target.top) + 2 * pad
                self.box.cv.itemconfigure(self.box_text, text=target.label())
                if target.top - pad - LABEL_H >= 0:   # name above the box
                    self.box.cv.coords(self.box_rect, 2, LABEL_H + 2, bw - 2, LABEL_H + bh - 2)
                    self.box.cv.coords(self.box_text, 2, 2)
                    self.box.move(target.left - pad, target.top - pad - LABEL_H, bw, bh + LABEL_H)
                else:                                  # at the top of the screen: name below the box
                    self.box.cv.coords(self.box_rect, 2, 2, bw - 2, bh - 2)
                    self.box.cv.coords(self.box_text, 2, bh + 2)
                    self.box.move(target.left - pad, target.top - pad, bw, bh + LABEL_H)
            else:
                self.box.hide()
            self.root.update()
        except Exception as e:
            print(f"[OVERLAY] stopped ({type(e).__name__}: {e})")
            self.ok = False

    def toggle(self):
        self.visible = not self.visible
        print(f"[TOGGLE] On-screen ball = {self.visible}")

    def close(self):
        if self.ok:
            try:
                self.root.destroy()
            except Exception:
                pass
