"""文本纠错层:热词替换(本地) + LLM 上下文纠错(可选,OpenAI 兼容 API)。

隐私:只有识别出的文本会发给 LLM API;音频永远不出本机。
LLM 未配置或调用失败时,原样返回,不阻塞输入。
"""
import json
import re
import time
import urllib.request

# 基于 2026-07 实测(1005字测试稿)得出的说话人混淆画像
SPEAKER_PROFILE = """你是语音输入法的纠错模块。说话人是湖北人,普通话有轻微口音,语音识别结果有如下已知易错点:
1. 平翘舌不分,尤其 s/sh(实测例:"三叔上山"曾被识别成"单书上"、"厨师"→"不食");
2. 偶发 n/l 声母混淆(实测例:"刘奶奶"→"牛奶奶"、"挪开"→"罗开");
3. 不发儿化音,属正常习惯,不要强行补"儿"字;
4. 常见同音字错误:的/得/地、订/定、声/生 等;
5. 中文夹英文专有词时,英文常被识别成发音相近的乱拼(实测例:Claude→cloth/cloud/clo、Codex→codeex、Python→kison、ChatGPT→cht GPT)。
{glossary_line}
你的任务:根据上下文修正识别文本中的错别字、同音字错误和拼错的英文专有词,规范标点。只做最小必要修改,绝不改写句式、不增删内容、不润色。直接输出修正后的文本,不要任何解释。"""

# config.json 没配 glossary 时的默认高频词表
DEFAULT_GLOSSARY = [
    "Claude", "Claude Code", "Codex", "ChatGPT", "GitHub", "Python",
    "API", "Cursor", "App", "iPhone", "Windows", "Mac",
]


class Corrector:
    """延迟策略:本地热词替换永远即时;LLM 只在"值得"时才上——
    短句直接出字(延迟敏感、微调模型已够准),长句限时等待,
    连续失败自动熔断 10 分钟(断网/接口抽风时体验无感回落为纯本地)。"""

    CIRCUIT_FAILS = 3
    CIRCUIT_COOLDOWN = 600  # 秒

    def __init__(self, llm_cfg: dict, hotwords: dict, glossary=None):
        self.cfg = llm_cfg or {}
        self.enabled = bool(self.cfg.get("enabled")) and bool(self.cfg.get("api_key"))
        # hotwords: {正确词: [错误形1, 错误形2]}
        self.hotwords = hotwords or {}
        self.glossary = list(glossary) if glossary else list(DEFAULT_GLOSSARY)
        gl = "6. 说话人的高频专有词表(识别结果里出现与这些词发音相近的错拼时,必须修正为词表写法):" \
             + "、".join(self.glossary) if self.glossary else ""
        self.system_prompt = SPEAKER_PROFILE.format(glossary_line=gl)
        self.min_chars = int(self.cfg.get("min_chars_for_llm", 10))
        self._fails = 0
        self._circuit_until = 0.0
        self.last_llm_used = False      # 最近一次 correct() 是否用上了云端润色
        self.circuit_just_opened = False  # 熔断刚刚发生(由上层消费并提示)

    def apply_hotwords(self, text: str) -> str:
        for right, wrongs in self.hotwords.items():
            for w in wrongs:
                if not w or w == right:
                    continue
                if re.fullmatch(r"[\x00-\x7f]+", w):
                    # 纯 ASCII 错形:忽略大小写整词替换(cloud code / Cloud Code 都命中)
                    text = re.sub(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])",
                                  right, text, flags=re.IGNORECASE)
                else:
                    text = text.replace(w, right)
        return text

    def _worth_llm(self, text: str) -> bool:
        if len(text) >= self.min_chars:
            return True
        # 短句但夹英文:英文词是历史弱项,值得一修
        return bool(re.search(r"[A-Za-z]{3,}", text))

    @property
    def circuit_open(self):
        return time.time() < self._circuit_until

    def correct(self, text: str) -> str:
        self.last_llm_used = False
        if not text:
            return text
        text = self.apply_hotwords(text)
        if (self.enabled and time.time() >= self._circuit_until
                and self._worth_llm(text)):
            fixed = self._llm(text)
            if fixed:
                self._fails = 0
                self.last_llm_used = True
                return fixed
            self._fails += 1
            if self._fails >= self.CIRCUIT_FAILS:
                self._circuit_until = time.time() + self.CIRCUIT_COOLDOWN
                self._fails = 0
                self.circuit_just_opened = True
                print(f"[corrector] LLM 连续失败,熔断 {self.CIRCUIT_COOLDOWN//60} 分钟(期间纯本地出字)")
        return text

    def _llm(self, text: str) -> str:
        try:
            url = (self.cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions"
            body = json.dumps(
                {
                    "model": self.cfg.get("model", "deepseek-chat"),
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": text},
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.cfg["api_key"],
                },
            )
            timeout = float(self.cfg.get("timeout_seconds", 3))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out = data["choices"][0]["message"]["content"].strip()
            # 简单防御:LLM 返回内容长度异常时放弃修正
            if out and 0.5 <= len(out) / max(1, len(text)) <= 2.0:
                return out
        except Exception as e:
            print(f"[corrector] LLM 纠错失败,使用原文: {e}")
        return ""
