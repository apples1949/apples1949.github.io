#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4D2 服务器状态采集器
================================================================
由 GitHub Actions 定时执行：
  1. 直接发 UDP 的 A2S_INFO / A2S_PLAYER 去查（能拿到真实人数、地图、玩家名）
  2. UDP 不通时（个别网络环境限制），自动退回 Steam Web API
  3. 结果写成 status.json，交给静态页面读取

本地也能跑：
    python3 tools/fetch_status.py --servers servers.json --out site/status.json

依赖：仅标准库。Steam 回退需要环境变量 STEAM_API_KEY（可选）。
"""

import argparse
import json
import os
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Windows 控制台默认 GBK，重定向时中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

A2S_INFO_REQ = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
A2S_PLAYER_REQ = b"\xFF\xFF\xFF\xFFU\xFF\xFF\xFF\xFF"
STEAM_API = "https://api.steampowered.com/IGameServersService/GetServerList/v1/"


# ------------------------------------------------------------------ A2S
def _send_recv(sock, addr, payload, timeout):
    sock.settimeout(timeout)
    sock.sendto(payload, addr)
    deadline = time.time() + timeout
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            raise socket.timeout()
        sock.settimeout(remain)
        data, _ = sock.recvfrom(4096)
        if len(data) < 5 or not data.startswith(b"\xFF\xFF\xFF\xFF"):
            continue
        if data[4] == 0xFE:
            raise RuntimeError("收到分片包")
        return data


def _read_str(buf, i):
    end = buf.find(b"\x00", i)
    if end < 0:
        raise RuntimeError("字符串截断")
    return buf[i:end].decode("utf-8", "replace"), end + 1


def _parse_info(buf):
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
    return {
        "server_name": name, "map": mapname, "folder": folder, "game": game,
        "appid": appid, "players": players, "max_players": max_players,
        "bots": bots, "vac": bool(vac), "version": version,
        "passworded": bool(visibility), "dedicated": server_type == "d",
    }


def _players(sock, addr, timeout):
    try:
        data = _send_recv(sock, addr, A2S_PLAYER_REQ, timeout)
        if data[4] == 0x41:
            data = _send_recv(sock, addr, b"\xFF\xFF\xFF\xFFU" + data[5:9], timeout)
        if data[4] != 0x44:
            return []
        count = data[5]
        i = 6
        out = []
        for _ in range(count):
            i += 1
            pname, i = _read_str(data, i)
            score = struct.unpack_from("<i", data, i)[0]; i += 4
            dur = struct.unpack_from("<f", data, i)[0]; i += 4
            out.append({"name": pname, "score": score, "seconds": int(dur)})
        return out
    except Exception:
        return []


def a2s_query(host, port, timeout=1.2, want_players=True, attempts=3):
    ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0]
    target = (ip, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    info = None
    last_err = None
    t0 = time.perf_counter()

    try:
        # UDP 会丢包，重试几次再判死
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                data = _send_recv(sock, target, A2S_INFO_REQ, timeout)
                if data[4] == 0x41:  # 挑战包，带上 challenge 再问一次
                    data = _send_recv(sock, target, A2S_INFO_REQ + data[5:9], timeout)
                if data[4] != 0x49:  # 明确的非 INFO 响应，重试也没用
                    raise RuntimeError("非 A2S_INFO 响应 (0x%02X)" % data[4])
                info = _parse_info(data[5:])
                break
            except (socket.timeout, OSError) as exc:
                last_err = exc
                if attempt < attempts - 1:
                    time.sleep(0.3)

        if info is None:
            raise last_err or RuntimeError("查询失败")

        info["ping"] = int((time.perf_counter() - t0) * 1000)
        info["players_list"] = _players(sock, target, timeout) if want_players else []
        info["source"] = "a2s"
        info["ok"] = True
        return info
    finally:
        sock.close()


# ------------------------------------------------------------------ Steam 回退
def steam_query(addr, key, timeout=8):
    flt = "\\addr\\" + addr
    qs = urllib.parse.urlencode({"key": key, "filter": flt, "limit": 1})
    req = urllib.request.Request(STEAM_API + "?" + qs, headers={"User-Agent": "l4d2-status/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    servers = (payload.get("response") or {}).get("servers") or []
    if not servers:
        raise RuntimeError("Steam 主列表里查不到该服")
    s = servers[0]
    return {
        "ok": True,
        "source": "steam",
        "server_name": s.get("name", ""),
        "map": s.get("map", ""),
        "folder": s.get("gamedir", ""),
        "game": s.get("product", ""),
        "appid": s.get("appid", 0),
        "players": s.get("players", 0),
        "max_players": s.get("max_players", 0),
        "bots": s.get("bots", 0),
        "vac": bool(s.get("secure", False)),
        "version": s.get("version", ""),
        "passworded": False,
        "ping": None,
        "players_list": [],
    }


# ------------------------------------------------------------------ 主流程
def split_addr(addr):
    host, _, port = addr.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("地址格式应为 host:port")
    return host, int(port)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--servers", default=os.path.join(here, "servers.json"))
    ap.add_argument("--out", default=os.path.join(here, "status.json"))
    ap.add_argument("--timeout", type=float, default=1.2)
    ap.add_argument("--refresh-minutes", type=int, default=10)
    ap.add_argument("--no-players", action="store_true", help="不查玩家列表，快一点")
    args = ap.parse_args()

    with open(args.servers, "r", encoding="utf-8") as f:
        servers = json.load(f)

    key = os.environ.get("STEAM_API_KEY", "").strip()
    want_players = not args.no_players
    results = []

    for item in servers:
        name = item.get("name") or item["addr"]
        addr = item["addr"]
        entry = {"name": name, "addr": addr}
        t0 = time.time()

        try:
            host, port = split_addr(addr)
            info = a2s_query(host, port, args.timeout, want_players)
            entry.update(info)
            entry["ip"] = socket.gethostbyname(host)
            print("[A2S  ] %-8s %-24s %s  %s/%s  %s" % (
                name, addr, info.get("server_name"), info.get("players"),
                info.get("max_players"), info.get("map")))

        except Exception as exc:
            a2s_err = str(exc) or exc.__class__.__name__
            restored = False

            if key:
                try:
                    info = steam_query(addr, key)
                    entry.update(info)
                    restored = True
                    print("[STEAM] %-8s %-24s %s  %s/%s" % (
                        name, addr, info.get("server_name"), info.get("players"),
                        info.get("max_players")))
                except Exception as exc2:
                    a2s_err += " / Steam: " + (str(exc2) or exc2.__class__.__name__)

            if not restored:
                entry.update({
                    "ok": False,
                    "source": "none",
                    "error": a2s_err,
                    "server_name": "", "map": "",
                    "players": 0, "max_players": 0, "bots": 0,
                    "players_list": [], "vac": False, "version": "",
                })
                print("[FAIL ] %-8s %-24s %s" % (name, addr, a2s_err))

        entry["checked_at_ms"] = int(time.time() * 1000)
        entry["elapsed_ms"] = int((time.time() - t0) * 1000)
        results.append(entry)

    online = sum(1 for r in results if r.get("ok"))
    sources = sorted({r.get("source") for r in results if r.get("ok")})

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_ms": int(time.time() * 1000),
        "refresh_minutes": args.refresh_minutes,
        "runner": os.environ.get("GITHUB_RUN_ID", "local"),
        "online": online,
        "total": len(results),
        "sources": sources,
        "servers": results,
    }

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print("---")
    print("在线 %d/%d  数据源 %s  ->  %s" % (online, len(results), ",".join(sources) or "无", args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
