#!/usr/bin/env python3
"""语音输入原型:按住热键说话,松开后识别+纠错+注入光标。

用法:
  python main.py                  正常运行(常驻,按 Ctrl+C 退出)
  python main.py --list-devices   列出输入设备
  python main.py --test 某.wav    离线测试流水线(不用麦克风/热键)

架构说明:
- 热键钩子回调只做入队(suppress=True 时回调在系统钩子线程同步执行,
  超过约300ms会被系统摘钩,绝不能做任何耗时操作);
- 主线程消费按键事件(串行,无竞态);
- 单工作线程消费识别任务(保证多段话按顺序上屏,防止慢句被快句超车)。
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)


def load_config():
    path = os.path.join(BASE, "config.json")
    try:
        # utf-8-sig: 兼容被记事本存成带 BOM 的 UTF-8
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        print("[错误] 找不到 config.json,请确认它和 main.py 在同一个文件夹里。")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[错误] config.json 格式有误(第{e.lineno}行附近): {e.msg}")
        print("       请检查最近一次修改,或找 Leo 要一份原始 config.json。")
        sys.exit(1)


def beep(freq, enabled):
    """880=开始说话 440=处理中 220=出错/截断"""
    if not enabled:
        return
    if sys.platform == "win32":
        import winsound

        threading.Thread(target=winsound.Beep, args=(freq, 120), daemon=True).start()
    else:
        print("\a", end="", flush=True)


def build_pipeline(cfg):
    from asr import Recognizer
    from corrector import Corrector
    from archiver import Archiver

    print("[main] 加载识别模型(首次约10秒)...")
    rec = Recognizer(cfg.get("models_dir", "models"), punctuation=cfg.get("punctuation", True),
                     language=cfg.get("language", "auto"))
    cor = Corrector(cfg.get("llm"), cfg.get("hotwords"), cfg.get("glossary"))
    arc = Archiver(cfg.get("archive"))
    if cor.enabled:
        print("[main] LLM 纠错: 开启(注意:每句出字会增加最多几秒延迟)")
    else:
        print("[main] LLM 纠错: 未配置(仅本地热词替换)")
    return rec, cor, arc


def process(samples_16k, rec, cor, arc, injector, min_sec):
    dur = len(samples_16k) / 16000.0
    if dur < min_sec:
        print(f"[main] 录音太短({dur:.2f}s),忽略")
        return
    t0 = time.time()
    raw = rec.transcribe(samples_16k, 16000)
    if not raw:
        print("[main] 没有识别到内容")
        return
    fixed = cor.correct(raw)
    print(f"[main] {dur:.1f}s 音频, 识别+纠错 {time.time()-t0:.2f}s")
    print(f"  原始: {raw}")
    if fixed != raw:
        print(f"  纠错: {fixed}")
    injector.inject(fixed)
    arc.save(samples_16k, raw, fixed)


def run_test(wav_path, cfg):
    import soundfile as sf
    from asr import resample_to_16k
    from injector import PrintInjector

    rec, cor, arc = build_pipeline(cfg)
    samples, sr = sf.read(wav_path, dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    samples = resample_to_16k(samples, sr)
    process(samples, rec, cor, arc, PrintInjector(), 0.0)


def run_live(cfg):
    import keyboard  # Windows 全局热键
    from asr import resample_to_16k
    from recorder import Recorder
    from injector import make_injector

    rec, cor, arc = build_pipeline(cfg)
    injector = make_injector(
        cfg.get("inject_method", "sendinput"), cfg.get("inject_fallback_clipboard", True)
    )
    hotkey = cfg.get("hotkey", "f9")
    min_sec = float(cfg.get("min_speech_seconds", 0.4))
    max_sec = float(cfg.get("max_speech_seconds", 60))
    beep_on = cfg.get("beep", True)

    recorder = Recorder(
        cfg.get("mic_name_contains", ""),
        max_seconds=max_sec,
        persistent=cfg.get("persistent_mic", True),
    )
    try:
        recorder.open()
    except Exception as e:
        print(f"[错误] 打开麦克风失败: {e}")
        print("       请确认耳机已连接并在 Windows「设置→声音→输入」里可见,然后重新启动。")
        sys.exit(1)

    # 热键回调:只入队,别的什么都不做
    events = queue.Queue()
    keyboard.on_press_key(hotkey, lambda _: events.put("down"), suppress=True)
    keyboard.on_release_key(hotkey, lambda _: events.put("up"), suppress=True)

    # 识别工作线程:单线程串行,保证出字顺序
    jobs = queue.Queue()

    def worker():
        while True:
            samples, sr, truncated = jobs.get()
            try:
                if truncated:
                    print(f"[main] 录音超过 {max_sec:.0f}s,只识别前 {max_sec:.0f}s")
                samples16 = resample_to_16k(samples, sr)
                process(samples16, rec, cor, arc, injector, min_sec)
            except Exception:
                traceback.print_exc()
                print("[main] 本次处理失败,已跳过(不影响下一次)")
            finally:
                jobs.task_done()

    threading.Thread(target=worker, daemon=True).start()

    recording = False
    t0 = 0.0
    print(f"[main] 就绪。按住 {hotkey.upper()} 说话,松开出字。Ctrl+C 退出。")
    while True:
        ev = events.get()
        if ev == "down":
            if recording:
                # 按住不放时系统会自动重复 down:忽略。
                # 若上次的 up 丢了(锁屏/UAC 弹窗时松键),超时自愈:
                if time.time() - t0 > max_sec + 5:
                    try:
                        recorder.stop_capture()
                    except Exception:
                        pass
                    recording = False
                    print("[main] 上次录音未正常结束,已自动重置")
                else:
                    continue
            try:
                recorder.start_capture()
                recording = True
                t0 = time.time()
                beep(880, beep_on)
            except Exception:
                recording = False
                beep(220, beep_on)
                traceback.print_exc()
                print(f"[main] 录音启动失败(耳机是否连接?),稍后再按 {hotkey.upper()} 重试")
        elif ev == "up":
            if not recording:
                continue
            recording = False
            beep(440, beep_on)
            try:
                samples, sr, truncated = recorder.stop_capture()
            except Exception:
                traceback.print_exc()
                continue
            if truncated:
                beep(220, beep_on)
            if len(samples) == 0:
                print("[main] 没有录到音频,忽略")
                continue
            jobs.put((samples, sr, truncated))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--test", metavar="WAV")
    args = ap.parse_args()
    cfg = load_config()

    if args.list_devices:
        from recorder import list_input_devices

        for i, name, api, sr in list_input_devices():
            print(f"#{i:3d}  {name}  [{api}]  {sr}Hz")
        return
    if args.test:
        run_test(args.test, cfg)
        return
    try:
        run_live(cfg)
    except KeyboardInterrupt:
        print("\n[main] 已退出,再见。")


if __name__ == "__main__":
    main()
