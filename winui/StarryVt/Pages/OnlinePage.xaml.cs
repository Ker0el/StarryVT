using System.Collections.ObjectModel;
using System.Text.Json;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Navigation;
using Starry.Vt.App.Fakes;
using Starry.Vt.App.Services;

namespace Starry.Vt.App.Pages;

/// <summary>在线翻译：单个链接 + 合集，两种模式在页内切换（同上，不再用导航栏两级子项）。</summary>
public sealed partial class OnlinePage : Page
{
    readonly IRunner _runner;
    readonly ObservableCollection<LogLine> _log = Mock.SeedLog();
    readonly List<QueueItem> _items = [];

    public IReadOnlyList<string> Voices => Mock.Voices;

    bool _ready;
    bool _running;
    string _mode = "single";

    /// <summary>「翻译完自动打开视频」开着没有（设置里的 auto_open，开跑前问一次后端）</summary>
    bool _autoOpen;

    /// <summary>已经解析过的链接（点开始翻译时用它，而不是框里此刻的内容 —— 中间可能又被改了）</summary>
    string _link = "";

    /// <summary>按标题认出来的背景信息（翻译模型的一句话上下文，跟着任务一起发过去）</summary>
    string _extra = "";

    /// <summary>设置里的默认输出（final / dub / subs）</summary>
    string _defaultOutput = "final";

    DispatcherQueueTimer? _pasteDelay;   // 粘完链接稍等一下再自动读，别粘到一半就发请求
    string _readUrl = "";                // 已经读过的那条链接，别重复读

    public OnlinePage()
    {
        InitializeComponent();

        LogList.ItemsSource = _log;

        // 默认源语言 / 音色 / 输出都在设置里配，工作页不再放这些下拉
        _ = LoadOutputDefaultAsync();

        // 真后端：开工请求 + 订阅它主动推的状态（见 LocalPage 里的同名说明）
        _runner = new RealRunner(Plan);
        _runner.Line += OnLine;
        _runner.Progress += OnProgress;
        _runner.Queue += OnQueue;
        _runner.Finished += OnFinished;

        foreach (var list in new[] { PlaylistPane, LogList })
            Services.Motion.ItemsAnimate(list);

        // 页面实例要**缓存复用**：不然每次切页都是新实例，翻译中的日志 / 队列 / 进度
        // 一离开就全没了（用户 2026-10-14 实测：翻译中切到设置再回来，结果没了）
        NavigationCacheMode = Microsoft.UI.Xaml.Navigation.NavigationCacheMode.Required;

        _ready = true;
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        // 设置页可能刚改过「默认输出」，回到本页时把按钮文字重新对一遍
        _ = LoadOutputDefaultAsync();
    }

    /// <summary>开工用哪个后端方法 + 什么参数（点按钮那一刻取界面上的值）</summary>
    (string, object) Plan() => _mode == "batch"
        ? ("start_batch", new
        {
            source = _link,
            batch_kind = "playlist",
            extra_info = _extra,
            do_dub = _defaultOutput is "final" or "dub",
            burn_subs = _defaultOutput == "final" || _defaultOutput == "subs-burn",
            download_only = _defaultOutput == "download",
            asr_only = _defaultOutput == "subs-src",
        })
        : ("start", new
        {
            source = _link,
            extra_info = _extra,
            do_dub = _defaultOutput is "final" or "dub",
            burn_subs = _defaultOutput == "final" || _defaultOutput == "subs-burn",
            download_only = _defaultOutput == "download",
            asr_only = _defaultOutput == "subs-src",
        });

    static string Str(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    static List<string> Strs(JsonElement el, string name)
    {
        var list = new List<string>();
        if (el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Array)
            foreach (var it in v.EnumerateArray())
                if (it.ValueKind == JsonValueKind.String) list.Add(it.GetString() ?? "");
        return list;
    }

    void Hint(string text)
    {
        QuietStatus.Text = text;
        Motion.Reveal(QuietStatus, 6, 200);
    }

    // ---------- 模式 ----------

    void ModeBar_SelectionChanged(SelectorBar sender, SelectorBarSelectionChangedEventArgs args)
    {
        if (!_ready) return;
        SetMode(sender.SelectedItem?.Tag as string == "online-batch" ? "batch" : "single");
    }

    void SetMode(string mode)
    {
        _mode = mode;
        bool batch = mode == "batch";
        InputTitle.Text = batch ? "粘贴合集链接" : "粘贴视频链接";
        LinkBox.PlaceholderText = batch ? "https://www.youtube.com/playlist?list=…"
                                        : "https://www.youtube.com/watch?v=…";
        SinglePane.Visibility = batch ? Visibility.Collapsed : Visibility.Visible;
        PlaylistBox.Visibility = batch ? Visibility.Visible : Visibility.Collapsed;
        Motion.Reveal(batch ? PlaylistBox : SinglePane, 14, 220);
        UpdateStartLabel();
        ModeBar.SelectedItem = ModeBar.Items[batch ? 1 : 0];
    }

    /// <summary>按钮文字跟着当前模式和「默认输出」走：下载视频时不叫「开始翻译」</summary>
    void UpdateStartLabel()
    {
        bool batch = _mode == "batch";
        StartLabel.Text = _defaultOutput switch
        {
            "download" => batch ? "开始批量下载" : "下载视频",
            "subs-src" => batch ? "开始批量提取字幕" : "提取原文字幕",
            _ => batch ? "开始批量翻译" : "开始翻译",
        };
    }

    // ---------- 剪贴板 ----------
    // 监听在**窗口层**（MainWindow）：用户可能在设置页、本地页复制链接，
    // 那时候这个页面压根没加载，挂在这儿就收不到（用户 2026-10-14 反馈）。
    // 页面这边只管「拿到一条链接之后怎么办」。

    /// <summary>输入框里现在这条（窗口层用来判断"是不是同一条，别重复处理"）</summary>
    internal string CurrentLink => LinkBox.Text.Trim();

    /// <summary>
    /// 从剪贴板来的一条链接：切模式 → 填进框 → 立刻读。
    /// 单个视频还是合集**按地址认**（LooksLikePlaylist），不花网络时间。
    /// </summary>
    internal void TakeLink(string url)
    {
        if (!_ready || url.Length == 0) return;
        SetMode(LooksLikePlaylist(url) ? "batch" : "single");
        LinkBox.Text = "";          // ⚠️ 内容一模一样时 TextChanged 不会触发，先清空再填
        LinkBox.Text = url;
        Hint("已从剪贴板读到链接");
    }

    /// <summary>
    /// 这条链接是「合集」还是「单个视频」—— 按地址认，不花网络时间。
    /// 认不出来的（别的站点）当单个视频处理。
    /// </summary>
    static bool LooksLikePlaylist(string url)
    {
        var s = url.ToLowerInvariant();
        // 单个：YouTube 的 /watch、youtu.be，B 站的 /video/、短链 b23.tv
        // （watch?v=…&list=… 是「合集里的一条」，YouTube 自己也先播那一条）
        if (s.Contains("youtube.com/watch") || s.Contains("youtu.be/")
            || s.Contains("bilibili.com/video/") || s.Contains("b23.tv/")) return false;

        return s.Contains("/playlist") || s.Contains("list=") || s.Contains("/medialist")
            || s.Contains("space.bilibili.com") || s.Contains("/channel/")
            || s.Contains("/series") || s.Contains("/@") || s.Contains("/c/") || s.Contains("/user/");
    }

    // ---------- 解析 / 读取列表 ----------

    /// <summary>
    /// 粘完链接**自己就查**，没有按钮（用户 2026-10-14：不要按钮了）。
    /// 判断"粘完了"的办法：以 http 开头、并且带 watch? / youtu.be / list= 这些特征，
    /// 然后等 0.6 秒没有新输入才发请求。
    /// </summary>
    void LinkBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!_ready || _running) return;
        var src = LinkBox.Text.Trim();
        if (src == _readUrl || !LooksLikeLink(src)) return;

        _pasteDelay ??= NewPasteTimer();
        _pasteDelay.Stop();
        _pasteDelay.Start();
    }

    /// <summary>回车 = 别等了，现在就查</summary>
    void LinkBox_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter || !_ready || _running) return;
        e.Handled = true;
        _pasteDelay?.Stop();
        _ = ReadNowAsync(LinkBox.Text.Trim());
    }

    DispatcherQueueTimer NewPasteTimer()
    {
        var t = DispatcherQueue.CreateTimer();
        t.Interval = TimeSpan.FromMilliseconds(600);
        t.IsRepeating = false;
        t.Tick += (_, _) =>
        {
            t.Stop();
            _ = ReadNowAsync(LinkBox.Text.Trim());
        };
        return t;
    }

    static bool LooksLikeLink(string s) =>
        s.StartsWith("http", StringComparison.OrdinalIgnoreCase)
        && (s.Contains("watch?") || s.Contains("youtu.be/") || s.Contains("list=")
            || s.Contains("v=") || s.Contains("://"));

    /// <summary>取一次标题（不下载），顺便按标题认出是哪款软件 —— 认出来的话，
    /// 相关术语会跟着这次翻译一起给模型（背景信息）。</summary>
    async Task ReadNowAsync(string src)
    {
        if (src.Length == 0 || src == _readUrl || !LooksLikeLink(src)) return;
        _readUrl = src;                     // 认下这条，别重复读
        SetReading(true);
        try
        {
            if (_mode == "single") await ReadSingleAsync(src);
            else await ReadPlaylistAsync(src);
        }
        catch (Exception ex) { App.Dump("自动解析失败: " + ex.Message); }
        finally { SetReading(false); }
    }

    /// <summary>读取中：框右边转个圈（原来「解析」按钮的位置）</summary>
    void SetReading(bool busy)
    {
        ReadRing.IsActive = busy;
        ReadRing.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
    }

    /// <summary>
    /// 封面：先直接挂网络地址（国内站点一下就出来），同时让后端（带代理）下一份本地缓存 ——
    /// 界面的 BitmapImage 走系统网络、**不走我们设置里的代理**，YouTube 的封面直连下不来。
    /// </summary>
    async Task SetShotAsync(string url)
    {
        InfoShot.ImageSource = null;
        if (url.Length == 0) return;
        try { InfoShot.ImageSource = new BitmapImage(new Uri(url)); } catch { }
        try
        {
            var r = await Backend.CallAsync("url_thumbs", new { urls = new[] { url } }, 60_000);
            if (r is not null && r.Value.TryGetProperty("files", out var f)
                && f.ValueKind == JsonValueKind.Object && f.TryGetProperty(url, out var p)
                && p.ValueKind == JsonValueKind.String)
            {
                var local = p.GetString() ?? "";
                if (local.Length > 0) InfoShot.ImageSource = new BitmapImage(new Uri(local));
            }
        }
        catch (Exception ex) { App.Dump("封面缓存失败（不影响其它）: " + ex.Message); }
    }

    /// <summary>合集每一行的封面也这么办：一次性交给后端批量下（走代理），下好按行替换</summary>
    async Task LoadListThumbsAsync(List<(QueueItem item, string url)> pending)
    {
        var urls = pending.Select(x => x.url).Where(u => u.Length > 0).Distinct().ToList();
        if (urls.Count == 0) return;
        try
        {
            var r = await Backend.CallAsync("url_thumbs", new { urls }, 180_000);
            if (r is null || !r.Value.TryGetProperty("files", out var files)
                || files.ValueKind != JsonValueKind.Object) return;
            foreach (var (item, url) in pending)
            {
                if (files.TryGetProperty(url, out var p) && p.ValueKind == JsonValueKind.String)
                {
                    var local = p.GetString() ?? "";
                    if (local.Length > 0)
                    {
                        try { item.Thumb = new BitmapImage(new Uri(local)); } catch { }
                    }
                }
            }
        }
        catch (Exception ex) { App.Dump("合集封面缓存失败（不影响其它）: " + ex.Message); }
    }

    async Task ReadSingleAsync(string src)
    {
        InfoCard.Visibility = Visibility.Visible;
        InfoTitle.Text = "正在读取标题…";
        InfoMeta.Text = "";
        InfoShot.ImageSource = null;                 // 先清掉上一个的封面
        InfoPath.Visibility = Visibility.Collapsed;
        Motion.Reveal(InfoCard, 12, 240);

        var r = await Backend.CallAsync("probe", new { source = src }, 120_000);
        if (r is null || !r.Value.TryGetProperty("title", out var t) || t.ValueKind != JsonValueKind.String)
        {
            InfoTitle.Text = "❌ 读不出标题：" + (Backend.LastError is { Length: > 0 } e ? e : "链接或网络问题");
            InfoMeta.Text = "照样可以点「开始翻译」，标题只影响背景信息";
            _link = src;                       // 解析失败也能开跑，交给后端去报真正的错
            return;
        }

        _link = src;
        _extra = Str(r.Value, "extra");
        InfoTitle.Text = t.GetString() ?? "";
        await SetShotAsync(Str(r.Value, "thumb"));

        // 第二行：作者 · 时长 · 认出来的软件（有哪样显示哪样，都没有就不占地方）
        var apps = Strs(r.Value, "apps");
        var bits = new List<string>();
        var by = Str(r.Value, "uploader");
        var dur = Str(r.Value, "duration");
        if (by.Length > 0) bits.Add(by);
        if (dur.Length > 0) bits.Add(dur);
        if (apps.Count > 0) bits.Add("认出来了：" + string.Join(" / ", apps));
        InfoMeta.Text = string.Join("  ·  ", bits);
        if (_extra.Length > 0)
        {
            InfoPath.Text = _extra;
            InfoPath.Visibility = Visibility.Visible;
        }
    }

    /// <summary>自测入口（`--probe=playlist:&lt;url&gt;`）：把链接填进去、切到合集模式，
    /// 后面的**自动读取**会自己跑起来（不用鼠标）。</summary>
    internal void SelfTestLink(string url, bool batch)
    {
        SetMode(batch ? "batch" : "single");
        // ⚠️ 先清空再填：内容和框里原来一模一样时 TextChanged 不会触发，
        //    自测拿默认那条链接跑就会「什么都没发生」
        LinkBox.Text = "";
        LinkBox.Text = url;              // 触发 TextChanged → 0.6 秒后自动读
    }

    /// <summary>合集：让后端用 yt-dlp 读出全部条目（不下载），顺便按标题批量认软件</summary>
    async Task ReadPlaylistAsync(string src)
    {
        _items.Clear();
        PlaylistPane.ItemsSource = null;            // 先把上一份清掉，免得看着像"没读出来"
        PlaylistHint.Text = "正在读取合集列表：" + src;
        PlaylistHint.Visibility = Visibility.Visible;

        var r = await Backend.CallAsync("scan_source", new { source = src, batch_kind = "playlist" }, 300_000);
        if (r is null || !r.Value.TryGetProperty("items", out var arr) || arr.ValueKind != JsonValueKind.Array)
        {
            PlaylistHint.Text = "❌ 读不出合集：" + (Backend.LastError is { Length: > 0 } e ? e : "链接或网络问题");
            return;
        }

        var pending = new List<(QueueItem item, string url)>();
        foreach (var it in arr.EnumerateArray())
        {
            // 行上先摆作者和时长（一开跑就会被后端推来的状态顶掉，正好当进度用）
            var by = Str(it, "by");
            var dur = Str(it, "dur");
            var sub = string.Join("  ·  ", new[] { by, dur }.Where(x => x.Length > 0));
            var item = new QueueItem { Title = Str(it, "title"), Detail = sub.Length > 0 ? sub : "待翻译" };
            var thumb = Str(it, "thumb");
            // 封面是网络图，BitmapImage 自己会异步去下；下不动就还是占位图
            // （YouTube 直连下不到，后面统一交给后端带代理下一份本地的替换掉）
            if (thumb.Length > 0)
            {
                try { item.Thumb = new BitmapImage(new Uri(thumb)); } catch { }
                pending.Add((item, thumb));
            }
            _items.Add(item);
        }

        // ⚠️ _items 是普通 List，改它**不会**让 ListView 刷新 —— 必须重新赋一次
        //   ItemsSource，否则读出来的条目一个都不显示（用户 2026-10-14 就是这么撞上的）
        PlaylistPane.ItemsSource = _items.ToList();
        _ = LoadListThumbsAsync(pending);           // 封面（走代理）后台补上，不挡列表

        _link = src;
        PlaylistHint.Visibility = _items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        if (_items.Count == 0) { PlaylistHint.Text = "这个合集里没有读到视频"; return; }

        // 一批标题一起认，比只看第一条准（合集基本都是一个软件一个系列）
        var titles = _items.Select(i => i.Title).ToList();
        var pr = await Backend.CallAsync("probe_titles", new { titles }, 120_000);
        if (pr is not null)
        {
            _extra = Str(pr.Value, "extra");
            var apps = Strs(pr.Value, "apps");
            // 合集自己的名字和作者（后端从 yt-dlp 的 playlist_* 字段拿的）
            var collName = "";
            var collBy = "";
            if (r.Value.TryGetProperty("coll", out var coll) && coll.ValueKind == JsonValueKind.Object)
            {
                collName = Str(coll, "title");
                collBy = Str(coll, "by");
            }
            var head = collName.Length > 0 ? $"《{collName}》" : "";
            if (collBy.Length > 0) head += (head.Length > 0 ? "  ·  " : "") + collBy;
            var bits = new List<string>();
            if (head.Length > 0) bits.Add(head);
            bits.Add($"共 {_items.Count} 集");
            if (apps.Count > 0) bits.Add("认出来了：" + string.Join(" / ", apps));
            PlaylistHint.Text = string.Join("  ·  ", bits);
            PlaylistHint.Visibility = Visibility.Visible;
        }
    }

    // ---------- 跑 ----------

    async void Start_Click(object sender, RoutedEventArgs e)
    {
        // ⚠️ async void 事件处理器**必须自己吞异常**（逃出去就是进程消失，2026-10-14 踩过）
        try
        {
            if (_running) return;
            var src = LinkBox.Text.Trim();
            if (src.Length == 0) { Hint("先把链接粘进来"); return; }

            // 设置页改成「下载视频」后工作页是缓存复用的，必须开工前再读一次，
            // 否则还按旧的「出完整成片」走，白白跑一遍人声分离 / 识别 / 翻译。
            await LoadOutputDefaultAsync();

            // 先查 Key 再开跑：没填就引导去设置页，别下载完一大半才发现翻不了
            // 「只提取原文字幕」不碰任何 Key，和下载视频一样直接放行
            if (!await Services.Keys.EnsureAsync(XamlRoot, doDub: _defaultOutput is "final" or "dub", downloadOnly: _defaultOutput is "download" or "subs-src")) return;

            // 「解析」是可选的：直接开跑也行，只是少了按标题认出来的背景信息。
            // 框里的内容跟解析过的不一样，就当没解析过（别把上一个链接的背景信息带过去）。
            if (src != _link)
            {
                _link = src;
                _extra = "";
            }

            if (_mode == "batch") StartPlaylist();
            else StartSingle();
        }
        catch (Exception ex) { App.Dump("开始翻译失败: " + ex); }
    }

    void StartSingle()
    {
        SetMode("single");
        _log.Clear();
        bool dl = _defaultOutput == "download";
        _log.Add(new LogLine { Text = (dl ? "开始下载：" : "开始下载并翻译：") + _link, Level = LogLevel.Dim });
        EnterRunning(dl ? "下载中…" : "下载并翻译中…");
        _runner.Start();
    }

    void StartPlaylist()
    {
        SetMode("batch");
        _log.Clear();
        _log.Add(new LogLine { Text = _defaultOutput == "download"
            ? $"合集共 {_items.Count} 个视频，逐个下载"
            : $"合集共 {_items.Count} 个视频，逐个下载 + 翻译", Level = LogLevel.Normal });
        EnterRunning(_defaultOutput == "download" ? "下载中…" : "下载并翻译中…");
        _runner.Start();
    }

    /// <summary>开跑时把界面摆成「在跑」的样子</summary>
    void EnterRunning(string busyText)
    {
        StateBar.Visibility = Visibility.Visible;
        TaskPanel.Visibility = Visibility.Visible;
        Motion.Reveal(StateBar, 10, 240);
        Motion.Reveal(TaskPanel, 22, 300);
        SetBusy(true, busyText);
        Bar.Value = 0;
        PctText.Text = "0%";
        StageText.Text = "准备中";
        ToolTipService.SetToolTip(StageText, null);
        OpenOutButton.Visibility = Visibility.Collapsed;
        StartButton.IsEnabled = false;
        CancelButton.IsEnabled = true;
        CancelButton.Visibility = Visibility.Visible;
        _running = true;
        _ = LoadAutoOpenAsync();          // 「翻完自动打开视频」按开跑那一刻的设置来
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
        // 收尾交给 Finished（后端走到下一步才会真的停）
        CancelButton.IsEnabled = false;
        SetBusy(true, "正在取消…");
        StateText.Text = "正在取消…";
        _runner.Cancel();
    }

    /// <summary>打开输出目录（按钮写的就是目录，所以开目录）</summary>
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

    /// <summary>合集：后端把整条队列推过来，直接整体换掉</summary>
    void OnQueue(IReadOnlyList<QueueItem> q)
    {
        PlaylistHint.Visibility = Visibility.Collapsed;
        // 后端推来的是全新条目，封面是本地的 —— 按顺序搬过去，不然一开跑封面就没了
        var thumbs = _items.Select(x => x.Thumb).ToList();
        _items.Clear();
        for (int i = 0; i < q.Count; i++)
        {
            if (i < thumbs.Count && thumbs[i] is not null) q[i].Thumb = thumbs[i];
            _items.Add(q[i]);
        }
        PlaylistPane.ItemsSource = _items.ToList();
    }

    void OnProgress(string stage, int pct, string detail)
    {
        if (_running && !BusyRing.IsActive) SetBusy(true);
        Bar.Value = pct;
        PctText.Text = pct + "%";
        StateText.Text = stage == "dub" && !string.IsNullOrWhiteSpace(detail)
            ? detail
            : stage switch
            {
                "download" => "正在下载…",
                "translate" => "正在翻译…",
                "dub" => "正在配音…",
                "burn" => "正在烧录字幕…",
                _ => "处理中…",
            };
        StageText.Text = stage switch
        {
            "download" => "下载视频",
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
                SetStage(_runner.ResultNote, "");
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
                StateText.Text = _mode == "batch" ? "合集处理完了" : "全部完成";
                SetStage(_runner.ResultNote, _runner.ResultPath);
                MaybeOpenResult();
                break;
        }
    }

    /// <summary>
    /// 设置里开了「翻译完自动打开视频」时，单个视频跑完自动用默认播放器打开成片。
    /// **只对单个视频生效**（合集一口气出几十个，自动开一堆窗口没意义）。
    /// </summary>
    void MaybeOpenResult()
    {
        if (!_autoOpen || _mode != "single") return;
        var file = _runner.ResultPath;
        if (file.Length == 0 || !File.Exists(file)) return;
        if (Path.GetExtension(file).Equals(".srt", StringComparison.OrdinalIgnoreCase)) return;  // 只出字幕时不打开
        _ = Backend.CallAsync("open_path", new { path = file }, 20_000);
    }

    /// <summary>把设置里的「默认输出」填到工作页</summary>
    async Task LoadOutputDefaultAsync()
    {
        try
        {
            var r = await Backend.CallAsync("boot", null, 30_000);
            if (r is null || !r.Value.TryGetProperty("settings", out var s)) return;
            var mode = Str(s, "default_output");
            // ⚠️ 新增输出模式**必须加进这个白名单**，否则设置里选了也读不进来
            //    （6.18 的 subs-src 就栽在这里：看着设置生效了，实际还在翻译）
            if (mode is "final" or "dub" or "subs" or "subs-src" or "subs-burn" or "download")
                _defaultOutput = mode;
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
    /// 进度条下面那行小字：单个显示成品文件名，合集显示「成功 N，失败 M」（路径是目录，
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
