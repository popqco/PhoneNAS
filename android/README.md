# PhoneNAS Android · 维护分支

| 模块 | 包名 | 用途 |
| --- | --- | --- |
| `:app` | `com.phonenas.server.probe` | 在存放文件的手机上托管选定的只读或读写共享；见[服务端部署](../docs/ANDROID-USER-GUIDE.md) |
| `:client` | `com.phonenas.client` | 在访问手机上连接、浏览、下载并向指定共享上传；见[客户端用法](../docs/ANDROID-CLOUD-CLIENT.md) |

两包独立构建。不替换旧 Termux/WsgiDAV，不内嵌或控制官方 Tailscale，不要求电脑在线。已有目录、连接、锁和同步基线不自动迁移。[验收记录](../docs/ANDROID-ACCEPTANCE.md)区分历史候选、当前构建和未验证的使用场景。

## 干净检出构建

| 依赖 | 版本 |
| --- | --- |
| JDK / Gradle / AGP | 21 / 8.13 / 8.13.1 |
| Kotlin / Compose BOM | 2.2.21 / 2025.11.00 |
| Android compile / target / min SDK | 36 / 36 / 30 |
| Android Build Tools | 36.0.0 |
| 服务端 Go / x/net | 1.27.4 / v0.62.0 |
| 服务端 x/mobile / NDK | v0.0.0-20261011034528-197770444867 / 27.2.12479018 |
| 服务端 native ABI | arm64-v8a |

1. 安装前述 JDK、Gradle 和 Android SDK；仅构建客户端时不需要 Go 或 NDK。本仓库不附带 Gradle wrapper，使用 Gradle **8.13**。设置 `JAVA_HOME`，将 Gradle 加到 `PATH`。用 `ANDROID_HOME` 指向本机 SDK；也可在 `android/local.properties` 中设 `sdk.dir`（如 `sdk.dir=C:/Android/Sdk`）。首次构建需要解析 Gradle/Android 依赖；本机缓存齐全后可加 `--offline`。
2. 客户端（不依赖服务端生成库），从仓库根执行：

   ```powershell
   gradle -p android --no-daemon :client:testDebugUnitTest :client:testCandidateUnitTest `
     :client:lintCandidate :client:assembleCandidate
   ```

3. 服务端：安装对应 NDK。将 Go 1.27.4 放入 `PATH`，用同一 Go 版本安装 **同版本**的 `gomobile` 和 `gobind`，二者放在同一工具目录；确保该目录可被 `build-native.ps1` 找到。生成的 `android/app/libs/phonenas-kernel.aar` 不提交。

   ```powershell
   go install golang.org/x/mobile/cmd/gomobile@v0.0.0-20261011034528-197770444867
   go install golang.org/x/mobile/cmd/gobind@v0.0.0-20261011034528-197770444867
   $goBin = (go env GOPATH).Split([IO.Path]::PathSeparator)[0] + '\bin'
   powershell -NoProfile -File android/build-native.ps1 `
     -GoExe (Get-Command go).Source -GoMobileExe (Join-Path $goBin 'gomobile.exe') `
     -SdkRoot $env:ANDROID_HOME
   gradle -p android --no-daemon :app:testDebugUnitTest :app:testCandidateUnitTest `
     :app:lintCandidate :app:assembleCandidate
   ```

4. 产物分别为 `client/build/outputs/apk/candidate/client-candidate.apk`、`app/build/outputs/apk/candidate/app-candidate.apk`（相对于 `android/`）。用 `apksigner verify --verbose --print-certs` 核对签名，`zipalign -c -P 16 4` 检查 APK 内的对齐，逐个用 `llvm-readelf -lW` 核对原生 `.so` 的 `PT_LOAD` 对齐不小于 `0x4000`，再记录 APK SHA-256、版本和包名。候选不可调试；服务端候选不包含 `src/debug` 的控制 Activity。

`candidate` 的开发签名来自当前构建机；另一台机器的签名不会自动匹配已安装包。不要以卸载、清数据或关闭系统安装验证处理签名不一致。无发布或上架签名材料。

## 可重复的验收入口

- `gokernel/`：Go `go test -count=1 ./...`、`go vet ./...`。使用临时目录、合成文件和隔离监听器。
- `app/src/test/`、`client/src/test/`：Android JVM 规则和 HTTPS 合同。
- `davcore/`：[测试范围](davcore/README.md)；设备脚本仅用于明确配置好的合成共享，不读取已有配置或自动启动/配置旧机。
- Windows 的 `tests/`：使用临时配置及隔离数据。不需要现用盘符。

本分支不附带 APK、AAR、密钥、私有配置、原始设备报告或 UI 记录。单测、合成 VPN、USB 回环和真实局域网/外网测试是不同证据，不相互替代。
