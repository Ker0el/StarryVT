; 星译 —— Inno Setup 安装脚本（3.0 起：WinUI 3 界面 + 随包后端）
;
; 目录结构（_stage\app 是 publish + 组装的结果，[Files] 直接整棵搬）：
;   {app}\StarryVt.exe      ← WinUI 3 界面（无 .NET 运行时也能跑，自包含发布）
;   {app}\backend\          ← Python 后端（PyInstaller 打的，含 ffmpeg / yt-dlp / 引擎安装器）
;   {app}\licenses\         ← 第三方组件许可证原文
;
#define AppName "星译"
#define AppVersion "6.39"
#define AppExeName "StarryVt.exe"
#define StageDir "_stage\app"

[Setup]
AppId={{7E3B9C2A-5D41-4A6E-9F2B-1C8E4A6D2B91}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
; 默认装到「安装包自己所在目录」下的 StarryVT\ —— 用户把安装包放到想装的盘上双击即可。
;
; ⚠⚠ 这个 {src} 只适合**用户手动双击**的场景，绝不能当成「自动更新会装到哪」的依据：
;   {src} = 安装包自己所在的目录，而自动更新是把新安装包下到 %TEMP%\StarryVT-Setup-new.exe
;   再静默运行的 —— 那样 {src} 就是 %TEMP%，更新会装进 %TEMP%\StarryVT，
;   覆盖不到用户真正在用的那一份。
;   所以：**更新器必须自己传 /DIR=<正在运行的程序目录>**（见 updater._update_script）。
;   第二道防线是 UsePreviousAppDir（默认就是 yes，这里显式写出来）。
;
; PrivilegesRequired=lowest：程序要往自己的目录写 output\（在线视频的成品）和日志，
; 装 Program Files 会写不进去。这样也免 UAC。
DefaultDirName={src}\StarryVT
UsePreviousAppDir=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer
; 文件名保持 ASCII：GitHub Release 的资产名只允许 [A-Za-z0-9._-]，
; 中文会被静默删掉。带版本号，下载下来一眼能认出是哪一版；
; 更新器读的是 latest.json 里的 url，不依赖这个文件名。
OutputBaseFilename=StarryVT-Setup_v{#AppVersion}
; 说明页：纯文本不认 Markdown，.rtf 才能让 B 站链接可点（改 说明.txt 后跑 mkrtf.py 重新生成）
InfoBeforeFile=installer\说明.rtf
; 安装包穿软件自己的图标（那颗星星）——用户 2026-10-14 定
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[InstallDelete]
; 改名后的老快捷方式（英文版叫星译）留着会指向同一个 exe，但名字对不上，清掉
Type: files; Name: "{autoprograms}\星译.lnk"
; ⚠️ 桌面的「星译.lnk」**不要在这里删**：安装包只在用户勾了「创建桌面快捷方式」
;    时才重建它；不勾的话先删后不建，用户桌面上就空了（2026-10-18 用户实测）。
; 3.7 起软件名从「星译·免费版」改成「星译」：老快捷方式不清掉的话，老用户升级后
; 开始菜单和桌面上会同时躺着新旧两个图标（指向同一个 exe），看着像装了两个软件。
; ⚠️ 这三条 [InstallDelete] 跑在 [Icons] 之前，所以先删后建，不会把新快捷方式删掉。
Type: files; Name: "{autoprograms}\星译·免费版.lnk"
Type: files; Name: "{autodesktop}\星译·免费版.lnk"
; 旧版曾把试听样音写进 web\；源码和安装包已不再带这个文件，升级时清掉残留
Type: files; Name: "{app}\backend\web\voice_*.wav"
Type: files; Name: "{app}\web\voice_*.wav"
; 2.3 及以前的版本是 pywebview 界面，程序本体就摆在这一层（StarryVT.exe + _internal\）。
; 3.0 改成「界面在根目录、Python 后端挪进 backend\」，不删掉旧的就会留一份 380 MB 的
; 旧运行时在盘上，而且根目录会同时躺着两个 exe。
Type: files; Name: "{app}\StarryVT.exe"
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\web"
; 3.0 早期用的是 PyInstaller（后端全塞在 backend\_internal\），后来换成 Nuitka（平铺在 backend\）。
; 上面那条只认根目录的 _internal\，**认不出 backend\ 里面那个** —— 从 PyInstaller 版升级上来的
; 用户会白白留下 376 MB 旧运行时，而且 Ffmpeg.cs 会优先拿它里面的旧 ffmpeg（2026-10-15 实测）。
Type: filesandordirs; Name: "{app}\backend\_internal"

[Files]
; _stage\app 里已经含：界面、backend\（Python 后端 + ffmpeg/yt-dlp）、licenses\、
; 用户协议 TERMS.md、第三方组件清单 THIRD_PARTY_LICENSES.md —— 整棵搬过去就行
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
{ 点取消直接退出，不再弹"确定要取消吗"确认框 }
procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  Confirm := False;
end;

[UninstallDelete]
; ★ 只删「可以重下」的 {app}\data（引擎模型/运行环境/语音克隆/缩略图缓存）。
;   **绝不能删整个 {app}**：默认成片目录就是 {app}\backend\output，
;   删安装目录等于把用户已经翻好的视频一起删掉（2026-10-24 对照 Buzz 发现）。
;   安装清单里的文件 Inno 自己会删，这里只补它不知道的那些。
Type: filesandordirs; Name: "{app}\data"
