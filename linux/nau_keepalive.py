#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京审计大学校园网门户保活守护 (Linux 最小实现, 纯标准库)
Project: NAU-AutoLogin
Location: linux/nau_keepalive.py

场景: 树莓派 dc-pi 等单 wlan0 链路设备 (SSID i-NAU)。双网卡切换、AP 优选、
Win32 单实例互斥体均为 Windows 场景专属, 此处不涉及; 单实例由 systemd 保证。

职责链 (对齐 issue#1 最小可用范围):
  探测外网 (204 探针超时/被门户 302 劫持 => 判定掉线) -> 门户登录
  -> 成功后按 check_interval 周期保活; 被踢下线后一个周期内自动重登。

认证后端 (auth_type, 2026-11 网络改造后南审切换为 Dr.COM 哆点):
  drcom  -- Dr.COM 4.x Portal: POST /drcom/login, 表单 DDDDD/upass/v4ip;
            成功页 Dr.COMWebLoginID_3.htm, 失败页 _2.htm; 重复登录含 "clientip online"
  legacy -- 旧自服务门户 POST /api/portal/v1/login (reply_code=0 成功), 保留兼容
  未显式配置时按"探测门户首页指纹"自动选择 (Dr.COM 特征: DrcomServer1.0 / 1.htm~3.htm)。

复用: session_manager.PortalSessionManager (登录后核验在线列表) 与其最小
HTTP 层 (与 nau_autologin.http_request 同语义, 自动绕过系统代理)。

配置: config.ini 仅含 portal_ip / check_interval / detect_url;
凭据走 .env (NAU_USERNAME / NAU_PASSWORD, 与根目录 .env.example 字段对齐)。
日志: stdout -> journald (systemd 接管, 轮转交给 journald, 不自建文件日志)。

用法:
  python3 nau_keepalive.py            # 守护常驻 (systemd 调用)
  python3 nau_keepalive.py --once     # 单轮检测+登录 (install.sh 健康检查/手动测试)
"""

import sys
import os
import json
import time
import enum
import socket
import argparse
import configparser
import http.client
import urllib.parse
import urllib.error
from pathlib import Path
from typing import Final, Sequence
from dataclasses import dataclass
from datetime import datetime

# session_manager 位于仓库根目录; 兼容两种落位: 仓库内 linux/ 子目录运行,
# 或安装后与本文件同目录的平铺布局。
_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from session_manager import PortalSessionManager, PortalAPIError, SessionInfo, http_request  # noqa: E402

DEFAULT_PORTAL_IP: Final = "10.255.252.12"
DEFAULT_AUTH_TYPE: Final = "auto"     # auto | drcom | legacy
DRCOM_LOGIN_PATH: Final = "/drcom/login"
DRCOM_PROBE_MARKERS: Final = ("DrcomServer", "哆点", "eportal")
DEFAULT_DETECT_URL: Final = "http://connect.rom.miui.com/generate_204"
DEFAULT_CHECK_INTERVAL: Final = 30
MIN_CHECK_INTERVAL: Final = 5
MAX_BACKOFF_SECONDS: Final = 300
LOGIN_TIMEOUT: Final = 8
PROBE_TIMEOUT: Final = 8
HEARTBEAT_EVERY: Final = 20  # 保活稳态下每隔多少周期输出一次心跳日志 (避免刷屏 journal)


class MissingCredentialError(RuntimeError):
    """NAU_USERNAME / NAU_PASSWORD 未配置 (拒绝以空凭据启动)"""


class CycleResult(enum.Enum):
    """单轮保活结果"""
    ONLINE = "online"            # 外网探测通过, 链路健康
    LOGIN_OK = "login_ok"        # 本轮触发了登录且成功
    ALREADY_ONLINE = "already_online"  # 门户提示该 IP 已在线 (等价健康)
    LOGIN_FAIL = "login_fail"    # 触发登录但失败
    NO_LINK = "no_link"          # 无到门户的路由 (wlan 未连上/链路未就绪)


class AuthType(enum.Enum):
    """认证后端类型 (drcom = 哆点 4.x, legacy = 旧自服务门户)"""
    DRCOM = "drcom"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class Settings:
    """运行配置 (config.ini 策略参数 + .env 凭据, 解析后不可变)"""
    portal_ip: str
    check_interval: int
    detect_url: str
    domain: str
    username: str
    password: str
    auth_type: str = DEFAULT_AUTH_TYPE
    portal_port: int = 80


def log(level, msg):
    """stdout 日志 (journald 接管轮转); flush 保证 systemd 下实时可见"""
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}", flush=True)


# ==================== 配置与凭据加载 ====================

def _strip_env_value(raw):
    """剥除 .env 值的首尾引号或行内注释 (与 Windows 版 load_env 同规则)"""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("\"", "'"):
        return raw[1:-1]
    return raw.split("#", 1)[0].strip()


def load_env():
    """加载 .env: 脚本目录 -> 仓库根 -> ~/.nau-autologin, 首个命中即止;
    已存在的环境变量优先, 不覆盖 (与 Windows 版一致)。"""
    candidates = [_HERE / ".env", _HERE.parent / ".env",
                  Path.home() / ".nau-autologin" / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), _strip_env_value(value.strip())
            if key and key not in os.environ:
                os.environ[key] = value
        return


def load_settings(config_path=None):
    """config.ini ([portal]) + 环境变量 -> Settings; 缺凭据抛 MissingCredentialError"""
    load_env()
    cfg = configparser.ConfigParser()
    paths = [config_path] if config_path else [
        _HERE / "config.ini", Path.home() / ".nau-autologin" / "config.ini"]
    for path in paths:
        if path is not None and path.is_file():
            cfg.read(path, encoding="utf-8")
            break
    portal_ip = (cfg.get("portal", "portal_ip", fallback="")
                 or os.environ.get("NAU_PORTAL_IP", "")).strip() or DEFAULT_PORTAL_IP
    detect_url = (cfg.get("portal", "detect_url", fallback="")
                  or os.environ.get("NAU_DETECT_URL", "")).strip() or DEFAULT_DETECT_URL
    interval = cfg.getint("portal", "check_interval", fallback=DEFAULT_CHECK_INTERVAL)
    auth_type = (cfg.get("portal", "auth_type", fallback="")
                 or os.environ.get("NAU_AUTH_TYPE", "")).strip().lower() or DEFAULT_AUTH_TYPE
    portal_port = cfg.getint("portal", "portal_port", fallback=80)
    username = os.environ.get("NAU_USERNAME", "").strip()
    password = os.environ.get("NAU_PASSWORD", "").strip()
    if not username or not password:
        raise MissingCredentialError(
            "NAU_USERNAME / NAU_PASSWORD 未配置: 请复制 .env.example 为 .env 填写学号密码")
    return Settings(
        portal_ip=portal_ip,
        detect_url=detect_url,
        check_interval=max(MIN_CHECK_INTERVAL, interval),  # 防配置手误造成请求风暴
        domain=os.environ.get("NAU_DOMAIN", "default").strip() or "default",
        username=username,
        password=password,
        auth_type=auth_type,
        portal_port=portal_port,
    )


# ==================== 探测 / 登录 / 核验 ====================

def detect_local_ip(portal_ip):
    """UDP connect 探测到门户的本机出口 IP (不发包); 无路由返回 None"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((portal_ip, 9))
            return sock.getsockname()[0]
    except OSError as e:
        log("WARN", f"获取本机出口 IP 失败 (wlan 未连上或无路由): {e}")
        return None


def probe_internet(detect_url, portal_ip, timeout=PROBE_TIMEOUT):
    """外网连通探测 (带重定向拦截): 204=通; 200 且正文无门户特征=通;
    302/超时/异常=被门户劫持或断网。返回 (ok, reason)。"""
    try:
        status, body, _ = http_request(detect_url, timeout=timeout, allow_redirects=False)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} (302 劫持到门户?)"
    except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as e:
        return False, f"探测不可达: {e}"
    if status == 204:
        return True, "probe 204 OK"
    if status == 200 and portal_ip not in body and "portal" not in body.lower():
        return True, "probe 200 OK"
    return False, f"captive portal 页面特征 (HTTP {status})"


def detect_auth_type(portal_ip, configured=DEFAULT_AUTH_TYPE, portal_port=80):
    """确定认证后端: 显式配置优先; auto 时按门户首页指纹探测。
    Dr.COM 的特征是 1.htm/3.htm 页面族与 'Dr.COMWebLoginID' 标记 (正文),
    Server 头 DrcomServer1.0 仅作补充判断。"""
    if configured in (AuthType.DRCOM.value, AuthType.LEGACY.value):
        return AuthType(configured)
    base = _portal_base(portal_ip, portal_port)
    try:
        _, body, _ = http_request(f"{base}/", timeout=5)
    except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError):
        # 首页探测失败时不臆断, 交给登录阶段按响应特征判定
        return AuthType.DRCOM
    if any(marker in body for marker in DRCOM_PROBE_MARKERS):
        return AuthType.DRCOM
    return AuthType.LEGACY


def _portal_base(portal_ip, portal_port=80):
    """门户基址; 默认 80 端口时不带冒号, 保持 URL 可读"""
    return f"http://{portal_ip}" if portal_port == 80 else f"http://{portal_ip}:{portal_port}"


def _drcom_status(portal_ip, portal_port=80):
    """Dr.COM 3.htm 认证成功页特征: Gno=03 / Dr.COMWebLoginID_3.htm。
    返回 (online, detail)。页面不可达视为未知, 交由 204 探针判断。"""
    try:
        _, body, _ = http_request(f"{_portal_base(portal_ip, portal_port)}/3.htm", timeout=5)
    except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as e:
        return None, f"3.htm 不可达: {e}"
    if "Dr.COMWebLoginID_3" in body or "Gno=03" in body:
        return True, "Dr.COM 认证成功页 (3.htm)"
    return False, "Dr.COM 未认证"


def _drcom_login(settings):
    """POST /drcom/login (移植自门户 JS: DDDDD/upass/0MKKey/v4ip)。
    成功/失败以返回的 Dr.COMWebLoginID_3.htm / _2.htm 判定;
    'clientip online' 表示该出口 IP 已有会话, 视为已在线。返回 (ok, msg, already)。"""
    v4ip = detect_local_ip(settings.portal_ip) or ""
    base = _portal_base(settings.portal_ip, settings.portal_port)
    url = f"{base}{DRCOM_LOGIN_PATH}"
    payload = {
        "DDDDD": settings.username,
        "upass": settings.password,
        "0MKKey": "123456",
        "R1": "0", "R2": "", "R3": "0", "R6": "0", "para": "00",
        "v4ip": v4ip, "v6ip": "", "terminal_type": "1", "lang": "zh",
    }
    # 必须 form-urlencode: http_request 对 dict 会走 JSON 编码, Dr.COM 不认
    body = urllib.parse.urlencode(payload)
    try:
        _, body, _ = http_request(url, data=body, timeout=LOGIN_TIMEOUT,
                                  headers={"Content-Type": "application/x-www-form-urlencoded",
                                           "Referer": f"{base}/0.htm"})
    except (urllib.error.URLError, http.client.HTTPException, OSError,
            TimeoutError, ValueError) as e:
        return False, f"login request error: {e}", False
    text = body if isinstance(body, str) else body.decode("gbk", "replace")
    if "clientip online" in text:
        return True, f"出口 IP {v4ip or '?'} 已在线 (clientip online), 无需重复登录", True
    if "Dr.COMWebLoginID_3" in text:
        return True, f"Dr.COM login success (v4ip={v4ip})", False
    msga = ""
    marker = "msga='"
    if marker in text:
        msga = text.split(marker, 1)[1].split("'", 1)[0]
    if msga:
        return False, f"Dr.COM login rejected: {msga}", False
    return False, "Dr.COM login rejected (无 3.htm 成功标志)", False


def _legacy_login(settings):
    """旧自服务门户 POST /api/portal/v1/login (reply_code=0 成功)。返回 (ok, msg, already)。"""
    url = f"http://{settings.portal_ip}/api/portal/v1/login"
    payload = {
        "domain": settings.domain,
        "username": settings.username,
        "password": settings.password,
    }
    try:
        _, body, _ = http_request(url, data=payload, timeout=LOGIN_TIMEOUT)
        resp = json.loads(body)
    except (urllib.error.URLError, http.client.HTTPException, OSError,
            TimeoutError, json.JSONDecodeError, ValueError) as e:
        return False, f"login request error: {e}", False
    rc = resp.get("reply_code")
    if rc == 0:
        user = (resp.get("results") or {}).get("username") or settings.username
        return True, f"login success (reply_code=0, user={user})", False
    msg = resp.get("reply_msg") or ""
    already = "已在线" in msg or "online" in msg.lower()
    return False, f"login rejected (reply_code={rc}, msg={msg})", already


def portal_login(settings):
    """门户登录统一入口, 按 auth_type 分发。单链路无需绑定源 IP, 门户按来源 IP 关联会话。
    返回 (ok, msg, already_online)。"""
    auth_type = detect_auth_type(settings.portal_ip, settings.auth_type, settings.portal_port)
    if auth_type is AuthType.DRCOM:
        return _drcom_login(settings)
    return _legacy_login(settings)


def verify_portal_session(settings, local_ip):
    """核验本次登录在门户侧的真实效果。
    Dr.COM 走认证成功页特征; 旧门户复用 PortalSessionManager 查在线列表。
    任何异常只记入返回值, 不外抛。"""
    auth_type = detect_auth_type(settings.portal_ip, settings.auth_type, settings.portal_port)
    if auth_type is AuthType.DRCOM:
        online, detail = _drcom_status(settings.portal_ip, settings.portal_port)
        if online is True:
            return f"门户已确认在线: {detail} (ip={local_ip})"
        return f"门户尚未确认在线 ({detail}), 以下一周期为准"
    try:
        session = PortalSessionManager(settings.portal_ip).find_by_ip(local_ip)
    except (PortalAPIError, urllib.error.URLError, http.client.HTTPException,
            OSError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        return f"在线列表查询失败 (不影响登录结果): {e}"
    if session is None:
        return f"门户在线列表暂未出现 {local_ip} (可能有同步延迟, 以下一周期为准)"
    login_at = session.login_time.strftime("%Y-%m-%d %H:%M:%S") if session.login_time else "N/A"
    return (f"门户在线列表已确认本机: ip={session.ip} id={session.sid} "
            f"user={session.username} 上线={login_at}")


# ==================== 守护主循环 ====================

def run_cycle(settings):
    """单轮保活: 探测 -> (掉线则)登录 -> 核验门户侧状态。返回 CycleResult。"""
    local_ip = detect_local_ip(settings.portal_ip)
    if local_ip is None:
        return CycleResult.NO_LINK
    ok, reason = probe_internet(settings.detect_url, settings.portal_ip)
    if ok:
        return CycleResult.ONLINE
    log("WARN", f"外网探测失败 [{reason}] (ip={local_ip}), 尝试门户登录...")
    login_ok, login_msg, already = portal_login(settings)
    if not login_ok:
        log("ERROR", f"门户登录失败: {login_msg}")
        return CycleResult.LOGIN_FAIL
    if already:
        log("INFO", f"门户判定已在线, 跳过登录: {login_msg}")
        return CycleResult.ALREADY_ONLINE
    log("INFO", f"门户登录成功: {login_msg}")
    log("INFO", verify_portal_session(settings, local_ip))
    return CycleResult.LOGIN_OK


def run_daemon(settings):
    """守护循环: 正常按 check_interval 保活; 登录失败指数退避 (上限 300s);
    无链路 (wlan 未关联) 以 5s 快速重查。"""
    interval = settings.check_interval
    backoff = interval
    cycles = 0
    log("INFO", "=" * 52)
    log("INFO", f"NAU-AutoLogin keepalive started (Linux) portal={settings.portal_ip} user={settings.username}")
    log("INFO", f"auth={settings.auth_type} probe={settings.detect_url} interval={interval}s")
    log("INFO", "=" * 52)
    while True:
        result = None
        try:
            result = run_cycle(settings)
        except Exception as exc:  # noqa: BROAD_EXCEPT_OK 顶层兜底: 守护进程须存活, Restart=always 是最后防线
            log("ERROR", f"保活周期异常: {exc!r}")
        if result is CycleResult.ONLINE:
            backoff = interval
            cycles += 1
            if cycles % HEARTBEAT_EVERY == 1:
                log("INFO", f"外网连通, 保活中 (第 {cycles} 周期)")
        elif result in (CycleResult.LOGIN_OK, CycleResult.ALREADY_ONLINE):
            backoff = interval
            cycles = 0
        elif result is CycleResult.NO_LINK:
            backoff = MIN_CHECK_INTERVAL  # wlan 未就绪, 快速轮询等待关联
            log("WARN", f"无到门户的路由, {backoff}s 后重查链路")
        else:  # LOGIN_FAIL / 未知异常: 指数退避
            backoff = min(max(backoff, interval) * 2, MAX_BACKOFF_SECONDS)
            log("WARN", f"{backoff}s 后重试")
        time.sleep(backoff)


def main(argv=None):
    """入口: --once 单轮运行 (健康检查/手动测试), 默认守护常驻"""
    parser = argparse.ArgumentParser(description="NAU campus portal keepalive daemon (Linux, stdlib only)")
    parser.add_argument("--once", action="store_true",
                        help="run a single keepalive cycle then exit (install.sh health check)")
    parser.add_argument("--config", type=Path, default=None,
                        help="config.ini 路径 (默认: 脚本目录或 ~/.nau-autologin/config.ini)")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except MissingCredentialError as e:
        log("ERROR", str(e))
        return 2
    if args.once:
        result = run_cycle(settings)
        if result is CycleResult.ONLINE:
            log("INFO", "单轮检测通过: 外网连通, 无需登录")
        elif result is CycleResult.NO_LINK:
            log("ERROR", "单轮检测未通过: 无到门户的路由 (wlan 未关联?)")
        # LOGIN_OK / ALREADY_ONLINE / LOGIN_FAIL 已在 run_cycle 内输出结论
        ok_results = (CycleResult.ONLINE, CycleResult.LOGIN_OK, CycleResult.ALREADY_ONLINE)
        return 0 if result in ok_results else 1
    run_daemon(settings)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
