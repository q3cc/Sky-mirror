# Sky-mirror

自动探测并镜像 Google Play 上的 **Sky: Children of the Light** Android 安装文件。

- Google Play: <https://play.google.com/store/apps/details?id=com.tgc.sky.android>
- Package: `com.tgc.sky.android`
- 默认架构: `arm64`
- 调度: 每 6 小时探测一次，也支持手动运行

本仓库只保存自动化代码。APK、split APK、OBB 和 Play Asset Delivery 文件封装为分片 ZIP，通过 GitHub Release Assets 发布，不进入 Git 历史。

## 工作方式

1. `probe` 使用 `gplaydl` 查询当前 `versionName` 和 `versionCode`。
2. 将稳定版本字段与最新 Release 中的 `release-manifest.json` 比较。
3. 没有变化时立即结束，不下载、不重复发版。
4. 发现新版时，从 Google Play 下载 base APK、配置 split 和附加资产，封装为完整 ZIP。
5. 校验探测版本与下载版本一致，生成 `SHA256SUMS.txt`、`release-manifest.json` 和 `download-index.json`。
6. 先创建 Draft Release，所有资产上传成功后再发布为 Latest。

文件保持 Google Play 交付的原始字节，不合并、不修改、不重新签名。`gplaydl` 还会按 Google Play 交付清单提供的 SHA-256 校验下载内容。

## 配置 GitHub Secret

此工作流通过非官方 Google Play 客户端访问 Play，需要使用独立 Google 账号。不要使用绑定付款方式或重要数据的主账号。

在 Windows PowerShell 中准备 API Key。以下命令将虚拟环境、依赖和 gplaydl 配置都放在 D 盘仓库内，并禁用 pip 缓存：

```powershell
Set-Location D:\Documents\Sky-mirror
$env:PIP_NO_CACHE_DIR = "1"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --requirement requirements.txt
$env:USERPROFILE = Join-Path $PWD ".gplaydl-home"
.\.venv\Scripts\gplaydl.exe link
```

按照提示在 Android 手机上的 gplaydl Authenticator 中添加账号并完成配对，然后读取生成的 Key：

```powershell
$config = Get-Content .\.gplaydl-home\.config\gplaydl\config.json | ConvertFrom-Json
$config.api_key
```

进入 GitHub 仓库：

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

添加：

| Secret | 必需 | 说明 |
|---|---|---|
| `GPLAYDL_API_KEY` | 是 | `config.json` 中的 `api_key` |
| `GPLAYDL_EMAIL` | 否 | 配置了多个账号时指定其中一个 |

API Key 只放在 GitHub Secret 中，不要提交到仓库。

## 下载

仓库发布后可使用以下固定入口，把 `<owner>` 替换为仓库所有者：

```text
https://github.com/<owner>/Sky-mirror/releases/latest
https://github.com/<owner>/Sky-mirror/releases/latest/download/download-index.json
https://github.com/<owner>/Sky-mirror/releases/latest/download/SHA256SUMS.txt
https://github.com/<owner>/Sky-mirror/releases/latest/download/release-manifest.json
https://github.com/<owner>/Sky-mirror/releases/latest/download/sky-android-arm64.zip.001
```

工作流始终生成 `sky-android-arm64.zip`；文件超过 1 GB 时自动切成 `.001`、`.002`、`.003` 等连续分片。`download-index.json` 会列出当前版本的全部分片链接。

下载全部分片和还原脚本，在同一目录运行：

```powershell
.\rebuild-zip.ps1
```

Linux 或 Android Termux 可运行：

```bash
bash rebuild-zip.sh
```

脚本会生成 `sky-android-arm64.zip` 并自动核对完整 ZIP 的 SHA-256。

## 安装

现代 Play 应用通常由 base APK 和多个 split APK 组成，base APK 不能单独安装。还原并解压 ZIP 后可使用：

```bash
adb install-multiple packages/com.tgc.sky.android-*.apk
```

如果 Release 还包含 `.obb`，需要按 Android 对 OBB 的要求放入对应应用目录。大型 Play Asset Delivery 内容也可能由应用首次启动后继续获取。

## 手动运行

打开仓库的 `Actions` → `Mirror Sky from Google Play` → `Run workflow`。

`force_release` 会强制重新准备当前版本，主要用于修复不完整的 Release。正常更新保持关闭即可。

## 本地测试

测试不访问 Google Play，也不需要真实 APK：

```bash
python -m unittest discover --start-directory tests --verbose
python -m py_compile scripts/mirror.py tests/test_mirror.py
```

## 风险与边界

- 这是非官方镜像，与 Google、Google Play、thatgamecompany 无隶属关系。
- Google 可以随时改变私有 Play 协议，届时可能需要升级下载器。
- 账号、地区、设备配置和分批发布都会影响可见版本。
- 请遵守应用许可、Google Play 条款和所在地区法律，不要镜像付费或 DRM 内容。
- 本项目不修改应用、不绕过授权、不移除 DRM。

## 致谢

自动化结构参考 [Wangnov/codex-app-mirror](https://github.com/Wangnov/codex-app-mirror)，Google Play 下载由 [rehmatworks/gplaydl](https://github.com/rehmatworks/gplaydl) 提供。

## License

MIT
