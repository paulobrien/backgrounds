#include <windows.h>
#include <dshow.h>
#include <streams.h>
#include <ks.h>
#include <ksmedia.h>
#include <new>
#include <cstring>
#include <string>

// {27F551F8-5C2A-4A53-AFBF-942DA7F5E451}
static const GUID CLSID_BackgroundsCamera = {0x27f551f8,0x5c2a,0x4a53,{0xaf,0xbf,0x94,0x2d,0xa7,0xf5,0xe4,0x51}};
static constexpr wchar_t kClsid[] = L"{27F551F8-5C2A-4A53-AFBF-942DA7F5E451}";
static constexpr wchar_t kName[] = L"Backgrounds Virtual Camera";
static constexpr wchar_t kCategory[] = L"{860BB310-5D01-11D0-BD3B-00A0C911CE86}";
static constexpr DWORD kWidth = 1280, kHeight = 720, kBytes = kWidth*kHeight*4;
static constexpr REFERENCE_TIME kFrameTime = 333333;
static HMODULE g_module;
#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif

class CameraStream final : public CSourceStream, public IAMStreamConfig, public IKsPropertySet {
public:
    CameraStream(HRESULT* result, CSource* parent)
        : CSourceStream(NAME("Backgrounds stream"), result, parent, L"Capture") {
        timer_ = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
        if (!timer_) timer_ = CreateWaitableTimerW(nullptr, FALSE, nullptr);
    }
    ~CameraStream() override {
        if (timer_) CloseHandle(timer_);
    }
    DECLARE_IUNKNOWN;
    STDMETHODIMP NonDelegatingQueryInterface(REFIID iid, void** object) override {
        if (iid == IID_IAMStreamConfig) return GetInterface(static_cast<IAMStreamConfig*>(this), object);
        if (iid == IID_IKsPropertySet) return GetInterface(static_cast<IKsPropertySet*>(this), object);
        return CSourceStream::NonDelegatingQueryInterface(iid, object);
    }
    HRESULT GetMediaType(int position, CMediaType* type) override {
        if (!type) return E_POINTER;
        if (position < 0) return E_INVALIDARG;
        if (position > 0) return VFW_S_NO_MORE_ITEMS;
        VIDEOINFOHEADER* v = reinterpret_cast<VIDEOINFOHEADER*>(type->AllocFormatBuffer(sizeof(VIDEOINFOHEADER)));
        if (!v) return E_OUTOFMEMORY;
        ZeroMemory(v, sizeof(*v));
        v->bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        v->bmiHeader.biWidth = kWidth;
        v->bmiHeader.biHeight = kHeight;
        v->bmiHeader.biPlanes = 1;
        v->bmiHeader.biBitCount = 32;
        v->bmiHeader.biCompression = BI_RGB;
        v->bmiHeader.biSizeImage = kBytes;
        v->AvgTimePerFrame = kFrameTime;
        SetRect(&v->rcSource, 0, 0, kWidth, kHeight);
        SetRect(&v->rcTarget, 0, 0, kWidth, kHeight);
        type->SetType(&MEDIATYPE_Video);
        type->SetSubtype(&MEDIASUBTYPE_RGB32);
        type->SetFormatType(&FORMAT_VideoInfo);
        type->SetTemporalCompression(FALSE);
        type->SetSampleSize(kBytes);
        return S_OK;
    }
    HRESULT CheckMediaType(const CMediaType* type) override {
        if (!type || *type->Type() != MEDIATYPE_Video || *type->Subtype() != MEDIASUBTYPE_RGB32 ||
            *type->FormatType() != FORMAT_VideoInfo || type->FormatLength() < sizeof(VIDEOINFOHEADER)) return VFW_E_TYPE_NOT_ACCEPTED;
        auto v = reinterpret_cast<const VIDEOINFOHEADER*>(type->Format());
        return v->bmiHeader.biWidth == kWidth && v->bmiHeader.biHeight == kHeight &&
               v->bmiHeader.biBitCount == 32 && v->bmiHeader.biCompression == BI_RGB
                   ? S_OK : VFW_E_TYPE_NOT_ACCEPTED;
    }
    HRESULT DecideBufferSize(IMemAllocator* allocator, ALLOCATOR_PROPERTIES* properties) override {
        if (!allocator || !properties) return E_POINTER;
        properties->cBuffers = 2; properties->cbBuffer = kBytes;
        ALLOCATOR_PROPERTIES actual{};
        HRESULT hr = allocator->SetProperties(properties, &actual);
        if (FAILED(hr)) return hr;
        return actual.cbBuffer >= static_cast<LONG>(kBytes) ? S_OK : E_FAIL;
    }
    HRESULT OnThreadCreate() override {
        next_ = 0;
        if (!QueryPerformanceFrequency(&frequency_) || !QueryPerformanceCounter(&nextFrame_)) return E_FAIL;
        return S_OK;
    }
    HRESULT FillBuffer(IMediaSample* sample) override {
        if (!sample) return E_POINTER;
        BYTE* output = nullptr;
        HRESULT hr = sample->GetPointer(&output);
        if (FAILED(hr)) return hr;
        if (sample->GetSize() < static_cast<LONG>(kBytes)) return E_FAIL;
        PaceFrame();
        bool copied = false;
        HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, L"Local\\BackgroundsCameraFrame");
        HANDLE mutex = OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, L"Local\\BackgroundsCameraMutex");
        if (mapping && mutex) {
            DWORD wait = WaitForSingleObject(mutex, 20);
            if (wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED) {
                const BYTE* view = static_cast<const BYTE*>(MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 16 + kBytes));
                if (view) {
                    const DWORD* header = reinterpret_cast<const DWORD*>(view);
                    if (header[0] == 0x42474331 && header[1] == kWidth && header[2] == kHeight) {
                        memcpy(output, view + 16, kBytes);
                        copied = true;
                    }
                    UnmapViewOfFile(view);
                }
                ReleaseMutex(mutex);
            }
        }
        if (mutex) CloseHandle(mutex);
        if (mapping) CloseHandle(mapping);
        if (!copied) ZeroMemory(output, kBytes);
        // Stamp live frames with the graph's stream time so clients render them now. A free-running
        // counter can drift from the graph clock and make clients queue frames for later.
        CRefTime streamTime;
        FILTER_STATE state = State_Stopped;
        REFERENCE_TIME start = SUCCEEDED(m_pFilter->GetState(0, &state)) && state == State_Running &&
                m_pFilter->StreamTime(streamTime) == S_OK ? static_cast<REFERENCE_TIME>(streamTime) : next_;
        REFERENCE_TIME end = start + kFrameTime;
        next_ = end;
        sample->SetTime(&start, &end);
        sample->SetActualDataLength(kBytes);
        sample->SetSyncPoint(TRUE);
        return S_OK;
    }
    STDMETHODIMP SetFormat(AM_MEDIA_TYPE* type) override {
        if (!type) return E_POINTER;
        CMediaType candidate(*type);
        return CheckMediaType(&candidate);
    }
    STDMETHODIMP GetFormat(AM_MEDIA_TYPE** type) override {
        if (!type) return E_POINTER;
        *type = nullptr;
        CMediaType current;
        HRESULT hr = GetMediaType(0, &current);
        if (FAILED(hr)) return hr;
        *type = CreateMediaType(&current);
        return *type ? S_OK : E_OUTOFMEMORY;
    }
    STDMETHODIMP GetNumberOfCapabilities(int* count, int* size) override {
        if (!count || !size) return E_POINTER;
        *count = 1; *size = sizeof(VIDEO_STREAM_CONFIG_CAPS); return S_OK;
    }
    STDMETHODIMP GetStreamCaps(int index, AM_MEDIA_TYPE** type, BYTE* caps) override {
        if (!type || !caps) return E_POINTER;
        if (index != 0) return S_FALSE;
        HRESULT hr = GetFormat(type); if (FAILED(hr)) return hr;
        auto c = reinterpret_cast<VIDEO_STREAM_CONFIG_CAPS*>(caps);
        ZeroMemory(c, sizeof(*c));
        c->guid = FORMAT_VideoInfo;
        c->InputSize = {static_cast<LONG>(kWidth), static_cast<LONG>(kHeight)};
        c->MinCroppingSize = c->MaxCroppingSize = c->InputSize;
        c->MinOutputSize = c->MaxOutputSize = c->InputSize;
        c->CropGranularityX = c->CropGranularityY = 1;
        c->OutputGranularityX = c->OutputGranularityY = 1;
        c->MinFrameInterval = c->MaxFrameInterval = kFrameTime;
        c->MinBitsPerSecond = c->MaxBitsPerSecond = kBytes * 8 * 30;
        return S_OK;
    }
    STDMETHODIMP Set(REFGUID, DWORD, LPVOID, DWORD, LPVOID, DWORD) override { return E_NOTIMPL; }
    STDMETHODIMP Get(REFGUID set, DWORD id, LPVOID, DWORD, LPVOID data, DWORD length, DWORD* returned) override {
        if (set != AMPROPSETID_Pin || id != AMPROPERTY_PIN_CATEGORY) return E_PROP_ID_UNSUPPORTED;
        if (returned) *returned = sizeof(GUID);
        if (!data) return returned ? S_OK : E_POINTER;
        if (length < sizeof(GUID)) return E_UNEXPECTED;
        *static_cast<GUID*>(data) = PIN_CATEGORY_CAPTURE;
        return S_OK;
    }
    STDMETHODIMP QuerySupported(REFGUID set, DWORD id, DWORD* support) override {
        if (set != AMPROPSETID_Pin || id != AMPROPERTY_PIN_CATEGORY) return E_PROP_ID_UNSUPPORTED;
        if (!support) return E_POINTER;
        *support = KSPROPERTY_SUPPORT_GET; return S_OK;
    }
private:
    void PaceFrame() {
        LARGE_INTEGER now{};
        if (!frequency_.QuadPart || !QueryPerformanceCounter(&now)) return;
        if (now.QuadPart < nextFrame_.QuadPart) {
            const LONGLONG remaining = nextFrame_.QuadPart - now.QuadPart;
            bool waited = false;
            if (timer_) {
                LARGE_INTEGER due{};
                due.QuadPart = -((remaining * 10000000 + frequency_.QuadPart - 1) / frequency_.QuadPart);
                if (SetWaitableTimer(timer_, &due, 0, nullptr, nullptr, FALSE))
                    waited = WaitForSingleObject(timer_, INFINITE) == WAIT_OBJECT_0;
            }
            if (!waited) {
                const DWORD sleepMs = static_cast<DWORD>(remaining * 1000 / frequency_.QuadPart);
                if (sleepMs > 1) Sleep(sleepMs - 1);
                do {
                    SwitchToThread();
                    QueryPerformanceCounter(&now);
                } while (now.QuadPart < nextFrame_.QuadPart);
            }
            QueryPerformanceCounter(&now);
        }
        const LONGLONG interval = frequency_.QuadPart * kFrameTime / 10000000;
        nextFrame_.QuadPart += interval;
        if (nextFrame_.QuadPart <= now.QuadPart) nextFrame_.QuadPart = now.QuadPart + interval;
    }

    HANDLE timer_ = nullptr;
    LARGE_INTEGER frequency_{};
    LARGE_INTEGER nextFrame_{};
    REFERENCE_TIME next_ = 0;
};

class Camera final : public CSource {
public:
    static CUnknown* WINAPI CreateInstance(LPUNKNOWN outer, HRESULT* result) {
        auto camera = new (std::nothrow) Camera(outer, result);
        if (!camera && result) *result = E_OUTOFMEMORY;
        return camera;
    }
    Camera(LPUNKNOWN outer, HRESULT* result) : CSource(NAME("Backgrounds camera"), outer, CLSID_BackgroundsCamera) {
        if (!new (std::nothrow) CameraStream(result, this)) *result = E_OUTOFMEMORY;
    }
};
CFactoryTemplate g_Templates[] = {{L"Backgrounds Virtual Camera", &CLSID_BackgroundsCamera, Camera::CreateInstance, nullptr, nullptr}};
int g_cTemplates = sizeof(g_Templates)/sizeof(g_Templates[0]);

static HRESULT WriteString(HKEY root, const wchar_t* path, const wchar_t* name, const wchar_t* value) {
    HKEY key = nullptr;
    LSTATUS rc = RegCreateKeyExW(root, path, 0, nullptr, 0, KEY_SET_VALUE, nullptr, &key, nullptr);
    if (rc == ERROR_SUCCESS) {
        rc = RegSetValueExW(key, name, 0, REG_SZ, reinterpret_cast<const BYTE*>(value), static_cast<DWORD>((wcslen(value)+1)*sizeof(wchar_t)));
        RegCloseKey(key);
    }
    return HRESULT_FROM_WIN32(rc);
}
STDAPI DllRegisterServer() {
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(g_module, path, MAX_PATH);
    if (!n || n == MAX_PATH) return HRESULT_FROM_WIN32(GetLastError() ? GetLastError() : ERROR_INSUFFICIENT_BUFFER);
    std::wstring cls = std::wstring(L"Software\\Classes\\CLSID\\") + kClsid;
    std::wstring instance = std::wstring(L"Software\\Classes\\CLSID\\") + kCategory + L"\\Instance\\" + kClsid;
    HRESULT hr = WriteString(HKEY_CURRENT_USER, cls.c_str(), nullptr, kName);
    if (SUCCEEDED(hr)) hr = WriteString(HKEY_CURRENT_USER, (cls + L"\\InprocServer32").c_str(), nullptr, path);
    if (SUCCEEDED(hr)) hr = WriteString(HKEY_CURRENT_USER, (cls + L"\\InprocServer32").c_str(), L"ThreadingModel", L"Both");
    if (SUCCEEDED(hr)) hr = WriteString(HKEY_CURRENT_USER, instance.c_str(), L"CLSID", kClsid);
    if (SUCCEEDED(hr)) hr = WriteString(HKEY_CURRENT_USER, instance.c_str(), L"FriendlyName", kName);
    return hr;
}
STDAPI DllUnregisterServer() {
    std::wstring cls = std::wstring(L"Software\\Classes\\CLSID\\") + kClsid;
    std::wstring instance = std::wstring(L"Software\\Classes\\CLSID\\") + kCategory + L"\\Instance\\" + kClsid;
    LSTATUS a = RegDeleteTreeW(HKEY_CURRENT_USER, instance.c_str());
    LSTATUS b = RegDeleteTreeW(HKEY_CURRENT_USER, cls.c_str());
    if (a != ERROR_SUCCESS && a != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(a);
    if (b != ERROR_SUCCESS && b != ERROR_FILE_NOT_FOUND) return HRESULT_FROM_WIN32(b);
    return S_OK;
}
extern "C" BOOL WINAPI DllEntryPoint(HINSTANCE, ULONG, LPVOID);
BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) g_module = module;
    return DllEntryPoint(module, reason, reserved);
}


