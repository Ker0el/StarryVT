using Microsoft.UI;
using Microsoft.UI.Xaml;
using Windows.UI;

namespace Starry.Vt.App.Services;

/// <summary>
/// 主题色（界面里的强调色）。设置页那个调色板改的就是它。
///
/// 框架自带的控件（勾选框、滑杆、进度条、超链接、下拉高亮…）全都从 App 资源里的
/// `SystemAccentColor*` 这几个键取色，所以这里只要把这几个键换掉就行 ——
/// 不用去挨个改控件。自己画的那几处（输入框焦点环）会现读现用，也自动跟上。
/// </summary>
public static class Accent
{
    /// <summary>调色板。第一个是默认色（樱花粉）。</summary>
    public static readonly (string Name, string Hex)[] Palette =
    [
        ("樱花粉", "#FF6FB5"),
        ("星空蓝", "#5B9CFF"),
        ("青碧", "#2FD4C4"),
        ("紫罗兰", "#A879FF"),
        ("珊瑚橙", "#FF8A5C"),
        ("琥珀", "#FFC44D"),
        ("森绿", "#55D07A"),
        ("石墨灰", "#9AA6B8"),
    ];

    public const string Default = "#FF6FB5";

    public static string Current => UiState.Accent;

    /// <summary>把主题色写进 App 资源（改完立刻生效）</summary>
    public static void Apply(string hex, bool save = true)
    {
        if (!TryParse(hex, out var c)) return;

        var res = Application.Current.Resources;
        res["SystemAccentColor"] = c;
        // 这几个是控件的悬停 / 按下态用的深浅变体，不一起换的话按下去会跳回蓝色
        res["SystemAccentColorLight1"] = Mix(c, Colors.White, 0.18);
        res["SystemAccentColorLight2"] = Mix(c, Colors.White, 0.34);
        res["SystemAccentColorLight3"] = Mix(c, Colors.White, 0.50);
        res["SystemAccentColorDark1"] = Mix(c, Colors.Black, 0.20);
        res["SystemAccentColorDark2"] = Mix(c, Colors.Black, 0.36);
        res["SystemAccentColorDark3"] = Mix(c, Colors.Black, 0.52);

        Repaint();
        if (save) UiState.Accent = Normalize(hex);
    }

    /// <summary>
    /// 光换资源还不够：框架那些强调色画刷（AccentFillColorDefaultBrush 之类）的颜色本身是
    /// **ThemeResource**，只在主题变化时才重新取值 —— 实测光改 App 资源，滑杆、超链接、
    /// 勾选框一个都不动。所以这里把根元素的主题闪一下（浅→深，同一个 tick 里改回来，
    /// 屏幕上看不出来），逼整棵树把主题资源重算一遍。
    /// </summary>
    static void Repaint()
    {
        try
        {
            if (MainWindow.Instance?.Content is not FrameworkElement root) return;
            var want = root.RequestedTheme == ElementTheme.Dark ? ElementTheme.Light : ElementTheme.Dark;
            root.RequestedTheme = want;
            root.RequestedTheme = ElementTheme.Dark;   // 本程序只有深色
        }
        catch { /* 换不动就等下次启动，不影响功能 */ }
    }

    /// <summary>解析 "#RRGGBB" / "#AARRGGBB"（容错：没带 # 也认）</summary>
    public static bool TryParse(string? hex, out Color color)
    {
        color = Colors.Transparent;
        var s = (hex ?? "").Trim().TrimStart('#');
        if (s.Length == 6) s = "FF" + s;
        if (s.Length != 8 || !uint.TryParse(s, System.Globalization.NumberStyles.HexNumber, null, out var v))
            return false;
        color = Color.FromArgb((byte)(v >> 24), (byte)(v >> 16), (byte)(v >> 8), (byte)v);
        return true;
    }

    public static string Normalize(string hex)
    {
        var s = hex.Trim().TrimStart('#');
        if (s.Length == 8) s = s[2..];      // 去掉 alpha
        return "#" + s.ToUpperInvariant();
    }

    /// <summary>往 to 方向混一点（0 = 原色，1 = 全变成 to）</summary>
    static Color Mix(Color c, Color to, double t) => Color.FromArgb(
        0xFF,
        (byte)Math.Round(c.R + (to.R - c.R) * t),
        (byte)Math.Round(c.G + (to.G - c.G) * t),
        (byte)Math.Round(c.B + (to.B - c.B) * t));
}
