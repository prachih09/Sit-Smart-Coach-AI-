# SitSmartCoach.py
# Lightweight always-on-top popup that gives realtime emoji posture feedback.
# - No video window
# - Movable popup
# - Works as .py and as a packaged .exe (with the .spec provided)
# - Logs to SitSmartCoach.log (useful when running as .exe with no console)

import os
import sys
import cv2
import time
import math
import queue
import ctypes
import getpass
import threading
import numpy as np
import tkinter as tk
import traceback
import datetime as dt

# -------- Logging (file-based so we can debug the EXE) --------
LOG_PATH = os.path.join(os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else __file__),
                        "SitSmartCoach.log")
def log(msg: str):
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

log("=== SitSmartCoach starting ===")

# -------- Import mediapipe safely & log details --------
try:
    import mediapipe as mp
    log(f"mediapipe imported from: {os.path.dirname(mp.__file__)}")
except Exception as e:
    log("ERROR importing mediapipe:\n" + traceback.format_exc())
    raise

mp_pose = mp.solutions.pose

# -------- Config & thresholds --------
ELBOW_MIN, ELBOW_MAX = 50, 180           # degrees considered OK
DIST_MIN_CM, DIST_MAX_CM = 70, 100       # perfect distance band (cm)
AVG_SHOULDER_WIDTH_CM = 30               # assumed average shoulder width
FOCAL_LENGTH_PX = 650                    # rough webcam focal length (tweak if needed)

# Smoothing to avoid flicker
SMOOTH_N = 7

# UI refresh & worker cadence
UI_REFRESH_MS = 400
WORKER_SLEEP_S = 0.05  # ~20 FPS processing

# -------- Safe helpers --------
def calculate_angle(a, b, c) -> float:
    """Angle ABC (deg). a,b,c are (x,y) in normalized coords."""
    a = np.array(a); b = np.array(b); c = np.array(c)
    ang = np.degrees(np.arctan2(c[1]-b[1], c[0]-b[0]) - np.arctan2(a[1]-b[1], a[0]-b[0]))
    ang = abs(ang)
    if ang > 180.0:
        ang = 360.0 - ang
    return float(ang)

def estimate_distance_cm(left_sh_px, right_sh_px) -> float:
    """Estimate distance from shoulder pixel gap using pinhole model."""
    try:
        dpx = float(np.linalg.norm(np.array(left_sh_px) - np.array(right_sh_px)))
        if dpx <= 1e-6:
            return 0.0
        # Z = f * real_width / pixel_width
        z = (FOCAL_LENGTH_PX * AVG_SHOULDER_WIDTH_CM) / dpx
        return float(z)
    except Exception:
        return 0.0

def center_gaze_label(nose_x, left_sh_x, right_sh_x) -> str:
    """Very lightweight head/gaze proxy using nose vs shoulder center."""
    cx = (left_sh_x + right_sh_x) / 2.0
    diff = nose_x - cx
    # deadband widened to reduce false 'Left'/'Right'
    if diff < -0.03:
        return "👁️ Looking Left"
    elif diff > 0.03:
        return "👁️ Looking Right"
    else:
        return "👁️ Looking Center"

# -------- Startup shortcut helpers (optional) --------
def _startup_paths():
    user = getpass.getuser()
    startup_dir = os.path.join("C:\\Users", user, "AppData", "Roaming",
                               "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    exe_name = "SitSmartCoach.exe"
    exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.join(os.getcwd(), exe_name)
    lnk_path = os.path.join(startup_dir, "SitSmartCoach.lnk")
    return startup_dir, exe_path, lnk_path

def add_to_startup():
    try:
        startup_dir, exe_path, lnk_path = _startup_paths()
        import win32com.client  # pywin32 must be present when packaging
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(lnk_path)
        shortcut.Targetpath = exe_path
        shortcut.WorkingDirectory = os.path.dirname(exe_path)
        shortcut.IconLocation = exe_path
        shortcut.save()
        log("Added to startup.")
    except Exception as e:
        log("Failed to add to startup: " + str(e))

def remove_from_startup():
    try:
        _, _, lnk_path = _startup_paths()
        if os.path.exists(lnk_path):
            os.remove(lnk_path)
            log("Removed from startup.")
    except Exception as e:
        log("Failed to remove from startup: " + str(e))

# -------- Camera worker thread --------
class PostureWorker(threading.Thread):
    def __init__(self, out_queue: queue.Queue):
        super().__init__(daemon=True)
        self.q = out_queue
        self._stop_evt = threading.Event()
        self._pose = None
        self._cam = None

        # smoothing buffers
        self.angles = []
        self.dists = []
        self.gazes = []

    def stop(self):
        self._stop_evt.set()

    def run(self):
        try:
            # Prefer DirectShow on Windows; set a reasonable resolution
            self._cam = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            self._cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self._cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            if not self._cam.isOpened():
                self.q.put(["⚠️ Camera not detected"])
                log("Camera open failed.")
                return

            self._pose = mp_pose.Pose(min_detection_confidence=0.5,
                                      min_tracking_confidence=0.5,
                                      model_complexity=1)
            log("Pose model created.")

            while not self._stop_evt.is_set():
                ok, frame = self._cam.read()
                if not ok:
                    self.q.put(["⚠️ Unable to read from camera"])
                    time.sleep(0.5)
                    continue

                ih, iw = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb.flags.writeable = False

                res = self._pose.process(rgb)

                msgs = []
                try:
                    lm = res.pose_landmarks.landmark

                    # Elbow angle (left side)
                    lsh = (lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value].x,
                           lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value].y)
                    lel = (lm[mp_pose.PoseLandmark.LEFT_ELBOW.value].x,
                           lm[mp_pose.PoseLandmark.LEFT_ELBOW.value].y)
                    lwr = (lm[mp_pose.PoseLandmark.LEFT_WRIST.value].x,
                           lm[mp_pose.PoseLandmark.LEFT_WRIST.value].y)
                    ang = calculate_angle(lsh, lel, lwr)

                    self.angles.append(ang)
                    if len(self.angles) > SMOOTH_N:
                        self.angles.pop(0)
                    ang_sm = float(np.median(self.angles))

                    if ELBOW_MIN <= ang_sm <= ELBOW_MAX:
                        msgs.append(f"✅ Elbow Angle OK ({int(ang_sm)}°)")
                    else:
                        msgs.append(f"⚠️ Adjust Elbow Angle ({int(ang_sm)}°)")

                    # Distance estimate from shoulder gap (pixels → cm)
                    lsh_px = (lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value].x * iw,
                              lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value].y * ih)
                    rsh_px = (lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].x * iw,
                              lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].y * ih)
                    z_cm = estimate_distance_cm(lsh_px, rsh_px)

                    if z_cm <= 0 or math.isinf(z_cm) or math.isnan(z_cm):
                        # fall back message when geometry not stable
                        msgs.append("⚠️ Re-center for distance")
                    else:
                        self.dists.append(z_cm)
                        if len(self.dists) > SMOOTH_N:
                            self.dists.pop(0)
                        z_sm = float(np.median(self.dists))

                        if DIST_MIN_CM <= z_sm <= DIST_MAX_CM:
                            msgs.append(f"✅ Distance OK ({int(z_sm)} cm)")
                        elif z_sm < DIST_MIN_CM:
                            msgs.append(f"⚠️ Too Close ({int(z_sm)} cm)")
                        else:
                            msgs.append(f"⚠️ Too Far ({int(z_sm)} cm)")

                    # Head/gaze proxy (nose vs shoulder center)
                    nose_x = lm[mp_pose.PoseLandmark.NOSE.value].x
                    gaze = center_gaze_label(nose_x,
                                             lm[mp_pose.PoseLandmark.LEFT_SHOULDER.value].x,
                                             lm[mp_pose.PoseLandmark.RIGHT_SHOULDER.value].x)
                    self.gazes.append(gaze)
                    if len(self.gazes) > SMOOTH_N:
                        self.gazes.pop(0)
                    # majority vote to reduce jitter
                    gaze_final = max(set(self.gazes), key=self.gazes.count)
                    msgs.append(gaze_final)

                except Exception as e:
                    msgs = ["⚠️ Move into Frame"]

                # push msgs to UI (non-blocking)
                try:
                    while not self.q.empty():
                        self.q.get_nowait()
                    self.q.put_nowait(msgs)
                except queue.Full:
                    pass

                time.sleep(WORKER_SLEEP_S)

        except Exception:
            log("Worker crashed:\n" + traceback.format_exc())
            try:
                self.q.put(["⚠️ Internal Error – see SitSmartCoach.log"])
            except Exception:
                pass
        finally:
            try:
                if self._pose:
                    self._pose.close()
            except Exception:
                pass
            try:
                if self._cam and self._cam.isOpened():
                    self._cam.release()
            except Exception:
                pass
            log("Worker stopped.")

# -------- Tkinter movable popup --------
class FloatingPopup(tk.Tk):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.overrideredirect(True)    # borderless
        self.attributes("-topmost", True)
        # Rounded-ish black panel look
        self.configure(bg="#000000")
        self.geometry("+120+120")

        # shadow pad
        self.pad = tk.Frame(self, bg="#000000")
        self.pad.pack(padx=2, pady=2)

        self.panel = tk.Frame(self.pad, bg="#111111")
        self.panel.pack(padx=6, pady=6)

        self.label = tk.Label(self.panel, text="Initializing…",
                              font=("Segoe UI", 12), fg="#FFFFFF", bg="#111111", justify="left")
        self.label.pack(padx=8, pady=(8, 4))

        ctrls = tk.Frame(self.panel, bg="#111111")
        ctrls.pack(pady=(0, 6))

        tk.Button(ctrls, text="❌ Exit", command=self.quit_app,
                  bg="#CC3333", fg="white", bd=0, padx=10, pady=4).pack(side="left", padx=4)

        tk.Button(ctrls, text="🟢 Add Startup", command=add_to_startup,
                  bg="#2E7D32", fg="white", bd=0, padx=10, pady=4).pack(side="left", padx=4)

        tk.Button(ctrls, text="⚪ Remove Startup", command=remove_from_startup,
                  bg="#CCCCCC", fg="#111111", bd=0, padx=10, pady=4).pack(side="left", padx=4)

        # Make window draggable (click anywhere)
        self.bind("<Button-1>", self._start_move)
        self.bind("<B1-Motion>", self._do_move)

        # Keep refreshing label
        self.after(UI_REFRESH_MS, self._pump_queue)

        # Prevent DPI scaling blur on Windows (optional)
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # per-monitor v2 awareness on Win 10+
        except Exception:
            pass

    def _start_move(self, e):
        self._mx, self._my = e.x, e.y

    def _do_move(self, e):
        x = e.x_root - getattr(self, "_mx", 0)
        y = e.y_root - getattr(self, "_my", 0)
        self.geometry(f"+{x}+{y}")

    def _pump_queue(self):
        try:
            msgs = None
            try:
                while True:
                    msgs = self.q.get_nowait()
            except queue.Empty:
                pass
            if msgs:
                self.label.config(text="\n".join(msgs))
        finally:
            self.after(UI_REFRESH_MS, self._pump_queue)

    def quit_app(self):
        self.destroy()

def main():
    # Single-instance guard (optional)
    try:
        import fasteners  # if present, great; otherwise ignore
        lockfile = os.path.join(os.path.dirname(LOG_PATH), "SitSmartCoach.lock")
        lock = fasteners.InterProcessLock(lockfile)
        if not lock.acquire(blocking=False):
            log("Another instance is running; exiting.")
            return
    except Exception:
        pass

    q = queue.Queue(maxsize=2)
    worker = PostureWorker(q)
    worker.start()

    app = FloatingPopup(q)
    try:
        app.mainloop()
    finally:
        worker.stop()
        worker.join(timeout=1.5)
        log("UI closed. Goodbye.")

if __name__ == "__main__":
    main()
