#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4D2 / Source 服务器 A2S 查询桥
================================================================
浏览器不能直接发 UDP，所以由这个小脚本替网页去查服务器，
再以 HTTP + JSON 的形式把结果交给网页。

启动：
    python l4d2_a2s_bridge.py                 # 默认 127.0.0.1:8765
    python l4d2_a2s_bridge.py --port 9000     # 改端口
    python l4d2_a2s_bridge.py --host 0.0.0.0  # 允许局域网 / 公网访问

接口：
    GET /ping                         -> {"ok":true,...}
    GET /query?addr=1.2.3.4:27015     -> 服务器信息 JSON
    GET /query?addr=host:port&players=0  -> 不查玩家列表（更快）

依赖：仅 Python 3.7+ 标准库，无需 pip install。
"""

import argparse
import json
import socket
import struct
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------- 协议常量
A2S_INFO_REQ = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
A2S_PLAYER_REQ = b"\xFF\xFF\xFF\xFFU\xFF\xFF\xFF\xFF"
SINGLE_HEADER = b"\xFF\xFF\xFF\xFF"

VERSION = "1.0"


class QueryError(Exception):
    pass


# ---------------------------------------------------------------- 底层收发
def _send_recv(sock, addr, payload, timeout):
    """发一个 UDP 包，收一个带 0xFFFFFFFF 头的合法响应。"""
    sock.settimeout(timeout)
    sock.sendto(payload, addr)
    deadline = time.time() + timeout
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            raise socket.timeout()
        sock.settimeout(remain)
        data, _ = sock.recvfrom(4096)
        if len(data) < 5 or not data.startswith(SINGLE_HEADER):
            continue  # 无关包，丢弃继续等
        if data[4] == 0xFE:  # 分片包，A2S_INFO 正常不会出现
            raise QueryError("收到分片包（该服协议异常）")
        return data


def _read_str(buf, i):
    end = buf.find(b"\x00", i)
    if end < 0:
        raise QueryError("字符串被截断")
    return buf[i:end].decode("utf-8", "replace"), end + 1


# ---------------------------------------------------------------- 解析
def parse_info(buf):
    """解析 A2S_INFO 响应体（已去掉 4 字节头 + 1 字节 'I'）。"""
    i = 0
    protocol = buf[i]; i += 1
    name, i = _read_str(buf, i)
    mapname, i = _read_str(buf, i)
    folder, i = _read_str(buf, i)
    game, i = _read_str(buf, i)
    appid = struct.unpack_from("<h", buf, i)[0]; i += 2
    players = buf[i]; i += 1
    max_players = buf[i]; i += 1
    bots = buf[i]; i += 1
    server_type = chr(buf[i]); i += 1
    env = chr(buf[i]); i += 1
    visibility = buf[i]; i += 1
    vac = buf[i]; i += 1
    version, i = _read_str(buf, i)

    info = {
        "protocol": protocol,
        "name": name,
        "map": mapname,
        "folder": folder,
        "game": game,
        "appid": appid,
        "players": players,
        "max_players": max_players,
        "bots": bots,
        "server_type": server_type,
        "env": env,
        "visibility": visibility,
        "vac": bool(vac),
        "version": version,
    }

    # 可选 EDF 扩展字段
    try:
        if i < len(buf):
            edf = buf[i]; i += 1
            if edf & 0x80 and i + 2 <= len(buf):
                info["game_port"] = struct.unpack_from("<H", buf, i)[0]; i += 2
            if edf & 0x10 and i + 8 <= len(buf):
                info["steamid"] = str(struct.unpack_from("<Q", buf, i)[0]); i += 8
            if edf & 0x40 and i + 2 <= len(buf):
                info["spectator_port"] = struct.unpack_from("<H", buf, i)[0]; i += 2
                info["spectator_name"], i = _read_str(buf, i)
            if edf & 0x20:
                info["keywords"], i = _read_str(buf, i)
    except Exception:
        pass  # 扩展字段残缺不影响主要信息
    return info


def query_players(sock, addr, timeout):
    """A2S_PLAYER：拿玩家名/时长。失败返回空列表，不影响主流程。"""
    try:
        data = _send_recv(sock, addr, A2S_PLAYER_REQ, timeout)
        if data[4] == 0x41:  # 挑战包，用返回的 challenge 重发
            data = _send_recv(sock, addr, b"\xFF\xFF\xFF\xFFU" + data[5:9], timeout)
        if data[4] != 0x44:  # 'D'
            return []
        count = data[5]
        i = 6
        out = []
        for _ in range(count):
            i += 1  # index
            pname, i = _read_str(data, i)
            score = struct.unpack_from("<i", data, i)[0]; i += 4
            duration = struct.unpack_from("<f", data, i)[0]; i += 4
            out.append({"name": pname, "score": score, "seconds": int(duration)})
        return out
    except Exception:
        return []


# ---------------------------------------------------------------- 单服查询
def query_server(addr_str, timeout=1.5, with_players=True):
    host, _, port_s = addr_str.rpartition(":")
    if not host or not port_s.isdigit():
        return {"ok": False, "error": "地址格式应为 host:port"}
    port = int(port_s)

    try:
        ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    except Exception as exc:
        return {"ok": False, "error": "域名解析失败: %s" % exc, "host": host, "port": port}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t0 = time.perf_counter()
    try:
        data = _send_recv(sock, ip, A2S_INFO_REQ, timeout)
        if data[4] == 0x41:  # 现代服务器要求带上挑战码再问一次
            data = _send_recv(sock, ip, A2S_INFO_REQ + data[5:9], timeout)
        if data[4] != 0x49:  # 'I'
            return {"ok": False, "error": "非 A2S_INFO 响应 (0x%02X)" % data[4],
                    "host": host, "port": port, "ip": ip}
        ping = int((time.perf_counter() - t0) * 1000)

        info = parse_info(data[5:])
        info.update({
            "ok": True,
            "host": host,
            "port": port,
            "ip": ip,
            "ping": ping,
            "playerlist": query_players(sock, ip, timeout) if with_players else [],
        })
        return info

    except socket.timeout:
        return {"ok": False, "error": "超时（UDP 无响应）", "host": host, "port": port, "ip": ip}
    except QueryError as exc:
        return {"ok": False, "error": str(exc), "host": host, "port": port, "ip": ip}
    except OSError as exc:
        return {"ok": False, "error": "网络错误: %s" % exc, "host": host, "port": port, "ip": ip}
    finally:
        sock.close()


# ---------------------------------------------------------------- HTTP 层
class Handler(BaseHTTPRequestHandler):
    server_version = "A2SBridge/" + VERSION
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path in ("/ping", "/"):
            return self._json({"ok": True, "service": "l4d2-a2s-bridge",
                               "version": VERSION, "time": int(time.time())})

        if parsed.path == "/query":
            addr = (qs.get("addr") or [""])[0].strip()
            if not addr:
                return self._json({"ok": False, "error": "缺少 addr 参数"}, 400)
            try:
                timeout = float((qs.get("timeout") or ["1.5"])[0])
                timeout = min(max(timeout, 0.3), 8.0)
            except ValueError:
                timeout = 1.5
            with_players = (qs.get("players") or ["1"])[0] != "0"
            return self._json(query_server(addr, timeout, with_players))

        return self._json({"ok": False, "error": "未知路径"}, 404)

    def log_message(self, fmt, *args):
        print("  [%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(description="L4D2 A2S 查询桥")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    ap.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 62)
    print("  L4D2 A2S 查询桥 已启动")
    print("  监听: http://%s:%d" % (args.host, args.port))
    print("  自检: http://%s:%d/ping" % (args.host, args.port))
    print("  现在可以打开 l4d2-status.html（保持本窗口不要关）")
    print("  停止: Ctrl + C")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        srv.server_close()


if __name__ == "__main__":
    main()
