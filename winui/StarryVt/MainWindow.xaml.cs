using Microsoft.UI.Composition.SystemBackdrops;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Shapes;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Starry.Vt.App.Fakes;
using Starry.Vt.App.Pages;
using Starry.Vt.App.Services;
using Windows.ApplicationModel.DataTransfer;
using Windows.Graphics;
using Windows.Media.Core;
using Windows.Media.Playback;

namespace Starry.Vt.App;

public sealed partial class MainWindow : Window
{
    /// <summary>设置页要改背景强度 / 主题，从这里拿窗口。</summary>
    public static MainWindow? Instance { get; private set; }

    /// <summary>有文件被拖进窗口。当前页面自己订阅（见 TranslatePage.OnNavigatedTo）。</summary>
    public static event Action<string[]>? FilesDropped;

    /// <summary>
    /// ⚠️ XAML 解析期间就会触发事件（IsSelected / SelectionChanged…），那时排在后面的控件还没建出来，
    /// 处理器一碰就是空引用，而报错完全不提空引用。所有处理器先看这个闸门。
    /// </summary>
    bool _ready;

    /// <summary>流水线正停在字幕检查暂停点（判断关掉开关时要不要帮它放行）。</summary>
    volatile bool _reviewPending;

    /// <summary>「字幕检查」导航项当前是否可见。状态推送在后台线程，不能直接读 UI 属性。</summary>
    volatile bool _subtitleNavVisible;

    /// <summary>
    /// 赞赏码的开合序号。每次开 / 关都 +1；收场动画跑完时对不上号就自己退场。
    /// 用来防两件事：连点两下叠出两遍收场动画、以及兜底计时器和 Completed 各跑一次。
    /// </summary>
    int _qrSeq;
    int _wxSeq;

    /// <summary>
    /// 磨砂层的静止透明度 —— 构造时从 XAML 读一次记住。
    /// ⚠️ 之后**不能再读 `ScrimBlur.Opacity`**：收场动画期间它是 0，
    ///    再进场时读到 0 就会把它当成目标（磨砂层直接全不透明，而且不报错）。
    /// </summary>
    double _scrimDim = 0.42;

    /// <summary>窗口句柄 —— 淡入淡出要拿它设 alpha，构造时取一次。</summary>
    IntPtr _hwnd;

    /// <summary>关闭的收场状态：0 正常 / 1 正在淡出 / 2 已放行。见构造里 AppWindow.Closing 那段。</summary>
    int _closeState;

    /// <summary>淡入只启动一次（帧回调和兜底计时器都够得着它）。</summary>
    bool _fadeInStarted;

    public MainWindow()
    {
        InitializeComponent();
        Instance = this;
        _scrimDim = ScrimBlur.Opacity;     // 见上面那条：只在这儿读一次

        // 系统真材料：Mica（Win11）→ 亚克力 → 没有就纯色
        if (MicaController.IsSupported()) SystemBackdrop = new MicaBackdrop();
        else if (DesktopAcrylicController.IsSupported()) SystemBackdrop = new DesktopAcrylicBackdrop();

        // 自绘标题栏。设完必须 SetTitleBar，否则窗口拖不动
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        ((FrameworkElement)Content).ActualThemeChanged += (_, _) => StyleTitleBar();
        StyleTitleBar();

        // 启动渐现 / 关闭渐隐：整窗（含 Mica 玻璃、标题栏、窗口阴影）由 DWM 按恒定 alpha 合成，
        // 不是只淡 XAML 内容 —— 细节和前提写在 Services/WindowFade.cs。
        // ⚠️ 必须在这儿就把 alpha 设成 0（窗口还没 Activate）：晚一步会先亮相一帧再淡，等于白做。
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        WindowFade.Enable(_hwnd, DispatcherQueue);

        // 自带的鼠标光标主题：设在根元素上就覆盖整个界面（不碰系统光标，详见 CursorTheme.cs）
        CursorTheme.Apply((FrameworkElement)Content);

        // 页面在 ctor 里抛异常时 Frame 只会「导航失败」并把内容留空，一点提示都没有 —— 记下来
        ContentFrame.NavigationFailed += (_, e) => App.Dump("页面导航失败: " + e.Exception);

        // 每换一页重新套一遍「文本 / 链接」那两类光标：根元素上的箭头会自动覆盖新页面，
        // 但文本框/超链接是**框架按控件**设的，得显式再设一次（见 CursorTheme.ApplyRoles）
        ContentFrame.Navigated += (_, _) =>
        {
            if (ContentFrame.Content is DependencyObject page) CursorTheme.ApplyRoles(page);
        };

        // 启动时后端自己会去查有没有新版本，查完主动推过来（见 winui_backend._auto_update）。
        // ⚠️ 这件事**只能在后端那条链上做**：老网页界面的入口 app.py main() 里也有一份，
        //    但 WinUI 不走它 —— 之前就是因为只在那边写了，切过来之后自动检查整个丢了。
        Services.Backend.UpdateReceived += OnUpdateInfo;
        Services.Backend.StatusReceived += OnUpdateProgress;   // 更新包下载中的进度（stage=update）
        Services.Backend.StatusReceived += OnReviewStatus;     // 字幕检查暂停点：自动切到字幕页
        Services.Backend.ReadyReceived += OnBackendReady;
        if (Services.Backend.Ready) UpdateTitleVersion();
        _ = PullUpdateStateAsync();                            // 光订阅不够，见那个方法的注释

        // 关窗口时把后端一起收掉（Windows 不会替我们杀子进程）——
        // 留着的后端会占着 backend\StarryVT.exe，装新版本时替换不掉
        Closed += (_, _) => Services.Backend.Stop();

        // 关窗口：先淡掉再真关 —— 把这一次关闭**取消掉**，等动画跑完再 Close()。
        // 不取消的话窗口当场就没了，根本没有淡出的机会。
        // _closeState 挡住第二次：那次是 Close() 自己引发的，放行。
        AppWindow.Closing += (_, e) =>
        {
            if (_closeState != 0 || !WindowFade.Enabled) return;   // 已经淡过 / 这窗口做不了 → 照常关
            _closeState = 1;
            e.Cancel = true;

            var watchdog = DispatcherQueue.CreateTimer();
            watchdog.Interval = TimeSpan.FromMilliseconds(800);   // 淡出 400ms，留一倍余量
            watchdog.IsRepeating = false;
            void Go()
            {
                if (_closeState == 2) return;      // 动画回调和兜底都会叫到这儿，只放行一次
                _closeState = 2;
                watchdog.Stop();
                Close();
            }
            watchdog.Tick += (_, _) => Go();
            WindowFade.FadeOutTo(0, 400, Go);   // 分层样式这时才临时挂回来，见 WindowFade 的说明
            watchdog.Start();
        };

        // 最小化 / 切走时把动图停掉：省电，也别让解码器一直空转
        VisibilityChanged += (_, e) =>
        {
            try
            {
                _winVisible = e.Visible;                       // 卡死自愈要据此避开「最小化时本来就该暂停」
                if (HeroVideo.MediaPlayer is not { } mp) return;
                if (e.Visible) mp.Play(); else mp.Pause();
            }
            catch { }
        };

        // 最大化/还原/改变尺寸：先把视频表面盖住，等它稳定再撤（见 ShowWallPoster）
        AppWindow.Changed += (_, args) =>
        {
            if (args.DidSizeChange || args.DidPresenterChange) ShowWallPoster();
        };

        Root.Loaded += (_, _) => { DrawStars(); SizeToDesign(); };   // 摆位放到 Loaded 之后：构造函数里 Move 会被 Windows「记住的上次位置」盖掉
        Root.Loaded += (_, _) => StartFadeIn();                      // 从 alpha 0 拉起来（Enable 失败时是个空操作）
        Root.Loaded += async (_, _) => await LoadSubtitleNavAsync();  // 字幕检查页的导航项按设置显隐
        Root.SizeChanged += (_, _) =>
        {
            DrawStars();
            ShowWallPoster();
        };
        // Esc 关赞赏码。
        // ⚠️ 别用 KeyboardAccelerator：WinUI 会把它做成悬停提示，屏幕上会一直飘着个「Esc」（用户实测）。
        // 路由事件挂在根元素上，谁按 Esc 都能冒泡到这儿。
        Root.AddHandler(UIElement.KeyDownEvent, new KeyEventHandler(OnRootKeyDown), true);

        InstallWin32DropTarget();
        // 再挂一套 OLE 的拖放目标：WM_DROPFILES 只在松手那一刻来消息，收不到「拖进来」，
        // 做不了拖动中的高亮 / 图标跳动（见 Services/FileDrop.cs）。注册失败不影响落下文件。
        FileDrop.Attach(WinRT.Interop.WindowNative.GetWindowHandle(this));
        LoadWallpaper();               // 每次启动现取一条（缓存里随机挑，没网也能换），顺便淡入

        // 剪贴板监听挂在**窗口**上（不是某个页面）：用户在浏览器复制一条链接，
        // 不管当时停在哪个页面都要有反应 —— 叫到前台 → 切到在线翻译 → 自动解析。
        // 窗口只活一次，所以不用摘（页面级那份已经不在了，见 OnlinePage 的说明）。
        try { Clipboard.ContentChanged += OnClipboardChanged; }
        catch (Exception e) { App.Dump("剪贴板监听没挂上（不影响手动粘贴）: " + e.Message); }

        _ready = true;

        // 自测入口：--probe=palette / --probe=qr（截图核对浮出层用，免得用鼠标点、坐标对不上）
        var probe = Environment.GetCommandLineArgs()
            .FirstOrDefault(a => a.StartsWith("--probe=", StringComparison.OrdinalIgnoreCase));
        if (probe is not null)
        {
            var what = probe["--probe=".Length..];
            DispatcherQueue.TryEnqueue(async () =>
            {
                await Task.Delay(2500);
                if (what == "motion") { await MotionSelfTest(); return; }
                // --probe=builtin：直接切到内置底图。内置底图**只在出错回退时才会露面**，
                // 平时看不到它，换了图想核对一眼只能靠这个口子。
                if (what == "builtin")
                {
                    ShowBuiltIn();
                    App.Dump("[probe] 已切到内置底图");
                    return;
                }
                // --probe=wallstall：**把故障造出来** —— 反复把播放进度钉回 0，做出
                // 「PlaybackState 是 Playing、画面却完全不动」这种状态（静默停播最像的一种）。
                // ⚠️ 别用 Pause 来造：那会被**旧的**看门狗接住（它判的就是「不是 Playing」），
                // 测不到这次新加的自愈。跑完去日志里找「播放卡住 → 自愈」那行。
                // 验证方式必须是这样：光「改完还能跑」证明不了自愈有没有生效（本来就不会卡）。
                if (what == "wallstall")
                {
                    await Task.Delay(6000);
                    try
                    {
                        var sess = HeroVideo.MediaPlayer?.PlaybackSession;
                        if (sess is null) { App.Dump("[probe] 没有 playback session"); return; }
                        // 把速率打到 0：位置就彻底不动了，而 PlaybackState 仍是 Playing ——
                        // 正是「显示在播、画面却不动」那种静默停播。
                        // （试过反复 Position=0：两次采样之间播放器还在推进，判据不成立，测不出来。）
                        sess.PlaybackRate = 0;
                        App.Dump($"[probe] 已把 PlaybackRate 打到 0（state={sess.PlaybackState}）");
                        for (int i = 1; i <= 9; i++)
                        {
                            await Task.Delay(1000);
                            App.Dump($"[probe] {i}s  state={sess.PlaybackState} "
                                     + $"rate={sess.PlaybackRate} pos={sess.Position.TotalSeconds:F2}");
                        }
                    }
                    catch (Exception ex) { App.Dump("[probe] 造故障失败: " + ex.Message); }
                    return;
                }
                // --probe=run:<视频路径>：拿真后端真跑一单（走的还是「选好视频 → 点开始翻译」
                // 那两条路，只是不用鼠标点），用来核对界面→后端→推流→收尾整条链。
                if (what.StartsWith("run:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("local");
                    await Task.Delay(1500);
                    (ContentFrame.Content as LocalPage)?.SelfTest(what["run:".Length..]);
                    return;
                }
                // --probe=fix:<engine|downloader>：走主页「就地修」那条路，同样不用鼠标点
                if (what.StartsWith("fix:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("home");
                    await Task.Delay(1500);
                    (ContentFrame.Content as HomePage)?.SelfTest(what["fix:".Length..]);
                    return;
                }
                // --probe=scan:<文件夹>：只把文件夹读成清单（含子文件夹），**不开始翻译**
                // （核对批量页的投放条和递归扫描用；run: 会真开跑，别拿它试大文件夹）
                if (what.StartsWith("scan:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("local");
                    await Task.Delay(1500);
                    (ContentFrame.Content as LocalPage)?.SelfTestScan(what["scan:".Length..]);
                    return;
                }
                // --probe=shot:<视频路径>：只把它放进单个视频的卡片（看封面），不开跑
                if (what.StartsWith("shot:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("local");
                    await Task.Delay(1500);
                    (ContentFrame.Content as LocalPage)?.SelfTestPick(what["shot:".Length..]);
                    return;
                }
                // --probe=clip:<url>：把链接塞进剪贴板，跑一遍完整流程
                //   故意先停在**设置页**：要看到它自己切到在线翻译页并解析（用户 2026-10-14 要的）
                if (what.StartsWith("clip:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("settings");
                    await Task.Delay(2000);
                    var url = what["clip:".Length..];
                    try
                    {
                        var dp = new DataPackage();
                        dp.SetText(url);
                        Clipboard.SetContent(dp);
                    }
                    catch (Exception e) { App.Dump("自测塞剪贴板失败: " + e.Message); return; }
                    await UseClipboardAsync();
                    return;
                }
                // --probe=link:<url>：跑一遍「粘链接 → 自动读标题/封面/作者」（单个视频）
                if (what.StartsWith("link:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("online");
                    await Task.Delay(1500);
                    (ContentFrame.Content as OnlinePage)?.SelfTestLink(what["link:".Length..], batch: false);
                    return;
                }
                // --probe=playlist:<url>：跑一遍「粘链接 → 自动读合集列表」
                if (what.StartsWith("playlist:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("online");
                    await Task.Delay(1500);
                    (ContentFrame.Content as OnlinePage)?.SelfTestLink(what["playlist:".Length..], batch: true);
                    return;
                }
                // --probe=setup：把「一键初始化」跑一遍（看清单一条条长出来的样子）
                if (what.Equals("setup", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("home");
                    await Task.Delay(1500);
                    (ContentFrame.Content as HomePage)?.SelfTestSetup();
                    return;
                }
                // --probe=recheck：把「重新检测」跑一遍（应该也是一项项打勾）
                if (what.Equals("recheck", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("home");
                    await Task.Delay(1500);
                    (ContentFrame.Content as HomePage)?.SelfTestRecheck();
                    return;
                }
                // --probe=pinsave：跑一遍「钉住当前这张」（下载进收藏库），然后把收藏库打印出来
                if (what.StartsWith("pinsave", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("settings");
                    await Task.Delay(2500);
                    // 走**界面那条路**（收藏 + 重铺收藏条 + 补预览图）。
                    // ⚠️ 别直接调 PinCurrentWallpaper：那样会绕过后面两步，
                    //    「收藏完没缩略图」这类 bug 就永远测不出来（2026-10-15 漏过一回）。
                    (ContentFrame.Content as SettingsPage)?.Probe("pin");
                    await Task.Delay(5000);          // 抽帧要几百毫秒，等它落盘再看
                    App.Dump("[wall] 收藏库: " + string.Join(" | ", Services.Wallpaper.Saved()));
                    (ContentFrame.Content as SettingsPage)?.Probe("gallery");   // 顺便展开预览图那排
                    return;
                }
                // --probe=site：跑一遍「剪贴板白名单」——哪些链接会被自动接管、哪些会被忽略
                if (what.Equals("site", StringComparison.OrdinalIgnoreCase))
                {
                    var samples = new[]
                    {
                        "https://www.youtube.com/watch?v=abc",
                        "https://youtu.be/abc",
                        "https://m.youtube.com/watch?v=abc",
                        "https://www.bilibili.com/video/BV1xx411c7mD",
                        "https://b23.tv/abcd",
                        "https://space.bilibili.com/123456/channel/collectiondetail?sid=1",
                        "https://www.google.com/search?q=youtube.com",
                        "https://foo.com/?next=youtube.com/watch",
                        "https://www.douyin.com/video/123",
                        "https://example.com/",
                    };
                    var lines = new System.Text.StringBuilder();
                    foreach (var s in samples)
                        lines.AppendLine($"{(IsVideoSite(s) ? "认  " : "忽略")} {s}");
                    App.Dump("[site] 剪贴板白名单：\n" + lines);
                    return;
                }
                // --probe=wall:<次数>：连按几次「换一张」（跟设置里那个按钮走同一条路），
                //   把每次的成败和挑到的文件名写进临时文件（查重复 / 查"点了没反应"用）
                // --probe=bg:<0..100>：把「背景亮度」滑杆拨到指定百分比。
                // 用来复现「拖到 100% 就整个背景黑掉」那条路（2026-10-15）。
                if (what.StartsWith("bg:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("settings");
                    await Task.Delay(2500);
                    (ContentFrame.Content as SettingsPage)?.Probe(what);
                    return;
                }
                if (what.StartsWith("wall:", StringComparison.OrdinalIgnoreCase))
                {
                    int n = int.TryParse(what["wall:".Length..], out var times) ? times : 6;
                    var lines = new System.Text.StringBuilder();
                    var keepPin = UiState.WallpaperPin;      // 探针不该动用户的收藏状态，跑完还回去
                    var keepMode = UiState.Wallpaper;
                    UiState.WallpaperPin = "";
                    foreach (var mode in new[] { "image", "video" })     // 两种都跑：视频那条路才是出过问题的
                    {
                        UiState.Wallpaper = mode;
                        lines.AppendLine($"— {mode} —");
                        for (int i = 0; i < n; i++)
                        {
                            var sw = System.Diagnostics.Stopwatch.StartNew();
                            var ok = await SwitchWallpaperNow();
                            sw.Stop();
                            lines.AppendLine($"{i + 1}. ok={ok} {sw.ElapsedMilliseconds}ms "
                                           + System.IO.Path.GetFileName(CurrentWallpaper)
                                           + (File.Exists(CurrentWallpaper) ? $" {new FileInfo(CurrentWallpaper).Length / 1024}KB" : ""));
                        }
                    }
                    UiState.WallpaperPin = keepPin;
                    UiState.Wallpaper = keepMode;
                    App.Dump("[wall] 连按换一张：\n" + lines);
                    return;
                }
                // --probe=about[:秒数]：等一会儿（默认 20 秒，让启动和壁纸都稳定）再切到「关于」页。
                //   纯粹是给自动截图对时用的 —— 守着那个时刻连拍，才抓得到入场动效。
                if (what.StartsWith("about", StringComparison.OrdinalIgnoreCase))
                {
                    int secs = int.TryParse(what["about".Length..].TrimStart(':'), out var sv) ? sv : 20;
                    await Task.Delay(secs * 1000);
                    NavigateTo("about");
                    return;
                }
                // --probe=fps[:秒数]：切到关于页、等 3 秒入场跑完，再数几秒渲染帧数。
                //   查「动效卡不卡」用的：数帧率比肉眼看可靠，改前改后能对比。
                if (what.StartsWith("fps", StringComparison.OrdinalIgnoreCase))
                {
                    int secs = int.TryParse(what["fps".Length..].TrimStart(':'), out var fv) ? fv : 3;
                    await Task.Delay(18_000);          // 等启动 + 壁纸都稳下来
                    NavigateTo("about");
                    await Task.Delay(5_000);           // 等 3 秒入场动画跑完
                    int frames = 0;
                    var stamps = new List<long>();
                    void OnRender(object? s, object e) { frames++; stamps.Add(Environment.TickCount64); }
                    Microsoft.UI.Xaml.Media.CompositionTarget.Rendering += OnRender;
                    await Task.Delay(secs * 1000);
                    Microsoft.UI.Xaml.Media.CompositionTarget.Rendering -= OnRender;

                    // 「卡」= 帧间隔**不均匀**，光看平均帧率看不出来，所以把间隔的分位数也打出来
                    var gaps = new List<long>();
                    for (int i = 1; i < stamps.Count; i++) gaps.Add(stamps[i] - stamps[i - 1]);
                    gaps.Sort();
                    string Dist(double p) => gaps.Count == 0 ? "-" : gaps[(int)Math.Min(gaps.Count - 1, gaps.Count * p)].ToString();
                    App.Dump($"[fps] 关于页稳定后 {secs} 秒渲染 {frames} 帧，约 {frames / (double)secs:F1} fps\n"
                           + $"     帧间隔(ms) 中位={Dist(0.5)} p90={Dist(0.9)} p99={Dist(0.99)} 最大={gaps.LastOrDefault()}\n"
                           + $"     （间隔越齐越顺；最大明显大于中位就是能看出来的顿挫）");
                    return;
                }
                // --probe=keycheck：走一遍「开始翻译」的 Key 检查。
                //   先把**机器判据**写进日志（代码里写死的），再真的调一次 EnsureAsync ——
                //   缺 Key 时它会弹引导框停在那儿等人点，正好截图核对。
                if (what.Equals("keycheck", StringComparison.OrdinalIgnoreCase))
                {
                    await Task.Delay(12_000);
                    var (d, m, du) = await Services.Keys.ProbeAsync();
                    App.Dump($"[keycheck] deepseek填了={d}  mimo填了={m}  配音开着={du}" + "\n"
                           + "      判定：没填 deepseek、或没填 mimo 且配音开着 → 会被拦下并引导去设置");
                    await Services.Keys.EnsureAsync(ContentFrame.XamlRoot);
                    return;
                }
                // --probe=navtest:<文件夹>：填好内容 → 切到设置 → 再切回来，看内容还在不在
                //   （页面开了 NavigationCacheMode，切页不该丢状态）
                //   ⚠️ 这行 if 之前被上一行的注释"吃"掉了（注释和代码写在同一行），
                //   结果是**任何**没在前面匹配上的探针都掉进这里、跑完还 return，
                //   最后的 SettingsPage.Probe 永远走不到（wallnext / qr / gallery 全废）。
                //   编译器的"无法访问的代码"警告就是这里，别再把注释和 if 写一行。
                if (what.StartsWith("navtest:", StringComparison.OrdinalIgnoreCase))
                {
                    NavigateTo("local");
                    await Task.Delay(1500);
                    (ContentFrame.Content as LocalPage)?.SelfTestScan(what["navtest:".Length..]);
                    await Task.Delay(7000);
                    NavigateTo("settings");
                    await Task.Delay(2500);
                    NavigateTo("local");
                    return;
                }
                NavigateTo("settings");
                await Task.Delay(2000);
                (ContentFrame.Content as SettingsPage)?.Probe(what);
            });
        }

        // 支持 --page=local-single 直接进某一页（截图核对 / 以后做托盘菜单、快捷方式都能用）
        var forced = Environment.GetCommandLineArgs()
            .FirstOrDefault(a => a.StartsWith("--page=", StringComparison.OrdinalIgnoreCase));

        // 截图/自测用：--theme=light 直接以浅色启动
        var themeArg = Environment.GetCommandLineArgs()
            .FirstOrDefault(a => a.StartsWith("--theme=", StringComparison.OrdinalIgnoreCase));
        if (themeArg is not null)
        {
            var v = themeArg["--theme=".Length..].ToLowerInvariant();
            SetTheme(v is "light" ? ElementTheme.Light : v is "auto" ? ElementTheme.Default : ElementTheme.Dark);
        }

        if (forced is not null)
        {
            NavigateTo(forced["--page=".Length..]);
        }
        // 环境初始化过 → 直接进「本地 · 单个视频」；没初始化过 → 停在主页做初始化
        // ⚠️ 必须**两个都满足**（2026-10-16）：`IsInitialized` 只是「向导跑过一遍」，
        //    引擎那一步失败时它也照样写盘。只看它的话，引擎没装好的用户一启动就落在
        //    翻译页（导航栏还灰着），点哪儿都不对。这种就该停在主页重新初始化。
        else if (Mock.IsInitialized && Mock.EngineReady)
        {
            NavigateTo("local");
            ContentFrame.Navigate(typeof(LocalPage));
        }
        else
        {
            RootNav.SelectedItem = RootNav.MenuItems[0];
            ContentFrame.Navigate(typeof(HomePage));
        }

        // 引擎没装好就把「本地翻译 / 在线翻译」灰掉，逼用户先走完初始化
        //（用户 2026-10-15：「第一次打开软件让用户强制先点初始化，其他 tab 栏全灰，要不然用户会乱点」）
        if (!Mock.EngineReady) SetNavLocked(true);
    }

    /// <summary>
    /// 灰 / 亮「本地翻译 / 在线翻译」。引擎真的装好了（<see cref="Mock.EngineReady"/>）
    /// 才调 SetNavLocked(false)，见 HomePage 跑完初始化那两处。
    ///
    /// ⚠️ **「设置」必须留着能点**：初始化失败最常见的原因就是代理填错了（默认值
    ///    http://127.0.0.1:7897 在没装代理的机器上必然连不上），而清空那一栏的唯一入口就在设置里。
    ///    连设置一起灰掉，用户就死锁了 —— 界面上再也没有能自救的地方。
    /// </summary>
    public void SetNavLocked(bool locked)
    {
        // 先跳走再灰：往一个已禁用的项上设 SelectedItem 可能不生效
        if (locked && ContentFrame.CurrentSourcePageType is { } t
            && (t == typeof(LocalPage) || t == typeof(OnlinePage)))
            NavigateTo("home");

        foreach (var it in NavItems())
            if (it.Tag as string is "local" or "online"
                or "local-single" or "local-batch" or "online-single" or "online-playlist")
                it.IsEnabled = !locked;
    }

    /// <summary>导航栏里所有项，含二级</summary>
    IEnumerable<NavigationViewItem> NavItems()
    {
        foreach (var obj in RootNav.MenuItems.Concat(RootNav.FooterMenuItems))
        {
            if (obj is not NavigationViewItem it) continue;
            yield return it;
            foreach (var c in it.MenuItems)
                if (c is NavigationViewItem child) yield return child;
        }
    }

    /// <summary>
    /// 摆上背景壁纸：
    ///   · 钉了收藏的 → 用收藏那条；
    ///   · 没钉 → 先放本地那个「当前」文件（启动瞬间就有画面，不用等下载），
    ///     再去图源要一条新的，下好了换上去 —— 所以每次启动都是新的。
    /// </summary>
    void LoadWallpaper()
    {
        try
        {
            Services.Wallpaper.CleanLegacy();      // 老版本囤的那些图，顺手清掉
            Services.Wallpaper.EnsureSeed();       // 头一次运行：把随包那条视频铺上并钉住
            ApplyWallpaper();
            // 启动时壁纸淡进来，别「啪」一下糊上去。
            BgLayer.Opacity = SafeBgOpacity(UiState.BgOpacity);   // ⚠️ 别直接赋 1.0，见 SafeBgOpacity
            Motion.FadeFrom(BgLayer, 0, 420);
        }
        catch { /* 壁纸失败就用内置那张 */ }
    }

    /// <summary>正在播/正在显示的那份文件路径（收藏、设置里显示都用它）。空 = 内置底图。</summary>
    internal string CurrentWallpaper => _currentWallUrl;

    /// <summary>这次换壁纸是什么时候换的 + 重试过几次（文件放不出来时自动重来一次）</summary>
    DateTime _wallSwitchedAt = DateTime.MinValue;
    int _wallRetry;

    // ★ 播放中卡死自愈用的三个量（2026-10-17 加，见 StartLoopGuard 里的说明）
    DateTime _wallLastMove = DateTime.Now;          // 上次看到播放进度**变化**的时刻
    TimeSpan _wallLastPos = TimeSpan.MinValue;      // 上次看到的播放进度
    DateTime _wallLastHealAt = DateTime.MinValue;   // 上次自愈的时刻（防抖）
    int _wallHealCount;                             // 自愈过几次（只记日志用）
    bool _winVisible = true;                        // 窗口是不是可见（最小化时不折腾）

    // 缩放遮罩：从当前视频壁纸抽的静帧，窗口尺寸变化时先盖上去
    DispatcherQueueTimer? _posterTimer;
    string _posterFor = "";

    internal void ApplyWallpaper()
    {
        try
        {
            var pin = UiState.WallpaperPin;
            if (pin.Length > 0)
            {
                if (File.Exists(pin)) { ShowWall(pin); return; }
                UiState.WallpaperPin = "";                      // 收藏的那份没了（手动删了）→ 自动取消钉住
            }

            // ★★ 没收藏、也**没主动点过「换一张」** → 用随包那条默认视频，**不去网上随机下**
            //
            // 为什么要有这一段：`EnsureSeed` 只在「一条壁纸都没有」的机器上铺一次，
            // 老用户（用过老版本 / 点过任何壁纸按钮）永远走不到，他们就一直看随机网图 ——
            // 用户 2026-10-15 报的「为什么用户打开不是我这个视频背景」。
            // 顺带省掉「每次启动都下 15~20MB 随机壁纸 + 后台再备两条」那笔流量。
            // 出口就是「换一张」：那条路会置 `WallpaperRandom = true`，之后跟以前一样随机换。
            if (!UiState.WallpaperRandom && Wallpaper.EnsureDefault() is { Length: > 0 } dft)
            {
                ShowWall(dft);
                return;
            }

            if (Wallpaper.CurrentFile() is { } cur) ShowWall(cur);   // 先显示上次那条
            else ShowBuiltIn();
            _ = RefreshWallpaperAsync();                        // 再去要一条新的换上
            _ = Wallpaper.RefillReadyAsync(UiState.Wallpaper != "image");   // 顺手把备用池填上
        }
        catch { ShowBuiltIn(); }
    }

    /// <summary>
    /// 去图源要一条随机的，下好了换上去（启动时、点「换一张」都走它）。
    /// 返回**是否真的换上了** —— 设置里那个按钮要等这个结果，别按固定秒数猜
    /// （下载快慢差很多，猜早了就是"点了没反应"，用户 2026-10-14 报过两次）。
    /// </summary>
    internal async Task<bool> RefreshWallpaperAsync()
    {
        try
        {
            if (UiState.WallpaperPin.Length > 0) return false;   // 用户挑了收藏的那条，别动
            var wantVideo = UiState.Wallpaper != "image";

            // 1) 备用池里有就直接扶正 —— 毫秒级。视频一条 15~20 MB、现下要十几秒，
            //    没有这一步「换一张」就是干等半分钟（用户 2026-10-14 报"点了没效果"）。
            var ready = Wallpaper.PromoteReady(wantVideo);
            if (ready.Length > 0)
            {
                ShowWall(ready);
                _ = Wallpaper.RefillReadyAsync(wantVideo);       // 后台接着补货
                return true;
            }

            // 2) 池子空（刚启动 / 一直在点）→ 老老实实下一份，按钮会一直显示"换一张中…"
            var f = await Wallpaper.FetchCurrentAsync(wantVideo);
            if (f.Length == 0) return false;
            if (UiState.WallpaperPin.Length > 0) return false;   // 下的这段时间里被收藏了
            ShowWall(f);
            _ = Wallpaper.RefillReadyAsync(wantVideo);
            return true;
        }
        catch { return false; }
    }

    /// <summary>按文件类型摆上去（mp4 → 播放器；图片 → Image）</summary>
    void ShowWall(string path)
    {
        _currentWallUrl = path;
        if (IsImageFile(path)) ShowImage(path);
        else ShowVideo(path);
        Wallpaper.PruneCurrent(path);  // 老的「当前」收拾掉（还在播的那份删不掉，留着下次清）
    }

    /// <summary>
    /// 钉住现在这条 = 把**正在播的那个文件**拷一份进收藏库。
    /// 不重新下载（那个接口每次给的都是另一条），所以钉住的必然是你看到的这条。
    /// </summary>
    internal bool PinCurrentWallpaper()
    {
        var cur = _currentWallUrl;
        if (cur.Length == 0 || !File.Exists(cur))
        {
            App.Dump($"[wall] 收藏失败：当前没有可用文件（cur='{cur}' 存在={File.Exists(cur)}）");
            return false;
        }
        var saved = Wallpaper.SaveCurrent(cur);
        if (saved.Length == 0) { App.Dump("[wall] 收藏失败：拷贝没成功（" + cur + "）"); return false; }
        UiState.WallpaperPin = saved;
        App.Dump("[wall] 收藏: " + saved);
        return true;
    }

    /// <summary>取消钉住：回到"每次启动现取一条"（收藏库里的都留着，随时能再选回来）</summary>
    internal void UnpinWallpaper()
    {
        UiState.WallpaperPin = "";
        ApplyWallpaper();
    }

    /// <summary>用收藏库里的某一条（设置里那排预览图点一下就是它）</summary>
    internal void UseSavedWallpaper(string path)
    {
        if (path.Length == 0 || !File.Exists(path)) return;
        UiState.WallpaperPin = path;
        ShowWall(path);
    }

    /// <summary>换一张：去图源要一条新的（不碰收藏库）</summary>
    internal Task<bool> SwitchWallpaperNow() => RefreshWallpaperAsync();

    static bool IsImageFile(string path) => Wallpaper.IsImagePath(path);

    void ShowVideo(string path)
    {
        try
        {
            App.Dump("[wall] 这次放的是: " + path);
            _wallSwitchedAt = DateTime.Now;
            _wallRetry = 0;
            _wallLastPos = TimeSpan.MinValue;      // 卡死自愈的计时重新起算
            _wallLastMove = DateTime.Now;
            if (!string.Equals(_posterFor, path, StringComparison.OrdinalIgnoreCase))
            {
                _posterFor = "";
                WallPoster.Source = null;          // 换了壁纸，旧遮罩不能用
                WallPoster.Visibility = Visibility.Collapsed;
            }
            HeroVideo.Visibility = Visibility.Visible;
            HeroImage.Visibility = Visibility.Collapsed;
            HeroVideo.Source = MediaSource.CreateFromUri(new Uri(path));
            if (HeroVideo.MediaPlayer is { } mp)
            {
                mp.IsMuted = true;                 // 背景别出声
                mp.IsLoopingEnabled = true;
                mp.CommandManager.IsEnabled = false;
                mp.MediaFailed -= OnMediaFailed;   // 文件坏了 / 放不了 → 退回内置底图
                mp.MediaFailed += OnMediaFailed;
                // ⚠️ 走到这里 = 循环守卫**没拦住**（本该提前跳回开头），播放器要拆一次管线，
                //    屏幕上就黑一下。留着这条日志是为了以后一眼分辨「闪黑是循环漏判」
                //    还是别的原因（2026-10-15 查「一直闪黑」时加的）。
                mp.MediaEnded -= OnMediaEnded;
                mp.MediaEnded += OnMediaEnded;
                mp.Play();
                ShowWallPoster();                  // 重建播放器时先盖住，等画面稳定再撤
            }
            StartLoopGuard();
            _ = LoadWallPosterAsync(path);
        }
        catch (Exception e)
        {
            App.Dump("动图壁纸放不了，退回静态图: " + e.Message);
            ShowBuiltIn();
        }
    }

    void OnMediaFailed(MediaPlayer sender, MediaPlayerFailedEventArgs args)
        => DispatcherQueue.TryEnqueue(() =>
        {
            App.Dump("动图播放失败，退回内置底图: " + args.ErrorMessage);
            ShowBuiltIn();
        });

    /// <summary>
    /// 播放**真的走到结尾了** = 循环守卫漏判（本该提前跳回开头）。
    /// 这一次播放器要拆管线重开，屏幕上就黑一下。看到这条日志就说明守卫没拦住，
    /// 正常情况下**一次都不该出现** —— 用来分辨「闪黑是循环漏判」还是别的原因。
    /// </summary>
    void OnMediaEnded(MediaPlayer sender, object args)
        => App.Dump("[wall] 播放走到结尾了 —— 循环守卫漏判，这一下会闪黑");

    void ShowImage(string path)
    {
        try
        {
            App.Dump("[wall] 这次放的是: " + path);
            StopVideo();
            var bmp = new BitmapImage
            {
                // ⚠️ 必须关掉按 URI 的图片缓存：否则同一个路径再设一次，它会直接把
                //    上一次解码好的那张旧图给你 —— 屏幕上是"点了换一张，图没变"
                //    （用户 2026-10-14 报"静态图点换一张没效果"，就是这个）。
                //    现在文件名每次都是新的，双保险。
                CreateOptions = BitmapCreateOptions.IgnoreImageCache,
            };
            bmp.ImageFailed += (_, _) => { try { HeroImage.Source = Wallpaper.BuiltIn(); } catch { } };
            bmp.UriSource = new Uri(path);
            HeroImage.Source = bmp;
            HeroImage.Visibility = Visibility.Visible;
        }
        catch { ShowBuiltIn(); }
    }

    void ShowBuiltIn()
    {
        try
        {
            // 有当前壁纸的静帧就用它兜底，别退到内置星空底图 —— 用户会以为壁纸坏了
            var poster = WallPoster.Source;
            StopVideo();
            _currentWallUrl = "";
            HeroImage.Source = poster ?? Wallpaper.BuiltIn();
            HeroImage.Visibility = Visibility.Visible;
        }
        catch { }
    }

    /// <summary>从后端抽一帧当前视频壁纸，供窗口缩放时遮罩（失败不影响播放）</summary>
    async Task LoadWallPosterAsync(string path)
    {
        try
        {
            if (!Services.Backend.Start()) return;
            var r = await Services.Backend.CallAsync("wallpaper_poster", new { path }, 20_000);
            if (r is null
                || !r.Value.TryGetProperty("ok", out var ok) || ok.ValueKind != JsonValueKind.True
                || !r.Value.TryGetProperty("file", out var file)
                || file.GetString() is not { Length: > 0 } png) return;
            if (!string.Equals(_currentWallUrl, path, StringComparison.OrdinalIgnoreCase)) return;
            var bmp = new BitmapImage { CreateOptions = BitmapCreateOptions.IgnoreImageCache };
            bmp.UriSource = new Uri(png);
            WallPoster.Source = bmp;
            _posterFor = path;
        }
        catch (Exception e) { App.Dump("壁纸遮罩生成失败: " + e.Message); }
    }

    /// <summary>
    /// 尺寸变化时先用静帧盖住视频表面，600ms 后再撤。
    /// 最大化/还原会重建 MediaPlayerElement 的视频表面，偶尔露出绿帧、黑帧；
    /// 播放器重建失败还会退回内置星空底图 —— 这一下就用当前壁纸的静帧顶住。
    /// </summary>
    void ShowWallPoster()
    {
        if (WallPoster.Source is null || HeroVideo.Visibility != Visibility.Visible) return;
        WallPoster.Visibility = Visibility.Visible;
        if (_posterTimer is null)
        {
            _posterTimer = DispatcherQueue.CreateTimer();
            _posterTimer.Interval = TimeSpan.FromMilliseconds(600);
            _posterTimer.IsRepeating = false;
            _posterTimer.Tick += (_, _) =>
            {
                _posterTimer!.Stop();
                if (HeroVideo.Visibility == Visibility.Visible)
                    WallPoster.Visibility = Visibility.Collapsed;
            };
        }
        _posterTimer.Stop();
        _posterTimer.Start();
    }

    DispatcherQueueTimer? _loopGuard;

    /// <summary>
    /// 循环不靠"播完再重来"：**提前跳回开头**。
    /// 走「播完」那条路时播放器要拆一次管线再重开，屏幕上就是"卡一下"（用户 2026-10-14 反馈）；
    /// 自己 seek 回去就绕开了那个收尾动作。IsLoopingEnabled 留着兜底。
    ///
    /// ⚠️⚠️ **tick 间隔必须小于判定窗口**，否则会周期性漏判（用户 2026-10-15 报「一直闪黑」）：
    ///   原来是 interval=200ms、窗口=150ms。两次 tick 之间播放位置前进 200ms，
    ///   而只有落在最后 150ms 里的那一次才会触发 seek —— 算下来
    ///   **每次循环有 25% 的概率跳不进去**，那一次就退回"播完再重开"，黑一下。
    ///   5 秒的壁纸平均每 20 秒闪一次，看着就像"一直在闪"。
    ///   现在 interval=80ms < 窗口 150ms，**任何 150ms 的窗口里必然包含至少一次 tick**，漏不掉。
    ///   改这两个数之前先算一遍：窗口 > 间隔 是硬条件。
    /// </summary>
    void StartLoopGuard()
    {
        if (_loopGuard is null)
        {
            _loopGuard = DispatcherQueue.CreateTimer();
            _loopGuard.Interval = TimeSpan.FromMilliseconds(80);
            _loopGuard.IsRepeating = true;
            _loopGuard.Tick += (_, _) =>
            {
                try
                {
                    var sess = HeroVideo.MediaPlayer?.PlaybackSession;
                    if (sess is null) return;
                    // 看门狗：换了壁纸 4 秒还没播起来（文件坏了 / 解码卡住）→ 重来一次
                    // （用户报过"点换一张有时候没反应"）
                    if (_wallRetry == 0 && _currentWallUrl.Length > 0 && HeroVideo.Visibility == Visibility.Visible
                        && (DateTime.Now - _wallSwitchedAt).TotalSeconds > 4
                        && sess.PlaybackState != MediaPlaybackState.Playing)
                    {
                        _wallRetry = 1;
                        App.Dump("[wall] 4 秒还没播起来，重来一次: " + _currentWallUrl);
                        ShowVideo(_currentWallUrl);
                        return;
                    }
                    HealWallIfStalled(sess);      // ★ 播放中途卡死也要自愈（见它的说明）
                    var dur = sess.NaturalDuration;
                    if (dur.TotalMilliseconds < 500) return;                 // 还没读出时长
                    if (sess.PlaybackState != MediaPlaybackState.Playing) return;
                    if ((dur - sess.Position).TotalMilliseconds < 150)
                        sess.Position = TimeSpan.Zero;                       // 提前回到开头
                }
                catch { }
            };
        }
        _loopGuard.Start();
    }

    /// <summary>
    /// ★ 播放**中途**卡死的自愈（2026-10-17 加，和上面那个看门狗不是一回事）。
    ///
    /// 起因：用户报「背景突然变黑，还原窗口也不回来」，重启软件才好。查日志**一条错都没有**
    /// （没有 MediaFailed、没有退回底图），是一次**静默停播**，根因没锁定。
    /// 而原来的看门狗只管「换壁纸后 4 秒还没播起来」，且只重试一次 —— 播到一半停住它不管。
    ///
    /// 所以这里补一个：**本该在播、进度却几秒不动** → 就地修（暂停的就 Play，其余重建播放器），
    /// 并把现场（播放状态 / 进度 / 时长 / 第几次）打进日志 —— 下次再出，日志里就有证据了。
    ///
    /// 不会误伤的三种情况都排掉了：窗口最小化（本来就该暂停）、播放器还没读出时长、
    /// 以及刚自愈过（10 秒防抖）。
    /// </summary>
    void HealWallIfStalled(MediaPlaybackSession sess)
    {
        if (_currentWallUrl.Length == 0) return;
        if (HeroVideo.Visibility != Visibility.Visible) return;
        if (!_winVisible) return;                                  // 最小化时暂停是正常的
        if (sess.NaturalDuration.TotalMilliseconds < 500) return;  // 还没读出时长，别下结论

        var pos = sess.Position;
        if (pos != _wallLastPos)
        {
            _wallLastPos = pos;
            _wallLastMove = DateTime.Now;
            return;
        }
        if ((DateTime.Now - _wallLastMove).TotalSeconds < 3.5) return;   // 还在正常推进
        if ((DateTime.Now - _wallLastHealAt).TotalSeconds < 10) return;  // 刚修过，别连环重建

        _wallLastHealAt = DateTime.Now;
        _wallHealCount++;
        var state = sess.PlaybackState;
        App.Dump($"[wall] 播放卡住 → 自愈第 {_wallHealCount} 次（state={state} "
                 + $"pos={pos.TotalSeconds:F1}s dur={sess.NaturalDuration.TotalSeconds:F1}s）：{_currentWallUrl}");

        if (state == MediaPlaybackState.Paused)
        {
            try { HeroVideo.MediaPlayer?.Play(); } catch { }         // 只是被暂停了，接着播
            _wallLastMove = DateTime.Now;
            return;
        }
        ShowVideo(_currentWallUrl);                                   // 真卡死了：整个重建
    }

    void StopVideo()
    {
        try
        {
            _loopGuard?.Stop();
            HeroVideo.MediaPlayer?.Pause();
            HeroVideo.Source = null;
            HeroVideo.Visibility = Visibility.Collapsed;
            HeroImage.Visibility = Visibility.Visible;
            WallPoster.Visibility = Visibility.Collapsed;
            WallPoster.Source = null;
            _posterFor = "";
        }
        catch { }
    }

    [DllImport("user32.dll")]
    static extern uint GetDpiForWindow(IntPtr hwnd);

    /// <summary>
    /// 把窗口从全透明拉亮（构造里已经把 alpha 设成 0 了）。
    ///
    /// ⚠️ 不能一 Loaded 就淡：窗口刚 Activate 时首帧可能还是空白的，那淡上来的就是个白窗口。
    ///    所以先数<b>两帧</b> CompositionTarget.Rendering，确认真画出来了再开始。
    /// ⚠️ 但那个事件在没合成的环境里可能根本不 tick，所以另挂一条 700ms 的兜底 ——
    ///    **窗口绝不能停在看不见的状态**，宁可生硬地亮起来。
    /// </summary>
    void StartFadeIn()
    {
        if (_fadeInStarted) return;
        _fadeInStarted = true;

        bool begun = false;
        int frames = 0;
        DispatcherQueueTimer? watchdog = null;

        void Begin()
        {
            if (begun) return;
            begun = true;
            CompositionTarget.Rendering -= OnFrame;
            watchdog?.Stop();
            // ★ 叫到前台再淡。用户点了图标就是要看这个窗口，而后台启动的进程光靠
            //   Activate() 是抢不到前台的（Windows 的前台锁会静默挡掉），表现成
            //   「点了没反应、好像没启动」—— 2026-10-16 用户就这么报上来的。
            //   实测（同一个壳里开/关淡入各起 5 次）：开不开淡入都抢不到，所以这是
            //   本来就有的毛病，跟淡入无关，顺手在这儿一起修了。
            // ⚠️ 要叫两次：窗口还是全透明的时候叫不一定成立（实测 3 次中 1 次成），
            //    淡完那次它已经不透明了，兜住。已经在前面时 Bring 自己会跳过。
            WindowFront.Bring(_hwnd);
            // 淡完立刻把分层样式摘掉，窗口变回普通窗口（拖放那些路径就完全不受影响了）
            // 600ms + 四次缓出：2026-10-16 用户说「太快了，慢一点才有高级感」——
            // 光拉时长不够，曲线也得换，理由写在 WindowFade.Step 里。
            WindowFade.FadeTo(1, 600, () => { WindowFade.Detach(); WindowFront.Bring(_hwnd); });
        }
        void OnFrame(object? sender, object e) { if (++frames >= 2) Begin(); }

        CompositionTarget.Rendering += OnFrame;
        watchdog = DispatcherQueue.CreateTimer();
        watchdog.Interval = TimeSpan.FromMilliseconds(700);
        watchdog.IsRepeating = false;
        watchdog.Tick += (_, _) => Begin();
        watchdog.Start();
    }

    /// <summary>
    /// 窗口尺寸照 DUM 那套：设计尺寸 1920×1180（原来 1032，用户嫌矮，加高了）。
    /// 「适配不同电脑屏幕」的做法是——先按屏幕缩放换算成物理像素，
    /// 再夹到工作区的 94%（小屏幕 / 高缩放时自动变小），最后居中。
    /// </summary>
    void SizeToDesign()
    {
        const int designW = 1920, designH = 1180;
        if (AppWindow is null) return;
        try
        {
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            double scale = GetDpiForWindow(hwnd) / 96.0;
            // 固定用主屏：多显示器时窗口不该随机落在一块屏上
            var work = DisplayArea.Primary.WorkArea;

            int w = (int)Math.Min(designW * scale, work.Width * 0.94);
            int h = (int)Math.Min(designH * scale, work.Height * 0.94);
            AppWindow.Resize(new SizeInt32(w, h));
            AppWindow.Move(new PointInt32(work.X + (work.Width - w) / 2, work.Y + (work.Height - h) / 2));
        }
        catch (Exception e)
        {
            App.Dump("SizeToDesign 失败（不影响使用）: " + e.Message);
        }
    }

    void RootNav_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (!_ready || sender.SelectedItem is not NavigationViewItem item) return;
        var tag = item.Tag as string;

        Type? page = tag switch
        {
            "home" or "env" => typeof(HomePage),   // 环境检查和「开始使用」合并成一页
            "local" or "local-single" or "local-batch" => typeof(LocalPage),
            "online" or "online-single" or "online-playlist" => typeof(OnlinePage),
            "subtitle" => typeof(SubtitlePage),
            "settings" => typeof(SettingsPage),
            "about" => typeof(AboutPage),
            _ => null,
        };
        if (page is not null && ContentFrame.CurrentSourcePageType != page)
            ContentFrame.Navigate(page, null, TransitionFor(page));
    }

    /// <summary>背景（星空那层）强度，0~1。设置页的滑杆调它。</summary>
    /// <summary>
    /// 背景亮度（0~1）。
    ///
    /// ⚠️⚠️ **绝不真的设成 1.0**：WinUI 在 `Opacity == 1` 时会把「不透明度图层」**整个优化掉**，
    ///   而 BgLayer 里装着 `MediaPlayerElement`（视频壁纸）—— 走那条没有图层的路时，
    ///   视频层不一定被正确合成，屏幕上是**整个背景全黑**。
    ///   XAML 里给的默认值是 0.95（有图层，正常），所以只有把滑杆拖到 100% 才会踩到。
    ///   （2026-10-15 用户自己定位到「背景亮度调到 100% 就是黑屏」这条触发条件。）
    ///   压到 0.995：肉眼完全看不出差别，但那条被优化掉的路径不会被选中。
    /// </summary>
    const double BgOpacityMax = 0.995;

    /// <summary>把 0~1 的亮度压到安全范围（1.0 会被 WinUI 优化掉，见 <see cref="BgOpacityMax"/>）</summary>
    static double SafeBgOpacity(double v)
    {
        var c = Math.Clamp(v, 0, 1);
        return c >= 1 ? BgOpacityMax : c;
    }

    public void SetBackgroundOpacity(double value) => BgLayer.Opacity = SafeBgOpacity(value);

    /// <summary>
    /// 「字幕检查」导航项显隐（设置里勾了「检查和编辑字幕」才出现）。
    /// 关闭时如果正停在这一页，就退回设置页，免得用户留在没有入口的页面上。
    /// </summary>
    public void SetSubtitleNavVisible(bool on)
    {
        _subtitleNavVisible = on;
        SubtitleNav.Visibility = on ? Visibility.Visible : Visibility.Collapsed;
        // 关掉开关时如果任务正停在暂停点，帮它放行，别让用户卡在一个没有入口的任务上
        if (!on && _reviewPending)
        {
            _reviewPending = false;
            _ = Services.Backend.CallAsync("continue_review", new { path = "" }, 30_000);
        }
        if (!on && ContentFrame.CurrentSourcePageType == typeof(SubtitlePage))
            NavigateTo("settings");
    }

    /// <summary>
    /// 识别完成、流水线停在字幕检查点时，把界面自动切到「字幕检查」页。
    /// 参照 pyvideotrans 的单视频交互：到暂停点就把编辑界面摆到用户面前，
    /// 而不是让他自己去侧边栏找。
    /// </summary>
    void OnReviewStatus(JsonElement st)
    {
        bool pending = st.TryGetProperty("awaiting_review", out var v) && v.ValueKind == JsonValueKind.True;
        _reviewPending = pending;
        if (!pending) return;
        if (!_subtitleNavVisible)
        {
            // 任务开始时开关还开着，跑到暂停点时用户已经关掉了：直接放行，别让任务卡住
            _reviewPending = false;
            _ = Services.Backend.CallAsync("continue_review", new { path = "" }, 30_000);
            return;
        }
        DispatcherQueue.TryEnqueue(() =>
        {
            if (_subtitleNavVisible
                && ContentFrame.CurrentSourcePageType != typeof(SubtitlePage))
                NavigateTo("subtitle");
        });
    }

    async Task LoadSubtitleNavAsync()
    {
        try
        {
            if (!Services.Backend.Start()) return;
            var r = await Services.Backend.CallAsync("boot", null, 30_000);
            if (r is JsonElement e && e.ValueKind == JsonValueKind.Object
                && e.TryGetProperty("settings", out var s) && s.ValueKind == JsonValueKind.Object)
            {
                SetSubtitleNavVisible(s.TryGetProperty("subtitle_review", out var v)
                    && v.ValueKind == JsonValueKind.True);
            }
        }
        catch
        {
            // 读不到设置就保持默认（隐藏），不影响其它页面
        }
    }

    /// <summary>自测：合成层动画到底有没有在跑（在 UI 线程上分几次读回真实不透明度）</summary>
    async Task MotionSelfTest()
    {
        var log = new System.Text.StringBuilder();
        var v = Microsoft.UI.Xaml.Hosting.ElementCompositionPreview.GetElementVisual(BgLayer);
        BgLayer.Opacity = 0.5;
        log.AppendLine($"设完属性(0.5) 立刻读 = {v.Opacity:F2}");
        Motion.FadeFrom(BgLayer, 0, 500);
        await Task.Delay(100);
        log.AppendLine($"起播后 100ms 读 = {v.Opacity:F2}");
        await Task.Delay(150);
        log.AppendLine($"起播后 250ms 读 = {v.Opacity:F2}");
        await Task.Delay(400);
        log.AppendLine($"起播后 650ms 读 = {v.Opacity:F2}");
        App.Dump(log.ToString());
    }

    /// <summary>弹赞赏码：整层轻磨砂 + 居中大图，点任意地方（或 Esc）关掉</summary>
    public void ShowQr()
    {
        _qrSeq++;                          // 让还没跑完的收场动画作废（见 HideQr）
        // ★★ 这一句**必须有**，别删：另外两张卡（感谢 / 术语库）弹出来时都会顺手把
        //    `QrCard` 设成 Collapsed（「同一时刻只开一张卡」），而**只有这里能把
        //    它设回 Visible**。XAML 里 QrCard 没写 Visibility（默认 Visible），
        //    所以症状特别能骗人：**第一次点能用，开过一次「感谢」之后永久失效** ——
        //    界面还会照常出那层磨砂，看着像「点了没反应 / 点了没东西」。
        //    （2026-10-16 加另外两张卡时漏的，3.9 已经带着它发出去了。）
        QrCard.Visibility = Visibility.Visible;
        ThanksCard.Visibility = Visibility.Collapsed;      // 同一时刻只开一张卡
        KnowledgeCard.Visibility = Visibility.Collapsed;
        WxCard.Visibility = Visibility.Collapsed;
        PreviewCard.Visibility = Visibility.Collapsed;
        ModalScrim.Visibility = Visibility.Visible;
        // 等这一帧排完版再动（刚 Visible 的元素量不到尺寸，缩放会以 (0,0) 为中心）
        Motion.NextFrame(DispatcherQueue, () =>
        {
            Motion.BackdropIn(ScrimBlur, _scrimDim, 420);   // 缓入缓出；比卡片慢一拍
            Motion.ModalIn(QrCard, 480);                    // 卡片像是「浮上来落定」
        });
    }

    /// <summary>
    /// 收赞赏码。**不能直接切 Collapsed** —— 那是硬断，跟「慢慢浮出来」对不上，显廉价。
    /// 动画跑完再收。
    ///
    /// 两个动画**同时长**，而且**都**调 CloseQrIfCurrent（它是幂等的）：
    /// 谁先跑完都会把整层收掉，另一个跑完时已经收起来了 —— 无论谁先，
    /// 都不会有人在用户面前把属性弹回原值（「关的时候闪一下」就是这么来的）。
    ///
    /// `_qrSeq` 用来作废：中途又点开的话序号会变，旧的收场回调就自己退场。
    /// </summary>
    public void HideQr()
    {
        if (ModalScrim.Visibility != Visibility.Visible) return;
        int seq = ++_qrSeq;

        Motion.BackdropOut(ScrimBlur, _scrimDim, 320, done: () => CloseQrIfCurrent(seq));
        Motion.ModalOut(QrCard, 320, done: () => CloseQrIfCurrent(seq));
        // 兜底：Completed 万一没来（窗口被遮挡 / 动画被系统掐掉），别把界面卡在打开态
        Motion.After(DispatcherQueue, 900, () => CloseQrIfCurrent(seq));
    }

    void CloseQrIfCurrent(int seq)
    {
        if (seq != _qrSeq) return;         // 期间又开 / 关过了，这次作废
        // ★★ 这一句和 ShowQr 里那句 `Visible` 是**一对**，缺哪个都出问题（2026-10-17 各栽一次）：
        //    `AnyCardOpen()` 就是读这张卡的 Visibility 来判断「还有没有卡开着」的 ——
        //    只把它设成 Visible、收场时不设回来，**遮罩就永远撤不掉**，
        //    症状是「点了空白关不掉，整页一直是灰的」。
        //    ShowThanks / ShowKnowledge 那两条**都**有这一句，只有这里漏了。
        QrCard.Visibility = Visibility.Collapsed;
        if (!AnyCardOpen()) ModalScrim.Visibility = Visibility.Collapsed;
    }

    /// <summary>还有卡片浮层开着吗 —— 遮罩要等**所有**卡都收完才能藏（点空白时三张卡
    /// 的收场动画是一起跑的，谁先跑完都不能自己把遮罩撤掉，否则剩下的卡片会在没有
    /// 磨砂的背景下继续淡，看着像脱了一层）。</summary>
    bool AnyCardOpen() =>
        QrCard.Visibility == Visibility.Visible ||
        WxCard.Visibility == Visibility.Visible ||
        PreviewCard.Visibility == Visibility.Visible ||
        ThanksCard.Visibility == Visibility.Visible ||
        KnowledgeCard.Visibility == Visibility.Visible;

    // ============================================================ 微信浮层

    /// <summary>弹微信二维码：同赞赏码那套，淡入 + 浮起，点空白 / Esc 关掉，没有关闭按钮。</summary>
    public void ShowWx()
    {
        _wxSeq++;                                   // 让还没跑完的收场动画作废
        QrCard.Visibility = Visibility.Collapsed;   // 同一时刻只开一张卡
        ThanksCard.Visibility = Visibility.Collapsed;
        KnowledgeCard.Visibility = Visibility.Collapsed;
        PreviewCard.Visibility = Visibility.Collapsed;
        WxCard.Visibility = Visibility.Visible;
        ModalScrim.Visibility = Visibility.Visible;
        Motion.NextFrame(DispatcherQueue, () =>
        {
            Motion.BackdropIn(ScrimBlur, _scrimDim, 420);
            Motion.ModalIn(WxCard, 480);
        });
    }

    public void HideWx()
    {
        if (WxCard.Visibility != Visibility.Visible) return;
        int seq = ++_wxSeq;
        Motion.BackdropOut(ScrimBlur, _scrimDim, 320, done: () => CloseWxIfCurrent(seq));
        Motion.ModalOut(WxCard, 320, done: () => CloseWxIfCurrent(seq));
        Motion.After(DispatcherQueue, 900, () => CloseWxIfCurrent(seq));   // 兜底
    }

    void CloseWxIfCurrent(int seq)
    {
        if (seq != _wxSeq) return;
        WxCard.Visibility = Visibility.Collapsed;
        if (!AnyCardOpen()) ModalScrim.Visibility = Visibility.Collapsed;
    }

    /// <summary>卡片内部的点击不关浮层</summary>
    void WxCard_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
        => e.Handled = true;

    // ============================================================ 字幕样式放大预览

    int _previewSeq;

    /// <summary>弹字幕样式大图：和微信共用遮罩，淡入 + 浮起；点任意地方 / Esc 关掉。</summary>
    public void ShowPreview(string path)
    {
        int seq = ++_previewSeq;
        QrCard.Visibility = Visibility.Collapsed;
        WxCard.Visibility = Visibility.Collapsed;
        ThanksCard.Visibility = Visibility.Collapsed;
        KnowledgeCard.Visibility = Visibility.Collapsed;

        // 先把图解码完再淡入，否则会先看到一块空卡片、图过一帧才「啪」地出现（就是闪一下）
        var bmp = new BitmapImage();
        bmp.ImageOpened += (_, _) => DispatcherQueue.TryEnqueue(() =>
        {
            if (seq != _previewSeq) return;      // 期间已经关掉 / 换了别的卡
            PreviewCard.Visibility = Visibility.Visible;
            ModalScrim.Visibility = Visibility.Visible;
            Motion.NextFrame(DispatcherQueue, () =>
            {
                Motion.BackdropIn(ScrimBlur, _scrimDim, 420);
                Motion.ModalIn(PreviewCard, 480);
            });
        });
        bmp.ImageFailed += (_, e) => App.Dump("预览图加载失败: " + e.ErrorMessage);
        PreviewImage.Source = bmp;
        try { bmp.UriSource = new Uri(path); }
        catch (Exception e) { App.Dump("预览图路径无效: " + e.Message); return; }
    }

    public void HidePreview()
    {
        _previewSeq++;                            // 连还没解码完的那次也一并作废
        if (PreviewCard.Visibility != Visibility.Visible) return;
        int seq = _previewSeq;
        Motion.BackdropOut(ScrimBlur, _scrimDim, 320, done: () => ClosePreviewIfCurrent(seq));
        Motion.ModalOut(PreviewCard, 320, done: () => ClosePreviewIfCurrent(seq));
        Motion.After(DispatcherQueue, 900, () => ClosePreviewIfCurrent(seq));   // 兜底
    }

    void ClosePreviewIfCurrent(int seq)
    {
        if (seq != _previewSeq) return;
        PreviewCard.Visibility = Visibility.Collapsed;
        if (!AnyCardOpen()) ModalScrim.Visibility = Visibility.Collapsed;
    }

    // ============================================================ 版本更新
    //
    // 启动时是后端自己去查新版本的（`winui_backend._auto_update`），查完把结果推过来。
    // 后端的下载 / 校验 / 静默安装 / 自动重启那一整套本来就有（`updater.py`），
    // 只是老网页界面接上了、切到 WinUI 之后一直没人调 —— 2026-10-15 补上。

    /// <summary>后端启动完成（后台线程回调）→ 回 UI 线程填标题栏版本号</summary>
    void OnBackendReady() => DispatcherQueue.TryEnqueue(UpdateTitleVersion);

    /// <summary>标题栏版本号只从后端拿，别再在 XAML 里写死（曾经写死 v6.10，更新后一直显示旧版）</summary>
    void UpdateTitleVersion()
    {
        var v = Services.Backend.Version;
        TitleVersion.Text = string.IsNullOrWhiteSpace(v) ? "" : "v" + v;
    }

    /// <summary>后端查到的更新信息（点「立即更新」要用里面的 pkg_url / sha256）</summary>
    JsonElement _updateInfo;

    /// <summary>正在下载/安装更新：用来把后端的 error/cancelled 收尾到更新条上</summary>
    bool _updateBusy;

    /// <summary>后端推来「有没有新版本」。有就挂横幅；没有、或查失败，就什么都不做。</summary>
    void OnUpdateInfo(JsonElement info)
    {
        // 这条是从后端读取线程来的，碰界面得回 UI 线程
        DispatcherQueue.TryEnqueue(() =>
        {
            try
            {
                if (_updateBusy) return;          // 下载中，别把进度条重置回「有新版本」
                _updateInfo = info;
                bool ok = info.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
                bool has = info.TryGetProperty("has_update", out var h) && h.ValueKind == JsonValueKind.True;

                // ★ 「查失败」和「没新版」是两件事，以前都一声不响、退回同一副样子 ——
                //   客户报「检测不到最新版」，实际是他的机器压根连不上 GitHub
                //   （2026-10-15 诊断报告实证）。这里把它说出来，顺带提一句：
                //   连不上更新服务器，一键装引擎也会卡，两件事要访问的是同一个地方。
                if (!ok)
                {
                    var err = info.TryGetProperty("error", out var e2) ? (e2.GetString() ?? "") : "";
                    UpdateTitle.Text = "检查更新没成功";
                    UpdateNotes.Text = (err.Length > 0 ? err + "。" : "")
                        + "机器连不上更新服务器（其它功能不受影响，但一键装引擎也要访问那里）。";
                    UpdateGoBtn.Visibility = Visibility.Collapsed;   // 没有东西可装
                    UpdateManualBtn.Visibility = Visibility.Visible; // 但可以手动去 Releases 下
                    UpdateLaterBtn.Content = "知道了";
                    UpdateBar.Visibility = Visibility.Visible;
                    Motion.Reveal(UpdateBar, 10, 320);
                    return;
                }
                if (!has) return;

                UpdateGoBtn.Visibility = Visibility.Visible;         // 上次可能是失败态，复原
                UpdateGoBtn.Content = "立即更新";
                UpdateGoBtn.IsEnabled = true;
                UpdateManualBtn.Visibility = Visibility.Collapsed;
                UpdateLaterBtn.Content = "以后再说";
                UpdateLaterBtn.IsEnabled = true;
                string ver = info.TryGetProperty("version", out var v) ? (v.GetString() ?? "") : "";
                string notes = info.TryGetProperty("notes", out var n) ? (n.GetString() ?? "") : "";
                UpdateTitle.Text = $"有新版本 v{ver}";
                UpdateNotes.Text = notes;
                UpdateBar.Visibility = Visibility.Visible;
                Motion.Reveal(UpdateBar, 10, 320);
            }
            catch (Exception e) { App.Dump("更新提示条显示失败: " + e.Message); }
        });
    }

    void UpdateLater_Click(object sender, RoutedEventArgs e) => UpdateBar.Visibility = Visibility.Collapsed;

    /// <summary>更新包下载中的进度 —— 后端走任务状态那条通道推的（stage=update）</summary>
    void OnUpdateProgress(JsonElement st)
    {
        if (!_updateBusy) return;                    // 不是我们发起的更新，别碰更新条
        string stage = st.TryGetProperty("stage", out var s) ? (s.GetString() ?? "") : "";
        string err = st.TryGetProperty("error", out var e) ? (e.GetString() ?? "") : "";

        // ★ 以前这里只认 stage=update：下载失败时后端推的是 stage=error，
        //   被整个忽略，更新条永远停在「正在下载新版本…」（2026-10-21 用户实测）。
        if (stage is "error" or "cancelled")
        {
            DispatcherQueue.TryEnqueue(() => FinishUpdateError(stage, err));
            return;
        }
        if (stage != "update") return;

        double pct = st.TryGetProperty("percent", out var p) && p.ValueKind == JsonValueKind.Number ? p.GetDouble() : 0;
        string msg = "";
        if (st.TryGetProperty("log", out var l) && l.ValueKind == JsonValueKind.Array && l.GetArrayLength() > 0)
            msg = l[l.GetArrayLength() - 1].GetString() ?? "";
        DispatcherQueue.TryEnqueue(() =>
        {
            if (!_updateBusy) return;
            UpdateManualBtn.Visibility = Visibility.Collapsed;
            UpdateGoBtn.Content = $"{pct:0}%";
            if (msg.Length > 0) UpdateNotes.Text = msg;
        });
    }

    /// <summary>下载失败/取消：把更新条从「正在下载」收回来，给重试和手动下载两个出口。</summary>
    void FinishUpdateError(string stage, string err)
    {
        if (!_updateBusy) return;
        _updateBusy = false;
        bool barVisible = UpdateBar.Visibility == Visibility.Visible;

        UpdateTitle.Text = stage == "cancelled" ? "已取消更新" : "更新没下载成功";
        UpdateNotes.Text = stage == "cancelled"
            ? "下载已取消，可以稍后再试。"
            : "下载失败，网络中断。可以点「重试」，或点「手动下载」去 GitHub 下载。";
        UpdateGoBtn.Content = stage == "cancelled" ? "立即更新" : "重试";
        UpdateGoBtn.IsEnabled = true;
        UpdateLaterBtn.Content = "以后再说";
        UpdateLaterBtn.IsEnabled = true;
        UpdateManualBtn.Visibility = Visibility.Visible;
        // 详细错误放到悬停提示里，不占那两行位置
        ToolTipService.SetToolTip(UpdateNotes, err.Length > 0 ? err : "下载失败");
        if (barVisible) UpdateBar.Visibility = Visibility.Visible;
    }

    /// <summary>手动下载：打开发布页，用户自己从浏览器下载安装包。</summary>
    async void UpdateManual_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            await Services.Backend.CallAsync("open_url",
                new { url = "https://github.com/Ker0el/StarryVT/releases/latest" }, 20_000);
        }
        catch (Exception ex) { App.Dump("打开发布页失败: " + ex.Message); }
    }

    /// <summary>
    /// 兜底主动拉一次更新状态。
    /// ⚠️ 光靠 <see cref="OnUpdateInfo"/> 那个订阅是不够的：后端的检查可能**比订阅还快**
    /// （清单在本地、或网络好时一两秒就回来），事件一旦早于订阅就**永久丢了**，
    /// 界面上什么都看不到，而且不报错 —— 实测就是这么漏的。
    /// `update_state` 在检查跑完之前返回 `{ok:false}`，所以轮询到 ok 为止。
    /// </summary>
    async Task PullUpdateStateAsync()
    {
        for (int i = 0; i < 14; i++)
        {
            try
            {
                var r = await Services.Backend.CallAsync("update_state", null, 15_000);
                if (r is null) return;                  // 后端起不来，别空转
                if (r is { } info && info.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True)
                {
                    OnUpdateInfo(info);
                    return;
                }
            }
            catch (Exception e) { App.Dump("拉取更新状态失败: " + e.Message); return; }
            await Task.Delay(1500);
        }
    }

    /// <summary>立即更新：交给后端下载 → 校验 → 静默安装 → 自己重启，这儿不用管后续</summary>
    async void UpdateGo_Click(object sender, RoutedEventArgs e)
    {
        if (_updateInfo.ValueKind != JsonValueKind.Object) return;
        _updateBusy = true;                  // 先置位：下载失败的状态可能比 do_update 返回还快
        UpdateGoBtn.IsEnabled = false;
        UpdateLaterBtn.IsEnabled = false;
        UpdateManualBtn.Visibility = Visibility.Collapsed;
        UpdateGoBtn.Content = "准备中…";
        UpdateNotes.Text = "正在连接更新服务器…";
        try
        {
            string pkg = _updateInfo.TryGetProperty("pkg_url", out var p) ? (p.GetString() ?? "") : "";
            string sha = _updateInfo.TryGetProperty("sha256", out var s) ? (s.GetString() ?? "") : "";
            var r = await Services.Backend.CallAsync("do_update", new { pkg_url = pkg, sha256 = sha }, 30_000);
            // ⚠️ CallAsync 回来的已经是 JsonElement 本身（不是 Nullable），别写 `rr.Value`
            if (r is { } rr && rr.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True)
                return;                       // 后端接管了，别再动按钮
            string err = "";
            if (r is { } x && x.TryGetProperty("error", out var er)) err = er.GetString() ?? "";
            if (err.Length == 0) err = Services.Backend.LastError;
            UpdateNotes.Text = "更新没起来：" + (err.Length > 0 ? err : "后端没有响应");
        }
        catch (Exception ex)
        {
            App.Dump("发起更新失败: " + ex.Message);
            UpdateNotes.Text = "更新没起来：" + ex.Message;
        }
        _updateBusy = false;
        UpdateGoBtn.IsEnabled = true;
        UpdateLaterBtn.IsEnabled = true;
        UpdateGoBtn.Content = "重试";
        UpdateManualBtn.Visibility = Visibility.Visible;
    }

    void ModalScrim_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
    {
        // 遮罩上点空白 = 关掉现在开着的那张（赞赏码 / 微信 / 感谢 / 术语库共用一个遮罩）
        HideQr();
        HideWx();
        HidePreview();
        HideThanks();
        HideKnowledge();
    }

    void OnRootKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Escape) return;
        if (ModalScrim.Visibility != Visibility.Visible) return;
        HideQr();
        HideWx();
        HidePreview();
        HideThanks();
        HideKnowledge();
        e.Handled = true;
    }

    // ============================================================ 「感谢」浮层
    //
    // 设置页那颗「感谢」按钮弹它（用户 2026-10-16 定：做个窗口、**渐隐渐现、点空白关掉、
    // 不要关闭按钮**）。整套动效和赞赏码共用 ModalScrim 那一层遮罩 —— 同一时刻只开一张卡。

    int _thanksSeq;
    bool _thanksFilled;

    /// <summary>弹「感谢」：淡入 + 卡片浮起来；关掉靠点空白 / Esc。</summary>
    public void ShowThanks()
    {
        _thanksSeq++;                            // 让还没跑完的收场动画作废（见 CloseThanksIfCurrent）
        FillThanks();                            // 列表只构建一次
        if (QrCard.Visibility == Visibility.Visible) QrCard.Visibility = Visibility.Collapsed;
        KnowledgeCard.Visibility = Visibility.Collapsed;      // 同一时刻只开一张卡
        WxCard.Visibility = Visibility.Collapsed;
        PreviewCard.Visibility = Visibility.Collapsed;
        ThanksCard.Visibility = Visibility.Visible;
        ModalScrim.Visibility = Visibility.Visible;
        // 等这一帧排完版再动（刚 Visible 的元素量不到尺寸，缩放会以 (0,0) 为中心）
        Motion.NextFrame(DispatcherQueue, () =>
        {
            Motion.BackdropIn(ScrimBlur, _scrimDim, 420);   // 缓入缓出；比卡片慢一拍
            Motion.ModalIn(ThanksCard, 480);
        });
    }

    /// <summary>收「感谢」。和赞赏码一样：**不能直接切 Collapsed**（硬断，显廉价），
    /// 两个动画同时长、都调同一个幂等收尾，谁先完都行。</summary>
    public void HideThanks()
    {
        if (ThanksCard.Visibility != Visibility.Visible) return;
        int seq = ++_thanksSeq;
        Motion.BackdropOut(ScrimBlur, _scrimDim, 320, done: () => CloseThanksIfCurrent(seq));
        Motion.ModalOut(ThanksCard, 320, done: () => CloseThanksIfCurrent(seq));
        Motion.After(DispatcherQueue, 900, () => CloseThanksIfCurrent(seq));   // 兜底
    }

    void CloseThanksIfCurrent(int seq)
    {
        if (seq != _thanksSeq) return;           // 期间又开 / 关过了，这次作废
        ThanksCard.Visibility = Visibility.Collapsed;
        if (!AnyCardOpen()) ModalScrim.Visibility = Visibility.Collapsed;
    }

    /// <summary>卡片内部的点击**不关浮层**（里面的项目链接还要能点）</summary>
    void ThanksCard_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
        => e.Handled = true;

    /// <summary>把感谢名单铺进卡片（只做一次）。链接点了直接开浏览器。</summary>
    void FillThanks()
    {
        if (_thanksFilled) return;
        _thanksFilled = true;

        (string Name, string What, string Lic, string Url)[] list =
        {
            // 素材类（不是开源项目）排在最前 —— 用户 2026-10-17 定：
            // 「在设置-感谢里把这个视频壁纸作者放第一位感谢」。
            // 随包那条默认视频背景就是他的片子，已获授权。
            ("B站 星雨布丁", "随包默认视频背景", "已获作者授权使用",
             "https://www.bilibili.com/video/BV1PaQHBaEsg"),
            ("思源黑体 / Noto Sans SC", "随包字幕字体（Light）", "SIL OFL 1.1",
             "https://github.com/notofonts/noto-cjk"),
            // finesub 的许可元数据是 GPL-3.0-or-later（不是纯 -3.0）；「由用户自行安装」
            // 是给「你们在分发 GPL 软件吗」这个问题一个当场可读的答案 —— 不，用户自己装的，
            // 我们只以独立进程调它（GPLv3 §9：跑一份副本本身无需接受许可、不产生义务）。
            ("finesub", "翻译引擎，整条流水线的主干", "GPL-3.0+（由用户自行安装）", "https://github.com/caca2331/finesub"),
            ("FFmpeg", "音视频处理与字幕烧录", "LGPL-3.0", "https://ffmpeg.org/"),
            // 2026-10-17：原来只写 "Unlicense"，用户自己看了都问「这个也没许可呀」——
            // 那名字看起来就像「没有许可」。许可清单（make_licenses.py）里本来就写作
            // 「Unlicense（公有领域）」，这里跟它保持一致。
            ("yt-dlp", "下载 YouTube / B 站视频", "Unlicense（公有领域）", "https://github.com/yt-dlp/yt-dlp"),
            ("faster-whisper / Whisper", "没有官方字幕时的语音识别", "MIT", "https://github.com/SYSTRAN/faster-whisper"),
            ("CTranslate2", "识别与翻译的推理加速", "MIT", "https://github.com/OpenNMT/CTranslate2"),
            ("Qwen3-ASR", "语音识别模型（第二模型复核）", "Apache-2.0", "https://github.com/QwenLM/Qwen3-ASR"),
            // ★ 2026-10-17：这一行原来指向 lucidrains/BS-Roformer —— 那是**架构代码**的作者，
            //   不是模型的人。而 UVR 的模型声明是**带条件**的：第三方应用可以用他们的模型，
            //   前提是「providing credit to UVR and its developers」。所以名字必须点到 UVR
            //   和三位作者。模型权重本身就是本软件在分发（镜像在对象存储上）。
            //   ⚠️ 许可那栏**只写 MIT 就行**：署名是靠这行本身完成的（名字 + 下面那行
            //   点出三位作者），再补一句「需署名」就是自我说明，用户 2026-10-17 要求去掉。
            //   依据上的那点微妙（依的是 UVR 的社区声明、不是逐模型授权）写在
            //   THIRD_PARTY_LICENSES.md 第四节，别搬到软件界面里来。
            ("BS-Roformer（UVR）", "人声与伴奏分离；模型由 UVR 发布（Anjok07、aufr33、viperx）",
             "MIT", "https://github.com/Anjok07/ultimatevocalremovergui"),
            ("Git for Windows", "引擎运行时需要的 git（MinGit 由引擎从上游下载）",
             "GPL-2.0", "https://github.com/git-for-windows/git"),
            ("IndexTTS", "本地语音克隆（可选）", "bilibili 模型使用许可", "https://github.com/index-tts/index-tts"),
            ("Windows App SDK / .NET", "界面", "MIT", "https://github.com/microsoft/WindowsAppSDK"),
        };

        ThanksList.Children.Add(new TextBlock
        {
            // 用户 2026-10-16：「站在肩膀上」那种比喻太油了，朴素点
            // 2026-10-17：名单里多了素材（视频壁纸），所以标题也得跟着带上「素材」
            Text = "感谢以下开源项目与素材：",
            TextWrapping = TextWrapping.Wrap, Opacity = 0.9, FontSize = 14,
        });
        foreach (var (name, what, lic, url) in list)
        {
            var row = new StackPanel { Spacing = 1 };
            row.Children.Add(new HyperlinkButton
            {
                Content = name + " ↗",
                NavigateUri = new Uri(url),
                Padding = new Thickness(0),
                FontSize = 13.5,
            });
            row.Children.Add(new TextBlock { Text = what, TextWrapping = TextWrapping.Wrap, FontSize = 13 });
            row.Children.Add(new TextBlock { Text = lic, FontSize = 11.5, Opacity = 0.55 });
            ThanksList.Children.Add(row);
        }
        ThanksList.Children.Add(new TextBlock
        {
            Text = "配音的音色来自小米 MiMo。\n完整许可证原文随软件附带（安装目录下的 THIRD_PARTY_LICENSES.md）。",
            TextWrapping = TextWrapping.Wrap, Opacity = 0.7, FontSize = 12.5, Margin = new Thickness(0, 4, 0, 0),
        });
        ThanksList.Children.Add(new TextBlock
        {
            Text = "点空白处关闭",
            FontSize = 12, Opacity = 0.45, HorizontalAlignment = HorizontalAlignment.Center,
            Margin = new Thickness(0, 6, 0, 0),
        });
    }

    // ============================================================ 「术语库」浮层
    //
    // 设置页「查看术语库…」弹它。用户 2026-10-16 定：跟「感谢」同一套观感
    //（渐隐渐现、点空白 / Esc 关掉、没有关闭按钮）。
    // 内容**每次打开都重取** —— 引擎翻译时会往里加词条，缓存一份就会显示旧的。

    int _kbSeq;
    int _kbBuiltin, _kbLearned;

    public void ShowKnowledge()
    {
        _kbSeq++;
        QrCard.Visibility = Visibility.Collapsed;         // 同一时刻只开一张卡
        ThanksCard.Visibility = Visibility.Collapsed;
        WxCard.Visibility = Visibility.Collapsed;
        PreviewCard.Visibility = Visibility.Collapsed;
        KnowledgeCard.Visibility = Visibility.Visible;
        ModalScrim.Visibility = Visibility.Visible;
        Motion.NextFrame(DispatcherQueue, () =>
        {
            Motion.BackdropIn(ScrimBlur, _scrimDim, 420);
            Motion.ModalIn(KnowledgeCard, 480);
        });
        _ = LoadKnowledgeAsync();
    }

    public void HideKnowledge()
    {
        if (KnowledgeCard.Visibility != Visibility.Visible) return;
        int seq = ++_kbSeq;
        Motion.BackdropOut(ScrimBlur, _scrimDim, 320, done: () => CloseKnowledgeIfCurrent(seq));
        Motion.ModalOut(KnowledgeCard, 320, done: () => CloseKnowledgeIfCurrent(seq));
        Motion.After(DispatcherQueue, 900, () => CloseKnowledgeIfCurrent(seq));   // 兜底
    }

    void CloseKnowledgeIfCurrent(int seq)
    {
        if (seq != _kbSeq) return;
        KnowledgeCard.Visibility = Visibility.Collapsed;
        if (!AnyCardOpen()) ModalScrim.Visibility = Visibility.Collapsed;
    }

    /// <summary>卡片内部的点击不关浮层（里面的按钮还要能按）</summary>
    void KnowledgeCard_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
        => e.Handled = true;

    async void KbRefresh_Click(object sender, RoutedEventArgs e) => await LoadKnowledgeAsync();

    /// <summary>
    /// 清空：删掉术语库再把随包的内置词条导回来（回到出厂状态）。
    /// ⚠️ 先弹确认 —— 这是**不可撤销**的（学到的东西删了就没了）。
    /// ⚠️ 确认框里必须说清「内置的会留下」：不说的话，用户清完看到还剩十几条
    ///    会以为没清干净，然后再点一次、还是那么多。
    /// </summary>
    async void KbClear_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new ContentDialog
        {
            XamlRoot = Content.XamlRoot,
            Title = "清空学到的术语？",
            Content = $"从你的视频里学到的 {_kbLearned} 条会删掉，删了找不回来。\n"
                    + $"软件自带的 {_kbBuiltin} 条通用术语会保留（它们不来自你的视频）。",
            PrimaryButtonText = "清空",
            CloseButtonText = "取消",
            DefaultButton = ContentDialogButton.Close,
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;

        KbClearBtn.IsEnabled = KbRefreshBtn.IsEnabled = false;
        KbStatus.Text = "正在清空…";
        try
        {
            var r = await Services.Backend.CallAsync("knowledge_clear", null, 5 * 60_000);
            if (r is not { } doc || !doc.TryGetProperty("ok", out var okp) || okp.ValueKind != JsonValueKind.True)
            {
                KbStatus.Text = r is { } r2 && r2.TryGetProperty("error", out var er)
                    ? er.GetString() : "清空没能完成";
            }
            else
            {
                int removed = doc.TryGetProperty("removed", out var rem) && rem.TryGetInt32(out var rv) ? rv : 0;
                KbStatus.Text = $"已清空 {removed} 条";
            }
        }
        catch (Exception ex) { KbStatus.Text = "清空失败：" + ex.Message; }
        finally
        {
            KbClearBtn.IsEnabled = KbRefreshBtn.IsEnabled = true;
        }
        await LoadKnowledgeAsync();
    }

    /// <summary>取术语库并铺进浮层。每次调用都重取（后端会先让引擎刷新索引）。</summary>
    async Task LoadKnowledgeAsync()
    {
        KbList.Children.Clear();
        KbSummary.Text = "正在读…";
        KbStatus.Text = "";

        JsonElement doc;
        try
        {
            var r = await Services.Backend.CallAsync("knowledge_list", null, 3 * 60_000);
            if (r is not { } rr) { KbSummary.Text = "读取失败：后端没响应"; return; }
            doc = rr;
        }
        catch (Exception ex) { KbSummary.Text = "读取失败：" + ex.Message; return; }

        if (doc.ValueKind != JsonValueKind.Object
            || !doc.TryGetProperty("ok", out var okp) || okp.ValueKind != JsonValueKind.True)
        {
            KbSummary.Text = doc.ValueKind == JsonValueKind.Object
                             && doc.TryGetProperty("error", out var er)
                ? "读取失败：" + er.GetString() : "读取失败";
            return;
        }

        _kbBuiltin = doc.TryGetProperty("builtin", out var bp) && bp.TryGetInt32(out var bv) ? bv : 0;
        _kbLearned = doc.TryGetProperty("learned", out var lp) && lp.TryGetInt32(out var lv) ? lv : 0;

        KbSummary.Text = $"共 {_kbBuiltin + _kbLearned} 条：软件自带的 {_kbBuiltin} 条 + 从你的视频里学到的 {_kbLearned} 条。\n"
                       + "只存在这台电脑上，不会上传；英文名、中文定名、别名都在这里，翻译时按它们对齐。";

        if (!doc.TryGetProperty("domains", out var domains) || domains.ValueKind != JsonValueKind.Array)
            return;

        foreach (var d in domains.EnumerateArray())
        {
            string title = d.TryGetProperty("title", out var t) && t.ValueKind == JsonValueKind.String
                ? t.GetString() ?? "" : "";
            var entries = d.TryGetProperty("entries", out var en) && en.ValueKind == JsonValueKind.Array
                ? en : default;

            var block = new StackPanel { Spacing = 8 };
            block.Children.Add(new TextBlock
            {
                Text = $"{title} · {(entries.ValueKind == JsonValueKind.Array ? entries.GetArrayLength() : 0)} 条",
                FontSize = 13, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold, Opacity = 0.85,
            });

            if (entries.ValueKind == JsonValueKind.Array)
            {
                foreach (var e in entries.EnumerateArray())
                {
                    string name = Str(e, "name"), desc = Str(e, "desc"), alias = Str(e, "aliases");
                    bool builtin = e.TryGetProperty("builtin", out var bi) && bi.ValueKind == JsonValueKind.True;

                    var head = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 7 };
                    head.Children.Add(new TextBlock { Text = name, FontSize = 13.5, FontWeight = Microsoft.UI.Text.FontWeights.SemiBold });
                    if (builtin)
                        head.Children.Add(new TextBlock { Text = "内置", FontSize = 11, Opacity = 0.5, VerticalAlignment = VerticalAlignment.Center });
                    block.Children.Add(head);

                    string line = desc;
                    if (alias is { Length: > 0 }) line += $"（别名：{alias}）";
                    block.Children.Add(new TextBlock
                    {
                        Text = line, FontSize = 12.5, Opacity = 0.72, TextWrapping = TextWrapping.Wrap,
                    });
                }
            }
            KbList.Children.Add(block);
        }

        if (KbList.Children.Count == 0)
            KbList.Children.Add(new TextBlock { Text = "还是空的。", Opacity = 0.6, FontSize = 13 });
    }

    static string Str(JsonElement o, string key) =>
        o.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    /// <summary>
    /// 换深浅。Application.RequestedTheme 运行期改不了，只能设根元素的 RequestedTheme。
    /// </summary>
    public void SetTheme(ElementTheme theme)
    {
        if (Content is FrameworkElement root) root.RequestedTheme = theme;
        if (theme == ElementTheme.Default) StyleTitleBar();

        // 注意：这里**不要**顺手重新导航当前页 —— 会来回触发事件、界面一直抖。
        // 面板颜色都写在元素上（不在 App 级样式里），元素级的 {ThemeResource} 会自己跟着主题重算。
    }

    /// <summary>
    /// 免安装程序系统给的那条标题栏默认是浅色的，和深色界面配在一起很突兀。
    /// 刷成透明让材料透上来，按钮改深色；老系统上不生效就退化成系统默认，不影响功能。
    /// </summary>
    void StyleTitleBar()
    {
        var tb = AppWindow?.TitleBar;
        if (tb is null) return;

        bool dark = (Content as FrameworkElement)?.ActualTheme != ElementTheme.Light;
        var transparent = Windows.UI.Color.FromArgb(0, 0, 0, 0);
        tb.BackgroundColor = transparent;
        tb.ButtonBackgroundColor = transparent;
        tb.ButtonHoverBackgroundColor = dark
            ? Windows.UI.Color.FromArgb(0x18, 0xFF, 0xFF, 0xFF)
            : Windows.UI.Color.FromArgb(0x14, 0x00, 0x00, 0x00);
        tb.ButtonPressedBackgroundColor = dark
            ? Windows.UI.Color.FromArgb(0x10, 0xFF, 0xFF, 0xFF)
            : Windows.UI.Color.FromArgb(0x0C, 0x00, 0x00, 0x00);
        tb.ForegroundColor = dark
            ? Windows.UI.Color.FromArgb(0xFF, 0xE8, 0xE8, 0xE8)
            : Windows.UI.Color.FromArgb(0xFF, 0x1A, 0x1A, 0x1A);
    }

    /// <summary>星空：固定种子随机撒点，窗口尺寸一变就重撒（大小跟着面积走）。</summary>
    void DrawStars()
    {
        double w = Root.ActualWidth, h = Root.ActualHeight;
        if (w < 50 || h < 50) return;      // 还没排好版，等 SizeChanged 再来

        StarCanvas.Children.Clear();
        var rnd = new Random(20261013);
        int count = (int)(w * h / 5200);

        for (int i = 0; i < count; i++)
        {
            double d = rnd.NextDouble();
            double size = d switch
            {
                < 0.78 => 2,
                < 0.94 => 3,
                _ => 4,
            };
            var star = new Ellipse
            {
                Width = size,
                Height = size,
                Fill = new SolidColorBrush(Windows.UI.Color.FromArgb(
                    (byte)(190 + rnd.Next(66)), 255, 255, 255)),
                Opacity = 0.5 + rnd.NextDouble() * 0.5,
            };
            // 像素对齐：不对齐的话 2px 的点会被抗锯齿摊成四个半亮像素，整片星空就糊了
            Canvas.SetLeft(star, Math.Round(rnd.NextDouble() * w));
            Canvas.SetTop(star, Math.Round(rnd.NextDouble() * h));
            StarCanvas.Children.Add(star);
        }

        // 两团星云：让深色底不是死平的
        foreach (var (cx, cy, r, color) in new[]
                 {
                     (0.78 * w, 0.10 * h, Math.Min(w, h) * 0.62, Windows.UI.Color.FromArgb(0x70, 0x84, 0x5C, 0xFF)),
                     (0.10 * w, 0.78 * h, Math.Min(w, h) * 0.56, Windows.UI.Color.FromArgb(0x5A, 0x2E, 0x7E, 0xFF)),
                 })
        {
            var glow = new Ellipse
            {
                Width = r * 2,
                Height = r * 2,
                IsHitTestVisible = false,
                Fill = new RadialGradientBrush
                {
                    Center = new Windows.Foundation.Point(0.5, 0.5),
                    GradientOrigin = new Windows.Foundation.Point(0.5, 0.5),
                    RadiusX = 0.5,
                    RadiusY = 0.5,
                },
            };
            ((RadialGradientBrush)glow.Fill).GradientStops.Add(new GradientStop { Color = color, Offset = 0 });
            ((RadialGradientBrush)glow.Fill).GradientStops.Add(
                new GradientStop { Color = Windows.UI.Color.FromArgb(0, 0, 0, 0), Offset = 1 });
            Canvas.SetLeft(glow, cx - r);
            Canvas.SetTop(glow, cy - r);
            StarCanvas.Children.Insert(0, glow);
        }
    }

    // ============================================================ 剪贴板：复制链接就用起来

    string _clipSeen = "";      // 处理过的剪贴板内容，别重复处理同一条

    /// <summary>现在屏幕上这条壁纸的**地址**（钉住 / 设置里显示的就是它）</summary>
    string _currentWallUrl = "";

    void OnClipboardChanged(object? sender, object e)
    {
        if (!_ready) return;
        DispatcherQueue.TryEnqueue(async () => await UseClipboardAsync());
    }

    /// <summary>
    /// 剪贴板里有链接就：**叫到前台 → 切到在线翻译 → 自动读**（单个/合集自己认）。
    /// 两种情况不打扰用户：**还没初始化**（人家正在做环境），以及**有任务在跑**（那会儿换链接没意义）。
    /// </summary>
    internal async Task UseClipboardAsync()
    {
        if (!Mock.IsInitialized || RealRunner.AnyRunning) return;

        var text = await ReadClipboardAsync();
        if (text.Length == 0 || text == _clipSeen) return;
        _clipSeen = text;

        var url = FirstLink(text);
        if (url.Length == 0) return;
        // 只认 YouTube / B 站：剪贴板里随便一条网址（购物、文档、公众号…）不该把软件弹出来
        // （用户 2026-10-14 定）
        if (!IsVideoSite(url)) return;
        // 框里已经是这一条（用户自己粘进去的）→ 别重来一遍
        if (ContentFrame.Content is OnlinePage cur && cur.CurrentLink == url) return;

        WindowFront.Bring(WinRT.Interop.WindowNative.GetWindowHandle(this));
        NavigateTo("online");
        await Task.Delay(300);                  // 等页面切出来（Frame 导航是异步的）
        (ContentFrame.Content as OnlinePage)?.TakeLink(url);
    }

    /// <summary>
    /// 这条链接是不是本站支持的站点（YouTube / B 站）。**按域名判断**，
    /// 不用 Contains —— 免得 `https://foo.com/?next=youtube.com/` 这种也被认成视频站。
    /// </summary>
    internal static bool IsVideoSite(string url)
    {
        try
        {
            var host = new Uri(url).Host.ToLowerInvariant().TrimEnd('.');
            if (host == "youtu.be" || host.EndsWith(".youtu.be")) return true;
            // www. / m. / music. 都算
            foreach (var domain in new[] { "youtube.com", "youtube-nocookie.com", "bilibili.com", "b23.tv" })
                if (host == domain || host.EndsWith("." + domain)) return true;
        }
        catch { /* 不是合法 URL */ }
        return false;
    }

    /// <summary>读剪贴板：别的程序正占着会抛异常，退避着重试几次</summary>
    internal static async Task<string> ReadClipboardAsync()
    {
        for (int i = 0; i < 3; i++)
        {
            try
            {
                var dp = Clipboard.GetContent();
                if (!dp.Contains(StandardDataFormats.Text)) return "";
                return ((await dp.GetTextAsync()) ?? "").Trim();
            }
            catch { await Task.Delay(120); }
        }
        return "";
    }

    /// <summary>剪贴板里常是「标题 + 链接 + 一堆废话」，挑第一个 http(s) 地址出来</summary>
    internal static string FirstLink(string text)
    {
        for (int i = text.IndexOf("http", StringComparison.OrdinalIgnoreCase); i >= 0;
             i = text.IndexOf("http", i + 1, StringComparison.OrdinalIgnoreCase))
        {
            var s = text[i..];
            if (!s.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                && !s.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) continue;

            int end = s.IndexOfAny([' ', '\r', '\n', '\t', '，', '。', '"', '<', '>', '、']);
            var url = (end < 0 ? s : s[..end]).Trim();
            return url.TrimEnd('，', '。', '）', ')', '、', ',', '.', ';', '；');
        }
        return "";
    }

    /// <summary>按设置里的选项造一个页面切换动效</summary>
    NavigationTransitionInfo BuildTransition() => Services.UiState.Transition switch
    {
        "slide-left" => new SlideNavigationTransitionInfo { Effect = SlideNavigationTransitionEffect.FromRight },
        "slide-bottom" => new SlideNavigationTransitionInfo { Effect = SlideNavigationTransitionEffect.FromBottom },
        "drill" => new DrillInNavigationTransitionInfo(),
        "common" => new CommonNavigationTransitionInfo(),
        "none" => new SuppressNavigationTransitionInfo(),
        _ => new EntranceNavigationTransitionInfo(),
    };

    /// <summary>
    /// 关于页**不用全局的页面转场** —— 它自带逐块错开的 hero 入场（见 AboutPage.PlayEntrance）。
    /// 叠在一起会变成"先钻取放大、再各块浮上来"，两套动效打架，反而糊。
    /// </summary>
    NavigationTransitionInfo TransitionFor(Type page) =>
        page == typeof(AboutPage) ? new SuppressNavigationTransitionInfo() : BuildTransition();

    /// <summary>
    /// 切到某个页面。先按导航项找；找不到的就是**不在导航栏里的工作区页面**
    /// （本地 / 在线翻译页 —— 用户说它们在导航栏里和页面顶部的切换重复了，所以摘了出去），
    /// 这时直接给 Frame 导航，并且把导航栏的选中清掉。
    /// </summary>
    public void NavigateTo(string tag)
    {
        if (tag == "subtitle" && SubtitleNav.Visibility != Visibility.Visible) return;
        foreach (var obj in RootNav.MenuItems.Concat(RootNav.FooterMenuItems))
        {
            if (obj is not NavigationViewItem it) continue;
            if ((it.Tag as string) == tag) { RootNav.SelectedItem = it; return; }
            foreach (var child in it.MenuItems)
                if (child is NavigationViewItem c && (c.Tag as string) == tag) { RootNav.SelectedItem = c; return; }
        }

        Type? page = tag switch
        {
            "env" or "home" => typeof(HomePage),   // 环境检查和「开始使用」是一页
            "local" or "local-single" or "local-batch" => typeof(LocalPage),
            "online" or "online-single" or "online-batch" => typeof(OnlinePage),
            "subtitle" => typeof(SubtitlePage),
            "settings" => typeof(SettingsPage),    // 这两个在导航栏里能直接找到，这里是 --page= 进来的兜底
            "about" => typeof(AboutPage),
            _ => null,
        };
        if (page is null) return;
        if (ContentFrame.CurrentSourcePageType != page)
            ContentFrame.Navigate(page, null, TransitionFor(page));
    }

    // ============================================================ Win32 投放目标（绕过 WinUI 的拖放）
    //
    // ⚠️⚠️ WinUI 3 内建的拖放（AllowDrop / DragOver / Drop）在**UAC 关闭**的机器上是哑的：
    // 事件一个都不触发、光标一直是禁止符号，而且什么提示都没有。
    // 已知 issue：microsoft/microsoft-ui-xaml#10119（提问者把 EnableLUA 改回 1 重启才好）。
    // 本机 UAC 就是关的（EnableLUA=0）。
    //
    // 所以不靠它：把窗口注册成**经典 Win32 投放目标**（DragAcceptFiles + WM_DROPFILES），
    // 这条路不经过 XAML，UAC 开不开都能用。
    // ⚠️ 代价：拖动过程中光标**不会**变成「可放下」的样子，松手才会生效 —— 用户容易以为没生效。

    const uint WM_DROPFILES = 0x0233;

    delegate IntPtr SubclassProc(IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam,
                                 UIntPtr uIdSubclass, IntPtr dwRefData);

    [DllImport("comctl32.dll", SetLastError = true)]
    static extern bool SetWindowSubclass(IntPtr hWnd, SubclassProc pfnSubclass, UIntPtr uIdSubclass, IntPtr dwRefData);

    [DllImport("comctl32.dll")]
    static extern IntPtr DefSubclassProc(IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam);

    [DllImport("shell32.dll", SetLastError = true)]
    static extern void DragAcceptFiles(IntPtr hWnd, bool fAccept);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    static extern uint DragQueryFileW(IntPtr hDrop, uint iFile, StringBuilder? lpszFile, uint cch);

    [DllImport("shell32.dll")]
    static extern void DragFinish(IntPtr hDrop);

    SubclassProc? _wndProc;      // 必须留引用：被 GC 回收掉窗口就崩

    void InstallWin32DropTarget()
    {
        try
        {
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
            DragAcceptFiles(hwnd, true);
            _wndProc = WndProcHook;
            SetWindowSubclass(hwnd, _wndProc, 1, IntPtr.Zero);
        }
        catch (Exception e)
        {
            App.Dump("InstallWin32DropTarget 失败: " + e);
        }
    }

    IntPtr WndProcHook(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam, UIntPtr id, IntPtr data)
    {
        if (msg == WM_DROPFILES)
        {
            try
            {
                var files = new List<string>();
                uint count = DragQueryFileW(wParam, 0xFFFFFFFF, null, 0);
                for (uint i = 0; i < count; i++)
                {
                    uint len = DragQueryFileW(wParam, i, null, 0);
                    if (len == 0) continue;
                    var sb = new StringBuilder((int)len + 2);
                    DragQueryFileW(wParam, i, sb, (uint)sb.Capacity);
                    files.Add(sb.ToString());
                }
                DragFinish(wParam);
                if (files.Count > 0) FilesDropped?.Invoke(files.ToArray());
            }
            catch (Exception e)
            {
                App.Dump("处理 WM_DROPFILES 失败: " + e);
            }
            return IntPtr.Zero;
        }
        return DefSubclassProc(hWnd, msg, wParam, lParam);
    }
}
