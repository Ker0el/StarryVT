using System.Runtime.InteropServices;
using System.Text.Json;
using System.Globalization;
using Microsoft.UI;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Microsoft.UI.Xaml.Shapes;
using Windows.Foundation;
using Windows.UI;

using Starry.Vt.App.Fakes;
using Starry.Vt.App.Services;

namespace Starry.Vt.App.Pages;

/// <summary>
/// 设置页。左边是说明、右边是控件，改完**失焦就自动存**（没有「保存」按钮）。
/// 配置的真身在 %APPDATA%\StarryVT\settings.json，这里只是它的一个界面（存/读都走后端）。
/// </summary>
public sealed partial class SettingsPage : Page
{
    bool _ready;
    bool _filling;                 // 回填配置期间别把「程序填进去的值」又存一遍
    bool _lastPointerWasBlank;     // 这次鼠标点击是不是落在 ScrollView 的空白处
    string _homeSaved = "";        // 当前生效的「引擎数据目录」，改之前拿它比
    string _homeDefault = "";      // 引擎数据目录的默认位置（后端算好的，给提示用）
    string _dataDefault = "";      // 「数据目录」的默认位置（后端算好的，给提示用）
    string _subFontName = "Starry Han Sans Light";   // 字幕字体族名（ASS 里用的那个）
    string _subFontFile = "";                        // 自定义字体文件；空 = 随包的思源黑体
    Flyout? _paletteFlyout;
    ColorPicker? _picker;
    DateTime _lastAccentAt = DateTime.MinValue;   // 调色板拖动时的节流（见 ColorChanged）
    string _pendingAccent = "";                   // 还没落定的颜色（松手时补一次）
    DispatcherQueueTimer? _saved;  // 「已保存 ✓」几秒后自己变回去

    // ---- API Key 的自动检测（用户 2026-10-15：不要「测试」按钮）----
    // 两条触发路径：
    //   · 打字/粘贴之后**停手 2.5 秒** → 自己测（填完就撂下的人）
    //   · 点到别处（失焦）→ 立刻存 + 测（填完就点走的人）
    // 同一个 key 只测一次：值没变就不重复发请求；测失败就把记录清掉，下次还会再试
    //（网络抖一下不算数，别让用户以为 key 坏了）。
    DispatcherQueueTimer? _keyDebounce;
    readonly Dictionary<string, string> _keyTested = new();
    readonly Dictionary<string, string> _links = [];

    /// <summary>设置内容最多 1120 宽，靠左；窗口更窄时跟着缩（不要居中）</summary>
    void FitContentWidth()
    {
        double w = PageScroll.ActualWidth;
        if (w <= 0) return;
        SettingsContent.Width = Math.Min(1120, Math.Max(320, w - 8));
    }

    /// <summary>
    /// 滚到设置页的某个分区（首页自检 / 弹框里的「去设置」用）。
    ///
    /// 2026-10-23 用户报障：点「去设置」只切页、不定位 —— 页面又缓存了上次的
    /// 滚动位置，所以有人看到的是最下面、有人看到最上面，还得自己滑。
    /// section 用 doctor 的 item id（keys / data_dir / output …），认不出来就
    /// 保持当前位置（至少不会乱跳）。
    /// </summary>
    public void ScrollToSection(string section)
    {
        var key = (section ?? "").Trim().ToLowerInvariant();
        FrameworkElement? target = key switch
        {
            "keys" or "api" or "net" => KeysCard,
            "translate" or "voice" or "clone" or "llm_route" => TranslateCard,
            "subtitle" or "subtitle_style" => SubtitleCard,
            "output" => OutputCard,
            "data_dir" or "engine" or "models" or "disk" => EngineHomeRow,
            "data" or "data_home" => DataHomeRow,
            "proxy" or "github" or "network" or "mirror" => ProxyRow,
            "downloader" or "update" => UpdateRow,
            "appearance" or "wall" or "wallpaper" => AppearanceCard,
            "install_dir" or "hardware" or "tools" => TroubleshootCard,
            _ => null,
        };
        if (target is null) return;

        // 「高级」里的行折叠时 BringIntoView 只会停在卡片头 —— 先展开，再等一帧
        // 让它完成布局，否则滚动请求会被丢掉。别的分区不动这个折叠状态。
        if (ReferenceEquals(target, ProxyRow) || ReferenceEquals(target, UpdateRow))
            AdvancedExpander.IsExpanded = true;
        DispatcherQueue.TryEnqueue(DispatcherQueuePriority.Low, () =>
        {
            try
            {
                target.StartBringIntoView(new BringIntoViewOptions
                {
                    AnimationDesired = true,
                    VerticalAlignmentRatio = 0.08,
                    HorizontalAlignmentRatio = 0.0,
                });
                if (key is "keys" or "api" or "net")
                    (DeepBox.Password.Length == 0 ? DeepBox : MimoBox).Focus(FocusState.Programmatic);
            }
            catch (Exception e) { App.Dump("滚动到设置分区失败: " + e.Message); }
        });
    }

    public SettingsPage()
    {
        InitializeComponent();
        // 设置内容固定 1120 宽、**靠左**：窗口更宽时不要把整页居中（用户 2026-10-21 定）
        PageScroll.SizeChanged += (_, _) => FitContentWidth();
        FitContentWidth();
        // 点空白时别让 WinUI 把焦点丢给 ScrollView 里第一个可聚焦控件（会把页面拽回顶部）。
        // 在 PageScroll 上监听（handledEventsToo），空白处就聚焦旁边的 FocusSink。
        PageScroll.AddHandler(UIElement.PointerPressedEvent,
            new PointerEventHandler(PageScroll_PointerPressed), true);
        AddHandler(UIElement.GettingFocusEvent,
            new TypedEventHandler<UIElement, GettingFocusEventArgs>(Page_GettingFocus), true);
        // 页面实例要**缓存复用**：不然每次切页都是新实例，翻译中的日志 / 队列 / 进度
        // 一离开就全没了（用户 2026-10-14 实测：翻译中切到设置再回来，结果没了）
        NavigationCacheMode = Microsoft.UI.Xaml.Navigation.NavigationCacheMode.Required;
        // 回填当前亮度（滑杆原来只有拖动才生效，看着像坏的）
        int v = (int)Math.Round(UiState.BgOpacity * 100);
        BgSlider.Value = v;
        BgText.Text = v + "%";
        BuildAccentRow();

        // 自绘下拉：显示的字 / 给后端用的值
        // 实测云端 4 路并发的克隆明显快于本机；原来写「本机速度快」是错的。
        // 本机赢在免费、离线、不受限。
        ModeBox.SetItems([("标准模式（推荐，Deepseek精翻+Mimo配音，速度慢）", "standard"),
                          ("快速模式（全流程免费，Google 机翻 + 微软配音，速度快）", "fast")]);
        ModeBox.Changed += idx => { _ = OnModeChanged(); };
        CloneEngineBox.SetItems([("云端 MiMo（更快；需联网、按量计费，可能限流）", "cloud"),
                                 ("Index语音克隆（免费、本地、离线；显卡越强越快）", "local"),
                                 ("Qwen语音克隆（免费、本地、离线；显卡越强越快）", "qwen")]);
        CloneEngineBox.Changed += idx => { _ = OnCloneEngineChanged(); };

        // 双语字幕：原文那一行放上面还是下面
        SubBilingualOrderBox.SetItems([("原文在上（推荐）", "src_top"),
                                       ("原文在下", "src_bottom")]);
        SubBilingualOrderBox.Changed += idx => { _ = SubBilingualOrderChangedAsync(); };

        // 工作页的默认值：这里改的是「默认」，单个任务里还能临时改
        DefaultOutputBox.SetItems([("出完整成片", "final"), ("只加字幕（保留原声）", "subs-burn"),
                                   ("只出配音", "dub"), ("只出字幕", "subs"),
                                   ("只提取原文字幕（不翻译）", "subs-src"),
                                   ("下载视频", "download")]);
        DefaultOutputBox.Changed += idx => { _ = SaveAsync(new Dictionary<string, object?>
        {
            ["default_output"] = DefaultOutputBox.SelectedTag is { Length: > 0 } t ? t : "final",
        }); };

        LangBox.SetItems([("英语 → 中文", "en"), ("日语 → 中文", "ja"),
                          ("韩语 → 中文", "ko"), ("俄语 → 中文", "ru"),
                          ("德语 → 中文", "de"),
                          ("中文 → 英语", "zh-en"),
                          ("中文（换音色重配）", "zh")]);
        LangBox.Changed += idx => { _ = SaveAsync(new Dictionary<string, object?>
        {
            ["source_lang"] = LangBox.SelectedTag is { Length: > 0 } t ? t : "en",
        }); UpdateLangHint(); };

        VoiceBox.SetItems(Mock.Voices.Select(v => (v, v)));
        VoiceBox.Changed += idx => { _ = OnVoiceChanged(); };

        SpeedBox.SetItems([("标准", "auto"), ("+5%", "5"), ("+10%", "10")]);
        SpeedBox.Changed += idx => { _ = SaveAsync(new Dictionary<string, object?>
        {
            ["tts_speed"] = SpeedBox.SelectedTag is { Length: > 0 } t ? t : "auto",
        }); };

        SubsBox.SetItems([("优先用创作者字幕", "manual"),
                          ("字幕优先，自动字幕也认", "auto"),
                          ("不用字幕，一律语音识别", "off")]);
        SubsBox.Changed += idx => { _ = SaveAsync(new Dictionary<string, object?>
        {
            ["yt_subs"] = SubsBox.SelectedTag is { Length: > 0 } t ? t : "manual",
        }); };
        LoadWallpaper();          // 壁纸那行（动图/静态、换一张、钉住）——不用等后端
        _ready = true;
        Loaded += async (_, _) => await LoadAsync();
    }

    /// <summary>
    /// 空白点击的焦点接收：点到真正的可聚焦控件时不动；点到空白就聚焦 FocusSink，
    /// 避免 WinUI 把焦点丢给 ScrollView 里第一个 TextBox（页面会滚回顶部）。
    /// </summary>
    void PageScroll_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _lastPointerWasBlank = false;
        if (e.Pointer.PointerDeviceType != Microsoft.UI.Input.PointerDeviceType.Mouse) return;
        if (e.OriginalSource is not DependencyObject src) return;
        if (!IsInside(src, PageScroll)) return;
        for (DependencyObject? d = src; d is not null && !ReferenceEquals(d, PageScroll);
             d = VisualTreeHelper.GetParent(d))
        {
            if (d is Control c && c.IsTabStop) return;   // 点到输入框/按钮/下拉，不抢
        }
        _lastPointerWasBlank = true;
        e.Handled = true;      // 别让 ScrollView 自己再把焦点交给第一个子控件
    }

    /// <summary>
    /// 空白点击后 WinUI 仍会尝试把焦点给第一个可聚焦控件（顶部 API Key）。
    /// 在 GettingFocus 里改送到 FocusSink；FocusSink 不在 ScrollView 里，页面不会滚。
    /// </summary>
    void Page_GettingFocus(UIElement sender, GettingFocusEventArgs args)
    {
        if (!_lastPointerWasBlank) return;
        if (args.InputDevice != FocusInputDeviceKind.Mouse)
        {
            _lastPointerWasBlank = false;
            return;
        }
        if (args.NewFocusedElement is not DependencyObject nf || !IsInside(nf, PageScroll))
        {
            _lastPointerWasBlank = false;
            return;
        }
        _lastPointerWasBlank = false;
        if (!args.TrySetNewFocusedElement(FocusSink)) args.TryCancel();
    }

    static bool IsInside(DependencyObject? d, DependencyObject root)
    {
        for (; d is not null; d = VisualTreeHelper.GetParent(d))
            if (ReferenceEquals(d, root)) return true;
        return false;
    }

    void BgSlider_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (!_ready) return;
        double v = e.NewValue / 100.0;
        BgText.Text = $"{(int)e.NewValue}%";
        MainWindow.Instance?.SetBackgroundOpacity(v);
        UiState.BgOpacity = v;   // 下次启动还用这个亮度
    }

    // ============================================================ 读 / 存配置

    async Task LoadAsync()
    {
        if (!Backend.Start())
        {
            SaveState.Text = "没连上后端：" + Backend.LastError;
            return;
        }
        var r = await Backend.CallAsync("boot", null, 30_000);
        if (r is null || !r.Value.TryGetProperty("settings", out var s))
        {
            SaveState.Text = "读设置失败：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应");
            return;
        }

        _filling = true;
        if (s.TryGetProperty("keys_plain", out var kp))
        {
            MimoBox.Password = Str(kp, "mimo");
            DeepBox.Password = Str(kp, "deepseek");
        }
        OutDirBox.Text = Str(s, "output_dir");
        TranslateNamesCheck.IsChecked = Flag(s, "translate_names", true);
        AutoOpenCheck.IsChecked = Flag(s, "auto_open", false);
        LearnTermsCheck.IsChecked = Flag(s, "learn_terms", false);
        DeepSeekSearchCheck.IsChecked = Flag(s, "deepseek_web_search", false);
        ProxyBox.Text = Str(s, "proxy");
        MirrorBox.Text = Str(s, "github_mirror");
        FinesubBox.Text = Str(s, "finesub_exe");
        HomeBox.Text = Str(s, "finesub_home");
        _homeSaved = HomeBox.Text;          // 记着当前生效的那个 —— 改之前要拿它比（见 ConfirmHomeChangeAsync）
        _homeDefault = Str(s, "finesub_home_default");
        var homeActual = Str(s, "finesub_home_actual");
        if (homeActual.Length > 0)
            HomeHint.Text = "当前：" + homeActual
                          + (HomeBox.Text.Trim().Length > 0 ? "" : "（默认）");
        DataHomeBox.Text = Str(s, "data_home");
        _dataDefault = Str(s, "data_default_root");
        var dataRootNow = Str(s, "data_root");
        if (dataRootNow.Length > 0)
            DataHomeHint.Text = "当前：" + dataRootNow + (DataHomeBox.Text.Trim().Length > 0 ? "" : "（默认）");
        _filling = false;

        if (r.Value.TryGetProperty("links", out var links))
            foreach (var name in new[] { "mimo", "deepseek" })
            {
                var url = Str(links, name);
                if (url.Length > 0) _links[name] = url;
            }

        var ver = Str(r.Value, "app_version");
        UpdateText.Text = $"当前版本 v{ver}";
        // 产品名 / 标语 / 赞赏码 / 微信号那一整块搬去了「关于」页（用户 2026-10-14）

        SetHint(MimoHint, MimoBox.Password.Length > 0 ? "已填写" : "还没填 —— 配音要用", null);
        SetHint(DeepHint, DeepBox.Password.Length > 0 ? "已填写" : "还没填 —— 翻译要用", null);

        // 填过的 key 顺手测一次连通性（没填就不必白等一次超时）—— 老界面也是这么做的
        if (MimoBox.Password.Length > 0) await TestKeyAsync("mimo", MimoBox.Password.Trim(), MimoHint);
        if (DeepBox.Password.Length > 0)
        {
            await TestKeyAsync("deepseek", DeepBox.Password.Trim(), DeepHint);
            await RefreshDeepSeekBalanceAsync();
        }

        LoadDub(s);
        await RefreshCloneAsync();
        await RefreshQwenAsync();
    }

    // ============================================================ 配音

    static double Num(JsonElement el, string name, double fallback) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetDouble(out var d)
            ? d : fallback;

    static bool Flag(JsonElement el, string name, bool fallback) =>
        el.TryGetProperty(name, out var v) && v.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? v.GetBoolean() : fallback;

    void LoadDub(JsonElement s)
    {
        bool was = _filling;
        _filling = true;
        DefaultOutputBox.SelectTag(Str(s, "default_output") is { Length: > 0 } outMode ? outMode : "final",
                                   notify: false);
        ModeBox.SelectTag(Str(s, "mode") is { Length: > 0 } mode ? mode : "standard", notify: false);
        _voiceStd = Str(s, "voice") is { Length: > 0 } v0 ? v0 : "冰糖";
        _edgeVoice = Str(s, "edge_voice") is { Length: > 0 } ev ? ev : "zh-CN-XiaoxiaoNeural";
        _cloneEngineStd = Str(s, "clone_engine") == "cloud" ? "cloud" : "local";
        int ov = (int)Math.Round(Num(s, "orig_vol", 0.0) * 100);
        OrigSlider.Value = ov;
        OrigText.Text = ov + "%";
        StyleBox.Text = Str(s, "tts_style");
        CloneCheck.IsChecked = Flag(s, "tts_clone", false);
        SubtitleReviewCheck.IsChecked = Flag(s, "subtitle_review", false);
        // 设置页可能比主窗口的启动检查先读到配置，这里也同步一次导航栏
        MainWindow.Instance?.SetSubtitleNavVisible(SubtitleReviewCheck.IsChecked == true);
        UpdateVoiceEnabled();
        CloneEngineBox.Select(Str(s, "clone_engine") == "cloud" ? 0 : 1, notify: false);
        SubsBox.SelectTag(Str(s, "yt_subs") is { Length: > 0 } subs ? subs : "manual", notify: false);
        LangBox.SelectTag(Str(s, "source_lang") is { Length: > 0 } lang ? lang : "en", notify: false);
        UpdateLangHint();
        VoiceBox.SelectTag(Str(s, "voice") is { Length: > 0 } voice ? voice : "冰糖", notify: false);
        SpeedBox.SelectTag(Str(s, "tts_speed") is { Length: > 0 } spd ? spd : "auto", notify: false);
        SubSizeSlider.Value = Num(s, "subtitle_size", 42);
        SubSpacingSlider.Value = Num(s, "subtitle_spacing", 0);
        SubOutlineSlider.Value = Num(s, "subtitle_outline", 2.2);
        SubMarginSlider.Value = Num(s, "subtitle_margin_v", 30);
        SubSizeText.Text = FormatSize(SubSizeSlider.Value);
        SubSpacingText.Text = FormatSpacing(SubSpacingSlider.Value);
        SubOutlineText.Text = SubOutlineSlider.Value.ToString("0.0");
        SubMarginText.Text = ((int)SubMarginSlider.Value) + " px";
        SubBilingualCheck.IsChecked = Flag(s, "subtitle_bilingual", false);
        SubBilingualScaleSlider.Value = Num(s, "subtitle_bilingual_scale", 80);
        SubBilingualScaleText.Text = ((int)SubBilingualScaleSlider.Value) + "%";
        SubBilingualOrderBox.SelectTag(
            Str(s, "subtitle_bilingual_order") is { Length: > 0 } bo ? bo : "src_top", notify: false);
        UpdateSubBilingualUI();
        _subFontName = Str(s, "subtitle_font") is { Length: > 0 } fn ? fn : "Starry Han Sans Light";
        _subFontFile = Str(s, "subtitle_font_file");
        UpdateSubFontUI();
        var outlineColor = Str(s, "subtitle_outline_color");
        SubOutlineColorPicker.Color = ParseHexColor(outlineColor.Length == 0 ? "#000000" : outlineColor);
        UpdateSubOutlineUI(outlineColor.Length == 0 ? "#000000" : outlineColor);
        var subColor = Str(s, "subtitle_color") is { Length: > 0 } cc ? cc : "#FFFFFF";
        SubColorPicker.Color = ParseHexColor(subColor);
        UpdateSubColorUI(subColor);
        ApplyModeUI(ModeBox.SelectedTag is { Length: > 0 } mm ? mm : "standard");
        _filling = was;
        ScheduleSubPreview();
    }

    /// <summary>输出目录和成片文件名用翻译后的名字（原文件不改）。</summary>
    async void TranslateNames_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        await SaveAsync(new Dictionary<string, object?>
        {
            ["translate_names"] = TranslateNamesCheck.IsChecked == true,
        });
    }

    /// <summary>字幕检查页开关：勾上后左侧导航栏出现「字幕检查」。</summary>
    async void SubtitleReview_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        await SaveAsync(new Dictionary<string, object?>
        {
            ["subtitle_review"] = SubtitleReviewCheck.IsChecked == true,
        });
    }

    /// <summary>翻完自动打开成片（单个视频）——两个工作页在开跑时读这个值</summary>
    async void AutoOpen_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        await SaveAsync(new Dictionary<string, object?> { ["auto_open"] = AutoOpenCheck.IsChecked == true });
    }

    /// <summary>术语学习：翻译时自动把新学到的术语写回词条库（越用越准）。
    /// 关掉 = 引擎只读词条库、不写回（见 builtin_kb.py / pipeline 的 --knowledge）。</summary>
    async void LearnTerms_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        await SaveAsync(new Dictionary<string, object?> { ["learn_terms"] = LearnTermsCheck.IsChecked == true });
    }

    /// <summary>DeepSeek 联网查证：翻译前搜一次，把摘要并进 extra_info。
    /// 和引擎自带的 web_search（Google/Gemini native retrieval）是两个独立开关。</summary>
    async void DeepSeekSearch_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        await SaveAsync(new Dictionary<string, object?>
        {
            ["deepseek_web_search"] = DeepSeekSearchCheck.IsChecked == true,
        });
    }

    /// <summary>弹「术语库」浮层（看有什么 / 清空）。内容由浮层每次现取。</summary>
    void ViewKnowledge_Click(object sender, RoutedEventArgs e)
        => MainWindow.Instance?.ShowKnowledge();

    // ---------- 背景壁纸（动图 / 静态图，可钉住某一张） ----------

    void LoadWallpaper()
    {
        WallBox.SetItems([("视频（每次启动换一条）", "video"), ("静态图（每次启动换一张）", "image")]);
        WallBox.SelectTag(UiState.Wallpaper, notify: false);
        WallBox.Changed += _ => WallModeChanged();
        UpdateWallRow();
        RefreshSavedGallery();
        _ = EnsureThumbsAsync();     // 默认就是展开的，加载时就得补预览图（见该方法注释）
    }

    void WallModeChanged()
    {
        if (!_ready || _filling) return;
        UiState.Wallpaper = WallBox.SelectedTag is { Length: > 0 } t ? t : "video";
        UiState.WallpaperPin = "";                       // 换类型了，钉住的那条（多半是另一种）就不留了
        MainWindow.Instance?.ApplyWallpaper();
        UpdateWallRow();
        RefreshSavedGallery();
    }

    /// <summary>换一张：**从图源现取一条**（不碰收藏库，也不从本地缓存里挑）</summary>
    async void WallNext_Click(object sender, RoutedEventArgs e)
    {
        WallNextBtn.IsEnabled = false;
        WallNextBtn.Content = "换一张中…";

        // 备用池里没有时要现下，视频一条 15~20 MB、得十几秒。**必须让它肉眼可见在动** ——
        // 不然按钮一直写着"换一张中…"、画面又没变，就是"点了没换、也不知道是不是延迟"
        // （用户 2026-10-14 原话）。走秒 + 文案把人话说明白。
        var t0 = DateTime.Now;
        // ⚠️ 用 Motion.Repeat，别自己 new 一个局部计时器 —— 局部变量会被 GC 掉，
        //    计时器再也不 Tick，走秒就永远停在"换一张中…"（不报错）。停的时候配 Motion.Stop。
        var tick = Motion.Repeat(DispatcherQueue, 400, () =>
        {
            var s = (int)(DateTime.Now - t0).TotalSeconds;
            if (s >= 3) WallNextBtn.Content = $"正在下载… {s}s";
        });

        try
        {
            // 「换一张」= 回到随机。收藏着的时候得先把收藏撤掉 ——
            // RefreshWallpaperAsync 开头就是"有收藏就别动"，不撤的话这里直接空转返回，
            // 按了跟没按一样（提示语明明写着"点换一张回到随机"，用户 2026-10-14 报了）。
            if (UiState.WallpaperPin.Length > 0) UiState.WallpaperPin = "";
            // ★ 点了这一下就表示「我要随机的」，从此不再回随包那条默认视频
            //   （否则 ApplyWallpaper 每次启动又会把默认视频铺回来，"换一张"等于白按）。
            UiState.WallpaperRandom = true;

            // 等**真的换完**再复原，别按固定秒数猜
            var ok = await (MainWindow.Instance?.SwitchWallpaperNow() ?? Task.FromResult(false));
            if (!ok) SetHint(WallHint, "图源没给，再点一次试试", false);
        }
        finally
        {
            Motion.Stop(tick);
            WallNextBtn.Content = "换一张";
            WallNextBtn.IsEnabled = true;      // 它永远是"回到随机"的出口，不该被禁用
            UpdateWallRow();
            RefreshSavedGallery();
        }
    }

    /// <summary>
    /// 收藏这张 = 把**正在播的那份**拷进收藏库。**只做这一件事**：
    /// 之前这里还兼着"取消收藏"，一个按钮两套逻辑，用户看了直说"不合理"（2026-10-14）。
    /// 存过同样的就直接提示，也不重复存。
    /// </summary>
    void WallPin_Click(object sender, RoutedEventArgs e) => DoPin();

    /// <summary>
    /// 收藏这张的实际动作。**特意从点击处理器里拆出来**，好让 --probe=pinsave 走**同一条路**：
    /// 探针要是直接调 PinCurrentWallpaper，就绕过了「重铺收藏条 + 补预览图」这两步，
    /// 这条链上的 bug 永远测不出来（2026-10-15 就是因为这个漏掉了收藏后没缩略图）。
    /// </summary>
    void DoPin()
    {
        var cur = MainWindow.Instance?.CurrentWallpaper ?? "";
        if (cur.Length == 0)
        {
            SetHint(WallHint, "这张还没落地（可能正在下载）—— 等画面出来再点", false);
            return;
        }
        if (Services.Wallpaper.IsSaved(cur))
        {
            SetHint(WallHint, "这张已经在收藏里了", false);
            return;
        }
        if (MainWindow.Instance?.PinCurrentWallpaper() != true)
        {
            SetHint(WallHint, "没能存下来，稍后再试", false);
            return;
        }
        UpdateWallRow();
        RefreshSavedGallery();
        // ⚠️ 收藏完**必须**补一次预览图：RefreshSavedGallery 只是同步铺一遍
        //    「磁盘上已经有图」的那些格子，而刚收藏的视频这时候还没抽过帧 ——
        //    不补的话新收的那条就是一格空白，非得把展开器收起来再展开才出现。
        //    （用户 2026-10-15 报「点收藏壁纸又没缩略图」。）
        _ = EnsureThumbsAsync();
    }

    /// <summary>
    /// 画「已收藏的壁纸」那排预览图（生长菜单展开时看的就是它）。
    /// 预览图是拿随包 ffmpeg 抽第一帧 —— 抽图要几百毫秒，所以**展开时才抽**（见 WallExpander_Expanding）。
    /// </summary>
    void RefreshSavedGallery()
    {
        var saved = Services.Wallpaper.Saved();
        WallExpanderHeader.Text = saved.Count == 0 ? "已收藏的壁纸（还没有）" : $"已收藏的壁纸（{saved.Count}）";
        WallStrip.Children.Clear();

        if (saved.Count == 0)
        {
            WallStrip.Children.Add(new TextBlock
            {
                Text = "看到喜欢的，点上面「收藏这张」就收进来了",
                FontSize = 12.5,
                Opacity = 0.6,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(6, 0, 0, 0),
            });
            return;
        }
        foreach (var f in saved) WallStrip.Children.Add(BuildThumb(f));
    }

    /// <summary>一条收藏：预览图 + 底下一行名字，点一下就用它</summary>
    FrameworkElement BuildThumb(string path)
    {
        var brush = new ImageBrush { Stretch = Stretch.UniformToFill };
        // 已经有抽好的预览图就直接挂上（展开时那一轮是把缺的补上）
        // ⚠️ 收藏的**本身就是图片**时，它自己就是预览图，直接挂 —— 别傻等 ffmpeg。
        //    之前只认 `<文件>.thumb.jpg`，图片收藏就永远是个空框（用户 2026-10-14 报过）。
        var thumb = path + ".thumb.jpg";
        if (!File.Exists(thumb) && Services.Wallpaper.IsImagePath(path)) thumb = path;
        if (File.Exists(thumb))
        {
            try
            {
                brush.ImageSource = new BitmapImage
                {
                    CreateOptions = BitmapCreateOptions.IgnoreImageCache,
                    UriSource = new Uri(thumb),
                };
            }
            catch { }
        }
        // 外框用 Border：圆角、底色、以及"正在用的那条"的高亮边框都靠它
        // （原来是 Grid 叠一层 Border + Rectangle，可 Grid 没有 BorderBrush，做不了高亮）
        var frame = new Border
        {
            Width = 148,
            Height = 84,
            CornerRadius = new CornerRadius(6),
            BorderThickness = new Thickness(2),
            Background = (Brush)Application.Current.Resources["PanelSubtleBrush"],
            Child = new Rectangle { RadiusX = 4, RadiusY = 4, Fill = brush },
        };

        var lbl = new TextBlock
        {
            Text = Services.Wallpaper.Label(path),
            FontSize = 12,
            Opacity = 0.75,
            HorizontalAlignment = HorizontalAlignment.Center,
            Margin = new Thickness(0, 4, 0, 0),
        };
        var stack = new StackPanel { Spacing = 0 };
        stack.Children.Add(frame);
        stack.Children.Add(lbl);

        var btn = new Button
        {
            Padding = new Thickness(4),
            BorderThickness = new Thickness(0),
            Background = new SolidColorBrush(Colors.Transparent),
            Content = stack,
            Tag = (path, brush),                 // 抽图时要用：路径 + 要塞图的刷子
        };
        ToolTipService.SetToolTip(btn, "点一下换成这条；右键直接删掉");

        // 右键 → 删掉这条收藏
        var menu = new MenuFlyout();
        var del = new MenuFlyoutItem { Text = "删掉这条收藏" };
        del.Click += (_, _) => DeleteSavedWallpaper(path);
        menu.Items.Add(del);
        btn.ContextFlyout = menu;

        // 正在用的那条：加个高亮边框，一眼看出在用哪张
        if (string.Equals(UiState.WallpaperPin, path, StringComparison.OrdinalIgnoreCase))
            frame.BorderBrush = (Brush)Application.Current.Resources["AccentFillColorDefaultBrush"];

        btn.Click += (_, _) => { MainWindow.Instance?.UseSavedWallpaper(path); UpdateWallRow(); RefreshSavedGallery(); };
        return btn;
    }

    /// <summary>
    /// 右键删一条收藏 —— **直接删，不弹确认框**（用户 2026-10-14 定：右键本来就带意图，
    /// 再问一句反而烦；真删错了重新收藏一次就行，代价很低）。
    /// 删的正好是正在用的那条就回到随机。
    /// </summary>
    void DeleteSavedWallpaper(string path)
    {
        bool wasCurrent = string.Equals(UiState.WallpaperPin, path, StringComparison.OrdinalIgnoreCase);
        Services.Wallpaper.DeleteSaved(path);
        if (wasCurrent) MainWindow.Instance?.UnpinWallpaper();
        RefreshSavedGallery();
        UpdateWallRow();
    }

    async void WallExpander_Expanding(Expander sender, ExpanderExpandingEventArgs args)
    {
        // ⚠️ async void 的事件处理器**必须自己吞异常**：逃出去没人接，整个进程直接消失
        //    （2026-10-14 实测：点设置就崩）。
        try { await EnsureThumbsAsync(); }
        catch (Exception ex) { App.Dump("补预览图失败: " + ex); }
    }

    /// <summary>
    /// 给还缺预览图的收藏补一张（一次一条，别一下起一堆 ffmpeg）。
    /// ⚠️ 除了"展开"事件，**页面加载时也要叫一次**：展开器现在默认就是展开的
    /// （用户 2026-10-14 要求），而 XAML 里写死的 `IsExpanded="True"` **不会**触发
    /// Expanding 事件 —— 只挂事件的话，视频收藏永远没有预览图。
    /// </summary>
    async Task EnsureThumbsAsync()
    {
        // ⚠️⚠️ `IsExpanded="True"` 会让 Expanding 在 **InitializeComponent() 过程中**就触发，
        //     那时展开器里面的 WallStrip 还没被赋值（XAML 是自上而下建的）→ 空引用。
        //     用户 2026-10-14 点设置就是这么崩的。这里按"控件在不在"挡一道，
        //     别用 `_ready` —— 构造函数里的 LoadWallpaper() 跑在 `_ready = true` 之前，
        //     用它当条件会把"加载时补预览图"那条路一起挡掉。
        if (WallStrip is null) return;

        int made = 0;
        foreach (var child in WallStrip.Children.OfType<FrameworkElement>())
        {
            if (child.Tag is not ValueTuple<string, ImageBrush> tag) continue;
            var (path, brush) = tag;
            if (brush.ImageSource is not null) continue;          // 已经有了
            var t = await Services.Ffmpeg.ThumbAsync(path);
            if (t.Length == 0) continue;
            try { brush.ImageSource = new BitmapImage(new Uri(t)); made++; } catch { }
        }
        App.Dump($"[wall] 预览图补齐 {made} 张");
    }

    /// <summary>
    /// 文案只说两件事：现在是"随机的"还是"你挑的那条"。
    /// 「收藏这张」永远只管收藏，「换一张」永远只管换 —— 不做双关按钮。
    /// </summary>
    void UpdateWallRow()
    {
        var pick = UiState.WallpaperPin;
        WallPinBtn.Content = "收藏这张";
        WallNextBtn.Content = "换一张";
        WallHint.Text = pick.Length > 0
            ? $"正在用收藏里的「{Services.Wallpaper.Label(pick)}」（点「换一张」回到随机）"
            : "每次启动换一条新的；看到喜欢的点「收藏这张」";
    }

    static string ShortUrl(string url)
    {
        try { return new Uri(url).Host; }
        catch { return url.Length > 30 ? url[..30] + "…" : url; }
    }

    DispatcherQueueTimer? _origDelay;

    async void Orig_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (!_ready || _filling) return;
        OrigText.Text = $"{(int)e.NewValue}%";
        // 拖一次滑杆会来几十个事件，别每格都写一次配置文件 —— 停下来 0.7 秒再存
        if (_origDelay is null)
        {
            _origDelay = DispatcherQueue.CreateTimer();
            _origDelay.Interval = TimeSpan.FromMilliseconds(700);
            _origDelay.Tick += async (_, _) =>
            {
                _origDelay!.Stop();
                await SaveAsync(new Dictionary<string, object?>
                {
                    ["orig_vol"] = Math.Round(OrigSlider.Value / 100.0, 3),
                });
            };
        }
        _origDelay.Stop();
        _origDelay.Start();
    }

    DispatcherQueueTimer? _subDelay;

    void SubSize_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (SubSizeText is null) return;
        SubSizeText.Text = FormatSize(e.NewValue);
        ScheduleSubSave();
        ScheduleSubPreview();
    }

    static string FormatSize(double v)
        => (Math.Abs(v - Math.Round(v)) < 0.001 ? v.ToString("0") : v.ToString("0.0")) + " px";

    void SubSpacing_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (SubSpacingText is null) return;
        SubSpacingText.Text = FormatSpacing(e.NewValue);
        ScheduleSubSave();
        ScheduleSubPreview();
    }

    static string FormatSpacing(double v)
        => v == 0 ? "0 px" : (v > 0 ? "+" : "") + v.ToString("0.0") + " px";

    void SubColor_Changed(ColorPicker sender, ColorChangedEventArgs args)
    {
        UpdateSubColorUI(ToHex(args.NewColor));
        if (!_ready || _filling) return;
        ScheduleSubSave();
        ScheduleSubPreview();
    }

    void UpdateSubColorUI(string hex)
    {
        var c = ParseHexColor(hex);
        SubColorSwatch.Background = new SolidColorBrush(c);
        SubColorText.Text = ToHex(c);
    }

    string CurrentSubColor()
    {
        var s = SubColorText?.Text ?? "";
        return s.Length == 7 && s[0] == '#' ? s : "#FFFFFF";
    }

    static Color ParseHexColor(string? hex)
    {
        var s = (hex ?? "").Trim().TrimStart('#');
        if (s.Length != 6 || !uint.TryParse(s, NumberStyles.HexNumber,
                CultureInfo.InvariantCulture, out var v))
            v = 0xFFFFFF;
        return Color.FromArgb(255, (byte)(v >> 16), (byte)(v >> 8), (byte)v);
    }

    static string ToHex(Color c) => $"#{c.R:X2}{c.G:X2}{c.B:X2}";

    void SubOutline_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (SubOutlineText is null) return;
        SubOutlineText.Text = e.NewValue.ToString("0.0");
        ScheduleSubSave();
        ScheduleSubPreview();
    }

    // ---------------- 字幕字体（自定义字体文件 / 恢复默认）----------------

    async void PickSubFont_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        string? path = await PickFontFileAsync();
        if (path is null) return;

        SubFontHint.Text = "正在读取字体…";
        var r = await Backend.CallAsync("font_info", new { path }, 20_000);
        string fam = "";
        if (r is { } rr && rr.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.True)
            fam = Str(rr, "family");
        if (fam.Length == 0)
        {
            SubFontHint.Text = "这个字体文件读不出字体名，换一个试试（.ttf / .otf / .ttc）";
            return;
        }
        _subFontName = fam;
        _subFontFile = path;
        UpdateSubFontUI();
        await SaveAsync(new Dictionary<string, object?>
        {
            ["subtitle_font"] = fam,
            ["subtitle_font_file"] = path,
        });
        ScheduleSubPreview();
    }

    async void ResetSubFont_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        _subFontName = "Starry Han Sans Light";
        _subFontFile = "";
        UpdateSubFontUI();
        await SaveAsync(new Dictionary<string, object?>
        {
            ["subtitle_font"] = "Starry Han Sans Light",
            ["subtitle_font_file"] = "",
        });
        ScheduleSubPreview();
    }

    void UpdateSubFontUI()
    {
        bool custom = _subFontFile.Trim().Length > 0;
        bool exists = custom && System.IO.File.Exists(_subFontFile);
        if (!custom)
        {
            SubFontText.Text = "思源黑体 Light（随包）";
            SubFontHint.Text = "支持 .ttf / .otf / .ttc；请使用你有权使用的字体";
        }
        else if (!exists)
        {
            SubFontText.Text = "思源黑体 Light（随包）";
            SubFontHint.Text = "找不到之前选的字体文件，已自动用回随包字体";
        }
        else
        {
            SubFontText.Text = _subFontName;
            SubFontHint.Text = "正在用：" + System.IO.Path.GetFileName(_subFontFile);
        }
        SubFontResetBtn.IsEnabled = custom;
    }

    async Task<string?> PickFontFileAsync()
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FileOpenPicker();
            picker.FileTypeFilter.Add(".ttf");
            picker.FileTypeFilter.Add(".otf");
            picker.FileTypeFilter.Add(".ttc");
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(MainWindow.Instance!);
            WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            var file = await picker.PickSingleFileAsync();
            return file?.Path;
        }
        catch (Exception ex)
        {
            App.Dump("选字体文件失败（不影响其它设置）: " + ex.Message);
            return null;
        }
    }

    // ---------------- 描边颜色（固定默认黑色）----------------

    void SubOutlineColor_Changed(ColorPicker sender, ColorChangedEventArgs args)
    {
        UpdateSubOutlineUI(ToHex(args.NewColor));
        if (!_ready || _filling) return;
        ScheduleSubSave();
        ScheduleSubPreview();
    }

    void UpdateSubOutlineUI(string? hex = null)
    {
        if (hex is { Length: 7 })
        {
            var c = ParseHexColor(hex);
            SubOutlineColorSwatch.Background = new SolidColorBrush(c);
            SubOutlineColorText.Text = ToHex(c);
        }
    }

    string CurrentOutlineColor()
    {
        var s = SubOutlineColorText?.Text ?? "";
        return s.Length == 7 && s[0] == '#' ? s : "#000000";
    }

    /// <summary>整组字幕样式恢复默认（用户 2026-10-21 要求能重置）。</summary>
    async void ResetSubtitleStyle_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "恢复默认字幕样式？",
            Content = "字体、字号、字间距、描边、描边颜色、字幕颜色、底部距离和双语字幕设置都会回到默认值。",
            PrimaryButtonText = "恢复默认",
            CloseButtonText = "算了",
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;

        var r = await Backend.CallAsync("reset_subtitle_style", null, 20_000);
        if (r is null || !(r.Value.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True))
        {
            SaveState.Text = "恢复失败：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应");
            return;
        }
        await LoadAsync();          // 重新读一遍配置，界面全部刷新
        SaveState.Text = "字幕样式已恢复默认 ✓";
    }

    void SubMargin_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (SubMarginText is null) return;
        SubMarginText.Text = ((int)e.NewValue) + " px";
        ScheduleSubSave();
        ScheduleSubPreview();
    }

    /// <summary>双语字幕开关：关掉时把「原文位置 / 原文字号」两行一起收起来。</summary>
    async void SubBilingual_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        UpdateSubBilingualUI();
        ScheduleSubPreview();
        await SaveAsync(new Dictionary<string, object?>
        {
            ["subtitle_bilingual"] = SubBilingualCheck.IsChecked == true,
        });
    }

    void UpdateSubBilingualUI()
    {
        SubBilingualRows.Visibility = SubBilingualCheck.IsChecked == true
            ? Visibility.Visible : Visibility.Collapsed;
    }

    async Task SubBilingualOrderChangedAsync()
    {
        if (!_ready || _filling) return;
        ScheduleSubPreview();
        await SaveAsync(new Dictionary<string, object?>
        {
            ["subtitle_bilingual_order"] =
                SubBilingualOrderBox.SelectedTag is { Length: > 0 } t ? t : "src_top",
        });
    }

    void SubBilingualScale_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (SubBilingualScaleText is null) return;
        SubBilingualScaleText.Text = ((int)e.NewValue) + "%";
        ScheduleSubSave();
        ScheduleSubPreview();
    }

    /// <summary>字幕样式滑杆掉一次会来几十个事件，停 0.5 秒再存。</summary>
    void ScheduleSubSave()
    {
        if (!_ready || _filling) return;
        if (_subDelay is null)
        {
            _subDelay = DispatcherQueue.CreateTimer();
            _subDelay.Interval = TimeSpan.FromMilliseconds(500);
            _subDelay.Tick += async (_, _) =>
            {
                _subDelay!.Stop();
                await SaveAsync(new Dictionary<string, object?>
                {
                    ["subtitle_size"] = Math.Round(SubSizeSlider.Value, 1),
                    ["subtitle_spacing"] = Math.Round(SubSpacingSlider.Value, 1),
                    ["subtitle_color"] = CurrentSubColor(),
                    ["subtitle_outline"] = Math.Round(SubOutlineSlider.Value, 1),
                    ["subtitle_outline_color"] = CurrentOutlineColor(),
                    ["subtitle_margin_v"] = (int)SubMarginSlider.Value,
                    ["subtitle_bilingual_scale"] = (int)SubBilingualScaleSlider.Value,
                });
            };
        }
        _subDelay.Stop();
        _subDelay.Start();
    }

    // ---------------- 字幕样式实时预览 ----------------
    // 用后端真实的 srt2ass + libass 渲一帧（不是 XAML 里拿 TextBlock 近似），
    // 所以字体、描边、字号、底部距离和最终成片完全一致。

    DispatcherQueueTimer? _previewDelay;
    int _previewSeq;
    bool _previewLargeBusy;
    bool _previewDirty;
    bool _previewRendering;

    /// <summary>滑杆拖动时每 100ms 渲一帧；上一帧没渲完就等它，渲完接着渲最新的。</summary>
    void ScheduleSubPreview()
    {
        if (!_ready || _filling) return;
        _previewDirty = true;
        if (_previewDelay is null)
        {
            _previewDelay = DispatcherQueue.CreateTimer();
            _previewDelay.Interval = TimeSpan.FromMilliseconds(100);
            _previewDelay.Tick += async (_, _) =>
            {
                _previewDelay!.Stop();
                if (!_previewDirty || _previewRendering) return;
                _previewDirty = false;
                _previewRendering = true;
                try { await RefreshSubPreviewAsync(); }
                finally { _previewRendering = false; }
                if (_previewDirty) _previewDelay.Start();
            };
        }
        if (!_previewRendering && !_previewDelay.IsRunning) _previewDelay.Start();
    }

    async Task RefreshSubPreviewAsync()
    {
        if (!_ready) return;
        int seq = ++_previewSeq;
        try
        {
            var r = await Backend.CallAsync("subtitle_preview", new
            {
                font = _subFontName,
                font_file = _subFontFile,
                size = SubSizeSlider.Value,
                spacing = Math.Round(SubSpacingSlider.Value, 1),
                color = CurrentSubColor(),
                outline = Math.Round(SubOutlineSlider.Value, 1),
                outline_color = CurrentOutlineColor(),
                margin_v = (int)SubMarginSlider.Value,
                bilingual = SubBilingualCheck.IsChecked == true,
                src_order = SubBilingualOrderBox.SelectedTag is { Length: > 0 } bo ? bo : "src_top",
                src_scale = SubBilingualScaleSlider.Value,
            }, 20_000);
            if (seq != _previewSeq) return;   // 期间又改了，丢弃这次结果

            if (r is not null
                && r.Value.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True
                && r.Value.TryGetProperty("file", out var file)
                && file.GetString() is { Length: > 0 } path)
            {
                SubPreviewImage.Source = new BitmapImage(new Uri(path));
                SubPreviewHint.Visibility = Visibility.Collapsed;
            }
            else
            {
                SubPreviewHint.Text = "预览生成失败";
                SubPreviewHint.Visibility = Visibility.Visible;
            }
        }
        catch (Exception ex)
        {
            App.Dump("字幕预览失败: " + ex.Message);
        }
    }

    async void SubPreview_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready || _previewLargeBusy) return;
        _previewLargeBusy = true;
        SubPreviewRing.Visibility = Visibility.Visible;
        SubPreviewRing.IsActive = true;
        string path = "";
        try
        {
            var r = await Backend.CallAsync("subtitle_preview", new
            {
                font = _subFontName,
                font_file = _subFontFile,
                size = SubSizeSlider.Value,
                spacing = Math.Round(SubSpacingSlider.Value, 1),
                color = CurrentSubColor(),
                outline = Math.Round(SubOutlineSlider.Value, 1),
                outline_color = CurrentOutlineColor(),
                margin_v = (int)SubMarginSlider.Value,
                bilingual = SubBilingualCheck.IsChecked == true,
                src_order = SubBilingualOrderBox.SelectedTag is { Length: > 0 } bo ? bo : "src_top",
                src_scale = SubBilingualScaleSlider.Value,
                large = true,
            }, 30_000);
            if (r is not null
                && r.Value.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True
                && r.Value.TryGetProperty("file", out var file))
                path = file.GetString() ?? "";
        }
        catch (Exception ex)
        {
            App.Dump("放大字幕预览失败: " + ex.Message);
        }
        finally
        {
            _previewLargeBusy = false;
            SubPreviewRing.IsActive = false;
            SubPreviewRing.Visibility = Visibility.Collapsed;
        }

        if (path.Length > 0)
        {
            MainWindow.Instance?.ShowPreview(path);
        }
        else
        {
            SubPreviewHint.Text = "放大预览生成失败";
            SubPreviewHint.Visibility = Visibility.Visible;
        }
    }

    async void Style_LostFocus(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        await SaveAsync(new Dictionary<string, object?> { ["tts_style"] = StyleBox.Text.Trim() });
    }

    async Task OnVoiceChanged()
    {
        if (!_ready || _filling) return;
        var tag = VoiceBox.SelectedTag is { Length: > 0 } t ? t : "";
        if (ModeBox.SelectedTag == "fast")
        {
            _edgeVoice = tag.Length > 0 ? tag : _edgeVoice;
            await SaveAsync(new Dictionary<string, object?> { ["edge_voice"] = _edgeVoice });
        }
        else
        {
            _voiceStd = tag.Length > 0 ? tag : _voiceStd;
            await SaveAsync(new Dictionary<string, object?> { ["voice"] = _voiceStd });
        }
    }

    /// <summary>按模式换音色列表：标准 = MiMo 音色；快速 = 微软音色 + 只留本地克隆。</summary>
    void ApplyModeUI(string mode)
    {
        bool fast = mode == "fast";
        var was = _filling;
        _filling = true;
        if (fast)
        {
            // 快速模式不用任何 API Key：整张卡片灰掉，别让用户以为还得填
            KeysCard.Opacity = 0.45;
            KeysCard.IsHitTestVisible = false;
            KeysHint.Text = "快速模式不需要 API Key（Google 机翻 + 微软配音）";
            VoiceBox.SetItems(EdgeVoices);
            VoiceBox.SelectTag(_edgeVoice, notify: false);
            CloneEngineBox.SetItems([("Index语音克隆（免费、本地、离线；显卡越强越快）", "local"),
                                     ("Qwen语音克隆（免费、本地、离线；显卡越强越快）", "qwen")]);
            CloneEngineBox.Select(0, notify: false);
            PreviewVoiceButton.IsEnabled = false;
            ToolTipService.SetToolTip(PreviewVoiceButton, "快速模式用微软音色，暂不支持试听");
        }
        else
        {
            KeysCard.Opacity = 1.0;
            KeysCard.IsHitTestVisible = true;
            KeysHint.Text = "只需填下面两个 Key，其他设置保持默认就能用。填完自动保存。（快速模式无需填写）";
            VoiceBox.SetItems(Mock.Voices.Select(v => (v, v)));
            VoiceBox.SelectTag(_voiceStd, notify: false);
            CloneEngineBox.SetItems([("云端 MiMo（更快；需联网、按量计费，可能限流）", "cloud"),
                                     ("Index语音克隆（免费、本地、离线；显卡越强越快）", "local"),
                                     ("Qwen语音克隆（免费、本地、离线；显卡越强越快）", "qwen")]);
            CloneEngineBox.SelectTag(_cloneEngineStd, notify: false);
            PreviewVoiceButton.IsEnabled = CloneCheck.IsChecked != true;
            ToolTipService.SetToolTip(PreviewVoiceButton, "合成一句样音，听听这个音色");
        }
        _filling = was;
        UpdateVoiceEnabled();
    }

    async void PreviewVoice_Click(object sender, RoutedEventArgs e)
        => await Services.VoicePreview.PlayAsync(
            VoiceBox.SelectedItem is { Length: > 0 } v ? v : "冰糖", PreviewVoiceButton);

    async Task OnModeChanged()
    {
        if (!_ready || _filling) return;
        var mode = ModeBox.SelectedTag is { Length: > 0 } t ? t : "standard";
        if (mode == "fast") _voiceStd = VoiceBox.SelectedTag is { Length: > 0 } v ? v : _voiceStd;
        else _edgeVoice = VoiceBox.SelectedTag is { Length: > 0 } v ? v : _edgeVoice;
        ApplyModeUI(mode);
        await SaveAsync(new Dictionary<string, object?> { ["mode"] = mode });
    }

    async void Clone_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        bool on = CloneCheck.IsChecked == true;
        UpdateVoiceEnabled();
        await SaveAsync(new Dictionary<string, object?> { ["tts_clone"] = on });
        if (on && CloneEngineBox.SelectedIndex == 1 && !_cloneReady)
            SetHint(CloneHint, "要先装本机那套：点右边的「下载并启用」（约 15 GB，装好了才能用）", false);
    }

    /// <summary>中文 → 英语时提示可用的英文音色。</summary>
    void UpdateLangHint()
    {
        bool on = LangBox.SelectedTag == "zh-en";
        if (on)
        {
            LangHint.Visibility = Visibility.Visible;
            Motion.Reveal(LangHint, 4, 180);
        }
        else
        {
            Motion.FadeOut(LangHint, 140);
            Motion.After(DispatcherQueue, 160, () =>
            {
                if (LangBox.SelectedTag != "zh-en") LangHint.Visibility = Visibility.Collapsed;
            });
        }
    }

    /// <summary>勾了「用原说话人的音色」时，默认音色不生效，灰掉并给一句说明。</summary>
    void UpdateVoiceEnabled()
    {
        bool clone = CloneCheck.IsChecked == true;
        VoiceBox.IsEnabled = !clone;
        PreviewVoiceButton.IsEnabled = !clone;
        // 不用硬切 Visibility / 硬改颜色：整块淡到 45%，提示淡入淡出，切换看着才顺
        Motion.FadeTo(VoiceControls, clone ? 0.45 : 1.0, 180);
        if (clone)
        {
            VoiceOffHint.Visibility = Visibility.Visible;
            Motion.Reveal(VoiceOffHint, 4, 180);
        }
        else
        {
            Motion.FadeOut(VoiceOffHint, 140);
            Motion.After(DispatcherQueue, 160, () =>
            {
                if (CloneCheck.IsChecked != true) VoiceOffHint.Visibility = Visibility.Collapsed;
            });
        }
    }

    async Task OnCloneEngineChanged()
    {
        if (!_ready || _filling) return;
        var eng = CloneEngineBox.SelectedTag is { Length: > 0 } t ? t : "local";
        await SaveAsync(new Dictionary<string, object?> { ["clone_engine"] = eng });
        if (eng == "local" && !_cloneReady)
            SetHint(CloneHint, "本机那套还没装：点右边的「下载并启用」（约 15 GB）", false);
        else
            await RefreshCloneAsync();
    }

    // ---------- 本地语音克隆：状态 / 装 / 删 ----------

    bool _cloneReady;
    bool _cloneInstalled;
    bool _cloneBusy;
    bool _qwenReady;
    bool _qwenInstalled;
    bool _qwenBusy;

    // 快速模式的微软音色（和后端 dubber.EDGE_VOICES 保持一致）
    static readonly (string, string)[] EdgeVoices =
    [
        ("晓晓（女声，推荐）", "zh-CN-XiaoxiaoNeural"),
        ("云希（男声）", "zh-CN-YunxiNeural"),
        ("晓伊（女声）", "zh-CN-XiaoyiNeural"),
        ("云健（男声，偏新闻）", "zh-CN-YunjianNeural"),
    ];
    string _voiceStd = "冰糖";
    string _edgeVoice = "zh-CN-XiaoxiaoNeural";
    string _cloneEngineStd = "local";

    async Task RefreshCloneAsync()
    {
        var r = await Backend.CallAsync("clone_status", null, 30_000);
        if (r is null)
        {
            SetHint(CloneHint, "读不到本地语音克隆的状态：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应"), false);
            return;
        }
        _cloneReady = r.Value.TryGetProperty("ready", out var ok) && ok.ValueKind == JsonValueKind.True;
        // 运行环境在、模型缺文件（下载中断）时，按钮要说「继续下载」而不是从头再来
        _cloneInstalled = r.Value.TryGetProperty("installed", out var ins) && ins.ValueKind == JsonValueKind.True;
        CloneInstallBtn.Visibility = _cloneReady ? Visibility.Collapsed : Visibility.Visible;
        CloneRemoveBtn.Visibility = _cloneReady ? Visibility.Visible : Visibility.Collapsed;
        CloneInstallBtn.Content = _cloneBusy ? "正在下载…" : (_cloneInstalled ? "继续下载" : "下载并启用");
        CloneInstallBtn.IsEnabled = !_cloneBusy;
        if (!_cloneBusy) SetHint(CloneHint, Str(r.Value, "detail"), _cloneReady ? true : null);
    }

    async void CloneInstall_Click(object sender, RoutedEventArgs e)
    {
        if (_cloneBusy) return;
        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = _cloneInstalled ? "接着下载本地语音克隆？" : "要下载本地语音克隆吗？",
            Content = _cloneInstalled
                ? "上次没下完，接着把缺的模型补上（已下好的不会重下）。"
                : "装在本机、以后不联网也能用，约 15 GB（运行环境 8 G + 模型 7 G），装一次就有。\n"
                  + "装在软件自己的数据目录里，不影响别的东西；随时可以在这一行删掉，空间会还回来。",
            PrimaryButtonText = _cloneInstalled ? "继续下载" : "开始下载",
            CloseButtonText = "以后再说",
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;

        _cloneBusy = true;
        CloneInstallBtn.Content = "正在下载…";
        CloneInstallBtn.IsEnabled = false;
        Backend.StatusReceived += OnCloneStatus;
        SetHint(CloneHint, "正在准备下载…", null);

        var r = await Backend.CallAsync("clone_install", null, 60_000);
        bool started = r is not null
            && (!r.Value.TryGetProperty("ok", out var ok) || ok.ValueKind != JsonValueKind.False);
        if (!started)
        {
            var err = r is null
                ? (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应")
                : Str(r.Value, "error");
            SetHint(CloneHint, "没能开始：" + (err.Length > 0 ? err : "后端没有响应"), false);
            Backend.StatusReceived -= OnCloneStatus;
            _cloneBusy = false;
            await RefreshCloneAsync();
        }
        // 启动成功时**不能在这里退订**：clone_install 是立即返回的后台任务，
        // 进度和完成/失败都靠后面的 status 事件推过来。退订早了就什么都看不到
        // （用户反馈过「点下载没反应、看不到进度」）。
    }

    /// <summary>下载中把进度和最新一行刷到提示上；完成/失败时收尾。
    /// 回调在读取线程，得回主线程。</summary>
    void OnCloneStatus(JsonElement st)
    {
        DispatcherQueue.TryEnqueue(() => HandleCloneStatus(st));
    }

    async void HandleCloneStatus(JsonElement st)
    {
        if (!_cloneBusy) return;
        bool running = st.TryGetProperty("running", out var run) && run.ValueKind == JsonValueKind.True;
        string stage = Str(st, "stage");
        if (!running)
        {
            // 只认本地克隆自己的终态，别被别的任务的 status 误收尾
            if (stage != "done" && stage != "error" && stage != "cancelled") return;
            if (stage == "done")
                SetHint(CloneHint, "本地语音克隆已装好", true);
            else if (stage == "error")
            {
                string err = Str(st, "error");
                SetHint(CloneHint, "下载失败：" + (err.Length > 0 ? err : "看日志目录里的详细错误"), false);
            }
            else if (stage == "cancelled")
                SetHint(CloneHint, "已取消", false);
            Backend.StatusReceived -= OnCloneStatus;
            _cloneBusy = false;
            await RefreshCloneAsync();
            return;
        }
        if (stage != "clone") return;
        int pct = st.TryGetProperty("percent", out var p) && p.TryGetInt32(out var n) ? n : 0;
        var line = "";
        if (st.TryGetProperty("log", out var log) && log.ValueKind == JsonValueKind.Array && log.GetArrayLength() > 0)
            line = log[log.GetArrayLength() - 1].GetString() ?? "";
        SetHint(CloneHint, $"下载中… {pct}%" + (line.Length > 0 ? "\n" + line : ""), null);
    }

    async void CloneRemove_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "删掉本地语音克隆？",
            Content = "会把下载的模型和运行环境一起删掉（腾出几个 GB）。\n"
                    + "以后再想用，重新点「下载并启用」就行；云端克隆不受影响。",
            PrimaryButtonText = "删除",
            CloseButtonText = "算了",
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;

        var r = await Backend.CallAsync("clone_uninstall", null, 120_000);
        if (r is null)
        {
            SetHint(CloneHint, "删除失败：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应"), false);
            return;
        }
        if (CloneCheck.IsChecked == true && CloneEngineBox.SelectedIndex == 1)
        {
            CloneCheck.IsChecked = false;
            CloneEngineBox.Select(0, notify: false);
        }
        await RefreshCloneAsync();
    }

    // ---------- Qwen3-TTS（本地，按需下载） ----------

    async Task RefreshQwenAsync()
    {
        var r = await Backend.CallAsync("qwen_status", null, 30_000);
        if (r is null)
        {
            SetHint(QwenHint, "读不到 Qwen3-TTS 的状态：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应"), false);
            return;
        }
        _qwenReady = r.Value.TryGetProperty("ready", out var ok) && ok.ValueKind == JsonValueKind.True;
        _qwenInstalled = r.Value.TryGetProperty("installed", out var ins) && ins.ValueKind == JsonValueKind.True;
        QwenInstallBtn.Visibility = _qwenReady ? Visibility.Collapsed : Visibility.Visible;
        QwenRemoveBtn.Visibility = _qwenReady ? Visibility.Visible : Visibility.Collapsed;
        QwenInstallBtn.Content = _qwenBusy ? "正在下载…" : (_qwenInstalled ? "继续下载" : "下载并启用");
        QwenInstallBtn.IsEnabled = !_qwenBusy;
        if (!_qwenBusy) SetHint(QwenHint, Str(r.Value, "detail"), _qwenReady ? true : null);
    }

    async void QwenInstall_Click(object sender, RoutedEventArgs e)
    {
        if (_qwenBusy) return;
        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "下载 Qwen3-TTS（本地）？",
            Content = "约 6 GB：运行环境 + 两个 0.6B 模型。\n"
                    + "装在本机、以后不联网也能用；有 NVIDIA 显卡会自动用 GPU，没有就走 CPU（会慢很多）。\n"
                    + "随时可以在这一行删掉，空间会还回来。",
            PrimaryButtonText = "开始下载",
            CloseButtonText = "以后再说",
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;

        _qwenBusy = true;
        QwenInstallBtn.Content = "正在下载…";
        QwenInstallBtn.IsEnabled = false;
        Backend.StatusReceived += OnQwenStatus;
        SetHint(QwenHint, "正在准备下载…", null);

        var r = await Backend.CallAsync("qwen_install", null, 60_000);
        bool started = r is not null
            && (!r.Value.TryGetProperty("ok", out var ok) || ok.ValueKind != JsonValueKind.False);
        if (!started)
        {
            var err = r is null
                ? (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应")
                : Str(r.Value, "error");
            SetHint(QwenHint, "没能开始：" + (err.Length > 0 ? err : "后端没有响应"), false);
            Backend.StatusReceived -= OnQwenStatus;
            _qwenBusy = false;
            await RefreshQwenAsync();
        }
    }

    void OnQwenStatus(JsonElement st)
    {
        DispatcherQueue.TryEnqueue(() => HandleQwenStatus(st));
    }

    async void HandleQwenStatus(JsonElement st)
    {
        if (!_qwenBusy) return;
        bool running = st.TryGetProperty("running", out var run) && run.ValueKind == JsonValueKind.True;
        string stage = Str(st, "stage");
        if (!running)
        {
            if (stage != "done" && stage != "error" && stage != "cancelled") return;
            if (stage == "done") SetHint(QwenHint, "Qwen3-TTS 已装好", true);
            else if (stage == "error")
            {
                string err = Str(st, "error");
                SetHint(QwenHint, "下载失败：" + (err.Length > 0 ? err : "看日志目录里的详细错误"), false);
            }
            else SetHint(QwenHint, "已取消", false);
            Backend.StatusReceived -= OnQwenStatus;
            _qwenBusy = false;
            await RefreshQwenAsync();
            return;
        }
        int pct = st.TryGetProperty("percent", out var p) && p.TryGetInt32(out var n) ? n : 0;
        string line = "";
        if (st.TryGetProperty("log", out var log) && log.ValueKind == JsonValueKind.Array
            && log.GetArrayLength() > 0)
            line = log[log.GetArrayLength() - 1].GetString() ?? "";
        SetHint(QwenHint, $"下载中… {pct}%" + (line.Length > 0 ? "\n" + line : ""), null);
    }

    async void QwenRemove_Click(object sender, RoutedEventArgs e)
    {
        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "删掉 Qwen3-TTS？",
            Content = "会把下载的模型和运行环境一起删掉（腾出几个 GB）。以后想用再重新下载。",
            PrimaryButtonText = "删除",
            CloseButtonText = "算了",
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary) return;
        var r = await Backend.CallAsync("qwen_uninstall", null, 120_000);
        if (r is null)
        {
            SetHint(QwenHint, "删除失败：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应"), false);
            return;
        }
        if (CloneEngineBox.SelectedTag == "qwen")
        {
            CloneCheck.IsChecked = false;
            _cloneEngineStd = "local";
            CloneEngineBox.SelectTag("local", notify: false);
        }
        await RefreshQwenAsync();
    }

    /// <summary>填 key 的框失焦了：存一下、顺手验一次（不再需要「测试」按钮）。</summary>
    async void Key_LostFocus(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling || sender is not PasswordBox box) return;
        _keyDebounce?.Stop();      // 人都点走了，不必再等那个「停手」计时器
        await SaveAndTestKeyAsync(box);
    }

    /// <summary>普通文本框：Tag 里写着它对应哪个配置项（output_dir / proxy / …）</summary>
    async void Field_LostFocus(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling || sender is not TextBox box || box.Tag is not string key) return;
        await SaveAsync(new Dictionary<string, object?> { [key] = box.Text.Trim() });
    }

    /// <summary>「检测」代理：填的地址通不通？不通就自动找 Windows 系统代理 / 本机常见端口。</summary>
    async void DetectProxy_Click(object sender, RoutedEventArgs e)
    {
        if (!_ready) return;
        ProxyTestBtn.IsEnabled = false;
        SetHint(ProxyHint, "正在检测…", null);
        try
        {
            // 先把框里现填的值存下去：检测用的必须是用户眼前这一份
            string typed = ProxyBox.Text.Trim();
            if (!await SaveAsync(new Dictionary<string, object?> { ["proxy"] = typed }))
            {
                SetHint(ProxyHint, "保存不上，检测没法继续", false);
                return;
            }
            var r = await Backend.CallAsync("detect_proxy", new { proxy = typed }, 30_000);
            if (r is null)
            {
                SetHint(ProxyHint, "检测失败：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应"), false);
                return;
            }
            bool ok = r.Value.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.True;
            string found = Str(r.Value, "proxy");
            string source = Str(r.Value, "source");
            bool saved = r.Value.TryGetProperty("saved", out var sv) && sv.ValueKind == JsonValueKind.True;
            if (!ok)
            {
                SetHint(ProxyHint, typed.Length > 0
                    ? "这个地址连不上，也没自动找到本机代理。没开代理软件的话，把这一栏清空再试。"
                    : "没找到本机代理。翻译本地文件不需要代理；下载在线视频时再填。", false);
                return;
            }
            if (saved) ProxyBox.Text = found;      // 后端已写进设置，框里也显示出来
            string where = source switch
            {
                "configured" => "你填的代理可用",
                "system" => "已自动用上 Windows 系统代理",
                _ when source.StartsWith("port:") => "已自动找到本机代理",
                _ => "代理可用",
            };
            SetHint(ProxyHint, $"{where}：{found}", true);
        }
        finally
        {
            ProxyTestBtn.IsEnabled = true;
        }
    }

    /// <summary>
    /// 换「引擎数据目录」之前把代价说清楚。返回 true = **别保存**（用户选了「算了」）。
    ///
    /// ★★ 为什么必须拦（2026-10-16）：换目录 = 引擎在**新位置从头下 5~6 GB**，
    ///   而旧目录那 15 GB 还原封不动占着盘、**不会自动删** —— 用户根本不知道这回事。
    ///   `datadir.pick()` 里那句「装过就别换地方：换 = 重下好几个 GB」只管**自动挑选**
    ///   那条路；用户手动改这条路以前没有任何守卫（手输、点「选择…」都是直接存）。
    /// </summary>
    async Task<bool> ConfirmHomeChangeAsync(string want)
    {
        var old = (_homeSaved ?? "").Trim();
        if (string.Equals(want.Trim(), old, StringComparison.OrdinalIgnoreCase)) return false;   // 没改，放行
        // 旧目录里还没装运行时 → 换了也不可惜，别拿弹框烦人
        // ★ 空串 = 用**实际默认**（软件旁的 data\engine），不是硬编码的 C 盘
        //   %LOCALAPPDATA%\FineSub —— 否则 D 盘用户改目录时会被告知「原来装在 C 盘」。
        var probe = old.Length > 0 ? old : (_homeDefault ?? "").Trim();
        if (probe.Length == 0)
            probe = System.IO.Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FineSub");
        try
        {
            if (!File.Exists(System.IO.Path.Combine(probe, "runtime", "python", "Scripts", "python.exe")))
                return false;
        }
        catch { return false; }

        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "换引擎数据目录？",
            Content = "引擎和模型现在已经装在：\n" + probe + "\n"
                    + "手输别的路径 = 在新位置重新准备约 5~6 GB（原来那份不会被删，还占着盘）。\n"
                    + "想省这次下载的话，用右边「选择…」那个按钮 —— 它会先把数据整个搬过去"
                    + "（同一块盘瞬间完成，跨盘是复制几分钟）。",
            PrimaryButtonText = "还是换",
            CloseButtonText = "算了",
        };
        return await dlg.ShowAsync() != ContentDialogResult.Primary;
    }

    async Task<bool> SaveAsync(Dictionary<string, object?> patch, bool skipHomeConfirm = false)
    {
        // ★ 改「引擎数据目录」先拦一道（2026-10-16）：
        //   两个入口（手输后失焦 / 点「选择…」）都汇到这里，放这儿最稳。
        //   返回 true = 用户反悔了 → 一个字都别保存（连 "已保存 ✓" 都不该显示）。
        //   ⚠️ 「选择…」那条路**先把数据搬好、该问的也问过了**，所以传 skipHomeConfirm=true
        //      直接落盘；这里再弹一次「换 = 重下」的框就纯属吓人（搬完的语义根本不是重下）。
        if (!skipHomeConfirm && patch.ContainsKey("finesub_home")
            && await ConfirmHomeChangeAsync(patch["finesub_home"] as string ?? ""))
        {
            HomeBox.Text = _homeSaved;          // 输入框退回原值，别显示一个没生效的路径
            return false;
        }

        var r = await Backend.CallAsync("save_settings", new { patch }, 30_000);
        // 存上了才更新「当前生效值」—— 没存上还留着旧的当基准
        if (r is not null && patch.TryGetValue("finesub_home", out var hv))
        {
            _homeSaved = (hv as string ?? "").Trim();
            var nowHome = _homeSaved.Length > 0 ? _homeSaved : _homeDefault;
            if (nowHome.Length > 0)
                HomeHint.Text = "当前：" + nowHome
                              + (_homeSaved.Length > 0 ? "" : "（默认）");
        }
        if (r is null)
        {
            SaveState.Text = "没保存上：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应");
            return false;
        }
        SaveState.Text = "已保存 ✓";
        if (patch.TryGetValue("subtitle_review", out var sr))
            MainWindow.Instance?.SetSubtitleNavVisible(sr is bool on && on);
        if (_saved is null)
        {
            _saved = DispatcherQueue.CreateTimer();
            _saved.Interval = TimeSpan.FromSeconds(3);
            _saved.Tick += (_, _) => { _saved.Stop(); SaveState.Text = "改动会自动保存"; };
        }
        _saved.Stop();
        _saved.Start();
        return true;
    }

    static string Str(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    static void SetHint(TextBlock hint, string text, bool? ok)
    {
        hint.Text = text;
        hint.Foreground = ok switch
        {
            true => new SolidColorBrush(Color.FromArgb(0xFF, 0x6E, 0xE7, 0xB7)),
            false => new SolidColorBrush(Color.FromArgb(0xFF, 0xFF, 0x8F, 0x99)),
            _ => (Brush)Application.Current.Resources["TextFillColorTertiaryBrush"],
        };
    }

    // ============================================================ API Key

    /// <summary>
    /// 显示 / 隐藏：平时是星号，点一下变明文（再点变回去）。
    /// 只是**显示方式**变了，`Text` 里存的始终是完整 key —— 测试和保存都不受影响。
    /// </summary>
    void ToggleMimo_Click(object sender, RoutedEventArgs e) => ToggleKey(MimoBox, MimoEye);

    void ToggleDeep_Click(object sender, RoutedEventArgs e) => ToggleKey(DeepBox, DeepEye);

    static void ToggleKey(PasswordBox box, Button eye)
    {
        bool reveal = box.PasswordRevealMode != PasswordRevealMode.Visible;
        box.PasswordRevealMode = reveal ? PasswordRevealMode.Visible : PasswordRevealMode.Hidden;
        eye.Content = reveal ? "隐藏" : "显示";
    }

    /// <summary>在 key 框里打字/粘贴之后，**停手 2.5 秒**就自己测一次（不用点任何按钮）</summary>
    void Key_Changed(object sender, RoutedEventArgs e)
    {
        if (!_ready || _filling) return;
        if (_keyDebounce is null)
        {
            _keyDebounce = DispatcherQueue.CreateTimer();
            _keyDebounce.Interval = TimeSpan.FromSeconds(2.5);
            _keyDebounce.IsRepeating = false;
            _keyDebounce.Tick += async (_, _) =>
            {
                _keyDebounce.Stop();
                // 两个框都看一眼：非空、而且跟上次测通过的值不一样，才去测
                foreach (var box in new[] { MimoBox, DeepBox })
                {
                    var kind = ReferenceEquals(box, MimoBox) ? "mimo" : "deepseek";
                    var text = box.Password.Trim();
                    if (text.Length > 0 && !(_keyTested.TryGetValue(kind, out var done) && done == text))
                        await SaveAndTestKeyAsync(box);
                }
            };
        }
        _keyDebounce.Stop();
        _keyDebounce.Start();
    }

    /// <summary>存一下 + 验一次。失焦和「停手」两条路都走这里。</summary>
    async Task SaveAndTestKeyAsync(PasswordBox box)
    {
        bool mimo = ReferenceEquals(box, MimoBox);
        var kind = mimo ? "mimo" : "deepseek";
        var hint = mimo ? MimoHint : DeepHint;
        var text = box.Password.Trim();

        if (!await SaveAsync(new Dictionary<string, object?> { [mimo ? "mimo_key" : "deepseek_key"] = text }))
            return;

        if (text.Length == 0) { _keyTested.Remove(kind); SetHint(hint, "还没填", null); return; }
        await TestKeyAsync(kind, text, hint);
    }

    /// <summary>验一次 key。值跟上次测通过的一样就跳过，不重复发请求。</summary>
    async Task TestKeyAsync(string kind, string key, TextBlock hint)
    {
        if (key.Length == 0) { SetHint(hint, "还没填", null); return; }
        if (_keyTested.TryGetValue(kind, out var done) && done == key) return;

        SetHint(hint, "正在检测…", null);
        var r = await Backend.CallAsync("test_key", new { kind, key }, 45_000);
        if (r is null)
        {
            _keyTested.Remove(kind);      // 后端没响应 ≠ key 不对，下次还会再试
            SetHint(hint, "❌ " + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应"), false);
            return;
        }
        bool ok = r.Value.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
        var msg = ok ? Str(r.Value, "detail") : Str(r.Value, "error");
        if (msg.Length == 0) msg = ok ? "连接正常" : "测试失败";
        if (ok) _keyTested[kind] = key; else _keyTested.Remove(kind);
        SetHint(hint, (ok ? "✅ " : "❌ ") + msg, ok);
        if (ok && kind == "deepseek") _ = RefreshDeepSeekBalanceAsync();
    }

    /// <summary>查 DeepSeek 账户余额（GET /user/balance）。失败就显示「查询失败」，不弹框。</summary>
    async Task RefreshDeepSeekBalanceAsync()
    {
        DeepBalanceText.Text = "查询中…";
        var r = await Backend.CallAsync("deepseek_balance", null, 20_000);
        if (r is null || !r.Value.TryGetProperty("ok", out var ok)
            || ok.ValueKind != JsonValueKind.True)
        {
            DeepBalanceText.Text = "查询失败";
            return;
        }
        double total = r.Value.TryGetProperty("total", out var t)
                       && t.ValueKind == JsonValueKind.Number && t.TryGetDouble(out var d) ? d : 0;
        var cur = Str(r.Value, "currency");
        var sym = cur == "CNY" ? "¥" : cur == "USD" ? "$" : (cur.Length > 0 ? cur + " " : "");
        DeepBalanceText.Text = $"{sym}{total:F2}";
    }

    async void DeepBalanceRefresh_Click(object sender, RoutedEventArgs e)
        => await RefreshDeepSeekBalanceAsync();

    /// <summary>「去申请 ↗」：申请入口是后端给的（免得两处写死地址对不上）</summary>
    async void Apply_Click(object sender, RoutedEventArgs e)
    {
        var which = ReferenceEquals(sender, MimoApply) ? "mimo" : "deepseek";
        if (!_links.TryGetValue(which, out var url) || url.Length == 0)
        {
            SaveState.Text = "申请入口还没读到（后端没连上）";
            return;
        }
        await Backend.CallAsync("open_url", new { url }, 20_000);
    }

    // ============================================================ 选目录

    async void PickOutDir_Click(object sender, RoutedEventArgs e)
    {
        var p = await PickFolderAsync();
        if (p is null) return;
        OutDirBox.Text = p;
        await SaveAsync(new Dictionary<string, object?> { ["output_dir"] = p });
    }

    /// <summary>换引擎数据目录：**先搬**（同盘瞬时 / 跨盘复制几分钟，不用重下），
    /// 搬不动或自检不过才回退到「在新位置重新准备（要重下几个 GB）」。
    /// 2026-10-16 用户定：「先搬；搬不动、或搬完自检不过 → 自动回退重下」。</summary>
    async void PickHome_Click(object sender, RoutedEventArgs e)
    {
        var p = await PickFolderAsync();
        if (p is null) return;

        SaveState.Text = "正在搬引擎数据（同盘瞬时；跨盘要复制几分钟，别关软件）…";
        var r = await Backend.CallAsync("move_home", new { new_dir = p }, 40 * 60_000);
        bool ok = r is { } rr && rr.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
        if (ok)
        {
            HomeBox.Text = p;
            await SaveAsync(new Dictionary<string, object?> { ["finesub_home"] = p }, skipHomeConfirm: true);
            var moved = r is { } r3 && r3.TryGetProperty("moved_to", out var mv) && mv.ValueKind == JsonValueKind.String
                ? mv.GetString() : "";
            SaveState.Text = moved is { Length: > 0 }
                ? "已把引擎数据搬过去 ✓（原位置那份已删，不用重下）"
                : "已记下新位置（还没有数据可搬，装引擎时直接用这儿）";
            return;
        }

        // 任务跑着的时候后端不让搬：搬一半会毁掉正在跑的任务。
        // 这条要单独处理，别落到下面那句「搬不成 → 还是换（重新下载）」上（那会把用户带偏）。
        if (r is { } rb && rb.TryGetProperty("busy", out var bz) && bz.ValueKind == JsonValueKind.True)
        {
            SaveState.Text = rb.TryGetProperty("reason", out var br) && br.ValueKind == JsonValueKind.String
                ? br.GetString() ?? "现在有任务正在运行"
                : "现在有任务正在运行";
            return;
        }

        // 搬不成 → 问一句再回退：重下要几个 GB，别自作主张
        var why = r is { } r2 && r2.TryGetProperty("reason", out var rs) && rs.ValueKind == JsonValueKind.String
            ? rs.GetString() ?? "搬不过去" : "搬不过去";
        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "这次没能直接搬过去",
            Content = why + "\n\n"
                    + "如果还是想换到那儿，引擎就要在新位置重新准备一遍（约 5~6 GB 下载）；\n"
                    + "原来那份不会被删掉，还占着盘。",
            PrimaryButtonText = "还是换（重新下载）",
            CloseButtonText = "算了，留在原处",
        };
        if (await dlg.ShowAsync() != ContentDialogResult.Primary)
        {
            SaveState.Text = "保持原样（数据还在原处，一切照旧）";
            return;
        }
        HomeBox.Text = p;
        await SaveAsync(new Dictionary<string, object?> { ["finesub_home"] = p }, skipHomeConfirm: true);
    }

    /// <summary>换「数据目录」：语音克隆 + 配音缓存 + 缩略图 + wheel 缓存一起搬。
    /// 和引擎那项一样是**先搬**；搬不成原处一个字不动，数据一切照旧。</summary>
    async void PickDataHome_Click(object sender, RoutedEventArgs e)
    {
        var p = await PickFolderAsync();
        if (p is null) return;

        SaveState.Text = "正在搬数据（同盘瞬时；跨盘要复制几分钟，别关软件）…";
        var r = await Backend.CallAsync("move_data_home", new { new_dir = p }, 60 * 60_000);
        bool ok = r is { } rr && rr.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
        if (ok)
        {
            // 后端会把「等于默认位置」存成空串，界面跟着它显示
            var saved = r is { } r3 && r3.TryGetProperty("saved", out var sv) && sv.ValueKind == JsonValueKind.String
                ? sv.GetString() ?? "" : p;
            DataHomeBox.Text = saved;
            var now = saved.Length > 0 ? saved : _dataDefault;
            if (now.Length > 0)
                DataHomeHint.Text = "当前：" + now + (saved.Length > 0 ? "" : "（默认）");
            SaveState.Text = saved.Length > 0 ? "已把数据搬过去 ✓" : "已改回默认位置 ✓";
            await RefreshCloneAsync();          // 克隆那行的「位置」跟着刷新
            return;
        }

        var why = r is { } r2 && r2.TryGetProperty("reason", out var rs) && rs.ValueKind == JsonValueKind.String
            ? rs.GetString() ?? "搬不过去" : "搬不过去";
        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = "这次没能搬过去",
            Content = why + "\n\n数据还在原来的位置，一切照旧。",
            CloseButtonText = "知道了",
        };
        await dlg.ShowAsync();
        SaveState.Text = "没有改动";
    }

    async Task<string?> PickFolderAsync()
    {
        try
        {
            var picker = new Windows.Storage.Pickers.FolderPicker();
            picker.FileTypeFilter.Add("*");     // 不加一条会直接抛异常
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(MainWindow.Instance!);
            WinRT.Interop.InitializeWithWindow.Initialize(picker, hwnd);
            var folder = await picker.PickSingleFolderAsync();
            return folder?.Path;
        }
        catch (Exception ex)
        {
            App.Dump("选文件夹失败（不影响其它设置）: " + ex.Message);
            return null;
        }
    }

    // ============================================================ 版本与更新

    async void CheckUpdate_Click(object sender, RoutedEventArgs e)
    {
        UpdateBtn.IsEnabled = false;
        UpdateText.Text = "正在检查…";
        try
        {
            var r = await Backend.CallAsync("check_update", null, 45_000);
            if (r is null)
            {
                UpdateText.Text = "检查失败：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应");
                return;
            }
            var cur = Str(r.Value, "current");
            // ★ 先看 ok：查失败（连不上 GitHub / 代理不通）时 has_update 也是 false，
            //   直接按 has 判断会把「没查成」显示成「已是最新」—— 客户报「检测不到最新版」
            //   就是这么来的（2026-10-15 诊断报告实证）。
            bool ok = r.Value.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True;
            if (!ok)
            {
                var err = Str(r.Value, "error");
                UpdateText.Text = "检查失败：" + (err.Length > 0 ? err : "连不上更新服务器")
                                  + (cur.Length > 0 ? $"（当前 v{cur}）" : "");
                return;
            }
            bool has = r.Value.TryGetProperty("has_update", out var h) && h.ValueKind == JsonValueKind.True;
            var ver = Str(r.Value, "version");
            if (!has)
            {
                UpdateText.Text = $"当前版本 v{cur} · 已是最新";
                return;
            }
            UpdateText.Text = $"有新版本 v{ver}（当前 v{cur}）";
            var notes = Str(r.Value, "notes");
            var dlg = new ContentDialog
            {
                XamlRoot = XamlRoot,
                Title = $"有新版本 v{ver}",
                Content = notes.Length > 0 ? notes : $"当前 v{cur}，最新 v{ver}。",
                PrimaryButtonText = "立即更新",
                SecondaryButtonText = "去发布页",
                CloseButtonText = "以后再说",
            };
            var res = await dlg.ShowAsync();
            if (res == ContentDialogResult.Primary)
            {
                await StartUpdateAsync(r.Value);
            }
            else if (res == ContentDialogResult.Secondary)
            {
                // 发布页 = 包地址里「/releases/」之前那一段 + /releases/latest
                // （不另写死仓库地址，免得以后挪了地方两处对不上）
                var pkg = Str(r.Value, "pkg_url");
                int i = pkg.IndexOf("/releases/", StringComparison.OrdinalIgnoreCase);
                if (i > 0)
                    await Backend.CallAsync("open_url", new { url = pkg[..i] + "/releases/latest" }, 20_000);
                else
                    SaveState.Text = "新版地址读不出来，去发布页手动看一下";
            }
        }
        finally { UpdateBtn.IsEnabled = true; }
    }

    /// <summary>
    /// 发起更新：下载 → 校验 sha256 → 静默安装 → 自己重启，整套在后端（`updater.py`）。
    /// 和主窗口顶部横幅那颗「立即更新」做的是同一件事。
    /// </summary>
    async Task StartUpdateAsync(JsonElement info)
    {
        LastUpdateInfo = info;
        Backend.StatusReceived -= OnUpdateStatus;      // 防重复挂
        Backend.StatusReceived += OnUpdateStatus;
        UpdateText.Text = "正在准备更新…";
        try
        {
            var r = await Backend.CallAsync("do_update",
                new { pkg_url = Str(info, "pkg_url"), sha256 = Str(info, "sha256") }, 30_000);
            // ⚠️ CallAsync 回来的已经是 JsonElement 本身，别写 `rr.Value`
            if (r is { } rr && rr.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.True)
                return;                                // 后端接管了：下完自己装、自己重启
            string why = "后端没有响应";
            if (r is { } x && x.TryGetProperty("error", out var er)) why = er.GetString() ?? why;
            UpdateText.Text = "更新没起来：" + why;
        }
        catch (Exception ex)
        {
            UpdateText.Text = "更新没起来：" + ex.Message;
        }
        finally
        {
            Backend.StatusReceived -= OnUpdateStatus;
        }
    }

    JsonElement LastUpdateInfo;

    /// <summary>更新包下载进度 —— 后端走任务状态那条通道推的（stage=update）</summary>
    void OnUpdateStatus(JsonElement st)
    {
        string stage = st.TryGetProperty("stage", out var s) ? (s.GetString() ?? "") : "";
        if (stage != "update") return;
        double pct = st.TryGetProperty("percent", out var p) && p.ValueKind == JsonValueKind.Number ? p.GetDouble() : 0;
        DispatcherQueue.TryEnqueue(() => UpdateText.Text = $"正在下载更新包… {pct:0}%");
    }

    // ============================================================ 日志与诊断

    // （原来这里有个 CopyLog_Click「复制日志」：把这次会话的日志整段拷进剪贴板。
    //   2026-10-16 用户定去掉 —— 导出诊断报告已经把日志+环境+自检打包成 zip，
    //   这一排少一个按钮更清爽，也免得用户只粘半截日志过来。）

    async void OpenLogs_Click(object sender, RoutedEventArgs e) => await SimpleAsync("open_logs", "日志目录已打开");
    async void ExportDiag_Click(object sender, RoutedEventArgs e) => await SimpleAsync("export_diag", "诊断报告已导出");
    async void Terms_Click(object sender, RoutedEventArgs e) => await SimpleAsync("open_doc", "已打开用户协议", "terms");
    /// <summary>「感谢」：弹软件里的浮层，列出用到的开源项目（用户 2026-10-16 定：
    /// 「感谢要做个窗口，感谢一些开源项目」——渐隐渐现、点空白关掉、没有按钮）。
    /// 以前是直接甩一个 .md 到系统里打开 —— 而且那个文件在生产版的路径探测里根本找不到
    /// （后端 exe 在 backend\，文档在它的上一级），点了**没反应**。都修了。</summary>
    void Licenses_Click(object sender, RoutedEventArgs e) => MainWindow.Instance?.ShowThanks();

    async Task SimpleAsync(string method, string okText, string? arg = null)
    {
        var r = arg is null
            ? await Backend.CallAsync(method, null, 30_000)
            : await Backend.CallAsync(method, new { which = arg }, 30_000);
        if (r is null)
        {
            SaveState.Text = "没成功：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应");
            return;
        }
        // 有路径就把路径也说出来，用户要转发的时候找得到
        var path = Str(r.Value, "path");
        SaveState.Text = path.Length > 0 ? $"{okText}：{path}" : okText;
    }

    // ============================================================ 主题色

    /// <summary>自测用：不用鼠标，直接把浮出层打开（截图核对用）</summary>
    public void Probe(string what)
    {
        if (what == "qr") MainWindow.Instance?.ShowQr();
        // 自测：把「背景亮度」滑杆拨到 N% —— **走真实控件**，会触发 BgSlider_ValueChanged，
        // 和用户手动拖到那个位置走的是同一条路（为了复现「拖到 100% 就黑屏」，2026-10-15）
        else if (what.StartsWith("bg:", StringComparison.OrdinalIgnoreCase))
        {
            if (int.TryParse(what["bg:".Length..], out var pct)) BgSlider.Value = pct;
        }
        else if (what == "pin") DoPin();            // 自测：走一遍「收藏这张」（验新收的那条有没有缩略图）
        else if (what == "gallery")                 // 自测：展开「已收藏的壁纸」那排预览图
        {
            WallExpander.IsExpanded = true;
            RefreshSavedGallery();
            PageScroll.ScrollTo(PageScroll.HorizontalOffset, PageScroll.ScrollableHeight);   // 滚到底，让那排可见
        }
        else if (what == "savedlist")               // 自测：收藏列表读出来（查"重启之后收藏还在不在"）
        {
            var saved = Services.Wallpaper.Saved();
            var report = $"[wall] 收藏列表：{saved.Count} 条\n"
                + $"目录={Services.Wallpaper.Dir}\n"
                + string.Join("\n", saved.Select(f =>
                      $"  {System.IO.Path.GetFileName(f)}  {new FileInfo(f).Length / 1024}KB"
                      + (string.Equals(UiState.WallpaperPin, f, StringComparison.OrdinalIgnoreCase) ? "  ←正在用" : "")))
                + $"\n表头='{WallExpanderHeader.Text}' 展开={WallExpander.IsExpanded} 缩略图{WallStrip.Children.Count}个";
            App.Dump(report);
            try
            {
                System.IO.File.WriteAllText(
                    System.IO.Path.Combine(System.IO.Path.GetTempPath(), "starryvt-saved.txt"), report);
            }
            catch { }
        }
        else if (what == "wallnext")                // 自测：收藏着的时候点「换一张」应该能回到随机
        {
            var before = UiState.WallpaperPin;
            MainWindow.Instance?.PinCurrentWallpaper();          // 先收藏现在这张 = 进入"正在用收藏"状态
            var pinned = UiState.WallpaperPin;
            WallNext_Click(WallNextBtn, new RoutedEventArgs());
            var t = DispatcherQueue.CreateTimer();               // WallNext_Click 是 async void，只能延时观察
            t.Interval = TimeSpan.FromSeconds(8);
            t.IsRepeating = false;
            t.Tick += async (_, _) =>
            {
                var report = $"[wall] 点换一张自测：\n"
                    + $"点之前   收藏='{before}'\n"
                    + $"刚收藏完 收藏='{pinned}'\n"
                    + $"点完 8s  收藏='{UiState.WallpaperPin}'\n"
                    + $"当前文件={System.IO.Path.GetFileName(MainWindow.Instance?.CurrentWallpaper ?? "")}\n"
                    + $"按钮='{WallNextBtn.Content}' 可用={WallNextBtn.IsEnabled}\n"
                    + $"提示={WallHint.Text}";
                App.Dump(report);
                // 单独再写一份：App.Dump 是覆盖写的，随后壁纸下好时的日志会把它冲掉
                try
                {
                    await System.IO.File.WriteAllTextAsync(
                        System.IO.Path.Combine(System.IO.Path.GetTempPath(), "starryvt-wallnext.txt"), report);
                }
                catch { }
            };
            t.Start();
        }
        else ShowPalette_Click(PaletteButton, new RoutedEventArgs());
    }

    /// <summary>快捷色：一排圆形色块，点一下就换（当前这个套一圈白边）</summary>
    void BuildAccentRow()
    {
        AccentRow.Children.Clear();
        var current = Accent.Normalize(Accent.Current);

        // 色块上不挂颜色名提示（用户 2026-10-14：不要显示"紫色/粉色"这类文字）
        foreach (var (_, hex) in Accent.Palette)
        {
            bool on = string.Equals(Accent.Normalize(hex), current, StringComparison.OrdinalIgnoreCase);
            Accent.TryParse(hex, out var color);

            var dot = new Border
            {
                Width = 26,
                Height = 26,
                CornerRadius = new CornerRadius(13),
                Background = new SolidColorBrush(color),
                Child = new FontIcon
                {
                    Glyph = "",      // 对勾
                    FontSize = 12,
                    Foreground = new SolidColorBrush(Colors.White),
                    Visibility = on ? Visibility.Visible : Visibility.Collapsed,
                },
            };

            var btn = new Button
            {
                Content = dot,
                Padding = new Thickness(0),
                Width = 34,
                Height = 34,
                CornerRadius = new CornerRadius(17),
                Background = new SolidColorBrush(Colors.Transparent),
                BorderThickness = new Thickness(2),
                BorderBrush = new SolidColorBrush(on ? Colors.White : Colors.Transparent),
                Tag = hex,
            };
            btn.Click += (_, _) =>
            {
                Accent.Apply(hex);
                BuildAccentRow();
            };
            AccentRow.Children.Add(btn);
        }
    }

    /// <summary>调色板：WinUI 自带的取色器，**正方形色域**，
    /// 开在鼠标右边（不是弹窗），拖动时主题色实时跟着变</summary>
    void ShowPalette_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement anchor) return;

        if (_paletteFlyout is null)
        {
            var picker = new ColorPicker
            {
                ColorSpectrumShape = ColorSpectrumShape.Box,     // 正方形色域（不是那个圆环）
                // 只留正方形色域，别的（色轮/色带/色号/更多）一概不要 ——
                // 「更多/更少」那个展开按钮用户 2026-10-14 点名不要
                IsColorChannelTextInputVisible = false,
                IsHexInputVisible = false,
                IsAlphaEnabled = false,
                IsAlphaSliderVisible = false,
                IsAlphaTextInputVisible = false,
                IsMoreButtonVisible = false,
                // 宽高都要给足：塞在浮出层里被压扁的话，色域就成横条了（正方形要的是等宽等高）
                Width = 340,
                Height = 340,
                HorizontalAlignment = HorizontalAlignment.Center,
            };
            picker.ColorChanged += (_, args) =>
            {
                var hex = $"#{args.NewColor.R:X2}{args.NewColor.G:X2}{args.NewColor.B:X2}";
                _pendingAccent = hex;
                // ⚠️ 拖动时 ColorChanged 一秒来几十次，而 Accent.Apply 会**把整棵可视化树
                //    重刷一遍主题**（见 Accent.Repaint 的说明）—— 每一次都刷就是"一卡一卡"。
                //    所以这里节流：拖动过程中最多 6 次/秒，松手（浮出层关掉）时再落一次最终值。
                if ((DateTime.Now - _lastAccentAt).TotalMilliseconds < 160) return;
                _lastAccentAt = DateTime.Now;
                Accent.Apply(hex);
            };
            _picker = picker;
            _paletteFlyout = new Flyout
            {
                Content = picker,
                Placement = FlyoutPlacementMode.Right,
                FlyoutPresenterStyle = (Style)Application.Current.Resources["PaletteFlyoutPresenterStyle"],
            };
            // 每次打开先回到当前色（不然记住的是上次拖到一半的位置）
            _paletteFlyout.Opened += (_, _) =>
            {
                if (Accent.TryParse(Accent.Current, out var c)) picker.Color = c;
            };
            // 关掉浮出层 = 这一轮挑色结束：把最终颜色落定（拖动中的节流可能把它跳过去了），
            // 顺便把那一排圆点的选中态刷新到最新
            _paletteFlyout.Closed += (_, _) =>
            {
                if (_pendingAccent.Length == 0) return;
                Accent.Apply(_pendingAccent);
                _pendingAccent = "";
                BuildAccentRow();
            };
        }

        _paletteFlyout.ShowAt(anchor, new FlyoutShowOptions
        {
            Placement = FlyoutPlacementMode.Right,     // 鼠标右边
            Position = CursorIn(anchor),
        });
    }

    [StructLayout(LayoutKind.Sequential)]
    struct POINT { public int X; public int Y; }

    [DllImport("user32.dll")]
    static extern bool GetCursorPos(out POINT p);

    /// <summary>鼠标在某个元素里的坐标（拿来做「跟着鼠标弹出」的锚点）</summary>
    static Point CursorIn(FrameworkElement el)
    {
        try
        {
            if (!GetCursorPos(out var p) || MainWindow.Instance?.AppWindow is not { } win || el.XamlRoot is null)
                return new Point(el.ActualWidth / 2, el.ActualHeight / 2);

            double scale = el.XamlRoot.RasterizationScale <= 0 ? 1 : el.XamlRoot.RasterizationScale;
            var winPos = win.Position;                                    // 物理像素
            double rootX = (p.X - winPos.X) / scale;
            double rootY = (p.Y - winPos.Y) / scale;

            // 夹一下：浮出层大概 420×620，太靠边就把弹点往里挪，
            // 否则它会被放到窗口外面（踩过：鼠标在窗口外时整块看不见）
            var size = el.XamlRoot.Size;
            rootX = Math.Clamp(rootX, 8, Math.Max(8, size.Width - 430));
            rootY = Math.Clamp(rootY, 8, Math.Max(8, size.Height - 640));

            var origin = el.TransformToVisual(null).TransformPoint(new Point(0, 0));
            return new Point(rootX - origin.X, rootY - origin.Y);
        }
        catch
        {
            return new Point(el.ActualWidth / 2, el.ActualHeight / 2);
        }
    }

    // 关于 / 赞赏码 / 微信号都搬去「关于」页了（Pages/AboutPage.xaml.cs），
    // 这里不再放 —— 用户 2026-10-14 要求把「关于与支持」整块移出去。
}
