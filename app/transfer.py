"""听晓家庭快传:同一局域网内两台设备直传文件/文字(Mac↔Windows)。

纯标准库(socket + http.server),便携 Python 无需装任何新依赖。

发现:UDP 广播 {id,name,ip,port,disc_fp}。disc_fp 只用于"同一家庭才互相可见",
      它无法反推家庭码,也不是鉴权凭据。
鉴权:每次传输带 nonce+时间戳+HMAC(家庭码, 请求要素)。服务端重算校验、
      限时间窗(±60s)、记忆近期 nonce 防重放——线路上没有可复用的口令。
接收:文件流式落盘(校验文件名/大小/磁盘余量/并发上限),文字进剪贴板。
"""
import hashlib
import hmac
import http.server
import json
import os
import re
import shutil
import socket
import threading
import time
import uuid

DISCOVERY_PORT = 50801
DEFAULT_HTTP_PORT = 50802
BROADCAST_INTERVAL = 3.0
PEER_TIMEOUT = 10.0
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024   # 单文件 2GB 上限
MAX_TEXT_BYTES = 1 * 1024 * 1024          # 文字 1MB 上限
DISK_MARGIN = 512 * 1024 * 1024           # 落盘前保留的磁盘余量
MAX_CONCURRENT_RECV = 4                   # 并发接收上限
TIME_WINDOW = 60                          # HMAC 时间窗(秒)

_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _disc_fp(code: str) -> str:
    """公开发现指纹:只标识"同一家庭",不是凭据。"""
    return hashlib.sha256(("tingxiao-disc:" + (code or "")).encode()).hexdigest()[:16]


def _sign(code: str, parts) -> str:
    msg = "|".join(str(p) for p in parts).encode("utf-8")
    return hmac.new((code or "").encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _safe_name(raw: str) -> str:
    name = os.path.basename(raw).replace("\\", "").replace("/", "")
    name = re.sub(r"[\x00-\x1f]", "", name).rstrip(" .")
    if not name:
        return "file.bin"
    stem, ext = os.path.splitext(name)
    if stem.upper() in _WIN_RESERVED:
        stem = "_" + stem
    # 组件字节数封顶(保留扩展名)
    name = stem + ext
    if len(name.encode("utf-8")) > 200:
        stem = stem.encode("utf-8")[:180].decode("utf-8", "ignore")
        name = stem + ext
    return name or "file.bin"


class TransferService:
    def __init__(self, cfg: dict, on_incoming=None, on_peers=None):
        c = cfg or {}
        self.name = c.get("device_name") or socket.gethostname()
        self.family_code = c.get("family_code", "")
        self.save_dir = os.path.expanduser(c.get("save_dir", "~/Downloads"))
        self.disc_port = int(c.get("discovery_port", DISCOVERY_PORT))
        self.http_port = int(c.get("http_port", DEFAULT_HTTP_PORT))
        self.device_id = c.get("device_id") or uuid.uuid4().hex[:12]

        self.on_incoming = on_incoming or (lambda *a: None)
        self.on_peers = on_peers or (lambda *a: None)
        self.error = ""                 # 启动错误(端口占用等),供 UI 显示
        self._peers = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._httpd = None
        self._ip = _local_ip()
        self._ip_ts = time.time()
        self._recv_sem = threading.Semaphore(MAX_CONCURRENT_RECV)
        self._seen_nonces = {}          # nonce -> 到期时间(防重放)
        self._nonce_lock = threading.Lock()

    def local_ip(self):
        if time.time() - self._ip_ts > 30:  # 缓存,避免每次广播都建 socket
            self._ip = _local_ip()
            self._ip_ts = time.time()
        return self._ip

    # ---- 生命周期 ----

    def start(self):
        if not self.family_code:
            self.error = "快传未配对(请安装 Leo 提供的家庭版,含专属家庭码)"
            print(f"[transfer] {self.error},服务不启动")
            return
        try:
            os.makedirs(self.save_dir, exist_ok=True)
        except Exception as e:
            self.error = f"快传保存目录不可写: {e}"
        threading.Thread(target=self._serve_http, daemon=True).start()
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()
        threading.Thread(target=self._reap_loop, daemon=True).start()
        print(f"[transfer] 快传就绪:{self.name} @ {self.local_ip()}:{self.http_port}")

    def stop(self):
        self._stop.set()
        httpd = self._httpd
        if httpd:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass

    def peers(self):
        with self._lock:
            return [dict(p) for p in self._peers.values()]

    # ---- 发现 ----

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not self._stop.is_set():
            msg = json.dumps({
                "t": "tingxiao-hello", "id": self.device_id, "name": self.name,
                "ip": self.local_ip(), "port": self.http_port,
                "fp": _disc_fp(self.family_code),
            }).encode("utf-8")
            for addr in ("255.255.255.255", "127.255.255.255"):
                try:
                    sock.sendto(msg, (addr, self.disc_port))
                except Exception:
                    pass
            self._stop.wait(BROADCAST_INTERVAL)
        sock.close()

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", self.disc_port))
        except OSError as e:
            self.error = f"发现端口 {self.disc_port} 被占用({e}),快传无法发现设备"
            print(f"[transfer] {self.error}")
            return
        sock.settimeout(1.0)
        my_fp = _disc_fp(self.family_code)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                m = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if (m.get("t") != "tingxiao-hello" or m.get("fp") != my_fp
                    or m.get("id") == self.device_id):
                continue
            entry = {"id": m["id"], "name": m.get("name", "?"),
                     "ip": m.get("ip") or addr[0], "port": m.get("port"),
                     "ts": time.time()}
            changed = False
            with self._lock:
                old = self._peers.get(m["id"])
                if (not old or old["name"] != entry["name"]
                        or old["ip"] != entry["ip"] or old["port"] != entry["port"]):
                    changed = True
                self._peers[m["id"]] = entry
            if changed:  # 只在设备真的新增/信息变了时回调,不是每个心跳
                self.on_peers(self.peers())
        sock.close()

    def _reap_loop(self):
        while not self._stop.is_set():
            now = time.time()
            changed = False
            with self._lock:
                for k in [k for k, p in self._peers.items()
                          if now - p["ts"] > PEER_TIMEOUT]:
                    del self._peers[k]
                    changed = True
            if changed:
                self.on_peers(self.peers())
            # 顺便清理过期 nonce
            with self._nonce_lock:
                for n in [n for n, exp in self._seen_nonces.items() if exp < now]:
                    del self._seen_nonces[n]
            self._stop.wait(2.0)

    def _check_nonce(self, nonce, ts) -> bool:
        """时间窗内 + 未见过的 nonce 才放行(防重放)。"""
        now = time.time()
        try:
            if abs(now - float(ts)) > TIME_WINDOW:
                return False
        except (TypeError, ValueError):
            return False
        with self._nonce_lock:
            if nonce in self._seen_nonces:
                return False
            self._seen_nonces[nonce] = now + TIME_WINDOW * 2
        return True

    # ---- 接收 ----

    def _serve_http(self):
        service = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = 30  # 慢速连接自动断开,防 slowloris

            def log_message(self, *a):
                pass

            def _deny(self, code=403, msg="deny"):
                try:
                    self.close_connection = True  # 拒绝即断,不等未读的body
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except Exception:
                    pass

            def _ok(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self):
                h = self.headers
                kind = h.get("X-Tingxiao-Kind", "file")
                name_hdr = h.get("X-Tingxiao-Name", "file.bin")
                nonce = h.get("X-Tingxiao-Nonce", "")
                ts = h.get("X-Tingxiao-Ts", "")
                mac = h.get("X-Tingxiao-Mac", "")
                try:
                    length = int(h.get("Content-Length", 0))
                except (TypeError, ValueError):
                    return self._deny(400, "bad length")
                if length < 0:
                    return self._deny(400, "bad length")
                # 请求级 HMAC 校验(恒定时间比较)+ 防重放
                expect = _sign(service.family_code,
                               [kind, name_hdr, length, nonce, ts])
                if not hmac.compare_digest(mac, expect):
                    return self._deny(403, "bad signature")
                if not service._check_nonce(nonce, ts):
                    return self._deny(403, "replay/expired")

                sender = self._decode(h.get("X-Tingxiao-From", "对方"))

                if kind == "text":
                    if length > MAX_TEXT_BYTES:
                        return self._deny(413, "text too large")
                    body = self.rfile.read(min(length, MAX_TEXT_BYTES)).decode(
                        "utf-8", "replace")
                    self._ok()
                    service.on_incoming("text", sender, body)
                    return

                # 文件
                if length > MAX_FILE_BYTES:
                    return self._deny(413, "too large")
                try:
                    free = shutil.disk_usage(service.save_dir).free
                    if free < length + DISK_MARGIN:
                        return self._deny(507, "disk full")
                except Exception:
                    pass
                if not service._recv_sem.acquire(blocking=False):
                    return self._deny(503, "busy")
                try:
                    self._recv_file(service, length, name_hdr, sender)
                finally:
                    service._recv_sem.release()

            def _recv_file(self, service, length, name_hdr, sender):
                fname = _safe_name(self._decode(name_hdr))
                dst = os.path.join(service.save_dir, fname)
                base, ext = os.path.splitext(dst)
                i = 1
                while os.path.exists(dst):
                    dst = f"{base}({i}){ext}"
                    i += 1
                tmp = dst + ".part"
                got = 0
                try:
                    with open(tmp, "wb") as f:
                        while got < length:
                            chunk = self.rfile.read(min(1 << 20, length - got))
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                    if got < length:
                        raise IOError("incomplete")
                    os.replace(tmp, dst)
                except Exception:
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except OSError:
                        pass
                    return self._deny(500, "recv failed")
                self._ok()
                service.on_incoming("file", sender, dst)

            @staticmethod
            def _decode(v):
                try:
                    return bytes(v, "latin-1").decode("utf-8")
                except Exception:
                    return v

        try:
            httpd = http.server.ThreadingHTTPServer(("", self.http_port), Handler)
        except OSError as e:
            self.error = f"接收端口 {self.http_port} 被占用({e}),快传无法接收"
            print(f"[transfer] {self.error}")
            return
        self._httpd = httpd
        httpd.serve_forever(poll_interval=0.5)

    # ---- 发送 ----

    def _headers(self, kind, length, name=""):
        nonce = uuid.uuid4().hex
        ts = str(int(time.time()))
        name_enc = name.encode("utf-8").decode("latin-1") if name else ""
        return {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(length),
            "X-Tingxiao-Kind": kind,
            "X-Tingxiao-Name": name_enc,
            "X-Tingxiao-From": self.name.encode("utf-8").decode("latin-1"),
            "X-Tingxiao-Nonce": nonce,
            "X-Tingxiao-Ts": ts,
            "X-Tingxiao-Mac": _sign(self.family_code, [kind, name_enc, length, nonce, ts]),
        }

    def send_file(self, peer, path, progress=None):
        size = os.path.getsize(path)
        name = os.path.basename(path)
        headers = self._headers("file", size, name)
        return self._post(peer, headers, _FileReader(path, size, progress))

    def send_text(self, peer, text):
        data = text.encode("utf-8")
        headers = self._headers("text", len(data))
        return self._post(peer, headers, data)

    def _post(self, peer, headers, body):
        conn = None
        try:
            import http.client

            conn = http.client.HTTPConnection(peer["ip"], peer["port"], timeout=30)
            conn.request("POST", "/recv", body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()
            return resp.status == 200, ("" if resp.status == 200 else f"HTTP {resp.status}")
        except Exception as e:
            return False, str(e)
        finally:
            if conn:
                conn.close()


class _FileReader:
    def __init__(self, path, size, progress=None):
        self._f = open(path, "rb")
        self._size = size
        self._sent = 0
        self._progress = progress
        self._last_emit = 0.0

    def read(self, n=-1):
        chunk = self._f.read(1 << 20 if n < 0 else n)
        if chunk:
            self._sent += len(chunk)
            if self._progress:
                now = time.time()
                # 节流:≥120ms 或传完才回调一次,避免刷爆 UI
                if now - self._last_emit >= 0.12 or self._sent >= self._size:
                    self._last_emit = now
                    try:
                        self._progress(self._sent, self._size)
                    except Exception:
                        pass
        else:
            self._f.close()
        return chunk

    def __len__(self):
        return self._size
