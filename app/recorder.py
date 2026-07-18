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


def _av_buffer_to_array(buffer, channels):
    """Copy selected AVAudioPCMBuffer channels before Core Audio reuses it."""
    frames = int(buffer.frameLength())
    available = int(buffer.format().channelCount())
    channel_data = buffer.floatChannelData()
    if channel_data is None or frames <= 0 or available <= 0:
        return np.empty((0, max(1, channels)), dtype=np.float32)
    count = min(max(1, channels), available)
    result = np.empty((frames, count), dtype=np.float32)
    for channel in range(count):
        raw = channel_data[channel].as_buffer(frames * np.dtype(np.float32).itemsize)
        result[:, channel] = np.frombuffer(raw, dtype=np.float32, count=frames)
    return result


class _MacNativeInputStream:
    """InputStream-compatible AVAudioEngine wrapper for the current macOS input."""

    def __init__(self, samplerate, channels, callback):
        from AVFoundation import AVAudioEngine

        self._engine = AVAudioEngine.alloc().init()
        self._node = self._engine.inputNode()
        self._callback = callback
        self._installed = False
        self._active = False
        self._tap_error = None
        audio_format = self._node.outputFormatForBus_(0)
        self.samplerate = int(audio_format.sampleRate())
        available_channels = int(audio_format.channelCount())
        if self.samplerate <= 0 or available_channels <= 0:
            raise RuntimeError(
                f"AVAudioEngine 没有可用输入格式: {self.samplerate}Hz/{available_channels}ch")
        self.channels = min(max(1, int(channels)), available_channels)

        def tap(buffer, _when):
            try:
                data = _av_buffer_to_array(buffer, self.channels)
                if len(data):
                    self._callback(data, len(data), None, None)
            except Exception as exc:
                self._tap_error = exc
                self._active = False

        self._tap = tap

    @property
    def active(self):
        return self._active and self._tap_error is None and bool(self._engine.isRunning())

    def start(self):
        self._node.installTapOnBus_bufferSize_format_block_(0, 1024, None, self._tap)
        self._installed = True
        self._engine.prepare()
        ok, error = self._engine.startAndReturnError_(None)
        if not ok:
            self.close()
            raise RuntimeError(f"AVAudioEngine 无法启动: {error}")
        self._active = True

    def stop(self):
        if self._active or self._engine.isRunning():
            self._engine.stop()
        self._active = False

    def close(self):
        self.stop()
        if self._installed:
            self._node.removeTapOnBus_(0)
            self._installed = False


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


# Windows 音频接口优先级:WASAPI 最稳,WDM-KS 最容易开不动(蓝牙耳机常炸)
_API_RANK = ["WASAPI", "DirectSound", "MME", "WDM-KS", "ASIO"]


def _api_rank(api: str) -> int:
    for i, k in enumerate(_API_RANK):
        if k.lower() in api.lower():
            return i
    return len(_API_RANK)


# 虚拟/聚集/回环设备:兜底时跳过它们(会采到静音或回环,不是真麦克风)
_VIRTUAL_HINTS = ("blackhole", "aggregate", "virtual", "surround", "soundflower",
                  "loopback", "cable", "vb-audio", "多输出", "聚集设备")


def _is_virtual(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _VIRTUAL_HINTS)


def pick_devices(name_contains: str):
    """返回按音频接口稳定性排序的候选 [(index, name, api, sr)],供逐个尝试。
    name_contains 为空=自动:系统默认输入优先,其它真实麦克风兜底(首选开不动时自动降级)。
    name_contains 指定=严格:只试匹配的设备,开不动就报错(让用户知道该修那只麦)。"""
    devs = list_input_devices()
    if name_contains:
        cands = [d for d in devs if name_contains.lower() in d[1].lower()]
        if not cands:
            raise RuntimeError(
                f"没有找到名称包含「{name_contains}」的输入设备。"
                f"请确认耳机已连接,或在设置里重选麦克风。")
        return sorted(cands, key=lambda d: (_api_rank(d[2]), d[0]))
    try:
        default_name = sd.query_devices(kind="input")["name"]
    except Exception:
        default_name = ""
    # 默认设备的核心名(去掉接口后缀噪声),匹配它的所有接口实例——优先
    key = default_name.split("(")[0].strip()[:12]
    primary = sorted([d for d in devs if key and key in d[1]],
                     key=lambda d: (_api_rank(d[2]), d[0]))
    chosen = {d[0] for d in primary}
    # 兜底:其它真实麦克风(排除虚拟/聚集设备),默认麦打不开时自动切过去
    others = sorted([d for d in devs if d[0] not in chosen and not _is_virtual(d[1])],
                    key=lambda d: (_api_rank(d[2]), d[0]))
    return (primary + others) or devs


def _resolve_samplerate(device, api, channels=1):
    """macOS 使用设备原生采样率,其他平台优先请求 16k。"""
    try:
        info = sd.query_devices(device) if device is not None else sd.query_devices(kind="input")
        native_rate = int(info["default_samplerate"])
    except Exception:
        native_rate = 48000
    if sys.platform == "darwin":
        sd.check_input_settings(
            device=device, samplerate=native_rate, channels=channels, dtype="float32")
        return native_rate, None

    is_wasapi = sys.platform == "win32" and "wasapi" in (api or "").lower()
    if is_wasapi:
        try:
            extra = sd.WasapiSettings(auto_convert=True)
            sd.check_input_settings(
                device=device, samplerate=16000, channels=1, dtype="float32",
                extra_settings=extra)
            return 16000, extra
        except Exception:
            pass
    try:
        sd.check_input_settings(device=device, samplerate=16000, channels=1, dtype="float32")
        return 16000, None
    except Exception:
        pass
    try:
        return native_rate, None
    except Exception:
        return 48000, None


def _capture_channels(device):
    """DJI 接收器在 macOS 下是双声道;同时采集可兼容 TX1/TX2 路由。"""
    if sys.platform != "darwin":
        return 1
    try:
        info = sd.query_devices(device) if device is not None else sd.query_devices(kind="input")
        return max(1, min(2, int(info["max_input_channels"])))
    except Exception:
        return 1


def _mix_to_mono(indata):
    """等功率合并输入声道,单个发射器不会因双声道采集衰减 6dB。"""
    if indata.ndim == 1:
        return indata.astype(np.float32, copy=True)
    channels = indata.shape[1]
    if channels <= 1:
        return indata[:, 0].astype(np.float32, copy=True)
    mixed = np.sum(indata, axis=1, dtype=np.float32) / np.sqrt(float(channels))
    return np.clip(mixed, -1.0, 1.0).astype(np.float32, copy=False)


class Recorder:
    def __init__(self, name_contains: str, max_seconds: float = 60.0, persistent: bool = True):
        self.name_contains = name_contains
        self.max_seconds = float(max_seconds)
        self.persistent = persistent
        self.sample_rate = 16000
        self.capture_channels = 1
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
        cands = pick_devices(self.name_contains)
        last_err = None
        for idx, (device, name, api, _sr) in enumerate(cands):
            try:
                self._open_on(device, api)
                print(
                    f"[recorder] 使用设备 #{device} {name} ({api}) "
                    f"@ {self.sample_rate}Hz/{self.capture_channels}ch")
                return
            except Exception as e:
                last_err = e
                print(f"[recorder] {name} ({api}) 打开失败,尝试下一个接口: {e}")
                self._close()
        raise RuntimeError(f"所有音频接口都打不开麦克风。最后错误: {last_err}")

    def _open_on(self, device, api):
        self.capture_channels = _capture_channels(device)
        self.sample_rate, extra = _resolve_samplerate(device, api, self.capture_channels)

        def cb(indata, frames, time_info, status):
            with self._lock:
                if status:
                    self._stream_error = True  # 只置标志,绝不在回调里 print
                mono = _mix_to_mono(indata)
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

        if sys.platform == "darwin":
            default_device = sd.default.device[0]
            if int(default_device) != int(device):
                raise RuntimeError("macOS 原生录音要求所选麦克风同时是系统默认输入设备")
            self._stream = _MacNativeInputStream(
                samplerate=self.sample_rate,
                channels=self.capture_channels,
                callback=cb,
            )
            self.sample_rate = self._stream.samplerate
            self.capture_channels = self._stream.channels
        else:
            kwargs = dict(device=device, channels=self.capture_channels, samplerate=self.sample_rate,
                          dtype="float32", callback=cb)
            if extra is not None:
                kwargs["extra_settings"] = extra
            self._stream = sd.InputStream(**kwargs)
        self._max_frames = int(self.max_seconds * self.sample_rate)
        self._preroll_target = int(0.3 * self.sample_rate)
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
