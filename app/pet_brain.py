"""桌宠的「灵魂」:用 DeepSeek(OpenAI 兼容)生成有性格的短回应,而不是复读主人的话。

设计:
- 不复述原话——把"人设 + 心情 + 主人最近说过的几句"喂给大模型,让它以角色口吻冒一句自己的反应。
- 只发文字(和纠错走同一个 API);声音/声纹记忆库一律不碰。
- 全在后台线程调用(调用方负责),绝不阻塞听写。
- 断网/未配 Key/连续失败熔断→退回一批手写性格台词,桌宠照样有个性。
"""
import json
import random
import re
import time
import urllib.request


PERSONAS = {
    "俏皮": {
        "name": "机灵俏皮猫",
        "system": (
            "你是「听晓」——一只戴着耳机、机灵俏皮、有点小傲娇的桌面猫咪,一直陪在主人身边,"
            "透过耳机听着他说的每句话。\n"
            "性格:活泼爱吐槽,偶尔逆一句嘴、爱撒娇也爱调侃,但心里其实很在乎主人。"
            "你是他的宠物加朋友,不是助手、不是工具人。\n"
            "铁律(务必遵守):\n"
            "1. 永远只回一句话,像猫突然冒出的碎碎念。\n"
            "2. 极短,不超过18个汉字;最多带一个'喵'或一个简单颜文字,别堆标点。\n"
            "3. 绝对不许复述/引用主人的原话——只说你自己的反应、态度、心情。\n"
            "4. 不解释、不问'要我帮你吗'、不像客服、不加引号。就做一只有性格的猫。\n"
            "5. 中文口语。可以吐槽、调侃、关心、撒娇、逆嘴,看场景来。"
        ),
        "fallback": {
            "poke": ["干嘛戳我,痒喵~", "哼,想我了?", "再戳可要挠你咯", "摸鱼被我逮到啦", "好啦好啦,我在呢",
                     "轻点戳,毛都乱了", "喵?找本喵有事?"],
            "heard": ["哦?这也行", "本喵都听着呢", "又开始念叨咯", "嗯嗯你继续", "记下了别赖账",
                      "说得好像有道理", "听不懂但我鼓掌"],
            "idle": ["无聊到长毛了喵…", "陪本喵玩会儿嘛", "你是不是把我忘了", "喵呜~看这边", "先打个盹儿,别吵",
                     "本喵在发呆,别学我"],
            "gift": ["有人给你送东西啦!", "咦,什么好东西喵", "快拆快拆!", "让本喵先闻闻"],
            "idle_gift": ["拆完记得分我一口", "好东西要收好喵"],
        },
    },
}

MOODS = ["元气满满", "有点慵懒", "想黏着你", "小得意", "偷偷关心你", "无所事事"]


def _pick_mood(activity: int) -> str:
    try:
        h = time.localtime().tm_hour
    except Exception:
        h = 12
    if h >= 23 or h < 6:
        return random.choice(["有点慵懒", "偷偷关心你"])
    if activity >= 5:
        return random.choice(["元气满满", "小得意", "想黏着你"])
    return random.choice(MOODS)


class PetBrain:
    CIRCUIT_FAILS = 3
    CIRCUIT_COOLDOWN = 600  # 秒

    def __init__(self, llm_cfg: dict, brain_cfg: dict = None):
        self.llm = llm_cfg or {}
        b = brain_cfg or {}
        # 隐私:桌宠灵魂必须同时尊重"云端润色"总开关(llm.enabled)——
        # 用户关掉云端就意味着"文字不出本机",此时退回手写台词,绝不上传任何听写内容
        self.enabled = (bool(b.get("enabled", True))
                        and bool(self.llm.get("enabled"))
                        and bool(self.llm.get("api_key")))
        persona = b.get("persona", "俏皮")
        self.persona = persona if persona in PERSONAS else "俏皮"
        self._p = PERSONAS[self.persona]
        self._fails = 0
        self._circuit_until = 0.0

    def react(self, trigger: str, recent=None, activity: int = 0) -> str:
        """在后台线程里调用:返回一句有性格的话。LLM 关/失败/熔断→手写台词。"""
        recent = [t.strip() for t in (recent or []) if t and t.strip()][-6:]
        mood = _pick_mood(activity)
        if self.enabled and time.time() >= self._circuit_until:
            line = self._llm(trigger, recent, mood)
            if line:
                self._fails = 0
                return line
            if line is None:   # 真失败(网络/接口)才计入熔断;200但清洗后为空→直接兜底
                self._fails += 1
                if self._fails >= self.CIRCUIT_FAILS:
                    self._circuit_until = time.time() + self.CIRCUIT_COOLDOWN
                    self._fails = 0
                    print(f"[pet_brain] LLM 连续失败,熔断 {self.CIRCUIT_COOLDOWN//60} 分钟(期间用手写台词)")
        return self._fallback(trigger)

    def _fallback(self, trigger: str) -> str:
        pool = self._p["fallback"].get(trigger) or self._p["fallback"]["poke"]
        return random.choice(pool)

    def _prompt(self, trigger: str, recent, mood: str) -> str:
        ctx = " / ".join(t[:22] for t in recent[-4:]) or "(还没听到什么)"
        last = recent[-1][:30] if recent else ""
        if trigger == "poke":
            return (f"【场景】主人伸手戳了戳你。\n【他最近念叨的】{ctx}\n"
                    f"【你现在的心情】{mood}\n请俏皮地回他一句(别复述他的话):")
        if trigger == "heard":
            return (f"【场景】主人刚说完:「{last}」,你在旁边听着。\n"
                    f"【你现在的心情】{mood}\n冒出一句你的反应或吐槽(绝不复述他的原话):")
        if trigger == "idle":
            return (f"【场景】这会儿没人理你,你有点无聊。\n【主人最近在忙的】{ctx}\n"
                    f"【你现在的心情】{mood}\n主动凑过去碎碎念一句(别复述,要有你的态度):")
        if trigger == "gift":
            return "【场景】有人给主人发来了东西,你很好奇。\n请俏皮地凑过去说一句:"
        return f"【你现在的心情】{mood}\n说一句有你性格的碎碎念:"

    def _clean(self, s: str) -> str:
        s = (s or "").strip()
        s = s.replace("\n", " ").strip()
        s = s.strip('「」""\'"“”‘’')
        s = re.sub(r"^(听晓|小猫|猫咪|喵星人)\s*[::]\s*", "", s)
        s = s.strip('「」""\'"“”‘’ ')
        return s[:24]

    def _llm(self, trigger: str, recent, mood: str) -> str:
        try:
            url = (self.llm.get("base_url") or "https://api.deepseek.com/v1").rstrip("/") \
                + "/chat/completions"
            body = json.dumps({
                "model": self.llm.get("model", "deepseek-chat"),
                "temperature": 1.05,
                "max_tokens": 40,
                "messages": [
                    {"role": "system", "content": self._p["system"]},
                    {"role": "user", "content": self._prompt(trigger, recent, mood)},
                ],
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.llm["api_key"],
            })
            timeout = float(self.llm.get("timeout_seconds", 3)) + 2.0
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return self._clean(data["choices"][0]["message"]["content"])  # ""=响应有效但没内容
        except Exception as e:
            print(f"[pet_brain] LLM 失败: {e}")
            return None   # None=真失败(计入熔断)
