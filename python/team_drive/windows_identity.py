"""Windows process identity checks for a same-user local IPC service.

No impersonation, credential prompts or privilege elevation. A named-pipe
client must have the same user SID as the broker, even if it knows a token.
"""
import hashlib
import os

from .errors import DriveError


class _WindowsAPI:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows identity APIs are unavailable")
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.types = ctypes, wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
        self.kernel.GetNamedPipeClientProcessId.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        self.advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.advapi.OpenProcessToken.restype = wintypes.BOOL
        self.advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.advapi.GetTokenInformation.restype = wintypes.BOOL
        self.advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        self.advapi.ConvertSidToStringSidW.restype = wintypes.BOOL

    def current_process(self):
        return self.kernel.GetCurrentProcess()

    def client_pid(self, pipe):
        pid = self.types.ULONG()
        if not self.kernel.GetNamedPipeClientProcessId(pipe, self.ctypes.byref(pid)) or not pid.value:
            raise OSError("Cannot identify pipe client")
        return pid.value

    def open_process(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            raise OSError("Cannot inspect pipe client process")
        return handle

    def close_process(self, process):
        self.kernel.CloseHandle(process)

    def process_sid(self, process):
        ctypes, wintypes = self.ctypes, self.types
        token, sid = wintypes.HANDLE(), wintypes.LPWSTR()
        try:
            if not self.advapi.OpenProcessToken(process, 8, ctypes.byref(token)):  # TOKEN_QUERY
                raise OSError("Cannot inspect Windows token")
            size = wintypes.DWORD()
            self.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            if not 1 <= size.value <= 65536:
                raise OSError("Invalid Windows token information")
            data = ctypes.create_string_buffer(size.value)
            if not self.advapi.GetTokenInformation(token, 1, data, size.value, ctypes.byref(size)):
                raise OSError("Cannot read Windows token user")
            pointer = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
            if not self.advapi.ConvertSidToStringSidW(pointer, ctypes.byref(sid)) or not sid.value:
                raise OSError("Cannot identify Windows token user")
            return sid.value
        finally:
            if sid:
                self.kernel.LocalFree(sid)
            if token:
                self.kernel.CloseHandle(token)


def principal_binding():
    try:
        api = _WindowsAPI()
        return hashlib.sha256(api.process_sid(api.current_process()).encode("ascii")).hexdigest()
    except (OSError, AttributeError, ValueError, UnicodeError):
        raise DriveError("SHARE_IDENTITY_UNVERIFIED", "The current Windows account identity cannot be verified.") from None


def require_same_user_pipe(handle):
    """Reject a named-pipe client belonging to another Windows user, fail closed."""
    process = None
    api = None
    try:
        api = _WindowsAPI()
        pid = api.client_pid(handle)
        process = api.open_process(pid)
        client = api.process_sid(process)
        owner = api.process_sid(api.current_process())
        if api.client_pid(handle) != pid:
            raise OSError("Pipe client changed during authentication")
        if client != owner:
            raise DriveError("SERVICE_IDENTITY_MISMATCH", "This local index service accepts only the same Windows user as its owner.", False,
                             "Start a separate service and private index under your own Windows account.")
    except (OSError, AttributeError, ValueError):
        raise DriveError("SERVICE_IDENTITY_UNVERIFIED", "The named-pipe client's Windows identity could not be verified.", False,
                         "Reconnect using your own local Windows account; administrator or shared-user services are not supported.") from None
    finally:
        if api is not None and process is not None:
            api.close_process(process)
