// BackgroundsCapture: minimal Media Foundation webcam capture for Backgrounds Camera.
//
//   BackgroundsCapture.exe --list              Print video capture device names, one per line (UTF-8).
//   BackgroundsCapture.exe --camera <name>     Stream 1280x720 RGBA frames to stdout.
//   BackgroundsCapture.exe --test              Stream a 30 fps test pattern to stdout.
//
// Frames are written as raw, top-down RGBA with no header. The picture is scaled to fit and
// letterboxed. Errors are written to stderr. The process exits when stdout is closed.
#include <windows.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <wrl/client.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

constexpr UINT32 kWidth = 1280;
constexpr UINT32 kHeight = 720;
constexpr size_t kFrameBytes = size_t{kWidth} * kHeight * 4;

std::string Utf8(const std::wstring& text) {
    if (text.empty()) return {};
    int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
    std::string result(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), result.data(), size, nullptr, nullptr);
    return result;
}

int Fail(const std::wstring& message, HRESULT hr = S_OK) {
    std::string text = Utf8(message);
    if (FAILED(hr)) {
        char code[32];
        snprintf(code, sizeof(code), " (0x%08lX)", static_cast<unsigned long>(hr));
        text += code;
    }
    text += "\n";
    fwrite(text.data(), 1, text.size(), stderr);
    fflush(stderr);
    return 1;
}

std::wstring Describe(HRESULT hr) {
    switch (hr) {
    case E_ACCESSDENIED:
        return L"Camera access was denied. Check Settings > Privacy & security > Camera, including desktop app access.";
    case MF_E_HW_MFT_FAILED_START_STREAMING:
    case MF_E_VIDEO_RECORDING_DEVICE_PREEMPTED:
    case HRESULT_FROM_WIN32(ERROR_SHARING_VIOLATION):
        return L"The camera is in use by another application.";
    case MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED:
        return L"The camera was disconnected.";
    default:
        return L"Camera capture failed.";
    }
}

bool WriteAll(HANDLE output, const uint8_t* data, size_t size) {
    while (size > 0) {
        DWORD written = 0;
        DWORD chunk = static_cast<DWORD>(std::min<size_t>(size, 1u << 24));
        if (!WriteFile(output, data, chunk, &written, nullptr) || written == 0) return false;
        data += written;
        size -= written;
    }
    return true;
}

HRESULT EnumerateCameras(std::vector<ComPtr<IMFActivate>>& devices) {
    ComPtr<IMFAttributes> attributes;
    HRESULT hr = MFCreateAttributes(&attributes, 1);
    if (SUCCEEDED(hr)) hr = attributes->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);
    IMFActivate** found = nullptr;
    UINT32 count = 0;
    if (SUCCEEDED(hr)) hr = MFEnumDeviceSources(attributes.Get(), &found, &count);
    if (FAILED(hr)) return hr;
    for (UINT32 index = 0; index < count; ++index) {
        ComPtr<IMFActivate> device;
        device.Attach(found[index]);
        devices.push_back(device);
    }
    CoTaskMemFree(found);
    return S_OK;
}

std::wstring FriendlyName(IMFActivate* device) {
    WCHAR* name = nullptr;
    UINT32 length = 0;
    if (FAILED(device->GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &name, &length))) return {};
    std::wstring result(name, length);
    CoTaskMemFree(name);
    return result;
}

int ListCameras() {
    std::vector<ComPtr<IMFActivate>> devices;
    HRESULT hr = EnumerateCameras(devices);
    if (FAILED(hr)) return Fail(L"Could not enumerate cameras.", hr);
    for (auto& device : devices) {
        std::string line = Utf8(FriendlyName(device.Get())) + "\n";
        fwrite(line.data(), 1, line.size(), stdout);
    }
    fflush(stdout);
    return 0;
}

struct Mode {
    DWORD index = 0;
    UINT32 width = 0, height = 0;
    double fps = 0;
    bool compressed = false;
};

// Prefer >= 30 fps, then the smallest resolution that still covers 1280x720 (or the largest
// smaller one), then uncompressed formats to avoid decoding, then the rate closest to 30 fps.
bool Better(const Mode& a, const Mode& b) {
    bool a_fps = a.fps >= 29, b_fps = b.fps >= 29;
    if (a_fps != b_fps) return a_fps;
    bool a_covers = a.width >= kWidth && a.height >= kHeight, b_covers = b.width >= kWidth && b.height >= kHeight;
    if (a_covers != b_covers) return a_covers;
    uint64_t a_area = uint64_t{a.width} * a.height, b_area = uint64_t{b.width} * b.height;
    if (a_area != b_area) return a_covers ? a_area < b_area : a_area > b_area;
    if (a.compressed != b.compressed) return !a.compressed;
    return std::abs(a.fps - 30) < std::abs(b.fps - 30);
}

HRESULT ChooseMode(IMFSourceReader* reader, Mode& best) {
    bool found = false;
    for (DWORD index = 0;; ++index) {
        ComPtr<IMFMediaType> type;
        HRESULT hr = reader->GetNativeMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, index, &type);
        if (hr == MF_E_NO_MORE_TYPES) break;
        if (FAILED(hr)) return hr;
        Mode mode;
        mode.index = index;
        UINT32 numerator = 0, denominator = 1;
        GUID subtype{};
        if (FAILED(MFGetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, &mode.width, &mode.height)) ||
            FAILED(type->GetGUID(MF_MT_SUBTYPE, &subtype))) continue;
        MFGetAttributeRatio(type.Get(), MF_MT_FRAME_RATE, &numerator, &denominator);
        mode.fps = denominator ? static_cast<double>(numerator) / denominator : 0;
        mode.compressed = subtype == MFVideoFormat_MJPG || subtype == MFVideoFormat_H264 || subtype == MFVideoFormat_HEVC;
        if (!found || Better(mode, best)) best = mode;
        found = true;
    }
    return found ? S_OK : MF_E_INVALIDMEDIATYPE;
}

struct Layout {
    UINT32 width = 0, height = 0, left = 0, top = 0;
};

Layout Fit(UINT32 width, UINT32 height) {
    double scale = std::min(static_cast<double>(kWidth) / width, static_cast<double>(kHeight) / height);
    Layout layout;
    layout.width = std::min(kWidth, std::max<UINT32>(2, static_cast<UINT32>(std::lround(width * scale / 2)) * 2));
    layout.height = std::min(kHeight, std::max<UINT32>(2, static_cast<UINT32>(std::lround(height * scale / 2)) * 2));
    layout.left = (kWidth - layout.width) / 2;
    layout.top = (kHeight - layout.height) / 2;
    return layout;
}

HRESULT SetOutputType(IMFSourceReader* reader, const Layout& layout) {
    ComPtr<IMFMediaType> type;
    HRESULT hr = MFCreateMediaType(&type);
    if (SUCCEEDED(hr)) hr = type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (SUCCEEDED(hr)) hr = type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_RGB32);
    if (SUCCEEDED(hr)) hr = MFSetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, layout.width, layout.height);
    if (SUCCEEDED(hr)) hr = MFSetAttributeRatio(type.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    if (SUCCEEDED(hr)) hr = type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    if (SUCCEEDED(hr)) hr = reader->SetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, nullptr, type.Get());
    return hr;
}

// Copies BGRX rows into the RGBA frame at the letterbox position.
void CopyRows(const uint8_t* source, LONG pitch, const Layout& layout, std::vector<uint8_t>& frame) {
    for (UINT32 y = 0; y < layout.height; ++y) {
        const uint8_t* in = source + static_cast<ptrdiff_t>(pitch) * y;
        uint8_t* out = frame.data() + (size_t{layout.top + y} * kWidth + layout.left) * 4;
        for (UINT32 x = 0; x < layout.width; ++x, in += 4, out += 4) {
            out[0] = in[2];
            out[1] = in[1];
            out[2] = in[0];
            out[3] = 255;
        }
    }
}

bool CopySample(IMFSample* sample, const Layout& layout, LONG default_stride, std::vector<uint8_t>& frame) {
    ComPtr<IMFMediaBuffer> buffer;
    if (FAILED(sample->ConvertToContiguousBuffer(&buffer))) return false;
    ComPtr<IMF2DBuffer> buffer2d;
    if (SUCCEEDED(buffer.As(&buffer2d))) {
        BYTE* scanline = nullptr;
        LONG pitch = 0;
        if (SUCCEEDED(buffer2d->Lock2D(&scanline, &pitch))) {
            CopyRows(scanline, pitch, layout, frame);
            buffer2d->Unlock2D();
            return true;
        }
    }
    BYTE* data = nullptr;
    DWORD length = 0;
    if (FAILED(buffer->Lock(&data, nullptr, &length))) return false;
    LONG stride = default_stride ? default_stride : static_cast<LONG>(layout.width * 4);
    bool fits = size_t{static_cast<DWORD>(std::abs(stride))} * layout.height <= length;
    if (fits) {
        const BYTE* first = stride < 0 ? data + size_t{static_cast<DWORD>(-stride)} * (layout.height - 1) : data;
        CopyRows(first, stride, layout, frame);
    }
    buffer->Unlock();
    return fits;
}

int Capture(const std::wstring& camera) {
    std::vector<ComPtr<IMFActivate>> devices;
    HRESULT hr = EnumerateCameras(devices);
    if (FAILED(hr)) return Fail(L"Could not enumerate cameras.", hr);
    auto device = std::find_if(devices.begin(), devices.end(),
                               [&](auto& candidate) { return FriendlyName(candidate.Get()) == camera; });
    if (device == devices.end()) return Fail(L"Camera not found: " + camera);

    ComPtr<IMFMediaSource> source;
    hr = (*device)->ActivateObject(IID_PPV_ARGS(&source));
    if (FAILED(hr)) return Fail(Describe(hr), hr);
    ComPtr<IMFAttributes> attributes;
    hr = MFCreateAttributes(&attributes, 1);
    // Lets Media Foundation decode, colour-convert and scale to the requested RGB size.
    if (SUCCEEDED(hr)) hr = attributes->SetUINT32(MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING, TRUE);
    ComPtr<IMFSourceReader> reader;
    if (SUCCEEDED(hr)) hr = MFCreateSourceReaderFromMediaSource(source.Get(), attributes.Get(), &reader);
    if (FAILED(hr)) return Fail(Describe(hr), hr);

    Mode mode;
    hr = ChooseMode(reader.Get(), mode);
    if (FAILED(hr)) return Fail(L"The camera reported no video formats. If Windows Studio Effects is on, try turning it off.", hr);
    ComPtr<IMFMediaType> native;
    hr = reader->GetNativeMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, mode.index, &native);
    if (SUCCEEDED(hr)) hr = reader->SetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, nullptr, native.Get());
    if (FAILED(hr)) return Fail(L"Could not select the camera format.", hr);
    Layout layout = Fit(mode.width, mode.height);
    hr = SetOutputType(reader.Get(), layout);
    if (FAILED(hr)) return Fail(L"Could not convert the camera video to RGB.", hr);

    std::vector<uint8_t> frame(kFrameBytes, 0);
    for (size_t index = 3; index < frame.size(); index += 4) frame[index] = 255;
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    for (;;) {
        DWORD stream = 0, flags = 0;
        LONGLONG timestamp = 0;
        ComPtr<IMFSample> sample;
        hr = reader->ReadSample(MF_SOURCE_READER_FIRST_VIDEO_STREAM, 0, &stream, &flags, &timestamp, &sample);
        if (FAILED(hr)) return Fail(Describe(hr), hr);
        if (flags & (MF_SOURCE_READERF_ERROR | MF_SOURCE_READERF_ENDOFSTREAM)) return Fail(L"The camera stopped sending video.");
        if (!sample) continue;
        ComPtr<IMFMediaType> current;
        LONG stride = 0;
        if (SUCCEEDED(reader->GetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, &current))) {
            UINT32 width = 0, height = 0;
            if (SUCCEEDED(MFGetAttributeSize(current.Get(), MF_MT_FRAME_SIZE, &width, &height)) &&
                (width != layout.width || height != layout.height)) {
                return Fail(L"The camera video could not be scaled to 1280x720.");
            }
            stride = static_cast<LONG>(MFGetAttributeUINT32(current.Get(), MF_MT_DEFAULT_STRIDE, 0));
        }
        if (!CopySample(sample.Get(), layout, stride, frame)) continue;
        if (!WriteAll(output, frame.data(), frame.size())) return 0;  // The app closed the pipe.
    }
}

int TestPattern() {
    static const uint8_t bars[8][3] = {{192, 192, 192}, {192, 192, 0}, {0, 192, 192}, {0, 192, 0},
                                       {192, 0, 192},   {192, 0, 0},   {0, 0, 192},   {16, 16, 16}};
    std::vector<uint8_t> frame(kFrameBytes);
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    LARGE_INTEGER frequency, start, now;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    for (uint64_t count = 0;; ++count) {
        UINT32 box = static_cast<UINT32>((count * 8) % (kWidth - 120));
        for (UINT32 y = 0; y < kHeight; ++y) {
            for (UINT32 x = 0; x < kWidth; ++x) {
                uint8_t* pixel = frame.data() + (size_t{y} * kWidth + x) * 4;
                bool moving = y >= 560 && y < 680 && x >= box && x < box + 120;
                const uint8_t* colour = bars[x * 8 / kWidth];
                pixel[0] = moving ? 255 : colour[0];
                pixel[1] = moving ? 255 : colour[1];
                pixel[2] = moving ? 255 : colour[2];
                pixel[3] = 255;
            }
        }
        if (!WriteAll(output, frame.data(), frame.size())) return 0;
        LONGLONG due = start.QuadPart + static_cast<LONGLONG>((count + 1) * frequency.QuadPart / 30);
        QueryPerformanceCounter(&now);
        if (now.QuadPart < due) Sleep(static_cast<DWORD>((due - now.QuadPart) * 1000 / frequency.QuadPart));
    }
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    std::wstring command = argc > 1 ? argv[1] : L"";
    if (command == L"--test") return TestPattern();
    if (command != L"--list" && !(command == L"--camera" && argc > 2)) {
        return Fail(L"Usage: BackgroundsCapture.exe --list | --camera <name> | --test");
    }
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) return Fail(L"Could not initialise COM.", hr);
    hr = MFStartup(MF_VERSION, MFSTARTUP_LITE);
    if (FAILED(hr)) return Fail(L"Could not start Media Foundation.", hr);
    int result = command == L"--list" ? ListCameras() : Capture(argv[2]);
    MFShutdown();
    CoUninitialize();
    return result;
}
