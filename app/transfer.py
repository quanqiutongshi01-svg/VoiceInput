"""听晓家庭快传:同一局域网内两台设备直传文件/文字(Mac↔Windows)。

纯标准库(socket + http.server),便携 Python 无需装任何新依赖。

发现:UDP 广播 {id,name,ip,port,disc_fp}。disc_fp 只标识"同一家庭",不可反推。
握手:文件先 POST /offer(带元数据)→ 接收方决定 接收/拒绝(auto_accept 或弹窗)
      → 同意则发一次性 ticket → 发送方 POST /recv 带 ticket 传字节。文字直接传。
鉴权:每次请求带 nonce+时间戳+HMAC(家庭码,请求要素),限时间窗+防重放。
历史:发送/接收都记到 history/{sent,recv}.jsonl。
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
MAX_FILE_BYTES = 5 * 1024 * 1024 * 1024
MAX_TEXT_BYTES = 1 * 1024 * 1024
DISK_MARGIN = 512 * 1024 * 1024
MAX_CONCURRENT_RECV = 4
TIME_WINDOW = 60
OFFER_TIMEOUT = 45        # 接收方多少秒内不选择就当拒绝
TICKET_TTL = 300

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


def _disc_fp(code):
    return hashlib.sha256(("tingxiao-disc:" + (code or "")).encode()).hexdigest()[:16]


def _sign(code, parts):
    msg = "|".join(str(p) for p in parts).encode("utf-8")
    return hmac.new((code or "").encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _safe_name(raw):
    name = os.path.basename(raw).replace("\\", "").replace("/", "")
    name = re.sub(r"[\x00-\x1f]", "", name).rstrip(" .")
    if not name:
        return "file.bin"
    stem, ext = os.path.splitext(name)
    if stem.upper() in _WIN_RESERVED:
        stem = "_" + stem
    name = stem + ext
    if len(name.encode("utf-8")) > 200:
        stem = stem.encode("utf-8")[:180].decode("utf-8", "ignore")
        name = stem + ext
    return name or "file.bin"


def _enc(v):
    return v.encode("utf-8").decode("latin-1") if v else ""


def _dec(v):
    try:
        return bytes(v, "latin-1").decode("utf-8")
    except Exception:
        return v


class TransferService:
    def __init__(self, cfg, on_incoming=None, on_peers=None, on_offer=None):
        c = cfg or {}
        self.name = c.get("device_name") or socket.gethostname()
        self.family_code = c.get("family_code", "")
        self.save_dir = os.path.expanduser(c.get("save_dir", "~/Downloads"))
        self.hist_dir = os.path.expanduser(c.get("hist_dir", os.path.join(self.save_dir, ".history")))
        self.disc_port = int(c.get("discovery_port", DISCOVERY_PORT))
        self.http_port = int(c.get("http_port", DEFAULT_HTTP_PORT))
        self.device_id = c.get("device_id") or uuid.uuid4().hex[:12]
        self.auto_accept = bool(c.get("auto_accept", False))

        self.on_incoming = on_incoming or (lambda *a: None)   # (kind, sender, payload)
        self.on_peers = on_peers or (lambda *a: None)
        self.on_offer = on_offer or (lambda *a: None)         # (offer_id, sender, name, size)
        self.error = ""
        self._peers = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._httpd = None
        self._ip = _local_ip()
        self._ip_ts = time.time()
        self._recv_sem = threading.Semaphore(MAX_CONCURRENT_RECV)
        self._seen_nonces = {}
        self._nonce_lock = threading.Lock()
        self._pending_offers = {}   # offer_id -> {event, accept}
        self._tickets = {}          # ticket -> {name,size,sender,exp}
        self._plock = threading.Lock()

    def local_ip(self):
        if time.time() - self._ip_ts > 30:
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
            os.makedirs(self.hist_dir, exist_ok=True)
        except Exception as e:
            self.error = f"快传保存目录不可写: {e}"
        threading.Thread(target=self._serve_http, daemon=True).start()
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()
        threading.Thread(target=self._reap_loop, daemon=True).start()
        print(f"[transfer] 快传就绪:{self.name} @ {self.local_ip()}:{self.http_port} 收件夹={self.save_dir}")

    def stop(self):
        self._stop.set()
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass

    def peers(self):
        with self._lock:
            return [dict(p) for p in self._peers.values()]

    # ---- 接收方决策(供 HTTP 线程阻塞等待 UI) ----

    def decide_offer(self, offer_id, sender, name, size):
        if self.auto_accept:
            return True
        ev = threading.Event()
        with self._plock:
            self._pending_offers[offer_id] = {"event": ev, "accept": False}
        try:
            self.on_offer(offer_id, sender, name, size)
        except Exception:
            pass
        ev.wait(OFFER_TIMEOUT)
        with self._plock:
            d = self._pending_offers.pop(offer_id, {"accept": False})
        return bool(d["accept"])

    def resolve_offer(self, offer_id, accept):
        with self._plock:
            d = self._pending_offers.get(offer_id)
            if d:
                d["accept"] = bool(accept)
                d["event"].set()

    def _issue_ticket(self, name, size, sender):
        t = uuid.uuid4().hex
        with self._plock:
            self._tickets[t] = {"name": name, "size": size, "sender": sender,
                                "exp": time.time() + TICKET_TTL}
        return t

    def _claim_ticket(self, ticket):
        with self._plock:
            d = self._tickets.pop(ticket, None)
        if d and d["exp"] > time.time():
            return d
        return None

    # ---- 历史 ----

    def log_history(self, kind, entry):
        try:
            entry = dict(entry)
            entry.setdefault("ts", time.strftime("%Y%m%d_%H%M%S"))
            with open(os.path.join(self.hist_dir, f"{kind}.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def history(self, kind, limit=100):
        path = os.path.join(self.hist_dir, f"{kind}.jsonl")
        if not os.path.isfile(path):
            return []
        out = []
        try:
            for line in open(path, encoding="utf-8").read().splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except Exception:
            return []
        return out[-limit:][::-1]

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
            self.error = f"发现端口 {self.disc_port} 被占用({e})"
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
            if changed:
                self.on_peers(self.peers())
        sock.close()

    def _reap_loop(self):
        while not self._stop.is_set():
            now = time.time()
            changed = False
            with self._lock:
                for k in [k for k, p in self._peers.items() if now - p["ts"] > PEER_TIMEOUT]:
                    del self._peers[k]
                    changed = True
            if changed:
                self.on_peers(self.peers())
            with self._nonce_lock:
                for n in [n for n, exp in self._seen_nonces.items() if exp < now]:
                    del self._seen_nonces[n]
            with self._plock:
                for t in [t for t, d in self._tickets.items() if d["exp"] < now]:
                    del self._tickets[t]
            self._stop.wait(2.0)

    def _check_nonce(self, nonce, ts):
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

    # ---- HTTP 服务 ----

    def _serve_http(self):
        service = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = 30

            def log_message(self, *a):
                pass

            def _deny(self, code=403, msg="deny"):
                try:
                    self.close_connection = True
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except Exception:
                    pass

            def _json(self, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self, parts, mac):
                if not hmac.compare_digest(mac, _sign(service.family_code, parts)):
                    return False
                return True

            def do_POST(self):
                h = self.headers
                nonce = h.get("X-Tingxiao-Nonce", "")
                ts = h.get("X-Tingxiao-Ts", "")
                mac = h.get("X-Tingxiao-Mac", "")
                sender = _dec(h.get("X-Tingxiao-From", "对方"))
                if not service._check_nonce(nonce, ts):
                    return self._deny(403, "replay/expired")

                if self.path == "/offer":
                    kind = h.get("X-Tingxiao-Kind", "file")
                    name = _dec(h.get("X-Tingxiao-Name", "file.bin"))
                    try:
                        size = int(h.get("X-Tingxiao-Size", 0))
                    except (TypeError, ValueError):
                        return self._deny(400)
                    if not self._auth(["offer", kind, h.get("X-Tingxiao-Name", ""),
                                       size, nonce, ts], mac):
                        return self._deny(403, "bad sig")
                    offer_id = uuid.uuid4().hex
                    accept = service.decide_offer(offer_id, sender, name, size)
                    ticket = service._issue_ticket(_safe_name(name), size, sender) if accept else ""
                    return self._json({"accept": accept, "ticket": ticket})

                if self.path == "/recv":
                    ticket = h.get("X-Tingxiao-Ticket", "")
                    kind = h.get("X-Tingxiao-Kind", "file")
                    try:
                        length = int(h.get("Content-Length", 0))
                    except (TypeError, ValueError):
                        return self._deny(400)
                    if length < 0:
                        return self._deny(400)
                    if kind == "text":
                        if not self._auth(["recv", "text", length, nonce, ts], mac):
                            return self._deny(403, "bad sig")
                        if length > MAX_TEXT_BYTES:
                            return self._deny(413)
                        body = self.rfile.read(min(length, MAX_TEXT_BYTES)).decode("utf-8", "replace")
                        self._json({"ok": True})
                        service.log_history("recv", {"kind": "text", "from": sender,
                                                     "text": body[:200]})
                        service.on_incoming("text", sender, body)
                        return
                    # 文件:必须有有效 ticket
                    if not self._auth(["recv", ticket, length, nonce, ts], mac):
                        return self._deny(403, "bad sig")
                    tk = service._claim_ticket(ticket)
                    if not tk or tk["size"] != length:
                        return self._deny(403, "bad ticket")
                    if length > MAX_FILE_BYTES:
                        return self._deny(413)
                    try:
                        if shutil.disk_usage(service.save_dir).free < length + DISK_MARGIN:
                            return self._deny(507)
                    except Exception:
                        pass
                    if not service._recv_sem.acquire(blocking=False):
                        return self._deny(503)
                    try:
                        self._recv_file(service, length, tk["name"], sender)
                    finally:
                        service._recv_sem.release()
                    return
                return self._deny(404)

            def _recv_file(self, service, length, fname, sender):
                fname = _safe_name(fname)
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
                self._json({"ok": True})
                service.log_history("recv", {"kind": "file", "from": sender,
                                             "name": os.path.basename(dst), "path": dst,
                                             "size": length})
                service.on_incoming("file", sender, dst)

        try:
            self._httpd = http.server.ThreadingHTTPServer(("", self.http_port), Handler)
        except OSError as e:
            self.error = f"接收端口 {self.http_port} 被占用({e})"
            print(f"[transfer] {self.error}")
            return
        self._httpd.serve_forever(poll_interval=0.5)

    # ---- 发送 ----

    def _req(self, peer, path, headers, body=None):
        import http.client

        conn = None
        try:
            conn = http.client.HTTPConnection(peer["ip"], peer["port"], timeout=45)
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data
        finally:
            if conn:
                conn.close()

    def _base_headers(self, extra_sign_parts, more):
        nonce = uuid.uuid4().hex
        ts = str(int(time.time()))
        h = {"X-Tingxiao-From": _enc(self.name),
             "X-Tingxiao-Nonce": nonce, "X-Tingxiao-Ts": ts}
        h.update(more)
        h["X-Tingxiao-Mac"] = _sign(self.family_code, extra_sign_parts + [nonce, ts])
        return h

    def send_file(self, peer, path, progress=None):
        size = os.path.getsize(path)
        name = os.path.basename(path)
        # 1) 询问
        try:
            oh = self._base_headers(
                ["offer", "file", _enc(name), size],
                {"X-Tingxiao-Kind": "file", "X-Tingxiao-Name": _enc(name),
                 "X-Tingxiao-Size": str(size), "Content-Length": "0"})
            st, data = self._req(peer, "/offer", oh)
            if st != 200:
                return False, f"对方无法接收(HTTP {st})"
            r = json.loads(data.decode("utf-8"))
            if not r.get("accept"):
                return False, "对方拒绝了,或未在45秒内确认"
            ticket = r.get("ticket", "")
        except Exception as e:
            return False, f"连接对方失败:{e}"
        # 2) 传字节
        try:
            rh = self._base_headers(
                ["recv", ticket, size],
                {"X-Tingxiao-Kind": "file", "X-Tingxiao-Ticket": ticket,
                 "X-Tingxiao-Name": _enc(name), "Content-Length": str(size),
                 "Content-Type": "application/octet-stream"})
            st, _d = self._req(peer, "/recv", rh, _FileReader(path, size, progress))
            if st != 200:
                return False, f"传输失败(HTTP {st})"
        except Exception as e:
            return False, f"传输出错:{e}"
        self.log_history("sent", {"kind": "file", "to": peer.get("name", "?"),
                                  "name": name, "size": size})
        return True, ""

    def send_text(self, peer, text):
        data = text.encode("utf-8")
        try:
            rh = self._base_headers(
                ["recv", "text", len(data)],
                {"X-Tingxiao-Kind": "text", "Content-Length": str(len(data)),
                 "Content-Type": "text/plain; charset=utf-8"})
            st, _d = self._req(peer, "/recv", rh, data)
            if st != 200:
                return False, f"发送失败(HTTP {st})"
        except Exception as e:
            return False, f"发送出错:{e}"
        self.log_history("sent", {"kind": "text", "to": peer.get("name", "?"),
                                  "text": text[:200]})
        return True, ""


class _FileReader:
    def __init__(self, path, size, progress=None):
        self._f = open(path, "rb")
        self._size = size
        self._sent = 0
        self._progress = progress
        self._last = 0.0

    def read(self, n=-1):
        chunk = self._f.read(1 << 20 if n < 0 else n)
        if chunk:
            self._sent += len(chunk)
            if self._progress:
                now = time.time()
                if now - self._last >= 0.12 or self._sent >= self._size:
                    self._last = now
                    try:
                        self._progress(self._sent, self._size)
                    except Exception:
                        pass
        else:
            self._f.close()
        return chunk

    def __len__(self):
        return self._size
