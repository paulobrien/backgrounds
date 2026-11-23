"""Backgrounds: camera capture, simple effects and PNG overlay preview.

The virtual-camera filter consumes the composited frames via named shared memory.
Requires Windows, Python 3, Pillow and an FFmpeg build with DirectShow support.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import mmap
import os
from pathlib import Path
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import BinaryIO

from PIL import Image, ImageOps, ImageTk

WIDTH, HEIGHT = 1280, 720
PREVIEW_SIZE = (720, 405)
MAP_SIZE = 16 + WIDTH * HEIGHT * 4
MAP_NAME = "Local\\BackgroundsCameraFrame"
MUTEX_NAME = "Local\\BackgroundsCameraMutex"
INSTANCE_MUTEX_NAME = "Local\\BackgroundsCameraInstance"
VIRTUAL_CAMERA_NAME = "Backgrounds Virtual Camera"
MAGIC = 0x42474331
TEST_SOURCE = "Test pattern (no camera)"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ERROR_ALREADY_EXISTS = 183
SETTINGS_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "BackgroundsCamera" / "settings.json"
DEFAULT_SETTINGS = dict(camera=None, overlay_path=None, x=100, y=100, size=25, opacity=100,
                        mirror=False, grayscale=False, show_preview=True)
SETTING_RANGES = dict(x=(0, 100), y=(0, 100), size=(5, 100), opacity=(0, 100))


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """Return saved settings merged over defaults, ignoring missing, invalid or out-of-range values."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if not isinstance(saved, dict):
        return settings
    for key in ("camera", "overlay_path"):
        if isinstance(saved.get(key), str):
            settings[key] = saved[key]
    for key, (low, high) in SETTING_RANGES.items():
        value = saved.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            settings[key] = min(high, max(low, round(value)))
    for key in ("mirror", "grayscale", "show_preview"):
        if isinstance(saved.get(key), bool):
            settings[key] = saved[key]
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def find_ffmpeg() -> str | None:
    """Locate ffmpeg.exe in a bundled executable directory or on PATH."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ffmpeg.exe")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "ffmpeg.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffmpeg")


def acquire_instance_lock() -> tuple[ctypes.WinDLL, int] | None:
    """Acquire a per-session mutex so only one app instance writes to shared memory."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel.CloseHandle(handle)
        return None
    return kernel, handle


class FrameWriter:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel = kernel
        self.mutex = kernel.CreateMutexW(None, False, MUTEX_NAME)
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.mapping = mmap.mmap(-1, MAP_SIZE, tagname=MAP_NAME, access=mmap.ACCESS_WRITE)
        except Exception:
            kernel.CloseHandle(self.mutex)
            raise
        self.sequence = 0

    def write(self, image: Image.Image):
        # DirectShow RGB32 is bottom-up, with BGRA byte order.
        rgba = image if image.mode == "RGBA" else image.convert("RGBA")
        frame = rgba.tobytes("raw", "BGRA", 0, -1)
        result = self.kernel.WaitForSingleObject(self.mutex, 1000)
        if result not in (0, 0x80):  # success or abandoned mutex
            raise RuntimeError("Could not lock the virtual camera frame buffer")
        try:
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
            self.mapping.seek(0)
            self.mapping.write(struct.pack("<4I", MAGIC, WIDTH, HEIGHT, self.sequence))
            self.mapping.write(frame)
        finally:
            self.kernel.ReleaseMutex(self.mutex)

    def close(self):
        self.mapping.close()
        self.kernel.CloseHandle(self.mutex)


class LatestFrameReader:
    """Drain a buffered raw-video pipe without queuing stale frames."""

    def __init__(self, pipe: BinaryIO, frame_size: int, stop_event: threading.Event):
        self.pipe = pipe
        self.frame_size = frame_size
        self.stop_event = stop_event
        self.ready = threading.Condition()
        self.latest: bytes | None = None
        self.finished = False
        self.error: OSError | None = None

    def drain(self):
        try:
            while not self.stop_event.is_set():
                frame = self.pipe.read(self.frame_size)
                if len(frame) != self.frame_size:
                    break
                with self.ready:
                    self.latest = frame
                    self.ready.notify()
        except OSError as error:
            self.error = error
        finally:
            with self.ready:
                self.finished = True
                self.ready.notify()

    def wake(self):
        with self.ready:
            self.ready.notify_all()

    def take(self) -> bytes | None:
        with self.ready:
            self.ready.wait_for(lambda: self.latest is not None or self.finished or self.stop_event.is_set())
            if self.stop_event.is_set():
                return None
            if self.latest is not None:
                frame, self.latest = self.latest, None
                return frame
            if self.error is not None:
                raise self.error
            return None


class OverlayCache:
    def __init__(self):
        self.source = None
        self.size = None
        self.opacity = None
        self.image = None

    def get(self, overlay: Image.Image, size: int, opacity: int) -> Image.Image:
        if self.source is not overlay or self.size != size or self.opacity != opacity:
            target_width = max(1, round(WIDTH * size / 100))
            target_height = max(1, round(overlay.height * target_width / overlay.width))
            image = overlay.resize((target_width, target_height), Image.Resampling.LANCZOS)
            if opacity < 100:
                image.putalpha(image.getchannel("A").point(lambda value: value * opacity // 100))
            self.source, self.size, self.opacity, self.image = overlay, size, opacity, image
        return self.image


def list_cameras(ffmpeg: str) -> list[str]:
    # Device listing intentionally returns an error status in some FFmpeg builds.
    result = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                            capture_output=True, text=True, errors="replace", timeout=12,
                            creationflags=NO_WINDOW)
    names = []
    for line in (result.stdout + result.stderr).splitlines():
        if "Alternative name" in line or "(audio)" in line:
            continue
        match = re.search(r'\] "(.*?)" \((?:video|none)\)', line)
        if match and match.group(1).casefold() != VIRTUAL_CAMERA_NAME.casefold():
            names.append(match.group(1))
    return list(dict.fromkeys(names))


def build_ffmpeg_command(ffmpeg: str, camera: str, request_mode: bool = True) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if camera == TEST_SOURCE:
        command += ["-re", "-f", "lavfi", "-i", f"testsrc=size={WIDTH}x{HEIGHT}:rate=30"]
    else:
        command += ["-thread_queue_size", "1", "-rtbufsize", "16M", "-f", "dshow"]
        if request_mode:
            command += ["-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", "30"]
        command += ["-i", "video=" + camera]
    command += ["-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
                "-fps_mode", "passthrough", "-pix_fmt", "rgba", "-f", "rawvideo", "pipe:1"]
    return command


def composite(image: Image.Image, options: dict, overlay_cache: OverlayCache | None = None) -> Image.Image:
    if options["mirror"]:
        image = ImageOps.mirror(image)
    if options["grayscale"]:
        image = ImageOps.grayscale(image).convert(image.mode)
    overlay = options.get("overlay")
    if overlay is not None:
        resized = (overlay_cache or OverlayCache()).get(overlay, options["size"], options["opacity"])
        x = round((WIDTH - resized.width) * options["x"] / 100)
        y = round((HEIGHT - resized.height) * options["y"] / 100)
        image.paste(resized, (x, y), resized)
    return image


class Application:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Backgrounds Camera")
        root.geometry("990x720")
        root.minsize(760, 600)
        self.ffmpeg = find_ffmpeg()
        self.writer = FrameWriter()
        try:
            self._init_ui(root)
        except Exception:
            self.writer.close()
            raise

    def _init_ui(self, root: tk.Tk):
        self.proc = None
        self.reader = None
        self.worker = None
        self.stop_event = threading.Event()
        self.process_lock = threading.Lock()
        self.options_lock = threading.Lock()
        settings = load_settings()
        self.saved_camera = settings["camera"]
        self.startup_warning: str | None = None
        self.overlay = None
        self.overlay_path = None
        self.options = {}
        self.messages = queue.Queue()
        self.preview = queue.Queue(maxsize=1)
        self.camera = tk.StringVar(value=TEST_SOURCE)
        self.status = tk.StringVar(value="Choose a camera and click Start.")
        self.overlay_label = tk.StringVar(value="No overlay selected")
        self.x = tk.IntVar(value=settings["x"])
        self.y = tk.IntVar(value=settings["y"])
        self.size = tk.IntVar(value=settings["size"])
        self.opacity = tk.IntVar(value=settings["opacity"])
        self.mirror = tk.BooleanVar(value=settings["mirror"])
        self.grayscale = tk.BooleanVar(value=settings["grayscale"])
        self.show_preview = tk.BooleanVar(value=settings["show_preview"])

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)
        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        ttk.Label(controls, text="Camera:").pack(side="left")
        self.camera_box = ttk.Combobox(controls, textvariable=self.camera, state="readonly", width=38,
                                       values=[TEST_SOURCE])
        self.camera_box.pack(side="left", padx=6)
        ttk.Button(controls, text="Refresh", command=self.refresh).pack(side="left")
        self.start_button = ttk.Button(controls, text="Start", command=self.start)
        self.start_button.pack(side="left", padx=6)
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        overlay_row = ttk.Frame(outer)
        overlay_row.pack(fill="x", pady=(10, 2))
        ttk.Button(overlay_row, text="Choose image…", command=self.choose_overlay).pack(side="left")
        ttk.Button(overlay_row, text="Remove image", command=self.remove_overlay).pack(side="left", padx=6)
        ttk.Label(overlay_row, textvariable=self.overlay_label).pack(side="left", padx=4)
        sliders = ttk.Frame(outer)
        sliders.pack(fill="x")
        self.slider_labels: list[tuple[str, tk.IntVar, ttk.Label]] = []
        for label, variable, start, end in (("Horizontal", self.x, 0, 100), ("Vertical", self.y, 0, 100),
                                            ("Size %", self.size, 5, 100), ("Opacity %", self.opacity, 0, 100)):
            box = ttk.Frame(sliders)
            box.pack(side="left", fill="x", expand=True, padx=(0, 8))
            heading = ttk.Label(box, text=f"{label}: {variable.get()}")
            heading.pack(anchor="w")
            self.slider_labels.append((label, variable, heading))
            ttk.Scale(box, from_=start, to=end, variable=variable, command=lambda _: self.update_options()).pack(fill="x")
        effects = ttk.Frame(outer)
        effects.pack(fill="x", pady=6)
        ttk.Checkbutton(effects, text="Mirror video", variable=self.mirror, command=self.update_options).pack(side="left")
        ttk.Checkbutton(effects, text="Grayscale video", variable=self.grayscale, command=self.update_options).pack(side="left", padx=12)
        ttk.Checkbutton(effects, text="Show preview", variable=self.show_preview, command=self.toggle_preview).pack(side="left")
        self.preview_label = ttk.Label(outer, anchor="center")
        self.preview_label.pack(fill="both", expand=True, pady=4)
        ttk.Label(outer, textvariable=self.status, wraplength=720).pack(fill="x", pady=(4, 0))
        self.clear_preview()
        if settings["overlay_path"]:
            self.load_overlay(settings["overlay_path"], show_errors=False)
        self.update_options()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.poll)
        if self.ffmpeg:
            self.refresh()
        else:
            self.status.set("FFmpeg not found. Install FFmpeg and make sure ffmpeg.exe is on PATH.")
            self.start_button.configure(state="disabled")

    def update_options(self):
        for label, variable, heading in self.slider_labels:
            heading.configure(text=f"{label}: {variable.get()}")
        with self.options_lock:
            self.options = dict(overlay=self.overlay, x=self.x.get(), y=self.y.get(), size=self.size.get(),
                                opacity=self.opacity.get(), mirror=self.mirror.get(), grayscale=self.grayscale.get(),
                                preview=self.show_preview.get())

    def current_settings(self) -> dict:
        return dict(camera=self.saved_camera or self.camera.get(), overlay_path=self.overlay_path,
                    x=self.x.get(), y=self.y.get(), size=self.size.get(), opacity=self.opacity.get(),
                    mirror=self.mirror.get(), grayscale=self.grayscale.get(), show_preview=self.show_preview.get())

    def clear_preview(self):
        text = "Preview appears here" if self.show_preview.get() else "Preview hidden"
        self.preview_label.configure(image="", text=text)
        self.preview_image = None

    def toggle_preview(self):
        self.update_options()
        self.clear_preview()

    def load_overlay(self, path: str, show_errors: bool = True):
        try:
            with Image.open(path) as image:
                self.overlay = image.convert("RGBA")
            self.overlay_path = str(Path(path).resolve())
            self.overlay_label.set(Path(path).name)
            self.update_options()
        except (OSError, ValueError) as error:
            if show_errors:
                messagebox.showerror("Could not open image", str(error))
            else:
                self.startup_warning = f"Saved overlay could not be loaded: {path}"
                self.status.set(self.startup_warning)

    def choose_overlay(self):
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All files", "*.*")])
        if path:
            self.load_overlay(path)

    def remove_overlay(self):
        self.overlay = None
        self.overlay_path = None
        self.overlay_label.set("No overlay selected")
        self.update_options()

    def refresh(self):
        if not self.ffmpeg:
            return
        self.status.set("Searching for cameras…")
        def run():
            try:
                self.messages.put(("cameras", list_cameras(self.ffmpeg)))
            except (OSError, subprocess.TimeoutExpired) as error:
                self.messages.put(("error", f"Could not enumerate cameras: {error}"))
        threading.Thread(target=run, daemon=True).start()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("Starting capture…")
        self.worker = threading.Thread(target=self.capture, args=(self.camera.get(),), daemon=True)
        self.worker.start()

    def capture(self, camera: str):
        attempts = [True, False] if camera != TEST_SOURCE else [False]
        overlay_cache = OverlayCache()
        try:
            for index, request_mode in enumerate(attempts):
                if self.stop_event.is_set():
                    break
                frames, detail = self._run_ffmpeg(build_ffmpeg_command(self.ffmpeg, camera, request_mode), overlay_cache)
                if frames > 0 or self.stop_event.is_set():
                    if not self.stop_event.is_set():
                        self.messages.put(("error", "Capture stopped. " + (detail or "Check the camera is connected and not in use.")))
                    break
                if index == len(attempts) - 1:
                    self.messages.put(("error", "Capture stopped. " + (detail or "Check the camera is connected and not in use.")))
        except (OSError, ValueError, RuntimeError) as error:
            self.messages.put(("error", f"Capture failed: {error}"))
        finally:
            try:
                self.writer.write(Image.new("RGB", (WIDTH, HEIGHT), "black"))
            except (OSError, RuntimeError):
                pass
            self.messages.put(("stopped", "Capture stopped."))

    def _run_ffmpeg(self, command: list[str], overlay_cache: OverlayCache) -> tuple[int, str]:
        error_file = None
        reader_thread = None
        frames = 0
        detail = ""
        try:
            # Capture stderr without blocking the FFmpeg process or hiding useful startup errors.
            error_file = tempfile.TemporaryFile(mode="w+b")
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_file, creationflags=NO_WINDOW)
            reader = LatestFrameReader(proc.stdout, WIDTH * HEIGHT * 4, self.stop_event)
            with self.process_lock:
                self.proc = proc
                self.reader = reader
                if self.stop_event.is_set():
                    self.terminate_process(proc)
                    reader.wake()
            reader_thread = threading.Thread(target=reader.drain, daemon=True)
            reader_thread.start()
            while not self.stop_event.is_set():
                data = reader.take()
                if data is None:
                    break
                with self.options_lock:
                    options = self.options.copy()
                image = composite(Image.frombytes("RGBA", (WIDTH, HEIGHT), data), options, overlay_cache)
                self.writer.write(image)
                if options.get("preview", True) and frames % 3 == 0:
                    image.thumbnail(PREVIEW_SIZE, Image.Resampling.BILINEAR)
                    try:
                        self.preview.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.preview.put_nowait(image)
                    except queue.Full:
                        pass
                frames += 1
                if frames == 1:
                    self.messages.put(("status", f"Streaming. Select {VIRTUAL_CAMERA_NAME} in your video application."))
            if not self.stop_event.is_set():
                error_file.seek(0)
                detail = error_file.read(3000).decode("utf-8", "replace").strip()
            return frames, detail
        finally:
            with self.process_lock:
                proc = self.proc
                self.proc = None
                reader = self.reader
                self.reader = None
            if reader is not None:
                reader.wake()
            if proc:
                self.terminate_process(proc)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                if reader_thread is not None:
                    reader_thread.join()
                proc.stdout.close()
            if error_file:
                error_file.close()

    def stop(self):
        self.stop_event.set()
        with self.process_lock:
            if self.proc:
                self.terminate_process(self.proc)
            if self.reader:
                self.reader.wake()
        self.stop_button.configure(state="disabled")
        self.status.set("Stopping capture…")

    @staticmethod
    def terminate_process(proc):
        # The process can exit between poll() and terminate().
        try:
            if proc.poll() is None:
                proc.terminate()
        except ProcessLookupError:
            pass
        except OSError:
            if proc.poll() is None:
                raise

    def poll(self):
        try:
            image = self.preview.get_nowait()
            if self.show_preview.get():
                self.preview_image = ImageTk.PhotoImage(image)
                self.preview_label.configure(image=self.preview_image, text="")
        except queue.Empty:
            pass
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "cameras":
                    choices = [TEST_SOURCE] + value
                    self.camera_box.configure(values=choices)
                    if self.saved_camera in choices:
                        self.camera.set(self.saved_camera)
                    elif self.camera.get() not in choices or (value and self.camera.get() == TEST_SOURCE):
                        self.camera.set(value[0] if value else TEST_SOURCE)
                    self.saved_camera = None
                    if not self.worker or not self.worker.is_alive():
                        summary = f"Found {len(value)} camera(s). Choose a camera and click Start."
                        if self.startup_warning:
                            summary = f"{summary} ({self.startup_warning})"
                            self.startup_warning = None
                        self.status.set(summary)
                elif kind == "stopped":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.clear_preview()
                    if not self.status.get().startswith("Capture failed") and not self.status.get().startswith("Capture stopped."):
                        self.status.set(value)
                else:
                    self.status.set(value)
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def close(self):
        try:
            save_settings(self.current_settings())
        except OSError:
            pass
        self.stop()
        # Termination releases the pipe read; wait for the worker before unmapping memory.
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)
        if not self.worker or not self.worker.is_alive():
            self.writer.close()
        self.root.destroy()


def main():
    if os.name != "nt":
        raise SystemExit("Backgrounds Camera requires Windows.")
    lock = acquire_instance_lock()
    root = tk.Tk()
    if lock is None:
        root.withdraw()
        messagebox.showinfo("Backgrounds Camera", "Backgrounds Camera is already running.")
        root.destroy()
        return
    kernel, handle = lock
    try:
        try:
            Application(root)
        except Exception as error:
            messagebox.showerror("Backgrounds could not start", str(error))
            root.destroy()
            return
        root.mainloop()
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
