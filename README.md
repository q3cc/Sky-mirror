<h1 align="center">Sky-mirror</h1>

<p align="center">
  定时镜像 Google Play 上的 <b>Sky: Children of the Light</b> Android 安装文件，<br>
  保留原始组件，提供 SHA-256 校验和国内网盘分流。
</p>

<p align="center">
  <a href="https://github.com/q3cc/Sky-mirror/releases/latest"><b>GitHub Release</b></a> ·
  <a href="https://1829561701.share.123pan.cn/123pan/uN8cjv-0hjPH"><b>123云盘国内分流</b></a> ·
  <a href="https://play.google.com/store/apps/details?id=com.tgc.sky.android">Google Play</a>
</p>

<p align="center">
  <a href="https://github.com/q3cc/Sky-mirror/actions/workflows/mirror.yml"><img src="https://github.com/q3cc/Sky-mirror/actions/workflows/mirror.yml/badge.svg" alt="Mirror Sky from Google Play"></a>
  <a href="https://github.com/q3cc/Sky-mirror/actions/workflows/ci.yml"><img src="https://github.com/q3cc/Sky-mirror/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://1829561701.share.123pan.cn/123pan/uN8cjv-0hjPH"><img src="https://img.shields.io/badge/国内分流-123云盘-00a1e9" alt="123云盘国内分流"></a>
</p>

`Sky-mirror` 每 6 小时检查一次 Google Play 上的 **Sky: Children of the Light** Android 版本。发现更新后，它会下载 Play 实际交付的 APK、split APK 和附加资产，整理成可校验的分片 ZIP，并发布到 GitHub Releases。

仓库里只有镜像脚本和工作流，不保存安装包。ZIP 内的安装组件保持 Google Play 交付的原始字节，不合并 APK、不修改内容，也不重新签名。

- Google Play：<https://play.google.com/store/apps/details?id=com.tgc.sky.android>
- Package：`com.tgc.sky.android`
- 默认架构：`arm64`
- 最新版本：[前往 Releases 下载](https://github.com/q3cc/Sky-mirror/releases/latest)

> 这是非官方镜像，与 Google、Google Play、thatgamecompany 没有隶属关系。

## 下载

| 渠道 | 地址 | 说明 |
|---|---|---|
| GitHub Releases | [下载最新版本](https://github.com/q3cc/Sky-mirror/releases/latest) | 工作流自动发布，包含版本清单和校验文件 |
| 国内网盘 | [123云盘分流](https://1829561701.share.123pan.cn/123pan/uN8cjv-0hjPH) | 适合 GitHub 下载较慢时使用 |

国内网盘不属于 GitHub Actions 自动发布流程。通过网盘下载后，建议找到相同版本，并用 GitHub Release 中的 `SHA256SUMS.txt` 核对文件。

每个 Release 都会提供 ZIP、校验文件和版本清单：

- `sky-android-arm64.zip`：文件不超过 1 GB 时直接发布
- `sky-android-arm64.zip.001`、`.002`……：超过 1 GB 时发布连续分片
- `rebuild-zip.ps1`、`rebuild-zip.sh`：分片发布时提供的 Windows、Linux 和 Android Termux 还原脚本
- `SHA256SUMS.txt`：Release Assets 校验值
- `release-manifest.json`：版本、架构、组件和校验信息
- `download-index.json`：当前版本的全部下载链接

可以在 [最新 Release](https://github.com/q3cc/Sky-mirror/releases/latest) 页面手动下载全部 ZIP 分片和对应的还原脚本，也可以用 GitHub CLI 一次取回：

```bash
gh release download --repo q3cc/Sky-mirror \
  --pattern 'sky-android-arm64.zip*' \
  --pattern 'rebuild-zip.*'
```

程序或下载器可以使用这几个固定入口，不需要先查询最新 tag：

```text
https://github.com/q3cc/Sky-mirror/releases/latest/download/download-index.json
https://github.com/q3cc/Sky-mirror/releases/latest/download/SHA256SUMS.txt
https://github.com/q3cc/Sky-mirror/releases/latest/download/release-manifest.json
```

## 还原 ZIP

把所有分片和还原脚本放在同一目录。

Windows PowerShell：

```powershell
.\rebuild-zip.ps1
```

Linux 或 Android Termux：

```bash
bash rebuild-zip.sh
```

脚本会生成 `sky-android-arm64.zip`，并自动核对完整 ZIP 的 SHA-256。解压后，安装组件位于 `packages/`，压缩包内的 `INSTALL.txt` 也会保留基本安装提示。

## 安装

现代 Play 应用通常由 base APK 和多个 split APK 组成，只安装 base APK 往往不能正常运行。连接 Android 设备并开启 USB 调试后，在 ZIP 解压目录执行：

```bash
adb install-multiple packages/com.tgc.sky.android-*.apk
```

如果包内包含 `.obb`，还需要按 Android 的 OBB 目录规则放到对应应用目录。Play Asset Delivery 的部分大型资源也可能在应用首次启动后继续下载。

## 镜像怎么更新

1. `probe` 使用 `gplaydl` 查询 Google Play 当前的 `versionName` 和 `versionCode`。
2. 脚本把稳定版本字段与最新 Release 中的 `release-manifest.json` 比较。
3. 版本没变就结束，不下载，也不重复发版。
4. 发现新版后，工作流下载 base APK、配置 split 和附加资产，并核对探测版本与实际下载版本。
5. 脚本生成 ZIP、`SHA256SUMS.txt`、`release-manifest.json` 和 `download-index.json`。ZIP 超过 1 GB 时自动切成连续分片。
6. 工作流先创建 Draft Release；所有 Assets 上传成功后，才把它发布为 Latest。

`gplaydl` 会按 Google Play 交付清单中的 SHA-256 校验下载内容，仓库自己的发布脚本还会为 ZIP、分片和清单记录校验值。

## 在自己的仓库运行

如果你只是下载 Sky 安装文件，前面的下载和安装章节已经够用。下面这些配置只面向要 fork 仓库、自行维护镜像的人。

工作流通过非官方 Google Play 客户端访问 Play，需要一个独立 Google 账号。不要使用绑定付款方式或存有重要数据的主账号。

### 准备 gplaydl API Key

下面以 Windows PowerShell 为例。命令会把虚拟环境、依赖和 `gplaydl` 配置都放在仓库目录内，并禁用 pip 缓存：

```powershell
Set-Location D:\Documents\Sky-mirror
$env:PIP_NO_CACHE_DIR = "1"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --requirement requirements.txt
$env:USERPROFILE = Join-Path $PWD ".gplaydl-home"
.\.venv\Scripts\gplaydl.exe link
```

按照终端提示，在 Android 手机上的 gplaydl Authenticator 中添加账号并完成配对。配对成功后读取生成的 Key：

```powershell
$config = Get-Content .\.gplaydl-home\.config\gplaydl\config.json | ConvertFrom-Json
$config.api_key
```

### 配置 GitHub Secrets

进入仓库的 `Settings` → `Secrets and variables` → `Actions` → `New repository secret`，添加：

| Secret | 必需 | 说明 |
|---|---|---|
| `GPLAYDL_API_KEY` | 是 | `config.json` 中的 `api_key` |
| `GPLAYDL_EMAIL` | 否 | 配置了多个账号时指定其中一个 |

API Key 只放在 GitHub Secret 中，不要提交到仓库。

### 调整镜像目标

镜像参数集中在 [`mirror-config.json`](mirror-config.json)：

| 配置 | 当前值 | 用途 |
|---|---|---|
| `app.packageName` | `com.tgc.sky.android` | Google Play 包名 |
| `download.architecture` | `arm64` | 下载架构 |
| `download.locales` | `[]` | 需要额外下载的语言 split |
| `download.includeSplits` | `true` | 包含配置 split |
| `download.includeExtras` | `true` | 包含 OBB 和附加资产 |
| `download.includeDexMetadata` | `false` | 是否包含 Dex Metadata |
| `release.bundlePartMaxBytes` | `1000000000` | 单个 ZIP 分片上限 |
| `release.domesticMirrorUrl` | `https://1829561701.share.123pan.cn/123pan/uN8cjv-0hjPH` | 国内网盘分流入口 |

`gplaydl` 当前支持的架构值为 `arm64`、`armv7`、`x86`、`x86_64` 和 `tv`。

### 手动触发

打开仓库的 `Actions` → `Mirror Sky from Google Play` → `Run workflow`。

`force_release` 会强制重新准备当前版本，适合修复不完整的 Release。正常探测更新时保持关闭。

## 本地测试

测试不会访问 Google Play，也不需要真实 APK：

```bash
python -m unittest discover --start-directory tests --verbose
python -m py_compile scripts/mirror.py tests/test_mirror.py
```

## 风险与边界

- Google 可以随时调整私有 Play 协议，届时可能需要升级 `gplaydl`。
- 账号、地区、设备配置和分批发布会影响可见版本；镜像看到的版本不一定和每位用户同时更新。
- 请遵守应用许可、Google Play 条款和所在地区法律，不要用它镜像付费或 DRM 内容。
- 本项目不修改应用、不绕过授权，也不移除 DRM。

## 致谢

自动化结构参考 [Wangnov/codex-app-mirror](https://github.com/Wangnov/codex-app-mirror)，Google Play 下载由 [rehmatworks/gplaydl](https://github.com/rehmatworks/gplaydl) 提供。

## License

MIT
