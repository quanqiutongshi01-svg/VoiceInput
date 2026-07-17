"""提示音播放:合成的柔和音色(sounds/*.wav),异步播放不阻塞。
Windows 用 winsound(系统自带);macOS 开发环境用 afplay;文件缺失退回蜂鸣。"""
import os
import subprocess
import sys
import threading

def _sounds_dir():
    # 冻结成 .app 时用 cwd(app 已 chdir 到数据目录并播种了 sounds);否则用本文件旁
    for d in (os.path.join(os.getcwd(), "sounds"),
              os.path.join(getattr(sys, "_MEIPASS", ""), "sounds"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")):
        if d and os.path.isdir(d):
            return d
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")


BASE = _sounds_dir()
_FILES = {k: os.path.join(BASE, f"{k}.wav") for k in ("start", "stop", "error")}
_FALLBACK_FREQ = {"start": 880, "stop": 440, "error": 220}


def play(kind: str, enabled: bool = True):
    if not enabled:
        return
    path = _FILES.get(kind)
    if path and os.path.isfile(path):
        try:
            if sys.platform == "win32":
                import winsound

                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
            if sys.platform == "darwin":
                subprocess.Popen(["afplay", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except Exception:
            pass
    # 兜底:老式蜂鸣
    freq = _FALLBACK_FREQ.get(kind, 440)
    if sys.platform == "win32":
        import winsound

        threading.Thread(target=winsound.Beep, args=(freq, 120), daemon=True).start()
    else:
        print("\a", end="", flush=True)
