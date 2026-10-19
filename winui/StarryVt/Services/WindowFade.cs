using Microsoft.UI.Dispatching;
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Starry.Vt.App.Services;

/// <summary>
/// 整个窗口的渐隐渐现 —— 玻璃、标题栏、窗口阴影、内容**一起**淡，
/// 不是只淡 XAML 内容（那样后面的 Mica 会是「啪」地整块出现，很难看）。
///
/// 做法：给窗口挂 `WS_EX_LAYERED` + `SetLayeredWindowAttributes(LWA_ALPHA)`，
/// 由 DWM 把整窗按一个恒定 alpha 合成，我们再把这个 alpha 一帧一帧推上去。
///
/// ⚠️ 这条路**成立的前提是窗口有重定向表面**。实测本窗口的 GWL_EXSTYLE 里没有
///    `WS_EX_NOREDIRECTIONBITMAP`（0x110），所以 DWM 是按位图合成整窗的，alpha 生效。
///    要是哪天 WinUI 换了窗口实现、给这窗口加上那个样式，这里会**静默失效**
///    （alpha 设了没反应、窗口照样全亮）—— 所以 Attach 会回读样式确认。
///
/// ⚠️⚠️ **分层样式只在「正在淡」的那几百毫秒里挂着，淡完立刻摘掉。**
///    它是改窗口样式的操作，而分层窗口在拖放 / 硬件叠层 / 输入法这些路径上都可能有
///    自己的行为。本软件的看家功能就是「把文件拖进来」，真出了问题是查不出来的那种。
///    所以：启动渐现跑完 → <see cref="Detach"/>；要淡出之前 → 临时再挂上。
///    这样窗口 99.9% 的时间是个没被动过的普通窗口。
///
/// ⚠️ **绝不把窗口留在看不见的状态**：任何一步失败都回滚成原样，
///    调用方那就当没有淡入这回事。另有一条兜底见 MainWindow.StartFadeIn。
/// </summary>
public static class WindowFade
{
    const int GWL_EXSTYLE = -20;
    const long WS_EX_LAYERED = 0x00080000;
    const uint LWA_ALPHA = 0x00000002;

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW", SetLastError = true)]
    static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)]
    static extern IntPtr SetWindowLongPtr(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

    [DllImport("user32.dll", SetLastError = true)]
    static extern bool SetLayeredWindowAttributes(IntPtr hWnd, uint crKey, byte bAlpha, uint dwFlags);

    /// <summary>这个窗口能做淡入淡出（Enable 成功过）。false 时所有接口都是空操作。</summary>
    public static bool Enabled { get; private set; }

    static IntPtr _hwnd;
    static DispatcherQueue? _dq;
    static bool _attached;              // 当前挂着 WS_EX_LAYERED
    static double _alpha = 1.0;
    static int _lastSent = -1;          // 同一个字节数重复下发没意义，15ms 一帧里省点系统调用

    static DispatcherQueueTimer? _timer;
    static Stopwatch? _sw;
    static double _from, _to;
    static int _ms;
    static Action? _done;

    /// <summary>
    /// 启动渐现用：挂上分层样式并**立刻设成全透明**。
    /// ⚠️ 必须在窗口显示出来之前调（构造里），否则会先亮相一帧再淡，等于白做。
    /// </summary>
    public static bool Enable(IntPtr hwnd, DispatcherQueue dq)
    {
        // 开关：某些显卡/远程桌面环境可能出现异常（窗口一直在透明状态之类），
        // 设了它就完全不碰窗口样式。也是排查"到底是不是淡入害的"用的 A/B 开关。
        if (Environment.GetEnvironmentVariable("STARRY_NO_WINDOW_FADE") is { Length: > 0 })
            return false;

        _hwnd = hwnd;
        _dq = dq;
        if (!Attach(0)) return false;
        _alpha = 0.0;
        Enabled = true;
        return true;
    }

    /// <summary>摘掉分层样式 —— 窗口就此变回普通窗口（淡入跑完就该调它）。</summary>
    public static void Detach()
    {
        if (!_attached) return;
        try
        {
            long ex = GetWindowLongPtr(_hwnd, GWL_EXSTYLE).ToInt64();
            SetWindowLongPtr(_hwnd, GWL_EXSTYLE, new IntPtr(ex & ~WS_EX_LAYERED));
        }
        catch { /* 摘不掉也无所谓：只是这窗口一直带着分层样式而已，功能不受影响 */ }
        _attached = false;
    }

    /// <summary>
    /// 淡出用：先把分层样式挂回来（alpha 从当前该有的值起），再淡到目标。
    /// 挂不上就直接调 <paramref name="done"/> —— 调用方不用写「没启用怎么办」。
    /// </summary>
    public static void FadeOutTo(double to, int ms, Action? done)
    {
        if (!Enabled || !Attach((byte)Math.Round(_alpha * 255))) { done?.Invoke(); return; }
        FadeTo(to, ms, done);
    }

    /// <summary>推到指定透明度（0~1），跑完调 <paramref name="done"/>。</summary>
    public static void FadeTo(double to, int ms, Action? done = null)
    {
        if (!Enabled) { done?.Invoke(); return; }

        Stop();
        _from = _alpha;
        _to = Math.Clamp(to, 0, 1);
        _ms = Math.Max(1, ms);
        _done = done;

        if (Math.Abs(_to - _from) < 0.002) { _alpha = _to; Send(); Finish(); return; }

        _sw = Stopwatch.StartNew();
        _timer = _dq!.CreateTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(15);
        _timer.IsRepeating = true;
        _timer.Tick += (_, _) => Step();
        _timer.Start();
    }

    /// <summary>立刻变成完全不透明（兜底用：淡入的链路万一没跑起来，靠这个救回来）。</summary>
    public static void SnapOpaque()
    {
        if (!Enabled) return;
        Stop();
        _alpha = 1.0;
        Send();
    }

    /// <summary>挂上 WS_EX_LAYERED 并设成指定 alpha。失败回滚成原样、返回 false。</summary>
    static bool Attach(byte alpha)
    {
        try
        {
            long ex = GetWindowLongPtr(_hwnd, GWL_EXSTYLE).ToInt64();
            if ((ex & WS_EX_LAYERED) == 0)
            {
                SetWindowLongPtr(_hwnd, GWL_EXSTYLE, new IntPtr(ex | WS_EX_LAYERED));
                if ((GetWindowLongPtr(_hwnd, GWL_EXSTYLE).ToInt64() & WS_EX_LAYERED) == 0)
                    return false;                                   // 样式没挂上，别硬来
            }
            if (!SetLayeredWindowAttributes(_hwnd, 0, alpha, LWA_ALPHA))
            {
                SetWindowLongPtr(_hwnd, GWL_EXSTYLE, new IntPtr(ex));   // 回滚
                return false;
            }
            _attached = true;
            _lastSent = alpha;
            return true;
        }
        catch
        {
            return false;
        }
    }

    static void Step()
    {
        double t = Math.Min(1.0, _sw!.Elapsed.TotalMilliseconds / _ms);
        // 正弦缓入缓出 —— 两个方向都用它。
        // ⚠️ **起步的快慢比总时长更决定「像淡入还是像弹出」。** 原来出现用的是缓出
        //    （起步最快的那种），实测第二帧就冲到 alpha 84（33%），观感是「跳出来」
        //    而不是「淡出来」（用户 2026-10-16 原话：「好像是突然跳出来的」）。
        //    正弦缓入开头几乎不动、中段匀、末尾再收住，600ms 里前 80ms 都在 4% 以下。
        double e = (1 - Math.Cos(Math.PI * t)) / 2;
        _alpha = _from + (_to - _from) * e;
        Send();
        if (t >= 1.0) Finish();
    }

    static void Finish()
    {
        Stop();
        var d = _done;
        _done = null;            // 先摘再调：done 里可能又 FadeTo（比如淡完就 Close）
        d?.Invoke();
    }

    static void Stop()
    {
        if (_timer is not null) { _timer.Stop(); _timer = null; }
        _sw = null;
    }

    static void Send()
    {
        if (!_attached) return;
        int v = (int)Math.Round(Math.Clamp(_alpha, 0, 1) * 255);
        if (v == _lastSent) return;
        _lastSent = v;
        SetLayeredWindowAttributes(_hwnd, 0, (byte)v, LWA_ALPHA);
    }
}
