using System.Text.Json;

namespace Starry.Vt.App.Services;

/// <summary>
/// 启动自检 —— **每次打开软件**问一遍后端「环境还正常吗」。
///
/// ★★ 为什么必须每次查（2026-10-16 用户要求）：
///   以前**只有首页**会自检（`HomePage.LoadRealEnvAsync`）。而已经配好的用户，
///   启动后是**直接进「本地翻译」页**的 —— 首页根本不会被创建，那份自检**一次都不跑**。
///   于是下面这些事都要等用户点了「开始翻译」之后才炸出来：
///     · 引擎被卸载了 / 引擎数据目录被删了
///     · 磁盘被占满（`engine.check_space` 会拦，但那是**动手那一刻**才知道）
///   而界面上的导航锁读的是 `Mock.EngineReady` —— 那是**上次成功时写下的一个标记文件**，
///   早就过期了。
///
/// ⚠️ 别把这件事挪到 `AboutPage` 那类页面去调 —— 仓库里记着：
///   **让某一页成为「第一个拉后端的页面」会 fail-fast 崩掉**
///   （见 `AboutPage.LoadVersion` 的注释，实测过多次）。
///   这里是普通页面 Loaded 里调，和 HomePage 走的是同一条路。
/// </summary>
public static class EnvCheck
{
    static bool _done;                 // 一次运行只查一遍（见下面对成本说明）

    /// <summary>
    /// 引擎现在到底能不能用。
    ///
    /// 返回 `null` = 问不到（后端没起来 / 报告格式不对）——
    /// **这种时候调用方什么都别改**，判据失灵不能去误伤状态。
    ///
    /// ⚠️ 一次运行只真查一遍：`env_report` 里含 `nvidia-smi` 之类的子进程，
    ///    实测要 1~3 秒。挂在 Loaded 上而页面是缓存复用的（`NavigationCacheMode.Required`），
    ///    不挡一下就会变成「每次切回这一页都卡几秒」。
    /// </summary>
    public static async Task<bool?> EngineOkAsync()
    {
        if (_done) return null;                 // 已经查过：不重复查，也不改状态
        _done = true;
        try
        {
            if (!Backend.Start()) return null;
            var rep = await Backend.CallAsync("env_report", new { deep = false }, 30_000);
            if (rep is null || !rep.Value.TryGetProperty("items", out var arr)) return null;
            foreach (var it in arr.EnumerateArray())
            {
                if (!it.TryGetProperty("id", out var id) || id.GetString() != "engine") continue;
                bool ok = it.TryGetProperty("state", out var st) && st.GetString() == "ok";
                App.Dump($"[env] 启动自检：引擎 {(ok ? "正常" : "不可用")}");
                return ok;
            }
            App.Dump("[env] 启动自检：报告里没有引擎那一项（当问不到处理）");
            return null;                        // 报告里没有引擎那一项 —— 当问不到处理
        }
        catch (Exception e)
        {
            App.Dump("[env] 启动自检失败：" + e.Message);
            return null;
        }
    }
}
