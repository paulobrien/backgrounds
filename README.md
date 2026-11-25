# Backgrounds Camera

An early Windows 11 prototype that captures a webcam, composites an image and simple effects, and publishes the result through a per-user DirectShow virtual camera. The app offers a preview, transparent image overlay, placement, size and opacity controls, mirror and grayscale. It also has a test pattern that does not need a webcam.

**Compatibility is not yet established.** The virtual camera is an experimental 64-bit DirectShow filter; Teams and other apps must be tested. Apps that only enumerate Media Foundation cameras may not see it. A successful installation or preview does not establish that another app can use the virtual camera.

## Quick start (prebuilt, no Python needed)

The repository includes everything needed to run on 64-bit Windows 11:

- `dist\BackgroundsCamera.exe` is the app, with Python, Pillow and the camera capture helper bundled inside.
- `dist\BackgroundsCamera.dll` is the virtual camera filter.

1. Clone or download this repository (for a ZIP download, extract it first).
2. Open an ordinary, **non-administrator** PowerShell in the repository folder and register the virtual camera for your Windows account:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\install-camera.ps1
   ```

3. Run `dist\BackgroundsCamera.exe`. The executable is unsigned, so Windows SmartScreen may warn the first time; choose **More info → Run anyway** if you trust this copy. The first launch takes a few seconds while it unpacks.

Then continue with [Use](#use). You only need to install the camera once; re-run the installer after updating `dist\BackgroundsCamera.dll`.

## Requirements

- Windows 11, 64-bit, with camera access enabled. No administrator permission should be needed for the per-user camera installation; organisation policies may prevent it.
- The virtual camera filter is x64 only, so 32-bit camera clients cannot load it. See [the native build instructions](native/virtual-camera/README.md) to rebuild it; installing build tools may require administrator access, but installing the camera for your user does not.
- To run from source instead of the prebuilt executable: Python 3 with Tkinter and the packages in `requirements.txt`. Check with `py -3 --version`. The app uses the prebuilt `dist\BackgroundsCapture.exe` camera helper, or one built from [native/capture](native/capture) with `cmake -S native/capture -B build/capture -A x64` and `cmake --build build/capture --config Release` (Visual Studio C++ build tools and CMake required).

## Install for your Windows account

The camera installer copies `dist\BackgroundsCamera.dll` to `%LOCALAPPDATA%\BackgroundsCamera` and registers it for the current user; it does not install Python. To run the app from source, also install its Python dependencies:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-camera.ps1
py -3 -m pip install --user -r requirements.txt
```

### Licences

The DLL includes Microsoft DirectShow sample code; its [licence notice](dist/MICROSOFT-WINDOWS-CLASSIC-SAMPLES-LICENSE.txt) accompanies it. `BackgroundsCamera.exe` bundles ONNX Runtime with DirectML (MIT). On first use of a background effect, the app downloads the [Robust Video Matting](https://github.com/PeterL1n/RobustVideoMatting) model (GPL-3.0) from that project's GitHub release. Python packages downloaded by pip have their own licences.

## Use

1. Run `dist\BackgroundsCamera.exe`, or `py -3 .\app\backgrounds.py` from source, and leave the app running.
2. Pick a webcam and click **Start**. To check the app without a physical camera, choose **Test pattern (no camera)**.
3. Choose an image and adjust its position, size and opacity. Enable mirror or grayscale if desired. Transparent PNGs work well for overlays.
4. In Teams, open **Settings → Devices**, select **Backgrounds Virtual Camera**, and check the preview. Join a test call to confirm that the other participant receives the image. If Teams does not list it or displays a black image, this compatibility path has not worked on that setup; it should not be assumed to work in other apps either.

The app must remain running while it supplies video. Close or stop another program that has exclusive use of the physical webcam if capture fails. The current output is fixed at 1280 × 720, nominally 30 fps; actual frame rate depends on the machine. The virtual camera emits black frames while the app is not streaming.

Minimize the window to keep the camera running in the system tray; preview rendering pauses until the window is restored. Click the tray icon or use its **Show** menu item to restore it. The tray menu also offers **Start**, **Stop** and **Exit**. The window's close button exits the app. Enable **Start minimized** to start in the tray next time; this does not automatically start the camera. Launching the app again restores the existing window.

The app prioritizes fresh video: if processing falls behind, it skips pending frames and uses the newest complete frame from the capture helper. The helper reads the camera through Media Foundation, choosing a format of at least 30 fps closest to 1280 × 720 and letterboxing other aspect ratios. The virtual camera stamps frames using the video application's clock while streaming to reduce timestamp drift. Motion may look less smooth under load. These measures reduce the risk of delay building up, but the video application and camera driver keep their own buffers, so some latency can still occur. The in-app preview updates from every third processed frame (about 10 fps when processing 30 fps) to leave more time for the virtual-camera feed. Clear **Show preview** to skip preview rendering entirely; this saves roughly 2 ms per processed frame.

Settings are saved when the app closes and restored at startup. They are stored in `%APPDATA%\BackgroundsCamera\settings.json` and include the camera, overlay image path, slider positions, effects, background choices, preview choice and start-minimized preference. If the saved camera or image is unavailable, the app falls back to its defaults.

### Background blur and replacement (experimental)

Choose **Background: Blur**, **Image** or **Colour**. **Background image…** and **Colour…** select the replacement and switch to that mode; the **Blur** slider sets blur strength. **Best quality** uses the Robust Video Matting ResNet-50 model; **Faster** uses its MobileNetV3 model.

The app uses alpha matting rather than a simple person mask, so it estimates partial transparency for hair and edges, removes the old background's colour from them, and uses earlier frames to keep edges stable. Mirroring is applied first, so replacement images are not reversed; grayscale and the overlay are applied afterwards.

The first time you use a background effect at each quality, the app downloads the model from the Robust Video Matting GitHub release (about 54 MB for **Best quality**, 8 MB for **Faster**), checks its SHA-256 hash, and prepares it in `%LOCALAPPDATA%\BackgroundsCamera\models`. Video continues without the effect while the status line shows progress. This needs internet access to github.com once; afterwards the cached model is used offline.

Matting, blur and compositing run on the GPU through DirectML (any DirectX 12 GPU), preferring the high-performance GPU. On the development laptop (NVIDIA RTX PRO 500), a 1280 × 720 frame took about 27–32 ms with **Best quality** and 23–25 ms with **Faster**, which is close to the 33 ms available per frame at 30 fps. Without a usable GPU it falls back to the CPU, which was measured at about 200 ms per frame and is too slow for live video. Results will vary by machine. Quality depends heavily on lighting and contrast between you and the background. If the model cannot be downloaded or loaded, the app shows an error and continues without the background effect until capture is restarted.

## Rebuild the standalone executable

To regenerate `dist\BackgroundsCamera.exe` after changing the app, build the capture helper (see [Requirements](#requirements)) and use PyInstaller:

```powershell
py -3 -m pip install --user -r requirements.txt pyinstaller
powershell -ExecutionPolicy Bypass -File .\scripts\build-exe.ps1
```

The script bundles `build\capture\Release\BackgroundsCapture.exe` (copying it to `dist\`), or the existing `dist\BackgroundsCapture.exe` if it has not been built.

`app\matting_patch.bin` adds the preprocessing, blur and compositing steps to the downloaded model so they run on the GPU. If you change them in `scripts\build_matting_models.py`, regenerate the patch with `py -3 -m pip install --user onnx` and `py -3 scripts\build_matting_models.py`. A changed patch makes the app prepare the cached models again.

## Uninstall the virtual camera

Close the app and any applications using the virtual camera, then in ordinary PowerShell run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-camera.ps1
```

This removes the per-user virtual camera, not Python or Pillow. Saved app settings remain in `%APPDATA%\BackgroundsCamera\settings.json` and downloaded models in `%LOCALAPPDATA%\BackgroundsCamera\models`; delete them manually if you want a clean removal. Remove the repository folder separately if no longer needed.

## Tests

Run `py -3 -m unittest discover -s tests -v`. These tests cover compositing, shared-memory layout and synthetic capture; they do not replace testing in Teams or with a physical camera.
