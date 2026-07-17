#!/usr/bin/env python3
"""把听晓的个性化数据导出为 RIME(鼠须管/小狼毫)配置。

生成三件套到 RIME 用户目录(mac: ~/Library/Rime, win: %APPDATA%/Rime):
- default.custom.yaml       启用朙月拼音
- luna_pinyin.custom.yaml   口音模糊音(按说话人画像)+ 自定义词库开关
- custom_phrase.txt         听晓词表(glossary 英文词 + hotwords 中文词,拼音码自动生成)

数据同源:语音端的热词/专有词表 == 键盘端的用户词库,飞轮学到新词后重跑本脚本即同步。
用法: python rime_export.py [config.json路径]
"""
import json
import os
import sys

DEFAULT_CUSTOM = """# 听晓生成:启用朙月拼音
patch:
  schema_list:
    - schema: luna_pinyin
  menu/page_size: 7
"""

LUNA_CUSTOM = """# 听晓生成:口音模糊音(湖北画像:n/l、平翘舌、前后鼻音)+ 听晓词库
patch:
  speller/algebra:
    - erase/^xx$/
    # —— 模糊音:输错也能出对字(derive=容错,不改变标准拼法) ——
    - derive/^n/l/
    - derive/^l/n/
    - derive/^([zcs])h/$1/
    - derive/^([zcs])([^h])/$1h$2/
    - derive/([ei])n$/$1ng/
    - derive/([ei])ng$/$1n/
    # —— 常规简拼/规范 ——
    - abbrev/^([a-z]).+$/$1/
    - abbrev/^([zcs]h).+$/$1/
    - derive/^([nl])ve$/$1ue/
    - derive/^([jqxy])u/$1v/
  # 听晓词表(custom_phrase.txt)
  engine/translators/+:
    - table_translator@custom_phrase
  custom_phrase:
    dictionary: ""
    user_dict: custom_phrase
    db_class: stabledb
    enable_completion: false
    enable_sentence: false
    initial_quality: 900
"""


def rime_user_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Rime")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "Rime")
    return os.path.expanduser("~/.config/rime")


def build_phrases(cfg):
    """返回 [(词, 编码, 权重)]。英文词用其小写字母作编码;中文词用全拼。"""
    out = []
    words = list(cfg.get("glossary") or [])
    words += [w for w in (cfg.get("hotwords") or {}) if w not in words]
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        lazy_pinyin = None
    for w in words:
        if not w:
            continue
        if all(ord(c) < 128 for c in w):
            code = "".join(c for c in w.lower() if c.isalpha())
            if code:
                out.append((w, code, 100))
        elif lazy_pinyin:
            code = "".join(lazy_pinyin(w))
            if code.isascii() and code.isalpha():
                out.append((w, code, 100))
    return out


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")
    cfg = json.load(open(cfg_path, encoding="utf-8-sig"))
    dst = rime_user_dir()
    os.makedirs(dst, exist_ok=True)

    open(os.path.join(dst, "default.custom.yaml"), "w", encoding="utf-8").write(DEFAULT_CUSTOM)
    open(os.path.join(dst, "luna_pinyin.custom.yaml"), "w", encoding="utf-8").write(LUNA_CUSTOM)

    phrases = build_phrases(cfg)
    with open(os.path.join(dst, "custom_phrase.txt"), "w", encoding="utf-8") as f:
        f.write("# 听晓词表(自动生成,重跑 rime_export.py 会覆盖)\n"
                "# 格式: 词\t编码\t权重\n")
        for w, code, q in phrases:
            f.write(f"{w}\t{code}\t{q}\n")
    print(f"已写入 {dst}:default.custom.yaml / luna_pinyin.custom.yaml / "
          f"custom_phrase.txt({len(phrases)} 词)")
    print("下一步:安装鼠须管(mac)/小狼毫(win)后,在输入法菜单点「重新部署」生效。")


if __name__ == "__main__":
    main()
