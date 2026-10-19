using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace Starry.Vt.App.Services;

/// <summary>
/// 后端进程（Python，JSON-RPC over stdio）。
///
/// 前端只负责界面，真正的活全在原来的 Python 里 —— 这个类就是那根线：
/// 拉起 `winui_backend.py`，按行发 JSON 请求，同时收它主动推过来的任务状态。
///
/// 为什么不用本地 HTTP：端口段被 Hyper-V 圈走一堆、还要过防火墙；stdio 一了百了，
/// 而且父进程一死，子进程自然跟着结束。
/// </summary>
public static class Backend
{
    static Process? _proc;
    static int _nextId;
    static readonly Dictionary<int, TaskCompletionSource<JsonElement>> Pending = new();
    static readonly StringBuilder Stderr = new();

    /// <summary>往 stdin 写请求要串行（见 CallAsync 里那段说明）</summary>
    static readonly SemaphoreSlim WriteLock = new(1, 1);

    public static bool Ready { get; private set; }
    public static string LastError { get; private set; } = "";
    public static string Version { get; private set; } = "";

    /// <summary>后端主动推的任务状态（{"event":"status","status":{...}} 里的 status）</summary>
    public static event Action<JsonElement>? StatusReceived;

    /// <summary>
    /// 启动时自动检查更新的结果（{"event":"update","info":{...}} 里的 info）。
    /// 后端在后台线程里查，查完主动推过来 —— 界面不用轮询。
    /// </summary>
    public static event Action<JsonElement>? UpdateReceived;

    /// <summary>拉起后端。找不到脚本/python 就返回 false，调用方自己退回假数据。</summary>
    public static bool Start()
    {
        if (_proc is { HasExited: false }) return true;

        var launch = ResolveLaunch();
        if (launch is null)
        {
            LastError = "找不到后端：既没有随包的 StarryVT.exe，也没有 python + winui_backend.py";
            return false;
        }

        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = launch.Value.File,
                Arguments = launch.Value.Args,
                WorkingDirectory = launch.Value.WorkDir,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
                // ⚠️ 不能用 Encoding.UTF8 —— 它带 BOM，StreamWriter 会在**第一行**前面
                //    写出 EF BB BF，后端 json.loads 直接不认（实测报
                //    "Unexpected UTF-8 BOM"），于是首个请求永远等不到答复、界面退回假数据。
                StandardInputEncoding = new UTF8Encoding(false),
                StandardOutputEncoding = new UTF8Encoding(false),
                StandardErrorEncoding = new UTF8Encoding(false),
            };
            _proc = Process.Start(psi);
            if (_proc is null) { LastError = "进程没起来"; return false; }

            _proc.OutputDataReceived += (_, e) => { if (e.Data is not null) OnLine(e.Data); };
            _proc.ErrorDataReceived += (_, e) => { if (e.Data is not null) Stderr.AppendLine(e.Data); };
            _proc.BeginOutputReadLine();
            _proc.BeginErrorReadLine();
            _proc.EnableRaisingEvents = true;
            _proc.Exited += (_, _) => Ready = false;
            return true;
        }
        catch (Exception e)
        {
            LastError = e.Message;
            return false;
        }
    }

    /// <summary>后端怎么拉起来：(可执行文件, 参数, 工作目录)。找不到返回 null。</summary>
    /// <remarks>
    /// ⚠️ 装了正式版的机器上**没有 Python**，所以首选是打包好的 StarryVT.exe —— 它内部
    /// 就带着整套运行时（流水线/引擎/配音/更新器），`--rpc` 就是「以后端身份启动」。
    /// 开发时才退回「python + 仓库里的脚本」那条路（见下面两个 Find*）。
    ///
    /// ⚠️⚠️ **绝不允许把「界面自己那个 exe」当成后端去拉**。踩过一次，后果是滚雪球：
    /// 本项目编译出来的 exe 也叫 StarryVT.exe，早先按「看界面目录里有没有 StarryVT.exe」
    /// 去找后端 → 拉到的是界面自己 → 新窗口又去拉一个 → 桌面上瞬间堆了 44 个窗口。
    /// 所以下面两条锁都要留着：
    ///   ① 候选目录只有 `backend\` 子目录（随包布局），不看界面自己所在的目录；
    ///   ② 命中前逐条比对自身进程路径，等于自己就跳过。
    /// </remarks>
    static (string File, string Args, string WorkDir)? ResolveLaunch()
    {
        var self = Environment.ProcessPath ?? "";
        var exeEnv = Environment.GetEnvironmentVariable("STARRYVT_BACKEND_EXE");
        foreach (var exe in new[]
                 {
                     exeEnv,
                     Path.Combine(AppContext.BaseDirectory, "backend", "StarryVT.exe"),  // 随包布局
                     @"D:\AI-Project\StarryVT\dist\StarryVT\StarryVT.exe",              // 开发：本机打的包
                 })
        {
            if (string.IsNullOrWhiteSpace(exe) || !File.Exists(exe)) continue;
            if (SamePath(exe!, self)) continue;          // 别把自己拉成后端（见上面的坑）
            return (exe!, "--rpc", Path.GetDirectoryName(exe!)!);
        }

        var script = FindScript();
        var py = FindPython();
        if (script is not null && py is not null && !SamePath(py, self))
            return (py, $"\"{script}\"", Path.GetDirectoryName(script)!);
        return null;
    }

    static bool SamePath(string a, string b)
    {
        try
        {
            return Path.GetFullPath(a).TrimEnd('\\')
                .Equals(Path.GetFullPath(b).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    static string? FindScript()
    {
        var cands = new List<string> { Path.Combine(AppContext.BaseDirectory, "winui_backend.py") };
        var env = Environment.GetEnvironmentVariable("STARRYVT_BACKEND");
        if (!string.IsNullOrWhiteSpace(env)) cands.Insert(0, env);
        // 开发时：exe 在 winui\StarryVt\bin\... 下，脚本在仓库根
        cands.Add(@"D:\AI-Project\StarryVT\winui_backend.py");
        return cands.FirstOrDefault(File.Exists);
    }

    static string? FindPython()
    {
        var env = Environment.GetEnvironmentVariable("STARRYVT_PYTHON");
        if (!string.IsNullOrWhiteSpace(env) && File.Exists(env)) return env;
        foreach (var p in new[]
                 {
                     @"C:\Users\Star\AppData\Local\Programs\Python\Python312\python.exe",
                     "python.exe", "python",
                 })
        {
            if (p.EndsWith(".exe") && !File.Exists(p)) continue;
            return p;
        }
        return null;
    }

    static void OnLine(string line)
    {
        JsonElement msg;
        try { msg = JsonDocument.Parse(line).RootElement.Clone(); }
        catch { return; }          // 后端偶尔打点别的，忽略

        if (msg.TryGetProperty("event", out var ev))
        {
            var name = ev.GetString();
            if (name == "ready")
            {
                Ready = true;
                if (msg.TryGetProperty("version", out var v)) Version = v.GetString() ?? "";
            }
            else if (name == "status" && msg.TryGetProperty("status", out var st))
            {
                StatusReceived?.Invoke(st);
            }
            else if (name == "update" && msg.TryGetProperty("info", out var up))
            {
                UpdateReceived?.Invoke(up);
            }
            return;
        }

        if (msg.TryGetProperty("id", out var idEl) && idEl.TryGetInt32(out var id))
        {
            lock (Pending)
            {
                if (Pending.Remove(id, out var tcs)) tcs.TrySetResult(msg);
            }
        }
    }

    /// <summary>等后端开口说话（它一起来就推 ready）。进程中途死掉也算失败。</summary>
    static async Task<bool> WaitReadyAsync(int ms)
    {
        for (int waited = 0; waited < ms; waited += 200)
        {
            if (Ready) return true;
            if (_proc is null || _proc.HasExited) return false;
            await Task.Delay(200);
        }
        return Ready;
    }

    /// <summary>临时排查用：往独立文件追加（App.Dump 是覆盖写的，追不了序列）</summary>
    static void Trace(string s)
    {
        if (Environment.GetEnvironmentVariable("STARRYVT_BACKEND_TRACE") != "1") return;
        try
        {
            System.IO.File.AppendAllText(
                Path.Combine(Path.GetTempPath(), "starryvt-about-trace.txt"),
                $"{DateTime.Now:HH:mm:ss.fff}  [be] {s}\n");
        }
        catch { }
    }

    /// <summary>调后端一个方法。失败（没起来 / 超时 / 后端报错）返回 null，调用方自己兜底。</summary>
    public static async Task<JsonElement?> CallAsync(string method, object? args = null, int timeoutMs = 120_000)
    {
        Trace($"进入 {method}");
        if (_proc is null && !Start()) return null;
        if (_proc is null || _proc.HasExited) return null;

        // 起来了 ≠ 拉对了。后端启动后会立刻推一条 {"event":"ready"}，等不到就说明
        // 拉起来的不是后端（历史上拉成了界面自己，桌面上堆了 44 个窗口）——收掉它。
        if (!Ready && !await WaitReadyAsync(20_000))
        {
            LastError = "后端起来了但没应答（多半拉错了可执行文件），已关闭";
            Stop();
            return null;
        }
        Trace($"就绪，准备写 {method}");

        int id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (Pending) Pending[id] = tcs;

        try
        {
            var payload = JsonSerializer.Serialize(new { id, method, @params = args ?? new { } });
            // ⚠️⚠️ **写这一行必须串行**：StandardInput 是同一个 StreamWriter，多路并发
            //     往它写会让两行 JSON 交叉在一起，后端解不出来（回一句"这行不是 JSON"），
            //     而我们的请求永远等不到答复 —— 表现成"随机有那么一两项没反应"。
            //     批量列表按行并发取缩略图时实测撞上（2026-10-14）：7 项里固定丢 1~2 项。
            await WriteLock.WaitAsync();
            Trace($"拿到写锁 {method}");
            try
            {
                await _proc.StandardInput.WriteLineAsync(payload);
                await _proc.StandardInput.FlushAsync();
            }
            finally { WriteLock.Release(); }
            Trace($"已发出 {method} id={id}");
        }
        catch (Exception e)
        {
            lock (Pending) Pending.Remove(id);
            LastError = e.Message;
            Trace($"发送失败 {method}: {e.Message}");
            return null;
        }

        var done = await Task.WhenAny(tcs.Task, Task.Delay(timeoutMs));
        Trace($"等到了 {method}（超时={done != tcs.Task}）");
        if (done != tcs.Task)
        {
            lock (Pending) Pending.Remove(id);
            LastError = $"{method} 超时";
            return null;
        }

        var res = await tcs.Task;
        Trace($"解析结果 {method}");
        if (!res.TryGetProperty("ok", out var ok) || !ok.GetBoolean())
        {
            LastError = res.TryGetProperty("error", out var err) ? err.GetString() ?? "后端报错" : "后端报错";
            return null;
        }
        return res.TryGetProperty("result", out var r) ? r : null;
    }

    public static string Diag() => $"ready={Ready} err={LastError}\n{Stderr}";

    public static void Stop()
    {
        try { _proc?.Kill(entireProcessTree: true); } catch { }
        _proc = null;
        Ready = false;
    }
}
