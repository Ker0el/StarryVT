# 构建星译

## 环境

- Windows 10 / 11 x64
- Python 3.12
- .NET 8 SDK
- Inno Setup 6（默认找 `D:\Program Files (x86)\Inno Setup 6\ISCC.exe`，也可以用环境变量 `ISCC` 指定）

## 步骤

```powershell
git clone https://github.com/Ker0el/StarryVT.git
cd StarryVT

# 下载随包工具（ffmpeg / ffprobe / yt-dlp）和引擎离线包
python fetch_tools.py

# 后端（Nuitka）→ 界面（dotnet publish）→ 组装 → Inno 安装包
python build.py
```

产物：

- `_stage\app\`：完整程序目录
- `installer\StarryVT-Setup_v<版本>.exe`：安装包

只想改界面时，可以不重编后端：

```powershell
dotnet publish winui\StarryVt\StarryVt.csproj -c Release -p:Platform=x64 `
  -p:RuntimeIdentifier=win-x64 -o _stage\app --nologo
& "D:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

## 没有随源码分发的素材

以下文件因为第三方授权或个人信息原因不放在源码仓库里：

- `winui\StarryVt\Assets\bg-default.mp4`：默认视频背景
- `winui\StarryVt\Assets\hero.png`：背景视频的静帧
- `winui\StarryVt\Assets\cursors\`：购买的光标主题
- `winui\StarryVt\Assets\vx.png` / `zanshang.png`：联系方式和赞赏码
- `web\zs.png`：赞赏码
- `assets\ui-*.jpg`：带第三方视频画面的界面截图

缺这些文件不影响编译。背景视频缺失时程序会退回内置深色底图；光标和二维码缺失时对应位置为空。自行构建时请替换成你有权使用的素材。

## 运行依赖

翻译引擎本体及其 Python 依赖随安装包分发（`engine_whl\`，GPL-3.0 纯 Python wheel，内含源码）；
引擎运行环境和模型由程序在用户机器上首次使用时从上游下载。详见
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
