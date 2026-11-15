"""Read-only, same-user/session detection of the packaged Osaurus app.

The backend uses documented Windows APIs via ctypes. An injected backend keeps
the decision/lifetime tests independent of Windows DLLs and real applications.
No function in this module terminates or launches a process.
"""
import ctypes as C
import ntpath
import os
import threading


APP_BASENAME = "Osaurus.App.exe"
PACKAGE_FAMILY = "OsaurusInc.Osaurus_qms1nzmyz2vqe"
DWORD, LONG, BOOL, HANDLE = C.c_uint32, C.c_int32, C.c_int32, C.c_void_p
WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT, WAIT_FAILED = 0, 128, 258, 0xFFFFFFFF


class ProcessGone(OSError):
    """A candidate disappeared before its process handle could be opened."""


class FILETIME(C.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class PROCESSENTRY32W(C.Structure):
    _fields_ = [("dwSize", DWORD), ("cntUsage", DWORD), ("th32ProcessID", DWORD),
                ("th32DefaultHeapID", C.c_size_t), ("th32ModuleID", DWORD),
                ("cntThreads", DWORD), ("th32ParentProcessID", DWORD),
                ("pcPriClassBase", LONG), ("dwFlags", DWORD), ("szExeFile", C.c_wchar * 260)]


class _WindowsApi:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows process monitoring requires Windows")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.security = C.WinDLL("advapi32", use_last_error=True)
        for library, name, result, args in [
            (self.kernel, "CreateToolhelp32Snapshot", HANDLE, [DWORD, DWORD]),
            (self.kernel, "Process32FirstW", BOOL, [HANDLE, C.POINTER(PROCESSENTRY32W)]),
            (self.kernel, "Process32NextW", BOOL, [HANDLE, C.POINTER(PROCESSENTRY32W)]),
            (self.kernel, "OpenProcess", HANDLE, [DWORD, BOOL, DWORD]),
            (self.kernel, "GetCurrentProcessId", DWORD, []),
            (self.kernel, "GetProcessId", DWORD, [HANDLE]),
            (self.kernel, "GetProcessTimes", BOOL, [HANDLE] + [C.POINTER(FILETIME)] * 4),
            (self.kernel, "GetPackageFamilyName", LONG, [HANDLE, C.POINTER(DWORD), C.c_wchar_p]),
            (self.kernel, "QueryFullProcessImageNameW", BOOL, [HANDLE, DWORD, C.c_wchar_p, C.POINTER(DWORD)]),
            (self.kernel, "WaitForSingleObject", DWORD, [HANDLE, DWORD]),
            (self.kernel, "CloseHandle", BOOL, [HANDLE]),
            (self.kernel, "CreateMutexW", HANDLE, [C.c_void_p, BOOL, C.c_wchar_p]),
            (self.kernel, "ReleaseMutex", BOOL, [HANDLE]),
            (self.security, "OpenProcessToken", BOOL, [HANDLE, DWORD, C.POINTER(HANDLE)]),
            (self.security, "GetTokenInformation", BOOL, [HANDLE, C.c_int, C.c_void_p, DWORD, C.POINTER(DWORD)]),
            (self.security, "EqualSid", BOOL, [C.c_void_p, C.c_void_p]),
        ]:
            function = getattr(library, name)
            function.restype, function.argtypes = result, args
            setattr(self, name, function)

    @staticmethod
    def error(operation):
        return OSError(C.get_last_error(), operation + " failed")


class WindowsBackend:
    """Own Windows handles; never request process termination permissions."""
    def __init__(self):
        self.api = _WindowsApi()
        self._closed = False
        process = self.open(int(self.api.GetCurrentProcessId()))
        token = None
        try:
            token = self._token(process)
            # TOKEN_USER's first field is a PSID into this retained buffer.
            self._current_user = self._token_information(token, 1)
            self.session_id = self._session(token)
            self._identity = {"pid": int(self.api.GetCurrentProcessId()),
                              "creation_filetime": self._creation(process), "session_id": self.session_id}
        finally:
            if token:
                self.api.CloseHandle(token)
            self.close_process(process)

    def own_identity(self):
        return dict(self._identity)

    def _token(self, process):
        token = HANDLE()
        if not self.api.OpenProcessToken(process, 0x0008, C.byref(token)):  # TOKEN_QUERY
            raise self.api.error("OpenProcessToken")
        return token.value

    def _token_information(self, token, kind):
        size = DWORD()
        self.api.GetTokenInformation(token, kind, None, 0, C.byref(size))
        if not 0 < size.value <= 1024 * 1024:
            raise self.api.error("GetTokenInformation sizing")
        data = C.create_string_buffer(size.value)
        if not self.api.GetTokenInformation(token, kind, data, size.value, C.byref(size)):
            raise self.api.error("GetTokenInformation")
        return data

    def _session(self, token):
        data = self._token_information(token, 12)  # TokenSessionId
        if len(data) < C.sizeof(DWORD):
            raise OSError("Token session response is incomplete")
        return int(C.cast(data, C.POINTER(DWORD)).contents.value)

    def _creation(self, process):
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not self.api.GetProcessTimes(process, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
            raise self.api.error("GetProcessTimes")
        return (int(created.high) << 32) | int(created.low)

    def snapshot(self):
        snapshot = self.api.CreateToolhelp32Snapshot(0x00000002, 0)  # process list only
        if snapshot == C.c_void_p(-1).value:
            raise self.api.error("CreateToolhelp32Snapshot")
        try:
            entry = PROCESSENTRY32W(); entry.dwSize = C.sizeof(entry)
            result = []
            more = self.api.Process32FirstW(snapshot, C.byref(entry))
            while more:
                result.append((int(entry.th32ProcessID), entry.szExeFile))
                more = self.api.Process32NextW(snapshot, C.byref(entry))
            if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES is the only normal end
                raise self.api.error("Process32 enumeration")
            return result
        finally:
            self.api.CloseHandle(snapshot)

    def open(self, pid):
        # QUERY_LIMITED_INFORMATION | SYNCHRONIZE, never PROCESS_TERMINATE.
        handle = self.api.OpenProcess(0x00101000, False, pid)
        if not handle:
            if C.get_last_error() in (87, 1168):
                raise ProcessGone("Candidate process has exited")
            raise self.api.error("OpenProcess")
        return handle

    def inspect(self, handle):
        size = DWORD(32768); path = C.create_unicode_buffer(size.value)
        if not self.api.QueryFullProcessImageNameW(handle, 0, path, C.byref(size)):
            raise self.api.error("QueryFullProcessImageNameW")
        record = {"pid": int(self.api.GetProcessId(handle)), "creation_filetime": self._creation(handle),
                  "basename": ntpath.basename(path.value), "package_family": None,
                  "same_user": False, "session_id": None}
        if record["basename"].casefold() != APP_BASENAME.casefold():
            return record
        size = DWORD()
        code = self.api.GetPackageFamilyName(handle, C.byref(size), None)
        if code == 15700:  # APPMODEL_ERROR_NO_PACKAGE: known unpackaged lookalike
            return record
        if code != 122 or not 0 < size.value <= 1024:
            raise OSError(code, "GetPackageFamilyName sizing failed")
        family = C.create_unicode_buffer(size.value)
        code = self.api.GetPackageFamilyName(handle, C.byref(size), family)
        if code:
            raise OSError(code, "GetPackageFamilyName failed")
        record["package_family"] = family.value
        if family.value.casefold() != PACKAGE_FAMILY.casefold():
            return record
        token = self._token(handle)
        try:
            candidate = self._token_information(token, 1)
            current_sid = C.cast(self._current_user, C.POINTER(C.c_void_p)).contents.value
            candidate_sid = C.cast(candidate, C.POINTER(C.c_void_p)).contents.value
            record["same_user"] = bool(self.api.EqualSid(current_sid, candidate_sid))
            record["session_id"] = self._session(token)
        finally:
            self.api.CloseHandle(token)
        return record

    def alive(self, handle):
        result = self.api.WaitForSingleObject(handle, 0)
        if result == WAIT_TIMEOUT:
            return True
        if result == WAIT_OBJECT_0:
            return False
        raise self.api.error("WaitForSingleObject")

    def close_process(self, handle):
        self.api.CloseHandle(handle)

    def close(self):
        self._closed = True
        self._current_user = None


class AppMonitor:
    """Retain verified live handles; detection failures are never app exits.

    Injectable backend: session_id, snapshot()->[(pid, basename)], open(pid),
    inspect(handle)->identity metadata, alive(handle)->bool, close_process(handle),
    and close(). Only the safe PID/creation pair leaves this class.
    """
    def __init__(self, backend=None):
        self.backend = backend if backend is not None else WindowsBackend()
        self._tracked = {}
        self._closed = False

    def _close_handle(self, handle):
        try:
            self.backend.close_process(handle)
        except Exception:
            pass

    def sample(self):
        if self._closed:
            raise RuntimeError("AppMonitor is closed")
        unknown = False
        for pid, entry in list(self._tracked.items()):
            try:
                alive = self.backend.alive(entry["handle"])
            except Exception:
                alive, unknown = False, True
            if not alive:
                self._close_handle(entry["handle"]); del self._tracked[pid]
        try:
            candidates = self.backend.snapshot()
            for pid, basename in candidates:
                if not isinstance(basename, str) or basename.casefold() != APP_BASENAME.casefold():
                    continue
                if pid in self._tracked:
                    continue
                handle = None
                try:
                    handle = self.backend.open(pid)
                    info = self.backend.inspect(handle)
                    if not self.backend.alive(handle):
                        continue
                    if (info.get("basename", "").casefold() != APP_BASENAME.casefold()
                            or (info.get("package_family") or "").casefold() != PACKAGE_FAMILY.casefold()
                            or info.get("same_user") is not True or info.get("session_id") != self.backend.session_id):
                        continue
                    if (type(info.get("pid")) is not int or info["pid"] != pid
                            or type(info.get("creation_filetime")) is not int or info["creation_filetime"] <= 0):
                        raise ValueError("Incomplete process identity")
                    self._tracked[pid] = {"handle": handle,
                        "identity": {"pid": pid, "creation_filetime": info["creation_filetime"]}}
                    handle = None  # retained until exit or close
                except ProcessGone:
                    pass
                except Exception:
                    try:
                        gone = handle is not None and not self.backend.alive(handle)
                    except Exception:
                        gone = False
                    if not gone:
                        unknown = True
                finally:
                    if handle is not None:
                        self._close_handle(handle)
        except Exception:
            unknown = True
        processes = [dict(self._tracked[pid]["identity"]) for pid in sorted(self._tracked)]
        return {"state": "present" if processes else "unknown" if unknown else "absent", "processes": processes}

    def close(self):
        if self._closed:
            return
        self._closed = True
        for entry in self._tracked.values():
            self._close_handle(entry["handle"])
        self._tracked.clear()
        self.backend.close()


def own_identity(backend=None):
    """Current process identity, keeping FILETIME as an exact 100ns integer."""
    source = backend if backend is not None else WindowsBackend()
    try:
        return source.own_identity()
    finally:
        source.close()


class SingleInstance:
    """A named Windows mutex; acquire and close on the same thread.

    Names/scoping are supplied by the caller. An abandoned mutex is recoverable;
    an existing mutex name alone does not establish that another owner is alive.
    """
    def __init__(self, name, backend=None):
        self.name = name
        self.api = backend if backend is not None else _WindowsApi()
        self.handle = None
        self._owner = None

    def acquire(self):
        if self.handle is not None:
            return self._owner == threading.get_ident()
        handle = self.api.CreateMutexW(None, False, self.name)
        if not handle:
            raise self.api.error("CreateMutexW")
        result = self.api.WaitForSingleObject(handle, 0)
        if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            self.handle, self._owner = handle, threading.get_ident()
            return True
        self.api.CloseHandle(handle)
        if result == WAIT_TIMEOUT:
            return False
        raise self.api.error("WaitForSingleObject mutex")

    def close(self):
        if self.handle is None:
            return
        if self._owner != threading.get_ident():
            raise RuntimeError("Release SingleInstance on its acquiring thread")
        handle = self.handle
        try:
            if not self.api.ReleaseMutex(handle):
                raise self.api.error("ReleaseMutex")
        finally:
            self.api.CloseHandle(handle)
            self.handle = self._owner = None
