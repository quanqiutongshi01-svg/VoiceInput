#!/usr/bin/env python3
"""下载 SenseVoice 识别模型和标点模型(共约500MB,只需一次)。"""
import os
import shutil
import sys
import tarfile
import urllib.request

MODELS = "models"
FILES = [
    (
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
    ),
    (
        "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2",
    ),
]


def progress(n, bs, total):
    if total > 0:
        pct = min(100, n * bs * 100 // total)
        sys.stdout.write(f"\r  {pct}% ({n*bs//1048576}MB/{total//1048576}MB)")
        sys.stdout.flush()


def main():
    os.makedirs(MODELS, exist_ok=True)
    for dirname, url in FILES:
        target = os.path.join(MODELS, dirname)
        key_file = os.path.join(target, "model.int8.onnx")
        if os.path.isdir(target):
            if os.path.isfile(key_file):
                print(f"已存在,跳过: {dirname}")
                continue
            # 上次解压中断留下的残缺目录,删除后重新下载
            print(f"检测到不完整的模型目录,重新下载: {dirname}")
            shutil.rmtree(target)
        tmp_dir = target + ".tmp"
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)
        tarball = os.path.join(MODELS, dirname + ".tar.bz2")
        print(f"下载 {dirname} ...")
        print(f"  {url}")
        urllib.request.urlretrieve(url, tarball, reporthook=progress)
        print("\n  解压中...")
        with tarfile.open(tarball, "r:bz2") as t:
            t.extractall(tmp_dir)  # 得到 <tmp_dir>/<dirname>/...
        os.remove(tarball)
        os.rename(os.path.join(tmp_dir, dirname), target)  # 完整解压后才原子落位
        os.rmdir(tmp_dir)
        print(f"  完成: {target}")
    print("全部模型就绪。")


if __name__ == "__main__":
    main()
