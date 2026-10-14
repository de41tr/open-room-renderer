"""Small libasound adapter using the public PCM ABI.

This experimental adapter intentionally exposes ONLY null/file-on-null output.
It cannot select a default or physical device. Each handle has a local config;
no environment, global ALSA config, .asoundrc, service or mixer is modified.
"""
from __future__ import annotations

import ctypes as C
import errno
import fcntl
import os
import stat

from .playout import PcmFormat, PlayoutError


class AlsaError(PlayoutError):
    def __init__(self, operation: str, result: int):
        # Keep native diagnostics/paths out of ordinary reports.
        super().__init__(f"ALSA {operation} failed ({result})")
        self.result = result


class AlsaPcmSink:
    """Interleaved S16_LE/S32_LE; nonblocking writes, explicit reset on failure."""
    def __init__(self, format: PcmFormat, *, capture_fd: int | None = None):
        if not isinstance(format, PcmFormat):
            raise ValueError("PcmFormat required")
        if capture_fd is not None:
            if type(capture_fd) is not int or capture_fd < 0:
                raise ValueError("regular output file descriptor required")
            if not stat.S_ISREG(os.fstat(capture_fd).st_mode):
                raise ValueError("capture must be a regular file, never a pipe/device")
            if fcntl.fcntl(capture_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
                raise ValueError("capture file descriptor must be writable")
        self.format = format
        self._lib = C.CDLL("libasound.so.2")
        self._pcm = C.c_void_p()
        self._config = C.c_void_p()
        self._fd: int | None = None
        self._bind()
        input_handle = C.c_void_p()
        try:
            if capture_fd is not None:
                self._fd = os.dup(capture_fd)
                config = (f'pcm.orr_sink {{ type file slave.pcm {{ type null }} '
                          f'file {self._fd} format "raw" }}').encode("ascii")
            else:
                config = b'pcm.orr_sink { type null }'
            self._check("config creation", self._lib.snd_config_top(C.byref(self._config)))
            self._check("config input", self._lib.snd_input_buffer_open(
                C.byref(input_handle), config, len(config)))
            self._check("config load", self._lib.snd_config_load(self._config, input_handle))
            # Public ALSA enums: playback=0, NONBLOCK=1, RW_INTERLEAVED=3.
            self._check("open", self._lib.snd_pcm_open_lconf(
                C.byref(self._pcm), b"orr_sink", 0, 1, self._config))
            pcm_format = self._lib.snd_pcm_format_value(format.sample_format.encode("ascii"))
            self._check("format", pcm_format)
            self._check("parameters", self._lib.snd_pcm_set_params(
                self._pcm, pcm_format, 3, format.channels, format.rate, 0, 100_000))
        except BaseException:
            self.close()
            raise
        finally:
            if input_handle:
                self._lib.snd_input_close(input_handle)

    def _bind(self) -> None:
        ptr = C.c_void_p
        signatures = {
            "snd_config_top": ([C.POINTER(ptr)], C.c_int),
            "snd_input_buffer_open": ([C.POINTER(ptr), C.c_char_p, C.c_ssize_t], C.c_int),
            "snd_config_load": ([ptr, ptr], C.c_int),
            "snd_input_close": ([ptr], C.c_int),
            "snd_config_delete": ([ptr], C.c_int),
            "snd_pcm_open_lconf": ([C.POINTER(ptr), C.c_char_p, C.c_int, C.c_int, ptr], C.c_int),
            "snd_pcm_format_value": ([C.c_char_p], C.c_int),
            "snd_pcm_set_params": ([ptr, C.c_int, C.c_int, C.c_uint, C.c_uint, C.c_int, C.c_uint], C.c_int),
            "snd_pcm_writei": ([ptr, ptr, C.c_ulong], C.c_long),
            "snd_pcm_avail_delay": ([ptr, C.POINTER(C.c_long), C.POINTER(C.c_long)], C.c_int),
            "snd_pcm_drop": ([ptr], C.c_int),
            "snd_pcm_prepare": ([ptr], C.c_int),
            "snd_pcm_close": ([ptr], C.c_int),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self._lib, name)
            function.argtypes = args
            function.restype = result

    @staticmethod
    def _check(operation: str, result: int) -> int:
        if result < 0:
            raise AlsaError(operation, result)
        return result

    def _require_open(self) -> None:
        if not self._pcm:
            raise PlayoutError("ALSA sink is closed")

    def write(self, data: bytes) -> int:
        self._require_open()
        if type(data) is not bytes or not data or len(data) % self.format.frame_bytes:
            raise ValueError("whole interleaved PCM frames required")
        result = self._lib.snd_pcm_writei(self._pcm, C.c_char_p(data), len(data) // self.format.frame_bytes)
        if result in (-errno.EAGAIN, -errno.EINTR):
            return 0
        return self._check("write", result)

    def avail_delay(self) -> tuple[int, int]:
        self._require_open()
        available, delay = C.c_long(), C.c_long()
        self._check("avail/delay", self._lib.snd_pcm_avail_delay(
            self._pcm, C.byref(available), C.byref(delay)))
        return available.value, delay.value

    def drop_prepare(self) -> None:
        self._require_open()
        self._check("drop", self._lib.snd_pcm_drop(self._pcm))
        self._check("prepare", self._lib.snd_pcm_prepare(self._pcm))

    def close(self) -> None:
        error = 0
        if self._pcm:
            handle, self._pcm = self._pcm, C.c_void_p()
            # Deliberate immediate shutdown, not a drain/played-all guarantee.
            self._lib.snd_pcm_drop(handle)
            error = self._lib.snd_pcm_close(handle)
        if self._config:
            config, self._config = self._config, C.c_void_p()
            self._lib.snd_config_delete(config)
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)
        self._check("close", error)
