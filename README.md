# NAU-AutoLogin: 南审校园网智能自动登录与双网卡保活守护程序

NAU-AutoLogin 是专为南京审计大学（NAU）校园网环境打造的高稳定性、全自动网络守护程序。旨在解决日常有线断网、节假日断网、无线弱信号 AP 粘连（Sticky AP）、掉线重登等网络痛点。

---

## 一、核心特性与技术优势

1. **凭据安全隔离与环境解耦**：
   - 敏感认证信息（学号、密码、认证服务地址）独立存放于 `.env` 环境变量文件中，权限受严格控制，不入版本库；
   - 运行策略参数（网卡名称、检测周期、AP 优选阈值等）收敛至 `config.ini`，实现凭据与业务配置解耦。
2. **有线优先与双网卡平滑故障转移**：
   - **日常状态**：优先走有线（`以太网 2`，Metric 20 < WLAN Metric 50），享受高速低延迟；
   - **断网容灾**：有线断开或无 DHCP 时，毫秒级感知并自动拉起无线 `i-NAU`，完成免密/PAP 门户登录；
   - **平滑回切**：后台独立绑定有线源 IP 探测，有线稳定健康后平滑切回并可选断开无线；断开后立即复核，异常瞬间回滚。
3. **无线 AP 智能优选（打破 Windows AP 粘连）**：
   - Windows 漫游算法存在保守滞回特性，常粘连在弱信号 AP（如 20%~40% 信号或 DFS 频段）；
   - 本守护进程实时监测网卡信号强度与网关首跳延迟（RTT）；
   - 触发去抖门限后，自动扫描环境同 SSID 周围 AP 拓扑；若存在满足滞回余量（默认高出 15% 以上）的备选 AP 且度过冷却期，自动断开重连促使系统选优，并自动复核与补发登录认证。
4. **零黑框零闪屏静默运行**：
   - 网卡与 IP 查询基于 Win32 `iphlpapi.dll` 内存直读，0 外部子进程开销；
   - 外部命令均显式注入 `CREATE_NO_WINDOW` 与 `SW_HIDE`；
   - 基于 Local 命名互斥体实现单实例锁，多开自动退出；
   - 标准库自动日志轮转（默认 5MB），防日志无限增长。
5. **免管理员权限**：
   - 自启动注册至当前用户注册表 `HKCU:\Software\Microsoft\Windows\CurrentVersion\Run`；
   - WLAN 连接断开、网络查询等全流程普通用户权限均可执行。

---

## 二、文件结构

```
NAU-AutoLogin/
├── .env                  # 敏感认证凭据 (chmod 0600，严禁提交至公共仓库)
├── .env.example          # 凭据示例模板
├── .gitignore            # Git 忽略配置
├── config.ini            # 运行策略配置 (网卡名称、检测周期、AP 优选参数等)
├── nau_autologin.pyw     # 核心守护进程 (pythonw 静默常驻、双网卡调度、AP 优选)
├── diagnose.py           # 多维只读自检与 AP 诊断工具 (单次运行输出完整状态)
├── install.ps1           # Windows PowerShell 运维管理脚本 (部署自启、状态、停止等)
└── README.md             # 本文档
```

---

## 三、快速开始

### 1. 配置认证凭据 (`.env`)
复制 `.env.example` 为 `.env` 并填写实际账号信息：
```ini
NAU_USERNAME=mp2609026
NAU_PASSWORD=你的密码
NAU_PORTAL_IP=10.255.254.23
NAU_DOMAIN=default
NAU_DETECT_URL=http://connect.rom.miui.com/generate_204
```

### 2. 检查与调整运行策略 (`config.ini`)
根据本机实际网卡名称配置：
```ini
[adapter]
eth_name = 以太网 2
wlan_name = WLAN
wlan_profile = i-NAU

[ap_optimization]
enable_ap_optimize = true
weak_signal_threshold = 45
high_rtt_threshold_ms = 150
debounce_cycles = 2
hysteresis_margin = 15
cooldown_seconds = 300
max_daily_reassociations = 15
```

### 3. 一键运行只读诊断
在部署前，建议先运行自检脚本排查当前网络与 AP 拓扑：
```powershell
python diagnose.py
```
该工具会输出：
- 双网卡状态、IP、网关；
- 当前关联 AP 的 BSSID、频段、信道、信号强度、协商速率与首跳延迟；
- 周围同 SSID 下所有可见 AP 评分排序与优化建议；
- 账号全量在线会话与外网 204 探针检测。

### 4. 一键部署开机自启
在 PowerShell 中执行：
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```
脚本会自动同步文件至 `%USERPROFILE%\.nau-autologin\`，配置开机自启，并在后台静默拉起守护进程。

---

## 四、服务运维与管理指令

通过 `install.ps1` 可以便捷管理后台守护进程：

```powershell
# 查看运行状态与最新 20 行日志
powershell -ExecutionPolicy Bypass -File install.ps1 -Status

# 运行完整自检诊断
powershell -ExecutionPolicy Bypass -File install.ps1 -Diagnose

# 停止后台守护进程
powershell -ExecutionPolicy Bypass -File install.ps1 -Stop

# 重启后台守护进程
powershell -ExecutionPolicy Bypass -File install.ps1 -Restart

# 卸载开机自启 (保留配置文件与日志)
powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
```

---

## 五、AP 智能优选工作机制说明

1. **去抖判定（Debounce）**：网卡当前信号低于 `weak_signal_threshold`（默认 45%）或网关 RTT 高于 `high_rtt_threshold_ms`（默认 150ms），连续达到 `debounce_cycles`（默认 2 个周期 = 60s）才确认链路劣化。
2. **滞回评估（Hysteresis）**：扫描周围所有同 SSID AP，仅当存在信号高出当前 AP `hysteresis_margin`（默认 15%）以上的强信号 AP 时才触发动作，避免在信号接近的 AP 间来回震荡。
3. **安全选优与会话续期**：执行 `disconnect` 后等待 2 秒再 `connect`，促使 Windows WLAN AutoConfig 重新打分接入优质 AP；重连后立即请求外网 204 探针，若会话失效则自动重新认证。
4. **冷却保护（Cooldown）**：每次重选后进入 5 分钟冷却期，且设立单日重选上限（默认 15 次），彻底杜绝网络环境全盘异常时的死循环。
