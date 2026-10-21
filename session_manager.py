#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京审计大学校园网门户会话管理模块 (纯标准库实现, 跨平台, 自包含)
Project: NAU-AutoLogin
Location: session_manager.py

基于对门户自服务接口的实测 (2026-10, 认证服务器 10.255.254.23):

  GET  /api/selfservice/v1/online              在线会话列表 (支持 page/limit 分页)
       返回字段: id / user_ipv4(大端整数) / mac / area.name / acctstarttime(Unix 秒)
                acctsessiontime(秒) / acctinputoctets_ipv4 / acctoutputoctets_ipv4
                acctsessionid / username / service / nas_ip ...
  POST /api/selfservice/v1/online/kick/{id}    强制下线指定会话 (reply_code=0 成功)
  GET  /api/selfservice/v1/acctdetail/{YYYYMM} 历史会话详单 (sort_column/sort_direction/page/limit)
  GET  /api/selfservice/v1/authlog             认证日志 (page/limit)

实测结论:
  1. 以上接口均无需登录态/Cookie (服务端按来源推断账号), HTTP 直连即可;
  2. kick 对不存在的 id 返回 reply_code=404 "无此记录", 参数非法返回 400;
  3. 流量判活数据现成: online 列表自带上下行字节计数, 两次采样算增量,
     delta=0 且上线时间较久的会话即为"幽灵会话"(设备已离开但未注销)。

设计说明:
  本模块不 import nau_autologin (.pyw 在非 Windows 平台不可 import, 且守护进程
  以 __main__ 运行时懒加载会造成双重模块实例), 因此自带最小 HTTP 层与日志回调。
  守护进程通过 cleanup_stale_sessions(..., logger=log) 注入自己的日志函数。

安全设计:
  kick_by_ip() 只踢"本账号在线列表中出现"的会话。若旧 IP 已被 DHCP 回收
  分配给其他设备, 该 IP 不会出现在本账号列表中, 自动跳过, 杜绝误伤陌生人。
"""

import os
import sys
import json
import socket
import struct
import urllib.request
import urllib.error
import http.client
from datetime import datetime


# ==================== 最小 HTTP 层 (与 nau_autologin.http_request 同语义) ====================

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拦截 302/301 探测重定向"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_request(url, source_ip=None, data=None, headers=None, timeout=8, allow_redirects=True):
    """统一 HTTP 请求，绕过本地系统代理; data=b"" 表示空体 POST"""
    handlers = [urllib.request.ProxyHandler({})]
    if not allow_redirects:
        handlers.append(NoRedirectHandler())
    if source_ip:
        class BoundHTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(
                    lambda host, t: http.client.HTTPConnection(host, timeout=t,
                                                               source_address=(source_ip, 0)), req)

        class BoundHTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(
                    lambda host, t: http.client.HTTPSConnection(host, timeout=t,
                                                                source_address=(source_ip, 0)), req)
        handlers.append(BoundHTTPHandler())
        handlers.append(BoundHTTPSHandler())
    opener = urllib.request.build_opener(*handlers)

    req_headers = {"User-Agent": "NAU-AutoLogin/2.0"}
    if headers:
        req_headers.update(headers)

    payload = None
    if data is not None:
        if isinstance(data, (dict, list)):
            payload = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        elif isinstance(data, str):
            payload = data.encode("utf-8")
        elif isinstance(data, bytes):
            payload = data

    req = urllib.request.Request(url, data=payload, headers=req_headers)
    with opener.open(req, timeout=timeout) as resp:
        status_code = resp.getcode() if hasattr(resp, "getcode") else getattr(resp, "status", 200)
        final_url = resp.geturl() if hasattr(resp, "geturl") else url
        return status_code, resp.read().decode("utf-8", errors="replace"), final_url


def get_default_user_dir():
    """用户家目录下的部署配置目录 (与守护进程保持一致)"""
    user_home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    user_dir = os.path.join(user_home, ".nau-autologin")
    try:
        os.makedirs(user_dir, exist_ok=True)
    except Exception:
        pass
    return user_dir


def _default_logger(level, msg):
    """无注入日志函数时的降级输出 (stderr, 不写文件)"""
    print(f"[{level}] {msg}", file=sys.stderr)


# ==================== 数据结构与工具 ====================

class PortalAPIError(Exception):
    """门户接口返回非 0 的业务错误"""

    def __init__(self, code, msg):
        super().__init__(f"portal api error (reply_code={code}): {msg}")
        self.code = code
        self.msg = msg


def long2ip(val):
    """门户使用的大端整数 IP -> 点分十进制字符串"""
    if val is None:
        return None
    try:
        return socket.inet_ntoa(struct.pack(">I", int(val)))
    except Exception:
        return str(val)


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def human_duration(sec):
    sec = int(sec or 0)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return f"{d}天{h}时{m}分"
    if h:
        return f"{h}时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


class SessionInfo:
    """一条在线会话的结构化视图"""

    __slots__ = ("sid", "ip", "mac", "username", "area", "area_type",
                 "login_time", "duration", "rx_bytes", "tx_bytes",
                 "acct_session_id", "raw")

    def __init__(self, row):
        self.sid = row.get("id")
        self.ip = long2ip(row.get("user_ipv4"))
        self.mac = (row.get("mac") or "").lower()
        self.username = row.get("username")
        area = row.get("area") or {}
        self.area = area.get("name") or ""
        self.area_type = row.get("area_type")
        ts = row.get("acctstarttime")
        self.login_time = datetime.fromtimestamp(ts) if ts else None
        self.duration = row.get("acctsessiontime") or 0
        self.rx_bytes = row.get("acctinputoctets_ipv4") or 0
        self.tx_bytes = row.get("acctoutputoctets_ipv4") or 0
        self.acct_session_id = row.get("acctsessionid")
        self.raw = row

    @property
    def total_bytes(self):
        return (self.rx_bytes or 0) + (self.tx_bytes or 0)

    def to_dict(self):
        return {
            "id": self.sid,
            "ip": self.ip,
            "mac": self.mac,
            "username": self.username,
            "area": self.area,
            "login_time": self.login_time.strftime("%Y-%m-%d %H:%M:%S") if self.login_time else None,
            "duration_sec": self.duration,
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "acct_session_id": self.acct_session_id,
        }

    def __repr__(self):
        lt = self.login_time.strftime("%m-%d %H:%M:%S") if self.login_time else "N/A"
        return (f"<Session id={self.sid} ip={self.ip} mac={self.mac} area={self.area} "
                f"login={lt} dur={self.duration}s rx={self.rx_bytes} tx={self.tx_bytes}>")


# ==================== 门户会话管理客户端 ====================

class PortalSessionManager:
    """门户会话管理: 列表查询 / 按 IP 或 id 强制下线 / 历史详单 / 流量快照"""

    def __init__(self, portal_ip, timeout=8):
        self.portal_ip = portal_ip
        self.timeout = timeout

    # -------------------- 查询 --------------------

    def list_sessions(self, page_size=100, max_pages=20):
        """返回账号下全部在线会话 [SessionInfo, ...] (自动翻页)"""
        sessions = []
        for page in range(1, max_pages + 1):
            url = (f"http://{self.portal_ip}/api/selfservice/v1/online"
                   f"?page={page}&limit={page_size}")
            _, body, _ = http_request(url, timeout=self.timeout)
            data = json.loads(body)
            self._check(data)
            results = data.get("results") or {}
            rows = results.get("rows") or []
            sessions.extend(SessionInfo(r) for r in rows)
            total = results.get("total")
            if not rows or len(rows) < page_size:
                break
            if isinstance(total, int) and len(sessions) >= total:
                break
        return sessions

    def find_by_ip(self, ip):
        for s in self.list_sessions():
            if s.ip == ip:
                return s
        return None

    def find_by_mac(self, mac):
        mac = (mac or "").lower()
        for s in self.list_sessions():
            if s.mac == mac:
                return s
        return None

    def traffic_snapshot(self):
        """{ip: (rx_bytes, tx_bytes, duration_sec, login_time)} 当前流量快照"""
        snap = {}
        for s in self.list_sessions():
            snap[s.ip] = (s.rx_bytes, s.tx_bytes, s.duration, s.login_time)
        return snap

    # -------------------- 下线 --------------------

    def kick(self, session_id):
        """按会话 id 强制下线, 返回 (ok, msg)"""
        url = f"http://{self.portal_ip}/api/selfservice/v1/online/kick/{int(session_id)}"
        # data=b"" -> 空体 POST (与网页版 $.post(url) 行为一致)
        _, body, _ = http_request(url, data=b"", timeout=self.timeout)
        try:
            data = json.loads(body)
        except Exception:
            return False, f"kick 响应无法解析: {body[:120]}"
        code = data.get("reply_code")
        if code == 0:
            return True, "ok"
        return False, data.get("reply_msg") or f"reply_code={code}"

    def kick_by_ip(self, ip):
        """按 IP 下线: 先定位属于本账号的会话 id, 再执行 kick"""
        s = self.find_by_ip(ip)
        if s is None:
            return False, f"{ip} 无在线会话 (可能已超时自动清除)"
        return self.kick(s.sid)

    def kick_all(self, exclude_ips=None):
        """下线全部会话, exclude_ips 中的 IP 跳过. 返回 {ip: (ok, msg)}"""
        exclude = set(exclude_ips or [])
        result = {}
        for s in self.list_sessions():
            if s.ip in exclude:
                result[s.ip] = (True, "skipped (excluded)")
                continue
            result[s.ip] = self.kick(s.sid)
        return result

    # -------------------- 历史与日志 --------------------

    def acct_detail(self, month=None, limit=50):
        """历史会话详单 (month: 'YYYYMM', 默认当月)"""
        month = month or datetime.now().strftime("%Y%m")
        url = (f"http://{self.portal_ip}/api/selfservice/v1/acctdetail/{month}"
               f"?sort_column=acctstoptime&sort_direction=desc&page=1&limit={limit}")
        _, body, _ = http_request(url, timeout=self.timeout)
        data = json.loads(body)
        self._check(data)
        return (data.get("results") or {}).get("rows") or []

    def auth_log(self, limit=50):
        url = (f"http://{self.portal_ip}/api/selfservice/v1/authlog"
               f"?page=1&limit={limit}")
        _, body, _ = http_request(url, timeout=self.timeout)
        data = json.loads(body)
        self._check(data)
        return (data.get("results") or {}).get("rows") or []

    @staticmethod
    def _check(data):
        code = data.get("reply_code")
        if code != 0:
            raise PortalAPIError(code, data.get("reply_msg") or "")


# ==================== 本地状态与 IP 交接清理 ====================

def default_state_path():
    return os.path.join(get_default_user_dir(), "state.json")


def load_state(state_path):
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state_path, state):
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_path)
    except Exception as e:
        _default_logger("WARN", f"[会话交接] 状态文件写入失败: {e}")


def cleanup_stale_sessions(adapter_key, current_ip, settings, logger=None):
    """
    IP 交接清理 (替代原"白名单"方案):

    守护进程为每个网卡在本地 state.json 中记录 recent_ips (最近 N 个使用过的 IP)。
    当本次登录的 IP 与记录不一致时, 说明发生过 IP 变更 (DHCP 续租/AP 优选重连/
    睡眠唤醒/重启), 此时把 recent_ips 中所有旧 IP 在门户上的残留会话强制下线,
    然后更新本地记录。

    参数:
      adapter_key  -- 网卡标识, 如 "WLAN" / "以太网 2" (有线与无线各自独立记账)
      current_ip   -- 本次确认可用的 IP
      settings     -- 配置字典, 读取 PORTAL_IP / ENABLE_SESSION_CLEANUP /
                      RECENT_IPS_MAX / STATE_PATH
      logger       -- 日志回调 logger(level, msg), 守护进程传入自己的 log()

    安全约束:
      - 只清理"本账号在线列表中存在"的旧 IP 会话, 绝不会误伤其他设备;
      - 当前 IP 永远在排除列表外, 不会自己踢自己;
      - 稳态下 IP 未变化直接返回, 零 HTTP 开销;
      - 任何异常只记日志, 不影响保活主流程。
    """
    log_fn = logger or _default_logger

    if not settings.get("ENABLE_SESSION_CLEANUP", True):
        return
    if not current_ip:
        return

    portal_ip = settings.get("PORTAL_IP")
    state_path = settings.get("STATE_PATH") or default_state_path()
    try:
        max_ips = int(settings.get("RECENT_IPS_MAX", 5) or 5)
    except Exception:
        max_ips = 5

    try:
        state = load_state(state_path)
        prev = state.get(adapter_key) or {}
        recent = [ip for ip in (prev.get("recent_ips") or []) if ip]

        # IP 未变化 -> 直接返回 (稳态零开销, 不发任何请求)
        if prev.get("current_ip") == current_ip:
            return

        stale = [ip for ip in recent if ip != current_ip]
        if stale:
            mgr = PortalSessionManager(portal_ip)
            try:
                online = {s.ip: s.sid for s in mgr.list_sessions()}
            except Exception as e:
                log_fn("WARN", f"[会话交接] 查询在线会话失败, 本次跳过清理: {e}")
                online = None
            if online is not None:
                for old_ip in stale:
                    sid = online.get(old_ip)
                    if sid is None:
                        log_fn("INFO", f"[会话交接] 旧 IP {old_ip} 无在线会话 (已超时或被清理), 跳过")
                        continue
                    ok, msg = mgr.kick(sid)
                    if ok:
                        log_fn("INFO", f"[会话交接] 已强制下线旧 IP 残留会话 {old_ip} (id={sid})")
                    else:
                        log_fn("WARN", f"[会话交接] 下线旧 IP {old_ip} 失败: {msg}")

        # 更新本地记录: 当前 IP 置顶, 去重, 保留最近 max_ips 个
        recent = [current_ip] + [ip for ip in recent if ip != current_ip]
        state[adapter_key] = {
            "current_ip": current_ip,
            "recent_ips": recent[:max_ips],
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_state_quiet(state_path, state, log_fn)
    except Exception as e:
        log_fn("WARN", f"[会话交接] 清理流程异常 (不影响主流程): {e}")


def _save_state_quiet(state_path, state, log_fn):
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_path)
    except Exception as e:
        log_fn("WARN", f"[会话交接] 状态文件写入失败: {e}")
