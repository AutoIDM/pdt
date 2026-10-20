// The MSI setup step: runs the uv tool install that CustomActionData holds and shows its progress
// in the installer dialog. A failed install does not fail the MSI, because the first pdt run retries.
#include <windows.h>
#include <msiquery.h>
#include <cstring>
#include <string>

namespace {

const int TICKS = 500000000;
const DWORD TICK_MS = 250;

// seconds is how long the phase took on a test computer. A phase gets that share of the bar
// that is still empty, so a phase that uv skips (Python already present) leaves no jump.
struct Phase {
    const char* line;
    const wchar_t* text;
    double seconds;
};

const Phase PHASES[] = {
    {"", L"Setting up pdt", 3},
    {"Downloading cpython", L"Downloading Python", 8},
    {"Resolved ", L"Downloading pdt", 6},
    {"Prepared ", L"Installing pdt", 5},
    {"Installed ", L"Preparing pdt", 12},
};
const int COUNT = sizeof(PHASES) / sizeof(PHASES[0]);

class Progress {
public:
    explicit Progress(MSIHANDLE install) : install_(install) {}

    bool show(int phase) {
        phase_ = phase;
        double left = 0;
        for (int i = phase; i < COUNT; ++i) left += PHASES[i].seconds;
        end_ = position_ + (1 - position_) * PHASES[phase].seconds / left;
        PMSIHANDLE action = MsiCreateRecord(3);
        MsiRecordSetStringW(action, 1, L"SetUpPdt");
        MsiRecordSetStringW(action, 2, PHASES[phase].text);
        MsiRecordSetStringW(action, 3, L"");
        if (MsiProcessMessage(install_, INSTALLMESSAGE_ACTIONSTART, action) == IDCANCEL) return false;
        return send(1, 1);
    }

    bool tick() {
        position_ += (end_ - position_) * (TICK_MS / 1000.0) / (PHASES[phase_].seconds / 2);
        return move(position_);
    }

    bool finish() { return move(1.0); }

    void log(const std::wstring& line) {
        PMSIHANDLE record = MsiCreateRecord(1);
        MsiRecordSetStringW(record, 0, L"[1]");
        MsiRecordSetStringW(record, 1, line.c_str());
        MsiProcessMessage(install_, INSTALLMESSAGE_INFO, record);
    }

    int phase() const { return phase_; }

private:
    bool move(double position) {
        int target = static_cast<int>(position * TICKS);
        if (target <= sent_) return true;
        int step = target - sent_;
        sent_ = target;
        return send(2, step);
    }

    bool send(int kind, int ticks) {
        PMSIHANDLE record = MsiCreateRecord(3);
        MsiRecordSetInteger(record, 1, kind);
        MsiRecordSetInteger(record, 2, ticks);
        MsiRecordSetInteger(record, 3, 0);
        return MsiProcessMessage(install_, INSTALLMESSAGE_PROGRESS, record) != IDCANCEL;
    }

    MSIHANDLE install_;
    int phase_ = 0;
    int sent_ = 0;
    double position_ = 0;
    double end_ = 0;
};

std::wstring widen(const std::string& text) {
    int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring wide(size, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), &wide[0], size);
    return wide;
}

bool read_lines(HANDLE pipe, std::string& pending, Progress& progress) {
    DWORD available = 0;
    while (PeekNamedPipe(pipe, nullptr, 0, nullptr, &available, nullptr) && available) {
        char chunk[4096];
        DWORD got = 0;
        if (!ReadFile(pipe, chunk, available < sizeof(chunk) ? available : static_cast<DWORD>(sizeof(chunk)), &got, nullptr) || !got) break;
        pending.append(chunk, got);
        size_t end;
        while ((end = pending.find('\n')) != std::string::npos) {
            std::string line = pending.substr(0, end);
            pending.erase(0, end + 1);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            progress.log(widen(line));
            size_t first = line.find_first_not_of(' ');
            std::string text = first == std::string::npos ? "" : line.substr(first);
            for (int i = progress.phase() + 1; i < COUNT; ++i) {
                if (text.compare(0, strlen(PHASES[i].line), PHASES[i].line) == 0) {
                    if (!progress.show(i)) return false;
                    break;
                }
            }
        }
    }
    return true;
}

UINT run(MSIHANDLE install) {
    DWORD size = 0;
    wchar_t empty[1] = L"";
    MsiGetPropertyW(install, L"CustomActionData", empty, &size);
    std::wstring command(size + 1, L'\0');
    size += 1;
    MsiGetPropertyW(install, L"CustomActionData", &command[0], &size);
    command.resize(size);

    Progress progress(install);
    if (!progress.show(0)) return ERROR_INSTALL_USEREXIT;

    wchar_t local[MAX_PATH];
    DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", local, MAX_PATH);
    if (command.empty() || !length || length >= MAX_PATH) {
        progress.log(L"pdt setup skipped: no command or no LOCALAPPDATA");
        return progress.finish() ? ERROR_SUCCESS : ERROR_INSTALL_USEREXIT;
    }
    std::wstring home = std::wstring(local) + L"\\AutoIDM\\pdt";
    SetEnvironmentVariableW(L"UV_TOOL_DIR", (home + L"\\tools").c_str());
    SetEnvironmentVariableW(L"UV_TOOL_BIN_DIR", (home + L"\\bin").c_str());

    progress.log(L"pdt setup: " + command);
    SECURITY_ATTRIBUTES inherit = {sizeof(inherit), nullptr, TRUE};
    HANDLE read = nullptr, write = nullptr;
    if (!CreatePipe(&read, &write, &inherit, 0)) return ERROR_SUCCESS;
    SetHandleInformation(read, HANDLE_FLAG_INHERIT, 0);
    HANDLE nul = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &inherit, OPEN_EXISTING, 0, nullptr);

    STARTUPINFOW startup = {sizeof(startup)};
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = nul;
    startup.hStdOutput = write;
    startup.hStdError = write;
    PROCESS_INFORMATION process = {};
    BOOL started = CreateProcessW(nullptr, &command[0], nullptr, nullptr, TRUE, CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process);
    CloseHandle(write);
    if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
    if (!started) {
        progress.log(L"pdt setup could not start uv, error " + std::to_wstring(GetLastError()));
        CloseHandle(read);
        return progress.finish() ? ERROR_SUCCESS : ERROR_INSTALL_USEREXIT;
    }

    std::string pending;
    bool cancelled = false;
    for (;;) {
        bool exited = WaitForSingleObject(process.hProcess, TICK_MS) == WAIT_OBJECT_0;
        if (!read_lines(read, pending, progress) || !progress.tick()) {
            cancelled = true;
            TerminateProcess(process.hProcess, ERROR_INSTALL_USEREXIT);
            break;
        }
        if (exited) break;
    }
    DWORD code = 0;
    GetExitCodeProcess(process.hProcess, &code);
    progress.log(L"pdt setup: uv exited with code " + std::to_wstring(code));
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    CloseHandle(read);
    if (cancelled || !progress.finish()) return ERROR_INSTALL_USEREXIT;
    return ERROR_SUCCESS;
}

}  // namespace

extern "C" __declspec(dllexport) UINT __stdcall SetUpPdt(MSIHANDLE install) {
    if (!MsiGetMode(install, MSIRUNMODE_SCHEDULED)) {
        PMSIHANDLE record = MsiCreateRecord(2);
        MsiRecordSetInteger(record, 1, 3);
        MsiRecordSetInteger(record, 2, TICKS);
        return MsiProcessMessage(install, INSTALLMESSAGE_PROGRESS, record) == IDCANCEL ? ERROR_INSTALL_USEREXIT : ERROR_SUCCESS;
    }
    return run(install);
}
