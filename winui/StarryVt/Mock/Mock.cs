using System.Collections.ObjectModel;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;            // Visibility（日志进度条的显隐要它）
using Microsoft.UI.Xaml.Media;
using Starry.Vt.App.Services;
using Windows.UI;

namespace Starry.Vt.App.Fakes;

/// <summary>日志分级：决定那一行显示成什么颜色（和真实后端的分类规则一致）。</summary>
public enum LogLevel { Normal, Dim, Ok, Warn, Err, Progress }

public sealed class LogLine : System.ComponentModel.INotifyPropertyChanged
{
    string _text = "";
    double _pct = -1;

    public event System.ComponentModel.PropertyChangedEventHandler? PropertyChanged;
    void Raise(string n) => PropertyChanged?.Invoke(this, new System.ComponentModel.PropertyChangedEventArgs(n));

    public string Text
    {
        get => _text;
        set
        {
            if (_text == value) return;
            _text = value;
            Pct = ParsePct(value);
            Raise(nameof(Text));
        }
    }

    public LogLevel Level { get; init; }

    /// <summary>
    /// 这一行里带的百分比（0~100）；不是进度行就是 -1。
    /// 进度行不加这个就画不出条 —— 光靠 <see cref="LogLevel.Progress"/> 只知道"是进度"，
    /// 不知道到哪了（原来进度只能靠人读那串数字）。
    /// </summary>
    public double Pct
    {
        get => _pct;
        private set
        {
            if (Math.Abs(_pct - value) < 0.05) return;
            _pct = value;
            Raise(nameof(Pct));
            Raise(nameof(BarVisibility));
        }
    }

    /// <summary>有百分比的才显示那根条（给 XAML 绑的，Visibility 不能直接写三元）</summary>
    public Visibility BarVisibility => _pct >= 0 ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>
    /// 从文本里抠百分比。后端的进度行只有两种写法：
    ///   `[download] 26.4% of 48MiB at 3.2MiB/s`（yt-dlp 原样透传）
    ///   `[1/4] 语音识别 34% · 15/43` / `配音合成 12/43`（引擎那边的计数）
    /// 优先认 `%`；没有 `%` 就取**最后**一个 `a/b`（不能取第一个：`[1/4]` 是阶段序号，不是进度）。
    /// </summary>
    static double ParsePct(string s)
    {
        if (string.IsNullOrEmpty(s)) return -1;
        var m = System.Text.RegularExpressions.Regex.Match(s, @"(\d+(?:\.\d+)?)\s*%");
        if (m.Success && double.TryParse(m.Groups[1].Value, out var pv)) return Math.Clamp(pv, 0, 100);

        var ms = System.Text.RegularExpressions.Regex.Matches(s, @"(\d+)\s*/\s*(\d+)");
        for (int i = ms.Count - 1; i >= 0; i--)
        {
            if (!int.TryParse(ms[i].Groups[1].Value, out var a)) continue;
            if (!int.TryParse(ms[i].Groups[2].Value, out var b) || b <= 0) continue;
            if (a > b) continue;                       // 不是分数就别当进度
            return Math.Clamp(a * 100.0 / b, 0, 100);
        }
        return -1;
    }

    /// <summary>
    /// 新来的这行是不是「同一条进度的下一次上报」：是就地替换上一条、返回 true。
    ///
    /// 判据 = 两边都是进度行，且**把数字抹掉之后文本一样**：
    /// `[download] 26.4% of 48MiB` 和 `[download] 51.2% of 48MiB` 是同一件事的两次上报；
    /// 而 `语音识别 34%` 和 `纠错翻译 18%` 抹掉数字后不同 → 各占一行。
    ///
    /// 为什么必须合并：yt-dlp 一秒能推好几条进度，直接 append 会把日志刷屏，
    /// 用户看到的是「一屏重复的数字」而不是「一根条在动」（2026-10-15 用户定：不要刷屏）。
    /// </summary>
    public static bool Merge(LogLine last, LogLine next)
    {
        // ⚠️ 判据是「**有没有百分比**」，不是「Level 是不是 Progress」。
        //    这两者不等价：`配音合成 12/43` 这种**没有 `%`** 的进度行会被 Classify 判成 Normal
        //    （它只认 `%`），但它是实打实的进度（一句一行）。按 Level 判的话它每句都插一行 → 刷屏。
        //    2026-10-15 用户报「进度条还是刷屏」，就是这里。
        if (last.Pct < 0 || next.Pct < 0) return false;
        if (Key(last.Text) != Key(next.Text)) return false;
        last.Text = next.Text;          // 属性变了，x:Bind OneWay 自己刷新
        return true;
    }

    /// <summary>
    /// 合并键 = 抹掉数字之后**只取前 24 个字符**。
    ///
    /// ⚠️ 必须截断：进度行的**尾巴一直在变**，拿整串当键永远合不上 ——
    ///   `[download] 51.8% of 48MiB at 3.1MiB/s ETA 00:12` 和
    ///   同一行的下一帧 `... ETA Unknown` 尾串不同，按整串比就是两行，照样刷屏。
    ///   取前缀既能让同一件事的多次上报对上，又能把不同阶段（语音识别 / 纠错翻译）分开。
    /// </summary>
    static string Key(string s)
    {
        var k = System.Text.RegularExpressions.Regex.Replace(s ?? "", @"[\d.]+", "#");
        return k.Length > 24 ? k[..24] : k;
    }

    /// <summary>WinUI 的 TextBlock 不认 enum，这里直接把颜色算好</summary>
    public SolidColorBrush Foreground => Level switch
    {
        LogLevel.Ok => new SolidColorBrush(Color.FromArgb(255, 0x6E, 0xE7, 0xB7)),
        LogLevel.Warn => new SolidColorBrush(Color.FromArgb(255, 0xFF, 0xC4, 0x6B)),
        LogLevel.Err => new SolidColorBrush(Color.FromArgb(255, 0xFF, 0x8F, 0x99)),
        LogLevel.Dim => new SolidColorBrush(Color.FromArgb(120, 0xFF, 0xFF, 0xFF)),
        LogLevel.Progress => new SolidColorBrush(Color.FromArgb(180, 0xC8, 0xD4, 0xEE)),
        _ => new SolidColorBrush(Color.FromArgb(220, 0xD6, 0xDE, 0xF2)),
    };
}

/// <summary>批量队列的一行</summary>
public sealed class QueueItem : System.ComponentModel.INotifyPropertyChanged
{
    public event System.ComponentModel.PropertyChangedEventHandler? PropertyChanged;

    void N(string name) =>
        PropertyChanged?.Invoke(this, new System.ComponentModel.PropertyChangedEventArgs(name));

    public string Title { get; init; } = "";
    public string Detail { get; set; } = "";
    public string Status { get; set; } = "pending";   // pending / running / done / failed

    Microsoft.UI.Xaml.Media.ImageSource? _thumb;

    /// <summary>缩略图：后台抽好了才填进来（没抽到就留空，模板那边显示占位图）</summary>
    public Microsoft.UI.Xaml.Media.ImageSource? Thumb
    {
        get => _thumb;
        set { _thumb = value; N(nameof(Thumb)); }
    }

    public string Glyph => Status switch
    {
        "running" => "\uE895",      // 转圈
        "done" => "\uE73E",         // 对勾
        "failed" => "\uE711",       // 叉
        _ => "\uE91F",              // 空心圆
    };

    public SolidColorBrush IconBrush => Status switch
    {
        "running" => new SolidColorBrush(Color.FromArgb(255, 0x5B, 0x9C, 0xFF)),
        "done" => new SolidColorBrush(Color.FromArgb(255, 0x4E, 0xD6, 0xA0)),
        "failed" => new SolidColorBrush(Color.FromArgb(255, 0xFF, 0x7A, 0x85)),
        _ => new SolidColorBrush(Color.FromArgb(120, 0xFF, 0xFF, 0xFF)),
    };
}

/// <summary>环境自检的一行</summary>
public sealed class EnvItem
{
    public string Title { get; init; } = "";
    public string Detail { get; init; } = "";
    public string State { get; init; } = "ok";        // ok / warn / bad
    public string Fix { get; init; } = "";            // 空 = 这行没有修复按钮
    public string FixLabel { get; init; } = "修复";

    public string Glyph => State switch
    {
        "ok" => "\uE73E",
        "warn" => "\uE7BA",
        _ => "\uEA39",
    };

    public SolidColorBrush IconBrush => State switch
    {
        "ok" => new SolidColorBrush(Color.FromArgb(255, 0x4E, 0xD6, 0xA0)),
        "warn" => new SolidColorBrush(Color.FromArgb(255, 0xFF, 0xC4, 0x6B)),
        _ => new SolidColorBrush(Color.FromArgb(255, 0xFF, 0x7A, 0x85)),
    };

    public Microsoft.UI.Xaml.Visibility HasFix =>
        string.IsNullOrEmpty(Fix) ? Microsoft.UI.Xaml.Visibility.Collapsed : Microsoft.UI.Xaml.Visibility.Visible;
}


/// <summary>
/// 主页「环境初始化」清单里的一项。
///
/// 状态会**一项一项地变**（等着 → 转圈 → 打勾），所以必须实现 INotifyPropertyChanged，
/// 模板那边用 Mode=OneWay 绑定才吃得动。
/// </summary>
public sealed class SetupStep : System.ComponentModel.INotifyPropertyChanged
{
    public event System.ComponentModel.PropertyChangedEventHandler? PropertyChanged;

    void N(string name) => PropertyChanged?.Invoke(this, new System.ComponentModel.PropertyChangedEventArgs(name));

    public string Title { get; init; } = "";
    /// <summary>自检报告里那一项的 id（tools / engine / keys…），用来挑「首次初始化」要办哪几件</summary>
    public string Key { get; init; } = "";
    /// <summary>auto = 我们能自动搞定；manual = 要你自己去设置里填</summary>
    public string Kind { get; init; } = "auto";
    /// <summary>还没轮到它时显示的话</summary>
    public string Todo { get; init; } = "";
    /// <summary>自检报告里这项本来就是好的吗（好的就按 ok 收尾）</summary>
    public bool WasOk { get; init; } = true;
    public string Fix { get; init; } = "";          // 按钮动作 tag
    public string FixLabel { get; init; } = "";     // 按钮文字（空 = 这行不给按钮）

    string _state = "todo";     // todo / doing / ok / warn
    string _detail = "";

    public string State
    {
        get => _state;
        set
        {
            if (_state == value) return;
            _state = value;
            foreach (var n in new[] { nameof(State), nameof(Glyph), nameof(IconBrush),
                                      nameof(IsDoing), nameof(IconVisibility),
                                      nameof(RingVisibility), nameof(DotVisibility), nameof(RowBrush) })
                N(n);
        }
    }

    /// <summary>行里的说明文字：没写就显示「还没轮到」那句</summary>
    public string Detail
    {
        get => string.IsNullOrEmpty(_detail) ? Todo : _detail;
        set { _detail = value; N(nameof(Detail)); }
    }

    public bool IsDoing => _state == "doing";

    public string Glyph => _state switch
    {
        "ok" => "\uE73E",       // 对勾
        "warn" => "\uE7BA",     // 感叹号
        _ => "\uE73E",
    };

    public SolidColorBrush IconBrush => new(_state switch
    {
        "ok" => Color.FromArgb(255, 0x4E, 0xD6, 0xA0),
        "warn" => Color.FromArgb(255, 0xFF, 0xC4, 0x6B),
        _ => Color.FromArgb(255, 0x9A, 0xA6, 0xB8),
    });

    /// <summary>正在处理的那一行底色亮一档</summary>
    public SolidColorBrush RowBrush => new(_state == "doing"
        ? Color.FromArgb(0x33, 0xFF, 0xFF, 0xFF)
        : Color.FromArgb(0x1A, 0xFF, 0xFF, 0xFF));

    public Microsoft.UI.Xaml.Visibility IconVisibility => _state is "ok" or "warn"
        ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    public Microsoft.UI.Xaml.Visibility RingVisibility => IsDoing
        ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;

    /// <summary>还没开始：一个空心小圆点</summary>
    public Microsoft.UI.Xaml.Visibility DotVisibility => _state is "ok" or "warn" or "doing"
        ? Microsoft.UI.Xaml.Visibility.Collapsed : Microsoft.UI.Xaml.Visibility.Visible;

    public Microsoft.UI.Xaml.Visibility HasAction => string.IsNullOrEmpty(FixLabel)
        ? Microsoft.UI.Xaml.Visibility.Collapsed : Microsoft.UI.Xaml.Visibility.Visible;
}

/// <summary>「最近完成」里的一行：翻好的成品，点一下就能打开</summary>
public sealed class RecentItem
{
    public string Name { get; init; } = "";
    public string Info { get; init; } = "";
}

/// <summary>主界面左侧列表里的一行（照 DUM 的「我的收藏」那种：缩略图 + 名字 + 两个图标按钮）</summary>
public sealed class VideoItem
{
    public string Name { get; init; } = "";
    public string Info { get; init; } = "";
    public string Status { get; set; } = "";      // "" / running / done / failed

    public string Glyph => Status switch
    {
        "running" => "\uE895",
        "done" => "\uE73E",
        "failed" => "\uE711",
        _ => "\uE768",        // 空闲时显示播放箭头
    };

    public SolidColorBrush IconBrush => Status switch
    {
        "running" => new SolidColorBrush(Color.FromArgb(255, 0x5B, 0x9C, 0xFF)),
        "done" => new SolidColorBrush(Color.FromArgb(255, 0x4E, 0xD6, 0xA0)),
        "failed" => new SolidColorBrush(Color.FromArgb(255, 0xFF, 0x7A, 0x85)),
        _ => new SolidColorBrush(Color.FromArgb(230, 0xFF, 0xFF, 0xFF)),
    };
}

/// <summary>
/// 样机用的假数据。内容照着真程序里会出现的那些行抄，方便判断"信息密度够不够"。
/// 接后端时这个类整个删掉，换成 JSON-RPC 的返回。
/// </summary>
public static class Mock
{
    public const string AppName = "星译";
    public const string Version = "2.2";
    public const string Engine = "finesub 引擎已就绪（C:\\Users\\Star\\.local\\bin\\finesub.EXE）";
    public const string UpdateNotes = "成品默认存到原视频旁边：本地视频（或批量文件夹）翻译完，结果直接建在视频所在目录下的 output 文件夹里";
    public const string OutputDir = @"E:\教程\output\第一课-20261013-104512";
    public const string FinalFile = @"E:\教程\output\第一课-20261013-104512\第一课_final.mp4";

    public static readonly string[] Voices = ["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean", "mimo_default"];
    public static readonly string[] Langs = ["英语", "日语", "韩语", "俄语", "中文"];

    /// <summary>主界面左侧「我的视频」列表（真实程序里是用户最近选过的视频 + 本次待翻译的）</summary>
    public static readonly VideoItem[] Videos =
    [
        new() { Name = "第一课 - 基础操作.mp4", Info = "E:\\教程 · 12:48 · 英语 → 中文" },
        new() { Name = "第二课 - 材质入门.mp4", Info = "E:\\教程 · 21:05 · 英语 → 中文" },
        new() { Name = "第三课 - 灯光布置.mp4", Info = "E:\\教程 · 18:32 · 英语 → 中文" },
        new() { Name = "第四课 - 渲染输出.mp4", Info = "E:\\教程 · 09:17 · 英语 → 中文" },
    ];

    /// <summary>单个视频：开跑后逐条播的假日志（stage, 总进度, 这一行, 级别）</summary>
    public static readonly (string Stage, int Pct, string Text, LogLevel Level)[] SingleScript =
    [
        ("download", 2, "本地视频: 第一课.mp4", LogLevel.Normal),
        ("translate", 8, "输出目录: " + OutputDir, LogLevel.Progress),
        ("translate", 12, "提示：第一次翻译要先下载语音识别模型（约 1.6 GB，视网速可能十几分钟）。这一步引擎自己不报进度，请耐心等待。", LogLevel.Warn),
        ("translate", 20, "[1/4] 语音识别 34% · 15/43", LogLevel.Progress),
        ("translate", 34, "[1/4] 语音识别 100% · 43/43", LogLevel.Progress),
        ("translate", 40, "[2/4] 纠错翻译 18% · 8/43", LogLevel.Progress),
        ("translate", 52, "[2/4] 纠错翻译 100% · 43/43", LogLevel.Progress),
        ("translate", 55, "字幕已生成：第一课.srt（43 条，中文）", LogLevel.Ok),
        ("dub", 62, "开始 MiMo 配音合成...", LogLevel.Normal),
        ("dub", 70, "配音合成 12/43", LogLevel.Progress),
        ("dub", 82, "配音合成 43/43", LogLevel.Progress),
        ("dub", 85, "配音完成：43 句（38 句语速贴合）", LogLevel.Ok),
        ("burn", 92, "字幕样式生成（43 条）", LogLevel.Progress),
        ("burn", 100, "完成: 第一课_final.mp4", LogLevel.Ok),
    ];


    /// <summary>最近翻好的成品（右侧面板空闲时显示，不然右边太空）</summary>
    public static readonly RecentItem[] Recent =
    [
        new() { Name = "第一课 - 基础操作_final.mp4", Info = "12:48 · 8.4 MB · 刚刚" },
        new() { Name = "第二课 - 材质入门_final.mp4", Info = "21:05 · 13.1 MB · 今天 09:12" },
        new() { Name = "UE5 从零做一个小游戏 第3课_final.mp4", Info = "18:32 · 11.6 MB · 昨天" },
    ];

    /// <summary>主页的「开始使用」清单（真实程序里这几项都来自 doctor.py 的检查结果）</summary>
    public static readonly SetupStep[] Setup =
    [
        new() { Title = "翻译引擎", Todo = "finesub 翻译引擎（没装的话会自动装好）" },
        new() { Title = "运行组件", Todo = "ffmpeg / ffprobe / yt-dlp" },
        new() { Title = "API Key", Kind = "manual", Todo = "翻译和配音都要用，去设置里填一下",
                Fix = "settings", FixLabel = "去设置" },
        new() { Title = "网络代理（可选）", Kind = "manual", Todo = "最好全程开启，否则检查更新、下载视频都可能连不上",
                Fix = "settings", FixLabel = "去设置" },
    ];

    /// <summary>环境初始化过没有。样机里存在本地文件里，方便反复看两种启动行为。</summary>
    public static bool IsInitialized
    {
        get => File.Exists(StateFile);
        set
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(StateFile)!);
                if (value) File.WriteAllText(StateFile, "done");
                else if (File.Exists(StateFile)) File.Delete(StateFile);
            }
            catch { }
        }
    }

    static string StateFile => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "StarryVT-WinUI", "initialized.txt");

    /// <summary>
    /// 翻译引擎真装好了没有 —— 决定导航栏「本地翻译 / 在线翻译」灰不灰。
    ///
    /// ⚠️ 和 <see cref="IsInitialized"/> **不是一回事**，别合并：
    ///    那个只表示「跑过一遍初始化向导」，而向导那个循环里任何一步失败都只标 warn、不中断，
    ///    最后照样把标记写上。于是引擎装失败的用户下次启动会被直接送进本地翻译页，
    ///    一点「开始翻译」就报网络错（2026-10-15 客户实例：uv.zip 下载失败 10061）。
    ///    门控要看引擎是不是**真的好了**，不能看向导跑没跑过。
    /// </summary>
    public static bool EngineReady
    {
        get => File.Exists(EngineFile);
        set
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(EngineFile)!);
                if (value) File.WriteAllText(EngineFile, "done");
                else if (File.Exists(EngineFile)) File.Delete(EngineFile);
            }
            catch { }
        }
    }

    static string EngineFile => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "StarryVT-WinUI", "engine-ready.txt");


    /// <summary>在线视频：先下载再翻译（主页面上会多出下载那一段）</summary>
    public static readonly (string Stage, int Pct, string Text, LogLevel Level)[] OnlineScript =
    [
        ("download", 3, "开始下载: https://www.youtube.com/watch?v=dQw4w9WgXcQ", LogLevel.Normal),
        ("download", 12, "[download] 26.4% of 48.21MiB at 3.20MiB/s", LogLevel.Progress),
        ("download", 22, "[download] 100% of 48.21MiB", LogLevel.Progress),
        ("download", 26, "已下载：原始视频.mp4（48.2 MB）", LogLevel.Ok),
        ("translate", 30, "输出目录: " + OutputDir, LogLevel.Progress),
        ("translate", 40, "[1/4] 语音识别 42% · 18/43", LogLevel.Progress),
        ("translate", 52, "[2/4] 纠错翻译 100% · 43/43", LogLevel.Progress),
        ("translate", 55, "字幕已生成：字幕.srt（43 条，中文）", LogLevel.Ok),
        ("dub", 70, "配音合成 22/43", LogLevel.Progress),
        ("dub", 85, "配音完成：43 句（38 句语速贴合）", LogLevel.Ok),
        ("burn", 100, "完成: 视频_final.mp4", LogLevel.Ok),
    ];

    public static readonly QueueItem[] BatchSeed =
    [        new() { Title = "第一课 - 基础操作.mp4", Status = "done", Detail = "第一课 - 基础操作_final.mp4" },
        new() { Title = "第二课 - 材质入门.mp4", Status = "running", Detail = "配音合成 20/56" },
        new() { Title = "第三课 - 灯光布置.mp4", Status = "pending" },
        new() { Title = "第四课 - 渲染输出.mp4", Status = "pending" },
        new() { Title = "第五课 - 案例实战.mp4", Status = "failed", Detail = "连接超时（已重试 3 次）" },
        new() { Title = "第六课 - 常见问题.mp4", Status = "pending" },
    ];

    public static readonly EnvItem[] Env =
    [
        new() { Title = "翻译引擎", State = "ok", Detail = "finesub 0.4.0 已就绪（C:\\Users\\Star\\.local\\bin\\finesub.EXE）" },
        new() { Title = "随包运行组件", State = "ok", Detail = "ffmpeg / ffprobe / yt-dlp 都已就位" },
        new() { Title = "API Key", State = "ok", Detail = "DeepSeek、MiMo 都已配置" },
        new() { Title = "引擎数据目录", State = "warn", Fix = "settings", FixLabel = "去设置",
                Detail = "系统默认位置装不了引擎运行环境（os error 448 不受信任的装入点），已自动改用：E:\\StarryVT-FineSub" },
        new() { Title = "下载器（yt-dlp）", State = "warn", Fix = "downloader", FixLabel = "更新下载器",
                Detail = "当前 2026.08.19，官方已有 2026.10.08（稳定版）。下不了 YouTube 视频时先点右边这个按钮。" },
        new() { Title = "网络代理", State = "warn", Fix = "settings", FixLabel = "去设置",
                Detail = "要下载在线视频通常需要代理；只翻译本地文件的话可以不管它。" },
        new() { Title = "输出目录", State = "ok", Detail = "E:\\教程\\output（原视频旁边的 output 文件夹）" },
        new() { Title = "磁盘空间", State = "ok", Detail = "E:\\ 剩余 312.6 GB" },
        new() { Title = "外网依赖", State = "ok", Detail = "加速镜像可用：https://gh-proxy.com/" },
    ];

    public static ObservableCollection<LogLine> SeedLog() =>
    [
        new() { Text = "准备就绪。选好视频、语言和音色，点右下角「开始翻译」。", Level = LogLevel.Dim },
    ];
}

/// <summary>
/// 演示用的假后端：按时间轴把 <see cref="Mock.SingleScript"/> 一行行播出来，同时推进进度条。
///
/// ⚠️ 正式流程已经走 Services.RealRunner（真后端）了 —— 这个类只是「没接后端时看一眼界面
/// 长什么样」的样机，**别在正式路径上用它**：它演出来的「完成」是假的。
/// </summary>
public sealed class MockRunner : IRunner
{
    readonly DispatcherQueueTimer _timer;
    readonly IReadOnlyList<(string Stage, int Pct, string Text, LogLevel Level)> _script;
    readonly string _resultPath;
    int _i;

    public event Action<string, int, string>? Progress;
    public event Action<LogLine>? Line;
    public event Action? Finished;
    public event Action<IReadOnlyList<QueueItem>>? Queue;
    public bool Running { get; private set; }
    public string ResultPath => _resultPath;
    public string ResultDir => "";
    public string ResultNote => "";
    public string EndStage => "done";

    public MockRunner(IReadOnlyList<(string Stage, int Pct, string Text, LogLevel Level)> script,
                      string resultPath = "")
    {
        _script = script;
        _resultPath = resultPath;
        _timer = DispatcherQueue.GetForCurrentThread().CreateTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(700);
        _timer.Tick += (_, _) => Step();
    }

    public void Start()
    {
        if (Running) return;
        Running = true;
        _i = 0;
        _timer.Start();
        Step();               // 立刻走第一条，别等第一个 tick
    }

    public void Cancel()
    {
        _timer.Stop();
        if (!Running) return;
        Running = false;
        Line?.Invoke(new LogLine { Text = "⏹ 已取消", Level = LogLevel.Warn });
        Finished?.Invoke();
    }

    void Step()
    {
        if (_i >= _script.Count)
        {
            _timer.Stop();
            Running = false;
            Finished?.Invoke();
            return;
        }
        var s = _script[_i++];
        Progress?.Invoke(s.Stage, s.Pct, s.Text);
        Line?.Invoke(new LogLine { Text = s.Text, Level = s.Level });
    }
}
