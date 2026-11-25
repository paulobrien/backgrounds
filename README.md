# Backgrounds Camera

An early Windows 11 prototype that captures a webcam, composites an image and simple effects, and publishes the result through a per-user DirectShow virtual camera. The app offers a preview, transparent image overlay, placement, size and opacity controls, mirror and grayscale. It also has a test pattern that does not need a webcam.

**Compatibility is not yet established.** The virtual camera is an experimental 64-bit DirectShow filter; Teams and other apps must be tested. Apps that only enumerate Media Foundation cameras may not see it. A successful installation or preview does not establish that another app can use the virtual camera.

## Quick start (prebuilt, no Python or FFmpeg needed)

The repository includes everything needed to run on 64-bit Windows 11:

- `dist\BackgroundsCamera.exe` is the app, with Python, Pillow and FFmpeg bundled inside.
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
- To run from source instead of the prebuilt executable: Python 3 with Tkinter, the packages in `requirements.txt`, and FFmpeg with DirectShow and `lavfi` support available on `PATH`. Check with `py -3 --version` and `ffmpeg -version`.

## Install for your Windows account

The camera installer copies `dist\BackgroundsCamera.dll` to `%LOCALAPPDATA%\BackgroundsCamera` and registers it for the current user; it does not install Python or FFmpeg. To run the app from source, also install its Python dependencies:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-camera.ps1
py -3 -m pip install --user -r requirements.txt
```

### Licences

The DLL includes Microsoft DirectShow sample code; its [licence notice](dist/MICROSOFT-WINDOWS-CLASSIC-SAMPLES-LICENSE.txt) accompanies it. `BackgroundsCamera.exe` bundles an FFmpeg build licensed under the GNU GPL v3; see [dist/FFMPEG-LICENSE.txt](dist/FFMPEG-LICENSE.txt). The bundled build is the Gyan.dev FFmpeg 9.0.2 essentials build, whose source is available at <https://github.com/FFmpeg/FFmpeg/commit/946fcce07b>. Python packages downloaded by pip have their own licences.

## Use

1. Run `dist\BackgroundsCamera.exe`, or `py -3 .\app\backgrounds.py` from source, and leave the app running.
2. Pick a webcam and click **Start**. To check the app without a physical camera, choose **Test pattern (no camera)**.
3. Choose an image and adjust its position, size and opacity. Enable mirror or grayscale if desired. Transparent PNGs work well for overlays.
4. In Teams, open **Settings → Devices**, select **Backgrounds Virtual Camera**, and check the preview. Join a test call to confirm that the other participant receives the image. If Teams does not list it or displays a black image, this compatibility path has not worked on that setup; it should not be assumed to work in other apps either.

The app must remain running while it supplies video. Close or stop another program that has exclusive use of the physical webcam if capture fails. The current output is fixed at 1280 × 720, nominally 30 fps; actual frame rate depends on the machine. The virtual camera emits black frames while the app is not streaming.

Minimize the window to keep the camera running in the system tray; preview rendering pauses until the window is restored. Click the tray icon or use its **Show** menu item to restore it. The tray menu also offers **Start**, **Stop** and **Exit**. The window's close button exits the app. Enable **Start minimized** to start in the tray next time; this does not automatically start the camera. Launching the app again restores the existing window.

The app prioritizes fresh video: if processing falls behind, it skips pending frames and uses the newest complete frame from FFmpeg. FFmpeg's camera buffer is also kept small (about one compressed frame) to limit how much old video a brief stall can leave queued, and the virtual camera stamps frames using the video application's clock while streaming to reduce timestamp drift. Motion may look less smooth under load. These measures reduce the risk of delay building up, but the video application and camera driver keep their own buffers, so some latency can still occur. The in-app preview updates from every third processed frame (about 10 fps when processing 30 fps) to leave more time for the virtual-camera feed. Clear **Show preview** to skip preview rendering entirely; this saves roughly 2 ms per processed frame.

Settings are saved when the app closes and restored at startup. They are stored in `%APPDATA%\BackgroundsCamera\settings.json` and include the camera, overlay image path, slider positions, effects, preview choice and start-minimized preference. If the saved camera or image is unavailable, the app falls back to its defaults.

## Rebuild the standalone executable

To regenerate `dist\BackgroundsCamera.exe` after changing the app, use PyInstaller:

```powershell
py -3 -m pip install --user -r requirements.txt pyinstaller
powershell -ExecutionPolicy Bypass -File .\scripts\build-exe.ps1 -BundleFfmpeg
```

`-BundleFfmpeg` embeds the `ffmpeg.exe` found on `PATH`; use `-FfmpegPath <path-to-ffmpeg.exe>` to choose a specific build, or omit both to require FFmpeg on `PATH` at runtime. If you bundle a different FFmpeg build, update `dist\FFMPEG-LICENSE.txt` and the source link in [Licences](#licences) to match it.

## Uninstall the virtual camera

Close the app and any applications using the virtual camera, then in ordinary PowerShell run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\uninstall-camera.ps1
```

This removes the per-user virtual camera, not Python, Pillow or a separately installed FFmpeg. Saved app settings remain in `%APPDATA%\BackgroundsCamera\settings.json` and can be deleted manually if you want a clean removal. Remove the repository folder separately if no longer needed.

## Tests

Run `py -3 -m unittest discover -s tests -v`. These tests cover compositing, shared-memory layout and synthetic capture; they do not replace testing in Teams or with a physical camera.
