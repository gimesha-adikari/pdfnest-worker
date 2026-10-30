"""Linux pidfd bindings for Python builds without os.pidfd_open wrappers.

Absent libc/kernel support fails closed; numeric PID signaling is never used
as a fallback for an authority/owner relationship.
"""
import ctypes
import os
import select


def _call(name, *args):
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, name, None)
    if function is None:
        raise OSError(38, 'PIDFD_UNAVAILABLE')
    result = function(*args)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def pidfd_open(pid):
    return _call('pidfd_open', ctypes.c_int(pid), ctypes.c_uint(0))


def pidfd_signal(fd, signal):
    _call('pidfd_send_signal', ctypes.c_int(fd), ctypes.c_int(signal),
          ctypes.c_void_p(), ctypes.c_uint(0))


def require_child(fd):
    # Linux P_PIDFD=3. waitid checks that this stable process handle identifies
    # one of the monitor's children; WNOWAIT preserves the eventual waitpid.
    information = ctypes.create_string_buffer(128)
    _call('waitid', ctypes.c_int(3), ctypes.c_uint(fd), ctypes.byref(information),
          ctypes.c_int(os.WEXITED | os.WNOHANG | os.WNOWAIT))


def alive(fd):
    return not select.select([fd], [], [], 0)[0]
