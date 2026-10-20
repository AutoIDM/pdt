#include <windows.h>
#include <string>

#define WIDEN(x) L## #x
#define VERSION_TEXT(x) WIDEN(x)

namespace {

const wchar_t* VERSION = VERSION_TEXT(PDT_VERSION);
const char* NEEDS_UV = "pdt needs uv. Install it with: winget install astral-sh.uv\n"
                       "Then open a new PowerShell window and run pdt again.\n";
const DWORD TICK_MS = 250;

HANDLE err = GetStdHandle(STD_ERROR_HANDLE);

void write(const std::string& text) {
    DWORD wrote = 0;
    WriteFile(err, text.data(), static_cast<DWORD>(text.size()), &wrote, nullptr);
}

std::string padded(const std::string& text, size_t width) {
    return text.size() < width ? text + std::string(width - text.size(), ' ') : text;
}

std::wstring environment(const wchar_t* name) {
    DWORD size = GetEnvironmentVariableW(name, nullptr, 0);
    if (!size) return L"";
    std::wstring value(size, L'\0');
    value.resize(GetEnvironmentVariableW(name, &value[0], size));
    return value;
}

bool is_file(const std::wstring& path) {
    DWORD attributes = GetFileAttributesW(path.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

std::wstring find_uv() {
    wchar_t self[MAX_PATH];
    DWORD length = GetModuleFileNameW(nullptr, self, MAX_PATH);
    if (length && length < MAX_PATH) {
        std::wstring bundled(self, length);
        bundled = bundled.substr(0, bundled.find_last_of(L'\\') + 1) + L"uv.exe";
        if (is_file(bundled)) return bundled;
    }
    std::wstring path = environment(L"PATH");
    wchar_t found[MAX_PATH];
    if (!path.empty() && SearchPathW(path.c_str(), L"uv.exe", nullptr, MAX_PATH, found, nullptr)) return found;
    return L"";
}

// The arguments after the program name, as CommandLineToArgvW splits it: a quoted name ends at the
// next quote, an unquoted one at the first space or tab.
const wchar_t* arguments() {
    const wchar_t* line = GetCommandLineW();
    if (*line == L'"') {
        ++line;
        while (*line && *line != L'"') ++line;
        if (*line) ++line;
    } else {
        while (*line && *line != L' ' && *line != L'\t') ++line;
    }
    return line;
}

DWORD run(std::wstring command) {
    STARTUPINFOW startup = {sizeof(startup)};
    PROCESS_INFORMATION process = {};
    if (!CreateProcessW(nullptr, &command[0], nullptr, nullptr, FALSE, 0, nullptr, nullptr, &startup, &process)) {
        write("Could not start pdt, error " + std::to_string(GetLastError()) + "\n");
        return 1;
    }
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    return code;
}

void drain(HANDLE pipe, std::string& output) {
    DWORD available = 0;
    while (PeekNamedPipe(pipe, nullptr, 0, nullptr, &available, nullptr) && available) {
        char chunk[4096];
        DWORD got = 0;
        if (!ReadFile(pipe, chunk, available < sizeof(chunk) ? available : static_cast<DWORD>(sizeof(chunk)), &got, nullptr) || !got) break;
        output.append(chunk, got);
    }
}

bool install(const std::wstring& uv, const std::wstring& home) {
    ULONGLONG started = GetTickCount64();
    auto seconds = [&] { return std::to_string((GetTickCount64() - started) / 1000); };
    std::wstring command = L"\"" + uv + L"\" tool install --python 3.12 --compile-bytecode pdt-cli==" + VERSION;

    std::wstring uv_environment;
    wchar_t* strings = GetEnvironmentStringsW();
    for (const wchar_t* entry = strings; *entry; entry += wcslen(entry) + 1) {
        if (_wcsnicmp(entry, L"UV_TOOL_DIR=", 12) && _wcsnicmp(entry, L"UV_TOOL_BIN_DIR=", 16)) uv_environment.append(entry) += L'\0';
    }
    FreeEnvironmentStringsW(strings);
    uv_environment += L"UV_TOOL_DIR=" + home + L"\\tools" + L'\0';
    uv_environment += L"UV_TOOL_BIN_DIR=" + home + L"\\bin" + L'\0';

    SECURITY_ATTRIBUTES inherit = {sizeof(inherit), nullptr, TRUE};
    HANDLE read = nullptr, write_end = nullptr;
    if (!CreatePipe(&read, &write_end, &inherit, 0)) return false;
    SetHandleInformation(read, HANDLE_FLAG_INHERIT, 0);
    HANDLE nul = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &inherit, OPEN_EXISTING, 0, nullptr);
    STARTUPINFOW startup = {sizeof(startup)};
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = nul;
    startup.hStdOutput = write_end;
    startup.hStdError = write_end;
    PROCESS_INFORMATION process = {};
    BOOL launched = CreateProcessW(nullptr, &command[0], nullptr, nullptr, TRUE, CREATE_UNICODE_ENVIRONMENT, &uv_environment[0], nullptr, &startup, &process);
    DWORD error = GetLastError();
    CloseHandle(write_end);
    if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);

    DWORD mode = 0;
    bool tty = GetConsoleMode(err, &mode) != 0;
    if (!tty) write("Setting up pdt: started\n");
    std::string shown, output;
    DWORD code = 1;
    if (launched) {
        for (;;) {
            if (tty) {
                std::string line = "Setting up pdt (" + seconds() + "s)";
                write("\r" + padded(line, shown.size()));
                shown = line;
            }
            bool exited = WaitForSingleObject(process.hProcess, TICK_MS) == WAIT_OBJECT_0;
            drain(read, output);
            if (exited) break;
        }
        GetExitCodeProcess(process.hProcess, &code);
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
    } else {
        output = "Could not start uv, error " + std::to_string(error) + "\n";
    }
    CloseHandle(read);
    bool ok = code == 0;
    std::string end = "Setting up pdt: " + std::string(ok ? "done" : "failed") + " (" + seconds() + "s)";
    write((tty ? "\r" : "") + padded(end, shown.size()) + "\n");
    if (!ok) write(output);
    return ok;
}

}  // namespace

int wmain() {
    // Ctrl+C reaches every process on the console. The launcher ignores it and waits, so pdt decides
    // how to stop and the launcher returns its exit code. A handler, unlike SetConsoleCtrlHandler(NULL),
    // is not inherited by the child.
    SetConsoleCtrlHandler([](DWORD) -> BOOL { return TRUE; }, TRUE);

    std::wstring local = environment(L"LOCALAPPDATA");
    if (local.empty()) {
        write("pdt needs the LOCALAPPDATA environment variable.\n");
        return 1;
    }
    std::wstring home = local + L"\\AutoIDM\\pdt";
    DWORD installed = GetFileAttributesW((home + L"\\tools\\pdt-cli\\Lib\\site-packages\\pdt_cli-" + VERSION + L".dist-info").c_str());
    if (installed == INVALID_FILE_ATTRIBUTES || !(installed & FILE_ATTRIBUTE_DIRECTORY)) {
        std::wstring uv = find_uv();
        if (uv.empty()) {
            write(NEEDS_UV);
            return 1;
        }
        if (!install(uv, home)) return 1;
    }
    return static_cast<int>(run(L"\"" + home + L"\\bin\\pdt.exe\"" + arguments()));
}
