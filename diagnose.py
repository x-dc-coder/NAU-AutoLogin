#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京审计大学校园网多维状态自检与 AP 优选诊断工具 (只读安全诊断)
Project: NAU-AutoLogin
Location: /home/dc/projects/NAU-AutoLogin/diagnose.py
"""

import sys
import os
import json
import socket
import struct
import urllib.request
import urllib.error
import http.client
import subprocess
import time
import re

# 导入核心守护进程模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nau_autologin import (
    load_settings,
    get_all_adapters,
    get_wlan_detail,
    scan_candidate_aps,
    measure_gateway_rtt,
    check_portal_online,
    check_external_probe,
    http_request,
    int_to_ipv4,
    run_silent_cmd,
    SESSION_ACTIVE,
    NO_SESSION,
    QUERY_ERROR
)

def print_separator(title=""):
    if title:
        print(f"\n===== [ {title} ] " + "=" * (65 - len(title)))
    else:
        print("=" * 75)

def mask_string(s, visible_len=3):
    if not s:
        return "<空/未配置>"
    if len(s) <= visible_len:
        return "***"
    return s[:visible_len] + "*" * (len(s) - visible_len)

def get_account_online_sessions(portal_ip):
    """查询账号全部在线会话"""
    url = f"http://{portal_ip}/api/selfservice/v1/online"
    try:
        status, body, _ = http_request(url, timeout=5)
        data = json.loads(body)
        rows = (data.get("results") or {}).get("rows") or []
        return rows
    except Exception as e:
        return None

def main():
    print_separator("NAU-AutoLogin 校园网多维状态自检与 AP 诊断")
    settings = load_settings()
    
    print(f"脚本目录: {os.path.dirname(os.path.abspath(__file__))}")
    print(f"认证用户: {settings['USERNAME']}")
    print(f"认证密码: {mask_string(settings['PASSWORD'])}")
    print(f"认证服务: {settings['PORTAL_IP']} (Domain: {settings['DOMAIN']})")
    print(f"探针地址: {settings['DETECT_URL']}")
    print(f"目标有线: {settings['ETH_NAME']}")
    print(f"目标无线: {settings['WLAN_NAME']} (Profile: {settings['WLAN_PROFILE']})")
    print(f"AP优选开: {settings['ENABLE_AP_OPTIMIZE']} (弱信号阈值={settings['WEAK_SIGNAL_THRESHOLD']}%, 滞回余量={settings['HYSTERESIS_MARGIN']}%)")

    # 1. 物理网卡与 IP
    print_separator("1. Win32 内存级网卡实时状态")
    adapters = get_all_adapters()
    eth = adapters.get(settings['ETH_NAME'])
    wlan = adapters.get(settings['WLAN_NAME'])

    print(f"有线网卡 [{settings['ETH_NAME']}]:")
    if eth:
        print(f"  - 物理状态: {eth['state']}")
        print(f"  - IPv4 地址: {eth['ip'] or '无'}")
        print(f"  - 网关地址: {eth['gateway'] or '无'}")
    else:
        print("  - 警告: 系统中未找到该有线网卡名称，请检查 config.ini 中的 eth_name")

    print(f"无线网卡 [{settings['WLAN_NAME']}]:")
    if wlan:
        print(f"  - 物理状态: {wlan['state']}")
        print(f"  - IPv4 地址: {wlan['ip'] or '无'}")
        print(f"  - 网关地址: {wlan['gateway'] or '无'}")
    else:
        print("  - 警告: 系统中未找到该无线网卡名称，请检查 config.ini 中的 wlan_name")

    # 2. WLAN 射频与当前连接详情
    print_separator("2. WLAN 连接详情与首跳网关延迟")
    wlan_status = get_wlan_detail()
    if wlan_status["connected"]:
        print(f"当前 SSID: {wlan_status['ssid']} (Profile: {wlan_status['profile']})")
        print(f"当前 BSSID: {wlan_status['bssid']}")
        print(f"频段信道: {wlan_status['band']} / 信道 {wlan_status['channel']}")
        print(f"信号强度: {wlan_status['signal']}%")
        print(f"物理速率: 接收 {wlan_status['rx_rate']} Mbps / 发送 {wlan_status['tx_rate']} Mbps")
        
        gw_ip = wlan.get("gateway") if wlan else None
        if gw_ip:
            rtt = measure_gateway_rtt(gw_ip)
            if rtt is not None:
                print(f"首跳网关: {gw_ip} (延迟 = {rtt} ms)")
            else:
                print(f"首跳网关: {gw_ip} (ping 超时或未响应)")
    else:
        print("无线网卡当前未连接任何网络。")

    # 3. 现场 AP 拓扑与评分对比
    print_separator(f"3. 周围可见 AP 拓扑扫描 (SSID: {settings['WLAN_PROFILE']})")
    candidates = scan_candidate_aps(settings['WLAN_PROFILE'])
    if candidates:
        print(f"共发现 {len(candidates)} 个同 SSID 广播接入点 (按信号降序排列):")
        print(f"{'序号':<4} {'BSSID':<20} {'频段':<10} {'信道':<6} {'信号':<8} {'当前连接'}")
        print("-" * 65)
        curr_bssid = (wlan_status.get("bssid") or "").lower()
        for idx, ap in enumerate(candidates, 1):
            is_cur = "★ (当前)" if ap["bssid"] == curr_bssid else ""
            print(f"{idx:<4} {ap['bssid']:<20} {ap['band']:<10} {ap['channel']:<6} {str(ap['signal'])+'%':<8} {is_cur}")
            
        best = candidates[0]
        cur_sig = wlan_status.get("signal", 0)
        margin = settings["HYSTERESIS_MARGIN"]
        print("-" * 65)
        if curr_bssid == best["bssid"]:
            print(f"评估结果: 当前关联的已是环境中最强信号 AP ({cur_sig}%)，处于最优状态。")
        elif best["signal"] >= cur_sig + margin:
            print(f"评估结果: 发现更优 AP [{best['bssid']}] (信道 {best['channel']}, 信号 {best['signal']}%)，高出当前 AP {best['signal'] - cur_sig}% (>= {margin}% 滞回余量)。")
            print(f"          建议: 执行 AP 重选 (断开并重新连接) 可打破粘连并接入该最优 AP。")
        else:
            print(f"评估结果: 当前 AP 信号 {cur_sig}%，与最强 AP ({best['signal']}%) 差距未超过滞回阈值 (+{margin}%)，保持当前连接以防乒乓抖动。")
    else:
        print(f"未扫描到广播 '{settings['WLAN_PROFILE']}' 的接入点。")

    # 4. 门户与外网连通性
    print_separator("4. 校园网认证会话与外网探针测试")
    portal_ip = settings["PORTAL_IP"]
    
    # 账号全量会话
    all_sessions = get_account_online_sessions(portal_ip)
    if all_sessions is not None:
        print(f"账号 [{settings['USERNAME']}] 全网在线设备数: {len(all_sessions)}")
        for idx, s in enumerate(all_sessions, 1):
            raw_ip = s.get("user_ipv4")
            s_ip = int_to_ipv4(raw_ip) or str(raw_ip)
            dev_name = s.get("device_name") or s.get("bras_name") or "未知终端"
            print(f"  {idx}. IP: {s_ip:<16} 上线时间: {s.get('login_time', 'N/A')} ({dev_name})")
    else:
        print("无法跨网段查询自服务在线会话 (可能处于断网状态或门户限制)")

    # 分别测试有线与无线连通性
    if eth and eth["ip"]:
        st, info, msg = check_portal_online(portal_ip, eth["ip"])
        probe_ok, p_msg = check_external_probe(settings["DETECT_URL"], source_ip=eth["ip"])
        print(f"有线链路 [{eth['ip']}]: 门户会话={'在线' if st==SESSION_ACTIVE else '未在线'} ({msg}), 外网探针={'OK' if probe_ok else 'FAIL'} ({p_msg})")
        
    if wlan and wlan["ip"]:
        st, info, msg = check_portal_online(portal_ip, wlan["ip"])
        probe_ok, p_msg = check_external_probe(settings["DETECT_URL"], source_ip=wlan["ip"])
        print(f"无线链路 [{wlan['ip']}]: 门户会话={'在线' if st==SESSION_ACTIVE else '未在线'} ({msg}), 外网探针={'OK' if probe_ok else 'FAIL'} ({p_msg})")

    print_separator("自检完成")

if __name__ == "__main__":
    main()
