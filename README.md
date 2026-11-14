# PhoneNAS

把手机 WebDAV 挂载成 Windows 盘符，并把本地工作文件夹**单向镜像**到手机，自动保留历史版本。

Windows 10/11 · Python + PySide6 · rclone · WinFsp · 无广告

## 功能

- **挂载盘符**：把手机 WebDAV 挂成 `X:`（可改），像本地硬盘一样浏览
- **单向镜像**：本地工作文件夹 → 手机 `backup/work`
- **版本目录**：被覆盖或删除的文件自动进入 `backup/work-versions/<时间戳>/`
- **自动恢复**：挂载异常退出按 5/15/30/60/120 秒退避重挂
- **可调频率**：5–1440 分钟
- **可调保留**：1–3650 天（默认 30 天）
- **排除规则**：推荐（排除构建产物、保留 `Release`）/ 不排除 / 自定义
- **版本恢复**：按时间戳浏览、恢复单文件或整个版本、手动删除
- **托盘常驻**：关闭窗口最小化到托盘，可开机自启
- **离线部署**：内置 rclone 与 WinFsp MSI，不依赖目标电脑联网
- **密码安全**：Windows DPAPI 加密，导出配置不含密码

## 下载安装

到 [Releases](https://github.com/popqco/PhoneNAS/releases/latest) 下载：

| 文件 | 说明 |
| --- | --- |
| `PhoneNAS-Setup-x.y.z.exe` | 安装包，创建开始菜单/卸载项，可选开机自启 |
| `PhoneNAS-portable.zip` | 便携版，解压即用，配置保存在程序目录 `data\` |

> 首次使用需要安装 WinFsp（安装包已内置，程序会提示并弹出一次管理员确认）。

## 工作方式

```
本地工作文件夹 (D:\LefeetHardware)
        │
        │  每 N 分钟 rclone sync
        ▼
手机 WebDAV /backup/work                 ← 当前镜像
        │
        ├── 被覆盖/删除的文件
        ▼
手机 WebDAV /backup/work-versions/<时间戳> ← 历史版本
```

盘符 `X:` 是通过 `rclone mount` + WinFsp 实时挂载的网络盘，和镜像任务互不影响：

- `X:` 用于浏览、拷贝、打开文件
- `backup/work` 由定时任务维护，**不要直接编辑**
- `backup/work-versions` 可随时浏览和恢复

## 首次使用

1. 启动 PhoneNAS
2. 填手机 WebDAV 地址（Tailscale IP 或局域网 IP）、用户名、密码
3. 点「测试连接」
4. WinFsp 未安装时点「安装 WinFsp」（一次管理员确认）
5. 选择盘符、工作文件夹、同步频率、版本保留天数
6. 保存后自动挂载，并立即执行第一次完整同步

## 目录说明

| 位置 | 说明 |
| --- | --- |
| `X:\` | 手机 WebDAV 根目录 |
| `X:\backup\work` | 工作文件夹的当前镜像（只读心态） |
| `X:\backup\work-versions\<时间戳>` | 每次同步被覆盖/删除的旧文件 |
| `X:\cc-switch-sync` | CC Switch 自己的同步目录（本程序不管理） |

## 版本恢复

1. 打开「版本」页，刷新
2. 选一个时间戳版本
3. 「查看内容 / 恢复文件」恢复单个文件；「恢复整个版本」复制到指定目录
4. 「删除版本」手动清理；保留天数到期也会自动清理

## 从源码运行

```bash
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File build\fetch-vendor.ps1
python -m app
```

`fetch-vendor.ps1` 会下载并校验 rclone 和 WinFsp；如果访问 GitHub 较慢，可以传入镜像前缀：

```powershell
powershell -ExecutionPolicy Bypass -File build\fetch-vendor.ps1 -Mirror "https://ghfast.top"
```

## 从源码构建

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

产物：

- `dist\PhoneNAS\` — 免安装目录版（配置在 `%APPDATA%\PhoneNAS`）
- `dist\PhoneNAS-portable.zip` — 便携版（配置在解压目录 `data\`）
- `dist\PhoneNAS-Setup-1.0.0.exe` — 安装包（需要 Inno Setup）

## 项目结构

```
app/
  __main__.py       入口、单实例、自检
  constants.py      默认值与排除规则
  core/
    config.py       配置、DPAPI、便携模式
    rclone.py       rclone 子进程封装
    mount.py        挂载 supervisor
    sync.py         镜像任务
    retention.py    版本解析与清理
    scheduler.py    定时器
    winfsp.py       WinFsp 检测/安装
    autostart.py    开机自启
  ui/
    main_window.py  主窗口与托盘
    wizard.py       首次向导
    pages/          状态、镜像、版本、日志、设置
build/
  fetch-vendor.ps1  下载 rclone/WinFsp
  PhoneNAS.spec     PyInstaller 配置
  installer.iss     Inno Setup 安装包
  build.ps1         一键构建
tests/              单元测试
```

## 已验证

- `D:\LefeetHardware` → `phone:backup/work`：1.770 GiB / 2,884 个文件，约 3 分钟，约 13 MB/s
- 第二次同步：`0 B / There was nothing to transfer`
- `node_modules`、`app/build`、`build` 等构建产物被排除，`Release` 交付 APK 保留
- 修改/删除后的旧文件进入版本目录，单文件恢复内容校验通过
- 便携版独立目录自检通过

## 常见问题

**连接失败**
- 确认手机 Tailscale 在线，电脑登录了同一账号
- 手机 WebDAV 默认端口 8888，地址示例 `http://100.109.166.11:8888`
- 可先用浏览器打开该地址验证账号密码

**盘符被占用**
- 在「设置」里换一个盘符，保存后自动重挂

**挂载失败**
- 确认 WinFsp 已安装（设置页会显示）
- 查看 `%APPDATA%\PhoneNAS\logs\`

**同步很慢**
- 第一次是完整复制，之后只传变化文件
- 用排除规则跳过 `node_modules`、构建产物等

## 第三方组件

- [rclone](https://rclone.org/) — MIT
- [WinFsp](https://winfsp.dev/) — GPLv3（含特殊例外）

详见 [build/LICENSES.md](build/LICENSES.md)。

## License

MIT
