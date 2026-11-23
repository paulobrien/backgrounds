# Backgrounds Camera

An early Windows 11 prototype that captures a webcam, composites an image and simple effects, and publishes the result through a per-user DirectShow virtual camera. The app offers a preview, transparent image overlay, placement, size and opacity controls, mirror and grayscale. It also has a test pattern that does not need a webcam.

**Compatibility is not yet established.** The virtual camera is an experimental 64-bit DirectShow filter; Teams and other apps must be tested. Apps that only enumerate Media Foundation cameras may not see it. A successful installation or preview does not establish that another app can use the virtual camera.

## Requirements

- Windows 11, 64-bit, with camera access enabled. No administrator permission should be needed for the per-user camera installation; organisation policies may prevent it.
- Python 3 with Tkinter and Pillow, and FFmpeg with DirectShow and `lavfi` support available on `PATH`. If Python or FFmpeg is missing, obtain a per-user installation from their official distributors; this repository does not bundle them. Check with `py -3 --version` and `ffmpeg -version`.
- The compiled filter in `dist\BackgroundsCamera.dll` is included in the repository. The filter is x64 only, so 32-bit camera clients cannot load it. See [the native build instructions](native/virtual-camera/README.md) to rebuild it; installing build tools may require administrator access, but installing the camera for your user does not.

## Install for your Windows account

Open an ordinary, **non-administrator** PowerShell in this folder. Install the Python dependency and register the camera for your account:

```powershell
py -3 -m pip install --user Pillow
powershell -ExecutionPolicy Bypass -File .\scripts\install-camera.ps1
```

The camera installer registers the included native DLL for the current user; consult the script and native README for the location and uninstall procedure. It does not install Python or FFmpeg. Python packages downloaded by pip and FFmpeg have their own licenses. The bundled DLL includes Microsoft DirectShow sample code; its [licence notice](dist/MICROSOFT-WINDOWS-CLASSIC-SAMPLES-LICENSE.txt) accompanies it.

## Use

1. Run `py -3 .\app\backgrounds.py` and leave the window open.
2. Pick a webcam and click **Start**. To check the app without a physical camera, choose **Test pattern (no camera)**.
3. Choose an image and adjust its position, size and opacity. Enable mirror or grayscale if desired. Transparent PNGs work well for overlays.
4. In Teams, open **Settings → Devices**, select **Backgrounds Virtual Camera**, and check the preview. Join a test call to confirm that the other participant receives the image. If Teams does not list it or displays a black image, this compatibility path has not worked on that setup; it should not be assumed to work in other apps either.

The app must remain running while it supplies video. Close or stop another program that has exclusive use of the physical webcam if capture fails. The current output is fixed at 1280 × 720, nominally 30 fps; actual frame rate depends on the machine. The virtual camera emits black frames while the app is not streaming.

## Uninstall the virtual camera

Close the app and any applications using the virtual camera, then in ordinary PowerShell run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-camera.ps1
```

This removes the per-user virtual camera, not Python, Pillow or FFmpeg. Remove the repository folder separately if no longer needed.

## Tests

Run `py -3 -m unittest discover -s tests -v`. These tests cover compositing, shared-memory layout and synthetic capture; they do not replace testing in Teams or with a physical camera.
