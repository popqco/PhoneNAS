# PhoneNAS 第三方组件许可

本程序使用以下第三方组件，均以各自许可证分发：

## rclone

- 项目：https://rclone.org/
- 许可证：MIT
- 用途：WebDAV 挂载与文件同步

## WinFsp

- 项目：https://winfsp.dev/
- 许可证：GPLv3（含特殊例外）
- 用途：Windows 用户态文件系统驱动，rclone mount 依赖

WinFsp 安装包（`vendor/winfsp.msi`）来自官方发布：
https://github.com/winfsp/winfsp/releases/tag/v2.1
