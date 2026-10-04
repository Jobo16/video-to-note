param(
    [string]$Python = "",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

if (-not $Python) {
    $Python = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python 解释器不存在: $Python"
}

$versionLine = Select-String -Path (Join-Path $projectRoot "launcher.py") -Pattern '^VERSION = "([^"]+)"' | Select-Object -First 1
if (-not $versionLine) { throw "无法从 launcher.py 读取 VERSION" }
$version = $versionLine.Matches[0].Groups[1].Value
Write-Host "打包版本: $version" -ForegroundColor Cyan

# 版本元数据同步：从模板（scripts/version_info.txt.template）生成 version_info.txt，
# 版本号取自 launcher.py 的 VERSION，避免 exe 属性页版本号滞后
$versionInfoTemplate = Join-Path $projectRoot "scripts\version_info.txt.template"
$versionInfoPath = Join-Path $projectRoot "scripts\version_info.txt"
$templateText = Get-Content -LiteralPath $versionInfoTemplate -Raw -Encoding UTF8
$cleanVersion = $version -replace '[^0-9.].*$', ''
$versionParts = @(($cleanVersion -split '\.' | ForEach-Object { [int]$_ }) + @(0, 0, 0))[0..3]
$fileVersion = $versionParts -join '.'
$versionInfoText = $templateText `
    -replace '__VERSION_PARTS__', ($versionParts -join ', ') `
    -replace '__VERSION__', $fileVersion
[System.IO.File]::WriteAllText($versionInfoPath, $versionInfoText, [System.Text.Encoding]::UTF8)

if (-not $SkipInstall) {
    & $Python -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller 安装失败" }
}

Set-Location -LiteralPath $projectRoot
& $Python -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name "VideoToNo" `
    --add-data "frontend;frontend" `
    --add-data "sources/icon.png;sources" `
    --add-data "sources/icon.ico;sources" `
    --collect-data faster_whisper `
    --collect-all sherpa_onnx `
    --collect-submodules mcp.server `
    --collect-data mcp `
    --version-file "scripts\version_info.txt" `
    --icon "sources\icon.ico" `
    --hidden-import pystray._win32 `
    --exclude-module tkinter `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

$target = Join-Path $projectRoot "dist\VideoToNo-$version-portable.exe"
Copy-Item -LiteralPath (Join-Path $projectRoot "dist\VideoToNo.exe") -Destination $target -Force
# 固定名副本：桌面快捷方式指向它，升级后无需改快捷方式目标
$latest = Join-Path $projectRoot "dist\VideoToNo-portable.exe"
Copy-Item -LiteralPath (Join-Path $projectRoot "dist\VideoToNo.exe") -Destination $latest -Force
Remove-Item -LiteralPath (Join-Path $projectRoot "dist\VideoToNo.exe") -Force

# 包内自带的 VC++ 运行时必须是现代版本。PyInstaller 收的是构建机 System32 的那一份，
# 而包内 DLL 在搜索顺序上盖过用户自己的 System32 —— 构建机脏一次，每个用户拿到的包都
# 跟着崩。2026-10 群测 faster-whisper 与 sherpa-onnx 两个引擎都 0xC0000005，出错模块
# MSVCP140.dll 14.0.24215.1，就是这么漏出去的（CI 那份是 14.40，所以只影响本地包）。
# 门槛定 14.10：msvcp140_1.dll 这个组件从 VS2017 15.3 才有，base 比 _1 老一整条产品线是
# 不被支持的组合。宁可构建红叉，也不要发一个"用户一点就静默消失、日志一行不剩"的包。
$runtimeDir = Join-Path $projectRoot ".tmp\bundle-runtimes"
& $Python (Join-Path $projectRoot "scripts\extract_bundle_runtimes.py") $target --out $runtimeDir
if ($LASTEXITCODE -ne 0) { throw "抽不出包内运行时，无法判断版本（PyInstaller 可能没收到）" }
foreach ($dllName in @("msvcp140.dll", "vcruntime140.dll")) {
    $dllPath = Join-Path $runtimeDir $dllName
    if (-not (Test-Path -LiteralPath $dllPath)) { throw "包里没有 $dllName" }
    $version = (Get-Item -LiteralPath $dllPath).VersionInfo.FileVersion
    if ($version -notmatch '^(\d+)\.(\d+)') { throw "$dllName 版本号读不出来：$version" }
    Write-Host ("  包内运行时 {0} -> {1}" -f $dllName, $version)
    if ([int]$Matches[1] -lt 14 -or ([int]$Matches[1] -eq 14 -and [int]$Matches[2] -lt 10)) {
        throw ("包内 $dllName 是 $version，低于 14.10。先修复构建机的 VC++ 2015-2022 可再发行组件" +
               "（System32 里那份太旧）再重打，否则这个包会在转写时崩掉用户的任务。")
    }
}

Write-Host ""
Write-Host "构建完成: $target" -ForegroundColor Green
Write-Host "固定名（快捷方式可指向此文件）: $latest" -ForegroundColor Cyan
Write-Host "大小: $([math]::Round((Get-Item -LiteralPath $target).Length / 1MB, 1)) MB"
