#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京审计大学校园网自动登录与智能主备保活守护进程 (Windows 版)
Project: NAU-AutoLogin
Location: /home/dc/projects/NAU-AutoLogin

核心功能与架构保障:
1. 环境变量与安全凭据隔离:
   - 敏感认证信息 (学号/密码/门户IP) 独立由 .env 环境变量文件加载，严禁明文硬编码
   - 运行策略参数由 config.ini 控制，支持解耦管理
2. 有线优先与平滑故障转移:
   - 链路优先级: 有线网卡 (以太网 2) > 无线网卡 (WLAN / i-NAU)
   - 后台独立绑定有线源 IP 探测，有线恢复且稳定健康后自动平滑切回
   - 断开无线后执行毫秒级立即复核，失败瞬间瞬间回滚无线
3. 无线 AP 智能优选 (打破 Windows AP 粘连):
   - 实时采集当前 AP 信号 (Signal%)、信道、速率与网关首跳延迟 (RTT)
   - 弱信号/高延迟持续触发去抖门限后，扫描同 SSID 周围 AP 拓扑
   - 满足滞回余量 (Hysteresis) 且度过冷却期后，安全触发断开重连，促使系统优选强 AP
   - 重选后自动复核外网连通性与门户会话，必要时无缝重新登录
4. 零黑框零闪屏静默运行:
   - 适配器/IP 状态基于 Win32 iphlpapi.dll 内存直读 (ctypes)，0 外部子进程开销
   - 外部 netsh 命令显式注入 CREATE_NO_WINDOW (0x08000000) 与 SW_HIDE
   - 基于 Local 命名互斥体实现单实例锁
   - 自动日志轮转与异常安全流重定向
"""

import sys
import os
import time
import json
import subprocess
import socket
import struct
import urllib.request
import urllib.error
import http.client
import logging
import logging.handlers
from datetime import datetime, date
import ctypes
from ctypes import wintypes
import configparser
import re

# ==================== Win32 单实例互斥体 ====================

ERROR_ALREADY_EXISTS = 183
MUTEX_HANDLE = None

def acquire_single_instance_mutex(mutex_name="Local\\NAUAutoLoginMutex"):
    """创建或获取命名互斥体，确保单实例运行"""
    global MUTEX_HANDLE
    try:
        kernel32 = ctypes.windll.kernel32
        MUTEX_HANDLE = kernel32.CreateMutexW(None, False, mutex_name)
        last_err = kernel32.GetLastError()
        if last_err == ERROR_ALREADY_EXISTS:
            return False
        return True
    except Exception:
        return True

# ==================== 环境变量与配置文件安全加载 ====================

def get_script_dir():
    """获取脚本所在目录"""
    return os.path.dirname(os.path.abspath(__file__))

def get_default_user_dir():
    """获取用户家目录下部署配置目录"""
    user_home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    user_dir = os.path.join(user_home, ".nau-autologin")
    try:
        os.makedirs(user_dir, exist_ok=True)
    except Exception:
        pass
    return user_dir

def load_env():
    """
    加载 .env 环境变量文件。
    加载优先级: 1. 脚本同目录下的 .env; 2. %USERPROFILE%/.nau-autologin/.env
    """
    env_paths = [
        os.path.join(get_script_dir(), ".env"),
        os.path.join(get_default_user_dir(), ".env")
    ]
    
    target_env = None
    for p in env_paths:
        if os.path.exists(p):
            target_env = p
            break
            
    if target_env:
        try:
            with open(target_env, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    # 剥离可能的行内注释与首尾引号
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    else:
                        v = v.split("#", 1)[0].strip()
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

def load_settings():
    """
    加载 config.ini 配置项，并结合环境变量中的敏感认证凭据。
    """
    load_env()
    
    config_paths = [
        os.path.join(get_script_dir(), "config.ini"),
        os.path.join(get_default_user_dir(), "config.ini")
    ]
    
    cfg = configparser.ConfigParser()
    target_cfg = None
    for p in config_paths:
        if os.path.exists(p):
            target_cfg = p
            break
            
    if target_cfg:
        try:
            cfg.read(target_cfg, encoding="utf-8")
        except Exception:
            pass

    # 提取合并配置
    settings = {
        # 认证凭据 (优先从环境变量读取)
        "USERNAME": os.environ.get("NAU_USERNAME", "").strip(),
        "PASSWORD": os.environ.get("NAU_PASSWORD", "").strip(),
        "PORTAL_IP": os.environ.get("NAU_PORTAL_IP", "10.255.254.23").strip(),
        "DOMAIN": os.environ.get("NAU_DOMAIN", "default").strip(),
        "DETECT_URL": os.environ.get("NAU_DETECT_URL", "http://connect.rom.miui.com/generate_204").strip(),
        
        # 网卡配置
        "ETH_NAME": cfg.get("adapter", "eth_name", fallback="以太网 2").strip(),
        "WLAN_NAME": cfg.get("adapter", "wlan_name", fallback="WLAN").strip(),
        "WLAN_PROFILE": cfg.get("adapter", "wlan_profile", fallback="i-NAU").strip(),
        
        # 故障转移
        "CHECK_INTERVAL": cfg.getint("failover", "check_interval", fallback=30),
        "DISCONNECT_WLAN_WHEN_WIRED_HEALTHY": cfg.getboolean("failover", "disconnect_wlan_when_wired_healthy", fallback=True),
        "WIRED_STABLE_CYCLES": cfg.getint("failover", "wired_stable_cycles", fallback=2),
        "ALLOW_OVERRIDE_OTHER_SSID": cfg.getboolean("failover", "allow_override_other_ssid", fallback=False),
        
        # AP 优选
        "ENABLE_AP_OPTIMIZE": cfg.getboolean("ap_optimization", "enable_ap_optimize", fallback=True),
        "WEAK_SIGNAL_THRESHOLD": cfg.getint("ap_optimization", "weak_signal_threshold", fallback=45),
        "HIGH_RTT_THRESHOLD_MS": cfg.getint("ap_optimization", "high_rtt_threshold_ms", fallback=150),
        "DEBOUNCE_CYCLES": cfg.getint("ap_optimization", "debounce_cycles", fallback=2),
        "HYSTERESIS_MARGIN": cfg.getint("ap_optimization", "hysteresis_margin", fallback=15),
        "COOLDOWN_SECONDS": cfg.getint("ap_optimization", "cooldown_seconds", fallback=300),
        "MAX_DAILY_REASSOCIATIONS": cfg.getint("ap_optimization", "max_daily_reassociations", fallback=15),
        
        # 日志
        "LOG_PATH": cfg.get("logging", "log_path", fallback="").strip(),
        "LOG_MAX_BYTES": cfg.getint("logging", "log_max_bytes", fallback=5242880),
        "LOG_BACKUP_COUNT": cfg.getint("logging", "log_backup_count", fallback=3),
    }
    
    if not settings["LOG_PATH"]:
        settings["LOG_PATH"] = os.path.join(get_default_user_dir(), "autologin.log")
        
    return settings

# ==================== 运行时日志记录 ====================

app_logger = None

def init_logging(log_path, max_bytes=5242880, backup_count=3):
    """初始化日志，配置轮转，并保护 sys.stdout/stderr"""
    global app_logger
    logger = logging.getLogger("nau_autologin")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    
    log_dir = os.path.dirname(os.path.abspath(log_path))
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass
            
    try:
        rfh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8"
        )
        formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        rfh.setFormatter(formatter)
        logger.addHandler(rfh)
    except Exception:
        pass
        
    app_logger = logger

    class SafeLogStream:
        def __init__(self, lg, level_func):
            self.lg = lg
            self.level_func = level_func
        def write(self, s):
            s = s.strip()
            if s:
                try:
                    self.level_func(s)
                except Exception:
                    pass
        def flush(self):
            pass

    if sys.stdout is None or getattr(sys.stdout, "name", None) is None:
        sys.stdout = SafeLogStream(logger, logger.info)
    if sys.stderr is None or getattr(sys.stderr, "name", None) is None:
        sys.stderr = SafeLogStream(logger, logger.error)

def log(level, msg):
    """安全日志记录函数"""
    global app_logger
    if not app_logger:
        return
    try:
        lvl = level.upper()
        if lvl == "DEBUG":
            app_logger.debug(msg)
        elif lvl in ("WARN", "WARNING"):
            app_logger.warning(msg)
        elif lvl == "ERROR":
            app_logger.error(msg)
        else:
            app_logger.info(msg)
    except Exception:
        pass

# ==================== Win32 内存级网卡查询 ====================

GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
GAA_FLAG_SKIP_MULTICAST = 0x0004
GAA_FLAG_SKIP_DNS_SERVER = 0x0008

iphlpapi = ctypes.WinDLL("iphlpapi.dll")

class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.c_void_p),
        ("iSockaddrLength", wintypes.INT)
    ]

class sockaddr_in(ctypes.Structure):
    _fields_ = [
        ("sin_family", wintypes.SHORT),
        ("sin_port", wintypes.USHORT),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", wintypes.CHAR * 8)
    ]

class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass

IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG),
    ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", SOCKET_ADDRESS)
]

class IP_ADAPTER_GATEWAY_ADDRESS(ctypes.Structure):
    pass

IP_ADAPTER_GATEWAY_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG),
    ("Reserved", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Address", SOCKET_ADDRESS)
]

class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass

IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG),
    ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", wintypes.LPWSTR),
    ("Description", wintypes.LPWSTR),
    ("FriendlyName", wintypes.LPWSTR),
    ("PhysicalAddress", wintypes.BYTE * 8),
    ("PhysicalAddressLength", wintypes.DWORD),
    ("Flags", wintypes.DWORD),
    ("Mtu", wintypes.DWORD),
    ("IfType", wintypes.DWORD),
    ("OperStatus", wintypes.DWORD),
    ("Ipv6IfIndex", wintypes.DWORD),
    ("ZoneIndices", wintypes.DWORD * 16),
    ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_uint64),
    ("ReceiveLinkSpeed", ctypes.c_uint64),
    ("FirstWinsServerAddress", ctypes.c_void_p),
    ("FirstGatewayAddress", ctypes.POINTER(IP_ADAPTER_GATEWAY_ADDRESS))
]

def get_all_adapters():
    """Win32 内存级直接读取适配器信息 (耗时 < 5ms，0 进程开销，0 闪屏)"""
    buf_len = wintypes.ULONG(15000)
    buf = ctypes.create_string_buffer(buf_len.value)
    flags = GAA_FLAG_INCLUDE_GATEWAYS | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER
    
    ret = iphlpapi.GetAdaptersAddresses(2, flags, None, ctypes.byref(buf), ctypes.byref(buf_len)) # 2 = AF_INET
    if ret == 111: # ERROR_BUFFER_OVERFLOW
        buf = ctypes.create_string_buffer(buf_len.value)
        ret = iphlpapi.GetAdaptersAddresses(2, flags, None, ctypes.byref(buf), ctypes.byref(buf_len))
        
    if ret != 0:
        return {}
        
    adapters = {}
    p = ctypes.cast(buf, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while p:
        addr = p.contents
        name = addr.FriendlyName
        oper_status = addr.OperStatus
        
        ips = []
        u = addr.FirstUnicastAddress
        while u:
            sa_ptr = u.contents.Address.lpSockaddr
            if sa_ptr:
                sa = ctypes.cast(sa_ptr, ctypes.POINTER(sockaddr_in)).contents
                ip_str = ".".join(str(b) for b in sa.sin_addr)
                if not ip_str.startswith("169.254.") and not ip_str.startswith("127."):
                    ips.append(ip_str)
            u = u.contents.Next
            
        gws = []
        g = addr.FirstGatewayAddress
        while g:
            sa_ptr = g.contents.Address.lpSockaddr
            if sa_ptr:
                sa = ctypes.cast(sa_ptr, ctypes.POINTER(sockaddr_in)).contents
                gw_str = ".".join(str(b) for b in sa.sin_addr)
                if not gw_str.startswith("169.254."):
                    gws.append(gw_str)
            g = g.contents.Next
            
        adapters[name] = {
            "state": "connected" if oper_status == 1 else "disconnected",
            "ip": ips[0] if ips else None,
            "all_ips": ips,
            "gateway": gws[0] if gws else None
        }
        p = addr.Next
        
    return adapters

# ==================== 静默执行与 WLAN 接口控制 ====================

CREATE_NO_WINDOW = 0x08000000

def run_silent_cmd(cmd_list, timeout=8):
    """静默执行外部命令，杜绝控制台弹窗"""
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0 # SW_HIDE
        
        p = subprocess.run(
            cmd_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            startupinfo=startupinfo
        )
        for enc in ["utf-8", "gbk", "cp936"]:
            try:
                return p.returncode, p.stdout.decode(enc)
            except UnicodeDecodeError:
                continue
        return p.returncode, p.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        return -1, str(e)

def get_wlan_detail():
    """解析当前连接无线网卡的详细状态、BSSID、信道、信号及协商速率"""
    res = {
        "connected": False,
        "ssid": None,
        "profile": None,
        "bssid": None,
        "band": None,
        "channel": None,
        "signal": 0,
        "rx_rate": 0.0,
        "tx_rate": 0.0
    }
    rc, out = run_silent_cmd(["netsh", "wlan", "show", "interfaces"])
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, v = [x.strip() for x in line.split(":", 1)]
            k_lower = k.lower()
            
            if k_lower in ("state", "状态"):
                res["connected"] = ("connected" in v.lower() or "已连接" in v)
            elif k_lower in ("ssid",):
                res["ssid"] = v
            elif k_lower in ("profile", "配置文件"):
                res["profile"] = v
            elif "bssid" in k_lower:
                res["bssid"] = v.lower()
            elif any(w in k_lower for w in ("band", "波段", "频带", "频段")):
                res["band"] = v
            elif any(w in k_lower for w in ("channel", "通道", "频道", "信道")) and not any(w in k_lower for w in ("利用率", "utilization")):
                m = re.search(r"(\d+)", v)
                if m:
                    res["channel"] = int(m.group(1))
            elif any(w in k_lower for w in ("signal", "信号")):
                m = re.search(r"(\d+)", v)
                if m:
                    res["signal"] = int(m.group(1))
            elif "receive" in k_lower or "接收" in k_lower:
                m = re.search(r"([\d\.]+)", v)
                if m:
                    res["rx_rate"] = float(m.group(1))
            elif "transmit" in k_lower or "传输" in k_lower:
                m = re.search(r"([\d\.]+)", v)
                if m:
                    res["tx_rate"] = float(m.group(1))

    # Windows 11 非管理员或未开启位置服务时 netsh wlan show interfaces 可能会受限 (错误码 5)
    if not res["connected"] or not res["ssid"]:
        try:
            rc2, out2 = run_silent_cmd(["powershell", "-NoProfile", "-Command", "(Get-NetConnectionProfile -InterfaceAlias WLAN -ErrorAction SilentlyContinue).Name"], timeout=4)
            if rc2 == 0 and out2.strip():
                name = out2.strip()
                res["connected"] = True
                res["ssid"] = name
                res["profile"] = name
                if res["signal"] == 0:
                    res["signal"] = 80
        except Exception:
            pass

    return res

def scan_candidate_aps(target_ssid="i-NAU"):
    """
    扫描环境中属于 target_ssid 的所有可见 AP/BSSID，解析信道、信号强度及频段
    返回按信号强度从高到低排序的 AP 列表
    """
    candidates = []
    rc, out = run_silent_cmd(["netsh", "wlan", "show", "networks", "mode=bssid"], timeout=10)
    if rc != 0:
        return candidates
        
    current_ssid = None
    current_ap = None
    
    for line in out.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
            
        k, v = [x.strip() for x in line.split(":", 1)]
        k_lower = k.lower()
        
        if k_lower.startswith("ssid"):
            current_ssid = v
            continue
            
        if current_ssid != target_ssid:
            continue
            
        if k_lower.startswith("bssid"):
            if current_ap and "bssid" in current_ap:
                candidates.append(current_ap)
            current_ap = {
                "bssid": v.lower(),
                "signal": 0,
                "channel": 0,
                "band": "2.4 GHz",
                "utilization": 0
            }
        elif current_ap:
            if any(w in k_lower for w in ("signal", "信号")):
                m = re.search(r"(\d+)", v)
                if m:
                    current_ap["signal"] = int(m.group(1))
            elif any(w in k_lower for w in ("channel", "通道", "频道", "信道")) and not any(w in k_lower for w in ("utilization", "利用率")):
                m = re.search(r"(\d+)", v)
                if m:
                    current_ap["channel"] = int(m.group(1))
            elif any(w in k_lower for w in ("band", "波段", "频带", "频段")):
                current_ap["band"] = v
            elif any(w in k_lower for w in ("utilization", "利用率")):
                m = re.search(r"(\d+)\s*%", v)
                if m:
                    current_ap["utilization"] = int(m.group(1))
                
    if current_ap and "bssid" in current_ap:
        candidates.append(current_ap)
        
    candidates.sort(key=lambda x: x["signal"], reverse=True)
    return candidates

def measure_gateway_rtt(gateway_ip):
    """单次快速探测首跳网关延迟 (ms)"""
    if not gateway_ip:
        return None
    rc, out = run_silent_cmd(["ping", "-n", "1", "-w", "1000", gateway_ip], timeout=3)
    if rc == 0:
        m = re.search(r"(?:时间|time)[=<](\d+)ms", out, re.IGNORECASE)
        if m:
            return float(m.group(1))
        if "<1ms" in out:
            return 1.0
    return None

def connect_wlan(profile_name):
    """连接指定无线 Profile"""
    rc, out = run_silent_cmd(["netsh", "wlan", "connect", f"name={profile_name}"])
    return rc == 0

def disconnect_wlan():
    """断开无线连接"""
    rc, out = run_silent_cmd(["netsh", "wlan", "disconnect"])
    return rc == 0

def trigger_ap_reassociate(profile_name):
    """
    断开并重连，触发 Windows WLAN AutoConfig 重新打分并连接当前最优 AP
    """
    disconnect_wlan()
    time.sleep(2)
    connect_wlan(profile_name)

# ==================== IP 绑定 HTTP 与门户认证协议 ====================

class BoundHTTPHandler(urllib.request.HTTPHandler):
    """绑定特定源 IP 的 HTTP Handler"""
    def __init__(self, source_ip):
        super().__init__()
        self.source_ip = source_ip

    def http_open(self, req):
        return self.do_open(self._build_connection, req)

    def _build_connection(self, host, timeout=8):
        return http.client.HTTPConnection(host, timeout=timeout, source_address=(self.source_ip, 0))

class BoundHTTPSHandler(urllib.request.HTTPSHandler):
    """绑定特定源 IP 的 HTTPS Handler"""
    def __init__(self, source_ip, context=None):
        super().__init__(context=context)
        self.source_ip = source_ip

    def https_open(self, req):
        return self.do_open(self._build_connection, req)

    def _build_connection(self, host, timeout=8):
        return http.client.HTTPSConnection(host, timeout=timeout, source_address=(self.source_ip, 0), context=self._context)

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拦截 302/301 探测重定向"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def http_request(url, source_ip=None, data=None, headers=None, timeout=8, allow_redirects=True):
    """统一 HTTP 请求，支持源 IP 绑定并绕过本地系统代理"""
    handlers = [urllib.request.ProxyHandler({})]
    if not allow_redirects:
        handlers.append(NoRedirectHandler())
    if source_ip:
        handlers.append(BoundHTTPHandler(source_ip))
        handlers.append(BoundHTTPSHandler(source_ip))
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

def int_to_ipv4(val):
    """大端整数 IP 转标准 IPv4 字符串"""
    if val is None:
        return None
    if isinstance(val, str):
        val = val.strip()
        if "." in val:
            return val
        try:
            val = int(val)
        except ValueError:
            return None
    if isinstance(val, int):
        try:
            return socket.inet_ntoa(struct.pack(">I", val))
        except Exception:
            return None
    return None

SESSION_ACTIVE = 1
NO_SESSION = 0
QUERY_ERROR = -1

def check_portal_online(portal_ip, local_ip, bind_source=False):
    """三态检查 local_ip 是否在门户在线"""
    url = f"http://{portal_ip}/api/portal/v1/getinfo"
    src = local_ip if bind_source else None
    try:
        status, body, _ = http_request(url, source_ip=src, timeout=6)
        data = json.loads(body)
        rows = ((data.get("results") or {}).get("rows") or [])
        for row in rows:
            user_ip = int_to_ipv4(row.get("user_ipv4"))
            if user_ip and user_ip == local_ip:
                return SESSION_ACTIVE, row, f"session active for {local_ip} (user={row.get('username')})"
        return NO_SESSION, None, f"no active session for {local_ip} in portal getinfo"
    except Exception as e:
        if bind_source:
            # 尝试不绑定源 IP 进行回退探测
            try:
                status, body, _ = http_request(url, source_ip=None, timeout=6)
                data = json.loads(body)
                rows = ((data.get("results") or {}).get("rows") or [])
                for row in rows:
                    user_ip = int_to_ipv4(row.get("user_ipv4"))
                    if user_ip and user_ip == local_ip:
                        return SESSION_ACTIVE, row, f"session active for {local_ip} (user={row.get('username')})"
                return NO_SESSION, None, f"no active session for {local_ip} in portal getinfo"
            except Exception:
                pass
        return QUERY_ERROR, None, f"portal query failed: {e}"

def portal_login(portal_ip, local_ip, settings, bind_source=False):
    """向校园网门户发送登录认证请求"""
    url = f"http://{portal_ip}/api/portal/v1/login"
    payload = {
        "domain": settings.get("DOMAIN", "default"),
        "username": settings.get("USERNAME", ""),
        "password": settings.get("PASSWORD", "")
    }
    if not payload["username"] or not payload["password"]:
        return False, "login aborted: NAU_USERNAME or NAU_PASSWORD not configured"
        
    src = local_ip if bind_source else None
    try:
        status, body, _ = http_request(url, source_ip=src, data=payload, timeout=8)
        resp = json.loads(body)
        rc = resp.get("reply_code")
        msg = resp.get("reply_msg") or ""
        if rc == 0:
            user = (resp.get("results") or {}).get("username") or settings.get("USERNAME")
            return True, f"login success (reply_code=0, user={user})"
        else:
            return False, f"login rejected (reply_code={rc}, msg={msg})"
    except Exception as e:
        if bind_source:
            try:
                status, body, _ = http_request(url, source_ip=None, data=payload, timeout=8)
                resp = json.loads(body)
                rc = resp.get("reply_code")
                msg = resp.get("reply_msg") or ""
                if rc == 0:
                    user = (resp.get("results") or {}).get("username") or settings.get("USERNAME")
                    return True, f"login success (reply_code=0, user={user})"
                else:
                    return False, f"login rejected (reply_code={rc}, msg={msg})"
            except Exception:
                pass
        return False, f"login request error: {e}"

def check_external_probe(probe_url, source_ip=None, timeout=8):
    """通过 204 探针检测外网真实连通性 (带重定向拦截)"""
    try:
        status, body, final_url = http_request(probe_url, source_ip=source_ip, timeout=timeout, allow_redirects=False)
        if status == 204:
            return True, "external probe 204 OK"
        if status == 200 and ("10.255.254.23" not in body and "portal" not in body.lower()):
            return True, "external probe 200 OK"
        return False, f"captive portal or redirect detected (status={status})"
    except Exception as e:
        return False, f"external probe unreachable: {e}"

# ==================== 核心守护进程循环 ====================

def run_daemon():
    """主调度循环"""
    settings = load_settings()
    init_logging(settings["LOG_PATH"], settings["LOG_MAX_BYTES"], settings["LOG_BACKUP_COUNT"])
    
    if not acquire_single_instance_mutex():
        log("WARN", "Another instance of NAU-AutoLogin is already running. Exiting silently.")
        sys.exit(0)
        
    log("INFO", "========================================================")
    log("INFO", "NAU-AutoLogin Daemon started (Windows Desktop)")
    log("INFO", f"Config: ETH={settings['ETH_NAME']}, WLAN={settings['WLAN_NAME']}, Profile={settings['WLAN_PROFILE']}")
    log("INFO", f"Portal IP={settings['PORTAL_IP']}, User={settings['USERNAME']}")
    log("INFO", f"AP Optimize Enabled={settings['ENABLE_AP_OPTIMIZE']} (weak_threshold={settings['WEAK_SIGNAL_THRESHOLD']}%, hysteresis={settings['HYSTERESIS_MARGIN']}%)")
    log("INFO", "========================================================")
    
    check_interval = settings["CHECK_INTERVAL"]
    wired_stable_target = settings["WIRED_STABLE_CYCLES"]
    disconnect_wlan_on_wired = settings["DISCONNECT_WLAN_WHEN_WIRED_HEALTHY"]
    allow_override_other_ssid = settings["ALLOW_OVERRIDE_OTHER_SSID"]
    
    wired_healthy_cycles = 0
    wired_query_err_count = 0
    backoff = check_interval
    
    # AP 优选状态跟踪
    wlan_degraded_cycles = 0
    last_reassociate_time = 0
    daily_reassociate_count = 0
    current_day = date.today()

    while True:
        try:
            # 每日重选计数器重置
            today = date.today()
            if today != current_day:
                current_day = today
                daily_reassociate_count = 0

            eth_name = settings["ETH_NAME"]
            wlan_name = settings["WLAN_NAME"]
            wlan_profile = settings["WLAN_PROFILE"]
            portal_ip = settings["PORTAL_IP"]
            detect_url = settings["DETECT_URL"]
            
            # 1. 毫秒级内存直读网卡信息
            all_adapters = get_all_adapters()
            eth_info = all_adapters.get(eth_name, {"state": "disconnected", "ip": None, "gateway": None})
            wlan_info = all_adapters.get(wlan_name, {"state": "disconnected", "ip": None, "gateway": None})
            
            wired_is_healthy = False
            
            # ----------------- 步骤 1: 评估有线网络 -----------------
            if eth_info["state"] == "connected" and eth_info["ip"]:
                eth_ip = eth_info["ip"]
                sess_state, sess, reason = check_portal_online(portal_ip, eth_ip)
                
                if sess_state == SESSION_ACTIVE:
                    wired_query_err_count = 0
                    probe_ok, probe_msg = check_external_probe(detect_url, source_ip=eth_ip)
                    if probe_ok:
                        wired_is_healthy = True
                    else:
                        log("WARN", f"Wired portal online but external probe failed: {probe_msg}")
                elif sess_state == NO_SESSION:
                    wired_query_err_count = 0
                    log("WARN", f"Wired link is UP ({eth_ip}) but no portal session. Logging in...")
                    ok, login_msg = portal_login(portal_ip, eth_ip, settings)
                    if ok:
                        log("INFO", f"Wired portal login success: {login_msg}")
                        probe_ok, probe_msg = check_external_probe(detect_url, source_ip=eth_ip)
                        if probe_ok:
                            wired_is_healthy = True
                    else:
                        log("ERROR", f"Wired portal login failed: {login_msg}")
                else:
                    wired_query_err_count += 1
                    log("WARN", f"Wired portal query transient error ({wired_query_err_count}/3): {reason}")
                    if wired_query_err_count < 3 and wired_healthy_cycles > 0:
                        wired_is_healthy = True

            # ----------------- 步骤 2: 有线决策与平滑回切 -----------------
            if wired_is_healthy:
                wired_healthy_cycles += 1
                backoff = check_interval
                log("INFO", f"WIRED OK (ip={eth_info['ip']}, stable_cycles={wired_healthy_cycles}/{wired_stable_target})")
                
                if disconnect_wlan_on_wired and wired_healthy_cycles >= wired_stable_target:
                    if wlan_info["state"] == "connected":
                        log("INFO", "Wired connection is stable. Disconnecting WLAN to prioritize wired.")
                        disconnect_wlan()
                        
                        time.sleep(1)
                        rollback_probe_ok, rollback_msg = check_external_probe(detect_url, timeout=5)
                        if not rollback_probe_ok:
                            log("ERROR", f"Post-disconnect probe failed ({rollback_msg})! Instant rolling back WLAN...")
                            connect_wlan(wlan_profile)
                            wired_healthy_cycles = 0
            else:
                if wired_healthy_cycles > 0:
                    log("WARN", "Wired connection down or degraded. Resetting stable cycles.")
                wired_healthy_cycles = 0
                
                # ----------------- 步骤 3: 调度与保活无线网络 -----------------
                wlan_detail = get_wlan_detail()
                curr_ssid = wlan_detail.get("ssid")
                
                # 尊重用户手动连接的其他可用网络
                if wlan_detail["connected"] and curr_ssid and curr_ssid != wlan_profile and not allow_override_other_ssid:
                    other_ok, other_msg = check_external_probe(detect_url, timeout=6)
                    if other_ok:
                        log("INFO", f"Connected to user alternative WiFi '{curr_ssid}' with internet. Respecting user connection.")
                        backoff = check_interval
                        time.sleep(backoff)
                        continue
                    else:
                        log("WARN", f"Alternative WiFi '{curr_ssid}' has no internet. Falling back to '{wlan_profile}'...")

                # 若未连接目标校园网，执行连接
                if not wlan_detail["connected"] or curr_ssid != wlan_profile:
                    reason_msg = "Wired is down" if settings.get("ETH_NAME") else "WLAN is not connected"
                    log("WARN", f"{reason_msg}. Connecting to wireless profile '{wlan_profile}'...")
                    connect_wlan(wlan_profile)
                    for _ in range(5):
                        time.sleep(2)
                        all_adapters = get_all_adapters()
                        wlan_info = all_adapters.get(wlan_name, {"state": "disconnected", "ip": None, "gateway": None})
                        if wlan_info["state"] == "connected" and wlan_info["ip"]:
                            break
                            
                # 无线 IP 与门户登录保活
                if wlan_info["state"] == "connected" and wlan_info["ip"]:
                    wlan_ip = wlan_info["ip"]
                    sess_state, sess, reason = check_portal_online(portal_ip, wlan_ip, bind_source=False)
                    wlan_online = False
                    
                    if sess_state == SESSION_ACTIVE:
                        probe_ok, probe_msg = check_external_probe(detect_url)
                        if probe_ok:
                            log("INFO", f"WLAN OK (ip={wlan_ip})")
                            wlan_online = True
                            backoff = check_interval
                        else:
                            log("WARN", f"WLAN portal online but probe failed: {probe_msg}")
                    elif sess_state == NO_SESSION:
                        log("WARN", f"WLAN is UP ({wlan_ip}) but not logged in. Logging in...")
                        ok, login_msg = portal_login(portal_ip, wlan_ip, settings, bind_source=False)
                        if ok:
                            log("INFO", f"WLAN portal login success: {login_msg}")
                            probe_ok, probe_msg = check_external_probe(detect_url)
                            if probe_ok:
                                wlan_online = True
                            backoff = check_interval
                        else:
                            log("ERROR", f"WLAN portal login failed: {login_msg}")
                            backoff = min(backoff * 2, 300)
                    else:
                        log("WARN", f"WLAN portal query transient error: {reason}")
                        # 回退：若查询门户会话出错且外网探测不通，主动尝试一次登录
                        probe_ok, _ = check_external_probe(detect_url, timeout=3)
                        if not probe_ok:
                            log("INFO", "External probe failed after portal error. Attempting login as recovery...")
                            ok, login_msg = portal_login(portal_ip, wlan_ip, settings, bind_source=False)
                            if ok:
                                log("INFO", f"Recovery portal login success: {login_msg}")
                                backoff = check_interval

                    # ----------------- 步骤 4: 无线 AP 智能优选 (打破粘连) -----------------
                    if wlan_online and settings["ENABLE_AP_OPTIMIZE"]:
                        wlan_status = get_wlan_detail()
                        curr_sig = wlan_status["signal"]
                        curr_bssid = wlan_status["bssid"]
                        curr_ch = wlan_status["channel"]
                        gw_ip = wlan_info.get("gateway")
                        gw_rtt = measure_gateway_rtt(gw_ip)
                        
                        weak_thresh = settings["WEAK_SIGNAL_THRESHOLD"]
                        high_rtt_thresh = settings["HIGH_RTT_THRESHOLD_MS"]
                        
                        is_degraded = (curr_sig > 0 and curr_sig <= weak_thresh) or (gw_rtt is not None and gw_rtt >= high_rtt_thresh)
                        if is_degraded:
                            wlan_degraded_cycles += 1
                            log("WARN", f"WLAN quality degraded ({wlan_degraded_cycles}/{settings['DEBOUNCE_CYCLES']}): Signal={curr_sig}% (ch{curr_ch}), Gateway RTT={gw_rtt}ms")
                        else:
                            wlan_degraded_cycles = 0
                            
                        if wlan_degraded_cycles >= settings["DEBOUNCE_CYCLES"]:
                            now = time.time()
                            cooldown = settings["COOLDOWN_SECONDS"]
                            max_daily = settings["MAX_DAILY_REASSOCIATIONS"]
                            
                            if (now - last_reassociate_time >= cooldown) and (daily_reassociate_count < max_daily):
                                log("INFO", f"Scanning AP topology for SSID '{wlan_profile}'...")
                                candidates = scan_candidate_aps(wlan_profile)
                                best_cand = candidates[0] if candidates else None
                                
                                if best_cand and (best_cand["signal"] >= curr_sig + settings["HYSTERESIS_MARGIN"]):
                                    log("INFO", f"AP Optimization triggered: current [{curr_bssid}] (ch{curr_ch}, {curr_sig}%) -> better [{best_cand['bssid']}] (ch{best_cand['channel']}, {best_cand['signal']}%)")
                                    trigger_ap_reassociate(wlan_profile)
                                    last_reassociate_time = now
                                    daily_reassociate_count += 1
                                    wlan_degraded_cycles = 0
                                    
                                    # 重选后等待网络稳定并复核
                                    time.sleep(3)
                                    new_adapters = get_all_adapters()
                                    new_wlan_ip = (new_adapters.get(wlan_name) or {}).get("ip")
                                    if new_wlan_ip:
                                        p_state, _, _ = check_portal_online(portal_ip, new_wlan_ip)
                                        if p_state != SESSION_ACTIVE:
                                            log("INFO", f"Portal session expired after AP re-association. Re-authenticating {new_wlan_ip}...")
                                            portal_login(portal_ip, new_wlan_ip, settings)
                                    new_status = get_wlan_detail()
                                    log("INFO", f"AP Optimization finished: now on [{new_status['bssid']}] ch{new_status['channel']}, Signal={new_status['signal']}%, Rate={new_status['rx_rate']}/{new_status['tx_rate']} Mbps")
                                else:
                                    log("INFO", f"Scan complete. No candidate meets hysteresis margin (+{settings['HYSTERESIS_MARGIN']}%). Retaining current AP.")
                                    wlan_degraded_cycles = 0 # 重置计数，避免每个周期连续扫描
                            else:
                                log("DEBUG", "AP optimization triggered but cooldown is active or daily limit reached.")
                else:
                    log("WARN", "WLAN is associating, waiting for valid IPv4...")
                    backoff = 5

        except Exception as exc:
            log("ERROR", f"Daemon main loop exception: {exc}")
            backoff = min(max(backoff, check_interval) * 2, 300)
            
        time.sleep(backoff)

if __name__ == "__main__":
    run_daemon()
