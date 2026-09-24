from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path


def replace_file(source: Path, target: Path) -> None:
    if os.name != 'nt':
        os.replace(source, target)
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    rename.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    class RenameInfo(ctypes.Structure):
        _fields_ = [('flags', wintypes.DWORD), ('root', wintypes.HANDLE),
                    ('length', wintypes.DWORD), ('name', wintypes.WCHAR * 1)]

    encoded = str(target.absolute()).encode('utf-16-le')
    buffer = ctypes.create_string_buffer(RenameInfo.name.offset + len(encoded) + 2)
    info = RenameInfo.from_buffer(buffer)
    file_rename_replace_if_exists = 0x1
    file_rename_posix_semantics = 0x2
    info.flags = file_rename_replace_if_exists | file_rename_posix_semantics
    info.length = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.name.offset, encoded, len(encoded))
    handle = create(str(source), 0x00010000, 0x7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not rename(handle, 22, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)
