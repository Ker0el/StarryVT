using System.Collections.ObjectModel;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using Starry.Vt.App.Fakes;
using Starry.Vt.App.Services;
using Windows.UI;

namespace Starry.Vt.App.Pages;

public sealed partial class HomePage : Page
{
    /// <summary>后端自检的**完整**清单（已经初始化过、或者点「重新检测」时全显示）</summary>
    readonly List<SetupStep> _all = [];

    /// <summary>列表里现在真的显示的（首次初始化时是一条条长出来的）</summary>
    readonly ObservableCollection<SetupStep> _steps = [];

    bool _busy;
    bool _showAll;      // true = 显示完整报告（查状态），false = 首次初始化（只列要办的）

    /// <summary>
    /// 首次初始化**只办这几件**（顺序就是处理的顺序）。其余都是"看一眼"的信息
    /// （程序装在哪、磁盘剩多少、输出目录…），放进「重新检测」的完整报告里看 ——
    /// 第一次打开就铺十三行，用户只会一脸懵（用户 2026-10-14 定）。
    /// 另有：报告里任何**不正常**的项也都会列出来，不管它叫什么。
    /// </summary>
    static readonly string[] SetupKeys = ["tools", "engine", "models", "downloader", "keys"];

    public HomePage()
    {
        InitializeComponent();
        Steps.ItemsSource = _steps;
        // ⚠️ 两个条件**都要**（2026-10-16 修）：`IsInitialized` 只是「向导跑过一遍」，
        //    它**在引擎那一步失败时也照样写盘**（`initialized.txt`）。
        //    只看它的话，引擎没装好的用户此后每次启动，这个按钮都写着「进入本地翻译」，
        //    一点就被送进翻译页 —— 一点开始就报错（2026-10-15 客户实例就是这么来的）。
        _btnAction = (Mock.IsInitialized && Mock.EngineReady) ? "enter" : "setup";
        if (_btnAction == "enter") ShowEnterState();
        else StartPulse();                           // 还没初始化：外面套三层会呼吸的光
        SetStageVisible();                           // 没东西（还没查）时列表藏起来，只留提示
        // 页面实例要**缓存复用**：不然每次切页都是新实例，翻译中的日志 / 队列 / 进度
        // 一离开就全没了（用户 2026-10-14 实测：翻译中切到设置再回来，结果没了）
        NavigationCacheMode = Microsoft.UI.Xaml.Navigation.NavigationCacheMode.Required;
        // ⚠️ 事件**进出页面时挂/摘**：实例缓存复用了，只在构造里挂一次的话，
        //    切走一次就永久断了（装引擎的进度再也不会刷上来）
        Loaded += async (_, _) =>
        {
            Backend.StatusReceived -= OnBackendStatus;   // 防重复挂
            Backend.StatusReceived += OnBackendStatus;
            await LoadHomeRowAsync();                    // 「装到哪」那行（要读设置）
            await LoadRealEnvAsync();                    // 这条路可能改 _btnAction
            UpdateHomeRow();                             // 所以放它后面
        };
        Unloaded += (_, _) => Backend.StatusReceived -= OnBackendStatus;
    }

    /// <summary>列表空的时候别让那块地方空着 —— 空列表 + 一句提示，比一片空白清楚</summary>
    void SetStageVisible()
    {
        bool any = _steps.Count > 0;
        EmptyHint.Visibility = any ? Visibility.Collapsed : Visibility.Visible;
        Steps.Visibility = any ? Visibility.Visible : Visibility.Collapsed;
    }

    /// <summary>已经初始化过：按钮直接是「进入本地翻译」</summary>
    void ShowEnterState()
    {
        RecheckBtn.Visibility = Visibility.Visible;   // 配过了，「重新检测」这时才有意义
        SetupLabel.Text = "进入本地翻译";
        SetupIcon.Glyph = "";
        SummaryText.Text = "环境上次已经配好了，直接进去用就行";
        FootHint.Text = "以后启动都直接进本地翻译";
        StopPulse();
    }

    // ---------- 「一键初始化」外面那三层会呼吸的光 ----------

    Storyboard? _pulse;

    void StartPulse()
    {
        try
        {
            var accent = Application.Current.Resources.TryGetValue("SystemAccentColor", out var o) && o is Color c
                ? c : Color.FromArgb(0xFF, 0xFF, 0x6F, 0xB5);

            // 光晕的颜色按主题色来（三层渐隐，中间浓、边上透明，看着才是一团光）
            if (SetupGlow.Background is RadialGradientBrush grad && grad.GradientStops.Count >= 3)
            {
                grad.GradientStops[0].Color = Color.FromArgb(0x4D, accent.R, accent.G, accent.B);
                grad.GradientStops[1].Color = Color.FromArgb(0x1F, accent.R, accent.G, accent.B);
                grad.GradientStops[2].Color = Color.FromArgb(0x00, accent.R, accent.G, accent.B);
            }

            _pulse = new Storyboard();

            var ease = new CubicEase { EasingMode = EasingMode.EaseInOut };
            var fade = new DoubleAnimation
            {
                From = 0.35, To = 1.0,
                Duration = new Duration(TimeSpan.FromMilliseconds(1300)),
                AutoReverse = true,
                RepeatBehavior = RepeatBehavior.Forever,
                EasingFunction = ease,
                EnableDependentAnimation = true,
            };
            Storyboard.SetTarget(fade, SetupGlow);
            Storyboard.SetTargetProperty(fade, "Opacity");
            _pulse.Children.Add(fade);

            foreach (var prop in new[] { "ScaleX", "ScaleY" })
            {
                var a = new DoubleAnimation
                {
                    From = 1, To = 1.045,
                    Duration = new Duration(TimeSpan.FromMilliseconds(1300)),
                    AutoReverse = true,
                    RepeatBehavior = RepeatBehavior.Forever,
                    EasingFunction = ease,
                    EnableDependentAnimation = true,
                };
                Storyboard.SetTarget(a, GlowScale);
                Storyboard.SetTargetProperty(a, prop);
                _pulse.Children.Add(a);
            }
            _pulse.Begin();
        }
        catch (Exception e) { App.Dump("呼吸光失败（不影响按钮）: " + e.Message); }
    }

    void StopPulse()
    {
        try
        {
            _pulse?.Stop();
            SetupGlow.Opacity = 0;
        }
        catch { }
    }

    /// <summary>有真后端就用真的自检结果（doctor.check），没有就什么都不显示（别摆假数据）。</summary>
    /// <param name="render">false = 只把报告取回来（重新检测要先取最新报告，再由 RunSetupAsync 一条条长出来）</param>
    async Task LoadRealEnvAsync(bool render = true)
    {
        if (!Backend.Start()) return;
        var rep = await Backend.CallAsync("env_report", new { deep = false });
        if (rep is null || !rep.Value.TryGetProperty("items", out var arr)) return;

        _all.Clear();
        foreach (var it in arr.EnumerateArray()) _all.Add(MapItem(it));

        // 引擎那一条真的过了 → 记下来并解禁导航栏。
        // ⚠️ **必须放在下面那两个分支之外**：老用户（initialized.txt 已存在）走的是上面那个 if，
        //    根本走不到下面的 else if。放进去的话，升级上来的用户明明引擎完好，导航栏却灰着，
        //    非得重跑一遍一键初始化才解锁（自己实测撞到过）。
        if (Mock.EngineReady || _all.Any(s => s.Key == "engine" && s.WasOk))
        {
            Mock.EngineReady = true;
            MainWindow.Instance?.SetNavLocked(false);
        }

        if (!render) return;

        // 已经配好过 → 进来就是完整报告（这是「查状态」）
        if (Mock.IsInitialized) _showAll = true;
        // 没记过标记、但后端自检**样样都正常** → 也当成配好了，别让用户白点一遍
        // （标记只在跑完一键初始化时写；这条是给"自己把环境装齐了"的人兜底的）
        else if (_all.Count > 0 && _all.All(s => s.WasOk))
        {
            _showAll = true;
            _btnAction = "enter";      // ⚠️ 必须和 ShowEnterState() 一起改 ——
                                       //    只改文案不改这个，按钮写着「进入本地翻译」、
                                       //    点下去却当成没配好又跑一轮初始化（同一类病，2026-10-16）
            ShowEnterState();
        }
        RenderSteps();
        // ⚠️ 别把自检报告的结论写在标题上：「还没点就报一切正常」，很怪（用户实测）。
        // 结论等跑完一键初始化再显示。
    }

    /// <summary>自检报告的一项 → 界面上一行</summary>
    static SetupStep MapItem(JsonElement it)
    {
        var title = Str(it, "title");
        var detail = Str(it, "detail");
        var key = Str(it, "id");
        bool ok = Str(it, "state") == "ok";
        var fix = Str(it, "fix");
        var fixLabel = Str(it, "fix_label");
        // 「去设置」这类要用户自己动手的，初始化流程里跳过、标成待处理
        var kind = fix == "settings" && !ok ? "manual" : "auto";

        return new SetupStep
        {
            Key = key,
            Title = title,
            Todo = detail,
            WasOk = ok,
            Kind = kind,
            Fix = fix,
            FixLabel = ok ? "" : (fixLabel is { Length: > 0 } l ? l : "去处理"),
            // ★★ `State` 必须在这儿设（2026-10-16 修）。
            //    以前这里**只设了 WasOk、没设 State**，而 State 的默认值是 `"todo"`（空心点）——
            //    于是**老用户打开软件直接看到的那份自检报告，一行勾都没有**：
            //    数据层面明明知道「这项是好的」（WasOk），界面上却没人把它翻译成勾。
            //    只有「点过一键初始化/重新检测」跑一遍的那条路才会打勾（State 是跑的时候设的）——
            //    所以这个 bug 只在「没跑过、直接看报告」的机器上出现，用户报的就是这个。
            //    判据和 `RunSetupAsync` 收尾时保持一致：好 = ok，不好 = warn。
            State = ok ? "ok" : "warn",
        };
    }

    /// <summary>把「现在该显示的那批」摆进列表：完整报告 or 空（等一键初始化一条条长出来）</summary>
    void RenderSteps()
    {
        _steps.Clear();
        if (_showAll) foreach (var s in _all) _steps.Add(s);
        SetStageVisible();
    }

    /// <summary>
    /// 首次初始化要办的那几件：必办的（见 SetupKeys）+ 报告里任何不正常的项。
    /// 顺序按 SetupKeys 排，额外的排最后。
    /// </summary>
    List<SetupStep> SetupList()
    {
        var pick = _all.Where(s => SetupKeys.Contains(s.Key) || !s.WasOk);
        return [.. pick.OrderBy(s =>
        {
            int i = Array.IndexOf(SetupKeys, s.Key);
            return i < 0 ? SetupKeys.Length : i;
        })];
    }

    /// <summary>新的一条从这里「长」出来（先出现，紧接着就去验它）</summary>
    async Task AddGrowAsync(SetupStep s)
    {
        var isFirst = _steps.Count == 0;
        _steps.Add(s);
        if (isFirst) SetStageVisible();
        await Task.Delay(60);                     // 等容器生出来再动它
        if (Steps.ContainerFromItem(s) is FrameworkElement row) Motion.GrowIn(row);
        await Task.Delay(180);                    // 让它长完再开始转圈，不然看着像一下就蹦出来的
    }

    static string Str(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() ?? "" : "";

    // ============================================================ 就地修（装引擎 / 更新下载器）

    SetupStep? _busyRow;                       // 正在修的那一行（进度刷在它身上）
    bool _busySawRunning;                      // 见过后端「在跑」没有（见 OnBackendStatus）
    TaskCompletionSource<bool>? _jobDone;      // 修完叫醒 RunFixAsync
    string _jobStage = "";
    string _jobError = "";

    // 2026-10-16 加：去掉确认框之后，「停止」成了唯一的中止出口，需要这三个状态
    const string GlyphStop = "";         // 实心方块（Segoe MDL2 的 Stop）
    bool _inSetup;                             // 整轮初始化/重新检测在跑（**和 _busy 不是一回事**）
    bool _stopRequested;                       // 用户点过「停止」

    /// <summary>
    /// 这个按钮现在该干什么。**文案和点击行为都必须读它，不许各判各的。**
    ///
    /// ★★ 为什么要有这个字段（2026-10-16 用户实测踩到）：以前文案看一个条件、
    ///    `Setup_Click` 里看另一个条件，结果**按钮写着「继续」、点下去却进了翻译页** ——
    ///    因为 `IsInitialized && EngineReady` 在「引擎其实早装好了」的机器上恒为真，
    ///    跟他刚停没停过毫无关系。文案和行为只要不同源，就一定会出现这种自相矛盾。
    ///
    ///   "enter"  已经配好了，点它是「进去」
    ///   "setup"  首次初始化
    ///   "resume" 最后一轮被用户停了 → 接着装
    ///   "retry"  跑完了但引擎没装好 → 重新试
    /// </summary>
    string _btnAction = "setup";

    /// <summary>点「停止」：让后端把正在跑的那个任务取消掉</summary>
    /// <remarks>
    /// 后端 `cancel` 取消的是它手上那个 job（装引擎 / 更新下载器就是它）。
    /// 如果此刻**没有**任务在跑（比如刚跑完一项、正在检查下一项），这条调用什么也取消不了 ——
    /// 那种情况下靠 `RunSetupAsync` 循环里那句 `if (_stopRequested) break;` 自己停。
    /// 两条路都要有，缺一边就会出现「点了停止它还在往下跑」。
    /// </remarks>
    async Task StopSetupAsync()
    {
        if (_stopRequested || (!_inSetup && !_busy)) return;
        _stopRequested = true;
        SetupButton.IsEnabled = false;              // 防连点（取消是异步的）
        SetupIcon.Glyph = GlyphStop;
        SetupLabel.Text = "正在停止…";
        try { await Backend.CallAsync("cancel", null, 15_000); }
        catch { /* 取消不掉就让它自己跑完，这里绝不抛 */ }
    }

    // （原来这里有个 AskFixAsync：装引擎前弹「要现在装翻译引擎吗？/ 以后再说」。
    //   2026-10-16 删掉了 —— 理由写在 RunSetupAsync 里那个 if 上面。
    //   它的安全垫是「跑的时候按钮变『停止』」，见 Setup_Click / StopSetupAsync。）

    // ============================================================ 一键初始化

    /// <summary>读一次设置里的引擎数据目录，显示成「引擎和模型将装在 X 盘，约 15 GB」</summary>
    async Task LoadHomeRowAsync()
    {
        try
        {
            string home = "";
            var r = await Backend.CallAsync("boot", null, 30_000);
            if (r is { } rr && rr.TryGetProperty("settings", out var s)
                && s.TryGetProperty("finesub_home", out var h) && h.ValueKind == JsonValueKind.String)
                home = h.GetString() ?? "";
            if (home.Length == 0)
                home = System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FineSub");
            var drive = (System.IO.Path.GetPathRoot(home) ?? "").TrimEnd('\\', '/', ':');
            HomeLabel.Text = drive.Length > 0
                ? $"引擎和模型将装在 {drive} 盘，约 15 GB"
                : "引擎和模型约 15 GB";
            ToolTipService.SetToolTip(HomeLabel, home);      // 完整路径鼠标悬停能看到
        }
        catch (Exception e) { App.Dump("读引擎数据目录失败: " + e.Message); }
    }

    /// <summary>「装到哪」那行的「去设置」：**不在这屏换盘**（用户定），把想改的人送到
    /// 设置页的那一项去 —— 首页没有误点换盘的坑，但想知道/想改的人找得到路。</summary>
    void GoSettings_Click(object sender, RoutedEventArgs e)
        => MainWindow.Instance?.NavigateTo("settings");

    /// <summary>「装到哪」那一行只在**还没配好**时显示 —— 只告知装到哪，不给换
    /// （2026-10-16 用户定：初始化这一屏不放换盘入口，要换去设置页，那儿改前会确认）</summary>
    void UpdateHomeRow()
    {
        try { HomeRow.Visibility = _btnAction == "enter" ? Visibility.Collapsed : Visibility.Visible; }
        catch { }
    }

    async void Setup_Click(object sender, RoutedEventArgs e)
    {
        // 跑着的时候这个按钮就是「停止」—— 不是禁用，用户随时能中止。
        // ⚠️ 判据里 `_inSetup` 不能少：整轮初始化期间 `_busy` 是会来回翻的
        //    （每项修完它就置回 false），只看 `_busy` 就会在那一瞬间被再点一次，
        //    开出第二轮初始化。
        if (_inSetup || _busy) { await StopSetupAsync(); return; }
        // ★ 只认 `_btnAction`（按钮的文案就是按它写的）—— **不要在这里另判一套条件**。
        //   以前这里判的是 `IsInitialized && EngineReady`，而那两个在「引擎早就装好了」
        //   的机器上恒为真，跟「刚停没停过」毫无关系 —— 于是**按钮写着「继续」、
        //   点下去却进了翻译页**（2026-10-16 用户实测）。文案和动作必须同源。
        if (_btnAction == "enter")
        {
            MainWindow.Instance?.NavigateTo("local");    // 确实配好了，这个按钮就是「进去」
            return;
        }
        await RunSetupAsync();       // setup / resume / retry 三种都是「跑一轮初始化」
    }

    /// <summary>一项一项来：转圈 → 打勾，每项至少亮 500ms，不然一闪而过看不出在干什么。</summary>
    /// <param name="full">
    /// true = 「重新检测」：把自检报告里**每一项**都过一遍（含"看一眼"的那些信息项）；
    /// false = 首次「一键初始化」：只办要办的那几件 + 报告里不正常的项。
    /// </param>
    async Task RunSetupAsync(bool full = false)
    {
        // 首次进来清单是空的 → 用「要办的那几件」；已经查过再点，就按现在显示的这批重跑。
        // 然后**一条条长出来**，长一个验一个（不是一口气全铺上去）。
        // ⚠️ 重新检测也得走这条动画：以前它是一次性把报告铺出来的，用户看不到"一项项打勾"
        //    （2026-10-14 用户实测反馈）
        // ★「继续」要**真的接着**（2026-10-16 用户实测：中途点继续，看到的是从第一项重跑，
        //   以为白下了一遍）。底层其实一直是接着的 —— uv 装过就跳过、wheel 有缓存、
        //   模型按哈希校验跳过已下好的；「看起来从头来」纯粹是这里把列表清了重长。
        //   所以做法是：**已经打勾的那些留在列表里不动**，只重跑没打勾的 + 压根没轮到过的。
        bool resuming = _btnAction == "resume" && _steps.Count > 0;
        List<SetupStep> list;
        if (full)
        {
            list = _all.ToList();
        }
        else if (resuming)
        {
            var processed = _steps.Select(s => s.Key).ToHashSet();
            var redo = _steps.Where(s => s.State != "ok").ToList();      // 跑过但没成
            for (int i = _steps.Count - 1; i >= 0; i--)
                if (_steps[i].State != "ok") _steps.RemoveAt(i);         // 摘掉，等下重新长出来
            var fresh = SetupList().Where(s => !processed.Contains(s.Key));  // 上一轮压根没轮到
            list = [.. redo, .. fresh];
            // 没有要补的（极少见：正好在最后一项上按的停止）→ 退回把整份检查一遍
            if (list.Count == 0) { resuming = false; list = SetupList(); }
        }
        else
        {
            list = _steps.Count > 0 ? _steps.ToList() : SetupList();
        }
        if (list.Count == 0)
        {
            // 一项都没有 = 自检报告没拿到（后端没起来）。别把按钮切成「进入本地翻译」装作跑完了
            SummaryText.Text = "没拿到自检结果，稍等一会儿再点一次";
            Motion.Reveal(SummaryText, 6, 240);
            return;
        }

        _inSetup = true;
        _stopRequested = false;
        _busy = true;
        RecheckBtn.IsEnabled = false;
        // ★ 这时候那个按钮是**「停止」**，不是禁用的（2026-10-16 起）：
        //   去掉「要现在装引擎吗」那个确认框之后，这里就是用户唯一的中止出口 ——
        //   误点、或者在用手机流量的人，随时能停。
        //   ⚠️ 同时必须靠 `_inSetup` 挡住重复进入：按钮一旦可点，
        //      `Setup_Click` 就会在「上一轮还在跑」的时候被再点一次。
        BusyRing.IsActive = false;
        BusyRing.Visibility = Visibility.Collapsed;
        SetupIcon.Visibility = Visibility.Visible;
        SetupIcon.Glyph = GlyphStop;
        SetupButton.IsEnabled = true;
        SetupLabel.Text = "停止";
        FootHint.Text = full ? "一项一项过一遍" : "盯着看，一项一项来";

        RunBar.Visibility = Visibility.Visible;
        RunBar.Value = 0;
        Motion.Reveal(RunBar, 6, 220);

        // 接着跑的时候**不清列表** —— 已经打勾的那几行留在原地，用户才看得出「是接着来的」，
        // 而不是「又从第一项开始了」。清列表的那套只在「从头跑一遍」时走。
        if (!resuming)
        {
            _steps.Clear();
            _showAll = false;
            SetStageVisible();
        }

        RunBar.Maximum = Math.Max(1, list.Count);

        int done = 0;
        foreach (var s in list)
        {
            await AddGrowAsync(s);                      // 先长出来，紧接着就验它

            if (s.Kind == "manual")                     // 要你自己填的，直接标出来跳过
            {
                s.State = "warn";
                s.Detail = s.Todo;
                done++;
                RunBar.Value = done;
                await Task.Delay(200);
                continue;
            }

            s.State = "doing";
            s.Detail = "正在检查…";
            var t0 = DateTime.Now;

            var (ok, detail) = await VerifyAsync(s);

            // 后端能自己搞定的（装引擎、更新下载器），缺了就**直接装，不再弹确认框**。
            //
            // ★★ 2026-10-16 去掉的（用户定），理由三条：
            //   ① 用户点「一键初始化」**本身就是同意** —— 再问「要不要装引擎」等于问「你要不要初始化」。
            //   ② 那个框的「以后再说」对小白是**死胡同**：点了之后这一项被标成需要注意，
            //      他得自己找到这一行右边的按钮才能继续 —— 小白根本找不到，然后就以为软件坏了。
            //   ③ 下载过程本来就看得见（进度刷在行上、日志一行行涨），不是背着人下。
            //   安全垫换成了「跑的时候那个按钮变成『停止』」（见 Setup_Click）——
            //   误点、或者在用手机流量的人，随时能停。
            if (!ok && s.Fix is "engine" or "downloader")
            {
                s.Detail = detail;
                (ok, detail) = await RunFixAsync(s.Fix, s, fromSetup: true);
            }

            // 用户中途点了「停止」→ 这一轮到此为止，别再往下跑剩下的项
            if (_stopRequested) break;

            int left = 500 - (int)(DateTime.Now - t0).TotalMilliseconds;
            if (left > 0) await Task.Delay(left);

            s.State = ok ? "ok" : "warn";
            s.Detail = detail;
            done++;
            RunBar.Value = done;
            await Task.Delay(160);                      // 勾出来之后顿一下，看得清
        }

        _busy = false;
        _inSetup = false;                               // 这一轮结束了，按钮不再当「停止」用
        bool stopped = _stopRequested;                  // 用户中途点了停止
        _stopRequested = false;
        // ⚠️ 解禁导航栏看的是**引擎真的好了**，不是「向导跑过一遍」：
        //    上面那个循环里任何一步失败都只标 warn、不中断，照样会走到这一行。
        //    引擎没装好就放用户去点「开始翻译」，只会撞一鼻子灰（客户实例见 Mock.EngineReady）。
        bool engineOk = _steps.Any(s => s.Key == "engine" && s.State == "ok");
        if (engineOk)
        {
            Mock.EngineReady = true;
            MainWindow.Instance?.SetNavLocked(false);
        }
        // ⚠️ 「初始化过了」也必须**引擎真的好了**才算（2026-10-16）：
        //    以前这里是**无条件** `Mock.IsInitialized = true`，于是引擎那项失败时
        //    按钮照样变成「进入本地翻译」，用户一点就被送进翻译页 —— 而引擎没装好，
        //    只会撞一鼻子灰。磁盘不够被拦下来、或者安装报错，走的都是这条路。
        //    停掉的同样不算。
        bool ready = engineOk && !stopped;
        if (ready) Mock.IsInitialized = true;           // 记下来：以后启动直接进本地翻译
        RecheckBtn.Visibility = Visibility.Visible;     // 这一遍跑完，「重新检测」才露出来
        RecheckBtn.IsEnabled = true;
        StopPulse();                                    // 光圈收掉，别再抢注意力

        BusyRing.IsActive = false;
        BusyRing.Visibility = Visibility.Collapsed;
        SetupIcon.Glyph = "\uE768";                     // 变成播放图标
        SetupIcon.Visibility = Visibility.Visible;
        // ★ 按钮的状态在**这一处**定死，文案和点击行为都读它（见上面 _btnAction 的说明）：
        //   · 引擎好了          → 「进入本地翻译」
        //   · 被用户停掉的      → 「继续」（再点确实是接着装：已下好的按哈希校验会跳过，
        //                        不会从头再来；叫「一键初始化」会让人以为白下一遍）
        //   · 跑完了但引擎没弄好 → 「重新试一次」（别叫人「进入本地翻译」，他进不去）
        _btnAction = ready ? "enter" : (stopped ? "resume" : "retry");
        SetupLabel.Text = _btnAction switch
        {
            "enter" => "进入本地翻译",
            "resume" => "继续",
            _ => "重新试一次",
        };
        SetupButton.IsEnabled = true;
        if (stopped) FootHint.Text = "点「继续」接着装，已经下好的不会重下";
        UpdateHomeRow();          // 配好了就把「装到哪」那行收掉

        int warn = _steps.Count(x => x.State == "warn");
        int good = _steps.Count - warn;
        SummaryText.Text = full
            ? (warn == 0
                ? $"检测完成：{good} 项都正常"
                : $"检测完成：{good} 项正常，有 {warn} 项要注意（点每行右边的按钮）")
            : (warn == 0
                ? "初始化完成，以后启动直接进本地翻译"
                : $"主要的事都弄好了，还有 {warn} 项要你自己处理（点每行右边的按钮）");
        Motion.Reveal(SummaryText, 6, 240);
        FootHint.Text = full ? "想看状态随时点「重新检测」" : "以后不用再做这一步";
    }

    /// <summary>
    /// 单项检查：能问后端的就问后端（引擎 / 下载器是真实检测），
    /// 其它项沿用自检报告的结论。样机阶段不主动触发下载安装。
    /// </summary>
    async Task<(bool ok, string detail)> VerifyAsync(SetupStep s)
    {
        try
        {
            // 按 id 认，不按标题认：标题改字（比如「引擎运行环境与模型」）就认不出来了
            if (s.Key == "engine")
            {
                var r = await Backend.CallAsync("engine_status", null, 20_000);
                if (r is not null)
                {
                    bool ok = !(r.Value.TryGetProperty("ok", out var o) && o.ValueKind == JsonValueKind.False);
                    var d = Str(r.Value, "detail");
                    if (d.Length == 0) d = Str(r.Value, "version");
                    return (ok, d.Length > 0 ? d : ok ? "翻译引擎已就绪" : "引擎还没就绪");
                }
            }
            else if (s.Key == "downloader")
            {
                var r = await Backend.CallAsync("downloader_state", null, 20_000);
                if (r is not null)
                {
                    var v = Str(r.Value, "version");
                    return (true, "下载器已就绪");
                }
            }
        }
        catch (Exception e)
        {
            App.Dump("初始化单项检查失败: " + e.Message);
        }

        return (s.WasOk, string.IsNullOrWhiteSpace(s.Todo) ? "检查过了，没问题" : s.Todo);
    }

    /// <summary>让后端动手，进度从它推的状态里刷到这一行上；装完再问后端一次，不自己判成功。</summary>
    /// <param name="fromSetup">
    /// true = 从「一键初始化」那个循环里调进来的。这时候**按钮归外面管**（整轮都显示「停止」），
    /// 这里就不要再动它的启用状态和文案 —— 否则初始化还没跑完，按钮先被改回可点的别的样子，
    /// 用户一点就重复进入一轮初始化（这条缝以前就存在，2026-10-16 顺手堵掉）。
    /// </param>
    async Task<(bool ok, string detail)> RunFixAsync(string kind, SetupStep row, bool fromSetup = false)
    {
        _busy = true;
        _busyRow = row;
        _busySawRunning = false;
        _jobStage = _jobError = "";
        var prevLabel = SetupLabel.Text;               // 单点进来的：跑完把文案还回去
        if (!fromSetup)
        {
            // 单点某一行进来的（点那行右边的按钮）——这时候也得让用户能停
            BusyRing.IsActive = false;
            BusyRing.Visibility = Visibility.Collapsed;
            SetupIcon.Visibility = Visibility.Visible;
            SetupIcon.Glyph = GlyphStop;
            SetupButton.IsEnabled = true;
            SetupLabel.Text = "停止";
        }
        row.State = "doing";
        row.Detail = kind == "engine" ? "正在安装翻译引擎…" : "正在更新下载器…";

        try
        {
            var r = await Backend.CallAsync("env_fix", new { kind }, 60_000);
            if (r is null)
                return Settle(row, false, "没能开始：" + (Backend.LastError is { Length: > 0 } txt ? txt : "后端没有响应"));

            _jobDone = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            if (await Task.WhenAny(_jobDone.Task, Task.Delay(45 * 60_000)) != _jobDone.Task)
                return Settle(row, false, "等太久没动静了，去日志里看看");

            if (_jobError.Length > 0) return Settle(row, false, _jobError);
            if (_jobStage == "cancelled") return Settle(row, false, "已取消");

            var (ok, detail) = await VerifyAsync(row);
            return Settle(row, ok, detail);
        }
        finally
        {
            _jobDone = null;
            _busyRow = null;
            _busy = false;
            BusyRing.IsActive = false;
            BusyRing.Visibility = Visibility.Collapsed;
            // 从初始化循环进来的：按钮归外面管（整轮都写着「停止」），这里一个字都别动
            if (!fromSetup)
            {
                SetupButton.IsEnabled = true;
                SetupIcon.Glyph = "";      // 播放（进入本地翻译）
                SetupLabel.Text = prevLabel;
            }
        }

        // 修完必须自己把这一行落定 —— 从「检查」那条路进来时外面还会再写一遍（重复写没影响），
        // 但从行内按钮 / 自测进来时**没人替它写**，不收尾就会一直挂着转圈（2026-10-14 实测）。
        static (bool ok, string detail) Settle(SetupStep row, bool ok, string detail)
        {
            row.State = ok ? "ok" : "warn";
            row.Detail = detail;
            return (ok, detail);
        }
    }

    /// <summary>后端推来的状态：装引擎 / 更新下载器时，把最新一行刷到那一行上。</summary>
    /// <remarks>这个回调在 Backend 的读取线程上，碰界面必须回主线程。</remarks>
    void OnBackendStatus(JsonElement st)
    {
        DispatcherQueue.TryEnqueue(() =>
        {
            if (st.TryGetProperty("running", out var r) && r.ValueKind == JsonValueKind.True)
            {
                _jobStage = Str(st, "stage");
                if (_busyRow is not null && _jobStage is "engine" or "ytdlp" or "update")
                {
                    if (st.TryGetProperty("log", out var log) && log.ValueKind == JsonValueKind.Array
                        && log.GetArrayLength() > 0)
                    {
                        var line = log[log.GetArrayLength() - 1].GetString() ?? "";
                        if (line.Length > 0) _busyRow.Detail = line;
                    }
                    if (st.TryGetProperty("percent", out var p) && p.TryGetInt32(out var pct))
                        RunBar.Value = Math.Min(RunBar.Maximum, RunBar.Maximum * pct / 100.0);
                }
                _busySawRunning = true;
                return;
            }

            // ⚠️ 「没在跑」不等于「这一单跑完了」—— 后端刚起来那份空闲状态也是 running=false，
            //    所以必须「见过它在跑」才算收工（同 Services/Runner.cs 里那条）。
            _jobError = Str(st, "error");
            _jobStage = Str(st, "stage");
            if (_busySawRunning) _jobDone?.TrySetResult(true);
        });
    }

    // ============================================================ 其它按钮

    /// <summary>
    /// 重新检测：先取一份最新报告，然后**跟首次初始化一样**一项项过、一项项打勾
    /// （以前这里是直接把报告铺出来，看着像"什么都没检查"，用户 2026-10-14 反馈）。
    /// 差别只在清单范围：这个走**全部**项，首次初始化只办要办的那几件。
    /// </summary>
    async void Recheck_Click(object sender, RoutedEventArgs e)
    {
        if (_busy) return;
        RunBar.Visibility = Visibility.Collapsed;
        await LoadRealEnvAsync(render: false);           // 只刷新 _all，别先把整张表铺出来
        await RunSetupAsync(full: true);
    }

    /// <summary>
    /// 自测入口（`--probe=fix:&lt;engine|downloader&gt;`）：走「就地修」那条路，只是不用鼠标点。
    /// 清单是后端自检回来才有的，所以先等它到（最多 20 秒）。
    /// </summary>
    internal async void SelfTest(string kind)
    {
        var kw = kind == "engine" ? "引擎" : "下载器";
        SetupStep? row = null;
        for (int i = 0; i < 40 && row is null; i++)
        {
            row = _steps.FirstOrDefault(x => x.Fix == kind)
                  ?? _steps.FirstOrDefault(x => x.Title.Contains(kw));
            if (row is null) await Task.Delay(500);
        }
        row ??= new SetupStep { Title = kw, Todo = "", Fix = kind };
        if (!_steps.Contains(row)) _steps.Add(row);
        await RunFixAsync(kind, row);
    }

    /// <summary>自测入口（`--probe=setup`）：把「一键初始化」跑一遍，不用鼠标点。</summary>
    internal async void SelfTestSetup()
    {
        for (int i = 0; i < 40 && _all.Count == 0; i++) await Task.Delay(500);   // 等自检回来
        await RunSetupAsync();
    }

    /// <summary>自测入口（`--probe=recheck`）：把「重新检测」跑一遍（应该一项项打勾，不是一下铺出来）</summary>
    internal async void SelfTestRecheck()
    {
        for (int i = 0; i < 40 && _all.Count == 0; i++) await Task.Delay(500);
        RunBar.Visibility = Visibility.Collapsed;
        await LoadRealEnvAsync(render: false);
        await RunSetupAsync(full: true);
    }

    void StepAction_Click(object sender, RoutedEventArgs e)
    {
        var tag = (sender as FrameworkElement)?.Tag as string;
        switch (tag)
        {
            case "settings":
                MainWindow.Instance?.NavigateTo("settings");
                break;
            case "recheck":
                Recheck_Click(sender, e);
                break;
            // 后端能自己干的：装引擎 / 更新下载器（进度从推送的状态来，见 OnBackendStatus）
            case "engine":
            case "downloader":
                var row = _steps.FirstOrDefault(x => x.Fix == tag);
                if (!_busy && row is not null) _ = RunFixAsync(tag, row);
                break;
        }
    }
}
