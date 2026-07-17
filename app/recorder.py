"""麦克风录音:常驻音频流 + 预滚缓冲 + 帧数上限。

设计要点(针对蓝牙骨传导耳机):
- 常驻流:蓝牙耳机 A2DP→HFP 切换要 0.5~3 秒,每次按键才开流必丢话头;
- 预滚缓冲:始终保留最近 ~0.3s 音频,按键瞬间接到录音头部,不抢话;
- 帧数上限:超过 max_seconds 停止累积(截断而非丢弃),防止 release 丢失时内存无限增长;
- 回调里绝不 print(控制台 QuickEdit 模式会冻结输出线程,进而冻结音频回调);
- 流死亡(耳机休眠/断连)时自动刷新设备列表并重开。
"""
import sys
import threading
from collections import deque

import numpy as np
import sounddevice as sd


def list_input_devices():
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            out.append((i, d["name"], api, int(d["default_samplerate"])))
    return out


def refresh_devices():
    """PortAudio 设备表在初始化时冻结;重枚举需 terminate+initialize(此前必须已关闭所有流)。"""
    sd._terminate()
    sd._initialize()


def pick_device(name_contains: str):
    """返回 (device_index or None)。name_contains 为空则用系统默认。"""
    if name_contains:
        cands = []
        for i, name, api, sr in list_input_devices():
            if name_contains.lower() in name.lower():
                cands.append((i, name, api, sr))
        if not cands:
            raise RuntimeError(
                f"没有找到名称包含「{name_contains}」的输入设备。"
                f"请确认耳机已连接,或运行 python main.py --list-devices 查看设备列表。"
            )
        # Windows 上优先 WASAPI(延迟低、行为稳定)
        cands.sort(key=lambda c: 0 if "WASAPI" in c[2] else 1)
        i, name, api, sr = cands[0]
        print(f"[recorder] 使用设备 #{i} {name} ({api}, 默认{sr}Hz)")
        return i
    info = sd.query_devices(kind="input")
    print(f"[recorder] 使用系统默认输入: {info['name']} (默认{int(info['default_samplerate'])}Hz)")
    return None


def _resolve_samplerate(device):
    """优先让系统直接给 16k(WASAPI auto_convert),失败则用设备默认采样率。
    返回 (samplerate, extra_settings)。"""
    if sys.platform == "win32":
        try:
            extra = sd.WasapiSettings(auto_convert=True)
            sd.check_input_settings(
                device=device, samplerate=16000, channels=1, dtype="float32",
                extra_settings=extra,
            )
            return 16000, extra
        except Exception:
            pass
    try:
        sd.check_input_settings(device=device, samplerate=16000, channels=1, dtype="float32")
        return 16000, None
    except Exception:
        pass
    if device is not None:
        info = sd.query_devices(device)
    else:
        info = sd.query_devices(kind="input")
    return int(info["default_samplerate"]), None


class Recorder:
    def __init__(self, name_contains: str, max_seconds: float = 60.0, persistent: bool = True):
        self.name_contains = name_contains
        self.max_seconds = float(max_seconds)
        self.persistent = persistent
        self.sample_rate = 16000
        self.truncated = False
        self._lock = threading.Lock()
        self._stream = None
        self._active = False
        self._chunks = []
        self._frames = 0
        self._max_frames = 0
        self._preroll = deque()
        self._preroll_len = 0
        self._preroll_target = 0
        self._stream_error = False
        self._tap = None  # 采集期间把实时块推给流式识别的队列
        self.level = 0.0  # 最近一块的 RMS(悬浮条声波动画用)

    # ---- 流管理 ----

    def open(self):
        self._close()
        device = pick_device(self.name_contains)
        self.sample_rate, extra = _resolve_samplerate(device)
        self._max_frames = int(self.max_seconds * self.sample_rate)
        self._preroll_target = int(0.3 * self.sample_rate)
        print(f"[recorder] 以 {self.sample_rate}Hz 打开音频流"
              + ("(系统转换到16k)" if self.sample_rate == 16000 and extra else ""))

        def cb(indata, frames, time_info, status):
            with self._lock:
                if status:
                    self._stream_error = True  # 只置标志,绝不在回调里 print
                mono = indata[:, 0].copy()
                self.level = float((mono * mono).mean()) ** 0.5
                self._preroll.append(mono)
                self._preroll_len += len(mono)
                while self._preroll and self._preroll_len - len(self._preroll[0]) >= self._preroll_target:
                    self._preroll_len -= len(self._preroll.popleft())
                if self._active:
                    remain = self._max_frames - self._frames
                    if remain <= 0:
                        self.truncated = True
                        return
                    if len(mono) > remain:
                        mono = mono[:remain]
                        self.truncated = True
                    self._chunks.append(mono)
                    self._frames += len(mono)
                    if self._tap is not None:
                        try:
                            self._tap.put_nowait(mono)
                        except Exception:
                            pass  # 队列满就丢草稿块,不能阻塞音频回调

        kwargs = dict(device=device, channels=1, samplerate=self.sample_rate,
                      dtype="float32", callback=cb)
        if extra is not None:
            kwargs["extra_settings"] = extra
        self._stream = sd.InputStream(**kwargs)
        self._stream.start()
        self._stream_error = False

    def _close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None

    def _ensure_alive(self):
        dead = (
            self._stream is None
            or not getattr(self._stream, "active", False)
            or self._stream_error
        )
        if dead:
            if self._stream is not None:
                print("[recorder] 音频流已失效(耳机断连过?),刷新设备重连...")
            self._close()
            refresh_devices()
            self.open()

    # ---- 采集 ----

    def start_capture(self, tap=None):
        """tap: 可选 queue.Queue,采集期间实时收到每个音频块(供流式草稿识别)。"""
        self._ensure_alive()
        with self._lock:
            self._chunks = list(self._preroll)
            self._frames = sum(len(c) for c in self._chunks)
            self.truncated = False
            self._tap = tap
            if tap is not None:
                for c in self._chunks:  # 预滚部分也交给草稿
                    try:
                        tap.put_nowait(c)
                    except Exception:
                        pass
            self._active = True

    def stop_capture(self):
        """返回 (samples float32, sample_rate, truncated)。"""
        with self._lock:
            self._active = False
            self._tap = None
            chunks = self._chunks
            self._chunks = []
            truncated = self.truncated
        if not self.persistent:
            self._close()
        if not chunks:
            return np.zeros(0, dtype=np.float32), self.sample_rate, truncated
        return np.concatenate(chunks), self.sample_rate, truncated

    def close(self):
        self._close()
