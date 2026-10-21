using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.UI.Dispatching;
using Starry.Vt.App.Fakes;

namespace Starry.Vt.App.Services;

/// <summary>
/// 「跑一次任务」这件事的接口。工作页只认它，所以真后端和样机脚本可以随时对调。
///
/// 一个实例 = 一次任务的生命周期：<see cref="Start"/> 之后它会一路往外抛
/// 进度 / 日志 / 队列，最后抛一次 <see cref="Finished"/>；<see cref="Cancel"/> 随时可叫停。
/// </summary>
public interface IRunner
{
    /// <summary>阶段（download / translate / dub / burn）+ 总百分比</summary>
    event Action<string, int>? Progress;

    event Action<LogLine>? Line;

    /// <summary>跑完了 —— 成功、出错、取消都算（看 <see cref="EndStage"/> 分辨）</summary>
    event Action? Finished;

    /// <summary>批量：整条队列变了（后端每次推的都是完整队列，直接整体换掉）</summary>
    event Action<IReadOnlyList<QueueItem>>? Queue;

    bool Running { get; }

    /// <summary>成品路径（单个=成片；批量=输出目录）。没有就是空串。</summary>
    string ResultPath { get; }

    /// <summary>输出目录（单个=成片所在那一层；批量=批次目录）。「打开输出目录」按这个走。</summary>
    string ResultDir { get; }

    /// <summary>收尾那句话。批量是「成功 5，失败 1」，出错时是错误信息。</summary>
    string ResultNote { get; }

    /// <summary>done / error / cancelled</summary>
    string EndStage { get; }

    void Start();
    void Cancel();
}

/// <summary>
/// 真后端：把活交给 Python（<see cref="Backend"/>，JSON-RPC over stdio），
/// 自己只做三件事 —— 发开工请求、把后端**主动推**的状态翻译成界面事件、收尾。
///
/// 界面不用轮询：后端每 300ms 自己比一次状态，变了就推一整份过来。
/// </summary>
public sealed class RealRunner : IRunner
{
    readonly DispatcherQueue _ui;
    readonly Func<(string Method, object Args)> _plan;

    /// <summary>已经吐给界面的日志行数。后端每次推的是**完整**日志列表，只挑新增的发出去。</summary>
    int _seen;
    bool _done;
    bool _hooked;

    /// <summary>这一单里见过「正在跑」没有（见 Apply 里那段收尾判据）</summary>
    bool _sawRunning;

    /// <param name="plan">开跑那一刻才取：走哪个方法（start / start_batch）+ 参数。
    /// 做成回调是为了「点按钮那一刻界面上的选择」说了算，而不是构造时的。</param>
    public RealRunner(Func<(string Method, object Args)> plan)
    {
        _plan = plan;
        _ui = DispatcherQueue.GetForCurrentThread()
              ?? throw new InvalidOperationException("RealRunner 必须在界面线程上创建");
    }

    public event Action<string, int>? Progress;
    public event Action<LogLine>? Line;
    public event Action? Finished;
    public event Action<IReadOnlyList<QueueItem>>? Queue;

    public bool Running { get; private set; }
    public string ResultPath { get; private set; } = "";
    public string ResultDir { get; private set; } = "";
    public string ResultNote { get; private set; } = "";
    public string EndStage { get; private set; } = "";

    /// <summary>
    /// 全局「有没有一单在跑」—— 窗口级的东西（剪贴板监听）要知道这个：
    /// 跑着任务的时候别去切页面 / 换链接。两个工作页共用一个后端，只可能有一单。
    /// </summary>
    public static bool AnyRunning { get; private set; }

    public void Start()
    {
        if (Running) return;
        AnyRunning = true;
        _seen = 0;
        _sawRunning = false;
        _done = false;
        ResultPath = ResultDir = ResultNote = EndStage = "";
        Running = true;
        Hook();
        _ = KickAsync();
    }

    public void Cancel()
    {
        if (!Running || _done) return;
        Line?.Invoke(new LogLine { Text = "⏹ 正在取消…", Level = LogLevel.Warn });
        _ = CancelAsync();
    }

    // ---------- 内部 ----------

    void Hook()
    {
        if (_hooked) return;
        Backend.StatusReceived += OnStatus;
        _hooked = true;
    }

    void Unhook()
    {
        if (!_hooked) return;
        Backend.StatusReceived -= OnStatus;
        _hooked = false;
    }

    async Task KickAsync()
    {
        var (method, args) = _plan();
        var r = await Backend.CallAsync(method, args, 60_000);
        if (r is not null) return;      // 开工了，后面全看后端推的状态

        // ⚠️ 这里**不能**默不作声：后端没起来 / 拒绝开工（没配 key、没选文件…）时
        //    必须当场说清楚，绝不能变成「界面在跑、其实什么都没发生」。
        _ui.TryEnqueue(() =>
        {
            if (_done) return;
            Line?.Invoke(new LogLine
            {
                Text = "❌ 没能开工：" + (Backend.LastError is { Length: > 0 } e ? e : "后端没有响应"),
                Level = LogLevel.Err,
            });
            EndStage = "error";
            ResultNote = Backend.LastError;
            Finish();
        });
    }

    async Task CancelAsync()
    {
        var r = await Backend.CallAsync("cancel", null, 20_000);
        if (r is not null) return;      // 后端收下了，它会自己把状态推成 cancelled
        _ui.TryEnqueue(() =>
        {
            if (_done) return;
            Line?.Invoke(new LogLine
            {
                Text = "❌ 取消没送到后端：" + Backend.LastError + "（任务可能还在跑）",
                Level = LogLevel.Err,
            });
            Finish();
        });
    }

    /// <summary>后端推状态的回调在它的读取线程上，碰界面必须回主线程。</summary>
    void OnStatus(JsonElement st)
    {
        if (_done) return;
        _ui.TryEnqueue(() => Apply(st));
    }

    void Apply(JsonElement st)
    {
        if (_done) return;

        // 日志：只推新增的行。后端每次发的是**最后 300 行的窗口**，并带上 `log_index`
        // （这一截第一行在全量日志里的绝对序号）。
        // ⚠️ 只按数组长度记账会静默停更：窗口填满 300 行之后长度永远是 300，
        //    `n == _seen` 一直成立 → 新行一行都推不出来，界面看着像卡死
        //    （2026-10-21 用户报「配音合成卡住了」，实际是日志停在第 300 行）。
        if (st.TryGetProperty("log", out var log) && log.ValueKind == JsonValueKind.Array)
        {
            int n = log.GetArrayLength();
            int start = st.TryGetProperty("log_index", out var li) && li.TryGetInt32(out var si) ? si : 0;
            if (start > _seen) _seen = start;      // 前面的行已经被后端丢掉了，直接跳到窗口开头
            int from = _seen - start;
            if (from < 0) from = 0;
            var arr = log.EnumerateArray().ToArray();
            for (int i = from; i < n; i++)
            {
                if (arr[i].ValueKind == JsonValueKind.String)
                    Line?.Invoke(new LogLine { Text = arr[i].GetString() ?? "", Level = Classify(arr[i].GetString() ?? "") });
            }
            _seen = start + n;
        }

        var stage = Str(st, "stage");

        // 阶段为空 = 后端刚起来那份空闲状态，别拿它去刷进度条（会把「准备中」顶掉）
        if (stage.Length > 0 && st.TryGetProperty("percent", out var pe) && pe.TryGetInt32(out var pct))
            Progress?.Invoke(stage, pct);

        if (st.TryGetProperty("queue", out var q) && q.ValueKind == JsonValueKind.Array)
        {
            var list = new List<QueueItem>();
            foreach (var it in q.EnumerateArray())
                list.Add(new QueueItem
                {
                    Title = Str(it, "title"),
                    Status = Str(it, "status") is { Length: > 0 } s ? s : "pending",
                    Detail = Str(it, "detail"),
                });
            Queue?.Invoke(list);
        }

        // 还在跑就别收尾（哪怕这一推里已经带了点结果）
        if (st.TryGetProperty("running", out var run) && run.ValueKind == JsonValueKind.True)
        {
            _sawRunning = true;
            return;
        }

        // ⚠️⚠️ 「不在跑」**不等于**「这一单跑完了」。后端刚起来时会先推一份**空闲**状态
        //     （running=false / stage 为空），而这一份很可能比我们的开工请求先到 ——
        //     早先就是这么翻车的：点「开始翻译」的瞬间界面直接显示「全部完成、100%」，
        //     其实任务刚开跑，而且我们已经退订，后面真进度一条都收不到（2026-10-14 截图核对时抓到）。
        //     判据：见过它「在跑」，或者状态本身就是三种收尾之一。
        bool ended = stage is "done" or "error" or "cancelled";
        if (!_sawRunning && !ended) return;

        EndStage = stage switch
        {
            "error" => "error",
            "cancelled" => "cancelled",
            _ => "done",
        };

        if (st.TryGetProperty("result", out var res) && res.ValueKind == JsonValueKind.Object)
        {
            foreach (var k in new[] { "final", "dubbed", "srt", "video", "out_dir" })
            {
                var v = Str(res, k);
                if (v.Length > 0) { ResultPath = v; break; }
            }
            // 「打开输出目录」要的是**目录**：单个视频时上面挑到的是成片文件，
            // 得单独记一份 out_dir（否则按钮写着目录、打开的是视频 —— 用户 2026-10-14 反馈）
            ResultDir = Str(res, "out_dir");
            if (res.TryGetProperty("batch", out var b) && b.ValueKind == JsonValueKind.True)
                ResultNote = $"成功 {Num(res, "ok")}，失败 {Num(res, "failed")}";
        }
        if (EndStage == "error") ResultNote = Str(st, "error");
        else if (EndStage == "cancelled") ResultNote = "已取消";

        Finish();
    }

    void Finish()
    {
        if (_done) return;
        _done = true;
        Running = false;
        AnyRunning = false;
        if (EndStage.Length == 0) EndStage = "done";
        Unhook();
        Finished?.Invoke();
    }

    static string Str(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

    static int Num(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.TryGetInt32(out var n) ? n : 0;

    /// <summary>
    /// 日志分级：和老界面（web/app.js 的 classify）**一模一样**的规则，
    /// 免得同一行日志在两个界面上颜色不同。
    /// </summary>
    static readonly Regex ZeroFailure = new(@"失败\s*[:：]?\s*0(?!\d)", RegexOptions.Compiled);

    static LogLevel Classify(string s)
    {
        // 「失败 0」不算错误：批量收尾那行是「🎉 批量完成：成功 5，失败 0」，
        // 按关键字判会把全成功也标成红色（2026-10-21 用户反馈红色容易被当成报错）。
        var t = ZeroFailure.Replace(s ?? "", "");
        if (Regex.IsMatch(t, "❌|失败|出错|错误|error|exception|traceback", RegexOptions.IgnoreCase)) return LogLevel.Err;
        if (Regex.IsMatch(t, "⚠|warning|警告|跳过", RegexOptions.IgnoreCase)) return LogLevel.Warn;
        if (Regex.IsMatch(t, "✅|🎉|完成|就绪|已保存|已生成")) return LogLevel.Ok;
        if (Regex.IsMatch(t, @"\d+(\.\d+)?%")) return LogLevel.Progress;
        return LogLevel.Normal;
    }
}
