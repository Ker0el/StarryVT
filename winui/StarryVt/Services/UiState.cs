using System.Text.Json;

namespace Starry.Vt.App.Services;

/// <summary>
/// 样机自己的界面状态（背景亮度之类）。
/// 真接后端时这些会挪到 settings.json 由 Python 那边管，这里只是让样机「记得住」。
/// </summary>
public static class UiState
{
    static readonly string File_ = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "StarryVT-WinUI", "ui.json");

    sealed class Model
    {
        public double BgOpacity { get; set; } = 0.95;
        public string Accent { get; set; } = "#FF6FB5";

        /// <summary>背景壁纸：image = 静态图（每次启动换一张），video = 动图（mp4）</summary>
        /// <remarks>
        /// 默认 **video**：随包带一条默认视频（`Assets\bg-default.mp4`），
        /// 装完就有动图背景，不用先联网下一条。
        /// ⚠️ 中间改过一阵子 **image**（2026-10-16 → 10-17）：那条片子的商用授权当时还在申请、
        ///    文件从包里拿掉了，而没有它 "video" 模式首次启动要**联网等 15~25 秒**，
        ///    "image" 只要 0.3~1.3 秒。**2026-10-17 作者已同意授权，文件放回来了，这里随之改回 video。**
        ///    （素材留档在 `_private\星雨\`，说明与放回步骤也在那儿 —— 万一以后又要撤。）
        /// 老用户不受影响：他们的 ui.json 里已经存了具体值，反序列化会覆盖这个默认。
        /// </remarks>
        public string Wallpaper { get; set; } = "video";

        /// <summary>钉住的壁纸（完整路径）；空 = 每次启动随机换</summary>
        public string WallpaperPin { get; set; } = "";

        /// <summary>
        /// 用户**主动要过随机壁纸**（点过「换一张」）。
        /// false（默认，含所有老用户的 ui.json——里面没这个字段）= 启动时用随包那条默认视频。
        /// </summary>
        public bool WallpaperRandom { get; set; } = false;
    }

    static Model _m = Load();

    static Model Load()
    {
        try
        {
            if (File.Exists(File_))
                return JsonSerializer.Deserialize<Model>(File.ReadAllText(File_)) ?? new Model();
        }
        catch { /* 读不出来就用默认 */ }
        return new Model();
    }

    static void Save()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(File_)!);
            File.WriteAllText(File_, JsonSerializer.Serialize(_m));
        }
        catch { }
    }

    /// <summary>页面切换动效（entrance / slide-left / slide-bottom / drill / common / none）</summary>
    public static string Transition { get; set; } = "drill";   // 用户选的：放大进入（钻取）

    /// <summary>在本地页/在线页之间切换时，告诉对方「进来先切到哪个模式」</summary>
    public static string? PendingMode { get; set; }

    /// <summary>壁纸的明亮程度：0 = 纯深色底，1 = 壁纸原样</summary>
    public static double BgOpacity
    {
        get => _m.BgOpacity;
        set { _m.BgOpacity = Math.Clamp(value, 0, 1); Save(); }
    }

    /// <summary>主题色（强调色），"#RRGGBB"</summary>
    public static string Accent
    {
        get => string.IsNullOrWhiteSpace(_m.Accent) ? "#FF6FB5" : _m.Accent;
        set { _m.Accent = value; Save(); }
    }

    /// <summary>背景壁纸用哪种：「image」静态图 / 「video」动图（mp4）。默认动图。</summary>
    public static string Wallpaper
    {
        get => _m.Wallpaper == "image" ? "image" : "video";
        set { _m.Wallpaper = value == "image" ? "image" : "video"; Save(); }
    }

    /// <summary>
    /// 钉住的壁纸（完整路径，空 = 每次启动随机换）。
    /// 钉住的那张**不会被缓存清理删掉**（见 Wallpaper.FillAsync / FillVideosAsync 的裁剪）。
    /// </summary>
    public static string WallpaperPin
    {
        get => _m.WallpaperPin ?? "";
        set { _m.WallpaperPin = value ?? ""; Save(); }
    }

    /// <summary>
    /// 用户**主动要过随机壁纸**吗（点过设置页那个「换一张」）。
    ///
    /// false（默认值，也是所有老用户 ui.json 反序列化出来的值）
    /// → 启动时显示**随包那条默认视频**，不再每次去网上随机下一条。
    /// true  → 老行为：每次启动随机换一条在线壁纸。
    ///
    /// 用户 2026-10-15 报的「为什么用户打开软件不是我那个视频背景」就是这里：
    /// 老用户机器上早就有壁纸了，`EnsureSeed` 那条「空机器才铺」永远走不到。
    /// </summary>
    public static bool WallpaperRandom
    {
        get => _m.WallpaperRandom;
        set { _m.WallpaperRandom = value; Save(); }
    }
}
