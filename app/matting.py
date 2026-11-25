"""Background blur and replacement using Robust Video Matting (RVM) through ONNX Runtime.

RVM predicts a per-pixel alpha matte and the person's foreground colours. Compositing those
foreground colours (rather than the raw camera pixels) over the new background removes the old
background's colour from hair and edges, and RVM's recurrent state keeps edges stable over time.

The RVM models are downloaded from the official release on first use and checked against pinned
SHA-256 hashes. A small patch made by scripts/build_matting_models.py is then spliced into the
model's protobuf so that preprocessing, background blur and compositing all run on the GPU
alongside the network. The prepared model is cached in %LOCALAPPDATA%\\BackgroundsCamera\\models.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import sys
import threading
import urllib.request

import numpy as np
from PIL import Image, ImageOps

RELEASE_URL = "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/"
# quality: (release file, SHA-256, size in bytes)
SOURCES = {
    "best": ("rvm_resnet50_fp16.onnx", "a9266f5046411d604bbff38e18c54bf8c70d85d93bbc400564697590f5712738", 53752431),
    "fast": ("rvm_mobilenetv3_fp16.onnx", "6a0d5ce6cc17702613be548559879b4521ed424cfe14ddc48d1acaa44d616f64", 7503483),
}
QUALITY_NAMES = {"best": "best quality", "fast": "faster"}
PATCH_NAME = "matting_patch.bin"
CACHE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BackgroundsCamera" / "models"
OUTPUT_NAMES = ["output", "r1o", "r2o", "r3o", "r4o"]
BACKGROUND_MODES = ("off", "blur", "image", "colour")
# RVM's recommended downsample ratio for 1280x720 input; its refiner restores full-resolution detail.
DOWNSAMPLE_RATIO = 0.375
KERNEL_TAPS = 49
MAX_BLUR_SIGMA = 8.0  # In quarter-resolution pixels.
# Graph inputs and outputs replaced by the patch.
REPLACED_VALUES = {b"src", b"fgr", b"pha"}


def patch_path() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) / PATCH_NAME if meipass else Path(__file__).resolve().with_name(PATCH_NAME)


def _read_varint(data, position: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _fields(data):
    """Yield (field number, wire type, field start, payload start, field end) for a protobuf message."""
    position = 0
    while position < len(data):
        start = position
        key, position = _read_varint(data, position)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            _, position = _read_varint(data, position)
            payload = position
        elif wire_type == 1:
            payload, position = position, position + 8
        elif wire_type == 2:
            length, payload = _read_varint(data, position)
            position = payload + length
        elif wire_type == 5:
            payload, position = position, position + 4
        else:
            raise ValueError("Unsupported protobuf wire type in model")
        yield number, wire_type, start, payload, position


def _value_name(data) -> bytes:
    for number, wire_type, _, payload, end in _fields(data):
        if number == 1 and wire_type == 2:
            return bytes(data[payload:end])
    return b""


def splice(model: bytes, patch: bytes) -> bytes:
    """Insert the patch graph pieces into a serialised ONNX ModelProto without needing the onnx package.

    The patch holds two serialised GraphProto fragments: preprocessing nodes, which go before the
    model's nodes, and the postprocessing nodes, inputs, outputs and initializers, which go after.
    Protobuf appends repeated fields in the order they appear, so concatenation merges them.
    """
    (pre_length,) = struct.unpack_from("<I", patch)
    pre, post = patch[4:4 + pre_length], patch[4 + pre_length:]
    view = memoryview(model)
    out = bytearray()
    for number, wire_type, start, payload, end in _fields(view):
        if number != 7 or wire_type != 2:  # ModelProto.graph
            out += view[start:end]
            continue
        graph = view[payload:end]
        merged = bytearray(pre)
        for g_number, g_type, g_start, g_payload, g_end in _fields(graph):
            # GraphProto.input (11) and GraphProto.output (12) entries replaced by the patch.
            if g_number in (11, 12) and _value_name(graph[g_payload:g_end]) in REPLACED_VALUES:
                continue
            merged += graph[g_start:g_end]
        merged += post
        out += _varint(7 << 3 | 2) + _varint(len(merged)) + merged
    return bytes(out)


def download_source(quality: str, directory: Path, progress=None) -> Path:
    """Download and verify the official RVM model, reusing a verified copy if present."""
    name, digest, size = SOURCES[quality]
    target = directory / name
    if target.is_file() and target.stat().st_size == size and _sha256(target) == digest:
        return target
    directory.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    hasher = hashlib.sha256()
    received = reported = 0
    with urllib.request.urlopen(RELEASE_URL + name, timeout=30) as response, partial.open("wb") as file:
        while chunk := response.read(1 << 20):
            file.write(chunk)
            hasher.update(chunk)
            received += len(chunk)
            percent = received * 100 // size
            if progress and percent >= reported + 5:
                reported = percent
                progress(f"Downloading background model ({QUALITY_NAMES[quality]}): "
                         f"{min(percent, 100)}% of {size / 1e6:.0f} MB…")
    if received != size or hasher.hexdigest() != digest:
        partial.unlink(missing_ok=True)
        raise ValueError(f"Downloaded {name} failed its integrity check. Try again later.")
    os.replace(partial, target)
    return target


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()


def ensure_model(quality: str, progress=None, cache_dir: Path | None = None) -> Path:
    """Return the prepared model for a quality, downloading and preparing it on first use."""
    cache_dir = cache_dir or CACHE_DIR
    patch = patch_path().read_bytes()
    prefix = f"backgrounds_rvm_{quality}_"
    target = cache_dir / f"{prefix}{hashlib.sha256(patch).hexdigest()[:12]}.onnx"
    if target.is_file():
        return target
    source = download_source(quality, cache_dir, progress)
    if progress:
        progress("Preparing background model…")
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(splice(source.read_bytes(), patch))
    os.replace(temporary, target)
    source.unlink(missing_ok=True)
    for stale in cache_dir.glob(prefix + "*.onnx"):
        if stale != target:
            stale.unlink(missing_ok=True)
    return target


def blur_kernel(strength: int) -> np.ndarray:
    sigma = max(0.3, MAX_BLUR_SIGMA * strength / 100)
    offsets = np.arange(KERNEL_TAPS, dtype=np.float32) - KERNEL_TAPS // 2
    taps = np.exp(-0.5 * (offsets / sigma) ** 2)
    taps /= taps.sum()
    return np.broadcast_to(taps, (8, 1, 1, KERNEL_TAPS)).astype(np.float32)


def parse_colour(colour: str) -> np.ndarray:
    return np.array([[[int(colour[index:index + 2], 16) for index in (1, 3, 5)]]], np.uint8)


class Matter:
    """Runs the wrapped RVM model on consecutive frames, preferring the fastest GPU through DirectML."""

    def __init__(self, model_path: Path):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.log_severity_level = 3
        # Required by the DirectML execution provider.
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        providers: list = ["CPUExecutionProvider"]
        if "DmlExecutionProvider" in ort.get_available_providers():
            providers.insert(0, ("DmlExecutionProvider", {"performance_preference": "high_performance"}))
        self.session = ort.InferenceSession(str(model_path), options, providers=providers)
        self.device = "GPU" if self.session.get_providers()[0] == "DmlExecutionProvider" else "CPU"
        state_type = next(value.type for value in self.session.get_inputs() if value.name == "r1i")
        self.state_dtype = np.float16 if "float16" in state_type else np.float32
        self.ratio = np.array([DOWNSAMPLE_RATIO], np.float32)
        self.reset()

    def reset(self):
        self.state = [np.zeros((1, 1, 1, 1), self.state_dtype)] * 4

    def __call__(self, rgba: np.ndarray, background: np.ndarray, blur: bool, kernel: np.ndarray) -> np.ndarray:
        """Composite an RGBA uint8 frame over the background (or the blurred scene); returns RGB uint8."""
        inputs = dict(frame=rgba, background=background, blur_mix=np.array([1 if blur else 0], np.float32),
                      blur_kernel=kernel, downsample_ratio=self.ratio,
                      **{f"r{index}i": state for index, state in enumerate(self.state, 1)})
        output, *self.state = self.session.run(OUTPUT_NAMES, inputs)
        return output


class BackgroundProcessor:
    """Applies the selected background effect, preparing the matting model in the background.

    Frames pass through unchanged while a model downloads or loads. A failure is raised once from
    process() and not retried until the quality setting changes.
    """

    def __init__(self, matter_factory=Matter, model_provider=ensure_model, notify=None):
        self.matter_factory = matter_factory
        self.model_provider = model_provider
        self.notify = notify or (lambda message: None)
        self.lock = threading.Lock()
        self.thread = None
        self.matter = None
        self.quality = None
        self.error = None
        self.failed_quality = None
        self.active = False
        self.image_key = None
        self.image_array = None
        self.kernel_strength = None
        self.kernel = None
        self.blank = np.zeros((1, 1, 3), np.uint8)

    def load(self, quality: str):
        """Return the matter for a quality, or None while it is being prepared."""
        with self.lock:
            if self.matter is not None and self.quality == quality:
                return self.matter
            if self.error is not None and self.error[0] == quality:
                error, self.error, self.failed_quality = self.error[1], None, quality
                raise error
            if self.failed_quality == quality or (self.thread is not None and self.thread.is_alive()):
                return None
            self.failed_quality = None
            self.thread = threading.Thread(target=self._prepare, args=(quality,), daemon=True)
            self.thread.start()
            return None

    def _prepare(self, quality: str):
        try:
            path = self.model_provider(quality, self.notify)
            self.notify("Loading background model…")
            matter = self.matter_factory(path)
        except Exception as error:
            with self.lock:
                self.error = (quality, error)
            return
        with self.lock:
            self.matter, self.quality, self.active = matter, quality, False
        self.notify(f"Background effect ready ({getattr(matter, 'device', 'GPU')}).")

    def background_image(self, image: Image.Image, size: tuple[int, int]) -> np.ndarray:
        if self.image_key != (id(image), size):
            fitted = ImageOps.fit(image.convert("RGB"), size, Image.Resampling.LANCZOS)
            self.image_array = np.ascontiguousarray(np.asarray(fitted, np.uint8))
            self.image_key = (id(image), size)
        return self.image_array

    def blur_kernel(self, strength: int) -> np.ndarray:
        if self.kernel_strength != strength:
            self.kernel, self.kernel_strength = blur_kernel(strength), strength
        return self.kernel

    def process(self, image: Image.Image, options: dict) -> Image.Image:
        mode = options.get("background", "off")
        if mode == "off" or (mode == "image" and options.get("background_image") is None):
            self.active = False
            return image
        matter = self.load(options.get("quality", "best"))
        if matter is None:
            return image
        if not self.active:
            matter.reset()
            self.active = True
        if mode == "image":
            background = self.background_image(options["background_image"], image.size)
        elif mode == "colour":
            background = parse_colour(options.get("background_colour", "#000000"))
        else:
            background = self.blank
        rgba = np.asarray(image if image.mode == "RGBA" else image.convert("RGBA"))
        output = matter(rgba, background, mode == "blur", self.blur_kernel(options.get("blur", 50)))
        return Image.fromarray(output, "RGB")
