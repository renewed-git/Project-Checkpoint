# webcam-gaze-tracking
This project is a zero-hardware, AI-native Human-Computer Interaction (HCI) layer that transforms a standard webcam into a high-precision, semantically aware universal cursor. all to create a barrier-free accessibility and productivity tool.

## What it does

Your hands stay on the keyboard; your face replaces the mouse.

- **Steer a blue ball with your head.** Slow head movement is precise, a quick movement covers distance (pointer acceleration). Hold still and the ball holds still.
- **Hold Right Alt to aim, release to click.** The cursor jumps to the ball and steering gets finer while you hold the key. Shift + release = right-click, Esc while holding = cancel.
- **Works in any app** (browsers, VS Code, Terminal...) on Windows. No eye tracker, no calibration — just a webcam.
- Optional **Meta Muse Spark** (via Baseten) names what you're aiming at.

## Setup (Windows, Python 3.11+)

```powershell
python -m venv gaze-env
.\gaze-env\Scripts\Activate.ps1
pip install -r requirements.txt
python gaze_cursor_v10.py
```

Optional, for Muse: set your Baseten key as an environment variable (never put keys in the code):

```powershell
[System.Environment]::SetEnvironmentVariable("BASETEN_API_KEY", "<your key>", "User")
```

## Controls (from any app)

| Keys | Action |
|---|---|
| Hold **Right Alt** | Cursor jumps to the ball, finer steering |
| Release **Right Alt** | Click (hold **Shift** for right-click) |
| **Esc** while holding | Cancel the click |
| Right Alt + **Q** | Quit |
| Right Alt + **R** | Re-center the ball |
| Right Alt + **V** | Show / hide the ball |
| Right Alt + **7 / 8** | Slower / faster pointer |
| Right Alt + **9 / 0** | Steadier / snappier |
| Right Alt + **5 / 6** | Smoother / quicker |
| Right Alt + **F / H** | Flip left-right / up-down |
| Right Alt + **D / B** | Hands-free dwell / blink clicking |
| Mouse to a corner of the main screen | Emergency stop |

Other options: `$env:GAZE_ALL_MONITORS="1"` lets the ball roam all monitors; `$env:GAZE_CLICK_KEY="caps lock"` uses a different click key.

## Files

- `gaze_cursor_v10.py` — the app (webcam, face tracking, pointer, clicking, hotkeys)
- `gaze_overlay.py` — the on-screen blue ball (click-through, never takes focus)
- `muse_client.py` — optional Muse Spark calls on Baseten, in a background thread (`python muse_client.py` tests your key)

Built for the Meta Global AI Developer Hackathon 2026. MIT License.
