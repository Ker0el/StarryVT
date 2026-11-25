; 星译 —— Inno Setup 安装脚本（3.0 起：WinUI 3 界面 + 随包后端）
;
; 目录结构（_stage\app 是 publish + 组装的结果，[Files] 直接整棵搬）：
;   {app}\StarryVt.exe      ← WinUI 3 界面（无 .NET 运行时也能跑，自包含发布）
;   {app}\backend\          ← Python 后端（PyInstaller 打的，含 ffmpeg / yt-dlp / 引擎安装器）
;   {app}\licenses\         ← 第三方组件许可证原文
;
#define AppName "星译"
#define AppVersion "6.67"
#define AppExeName "StarryVt.exe"
#define StageDir "_stage\app"

[Setup]
AppId={{7E3B9C2A-5D41-4A6E-9F2B-1C8E4A6D2B91}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
; 默认装到「安装包所在的那个盘的盘根\StarryVT」（例如 D:\StarryVT）。
;
; ★ 2026-11-25 用户定：不再用 {src}\StarryVT。用户的安装包常放在中文文件夹里
;   （客户实例：安装包在 D:\软件安装包\星译\ 里双击 → 装进 D:\软件安装包\星译\StarryVT），
;   而数据目录 / 引擎 / 15 GB 的语音克隆默认都跟着安装目录走，于是整条路径都是中文。
;   中文路径本身能跑，但个别组件对代码页敏感（本机语音克隆的 Python 3.11 按 GBK
;   读 .pth，客户那张诊断报告就是卡在这儿）。默认到盘根就是纯英文，一劳永逸。
;   用户仍然可以自己改；路径含中文时 NextButtonClick 会提醒一句（见 [Code]）。
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
DefaultDirName={drive:{src}}\StarryVT
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

{ 2026-11-05 客户报障：手动删掉整个安装目录后重装，Inno 仍按注册表里记的
  「上次安装位置」往回装 —— 那个目录已经不在了（或者只剩个空壳），
  用户看到的就是「重装了，但还是旧的 / 装到别处去了」。
  这里只清掉「记录的那份程序已经不在了」的情况，正常的升级安装不受影响。 }
procedure ClearStaleAppPath();
var
  prev: String;
begin
  if not RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E3B9C2A-5D41-4A6E-9F2B-1C8E4A6D2B91}_is1',
       'Inno Setup: App Path', prev) then
    exit;
  if prev = '' then
    exit;
  if FileExists(AddBackslash(prev) + '{#AppExeName}') then
    exit;
  RegDeleteValue(HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E3B9C2A-5D41-4A6E-9F2B-1C8E4A6D2B91}_is1',
    'Inno Setup: App Path');
end;

function InitializeSetup(): Boolean;
begin
  ClearStaleAppPath();
  Result := True;
end;

{ 卸载时删掉「用户换过盘」的数据目录。
  Inno 的 [UninstallDelete] 只认这里写死的常量路径；两个可搬家的目录
  （引擎数据目录 / 数据目录）实际在哪，由程序在 settings.save 时写进
  HKCU\Software\StarryVT（REG_SZ，Unicode —— 中文路径不会乱码）。
  不弹任何询问框：用户 2026-11-25 定「卸载默认全删，模型和数据都不留」。

  ⚠ 只删「我们知道名字」的子目录，不 DelTree 整个自定义根：
    用户可能把数据目录选在一个还有别的文件的文件夹里，整个删会伤到无关数据。
    默认位置（安装目录\data、%LOCALAPPDATA%\StarryVT、%LOCALAPPDATA%\FineSub）
    由 [UninstallDelete] 整棵删，没有这个顾虑。 }
function JoinPath(const A, B: String): String;
var
  r: String;
begin
  r := A;
  while (Length(r) > 0) and (r[Length(r)] = '\') do
    Delete(r, Length(r), 1);
  Result := r + '\' + B;
end;

procedure DeleteDirSafe(const Dir: String);
var
  d: String;
begin
  d := Trim(Dir);
  while (Length(d) > 0) and (d[Length(d)] = '\') do
    Delete(d, Length(d), 1);
  { 只接受 X:\某个目录：宁可漏删，也不能因为注册表被写坏而删掉整个盘 }
  if Length(d) < 4 then exit;
  if (d[2] <> ':') or (d[3] <> '\') then exit;
  if DirExists(d) then
    DelTree(d, True, True, True);
end;

procedure DeleteDataItems(const Root: String);
begin
  DeleteDirSafe(JoinPath(Root, 'voiceclone'));
  DeleteDirSafe(JoinPath(Root, 'dub_cache'));
  DeleteDirSafe(JoinPath(Root, 'thumbs'));
  DeleteDirSafe(JoinPath(Root, 'wheels'));
  DeleteDirSafe(JoinPath(Root, 'subtitle_preview'));
end;

procedure DeleteEngineItems(const Root: String);
begin
  { 先确认这真是引擎数据目录（models / runtime / 注册脚本三选一），再点名删那几个
    子目录 —— 不能因为用户在设置里手填了一个普通文件夹就把人家整个清空 }
  if not (DirExists(JoinPath(Root, 'models'))
          or DirExists(JoinPath(Root, 'runtime'))
          or FileExists(JoinPath(Root, 'register-location.cmd'))) then
    exit;
  DeleteDirSafe(JoinPath(Root, 'models'));
  DeleteDirSafe(JoinPath(Root, 'runtime'));
  DeleteDirSafe(JoinPath(Root, 'cache'));
  DeleteDirSafe(JoinPath(Root, 'tasks'));
end;

{ 2026-11-25：安装路径含中文 / 其它非 ASCII 字符时提醒一句。
  程序里该兜的都兜了（克隆环境的 .pth 会改写、wetext 走英文临时目录），
  但纯英文路径最省事 —— 用户坚持也能装。 }
function HasNonAscii(const S: String): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to Length(S) do
    if Ord(S[i]) > 127 then
    begin
      Result := True;
      exit;
    end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID <> wpSelectDir then exit;
  if not HasNonAscii(WizardDirValue()) then exit;
  if MsgBox('安装路径里有中文或其它非英文字符：' + #13#10 + WizardDirValue() + #13#10 + #13#10 +
            '程序能装、能用，但个别可选组件（比如本地语音克隆）对中文路径比较敏感，' +
            '出问题时更难排查。建议改成纯英文路径，例如 ' +
            ExpandConstant('{drive:{src}}\StarryVT') + '。' + #13#10 + #13#10 +
            '仍然用这个路径继续安装吗？',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    Result := True
  else
    Result := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  v: String;
begin
  if CurUninstallStep <> usPostUninstall then exit;
  if RegQueryStringValue(HKCU, 'Software\StarryVT', 'EngineHome', v) then
  begin
    DeleteEngineItems(v);
    RemoveDir(v);                    { 只在已经空了时才成功 }
  end;
  if RegQueryStringValue(HKCU, 'Software\StarryVT', 'DataHome', v) then
  begin
    DeleteDataItems(v);
    { 引擎数据目录留空时，默认就跟着数据根走：<数据目录>\engine（见 datadir.py） }
    DeleteEngineItems(JoinPath(v, 'engine'));
    RemoveDir(JoinPath(v, 'engine'));
    RemoveDir(v);
  end;
  RegDeleteKeyIncludingSubkeys(HKCU, 'Software\StarryVT');
end;

[UninstallDelete]
; ★ 2026-11-25 用户定：卸载 = 默认把模型和数据一起删，不问、不保留。
;   仍然**不能删整个 {app}**：默认成片目录是 {app}\backend\output，
;   删安装目录会把用户已经翻好的视频一起删掉（2026-10-24 对照 Buzz 发现）。
;   `{app}\data` 是默认数据根（引擎 / 模型 / 语音克隆 / 缩略图 / wheel）。
;   用户换过盘的那两份见上面 [Code] 的 CurUninstallStepChanged。
Type: filesandordirs; Name: "{app}\data"
; 星译自己的配置：设置、API Key、日志、uv.exe / yt-dlp
Type: filesandordirs; Name: "{userappdata}\StarryVT"
; WinUI 界面状态与壁纸
Type: filesandordirs; Name: "{localappdata}\StarryVT-WinUI"
; 老版本的数据根（voiceclone 曾默认在这里，可达十几 GB）
Type: filesandordirs; Name: "{localappdata}\StarryVT"
; 老版本的引擎默认数据目录
Type: filesandordirs; Name: "{localappdata}\FineSub"
; uv 默认包缓存（装引擎 / 克隆环境时留下的轮子；以前一直堆在 C 盘）
Type: filesandordirs; Name: "{localappdata}\uv\cache"
; uv 给引擎本体建的工具环境（finesub 启动器就在这儿）
Type: filesandordirs; Name: "{userappdata}\uv\tools\finesub"
; 引擎 / 克隆用过的 HuggingFace、ModelScope 默认缓存
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\huggingface"
Type: filesandordirs; Name: "{%USERPROFILE}\.cache\modelscope"
; 启动器副本（真正的 exe 在 uv 工具环境里）
Type: files; Name: "{%USERPROFILE}\.local\bin\finesub.exe"
; 更新器下到临时目录的安装包和批处理（每次 282 MB，以前会一直留着）
Type: files; Name: "{%TEMP}\StarryVT-Setup-new.exe"
Type: files; Name: "{%TEMP}\starryvt-update.cmd"

