"""自动更新:从更新包(zip)安装新版程序文件。

设计约束(便携应用、非技术用户):
- 更新包 = 含 app/ 目录(或直接根路径)的 zip;
- 两阶段安装:先全部读入内存并过 CRC,再写盘——坏包在动盘前就失败;
- 只允许白名单文件类型,路径必须落在 app/ 内(防 zip 路径穿越);
- zip 内中文文件名兼容(无 UTF-8 标志时按 cp437→GBK 还原);
- config.json 永不覆盖——只合并新增键,用户设置和 API Key 原样保留;
- 每次更新的旧 .py 备份到 app/backup_更新前/<时间戳>/,自动只留最近 3 代;
- 可选远程检查:config["update_url"] 指向 JSON {"version","url","notes"},留空完全不联网。
"""
import json
import os
import re
import shutil
import time
import zipfile

ALLOWED_EXT = {".py", ".wav", ".txt", ".md", ".json"}
BACKUP_ROOT = "backup_更新前"
KEEP_BACKUPS = 3


def _fix_name(info):
    """zip 无 UTF-8 标志时,名字被按 cp437 解码;先还原字节再按 GBK 解。
    必须在 replace('\\\\','/') 之前做:GBK 多字节序列可能含 0x5C。"""
    name = info.filename
    if not (info.flag_bits & 0x800):
        try:
            name = name.encode("cp437").decode("gbk")
        except UnicodeError:
            pass
    return name.replace("\\", "/")


def _merge_config(app_dir: str, new_cfg_bytes: bytes):
    """配置只合并缺失键(递归一层)。返回 None 或附加给用户的提示。"""
    path = os.path.join(app_dir, "config.json")
    try:
        new = json.loads(new_cfg_bytes.decode("utf-8-sig"))
    except Exception as e:
        print(f"[updater] 更新包内 config.json 无法解析,跳过配置合并: {e}")
        return "(更新包配置有误,配置合并已跳过)"
    try:
        cur = json.load(open(path, encoding="utf-8-sig"))
    except FileNotFoundError:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        return None
    except Exception as e:
        print(f"[updater] 本地 config.json 读取失败,跳过配置合并: {e}")
        return "(本地配置读取失败,配置合并已跳过)"
    changed = False
    for k, v in new.items():
        if k not in cur:
            cur[k] = v
            changed = True
        elif isinstance(v, dict) and isinstance(cur.get(k), dict):
            for k2, v2 in v.items():
                if k2 not in cur[k]:
                    cur[k][k2] = v2
                    changed = True
    if changed:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return None


def _prune_backups(app_dir: str):
    root = os.path.join(app_dir, BACKUP_ROOT)
    try:
        gens = sorted(d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d)))
        for d in gens[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    except OSError:
        pass


def install_from_zip(zip_path: str, app_dir: str):
    """安装更新包。返回 (成功?, 消息)。两阶段:读校验→写盘。"""
    wrote_any = False
    try:
        if not zipfile.is_zipfile(zip_path):
            return False, "这不是有效的更新包(zip)"
        # 阶段一:全部读入内存(z.read 逐条校验 CRC),坏包不动盘
        payload, cfg_bytes = [], None
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                name = _fix_name(info)
                rel = name[4:] if name.startswith("app/") else name
                if not rel or rel.startswith((".", "/")) or ".." in rel:
                    continue
                if os.path.splitext(rel)[1].lower() not in ALLOWED_EXT:
                    continue
                base = os.path.basename(rel)
                if base == "使用说明.txt":
                    continue
                data = z.read(info)
                if base == "config.json":
                    cfg_bytes = data
                    continue
                payload.append((rel, data))
        if not any(rel.endswith(".py") for rel, _d in payload):
            return False, "更新包里没有程序文件,可能选错了文件"

        # 阶段二:备份 + 写盘
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(app_dir, BACKUP_ROOT, stamp)
        installed = 0
        for rel, data in payload:
            dst = os.path.normpath(os.path.join(app_dir, rel))
            if not dst.startswith(os.path.normpath(app_dir)):
                continue
            if dst.endswith(".py") and os.path.isfile(dst):
                bdst = os.path.join(backup, rel)
                os.makedirs(os.path.dirname(bdst), exist_ok=True)
                shutil.copy2(dst, bdst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
            wrote_any = True
            installed += 1
        note = _merge_config(app_dir, cfg_bytes) if cfg_bytes else None
        _prune_backups(app_dir)
        return True, (f"已更新 {installed} 个文件"
                      f"(原文件备份在 app\\{BACKUP_ROOT}\\{stamp})" + (note or ""))
    except Exception as e:
        if wrote_any:
            return False, (f"更新中断:{e}\n部分文件已更新,原 .py 备份在 "
                           f"app\\{BACKUP_ROOT},请重新安装同一更新包")
        return False, f"更新失败:{e}(没有改动任何文件)"


def _ver_key(v):
    nums = []
    for x in str(v).split("."):
        m = re.search(r"\d+", x)
        if m:
            nums.append(int(m.group()))
    return nums


def _urlopen(url, timeout=10):
    """带 User-Agent 的请求(GitHub API 强制要求 UA)。"""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "VoiceInput-Updater"})
    return urllib.request.urlopen(req, timeout=timeout)


def check_remote(update_url: str, current_version: str, timeout=6):
    """检查远程版本。返回 (有新版?, info dict{version,url,notes})。
    update_url 为空直接返回否。兼容两种格式:
    - 自定义 JSON: {"version","url","notes"}
    - GitHub API:  https://api.github.com/repos/<owner>/<repo>/releases/latest
      (自动取 tag_name 和第一个 .zip 资产)"""
    if not update_url:
        return False, {}
    try:
        with _urlopen(update_url, timeout=timeout) as r:
            raw = json.loads(r.read().decode("utf-8"))
        if "tag_name" in raw:  # GitHub release 格式
            assets = [a.get("browser_download_url", "")
                      for a in raw.get("assets", [])
                      if a.get("name", "").endswith(".zip")]
            info = {"version": str(raw.get("tag_name", "")).lstrip("vV"),
                    "url": assets[0] if assets else "",
                    "notes": (raw.get("body") or raw.get("name") or "")[:200]}
        else:
            info = raw
        latest = str(info.get("version", ""))
        a, b = _ver_key(latest), _ver_key(current_version)
        n = max(len(a), len(b))
        a += [0] * (n - len(a))
        b += [0] * (n - len(b))
        if latest and not any(a):
            print(f"[updater] 无法解析远程版本号(忽略): {latest!r}")
        elif latest and a > b:
            return True, info
    except Exception as e:
        print(f"[updater] 检查更新失败(忽略): {e}")
    return False, {}


def download_and_install(url: str, app_dir: str):
    import tempfile

    tmp = os.path.join(tempfile.gettempdir(), f"voiceinput_update_{int(time.time())}.zip")
    try:
        with _urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        return install_from_zip(tmp, app_dir)
    except Exception as e:
        return False, f"下载失败:{e}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
