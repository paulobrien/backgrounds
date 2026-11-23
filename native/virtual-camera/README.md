# Virtual camera backend

This is an experimental 64-bit DirectShow camera filter. It advertises one format: 1280 × 720, RGB32, approximately 30 frames per second. The application publishes frames in a current-session shared-memory mapping. The camera outputs black while no producer is running. The filter and its camera instance register only under the current user's `HKCU\Software\Classes`; they do not install a driver or Windows service.

The camera has been discovered and captured by 64-bit FFmpeg on the development machine, including a test of the frame colours and orientation. **Teams, browsers, the Windows Camera app, and 32-bit clients have not been verified.** A DirectShow camera will not necessarily be visible to every application that uses a webcam. Organisational policies can also restrict DLL loading or camera use.

## Build from source

Building is separate from installing: the build machine needs Visual Studio 2022 Build Tools with the C++ desktop workload, Windows SDK, CMake, Git, and internet access to obtain Microsoft's DirectShow base-class sample code. Installing those build tools may require an administrator; the end user does not need them if given a prebuilt DLL.

From the repository root in PowerShell, acquire the sample dependency (the build has been checked with Windows classic samples commit `434f6002bdf9cf9829406c3ff2b33387982d6168`):

```powershell
git clone --filter=blob:none --no-checkout https://github.com/microsoft/Windows-classic-samples.git .build-deps/windows-classic-samples
git -C .build-deps/windows-classic-samples sparse-checkout init --cone
git -C .build-deps/windows-classic-samples sparse-checkout set Samples/Win7Samples/multimedia/directshow/baseclasses
git -C .build-deps/windows-classic-samples checkout 434f6002bdf9cf9829406c3ff2b33387982d6168
```

Then build the 64-bit release DLL:

```powershell
$baseclasses = (Resolve-Path .build-deps/windows-classic-samples/Samples/Win7Samples/multimedia/directshow/baseclasses).Path
cmake -S native/virtual-camera -B build/virtual-camera -A x64 "-DDIRECTSHOW_BASECLASSES_DIR=$baseclasses"
cmake --build build/virtual-camera --config Release
```

The output is `build/virtual-camera/Release/BackgroundsCamera.dll`. The release build uses the static MSVC runtime. Microsoft’s sample source has its own license; retain its license and comply with it when distributing a build.

## Install for your user

Close applications that may be using the camera, open a **normal**, non-administrator PowerShell in the repository root and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-camera.ps1
```

The script copies the DLL to `%LOCALAPPDATA%\BackgroundsCamera` and registers it for the current Windows user. If using a prebuilt DLL at another location, pass `-DllPath` followed by its path. The script does not itself install the companion app or its dependencies; see the repository README for those steps.

Start the companion app and its camera feed, then start or restart Teams and select **Backgrounds Virtual Camera** in its device settings. Try a Teams preview and a test call; a black picture means no frame is being published or the producer cannot be accessed. If the camera does not appear, the application might not support this DirectShow device or may need restarting. Do not select Backgrounds Virtual Camera as the app's physical input: that creates a feedback loop.

## Uninstall

Close Teams and all apps using the camera, then run from the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\uninstall-camera.ps1
```

This unregisters the camera for the current user and removes its copied DLL. If Windows reports that the DLL is in use, close camera applications and retry. The source, build files and application dependencies are not removed.

## Protocol

Both processes use `Local\BackgroundsCameraFrame` and `Local\BackgroundsCameraMutex`. The mapping is 16 bytes of little-endian `uint32`: magic `0x42474331`, width, height, sequence; followed by 1280 × 720 × 4 bytes of bottom-up BGRA. Both sides hold the mutex while accessing the header and frame. The names are scoped to the current Windows session.
