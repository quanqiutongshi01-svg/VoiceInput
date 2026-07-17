"""数据飞轮:从使用记录(records.jsonl)自动挖掘个人热词。

原理:每条记录里存着"识别原文 raw"和"纠错定稿 corrected"。
LLM(或热词)每修正一次,就是一条"她的哪个音会被认成什么"的证据。
同一个错形反复出现且始终修成同一个正确形(≥3次、一致率≥80%),
就升级为本地热词——下次不用等网络,瞬间修正。全程本地,不联网。
"""
import difflib
import json
import os
import re
from collections import Counter, defaultdict

_PUNCT = r"[\s,。、?!,.?!:;:;·…\"'“”‘’()()]"


def _pairs_from(raw: str, corrected: str):
    sm = difflib.SequenceMatcher(None, raw, corrected, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "replace":
            continue
        w, r = raw[i1:i2].strip(), corrected[j1:j2].strip()
        if not (1 <= len(w) <= 8 and 1 <= len(r) <= 12):
            continue
        # 纯标点/空白差异不算
        if not re.sub(_PUNCT, "", w) or not re.sub(_PUNCT, "", r):
            continue
        out.append((w, r))
    return out


def mine(records_path: str, existing_hotwords: dict, min_count: int = 3):
    """返回 (新热词 {正确形: [错形...]}, 记录总数)。"""
    if not os.path.isfile(records_path):
        return {}, 0
    cnt = Counter()
    total = 0
    for line in open(records_path, encoding="utf-8"):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        raw, cor = e.get("raw", ""), e.get("corrected", "")
        if raw and cor and raw != cor:
            for w, r in _pairs_from(raw, cor):
                # 错形至少2个汉字,或者是一个完整英文词
                if len(w) >= 2 or re.fullmatch(r"[A-Za-z]{3,}", w):
                    cnt[(w, r)] += 1

    by_wrong = defaultdict(Counter)
    for (w, r), c in cnt.items():
        by_wrong[w][r] += c

    covered = set(existing_hotwords)
    for v in existing_hotwords.values():
        covered.update(v)

    new = {}
    for w, rc in by_wrong.items():
        r, c = rc.most_common(1)[0]
        if (c >= min_count and c / sum(rc.values()) >= 0.8
                and w not in covered and r and w != r):
            new.setdefault(r, []).append(w)
    return new, total


def merge_into(cfg: dict, new: dict) -> int:
    """把挖掘结果并入 cfg['hotwords'],返回新增错形数。"""
    hw = cfg.setdefault("hotwords", {})
    added = 0
    for right, wrongs in new.items():
        cur = hw.setdefault(right, [])
        for w in wrongs:
            if w not in cur:
                cur.append(w)
                added += 1
    return added
