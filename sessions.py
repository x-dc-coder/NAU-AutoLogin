#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京审计大学校园网会话管理命令行工具 (纯标准库, 跨平台)
Project: NAU-AutoLogin
Location: sessions.py

用法:
  python sessions.py -l                     列出账号全部在线会话
  python sessions.py -k 10.200.34.163       按 IP 强制下线指定会话
  python sessions.py -k 393587733           按会话 id 强制下线
  python sessions.py -k-all                 下线全部会话 (需确认, --yes 跳过确认)
  python sessions.py -k-all --exclude 10.100.109.52   下线全部但保留指定 IP
  python sessions.py -w                     流量巡检: 两次采样, 找出零流量幽灵会话
  python sessions.py -w --interval 300 --min-age 1800
  python sessions.py -d [YYYYMM]            查看历史会话详单 (默认当月)
  python sessions.py -l --json              JSON 输出 (便于脚本集成)

退出码: 0=成功 1=网络/请求失败 2=门户业务错误 3=参数错误
"""

import os
import sys
import json
import time
import argparse
import configparser
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_manager import (
    PortalSessionManager,
    PortalAPIError,
    human_bytes,
    human_duration,
    long2ip,
)


# ==================== 最小配置加载 (与守护进程同语义, 自包含) ====================

def get_script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def get_default_user_dir():
    user_home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    user_dir = os.path.join(user_home, ".nau-autologin")
    try:
        os.makedirs(user_dir, exist_ok=True)
    except Exception:
        pass
    return user_dir


def load_env():
    """加载 .env: 1. 脚本同目录; 2. %USERPROFILE%/.nau-autologin/"""
    env_paths = [
        os.path.join(get_script_dir(), ".env"),
        os.path.join(get_default_user_dir(), ".env"),
    ]
    for p in env_paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    else:
                        v = v.split("#", 1)[0].strip()
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass
        break


def load_settings():
    load_env()
    cfg = configparser.ConfigParser()
    for p in (os.path.join(get_script_dir(), "config.ini"),
              os.path.join(get_default_user_dir(), "config.ini")):
        if os.path.exists(p):
            try:
                cfg.read(p, encoding="utf-8")
            except Exception:
                pass
            break
    return {
        "PORTAL_IP": os.environ.get("NAU_PORTAL_IP", "10.255.254.23").strip(),
        "ENABLE_SESSION_CLEANUP": cfg.getboolean("session_mgmt", "enable_session_cleanup", fallback=True),
        "RECENT_IPS_MAX": cfg.getint("session_mgmt", "recent_ips_max", fallback=5),
    }


# ==================== 输出格式化 ====================

def print_session_table(sessions):
    if not sessions:
        print("当前账号无在线会话。")
        return
    print(f"在线会话数: {len(sessions)}")
    print(f"{'序号':<4} {'IP':<16} {'MAC':<19} {'区域':<12} {'上线时间':<18} {'时长':<12} {'下行/上行':<16} {'会话id'}")
    print("-" * 118)
    for i, s in enumerate(sessions, 1):
        lt = s.login_time.strftime("%m-%d %H:%M:%S") if s.login_time else "N/A"
        area = (s.area or "")[:10]
        print(f"{i:<4} {s.ip:<16} {s.mac:<19} {area:<12} {lt:<18} "
              f"{human_duration(s.duration):<12} "
              f"{human_bytes(s.rx_bytes) + '/' + human_bytes(s.tx_bytes):<16} {s.sid}")


def print_detail_table(rows):
    if not rows:
        print("无历史会话记录。")
        return
    print(f"历史会话记录数: {len(rows)}")
    print(f"{'上线时间':<18} {'时长':<12} {'下行':<10} {'上行':<10} {'IP':<16} {'MAC':<19} {'区域'}")
    print("-" * 108)
    for r in rows:
        st = r.get("acctstarttime")
        lt = datetime.fromtimestamp(st).strftime("%m-%d %H:%M:%S") if st else "N/A"
        area = ((r.get("area") or {}).get("name") or "")[:10]
        print(f"{lt:<18} {human_duration(r.get('acctsessiontime')):<12} "
              f"{human_bytes(r.get('acctinputoctets_ipv4')):<10} {human_bytes(r.get('acctoutputoctets_ipv4')):<10} "
              f"{long2ip(r.get('user_ipv4')):<16} {(r.get('mac') or ''):<19} {area}")


# ==================== 子命令实现 ====================

def cmd_list(mgr, as_json):
    sessions = mgr.list_sessions()
    if as_json:
        print(json.dumps([s.to_dict() for s in sessions], ensure_ascii=False, indent=2))
    else:
        print_session_table(sessions)
    return 0


def cmd_kick(mgr, target):
    """target 为 IP (含点) 或纯数字会话 id"""
    target = target.strip()
    if target.replace(".", "").isdigit() and "." in target:
        s = mgr.find_by_ip(target)
        if s is None:
            print(f"[跳过] {target} 当前无在线会话 (可能已超时自动清除)")
            return 0
        print(f"命中会话: IP={s.ip} MAC={s.mac} 区域={s.area} 会话id={s.sid}")
        ok, msg = mgr.kick(s.sid)
    elif target.isdigit():
        print(f"按会话 id 下线: {target}")
        ok, msg = mgr.kick(int(target))
    else:
        print(f"参数错误: '{target}' 既不是 IP 也不是数字 id", file=sys.stderr)
        return 3
    if ok:
        print(f"下线成功: {target}")
        return 0
    print(f"下线失败: {msg}", file=sys.stderr)
    return 1


def cmd_kick_all(mgr, exclude, assume_yes):
    sessions = mgr.list_sessions()
    if not sessions:
        print("当前账号无在线会话。")
        return 0
    targets = [s for s in sessions if s.ip not in set(exclude or [])]
    skipped = [s for s in sessions if s.ip in set(exclude or [])]
    if not targets:
        print("没有可下线的会话 (全部被 --exclude 排除)。")
        return 0
    print("将下线以下会话:")
    for s in targets:
        lt = s.login_time.strftime("%m-%d %H:%M:%S") if s.login_time else "N/A"
        print(f"  {s.ip:<16} {s.mac:<19} {s.area:<12} 上线={lt} id={s.sid}")
    for s in skipped:
        print(f"  [保留] {s.ip}")
    if not assume_yes:
        try:
            answer = input(f"确认下线以上 {len(targets)} 个会话? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消。")
            return 0
    failed = 0
    for s in targets:
        ok, msg = mgr.kick(s.sid)
        status = "成功" if ok else f"失败({msg})"
        print(f"  {s.ip:<16} -> {status}")
        if not ok:
            failed += 1
    print(f"完成: 成功 {len(targets) - failed} 个, 失败 {failed} 个")
    return 0 if failed == 0 else 1


def cmd_watch(mgr, interval, min_age, as_json):
    """流量巡检: 两次采样算增量, 零增量且上线较久的会话判定为幽灵会话"""
    print(f"[巡检] 第 1 次采样 ...")
    try:
        snap1 = mgr.traffic_snapshot()
    except Exception as e:
        print(f"请求失败: {e}", file=sys.stderr)
        return 1
    print(f"[巡检] 第 1 次采样完成 ({len(snap1)} 个会话), 等待 {interval} 秒后第 2 次采样 ...")
    if interval > 0:
        time.sleep(interval)
    snap2 = mgr.traffic_snapshot()

    ghosts, active, gone, new = [], [], [], []
    for ip, (rx2, tx2, dur2, lt2) in snap2.items():
        if ip not in snap1:
            new.append(ip)
            continue
        rx1, tx1, dur1, _ = snap1[ip]
        delta = (rx2 + tx2) - (rx1 + tx1)
        if delta <= 0 and dur2 >= min_age:
            ghosts.append({"ip": ip, "delta_bytes": delta, "duration": dur2,
                           "rx": rx2, "tx": tx2, "login_time": lt2})
        else:
            active.append({"ip": ip, "delta_bytes": delta, "duration": dur2})
    for ip in snap1:
        if ip not in snap2:
            gone.append(ip)

    if as_json:
        print(json.dumps({"ghosts": ghosts, "active": active,
                          "offline_during_watch": gone, "new_sessions": new},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"\n[巡检结果] 采样间隔 {interval}s, 幽灵判定阈值: 上线 >= {human_duration(min_age)} 且流量增量为 0")
    print(f"  活跃会话: {len(active)} 个")
    for a in active:
        print(f"    {a['ip']:<16} 增量 {human_bytes(max(a['delta_bytes'], 0)):<10} 已在线 {human_duration(a['duration'])}")
    if gone:
        print(f"  巡检期间自行掉线: {len(gone)} 个 -> {', '.join(gone)}")
    if new:
        print(f"  巡检期间新上线: {len(new)} 个 -> {', '.join(new)}")
    if ghosts:
        print(f"  !! 疑似幽灵会话: {len(ghosts)} 个 (零流量且上线较久, 设备可能已离开)")
        for g in ghosts:
            lt = g["login_time"].strftime("%m-%d %H:%M:%S") if g["login_time"] else "N/A"
            print(f"    {g['ip']:<16} 上线={lt} 已在线 {human_duration(g['duration'])} "
                  f"总流量 {human_bytes(g['rx'] + g['tx'])}")
        print("  清理命令: python sessions.py -k <IP>")
    else:
        print("  未发现幽灵会话。")
    return 0


def cmd_detail(mgr, month):
    rows = mgr.acct_detail(month=month)
    print_detail_table(rows)
    return 0


# ==================== 入口 ====================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="NAU 校园网会话管理工具 (列表/强制下线/流量巡检/历史详单)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python sessions.py -l\n"
               "  python sessions.py -k 10.200.34.163\n"
               "  python sessions.py -k-all --exclude 10.100.109.52\n"
               "  python sessions.py -w --interval 600 --min-age 1800\n")
    parser.add_argument("-l", "--list", action="store_true", help="列出全部在线会话")
    parser.add_argument("-k", "--kick", metavar="IP_OR_ID", help="强制下线指定会话 (按 IP 或会话 id)")
    parser.add_argument("-k-all", "--kick-all", action="store_true", help="强制下线全部会话 (需确认)")
    parser.add_argument("--exclude", nargs="*", default=[], metavar="IP",
                        help="配合 -k-all 使用, 保留指定 IP 不下线")
    parser.add_argument("--yes", action="store_true", help="-k-all 跳过交互确认")
    parser.add_argument("-w", "--watch", action="store_true",
                        help="流量巡检: 两次采样找出零流量幽灵会话")
    parser.add_argument("--interval", type=int, default=600,
                        help="巡检两次采样间隔秒数 (默认 600)")
    parser.add_argument("--min-age", type=int, default=1800,
                        help="巡检幽灵判定: 最短上线时长秒 (默认 1800)")
    parser.add_argument("-d", "--detail", nargs="?", const="", default=None, metavar="YYYYMM",
                        help="查看历史会话详单 (默认当月)")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    settings = load_settings()
    mgr = PortalSessionManager(settings["PORTAL_IP"])

    actions = [args.list, bool(args.kick), args.kick_all, args.watch, args.detail is not None]
    if not any(actions):
        args.list = True  # 无参数默认列表

    rc = 0
    try:
        if args.list:
            rc = cmd_list(mgr, args.json) or rc
        if args.kick:
            rc = cmd_kick(mgr, args.kick) or rc
        if args.kick_all:
            rc = cmd_kick_all(mgr, args.exclude, args.yes) or rc
        if args.watch:
            rc = cmd_watch(mgr, args.interval, args.min_age, args.json) or rc
        if args.detail is not None:
            rc = cmd_detail(mgr, args.detail or None) or rc
    except PortalAPIError as e:
        print(f"门户接口业务错误: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"请求失败 (请确认已在校园网环境): {e}", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
