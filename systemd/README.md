# 🚀 Metro PIS Systemd 服务配置指南

本目录包含用于将 **Metro PIS** 作为守护进程常驻运行的 systemd 配置文件模板。

---

## 📌 方案一：推荐使用 systemd 用户服务（User Service · 免 root 权限）

适用于 Linux 各大发行版（Fedora、Debian、Ubuntu、Arch、RHEL 等）：

### 1. 复制服务文件到用户 systemd 目录

```bash
mkdir -p ~/.config/systemd/user
cp systemd/metropis-server.service ~/.config/systemd/user/
cp systemd/metropis-simulator.service ~/.config/systemd/user/
```

> 💡 **提示**：服务单元中默认的工作路径为 `%h/Projects/MetroPIS`（`%h` 即当前用户的家目录）。如果在服务器上存放在其他位置，请修改文件中的 `WorkingDirectory`。

### 2. 启用并启动服务

```bash
systemctl --user daemon-reload
systemctl --user enable --now metropis-server.service
systemctl --user enable --now metropis-simulator.service
```

### 3. 开启用户常驻（开机免登录自启动）

```bash
loginctl enable-linger $USER
```

---

## 📌 方案二：作为系统级服务运行（System Service · 需要 root 权限）

若要部署在专用服务器全局系统目录下：

### 1. 修改路径与执行用户

将 `.service` 文件中的 `WorkingDirectory` 和 `User` 设为实际用户与路径（例如 `/opt/MetroPIS`）：

```ini
[Service]
User=www-data
WorkingDirectory=/opt/MetroPIS
ExecStart=/usr/bin/python3 server.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
```

### 2. 安装与管理

```bash
sudo cp systemd/metropis-server.service /etc/systemd/system/
sudo cp systemd/metropis-simulator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now metropis-server metropis-simulator
```

---

## 🛠️ 日常运维管理命令

```bash
# 查看实时日志（带时间戳）
journalctl --user -u metropis-server -f
journalctl --user -u metropis-simulator -f

# 查看服务运行状态
systemctl --user status metropis-server
systemctl --user status metropis-simulator

# 重启或停止服务
systemctl --user restart metropis-server
systemctl --user stop metropis-server
```
