"""声音记忆库:把你和家人每天说过的话——原始音质音频 + 文字 + 声纹——按人永久归档,
为"有一天想留住对方的声音和说话的样子"备一份底稿。

与数据飞轮(archiver.py)的区别:
  - 飞轮 data/ 是滚动缓存,攒满 2GB 就只记文本、丢音频,服务于"越用越准"。
  - 记忆库 memory/<人>/ 是**永久**归档,不删旧、存**原始音质**,服务于未来的声音克隆
    与"说话风格"语言模型。两者互不影响。

隐私:全部只存本机,绝不上传。每台设备只记本机主人(speaker_name)。可随时关闭、随时删除。
声纹用 sherpa-onnx 的 CAM++ 说话人嵌入(192 维),既是身份锚点,也是参考素材本身。
"""
import csv
import json
import os
import time
import wave

import numpy as np


def _safe(name: str) -> str:
    """人名 → 安全目录名。"""
    name = (name or "我").strip() or "我"
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in name)
    return out[:40] or "我"


def _write_wav(path: str, samples: np.ndarray, sr: int):
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


class VoiceprintExtractor:
    """CAM++ 声纹提取器的薄封装;模型缺失时优雅降级(archive 仍可用,只是没声纹)。"""

    def __init__(self, model_path: str, num_threads: int = 1):
        self.ok = False
        self._ex = None
        if not model_path or not os.path.exists(model_path):
            return
        try:
            import sherpa_onnx as so
            cfg = so.SpeakerEmbeddingExtractorConfig(model=model_path, num_threads=num_threads)
            self._ex = so.SpeakerEmbeddingExtractor(cfg)
            self.dim = self._ex.dim
            self.ok = True
        except Exception as e:
            print(f"[memory] 声纹模型加载失败(不影响存档): {e}")

    def embed(self, samples_16k: np.ndarray) -> "np.ndarray | None":
        if not self.ok or not len(samples_16k):
            return None
        try:
            st = self._ex.create_stream()
            st.accept_waveform(16000, samples_16k.astype(np.float32))
            st.input_finished()
            return np.array(self._ex.compute(st), dtype=np.float32)
        except Exception as e:
            print(f"[memory] 声纹计算失败: {e}")
            return None


def cosine(a, b) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class MemoryBank:
    """按人归档音频+文字+声纹。每台设备一个主人(speaker_name)。"""

    def __init__(self, cfg: dict, base_dir: str, model_path: str = ""):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.speaker = _safe(cfg.get("speaker_name", "我"))
        rel = cfg.get("dir", "memory")
        self.root = rel if os.path.isabs(os.path.expanduser(rel)) else os.path.join(base_dir, rel)
        self.root = os.path.expanduser(self.root)
        self._ext = None
        self._model_path = model_path
        self._profile = None  # 懒加载
        if self.enabled:
            os.makedirs(self._person_dir(), exist_ok=True)
            os.makedirs(os.path.join(self._person_dir(), "audio"), exist_ok=True)

    # -- 路径 --
    def _person_dir(self, speaker=None):
        return os.path.join(self.root, _safe(speaker or self.speaker))

    def _manifest(self, speaker=None):
        return os.path.join(self._person_dir(speaker), "utterances.jsonl")

    def _profile_path(self, speaker=None):
        return os.path.join(self._person_dir(speaker), "voiceprint.json")

    # -- 声纹提取器懒加载 --
    def _extractor(self):
        if self._ext is None:
            self._ext = VoiceprintExtractor(self._model_path)
        return self._ext

    # -- 声纹档案(累计均值) --
    def _load_profile(self, speaker=None):
        p = self._profile_path(speaker)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"speaker": _safe(speaker or self.speaker), "count": 0, "mean": None, "updated": ""}

    def _update_profile(self, emb: np.ndarray, speaker=None):
        prof = self._load_profile(speaker)
        n = int(prof.get("count", 0))
        mean = prof.get("mean")
        if mean is None or n == 0:
            new_mean = emb.astype(np.float64)
        else:
            new_mean = (np.asarray(mean, dtype=np.float64) * n + emb) / (n + 1)
        prof["count"] = n + 1
        prof["mean"] = [round(float(x), 6) for x in new_mean]
        prof["dim"] = len(new_mean)
        prof["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._profile_path(speaker), "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False)

    # -- 存一句 --
    def save(self, samples_orig: np.ndarray, sr_orig: int,
             samples_16k: np.ndarray, raw: str, corrected: str):
        """存一句话:原始音质音频 + 文字 + 声纹。archiver 之后调用,失败不影响主流程。"""
        if not self.enabled:
            return
        try:
            pdir = self._person_dir()
            os.makedirs(os.path.join(pdir, "audio"), exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
            wav_name = f"{ts}.wav"
            wav_path = os.path.join(pdir, "audio", wav_name)
            # 存原始音质(至少 16k);原始更适合未来克隆
            if samples_orig is not None and len(samples_orig) and sr_orig >= 16000:
                _write_wav(wav_path, samples_orig, sr_orig)
                save_sr = int(sr_orig)
            else:
                _write_wav(wav_path, samples_16k, 16000)
                save_sr = 16000

            emb = self._extractor().embed(samples_16k)
            selfsim = None
            if emb is not None:
                prof = self._load_profile()
                if prof.get("mean") is not None:
                    selfsim = round(cosine(emb, prof["mean"]), 3)
                self._update_profile(emb)

            rec = {
                "ts": ts,
                "wav": os.path.join("audio", wav_name),
                "sr": save_sr,
                "duration_s": round(len(samples_16k) / 16000.0, 2),
                "raw": raw,
                "text": corrected,
                "selfsim": selfsim,  # 与本人声纹的相似度,低说明可能不是本人在说
            }
            if emb is not None:
                rec["emb"] = [round(float(x), 4) for x in emb]
            with open(self._manifest(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[memory] 记忆库存档失败(不影响听写): {e}")

    # -- 统计 --
    def stats(self, speaker=None):
        speaker = _safe(speaker or self.speaker)
        pdir = self._person_dir(speaker)
        man = self._manifest(speaker)
        count = 0
        seconds = 0.0
        chars = 0
        if os.path.exists(man):
            with open(man, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    count += 1
                    seconds += float(r.get("duration_s", 0) or 0)
                    chars += len(r.get("text", "") or "")
        size_mb = 0.0
        adir = os.path.join(pdir, "audio")
        if os.path.isdir(adir):
            size_mb = sum(e.stat().st_size for e in os.scandir(adir) if e.is_file()) / 1048576.0
        prof = self._load_profile(speaker)
        return {
            "speaker": speaker,
            "count": count,
            "minutes": round(seconds / 60.0, 1),
            "chars": chars,
            "size_mb": round(size_mb, 1),
            "voiceprint_count": int(prof.get("count", 0)),
            "voiceprint_ready": prof.get("mean") is not None,
        }

    def list_speakers(self):
        if not os.path.isdir(self.root):
            return []
        out = []
        for e in os.scandir(self.root):
            if e.is_dir() and os.path.exists(os.path.join(e.path, "utterances.jsonl")):
                out.append(e.name)
        return sorted(out)

    # -- 谁在说(多设备/多人共用时用) --
    def identify(self, samples_16k: np.ndarray):
        """返回 (最匹配的人, 相似度)。用于多人共用一台设备时自动判断是谁在说。"""
        emb = self._extractor().embed(samples_16k)
        if emb is None:
            return (None, 0.0)
        best, best_s = None, -1.0
        for sp in self.list_speakers():
            prof = self._load_profile(sp)
            if prof.get("mean") is None:
                continue
            s = cosine(emb, prof["mean"])
            if s > best_s:
                best, best_s = sp, s
        return (best, round(best_s, 3))

    # -- 备份(NAS/移动盘)--

    def disk_usage_mb(self):
        """整个记忆库目录的磁盘占用(MB)。"""
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
        return total / 1048576.0

    def backup_state(self):
        p = os.path.join(self.root, "backup_state.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def backup(self, dest_root, free_audio=False):
        """增量备份整个记忆库到 dest_root/听晓声音记忆库备份/。
        只复制目标缺失或大小不同的文件(jsonl 追加后会重拷,保持最新)。
        free_audio=True 时,复制并逐一校验(存在+大小一致)后删除本地 wav,
        释放硬盘;文字清单(utterances.jsonl)与声纹(voiceprint.json)永远保留在本地。
        返回 dict(ok, copied, freed_mb, total_mb, dest, err)。"""
        import shutil
        dest = os.path.join(os.path.expanduser(dest_root), "听晓声音记忆库备份")
        copied = 0
        freed = 0.0
        total = 0.0
        try:
            os.makedirs(dest, exist_ok=True)
            probe = os.path.join(dest, ".write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except Exception as e:
            return {"ok": False, "err": f"备份目标不可写:{e}", "dest": dest,
                    "copied": 0, "freed_mb": 0.0, "total_mb": 0.0}
        try:
            for dirpath, _dirs, files in os.walk(self.root):
                for fn in files:
                    if fn == "backup_state.json":
                        continue
                    src = os.path.join(dirpath, fn)
                    rel = os.path.relpath(src, self.root)
                    dst = os.path.join(dest, rel)
                    try:
                        ssize = os.path.getsize(src)
                    except OSError:
                        continue
                    total += ssize
                    need = (not os.path.exists(dst)
                            or os.path.getsize(dst) != ssize)
                    if need:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)
                        copied += 1
                    # 释放:仅音频,且必须校验目标副本完好
                    if (free_audio and fn.lower().endswith(".wav")
                            and os.path.exists(dst)
                            and os.path.getsize(dst) == ssize):
                        try:
                            os.remove(src)
                            freed += ssize
                        except OSError:
                            pass
            state = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "dest": dest,
                     "copied": copied, "freed_mb": round(freed / 1048576.0, 1)}
            with open(os.path.join(self.root, "backup_state.json"), "w",
                      encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            return {"ok": True, "copied": copied,
                    "freed_mb": round(freed / 1048576.0, 1),
                    "total_mb": round(total / 1048576.0, 1),
                    "dest": dest, "err": ""}
        except Exception as e:
            return {"ok": False, "err": str(e), "dest": dest, "copied": copied,
                    "freed_mb": round(freed / 1048576.0, 1),
                    "total_mb": round(total / 1048576.0, 1)}

    # -- 导出「声音记忆包」--
    def export(self, speaker, out_dir):
        """把某人的音频+文字清单+声纹打包到 out_dir,直接可喂给未来的声音克隆/风格模型。
        返回 (成功, 消息, 包目录)。"""
        speaker = _safe(speaker)
        pdir = self._person_dir(speaker)
        man = self._manifest(speaker)
        if not os.path.exists(man):
            return (False, f"还没有 {speaker} 的记忆数据", "")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        pack = os.path.join(out_dir, f"{speaker}_声音记忆包_{stamp}")
        wav_out = os.path.join(pack, "wav")
        os.makedirs(wav_out, exist_ok=True)

        rows = []
        transcript_lines = []
        with open(man, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                src = os.path.join(pdir, r.get("wav", ""))
                base = os.path.basename(r.get("wav", ""))
                text = (r.get("text") or "").strip()
                if base and os.path.exists(src):
                    try:
                        import shutil
                        shutil.copy2(src, os.path.join(wav_out, base))
                    except Exception:
                        continue
                    rows.append((os.path.join("wav", base), text, r.get("duration_s", "")))
                if text:
                    transcript_lines.append(text)

        # metadata.csv:声音克隆管线通用的 (音频路径, 文字) 清单
        with open(os.path.join(pack, "metadata.csv"), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter="|")
            for path, text, dur in rows:
                w.writerow([path, text])
        # 纯文字语料:训练"说话风格"语言模型用
        with open(os.path.join(pack, "语料_全部说过的话.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(transcript_lines))
        # 声纹
        prof = self._load_profile(speaker)
        with open(os.path.join(pack, "voiceprint.json"), "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False, indent=2)
        # 说明
        total_sec = sum(float(d or 0) for _, _, d in rows)
        with open(os.path.join(pack, "README.txt"), "w", encoding="utf-8") as f:
            f.write(
                f"{speaker} 的声音记忆包\n"
                f"导出时间:{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"共 {len(rows)} 句、约 {total_sec/60:.1f} 分钟音频。\n\n"
                "目录说明:\n"
                "  wav/                  每句话的原始音质录音\n"
                "  metadata.csv          音频路径|文字(GPT-SoVITS / CosyVoice 等克隆管线通用格式)\n"
                "  语料_全部说过的话.txt   纯文字语料,可训练模仿说话风格的语言模型\n"
                "  voiceprint.json       192 维声纹(身份锚点)\n\n"
                "这份数据只属于本人,请妥善保管。声音克隆前请确保本人知情同意。\n"
            )
        return (True, f"已导出 {len(rows)} 句 / {total_sec/60:.1f} 分钟", pack)
