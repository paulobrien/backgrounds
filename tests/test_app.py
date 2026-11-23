"""Run with: python -m unittest discover -s tests -v"""
from io import BytesIO
import os
from pathlib import Path
import queue
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import backgrounds as b
from PIL import Image


class CompositingTests(unittest.TestCase):
    def test_alpha_overlay(self):
        image = Image.new("RGB", (b.WIDTH, b.HEIGHT), (0, 0, 255))
        options = dict(overlay=Image.new("RGBA", (1, 1), (255, 0, 0, 128)),
                       x=0, y=0, size=5, opacity=100, mirror=False, grayscale=False)
        result = b.composite(image, options)
        self.assertEqual(result.getpixel((0, 0)), (128, 0, 127))
        self.assertEqual(result.getpixel((100, 100)), (0, 0, 255))

    def test_grayscale_and_mirror(self):
        image = Image.new("RGB", (b.WIDTH, b.HEIGHT))
        image.putpixel((0, 0), (255, 0, 0))
        options = dict(overlay=None, x=0, y=0, size=5, opacity=100, mirror=True, grayscale=True)
        result = b.composite(image, options)
        self.assertEqual(result.getpixel((b.WIDTH - 1, 0)), (76, 76, 76))

    def test_overlay_cache_reuses_resize_and_invalidates_on_changes(self):
        overlay = Image.new("RGBA", (16, 8), (255, 0, 0, 128))
        cache = b.OverlayCache()
        resized = cache.get(overlay, 25, 50)
        self.assertIs(cache.get(overlay, 25, 50), resized)
        self.assertEqual(resized.size, (320, 160))
        self.assertEqual(resized.getpixel((0, 0))[3], 64)
        self.assertIsNot(cache.get(overlay, 25, 100), resized)


class LatestFrameReaderTests(unittest.TestCase):
    def test_slow_consumer_gets_latest_complete_frame_only(self):
        reader = b.LatestFrameReader(BytesIO(b"1112223334"), 3, threading.Event())
        reader.drain()
        self.assertEqual(reader.take(), b"333")
        self.assertIsNone(reader.take())

    def test_stop_discards_pending_frame(self):
        stop = threading.Event()
        reader = b.LatestFrameReader(BytesIO(b"123"), 3, stop)
        reader.drain()
        stop.set()
        self.assertIsNone(reader.take())


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_round_trip(self):
        path = self.folder / "nested" / "settings.json"
        saved = dict(b.DEFAULT_SETTINGS, camera="Integrated Webcam", overlay_path="C:\\logo.png", x=10,
                     size=40, mirror=True, show_preview=False)
        b.save_settings(saved, path)
        self.assertEqual(b.load_settings(path), saved)

    def test_missing_corrupt_and_invalid_values_fall_back_to_defaults(self):
        path = self.folder / "settings.json"
        self.assertEqual(b.load_settings(path), b.DEFAULT_SETTINGS)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(b.load_settings(path), b.DEFAULT_SETTINGS)
        path.write_text('{"size": 500, "opacity": "high", "mirror": 1, "camera": 3, "x": true}', encoding="utf-8")
        self.assertEqual(b.load_settings(path), dict(b.DEFAULT_SETTINGS, size=100))


@unittest.skipUnless(os.name == "nt", "Windows shared memory is required")
class SharedMemoryTests(unittest.TestCase):
    def test_header_and_bottom_up_bgra(self):
        image = Image.new("RGB", (b.WIDTH, b.HEIGHT))
        image.putpixel((0, 0), (1, 2, 3))
        image.putpixel((0, b.HEIGHT - 1), (4, 5, 6))
        writer = b.FrameWriter()
        try:
            writer.write(image)
            writer.mapping.seek(0)
            self.assertEqual(struct.unpack("<4I", writer.mapping.read(16)), (b.MAGIC, b.WIDTH, b.HEIGHT, 1))
            self.assertEqual(writer.mapping.read(4), bytes((6, 5, 4, 255)))
            writer.mapping.seek(16 + (b.HEIGHT - 1) * b.WIDTH * 4)
            self.assertEqual(writer.mapping.read(4), bytes((3, 2, 1, 255)))
        finally:
            writer.close()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg must be installed on PATH")
    def test_capture_test_pattern_and_stop(self):
        # Exercise the actual FFmpeg capture loop without opening a UI or webcam.
        app = b.Application.__new__(b.Application)
        app.ffmpeg = shutil.which("ffmpeg")
        app.proc = None
        app.stop_event = threading.Event()
        app.process_lock = threading.Lock()
        app.options_lock = threading.Lock()
        app.options = dict(overlay=None, x=0, y=0, size=5, opacity=100, mirror=False, grayscale=False)
        app.preview = queue.Queue(maxsize=1)
        app.messages = queue.Queue()
        app.writer = b.FrameWriter()
        worker = threading.Thread(target=app.capture, args=(b.TEST_SOURCE,), daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 15
            while app.writer.sequence == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreater(app.writer.sequence, 0, "Test pattern produced no frame")
        finally:
            app.stop_event.set()
            with app.process_lock:
                if app.proc:
                    app.terminate_process(app.proc)
            worker.join(timeout=8)
            if not worker.is_alive():
                app.writer.close()
        self.assertFalse(worker.is_alive(), "Capture thread did not stop")
        messages = []
        while not app.messages.empty():
            messages.append(app.messages.get_nowait())
        self.assertFalse([message for message in messages if message[0] == "error"], messages)


if __name__ == "__main__":
    unittest.main()
