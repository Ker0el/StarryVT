using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Starry.Vt.App.Services;

namespace Starry.Vt.App.Pages;

/// <summary>
/// 「字幕检查」页：翻译完成后暂停，双语对照检查译文。
///
/// 每一行上面是识别出的原文（只读），下面是翻译好的译文（可编辑）。
/// 点「继续」时会先保存译文，再继续配音和压制。批量任务、MCP 调用不会经过这里。
/// </summary>
public sealed partial class SubtitlePage : Page
{
    public ObservableCollection<SubtitleSegment> Segments { get; } = new();

    readonly List<SubtitleSegment> _all = new();
    string _rawPath = "";                 // 原文（识别结果），只用于对照显示
    string _finalPath = "";               // 成品译文，可编辑
    string _loadedPath = "";
    bool _dirty;                          // 用户改过、还没保存
    bool _loading;                        // 正在灌数据，别把绑定触发当成用户修改
    bool _filtering;
    bool _running;
    bool _awaitingReview;                 // 流水线停在译文检查点，等用户点「继续」
    string _reviewKind = "";
    bool _ready;
    int _loadSeq;                         // 防止旧加载覆盖新内容

    public SubtitlePage()
    {
        InitializeComponent();
        NavigationCacheMode = Microsoft.UI.Xaml.Navigation.NavigationCacheMode.Required;
        SegmentList.ItemsSource = Segments;
        Loaded += OnLoaded;
        Unloaded += OnUnloaded;
        _ready = true;
    }

    void OnLoaded(object sender, RoutedEventArgs e)
    {
        Backend.StatusReceived -= OnStatus;
        Backend.StatusReceived += OnStatus;
        _ = RefreshAsync();
    }

    void OnUnloaded(object sender, RoutedEventArgs e) => Backend.StatusReceived -= OnStatus;

    async Task RefreshAsync()
    {
        if (!Backend.Start())
        {
            SetStatus("没连上后端：" + Backend.LastError);
            return;
        }
        var r = await Backend.CallAsync("get_status", null, 15_000);
        if (r is JsonElement st) Apply(st);
    }

    void OnStatus(JsonElement st) => DispatcherQueue.TryEnqueue(() => Apply(st));

    void Apply(JsonElement st)
    {
        if (st.ValueKind != JsonValueKind.Object) return;
        _rawPath = Str(st, "subtitle_raw");
        _finalPath = Str(st, "subtitle_final");
        _running = st.TryGetProperty("running", out var run) && run.ValueKind == JsonValueKind.True;
        _awaitingReview = st.TryGetProperty("awaiting_review", out var ar) && ar.ValueKind == JsonValueKind.True;
        _reviewKind = Str(st, "review_kind");
        string stage = Str(st, "stage");

        if (_finalPath.Length > 0 && _finalPath != _loadedPath && !_dirty)
            _ = LoadPathAsync(_finalPath);
        else if (_finalPath.Length == 0 && _loadedPath.Length > 0)
        {
            // 新任务刚开始、译文还没出来：别把上一个任务的内容留在页面上
            _loadSeq++;
            _loading = false;
            _loadedPath = "";
            _dirty = false;
            _all.Clear();
            Segments.Clear();
            ShowEmpty(_running ? "翻译中…" : "还没有可检查的字幕",
                      _running ? "翻译完成后会出现在这里" : "在本地翻译或在线翻译页开始一个任务");
        }
        else if (_finalPath.Length == 0 && _loadedPath.Length == 0)
        {
            ShowEmpty(_running ? "翻译中…" : "还没有可检查的字幕",
                      _running ? "翻译完成后会出现在这里" : "在本地翻译或在线翻译页开始一个任务");
        }

        UpdateButtons();
        if (_awaitingReview)
        {
            SetStatus(_reviewKind == "final"
                ? "翻译完成，对照原文检查译文，改完点「保存并继续翻译」"
                : "检查字幕，改完点「保存并继续翻译」");
            return;
        }
        if (!_running)
        {
            if (stage == "done" && _loadedPath.Length > 0)
                SetStatus("翻译完成，可以继续修改译文并重新压制");
            else if (stage == "cancelled")
                SetStatus("任务已取消");
            else if (stage == "error")
                SetStatus("任务出错：" + Str(st, "error"));
            return;
        }
        SetStatus(stage switch
        {
            "translate" => "翻译中：完成后会暂停对照检查译文",
            "dub" => "配音中…",
            "burn" => "正在压制字幕",
            _ => "任务运行中",
        });
    }

    async Task LoadPathAsync(string path)
    {
        if (path.Length == 0) return;
        int seq = ++_loadSeq;
        _loading = true;
        var r = await Backend.CallAsync("subtitle_load", new { path }, 30_000);
        if (seq != _loadSeq) return;
        if (r is not JsonElement e || !Ok(e))
        {
            _loading = false;
            SetStatus("读不了字幕：" + ErrorOf(r));
            return;
        }
        // 双语对照：把原文也读一份，配到每行译文上方
        var source = new List<(double Start, string Text)>();
        if (_rawPath.Length > 0 && _rawPath != path)
        {
            var rs = await Backend.CallAsync("subtitle_load", new { path = _rawPath }, 30_000);
            if (seq != _loadSeq) return;
            if (rs is JsonElement es && Ok(es)
                && es.TryGetProperty("segments", out var sa) && sa.ValueKind == JsonValueKind.Array)
            {
                foreach (var x in sa.EnumerateArray())
                    source.Add((NumD(x, "start"), Str(x, "text")));
            }
        }
        _all.Clear();
        if (e.TryGetProperty("segments", out var arr) && arr.ValueKind == JsonValueKind.Array)
        {
            int total = arr.GetArrayLength();
            int idx = 0;
            foreach (var x in arr.EnumerateArray())
            {
                double start = NumD(x, "start");
                string src = "";
                if (source.Count > 0)
                    src = source.Count == total && idx < source.Count
                        ? source[idx].Text
                        : NearestSource(source, start);
                _all.Add(new SubtitleSegment
                {
                    No = x.TryGetProperty("i", out var i) && i.TryGetInt32(out var n) ? n : _all.Count + 1,
                    Start = start,
                    End = NumD(x, "end"),
                    Text = Str(x, "text"),
                    SourceText = src,
                });
                idx++;
            }
        }
        _loadedPath = path;
        _dirty = false;
        ApplyFilter();
        if (_all.Count == 0)
        {
            _loading = false;
            ShowEmpty("这份字幕是空的", "换一份字幕，或者重新翻译一次");
            return;
        }
        EmptyState.Visibility = Visibility.Collapsed;
        SegmentList.Visibility = Visibility.Visible;
        SetStatus(_awaitingReview
            ? "翻译完成，对照原文检查译文，改完点「保存并继续翻译」"
            : $"已载入 {_all.Count} 条 · {System.IO.Path.GetFileName(path)}");
        // ListView 的容器是延后创建的：等这一轮布局走完再解除保护，
        // 否则绑定初始值会被当成「用户改了字」，状态栏一上来就显示未保存。
        DispatcherQueue.TryEnqueue(() =>
        {
            if (seq != _loadSeq) return;
            _loading = false;
            UpdateButtons();
        });
    }

    void ApplyFilter()
    {
        string q = SearchBox.Text.Trim();
        _filtering = true;
        Segments.Clear();
        foreach (var s in _all)
            if (q.Length == 0 || s.Text.Contains(q, StringComparison.OrdinalIgnoreCase)
                || s.SourceText.Contains(q, StringComparison.OrdinalIgnoreCase))
                Segments.Add(s);
        _filtering = false;
        if (_all.Count > 0 && Segments.Count == 0) SetStatus("没有匹配的字幕");
    }

    void Search_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!_ready) return;
        ApplyFilter();
    }

    async void Retranslate_Click(object sender, RoutedEventArgs e)
    {
        if (_awaitingReview)
        {
            if (!await Confirm("保存并继续翻译？", "保存译文，继续配音和压制。", "保存并继续")) return;
            if (!await SaveAsync()) return;
            SetStatus("正在继续…");
            var r = await Backend.CallAsync("continue_review", new { path = _finalPath }, 30_000);
            if (r is JsonElement el && Ok(el))
            {
                _awaitingReview = false;
                _loadedPath = _finalPath;
                SetStatus("已继续，开始配音 / 压制");
            }
            else
            {
                SetStatus("没能继续：" + ErrorOf(r));
            }
            UpdateButtons();
            return;
        }
        if (_running || _finalPath.Length == 0) return;
        // 任务已经完成：这个按钮用于保存译文并重新压制
        if (!await Confirm("保存并继续翻译？", "保存当前译文，并重新压制一遍成片。配音不会重新生成。", "保存并继续")) return;
        if (!await SaveAsync()) return;
        SetStatus("正在重新压制…");
        var re = await Backend.CallAsync("reburn", null, 30_000);
        if (re is JsonElement rel && Ok(rel)) SetStatus("已开始重新压制，完成后会更新原成片");
        else SetStatus("没能重新压制：" + ErrorOf(re));
        UpdateButtons();
    }

    void SegmentText_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_loading || _filtering) return;
        // 只有用户真的在输入框里改才算；ListView 创建容器时绑定初始值不算。
        if (sender is TextBox tb && tb.FocusState != FocusState.Unfocused)
        {
            _dirty = true;
            SetStatus(_awaitingReview ? "有未保存的修改，点「保存并继续翻译」时会保存" : "有未保存的修改");
        }
    }

    async Task<bool> SaveAsync()
    {
        if (_finalPath.Length == 0)
        {
            SetStatus("没有可保存的字幕");
            return false;
        }
        var segs = _all.Select(s => new { start = s.Start, end = s.End, text = s.Text }).ToArray();
        var r = await Backend.CallAsync("subtitle_save", new { path = _finalPath, segments = segs }, 30_000);
        if (r is not JsonElement e || !Ok(e))
        {
            SetStatus("没保存上：" + ErrorOf(r));
            return false;
        }
        _dirty = false;
        _loadedPath = _finalPath;
        SetStatus($"已保存 {_all.Count} 条");
        return true;
    }

    async Task<bool> Confirm(string title, string text, string ok)
    {
        var dlg = new ContentDialog
        {
            XamlRoot = XamlRoot,
            Title = title,
            Content = text,
            PrimaryButtonText = ok,
            CloseButtonText = "取消",
        };
        return await dlg.ShowAsync() == ContentDialogResult.Primary;
    }

    void UpdateButtons()
    {
        bool hasFinal = _finalPath.Length > 0;
        bool canAct = hasFinal && (_awaitingReview || !_running);
        RetranslateButton.Visibility = canAct ? Visibility.Visible : Visibility.Collapsed;
        RetranslateButton.IsEnabled = canAct;
    }

    void ShowEmpty(string title, string hint)
    {
        EmptyTitle.Text = title;
        EmptyHint.Text = hint;
        EmptyState.Visibility = Visibility.Visible;
        SegmentList.Visibility = Visibility.Collapsed;
    }

    void SetStatus(string text) => StatusText.Text = text;

    static bool Ok(JsonElement e) =>
        e.TryGetProperty("ok", out var v) && v.ValueKind == JsonValueKind.True;

    static string ErrorOf(JsonElement? r)
    {
        if (r is JsonElement e)
        {
            string s = Str(e, "error");
            if (s.Length > 0) return s;
        }
        return Backend.LastError is { Length: > 0 } err ? err : "后端没有响应";
    }

    static string Str(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    static double NumD(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.TryGetDouble(out var d) ? d : 0;

    static string NearestSource(List<(double Start, string Text)> src, double start)
    {
        if (src.Count == 0) return "";
        var best = src[0];
        double bestDiff = Math.Abs(best.Start - start);
        foreach (var s in src)
        {
            double d = Math.Abs(s.Start - start);
            if (d < bestDiff) { bestDiff = d; best = s; }
        }
        return best.Text;
    }
}

/// <summary>一行字幕：序号 + 时间轴 + 原文（只读）+ 译文（可编辑）。</summary>
public sealed class SubtitleSegment : INotifyPropertyChanged
{
    public int No { get; set; }
    public double Start { get; set; }
    public double End { get; set; }

    string _text = "";
    public string Text
    {
        get => _text;
        set
        {
            if (_text == value) return;
            _text = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Text)));
        }
    }

    string _source = "";
    /// <summary>译文上方显示的原文（只读）。</summary>
    public string SourceText
    {
        get => _source;
        set
        {
            if (_source == value) return;
            _source = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(SourceText)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(SourceDisplay)));
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(SourceVisibility)));
        }
    }

    public string SourceDisplay => string.IsNullOrWhiteSpace(SourceText) ? "" : "原文：" + SourceText;
    public Visibility SourceVisibility =>
        string.IsNullOrWhiteSpace(SourceText) ? Visibility.Collapsed : Visibility.Visible;

    public string IndexText => No.ToString();
    public string TimeText => $"{Ft(Start)} → {Ft(End)}";

    static string Ft(double t)
    {
        var ts = TimeSpan.FromSeconds(Math.Max(0, t));
        return ts.TotalHours >= 1
            ? $"{(int)ts.TotalHours}:{ts.Minutes:00}:{ts.Seconds:00}.{ts.Milliseconds:000}"
            : $"{ts.Minutes:00}:{ts.Seconds:00}.{ts.Milliseconds:000}";
    }

    public event PropertyChangedEventHandler? PropertyChanged;
}
