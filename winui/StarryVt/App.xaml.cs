using Microsoft.UI.Xaml;
using System.Runtime.InteropServices;

namespace Starry.Vt.App;

public partial class App : Application
{
    Window? _window;

    /// <summary>
    /// 单实例锁。**必须要有**：程序会往自己的目录写壁纸的「当前」文件，
    /// 两份实例同时在跑时会互相把对方刚下好的文件删掉 ——
    /// 表现就是"点了换一张，壁纸纹丝不动"（用户 2026-10-14 真跑出过两个实例）。
    /// 拿不到锁就把已经开着的那份窗口叫到前面来，然后自己退出。
    /// </summary>
    static Mutex? _instanceLock;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr FindWindow(string? className, string? windowName);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);

    static bool TryTakeSingleInstance()
    {
        try
        {
            _instanceLock = new Mutex(true, @"Local\StarryVT-WinUI-single", out var isFirst);
            if (isFirst) return true;
            var h = FindWindow(null, "星译");
            // ⚠️ 过渡期（3.6 → 3.7 改名）：老的 3.6 窗口标题还叫「星译·免费版」，
            //    只找新名字会返回 0 → 既不启动新的、也不激活旧的，用户看到的是
            //    「双击了没反应」。两个名字都试一下，老的也给它激活上来。
            if (h == IntPtr.Zero) h = FindWindow(null, "星译·免费版");
            if (h != IntPtr.Zero) { ShowWindow(h, 9 /*SW_RESTORE*/); SetForegroundWindow(h); }
            return false;
        }
        catch { return true; }        // 判断不了就当能开，别把人挡在门外
    }

    public App()
    {
        // 一律深色起步（用户定的规矩：不跟随系统、默认就是深色）
        try { RequestedTheme = ApplicationTheme.Dark; } catch { /* 改不动就用系统默认 */ }

        // 免安装的自包含程序，崩了没有调试器可看 —— 把异常写到临时目录，
        // 否则 XAML 里一点小错就是「进程直接消失，什么提示都没有」。
        //
        // ⚠️⚠️ 这里**吞掉异常、不让进程死**（Handled = true），是用户 2026-10-14 踩过之后定的：
        //     他点了一下「设置」，async void 事件处理器里一个空引用就把进程整个干掉了。
        //     这个软件会跑一小时的翻译任务，**中途无声消失比出个小毛病的代价大得多**；
        //     而且界面上的小错（某个预览图没出来、某个按钮没反应）本来就不该拖垮全局。
        //     异常照样完整写进 crash 文件，出问题能查 —— 所以是"吞掉"，不是"看不见"。
        UnhandledException += (_, e) =>
        {
            Dump(e.Exception?.ToString() ?? e.Message);
            e.Handled = true;
        };
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Dump(e.ExceptionObject?.ToString() ?? "?");
        TaskScheduler.UnobservedTaskException += (_, e) => Dump(e.Exception.ToString());

        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        if (!TryTakeSingleInstance()) Environment.Exit(0);      // 已经开着一份了，把那边的窗口叫到前面就退
        HideInternalPaths();
        try
        {
            // 上次在设置里选的主题色（要赶在窗口建出来之前，否则第一帧还是默认蓝）
            Services.Accent.Apply(Services.Accent.Current, save: false);

            _window = new MainWindow();
            _window.Activate();
        }
        catch (Exception e)
        {
            Dump("窗口创建失败:\n" + e);
            throw;
        }
    }

    /// <summary>
    /// 把「实现」从资源管理器的默认视图里藏起来（用户 2026-10-14：别让用户看见技术栈）。
    ///
    /// 只改 Windows 的**隐藏属性**，文件内容和位置一个字节都不动：
    ///   · 资源管理器、桌面搜索默认就不列出来了（开了「显示隐藏文件」还是看得到，这不是加密）
    ///   · 程序自己读写完全不受影响
    ///
    /// 藏的是：随包的 Python 后端整棵 backend\、以及几个存数据/配置的目录。
    ///
    /// ⚠️ **licenses\ 和 THIRD_PARTY_LICENSES.md 没藏**（那里面也列着第三方组件名）。
    ///    第三方许可要求「随分发附带许可原文」，把许可文件藏起来是有风险的，
    ///    所以留着可见 —— 要不要一起藏是用户的决定，不是我能替他拍的。
    /// </summary>
    static void HideInternalPaths()
    {
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var roaming = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        foreach (var p in new[]
                 {
                     Path.Combine(AppContext.BaseDirectory, "backend"),  // Python 后端的整棵实现
                     Path.Combine(local, "StarryVT-WinUI"),              // 壁纸、界面状态
                     Path.Combine(roaming, "StarryVT"),                  // 配置、日志
                     Path.Combine(local, "StarryVT"),
                     // 翻译引擎自己建的数据目录（名字改不了，是它写死的）——
                     // 藏起来免得用户翻到「这个软件用了 XXX」。
                     Path.Combine(local, "FineSub"),
                     Path.Combine(local, "StarryVT-FineSub"),
                 })
        {
            try
            {
                if (!Directory.Exists(p)) continue;
                var attrs = File.GetAttributes(p);
                if ((attrs & FileAttributes.Hidden) == 0)
                    File.SetAttributes(p, attrs | FileAttributes.Hidden);
            }
            catch { /* 没权限就算了，不影响使用 */ }
        }
    }

    internal static void Dump(string text)
    {
        try
        {
            var p = Path.Combine(Path.GetTempPath(), "starryvt-winui-crash.txt");
            // ⚠️ 以前这里是 `File.WriteAllText` —— **每条都覆盖上一条，只留最后一条**。
            //    而用户报的往往是「偶尔闪一下」「有时候卡一下」这种间歇性问题，
            //    真正有用的那条早就被后面一条无关紧要的盖掉了（2026-10-16 改）。
            //    改成追写；超过 256 KB 就清掉重来，免得无限长大。
            try
            {
                if (File.Exists(p) && new FileInfo(p).Length > 256 * 1024)
                    File.WriteAllText(p, "");
            }
            catch { /* 量不到就当没超 */ }
            File.AppendAllText(p, $"\n[{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}] {text}\n");
        }
        catch { /* 记录本身失败就算了 */ }
    }
}
