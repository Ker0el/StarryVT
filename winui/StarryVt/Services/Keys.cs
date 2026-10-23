using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json;

namespace Starry.Vt.App.Services;

/// <summary>
/// 「开始翻译」之前的 Key 检查（用户 2026-10-14 要求：没填 Key 就别让它闷头跑，直接引导去填）。
///
/// 为什么每次开跑都问一次后端、而不是在本地存一份：
/// Key 只存在后端的设置里（`boot` 的 `settings.keys_plain`），本地再存一份必然不同步
/// —— 用户在设置页填完、回到工作页，本地那份还是旧的。
/// 这是个本机调用，几十毫秒的事，比"填了却说没填"划算。
/// </summary>
public static class Keys
{
    /// <summary>
    /// 检查能不能开跑。缺 Key 就弹框引导（点「去填写」直接跳设置页），返回 false。
    /// ⚠️ **问不到后端时不拦**（返回 true）—— 后端还没起来的话，后面那条路自己会报错，
    ///    在这儿拦下来反而会让用户看到"没填 Key"这种驴唇不对马嘴的提示。
    /// </summary>
    /// <param name="doDub">
    /// 这次任务要不要配音。null = 按设置里的 do_dub 判断（老行为）；
    /// false = 这次只出字幕，就算设置里开着配音也别拦着要 MiMo Key。
    /// </param>
    /// <param name="downloadOnly">只下载视频：不需要任何 API Key，直接放行。</param>
    public static async Task<bool> EnsureAsync(XamlRoot? root, bool? doDub = null,
                                               bool downloadOnly = false)
    {
        if (downloadOnly) return true;
        var (deep, mimo, dub) = await ReadAsync();
        if (deep is null || root is null) return true;      // 问不到就别拦

        var missing = new List<string>();
        if (deep == false) missing.Add("翻译用的 DeepSeek Key");
        // MiMo 只有开着配音才用得上；这次选「只出字幕」时同样不拦
        bool needMimo = doDub ?? (dub != false);
        if (mimo == false && needMimo) missing.Add("配音用的 MiMo Key");
        if (missing.Count == 0) return true;

        try
        {
            var dlg = new ContentDialog
            {
                XamlRoot = root,
                Title = "还差一步就能开始",
                Content = string.Join("\n", missing) + " 还没填。\n填好之后回来点「开始翻译」就行。",
                PrimaryButtonText = "去填写",
                CloseButtonText = "取消",
                DefaultButton = ContentDialogButton.Primary,
            };
            if (await dlg.ShowAsync() == ContentDialogResult.Primary)
                MainWindow.Instance?.NavigateToSettings("keys");   // 直接滚到 API Key 这张卡
        }
        catch (Exception e) { App.Dump("Key 引导框失败: " + e.Message); }
        return false;
    }

    /// <summary>
    /// 自测用：只读不弹框，把判定结果原样吐出来。
    /// （弹框那条路没法自动测 —— ShowAsync 会一直等人点。）
    /// </summary>
    public static Task<(bool? deep, bool? mimo, bool? dub)> ProbeAsync() => ReadAsync();

    /// <summary>问后端要一次：两个 Key 填没填、配音开着没。拿不到就是 null（三态，别当成"没填"）。</summary>
    static async Task<(bool? deep, bool? mimo, bool? dub)> ReadAsync()
    {
        try
        {
            var r = await Backend.CallAsync("boot", null, 20_000);
            if (r is null || !r.Value.TryGetProperty("settings", out var s)) return (null, null, null);

            bool? deep = null, mimo = null, dub = null;
            if (s.TryGetProperty("keys_plain", out var kp) && kp.ValueKind == JsonValueKind.Object)
            {
                deep = Str(kp, "deepseek").Length > 0;
                mimo = Str(kp, "mimo").Length > 0;
            }
            if (s.TryGetProperty("do_dub", out var d) && d.ValueKind is JsonValueKind.True or JsonValueKind.False)
                dub = d.GetBoolean();
            return (deep, mimo, dub);
        }
        catch { return (null, null, null); }
    }

    static string Str(JsonElement el, string name) =>
        el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";
}
