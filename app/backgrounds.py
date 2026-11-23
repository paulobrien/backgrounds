"""Backgrounds: camera capture, simple effects and PNG overlay preview.

The virtual-camera filter consumes the composited frames via named shared memory.
Requires Windows, Python 3, Pillow and an FFmpeg build with DirectShow support.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import mmap
import os
from pathlib import Path
import queue
import re
import shutil
import struct
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import BinaryIO

from PIL import Image, ImageOps, ImageTk

WIDTH, HEIGHT = 1280, 720
MAP_SIZE = 16 + WIDTH * HEIGHT * 4
MAP_NAME = "Local\\BackgroundsCameraFrame"
MUTEX_NAME = "Local\\BackgroundsCameraMutex"
MAGIC = 0x42474331
TEST_SOURCE = "Test pattern (no camera)"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
        frame = image.convert("RGBA").tobytes("raw", "BGRA", 0, -1)
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
        if match and not match.group(1).startswith("Backgrounds"):
            names.append(match.group(1))
    return list(dict.fromkeys(names))


def composite(image: Image.Image, options: dict, overlay_cache: OverlayCache | None = None) -> Image.Image:
    if options["mirror"]:
        image = ImageOps.mirror(image)
    if options["grayscale"]:
        image = ImageOps.grayscale(image).convert("RGB")
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
        self.ffmpeg = shutil.which("ffmpeg")
        self.writer = FrameWriter()
        self.proc = None
        self.worker = None
        self.stop_event = threading.Event()
        self.process_lock = threading.Lock()
        self.options_lock = threading.Lock()
        self.overlay = None
        self.options = {}
        self.messages = queue.Queue()
        self.preview = queue.Queue(maxsize=1)
        self.camera = tk.StringVar(value=TEST_SOURCE)
        self.status = tk.StringVar(value="Choose a camera and click Start.")
        self.overlay_label = tk.StringVar(value="No overlay selected")
        self.x = tk.IntVar(value=100)
        self.y = tk.IntVar(value=100)
        self.size = tk.IntVar(value=25)
        self.opacity = tk.IntVar(value=100)
        self.mirror = tk.BooleanVar(value=False)
        self.grayscale = tk.BooleanVar(value=False)

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
        for label, variable, start, end in (("Horizontal", self.x, 0, 100), ("Vertical", self.y, 0, 100),
                                            ("Size %", self.size, 5, 100), ("Opacity %", self.opacity, 0, 100)):
            box = ttk.Frame(sliders)
            box.pack(side="left", fill="x", expand=True, padx=(0, 8))
            ttk.Label(box, text=label).pack(anchor="w")
            ttk.Scale(box, from_=start, to=end, variable=variable, command=lambda _: self.update_options()).pack(fill="x")
        effects = ttk.Frame(outer)
        effects.pack(fill="x", pady=6)
        ttk.Checkbutton(effects, text="Mirror video", variable=self.mirror, command=self.update_options).pack(side="left")
        ttk.Checkbutton(effects, text="Grayscale video", variable=self.grayscale, command=self.update_options).pack(side="left", padx=12)
        self.preview_label = ttk.Label(outer, text="Preview appears here", anchor="center")
        self.preview_label.pack(fill="both", expand=True, pady=4)
        ttk.Label(outer, textvariable=self.status, wraplength=900).pack(fill="x", pady=(4, 0))
        self.update_options()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self.poll)
        if self.ffmpeg:
            self.refresh()
        else:
            self.status.set("FFmpeg not found. Install FFmpeg and make sure ffmpeg.exe is on PATH.")
            self.start_button.configure(state="disabled")

    def update_options(self):
        with self.options_lock:
            self.options = dict(overlay=self.overlay, x=self.x.get(), y=self.y.get(), size=self.size.get(),
                                opacity=self.opacity.get(), mirror=self.mirror.get(), grayscale=self.grayscale.get())

    def choose_overlay(self):
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All files", "*.*")])
        if not path:
            return
        try:
            with Image.open(path) as image:
                self.overlay = image.convert("RGBA")
            self.overlay_label.set(Path(path).name)
            self.update_options()
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not open image", str(error))

    def remove_overlay(self):
        self.overlay = None
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
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error"]
        if camera == TEST_SOURCE:
            command += ["-re", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30"]
        else:
            command += ["-thread_queue_size", "1", "-rtbufsize", "16M", "-f", "dshow", "-i", "video=" + camera]
        command += ["-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                    "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
        error_file = None
        reader_thread = None
        try:
            # Capture stderr without blocking the FFmpeg process or hiding useful startup errors.
            import tempfile
            error_file = tempfile.TemporaryFile(mode="w+b")
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_file, creationflags=NO_WINDOW)
            with self.process_lock:
                self.proc = proc
                if self.stop_event.is_set():
                    self.terminate_process(proc)
            reader = LatestFrameReader(proc.stdout, WIDTH * HEIGHT * 3, self.stop_event)
            reader_thread = threading.Thread(target=reader.drain, daemon=True)
            reader_thread.start()
            frames = 0
            overlay_cache = OverlayCache()
            while not self.stop_event.is_set():
                data = reader.take()
                if data is None:
                    break
                with self.options_lock:
                    options = self.options.copy()
                image = composite(Image.frombytes("RGB", (WIDTH, HEIGHT), data), options, overlay_cache)
                self.writer.write(image)
                if frames % 3 == 0:
                    image.thumbnail((800, 450), Image.Resampling.BILINEAR)
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
                    self.messages.put(("status", "Streaming. Select Backgrounds Camera in your video application."))
            if not self.stop_event.is_set():
                error_file.seek(0)
                detail = error_file.read(3000).decode("utf-8", "replace").strip()
                self.messages.put(("error", "Capture stopped. " + (detail or "Check the camera is connected and not in use.")))
        except (OSError, ValueError, RuntimeError) as error:
            self.messages.put(("error", f"Capture failed: {error}"))
        finally:
            with self.process_lock:
                proc = self.proc
                self.proc = None
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
            try:
                self.writer.write(Image.new("RGB", (WIDTH, HEIGHT), "black"))
            except (OSError, RuntimeError):
                pass
            self.messages.put(("stopped", "Capture stopped."))

    def stop(self):
        self.stop_event.set()
        with self.process_lock:
            if self.proc:
                self.terminate_process(self.proc)
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
            self.preview_image = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self.preview_image, text="")
        except queue.Empty:
            pass
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "cameras":
                    self.camera_box.configure(values=[TEST_SOURCE] + value)
                    if value and self.camera.get() == TEST_SOURCE:
                        self.camera.set(value[0])
                    if not self.worker or not self.worker.is_alive():
                        self.status.set(f"Found {len(value)} camera(s). Choose a camera and click Start.")
                elif kind == "stopped":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.preview_label.configure(image="", text="Preview appears here")
                    if not self.status.get().startswith("Capture failed") and not self.status.get().startswith("Capture stopped."):
                        self.status.set(value)
                else:
                    self.status.set(value)
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def close(self):
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
    root = tk.Tk()
    try:
        Application(root)
    except Exception as error:
        messagebox.showerror("Backgrounds could not start", str(error))
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
