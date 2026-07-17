"""SenseVoice 离线识别 + CT-Transformer 离线标点(全本地,无需联网)。"""
import os
import re
import numpy as np
import sherpa_onnx

SENSEVOICE_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
PUNCT_DIR = "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8"


class Recognizer:
    def __init__(self, models_dir, punctuation=True, num_threads=4, language="auto"):
        sv = os.path.join(models_dir, SENSEVOICE_DIR)
        if not os.path.isfile(os.path.join(sv, "model.int8.onnx")):
            raise FileNotFoundError(
                f"未找到 SenseVoice 模型: {sv}\n请先运行: python download_models.py"
            )
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(sv, "model.int8.onnx"),
            tokens=os.path.join(sv, "tokens.txt"),
            num_threads=num_threads,
            use_itn=True,
            language=language,  # auto: 中英混说时英文略好
        )
        self._punct = None
        if punctuation:
            pd = os.path.join(models_dir, PUNCT_DIR, "model.int8.onnx")
            if os.path.isfile(pd):
                cfg = sherpa_onnx.OfflinePunctuationConfig(
                    model=sherpa_onnx.OfflinePunctuationModelConfig(ct_transformer=pd)
                )
                self._punct = sherpa_onnx.OfflinePunctuation(cfg)
            else:
                print(f"[asr] 标点模型缺失,跳过标点: {pd}")

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        """samples: float32 单声道。返回带标点的文本。"""
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        if sample_rate != 16000:
            samples = resample_to_16k(samples, sample_rate)
        st = self._rec.create_stream()
        st.accept_waveform(16000, samples)
        self._rec.decode_stream(st)
        text = st.result.text.strip()
        # SenseVoice(use_itn=True)通常自带标点;外置标点模型只做无标点时的兜底
        if text and self._punct is not None and not re.search(r"[，。、？！；：]", text):
            text = self._punct.add_punctuation(text)
        return clean_punct(text)


def clean_punct(text: str) -> str:
    """去重相邻标点;去掉紧邻中文的空格,保留英文词间空格。"""
    # 　-〿 中文标点、㐀-鿿 汉字(含扩展A)、＀-￯ 全角形式
    cjk = r"　-〿㐀-鿿＀-￯"
    text = re.sub(rf"(?<=[{cjk}])\s+|\s+(?=[{cjk}])", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([，。、？！;：,.!?])(?:[，。、？！;：,.!?])+", r"\1", text)
    return text


def resample_to_16k(samples: np.ndarray, sr: int) -> np.ndarray:
    """重采样到16k。降采样先做抗混叠低通(8kHz以上的键盘声/齿擦音高频
    折叠进语音频段会直接污染 s/sh 区分线索)。"""
    if sr == 16000 or len(samples) == 0:
        return samples.astype(np.float32)
    if sr > 16000:
        samples = _antialias_lowpass(samples, sr)
    n_out = int(round(len(samples) * 16000.0 / sr))
    x_old = np.arange(len(samples), dtype=np.float64) / sr
    x_new = np.arange(n_out, dtype=np.float64) / 16000.0
    return np.interp(x_new, x_old, samples).astype(np.float32)


def _antialias_lowpass(x: np.ndarray, sr: int, cutoff: float = 7200.0, taps: int = 101) -> np.ndarray:
    """窗函数 sinc FIR 低通(线性相位,101阶@48k 约2ms群延迟)。"""
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2.0 * cutoff / sr * np.sinc(2.0 * cutoff / sr * n) * np.hamming(taps)
    h /= h.sum()
    return np.convolve(x.astype(np.float64), h, mode="same")
