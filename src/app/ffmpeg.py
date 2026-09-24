"""可选的 ffmpeg 引擎。

**核心功能不依赖它。** 无损抽取由 ``container.py`` 用标准库完成；ffmpeg 只是
「检测到就用」的增强，用来提供转码预设（MP3 / FLAC / WAV / M4A / OPUS）。

安全上刻意做窄（指引 38.3）：

* 只用 ``subprocess`` 的**参数列表**形式调用，绝不拼 shell 字符串 —— 没有
  ``shell=True``，也就没有命令注入面；
* 命令的映射是**固定的**：预设 → 一组写死的参数，API 不能传任意 ffmpeg 参数；
* 不下载、不安装 ffmpeg —— 只调用系统上已有的，没有就明确显示「不可用」。
"""

import os
import shutil
import subprocess
import threading

#: 预设 → ffmpeg 参数（写死，不接受外部传入）
PRESETS = {
    "mp3_128": {"label": "MP3 128k", "ext": ".mp3", "codec": "MP3 128 kbps",
                "args": ["-vn", "-c:a", "libmp3lame", "-b:a", "128k"]},
    "mp3_192": {"label": "MP3 192k", "ext": ".mp3", "codec": "MP3 192 kbps",
                "args": ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]},
    "mp3_320": {"label": "MP3 320k", "ext": ".mp3", "codec": "MP3 320 kbps",
                "args": ["-vn", "-c:a", "libmp3lame", "-b:a", "320k"]},
    "flac": {"label": "FLAC（无损）", "ext": ".flac", "codec": "FLAC",
             "args": ["-vn", "-c:a", "flac"]},
    "wav": {"label": "WAV（未压缩）", "ext": ".wav", "codec": "PCM 16-bit",
            "args": ["-vn", "-c:a", "pcm_s16le"]},
    "m4a": {"label": "M4A / AAC 256k", "ext": ".m4a", "codec": "AAC 256 kbps",
            "args": ["-vn", "-c:a", "aac", "-b:a", "256k"]},
    "opus": {"label": "OPUS 128k", "ext": ".opus", "codec": "Opus 128 kbps",
             "args": ["-vn", "-c:a", "libopus", "-b:a", "128k"]},
}

_state = {"checked": False, "available": False, "path": None, "version": "", "error": None}
_lock = threading.Lock()


def detect(force=False):
    """检测系统上是否有可用的 ffmpeg。结果会缓存。"""
    with _lock:
        if _state["checked"] and not force:
            return dict(_state)
        _state["checked"] = True
        path = shutil.which("ffmpeg")
        if not path:
            _state.update(available=False, path=None, version="",
                          error="系统上没有找到 ffmpeg 命令")
            return dict(_state)
        try:
            completed = subprocess.run(
                [path, "-version"], capture_output=True, timeout=10, check=False
            )
            first_line = (completed.stdout or b"").decode("utf-8", "replace").splitlines()
            _state.update(available=True, path=path,
                          version=first_line[0] if first_line else "", error=None)
        except (OSError, subprocess.SubprocessError) as exc:
            _state.update(available=False, path=path, version="",
                          error="调用 ffmpeg 失败：%s" % exc)
        return dict(_state)


def status():
    """给前端用的引擎状态。"""
    info = detect()
    return {
        "name": "ffmpeg",
        "available": info["available"],
        "version": info["version"],
        "detail": ("已检测到：%s" % info["version"]) if info["available"] else info["error"],
        "enables": ["transcode"],
    }


def preset_list():
    return [{"id": key, "label": value["label"], "ext": value["ext"],
             "codec": value["codec"]} for key, value in PRESETS.items()]


class TranscodeError(Exception):
    pass


def transcode(source, target, preset_id, timeout=3600):
    """按预设转码。参数全部来自写死的表，调用方只能选预设 ID。"""
    preset = PRESETS.get(preset_id)
    if preset is None:
        raise TranscodeError("未知的转码预设：%s" % preset_id)
    info = detect()
    if not info["available"]:
        raise TranscodeError(info["error"] or "ffmpeg 不可用")

    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)

    argv = [info["path"], "-hide_banner", "-nostdin", "-y",
            "-i", source] + list(preset["args"]) + [target]
    try:
        completed = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise TranscodeError("转码超时（超过 %d 秒）" % timeout)
    except OSError as exc:
        raise TranscodeError("无法启动 ffmpeg：%s" % exc)

    if completed.returncode != 0:
        # 只回最后几行 stderr，避免把整段输出丢给前端
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = " / ".join(detail[-3:]) if detail else "无输出"
        raise TranscodeError("ffmpeg 退出码 %d：%s" % (completed.returncode, tail))
    if not os.path.exists(target):
        raise TranscodeError("ffmpeg 报告成功，但没有生成输出文件")
    return {"output": target, "bytes": os.path.getsize(target), "preset": preset_id}
