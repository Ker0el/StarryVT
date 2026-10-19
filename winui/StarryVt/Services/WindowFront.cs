using System.Runtime.InteropServices;

namespace Starry.Vt.App.Services;

/// <summary>
/// 把窗口叫到前台（剪贴板一读到链接就用它 —— 用户刚从浏览器复制完，视线在那边）。
/// </summary>
internal static class WindowFront
{
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr pid);
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint attach, uint attachTo, bool fAttach);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();

    const int SW_RESTORE = 9;

    /// <summary>
    /// 最小化就先还原，然后抢到前台。
    /// ⚠️ 后台进程直接调 SetForegroundWindow 会被前台的锁挡掉（**返回 true 但没效果**）——
    ///    得先把自己的输入队列挂到当前前台线程上，这是 Windows 认可的合法做法；用完立刻摘掉。
    /// </summary>
    public static void Bring(IntPtr hWnd)
    {
        if (hWnd == IntPtr.Zero) return;
        try
        {
            if (IsIconic(hWnd)) ShowWindow(hWnd, SW_RESTORE);

            var fg = GetForegroundWindow();
            if (fg == hWnd) return;                       // 已经在前台了，别折腾

            uint me = GetCurrentThreadId();
            uint other = GetWindowThreadProcessId(fg, IntPtr.Zero);
            bool attached = other != 0 && other != me && AttachThreadInput(me, other, true);
            try { SetForegroundWindow(hWnd); }
            finally { if (attached) AttachThreadInput(me, other, false); }
        }
        catch { /* 叫不动就算了，别影响解析 */ }
    }
}
