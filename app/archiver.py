"""数据飞轮:本地保存每次听写的音频与文本,供日后热词挖掘和模型微调。

隐私:全部数据只存在本机 data/ 目录,绝不上传。config.json 里可随时关闭。
"""
import json
import os
import time
import wave

import numpy as np


class Archiver:
    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.save_audio = bool(cfg.get("save_audio", True))
        self.dir = cfg.get("dir", "data")
        self.max_total_mb = float(cfg.get("max_total_mb", 2048))
        self._size_bytes = 0
        self._cap_warned = False
        if self.enabled:
            os.makedirs(self.dir, exist_ok=True)
            self._size_bytes = sum(
                e.stat().st_size for e in os.scandir(self.dir) if e.is_file()
            )

    def save(self, samples_16k: np.ndarray, raw_text: str, corrected_text: str):
        if not self.enabled:
            return
        if self.save_audio and self._size_bytes > self.max_total_mb * 1048576:
            if not self._cap_warned:
                self._cap_warned = True
                print(f"[archiver] data 目录已达 {self.max_total_mb:.0f}MB 上限,"
                      f"之后只记文本不存音频(可在 config.json 调大 max_total_mb)")
            self.save_audio = False
        # 毫秒后缀防同秒覆盖
        ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000:03d}"
        wav_name = ""
        if self.save_audio and len(samples_16k):
            wav_name = f"{ts}.wav"
            path = os.path.join(self.dir, wav_name)
            pcm = np.clip(samples_16k * 32767.0, -32768, 32767).astype(np.int16)
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm.tobytes())
            self._size_bytes += os.path.getsize(path)
        rec = {
            "ts": ts,
            "wav": wav_name,
            "duration_s": round(len(samples_16k) / 16000.0, 2),
            "raw": raw_text,
            "corrected": corrected_text,
        }
        with open(os.path.join(self.dir, "records.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
