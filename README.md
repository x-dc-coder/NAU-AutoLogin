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
├── session_manager.py    # 门户会话管理 (在线列表/强制下线/IP 交接清理, 纯标准库跨平台)
├── sessions.py           # 会话管理命令行工具 (列表/下线/流量巡检/历史详单)
├── linux/                # Linux (树莓派) 部署
│   ├── nau_keepalive.py  # Linux 保活守护入口 (探测→登录→保活, 复用 session_manager)
│   └── config.ini.example # Linux 配置模板 (portal_ip / check_interval / 探针)
├── deploy/               # Linux 部署与升级
│   ├── nau-autologin.service # systemd 单元模板 (install.sh 渲染占位符)
│   └── install.sh        # 幂等安装/升级脚本 (releases 软链原子切换, 失败回滚)
├── install.ps1           # Windows PowerShell 运维管理脚本 (部署自启、状态、停止等)
└── README.md             # 本文档
```

---

## 三、快速开始

### 1. 配置认证凭据 (`.env`)
复制 `.env.example` 为 `.env` 并填写实际账号信息：
```ini
NAU_USERNAME=your_student_id
NAU_PASSWORD=your_password
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

## 五、多设备会话管理与"幽灵会话"清理

### 1. 什么是幽灵会话

校园网门户按 **IP** 统计在线会话。设备关机、重启、睡眠唤醒或无线漫游导致换 IP 时，
若未显式注销，旧 IP 的会话会一直残留直到门户空闲超时（通常 30 分钟 ~ 2 小时），
形成"幽灵会话"，白占账号的在线设备名额，严重时新设备无法登录。

### 2. IP 交接清理（自动，守护进程内置）

守护进程为每个网卡在本地 `state.json` 中记录最近使用过的 IP。当本次登录的 IP
与历史记录不一致时（DHCP 续租、AP 优选重连、睡眠唤醒、重启换 IP），自动把旧 IP
的残留会话强制下线：

- **只清理"本账号在线列表中存在"的旧 IP**，绝不会误伤其他设备（树莓派/笔记本/手机）；
- 当前 IP 永不下线，旧 IP 已被 DHCP 回收时自动跳过；
- IP 未变化时零 HTTP 开销；
- 清理失败只记日志，不影响保活主流程；
- 可在 `config.ini` 的 `[session_mgmt]` 段中关闭或调整记忆深度。

### 3. sessions.py 会话管理工具（手动清理其他设备的幽灵会话）

```powershell
# 列出账号全部在线会话 (IP / MAC / 区域 / 上线时间 / 实时流量)
python sessions.py -l

# 按 IP 或会话 id 强制下线
python sessions.py -k 10.200.34.163
python sessions.py -k 393587733

# 下线全部会话 (保留指定 IP 不下线)
python sessions.py -k-all --exclude 10.100.109.52

# 流量巡检: 两次采样, 找出"零流量且上线较久"的疑似幽灵会话 (只建议, 不自动杀)
python sessions.py -w --interval 600 --min-age 1800

# 历史会话详单 (核对某设备何时认证/掉线/漫游路径)
python sessions.py -d
```

也可在任意终端直接运行（注意用 `python.exe` 而非 `pythonw.exe`，后者无控制台输出）：

```powershell
python "$env:USERPROFILE\.nau-autologin\sessions.py" -l
python "$env:USERPROFILE\.nau-autologin\sessions.py" -k 10.200.34.163
python "$env:USERPROFILE\.nau-autologin\sessions.py" -w
```

### 4. 门户自服务接口备忘（实测于认证服务器）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/selfservice/v1/online` | 在线会话列表（含 id/IP/MAC/区域/流量/时长） |
| POST | `/api/selfservice/v1/online/kick/{id}` | 强制下线指定会话 |
| GET | `/api/selfservice/v1/acctdetail/{YYYYMM}` | 历史会话详单 |
| GET | `/api/selfservice/v1/authlog` | 认证日志 |

---

## 六、AP 智能优选工作机制说明

1. **去抖判定（Debounce）**：网卡当前信号低于 `weak_signal_threshold`（默认 45%）或网关 RTT 高于 `high_rtt_threshold_ms`（默认 150ms），连续达到 `debounce_cycles`（默认 2 个周期 = 60s）才确认链路劣化。
2. **滞回评估（Hysteresis）**：扫描周围所有同 SSID AP，仅当存在信号高出当前 AP `hysteresis_margin`（默认 15%）以上的强信号 AP 时才触发动作，避免在信号接近的 AP 间来回震荡。
3. **安全选优与会话续期**：执行 `disconnect` 后等待 2 秒再 `connect`，促使 Windows WLAN AutoConfig 重新打分接入优质 AP；重连后立即请求外网 204 探针，若会话失效则自动重新认证。
4. **冷却保护（Cooldown）**：每次重选后进入 5 分钟冷却期，且设立单日重选上限（默认 15 次），彻底杜绝网络环境全盘异常时的死循环。

---

## 七、Linux 部署（树莓派 dc-pi / i-NAU 门户保活）

面向树莓派等单 `wlan0` 链路设备的**最小保活守护**：`探测外网（204 探针超时 / 被 302 劫持到门户 = 掉线）→ POST 门户登录 → 按 `check_interval` 周期保活`。被踢下线（如手动 kick 自身 IP）后一个周期内自动重登。纯标准库实现，无任何第三方运行时依赖。

**明确不做**（Windows 场景专属）：双网卡故障转移、有线抢占、AP 优选、托盘/单实例互斥体、`sessions.py` 移植。

### 1. 文件与目录约定

| 路径 | 说明 |
|---|---|
| `linux/nau_keepalive.py` | 守护入口：探测 → 登录 → 保活，复用 `session_manager` 的 HTTP 层与 `PortalSessionManager`（登录后核验在线列表可见本机 IP） |
| `linux/config.ini.example` | 配置模板：仅 `portal_ip` / `check_interval` / 探测 URL |
| `deploy/nau-autologin.service` | systemd 单元模板（`Restart=always`、`WantedBy=multi-user.target`，由安装脚本渲染） |
| `deploy/install.sh` | 幂等安装/升级：解包 → venv（纯标准库自动跳过）→ 写配置 → `systemctl restart` → 健康检查 |
| `/opt/nau-autologin/releases/<ver>/` | 各版本完整解包 |
| `/opt/nau-autologin/current` | 指向当前版本的软链（原子切换，健康检查失败自动回滚） |
| `~/.nau-autologin/{config.ini,.env}` | 配置与凭据落位（跨版本保留，安装脚本永不覆盖） |

### 2. 快速开始

```bash
# ① 把仓库目录（或 CI 产出的 tar.gz 包）传到 pi
scp -r NAU-AutoLogin/ pi@dc-pi:~/          # 或: scp nau-autologin-1.0.0-linux.tar.gz pi@dc-pi:~/

# ② 首次安装（会生成凭据模板）
sudo ~/NAU-AutoLogin/deploy/install.sh --user pi

# ③ 填写凭据后重新执行一次安装（配置已存在不会覆盖，仅生效服务）
nano ~/.nau-autologin/.env                  # 填 NAU_USERNAME / NAU_PASSWORD
sudo ~/NAU-AutoLogin/deploy/install.sh --user pi
```

安装脚本健康检查会以运行用户执行 `nau_keepalive.py --once` 单轮检测：在 i-NAU 网内会真实登录一次；不在网内属预期（报 WARN 不影响安装）。

### 3. 服务运维

```bash
journalctl -u nau-autologin -f              # 实时日志（stdout → journald，轮转由 journald 管理）
sudo systemctl restart nau-autologin        # 重启
sudo systemctl stop nau-autologin           # 停止
python3 /opt/nau-autologin/current/linux/nau_keepalive.py --once   # 手动单轮检测/登录
```

### 4. 升级与回滚

```bash
sudo ./deploy/install.sh nau-autologin-1.1.0-linux.tar.gz   # 升级 = 解包新版本 + 原子切换软链
```

重启后健康检查失败时脚本自动回切上一版本软链并重启；新版本保留在 `releases/` 供排查。

### 5. CI/CD 打包约定（供后续 Release 流水线收口）

tar 包名为 `nau-autologin-<ver>-linux.tar.gz`，内容为仓库根目录**平铺**（或单一顶层目录，安装脚本两者兼容），必须包含：`session_manager.py`、`linux/nau_keepalive.py`、`linux/config.ini.example`、`.env.example`。链路：打 tag → GitHub Actions 打包发 Release → pi 定时拉取 → `install.sh` 原子切换 → 失败自动回滚。
