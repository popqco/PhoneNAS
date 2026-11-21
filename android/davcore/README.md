# WebDAV 合同验证

本目录保留开发时使用的有限验收脚本，不是生产服务、迁移工具或电脑保活任务。执行前确认目标和文件都是为验收单独创建的；不在已有 `X:`、用户目录、工作镜像或旧数据集上运行。

- `probe_webdav.py` 在随机本机回环端口用 rclone 导出临时目录。历史参考候选没满足独占 MKCOL 和截断 PUT 合同，不能代替 PhoneNAS Go 内核。原始报告不入库。
- `test_native_contract.py`、`test_native_lifecycle.py` 使用独立设备上的 App 演示根。生命周期测试会操控选定测试设备；不得给生产手机执行。
- `test_native_relay.py` 使用已授权的本机回环入口和固定合成验收共享，调用实际桌面同步引擎；不能声称是外网联调。
- `test_native_remote.py` 用显式提供的私有合成凭据、服务器公开证书和独立核对的指纹验证设备上的隔离 HTTPS 共享；不自动发现或重配设备。
- `test_native_*_safety.py` 验证脚本自己的参数和清理范围；`audit_runtime_journal.py` 可以离线核对已导出的脱敏结构化记录。

Go 主机合同优先从 `../gokernel` 运行 `go test -count=1 ./...`。测试只在临时根下工作；原始验收报告、已配置地址和私有输入均不在仓库中。[验收结论与边界](../../docs/ANDROID-ACCEPTANCE.md)。
