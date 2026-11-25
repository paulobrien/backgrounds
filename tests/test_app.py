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
import tkinter as tk
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import backgrounds as b
import matting
import numpy as np
from PIL import Image

MODEL_PATH = next(matting.CACHE_DIR.glob("backgrounds_rvm_fast_*.onnx"), Path("missing"))


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

    def test_rgba_composite_preserves_mode(self):
        image = Image.new("RGBA", (b.WIDTH, b.HEIGHT), (0, 0, 255, 255))
        options = dict(overlay=Image.new("RGBA", (1, 1), (255, 0, 0, 128)),
                       x=0, y=0, size=5, opacity=100, mirror=False, grayscale=False)
        result = b.composite(image, options)
        self.assertEqual(result.mode, "RGBA")
        self.assertEqual(result.getpixel((0, 0))[:3], (128, 0, 127))
        self.assertEqual(result.getpixel((100, 100))[:3], (0, 0, 255))

    def test_overlay_cache_reuses_resize_and_invalidates_on_changes(self):
        overlay = Image.new("RGBA", (16, 8), (255, 0, 0, 128))
        cache = b.OverlayCache()
        resized = cache.get(overlay, 25, 50)
        self.assertIs(cache.get(overlay, 25, 50), resized)
        self.assertEqual(resized.size, (320, 160))
        self.assertEqual(resized.getpixel((0, 0))[3], 64)
        self.assertIsNot(cache.get(overlay, 25, 100), resized)


class BackgroundTests(unittest.TestCase):
    """Fake matter: the 'person' is the left half of the frame."""

    class FakeMatter:
        def __init__(self, path):
            self.resets = 0

        def reset(self):
            self.resets += 1

        def __call__(self, rgba, background, blur, kernel):
            output = np.broadcast_to(background, rgba[..., :3].shape).copy()
            output[:, : rgba.shape[1] // 2] = rgba[:, : rgba.shape[1] // 2, :3]
            return output

    def processor(self, provider=lambda quality, notify: Path(f"{quality}.onnx")):
        processor = matting.BackgroundProcessor(matter_factory=self.FakeMatter, model_provider=provider)
        processor.load("best")
        processor.thread.join()
        return processor

    def test_colour_mirror_and_overlay_order(self):
        image = Image.new("RGBA", (b.WIDTH, b.HEIGHT), (0, 0, 255, 255))
        image.putpixel((b.WIDTH - 1, 0), (255, 255, 255, 255))
        options = dict(overlay=Image.new("RGBA", (1, 1), (255, 0, 0, 255)), x=100, y=100, size=5, opacity=100,
                       mirror=True, grayscale=False, background="colour", background_colour="#00ff00")
        result = b.composite(image, options, background=self.processor())
        self.assertEqual(result.getpixel((0, 0))[:3], (255, 255, 255))  # Mirrored before matting.
        self.assertEqual(result.getpixel((b.WIDTH - 100, 10))[:3], (0, 255, 0))
        self.assertEqual(result.getpixel((b.WIDTH - 1, b.HEIGHT - 1))[:3], (255, 0, 0))  # Overlay on top.

    def test_image_background_is_fitted_and_off_restores_original(self):
        processor = self.processor()
        image = Image.new("RGBA", (b.WIDTH, b.HEIGHT), (0, 0, 255, 255))
        background = Image.new("RGB", (640, 640), (9, 9, 9))
        result = processor.process(image, dict(background="image", background_image=background))
        self.assertEqual(result.getpixel((b.WIDTH - 1, 0)), (9, 9, 9))
        self.assertIs(processor.process(image, dict(background="off")), image)
        self.assertIs(processor.process(image, dict(background="image", background_image=None)), image)

    def test_recurrent_state_resets_when_effect_restarts(self):
        processor = self.processor()
        image = Image.new("RGBA", (8, 8))
        for mode in ("blur", "blur", "off", "colour"):
            processor.process(image, dict(background=mode))
        self.assertEqual(processor.matter.resets, 2)

    def test_model_prepares_in_background_and_failure_reports_once(self):
        release = threading.Event()
        messages = []

        def provider(quality, notify):
            notify(f"Downloading {quality}")
            release.wait(5)
            raise OSError("offline")

        processor = matting.BackgroundProcessor(matter_factory=self.FakeMatter, model_provider=provider,
                                                notify=messages.append)
        image = Image.new("RGBA", (8, 8))
        self.assertIs(processor.process(image, dict(background="blur", quality="fast")), image)
        self.assertIs(processor.process(image, dict(background="blur", quality="fast")), image)
        release.set()
        processor.thread.join()
        with self.assertRaisesRegex(OSError, "offline"):
            processor.process(image, dict(background="blur", quality="fast"))
        self.assertIs(processor.process(image, dict(background="blur", quality="fast")), image)
        self.assertEqual(messages, ["Downloading fast"])

    def test_splice_replaces_model_inputs_and_outputs(self):
        def field(number, payload):
            return matting._varint(number << 3 | 2) + matting._varint(len(payload)) + payload

        def value(name):
            return field(1, name.encode())

        graph = field(1, b"model-node") + field(11, value("src")) + field(11, value("r1i")) + \
            field(12, value("fgr")) + field(12, value("pha")) + field(12, value("r1o"))
        model = matting._varint(1 << 3) + matting._varint(6) + field(7, graph)
        pre, post = field(1, b"pre-node"), field(1, b"post-node") + field(12, value("output"))
        spliced = matting.splice(model, len(pre).to_bytes(4, "little") + pre + post)
        fields = list(matting._fields(spliced))
        self.assertEqual(spliced[:fields[0][4]], model[:2])  # ir_version kept.
        graph_bytes = spliced[fields[1][3]:fields[1][4]]
        self.assertEqual(graph_bytes, field(1, b"pre-node") + field(1, b"model-node") + field(11, value("r1i")) +
                         field(12, value("r1o")) + post)

    def test_download_rejects_corrupt_file(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        response = BytesIO(b"x" * matting.SOURCES["fast"][2])
        with mock.patch.object(matting.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "integrity"):
                matting.download_source("fast", folder)
        self.assertEqual(list(folder.iterdir()), [])

    def test_blur_kernel_is_normalised_and_widens_with_strength(self):
        weak, strong = matting.blur_kernel(10), matting.blur_kernel(100)
        self.assertEqual(strong.shape, (8, 1, 1, matting.KERNEL_TAPS))
        self.assertAlmostEqual(float(strong[0, 0, 0].sum()), 1, places=5)
        self.assertGreater(weak[0, 0, 0].max(), strong[0, 0, 0].max())


@unittest.skipUnless(MODEL_PATH.is_file(), "Select the Faster background quality once to download the model")
class MattingModelTests(unittest.TestCase):
    def test_wrapped_model_composites_colour_background(self):
        matter = matting.Matter(MODEL_PATH)
        frame = np.zeros((144, 256, 4), np.uint8)
        output = matter(frame, np.array([[[0, 255, 0]]], np.uint8), False, matting.blur_kernel(50))
        self.assertEqual(output.shape, (144, 256, 3))
        self.assertEqual(output.dtype, np.uint8)
        self.assertTrue((output[0, 0] == (0, 255, 0)).all())  # A blank frame contains no person.
        blurred = matter(frame + 200, np.zeros((1, 1, 3), np.uint8), True, matting.blur_kernel(50))
        self.assertTrue((abs(blurred.astype(int) - 200) <= 2).all())  # Uniform scene stays uniform when blurred.

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

    def test_wake_unblocks_waiting_consumer(self):
        stop = threading.Event()
        reader = b.LatestFrameReader(BytesIO(b""), 3, stop)
        result = []
        thread = threading.Thread(target=lambda: result.append(reader.take()), daemon=True)
        thread.start()
        time.sleep(0.05)
        stop.set()
        reader.wake()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [None])


class HeadLogTests(unittest.TestCase):
    def test_drains_whole_pipe_but_keeps_only_head(self):
        pipe = BytesIO(b"startup error\n" + b"x" * 200000)
        log = b.HeadLog(pipe, limit=20)
        log.drain()
        self.assertEqual(pipe.read(), b"")
        self.assertEqual(log.text(), "startup error\nxxxxxx")


class CameraDiscoveryTests(unittest.TestCase):
    def test_list_cameras_removes_blanks_duplicates_and_virtual_camera(self):
        output = "Integrated Webcam\r\n\r\nBackgrounds Virtual Camera\r\nCamé\r\nIntegrated Webcam\r\n".encode("utf-8")
        completed = mock.Mock(returncode=0, stdout=output, stderr=b"")
        with mock.patch.object(b.subprocess, "run", return_value=completed) as run:
            self.assertEqual(b.list_cameras("helper.exe"), ["Integrated Webcam", "Camé"])
        self.assertEqual(run.call_args.args[0], ["helper.exe", "--list"])

    def test_list_cameras_reports_helper_error(self):
        completed = mock.Mock(returncode=1, stdout=b"", stderr=b"Could not enumerate cameras.")
        with mock.patch.object(b.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(OSError, "Could not enumerate"):
                b.list_cameras("helper.exe")

    def test_build_capture_command(self):
        self.assertEqual(b.build_capture_command("h.exe", "Integrated Webcam"), ["h.exe", "--camera", "Integrated Webcam"])
        self.assertEqual(b.build_capture_command("h.exe", b.TEST_SOURCE), ["h.exe", "--test"])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_round_trip(self):
        path = self.folder / "nested" / "settings.json"
        saved = dict(b.DEFAULT_SETTINGS, camera="Integrated Webcam", overlay_path="C:\\logo.png", x=10,
                     size=40, mirror=True, show_preview=False, start_minimized=True, background="image",
                     background_image_path="C:\\office.jpg", background_colour="#123abc", blur=20, quality="fast")
        b.save_settings(saved, path)
        self.assertEqual(b.load_settings(path), saved)

    def test_missing_corrupt_and_invalid_values_fall_back_to_defaults(self):
        path = self.folder / "settings.json"
        self.assertEqual(b.load_settings(path), b.DEFAULT_SETTINGS)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(b.load_settings(path), b.DEFAULT_SETTINGS)
        path.write_text('{"size": 500, "opacity": "high", "mirror": 1, "camera": 3, "x": true, '
                        '"background": "sparkles", "quality": "ultra", "background_colour": "red"}', encoding="utf-8")
        self.assertEqual(b.load_settings(path), dict(b.DEFAULT_SETTINGS, size=100))


@unittest.skipUnless(os.name == "nt", "Windows system tray is required")
class TrayTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(b.DEFAULT_SETTINGS)
        self.root = tk.Tk()
        self.addCleanup(self.destroy_root)
        patches = (
            mock.patch.object(b, "load_settings", side_effect=lambda: self.settings.copy()),
            mock.patch.object(b, "save_settings"),
            mock.patch.object(b, "FrameWriter"),
            mock.patch.object(b, "find_capture_helper", return_value=None),
            mock.patch.object(b.pystray, "Icon"),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def destroy_root(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def make_app(self):
        app = b.Application(self.root)
        app.tray_started.set()
        return app

    def test_minimize_and_restore_suspend_preview_without_changing_preference(self):
        app = self.make_app()
        app.handle_tray("ready")
        app.hide_window()
        self.assertFalse(app.options["preview"])
        self.assertTrue(app.show_preview.get())
        self.assertEqual(self.root.state(), "withdrawn")
        app.handle_tray("show")
        self.assertTrue(app.options["preview"])
        self.assertNotEqual(self.root.state(), "withdrawn")
        app.close()

    def test_start_minimized_and_second_launch_restore(self):
        self.settings["start_minimized"] = True
        app = self.make_app()
        app.handle_tray("ready")
        self.assertEqual(self.root.state(), "withdrawn")
        app.messages.put(("tray", "show"))
        app.poll()
        self.assertNotEqual(self.root.state(), "withdrawn")
        self.assertTrue(app.current_settings()["start_minimized"])
        app.close()

    def test_show_before_tray_ready_does_not_hide_window(self):
        self.settings["start_minimized"] = True
        app = self.make_app()
        app.handle_tray("show")
        app.handle_tray("ready")
        self.assertNotEqual(self.root.state(), "withdrawn")
        app.close()

    def test_tray_start_can_restart_after_stop(self):
        app = self.make_app()
        app.helper = "helper.exe"
        app.stop_event.set()
        with mock.patch.object(app, "start") as start:
            app.handle_tray("start")
            start.assert_called_once()
        app.close()

    def test_instance_signal_delivers_restore_and_stops(self):
        name = f"Local\\BackgroundsCameraTest_{uuid.uuid4().hex}"
        signal = b.InstanceSignal(name)
        messages = queue.Queue()
        signal.start(messages)
        try:
            self.assertTrue(b.show_existing_instance(name))
            self.assertEqual(messages.get(timeout=3), ("tray", "show"))
        finally:
            signal.close()
        self.assertFalse(signal.thread.is_alive())


@unittest.skipUnless(os.name == "nt", "Windows shared memory is required")
class SharedMemoryTests(unittest.TestCase):
    def setUp(self):
        suffix = uuid.uuid4().hex
        patches = (
            mock.patch.object(b, "MAP_NAME", f"{b.MAP_NAME}_Test_{suffix}"),
            mock.patch.object(b, "MUTEX_NAME", f"{b.MUTEX_NAME}_Test_{suffix}"),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

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

    @unittest.skipUnless(b.find_capture_helper(), "BackgroundsCapture.exe must be built")
    def test_capture_test_pattern_and_stop(self):
        # Exercise the actual capture helper loop without opening a UI or webcam.
        app = b.Application.__new__(b.Application)
        app.helper = b.find_capture_helper()
        app.proc = None
        app.reader = None
        app.stop_event = threading.Event()
        app.process_lock = threading.Lock()
        app.options_lock = threading.Lock()
        app.options = dict(overlay=None, x=0, y=0, size=5, opacity=100, mirror=False, grayscale=False, preview=False)
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
                if app.reader:
                    app.reader.wake()
            worker.join(timeout=8)
            if not worker.is_alive():
                app.writer.close()
        self.assertFalse(worker.is_alive(), "Capture thread did not stop")
        self.assertTrue(app.preview.empty(), "Preview queue should stay empty when preview is disabled")
        messages = []
        while not app.messages.empty():
            messages.append(app.messages.get_nowait())
        self.assertFalse([message for message in messages if message[0] == "error"], messages)


if __name__ == "__main__":
    unittest.main()
