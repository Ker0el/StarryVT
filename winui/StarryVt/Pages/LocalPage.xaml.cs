using System.Collections.ObjectModel;
using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Navigation;
using Starry.Vt.App.Fakes;
using Starry.Vt.App.Services;
using Windows.UI;

namespace Starry.Vt.App.Pages;

/// <summary>
/// 本地翻译：单个视频 + 整个文件夹，两种模式在页内切换。
/// （原来做成导航栏的两级子项，点父项会弹一个飞出菜单，很怪，所以合成一页。）
/// </summary>
public sealed partial class LocalPage : Page
{
    readonly IRunner _runner;
    readonly ObservableCollection<LogLine> _log = Mock.SeedLog();
    readonly ObservableCollection<QueueItem> _items = [];

    bool _ready;
    bool _running;
    string _mode = "single";

    /// <summary>「翻译完自动打开视频」开着没有（设置里的 auto_open，开跑前问一次后端）</summary>
    bool _autoOpen;
    const string BatchDropHint = "把文件夹拖到这里";
    VideoItem? _current;

    /// <summary>单个模式选中的那个视频（拖进来 / 点选都往这儿放）</summary>
    VideoItem? _picked;

    /// <summary>
    /// 真正开跑时用的是**完整路径** —— VideoItem 只存了名字和所在目录，拼不回原路径
    /// （同名不同目录就错了），所以单独记一份。
    /// </summary>
    string _pickedPath = "";

    /// <summary>批量模式选中的文件夹</summary>
    string _batchFolder = "";

    /// <summary>设置里的默认输出（final / dub / subs）</summary>
    string _defaultOutput = "final";

    public IReadOnlyList<string> Voices => Mock.Voices;

    /// <summary>选中一个视频：卡片上显示出来</summary>
    void TakeVideo(string path)    {
        var name = Path.GetFileName(path);
        var dir = Path.GetDirectoryName(path) ?? "";
        _pickedPath = path;
        _picked = new VideoItem { Name = name, Info = dir };
        DropHint.Visibility = Visibility.Collapsed;
        PickedInfo.Visibility = Visibility.Visible;
        PickedName.Text = name;
        PickedPath.Text = dir;
        PickedShot.ImageSource = null;               // 先清掉上一个的封面
        Motion.Reveal(PickedInfo, 10, 220);
        _ = LoadPickedShotAsync(path);
    }

    /// <summary>给选中的那个视频抽一帧当封面（同上：抽不出来就一直看占位图）</summary>
    async Task LoadPickedShotAsync(string path)
    {
        try
        {
            var r = await Backend.CallAsync("video_thumb", new { path }, 60_000);
            if (r is null || !r.Value.TryGetProperty("ok", out var ok) || ok.ValueKind != JsonValueKind.True)
                return;
            var file = Str(r.Value, "file");
            if (file.Length == 0) return;
            PickedShot.ImageSource = new BitmapImage(new Uri(file));
        }
        catch (Exception ex) { App.Dump("取单个视频封面失败（不影响其它功能）: " + ex.Message); }
    }

    /// <summary>点投放区 → 弹系统文件对话框（选完就显示在卡片上）</summary>
    async void PickVideo_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FileOpenPicker();
            foreach (var ext in new[] { ".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".ts", ".m4v" })
                picker.FileTypeFilter.Add(ext);
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(MainWindow.Instance!);
            WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            var file = await picker.PickSingleFileAsync();
            if (file is not null) TakeVideo(file.Path);
        }
        catch (Exception ex) { App.Dump("选视频失败（不影响其它功能）: " + ex.Message); }
    }

    /// <summary>
    /// 自测入口（`--probe=run:&lt;视频路径或文件夹&gt;`）：拿真后端真跑一单，走的还是
    /// 「选中视频 / 选文件夹 → 点开始翻译」这几条路，只是不用鼠标去点。正式使用走不到这里。
    /// </summary>
    /// <summary>自测入口（`--probe=shot:&lt;视频&gt;`）：只把这个视频放进卡片（看封面），**不开跑**。</summary>
    internal void SelfTestPick(string path)
    {
        path = path.Trim().Trim('"');
        if (File.Exists(path)) TakeVideo(path);
    }

    /// <summary>自测入口（`--probe=scan:&lt;文件夹&gt;`）：只把文件夹读成清单，**不开跑**。</summary>
    internal async void SelfTestScan(string path)
    {
        path = path.Trim().Trim('"');        // 命令行里带空格的路径要加引号传进来
        if (!Directory.Exists(path)) { App.Dump("scan 探针：文件夹不存在 " + path); return; }
        await LoadFolderAsync(path);
    }

    internal async void SelfTest(string path)
    {
        if (Directory.Exists(path))
        {
            await LoadFolderAsync(path);                 // 批量：后端列清单 → 点开始批量翻译
            Start_Click(StartButton, new RoutedEventArgs());
            return;
        }
        TakeVideo(path);
        Start_Click(StartButton, new RoutedEventArgs());
    }

    // ---------- 拖进来的反馈（照 AI-image2 那个投放区的动效做的）----------

    Storyboard? _iconBounce;
    SolidColorBrush? _rectBrush;
    Color _rectIdle = Color.FromArgb(0x59, 0xFF, 0xFF, 0xFF);
    bool _dragOver;

    /// <summary>图标跳动：0.6s 来回、无限循环（对应 CSS 里的 zoneBounce）</summary>
    void EnsureBounce()
    {
        if (_iconBounce is not null) return;
        _iconBounce = new Storyboard();
        foreach (var prop in new[] { "ScaleX", "ScaleY" })
        {
            var a = new DoubleAnimation
            {
                From = 1.1,
                To = 1.25,
                Duration = new Duration(TimeSpan.FromMilliseconds(600)),
                AutoReverse = true,
                RepeatBehavior = RepeatBehavior.Forever,
                EasingFunction = new CubicEase { EasingMode = EasingMode.EaseInOut },
                EnableDependentAnimation = true,
            };
            Storyboard.SetTarget(a, DropIconScale);
            Storyboard.SetTargetProperty(a, prop);
            _iconBounce.Children.Add(a);
        }
    }

    /// <summary>拖动经过窗口时：虚线变实线 + 换成主题色 + 铺一层渐变 + 图标跳动</summary>
    void SetDragOver(bool on)
    {
        if (_dragOver == on) return;
        _dragOver = on;
        try
        {
            // 描边换成「本地画刷」才能单独给它的颜色做动画（主题画刷是共享的，动不得）
            _rectBrush ??= DropRect.Stroke is SolidColorBrush shared
                ? new SolidColorBrush(shared.Color)
                : new SolidColorBrush(_rectIdle);
            _rectIdle = _rectBrush.Color;
            DropRect.Stroke = _rectBrush;

            var accent = Application.Current.Resources.TryGetValue("SystemAccentColor", out var o) && o is Color c
                ? c : Color.FromArgb(0xFF, 0xFF, 0x6F, 0xB5);

            DropRect.StrokeDashArray = on ? [] : new DoubleCollection { 4, 3 };
            DropRect.StrokeThickness = on ? 2 : 1.4;

            var color = new Storyboard();
            var ca = new ColorAnimation
            {
                To = on ? accent : _rectIdle,
                Duration = new Duration(TimeSpan.FromMilliseconds(200)),
                EnableDependentAnimation = true,
            };
            Storyboard.SetTarget(ca, _rectBrush);
            Storyboard.SetTargetProperty(ca, "Color");
            color.Children.Add(ca);
            color.Begin();

            Motion.FadeTo(DropGlow, on ? 1 : 0, 200);
            Grow(DropScale, on ? 1.012 : 1, 200);

            // 批量模式：单个那边的投放区是藏着的，反馈给到队列顶上那条虚线框（样式和单个一致）
            if (_mode == "batch")
            {
                BatchDropRect.Stroke = new SolidColorBrush(on ? accent : Color.FromArgb(0x59, 0xFF, 0xFF, 0xFF));
                BatchDropRect.StrokeDashArray = on ? [] : new DoubleCollection { 4, 3 };
                BatchDropRect.StrokeThickness = on ? 2 : 1.4;
                BatchDropRect.Fill = new SolidColorBrush(on ? Color.FromArgb(0x1F, accent.R, accent.G, accent.B)
                                                            : Color.FromArgb(0x0D, 0xFF, 0xFF, 0xFF));
                Motion.FadeTo(BatchGlow, on ? 1 : 0, 200);
                BatchDropText.Text = on ? "松手就用这个文件夹" : BatchDropHint;
                BatchDropSub.Visibility = on ? Visibility.Collapsed : Visibility.Visible;
                BatchDropIcon.Glyph = on ? "" : "";
            }

            EnsureBounce();
            if (on) _iconBounce!.Begin();
            else { _iconBounce!.Stop(); Grow(DropIconScale, 1, 160); }
        }
        catch (Exception e) { App.Dump("拖入反馈失败（不影响落下文件）: " + e.Message); }
    }

    /// <summary>给 ScaleTransform 做个缩放动画</summary>
    static void Grow(ScaleTransform t, double to, int ms)
    {
        var sb = new Storyboard();
        foreach (var prop in new[] { "ScaleX", "ScaleY" })
        {
            var a = new DoubleAnimation
            {
                To = to,
                Duration = new Duration(TimeSpan.FromMilliseconds(ms)),
                EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
                EnableDependentAnimation = true,
            };
            Storyboard.SetTarget(a, t);
            Storyboard.SetTargetProperty(a, prop);
            sb.Children.Add(a);
        }
        sb.Begin();
    }

    void OnDragEnter() => SetDragOver(true);
    void OnDragLeave() => SetDragOver(false);

    /// <summary>松手落下：批量模式进队列，单个模式直接当选中</summary>
    void OnDropFiles(string[] files)
    {
        SetDragOver(false);
        OnFilesDropped(files);
    }

    public LocalPage()
    {
        InitializeComponent();
        // 页面实例要**缓存复用**：不然每次切页都是新实例，翻译中的日志 / 队列 / 进度
        // 一离开就全没了（用户 2026-10-14 实测：翻译中切到设置再回来，结果没了）
        NavigationCacheMode = Microsoft.UI.Xaml.Navigation.NavigationCacheMode.Required;

        // 默认源语言 / 音色 / 输出都在设置里配，工作页不再放这些下拉
        _ = LoadOutputDefaultAsync();

        QueueList.ItemsSource = _items;
        LogList.ItemsSource = _log;

        // 队列空的时候「清空」按钮就灰着。
        // ⚠️ 光靠 CollectionChanged 不够：启动时队列里已经有内容（种子数据），
        // 那些是构造前就加好的，事件不会补发 —— 按钮会一直是灰的（用户实测：点不了）。
        _items.CollectionChanged += (_, _) => ClearQueueButton.IsEnabled = _items.Count > 0;
        ClearQueueButton.IsEnabled = _items.Count > 0;

        // 真后端：开工请求 + 订阅它主动推的状态。走哪个方法在**点按钮那一刻**才决定
        // （单个 start / 批量 start_batch），参数也取那一刻界面上的选择。
        _runner = new RealRunner(Plan);
        _runner.Line += OnLine;
        _runner.Progress += OnProgress;
        _runner.Queue += OnQueue;
        _runner.Finished += OnFinished;

        foreach (var list in new[] { QueueList, LogList })
            Services.Motion.ItemsAnimate(list);

        // ★ 每次进软件都自检一遍（2026-10-16 用户要求）——
        //   完整理由见 Services/EnvCheck.cs 的说明，一句话：
        //   **已经配好的用户启动后直接进这一页，首页根本不会被创建，那份自检一次都不跑**，
        //   于是「引擎被卸载了 / 数据目录被删了」要等他点了「开始翻译」才炸出来，
        //   而导航锁读的还只是上次成功时写下的那个旧标记。
        Loaded += async (_, _) =>
        {
            var ok = await Services.EnvCheck.EngineOkAsync();
            if (ok is null) return;                       // 问不到就什么都别改（判据失灵不误伤）
            Mock.EngineReady = ok.Value;
            MainWindow.Instance?.SetNavLocked(!ok.Value);
            // 引擎真没了就把他带回首页 —— 只有那一页有清单和处理入口
            if (!ok.Value) MainWindow.Instance?.NavigateTo("home");
        };
        _ready = true;
    }

    /// <summary>开工用哪个后端方法 + 什么参数（点按钮那一刻取界面上的值）</summary>
    (string, object) Plan() => _mode == "batch"
        ? ("start_batch", new
        {
            source = _batchFolder,
            batch_kind = "folder",
            do_dub = _defaultOutput is "final" or "dub",
            burn_subs = _defaultOutput == "final" || _defaultOutput == "subs-burn",
            download_only = _defaultOutput == "download",
        })
        : ("start", new
        {
            source = _pickedPath,
            do_dub = _defaultOutput is "final" or "dub",
            burn_subs = _defaultOutput == "final" || _defaultOutput == "subs-burn",
            download_only = _defaultOutput == "download",
        });

    static string Str(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        MainWindow.FilesDropped += OnFilesDropped;       // 兜底通道（老的 WM_DROPFILES）
        Services.FileDrop.Enter += OnDragEnter;          // 拖动中的反馈靠它
        Services.FileDrop.Leave += OnDragLeave;
        Services.FileDrop.Dropped += OnDropFiles;
        // 设置页可能刚改过「默认输出」，回到本页时把按钮文字重新对一遍
        _ = LoadOutputDefaultAsync();
        // 从在线页切过来的话，进来先落到对应的模式
        var pm = Services.UiState.PendingMode;
        if (pm is not null && pm.StartsWith("local"))
        {
            Services.UiState.PendingMode = null;
            SetMode(pm == "local-batch" ? "batch" : "single");
        }
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e)
    {
        base.OnNavigatedFrom(e);
        MainWindow.FilesDropped -= OnFilesDropped;
        Services.FileDrop.Enter -= OnDragEnter;
        Services.FileDrop.Leave -= OnDragLeave;
        Services.FileDrop.Dropped -= OnDropFiles;
    }

    void OnFilesDropped(string[] files)
    {
        if (files.Length == 0) return;
        if (Directory.Exists(files[0]))
        {
            SetMode("batch");            // 拖进来的是文件夹 → 直接切到批量（含子文件夹一起找）
            _ = LoadFolderAsync(files[0]);
            return;
        }
        // 批量模式下拖进来一个视频文件：他多半是想"翻这个文件夹"（课程资料都是一集一个文件），
        // 那就按它所在的文件夹来，别切回单个
        if (_mode == "batch")
        {
            var dir = Path.GetDirectoryName(files[0]);
            if (!string.IsNullOrEmpty(dir)) { _ = LoadFolderAsync(dir); return; }
        }
        SetMode("single");
        TakeVideo(files[0]);
    }

    // ---------- 模式切换 ----------

    void ModeBar_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        if (!_ready) return;
        SetMode(sender.SelectedItem?.Tag as string == "local-batch" ? "batch" : "single");
    }

    void SetMode(string mode)
    {
        _mode = mode;
        bool batch = mode == "batch";
        BatchHeader.Visibility = batch ? Visibility.Visible : Visibility.Collapsed;
        DropZone.Visibility = batch ? Visibility.Collapsed : Visibility.Visible;
        BatchPane.Visibility = batch ? Visibility.Visible : Visibility.Collapsed;
        // 换模式时左边这块是整块换掉的，给它一点淡入，别硬切
        Motion.Reveal(batch ? BatchPane : DropZone, 14, 220);
        UpdateStartLabel();
        ModeBar.SelectedItem = ModeBar.Items[batch ? 1 : 0];
    }

    /// <summary>按钮文字跟着当前模式和「默认输出」走：下载视频时不叫「开始翻译」</summary>
    void UpdateStartLabel()
    {
        bool batch = _mode == "batch";
        StartLabel.Text = _defaultOutput == "download"
            ? (batch ? "开始批量下载" : "下载视频")
            : (batch ? "开始批量翻译" : "开始翻译");
    }

    // ---------- 列表操作 ----------

    /// <summary>点「选文件夹」→ 弹系统对话框，选完让后端列出里面的视频</summary>
    async void PickFolder_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FolderPicker();
            picker.FileTypeFilter.Add("*");       // 不加一条会直接抛异常
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(MainWindow.Instance!);
            WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            var folder = await picker.PickSingleFolderAsync();
            if (folder is null) return;
            await LoadFolderAsync(folder.Path);
        }
        catch (Exception ex) { App.Dump("选文件夹失败（不影响其它功能）: " + ex.Message); }
    }

    /// <summary>
    /// 让后端列出文件夹里的视频（真实的那一份清单，不递归子目录）。
    /// 单条要不要翻译由后端排队时再判断，这里只负责显示。
    /// </summary>
    async Task LoadFolderAsync(string path)
    {
        SetMode("batch");
        _batchFolder = path;
        _items.Clear();
        ClearQueueButton.IsEnabled = false;
        QueueHint.Text = "正在读取：" + path;
        QueueHint.Visibility = Visibility.Visible;

        var r = await Backend.CallAsync("scan_source", new { source = path, batch_kind = "folder" }, 120_000);
        if (r is null || !r.Value.TryGetProperty("items", out var arr) || arr.ValueKind != JsonValueKind.Array)
        {
            QueueHint.Text = "❌ 读不出来：" + (Backend.LastError is { Length: > 0 } e ? e : "后端没有响应");
            return;
        }

        // 递归扫出来的条目带上"在第几层"，同名文件一眼能分清
        var srcs = new List<string>();
        foreach (var it in arr.EnumerateArray())
        {
            var rel = Str(it, "rel");
            _items.Add(new QueueItem { Title = Str(it, "title"), Detail = rel.Length > 0 ? rel : "待翻译" });
            srcs.Add(Str(it, "src"));
        }

        QueueHint.Visibility = _items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        if (_items.Count == 0)
            QueueHint.Text = "这里没有能处理的视频：" + path;
        else
            QueueHint.Text = $"{_items.Count} 个视频 · " + path;

        _ = LoadThumbsAsync([.. _items], srcs);      // 缩略图慢慢来，不挡着清单显示
    }

    /// <summary>
    /// 逐行异步取缩略图（后端抽帧 + 缓存）。三路并发：抽一帧几百毫秒，
    /// 全串行的话几十集的文件夹要等十几秒；抽不出来的那行就留着占位图。
    /// </summary>
    async Task LoadThumbsAsync(List<QueueItem> items, List<string> srcs)
    {
        using var sem = new SemaphoreSlim(3);
        var tasks = new List<Task>();
        for (int i = 0; i < items.Count && i < srcs.Count; i++)
        {
            var item = items[i];
            var src = srcs[i];
            tasks.Add(Task.Run(async () =>
            {
                await sem.WaitAsync();
                try
                {
                    var r = await Backend.CallAsync("video_thumb", new { path = src }, 60_000);
                    if (r is null || !r.Value.TryGetProperty("ok", out var ok) || ok.ValueKind != JsonValueKind.True)
                        return;
                    var file = Str(r.Value, "file");
                    if (file.Length == 0) return;
                    // BitmapImage 必须在界面线程上建
                    DispatcherQueue.TryEnqueue(() =>
                    {
                        try { item.Thumb = new BitmapImage(new Uri(file)); }
                        catch (Exception ex) { App.Dump("缩略图建图失败（不影响其它功能）: " + ex.Message); }
                    });
                }
                catch (Exception ex) { App.Dump("取缩略图失败（不影响其它功能）: " + ex.Message); }
                finally { sem.Release(); }
            }));
        }
        await Task.WhenAll(tasks);
    }

    /// <summary>清空队列（正在跑的时候不让清：会把已经翻译着的条目抽掉）</summary>
    void ClearQueue_Click(object sender, RoutedEventArgs e)
    {
        if (_running)
        {
            QuietStatus.Text = "翻译中，先取消再清空";
            Motion.Reveal(QuietStatus, 6, 200);
            return;
        }
        _items.Clear();
        QueueList.ItemsSource = _items;          // 挂回 ObservableCollection，后面增删都能实时反映
        ClearQueueButton.IsEnabled = false;
    }

    // ---------- 开跑 ----------

    async void Start_Click(object sender, RoutedEventArgs e)
    {
        // ⚠️ async void 事件处理器**必须自己吞异常**（逃出去就是进程消失，2026-10-14 踩过）
        try
        {
            if (_running) return;

            // 设置页改成「下载视频」后工作页是缓存复用的，必须开工前再读一次，
            // 否则还按旧的「出完整成片」走，白白跑一遍人声分离 / 识别 / 翻译。
            await LoadOutputDefaultAsync();

            // 顺序：先查"本地该选的选没选"，再查 Key。
            // 反过来的话，用户还没选视频就先被叫去填 Key，很莫名其妙。
            if (_mode == "batch")
            {
                if (_items.Count == 0) { Hint("先选一个文件夹（或者把文件夹拖进来）"); return; }
                if (!await Services.Keys.EnsureAsync(XamlRoot, doDub: _defaultOutput is "final" or "dub", downloadOnly: _defaultOutput == "download")) return;   // 没填 Key → 引导去设置页
                StartBatch();
                return;
            }
            if (_picked is null) { Hint("先选一个视频"); return; }
            if (!await Services.Keys.EnsureAsync(XamlRoot, doDub: _defaultOutput is "final" or "dub", downloadOnly: _defaultOutput == "download")) return;
            StartSingle(_picked);
        }
        catch (Exception ex) { App.Dump("开始翻译失败: " + ex); }
    }

    void Hint(string text)
    {
        QuietStatus.Text = text;
        Motion.Reveal(QuietStatus, 6, 200);
    }

    void StartSingle(VideoItem? item)
    {
        if (item is null) return;
        SetMode("single");
        _current = item;

        _log.Clear();
        bool dl = _defaultOutput == "download";
        _log.Add(new LogLine { Text = (dl ? "开始下载：" : "开始翻译：") + item.Name, Level = LogLevel.Dim });

        EnterRunning(dl ? "下载中…" : "翻译中…");
        _runner.Start();
    }

    void StartBatch()
    {
        SetMode("batch");
        _log.Clear();
        _log.Add(new LogLine { Text = $"共 {_items.Count} 个视频，逐个处理（一个完成再接下一个）", Level = LogLevel.Normal });
        EnterRunning(_defaultOutput == "download" ? "下载中…" : "翻译中…");
        _runner.Start();
    }

    /// <summary>开跑时把界面摆成「在跑」的样子（日志面板、进度条、取消按钮…）</summary>
    void EnterRunning(string busyText)
    {
        StateBar.Visibility = Visibility.Visible;
        TaskPanel.Visibility = Visibility.Visible;
        Motion.Reveal(StateBar, 10, 240);
        Motion.Reveal(TaskPanel, 22, 300);   // 右边日志晚一点点出来，有点层次
        SetBusy(true, busyText);
        Bar.Value = 0;
        PctText.Text = "0%";
        StageText.Text = "准备中";
        ToolTipService.SetToolTip(StageText, null);
        OpenOutButton.Visibility = Visibility.Collapsed;   // 上一轮的成品收起来
        StartButton.IsEnabled = false;
        CancelButton.IsEnabled = true;
        CancelButton.Visibility = Visibility.Visible;
        _running = true;
        _ = LoadAutoOpenAsync();                           // 「翻完自动打开视频」按开跑那一刻的设置来
    }


    /// <summary>任务运行期间一直转；有百分比时也不隐藏，避免 AI 翻译/下模型的长步骤看着像卡住。</summary>
    void SetBusy(bool busy, string? text = null)
    {
        BusyRing.IsActive = busy;
        BusyRing.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        QuietStatus.Text = text ?? (busy ? "处理中…" : "就绪");
    }

    void Cancel_Click(object sender, RoutedEventArgs e)
    {
        // 真正的收尾（按钮恢复、状态横幅）交给 Finished：后端要走到下一步才会真的停下，
        // 这里先把它按灰，免得连点。
        CancelButton.IsEnabled = false;
        SetBusy(true, "正在取消…");
        StateText.Text = "正在取消…";
        _runner.Cancel();
    }

    /// <summary>打开输出目录（按钮写的就是目录，所以开目录 —— 单个视频时开成片文件很怪，用户 2026-10-14 反馈）</summary>
    async void OpenOut_Click(object sender, RoutedEventArgs e)
    {
        var dir = _runner.ResultDir;
        if (dir.Length == 0 && _runner.ResultPath.Length > 0)
            dir = Path.GetDirectoryName(_runner.ResultPath) ?? "";
        if (dir.Length == 0) { Hint("还没有成品，先跑一次"); return; }
        var r = await Backend.CallAsync("open_path", new { path = dir });
        if (r is null) Hint("打不开：" + Backend.LastError);
    }

    // ---------- 后端推来的东西 ----------

    void OnLine(LogLine line)
    {
        // 进度行**就地替换**上一条，别每 1% 插一行把日志刷爆（见 LogLine.Merge）
        if (_log.Count > 0 && LogLine.Merge(_log[^1], line)) return;
        _log.Add(line);
        DispatcherQueue.TryEnqueue(() => LogScroll.ChangeView(null, LogScroll.ScrollableHeight, null));
    }

    /// <summary>批量：后端把整条队列推过来，直接整体换掉（条目是新建的，属性改了不刷新也没关系）</summary>
    void OnQueue(IReadOnlyList<QueueItem> q)
    {
        QueueHint.Visibility = Visibility.Collapsed;
        // 后端每次推的是**全新的** QueueItem，缩略图是本地抽好挂在旧对象上的 ——
        // 按顺序搬过来，不然一开跑图就全没了
        var thumbs = _items.Select(x => x.Thumb).ToList();
        _items.Clear();
        for (int i = 0; i < q.Count; i++)
        {
            var it = q[i];
            if (i < thumbs.Count && thumbs[i] is not null) it.Thumb = thumbs[i];
            _items.Add(it);
        }
    }

    void OnProgress(string stage, int pct)
    {
        if (_running && !BusyRing.IsActive) SetBusy(true);
        Bar.Value = pct;
        PctText.Text = pct + "%";
        StateText.Text = stage switch
        {
            "download" => "准备中…",
            "translate" => "正在翻译…",
            "dub" => "正在配音…",
            "burn" => "正在烧录字幕…",
            _ => "处理中…",
        };
        StageText.Text = stage switch
        {
            "download" => "下载 / 准备",
            "translate" => "AI 翻译",
            "dub" => "中文配音",
            "burn" => "字幕烧录",
            _ => stage,
        };
    }

    void OnFinished()
    {
        _running = false;
        StartButton.IsEnabled = true;
        CancelButton.Visibility = Visibility.Collapsed;
        CancelButton.IsEnabled = true;

        switch (_runner.EndStage)
        {
            case "error":
                SetBusy(false, "出错了");
                StateText.Text = "出错了";
                // 进度条停在半路才是对的，别拉满骗人
                SetStage(_runner.ResultNote.Length > 0 ? _runner.ResultNote : "详细原因看右边日志", "");
                break;

            case "cancelled":
                SetBusy(false, "已取消");
                StateText.Text = "已取消";
                SetStage("已完成的不受影响", "");
                break;

            default:
                Bar.Value = 100;
                PctText.Text = "100%";
                SetBusy(false, "完成 · 刚翻好");
                StateText.Text = _mode == "batch" ? "批量完成" : "全部完成";
                SetStage(_runner.ResultNote, _runner.ResultPath);
                MaybeOpenResult();
                break;
        }
    }

    /// <summary>
    /// 设置里开了「翻译完自动打开视频」时，单个视频跑完自动用默认播放器打开成片。
    /// **只对单个视频生效**（批量一口气出几十个，自动开一堆窗口没意义）。
    /// </summary>
    void MaybeOpenResult()
    {
        if (!_autoOpen || _mode != "single") return;
        var file = _runner.ResultPath;
        if (file.Length == 0 || !File.Exists(file)) return;
        if (Path.GetExtension(file).Equals(".srt", StringComparison.OrdinalIgnoreCase)) return;  // 只出字幕时不打开
        _ = Backend.CallAsync("open_path", new { path = file }, 20_000);
    }

    /// <summary>读设置里的「默认输出」，开跑时作为默认模式</summary>
    async Task LoadOutputDefaultAsync()
    {
        try
        {
            var r = await Backend.CallAsync("boot", null, 30_000);
            if (r is null || !r.Value.TryGetProperty("settings", out var s)) return;
            var mode = Str(s, "default_output");
            if (mode is "final" or "dub" or "subs" or "subs-burn" or "download") _defaultOutput = mode;
        }
        catch { /* 读不到就用「出完整成片」 */ }
        UpdateStartLabel();
    }

    /// <summary>开跑前问一次设置（开跑那一刻才决定，改完设置不用重启）</summary>
    async Task LoadAutoOpenAsync()
    {
        try
        {
            var r = await Backend.CallAsync("boot", null, 30_000);
            if (r is not null && r.Value.TryGetProperty("settings", out var st)
                && st.ValueKind == JsonValueKind.Object
                && st.TryGetProperty("auto_open", out var v))
                _autoOpen = v.ValueKind == JsonValueKind.True;
        }
        catch { /* 读不到就当没开 */ }
    }

    /// <summary>
    /// 进度条下面那行小字：单个显示成品文件名，批量显示「成功 N，失败 M」（那个路径是目录，
    /// 显示目录名没意义）。完整路径挂在悬停提示上。
    /// </summary>
    void SetStage(string note, string path)
    {
        bool isDir = path.Length > 0 && Directory.Exists(path);
        StageText.Text = isDir && note.Length > 0 ? note
            : path.Length > 0 ? "成品：" + Path.GetFileName(path)
            : note.Length > 0 ? note : "完成";
        ToolTipService.SetToolTip(StageText, path.Length > 0 ? path : note);
        if (path.Length > 0) OpenOutButton.Visibility = Visibility.Visible;
    }
}
