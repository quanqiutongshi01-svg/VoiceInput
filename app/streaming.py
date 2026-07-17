"""流式"草稿"识别:边说边出的实时文字(最终定稿仍由 SenseVoice 全段识别)。

支持两种流式模型,按 models/ 下实际存在的目录自动选择:
- sherpa-onnx-streaming-paraformer-bilingual-zh-en(草稿质量高,体积大)
- sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23(体积小,草稿质量一般)
"""
import os

import numpy as np
import sherpa_onnx

PARAFORMER_DIR = "sherpa-onnx-streaming-paraformer-bilingual-zh-en"
ZIPFORMER_DIR = "sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23"


class StreamingDraft:
    """一次听写 = 一个 session(create_stream);feed 返回当前累计草稿文本。"""

    def __init__(self, models_dir, num_threads=2):
        pd = os.path.join(models_dir, PARAFORMER_DIR)
        zd = os.path.join(models_dir, ZIPFORMER_DIR)
        if os.path.isfile(os.path.join(pd, "encoder.int8.onnx")):
            self._rec = sherpa_onnx.OnlineRecognizer.from_paraformer(
                tokens=os.path.join(pd, "tokens.txt"),
                encoder=os.path.join(pd, "encoder.int8.onnx"),
                decoder=os.path.join(pd, "decoder.int8.onnx"),
                num_threads=num_threads,
                sample_rate=16000,
                feature_dim=80,
            )
            self.kind = "paraformer"
        elif os.path.isfile(os.path.join(zd, "encoder-epoch-99-avg-1.int8.onnx")):
            self._rec = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=os.path.join(zd, "tokens.txt"),
                encoder=os.path.join(zd, "encoder-epoch-99-avg-1.int8.onnx"),
                decoder=os.path.join(zd, "decoder-epoch-99-avg-1.onnx"),
                joiner=os.path.join(zd, "joiner-epoch-99-avg-1.int8.onnx"),
                num_threads=num_threads,
                sample_rate=16000,
                feature_dim=80,
            )
            self.kind = "zipformer"
        else:
            raise FileNotFoundError("未找到流式识别模型目录(streaming-paraformer 或 streaming-zipformer)")

    def new_session(self):
        return self._rec.create_stream()

    def feed(self, stream, samples_16k: np.ndarray) -> str:
        """喂入一段 16k float32 音频,返回当前草稿全文。"""
        if len(samples_16k):
            stream.accept_waveform(16000, samples_16k)
        while self._rec.is_ready(stream):
            self._rec.decode_stream(stream)
        return self._rec.get_result(stream)

    def finish(self, stream) -> str:
        stream.input_finished()
        while self._rec.is_ready(stream):
            self._rec.decode_stream(stream)
        return self._rec.get_result(stream)


def resample_chunk(samples: np.ndarray, sr: int) -> np.ndarray:
    """草稿路径的轻量逐块重采样(块边界的微小误差不影响草稿显示)。"""
    if sr == 16000 or len(samples) == 0:
        return samples.astype(np.float32)
    n_out = int(round(len(samples) * 16000.0 / sr))
    x_old = np.arange(len(samples), dtype=np.float64) / sr
    x_new = np.arange(n_out, dtype=np.float64) / 16000.0
    return np.interp(x_new, x_old, samples).astype(np.float32)
