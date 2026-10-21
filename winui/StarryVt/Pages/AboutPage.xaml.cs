using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Starry.Vt.App.Services;

namespace Starry.Vt.App.Pages;

/// <summary>
/// 关于页。内容从设置页的「关于与支持」搬过来（用户 2026-10-14 要求），
/// 版式参考 DreamTranslatePO 的 about：居中 hero + 两层入场动效。
/// </summary>
public sealed partial class AboutPage : Page
{
    public AboutPage()
    {
        InitializeComponent();
        Loaded += OnLoaded;
    }

    void OnLoaded(object sender, RoutedEventArgs e)
    {
        Loaded -= OnLoaded;                 // 只播一次
        PlayEntrance();
        LoadVersion();
    }

    // ⚠️ 这里原来有个 BuildCredit()「把图源域名现算出来填进署名」——
    //    2026-10-17 用户要求署名里**不列具体网址**，那行改成 XAML 里写死的一句
    //    「其他背景图来自公开图源」，它和 `Wallpaper.SourceHosts()` 一起删掉了。
    //    按域名现算本身没错（加/删图源能自动跟上），但既然不显示了，留着就是死代码。

    /// <summary>
    /// 入场动效。参数**照抄参考程序源码**（TypeDreamMoon/DreamTranslatePO，
    /// Views/AboutPage.xaml 里那个 AnimationSet / AnimationScope）：
    ///
    ///   ① **背景收清**（最显眼的一层）：模糊 32→0、饱和度 0→1.2、曝光 1→0
    ///   ② **整页**：从 1.1 倍缩回 1、从右侧 32px 归位、淡入
    ///   ③ **擦开**：从"零高度"往下展开（它的 ClipAnimation From="0,0,1280,0"）
    ///   ④ **文字**：逐块错开淡入（这一层是我们自己加的，用户说保留）
    ///
    /// 全部 Duration = **3 秒**、EaseOut。第一版按 0.7 秒做，用户一眼就说"哪有这么快"。
    /// </summary>
    void PlayEntrance()
    {
        // ①a 背景模糊收清：**已去掉**（原因见 AboutPage.xaml 顶部那段注释 ——
        //     CompositionBackdropBrush 抓不到视频壁纸，整页会变成深蓝星空，透不过背景）。

        // ①b 整页：1.1 倍缩回 1 + 右 32px 归位 + 淡入
        try { Motion.PageIn(HeroHost); }
        catch (Exception ex) { App.Dump("关于页整页入场失败: " + ex.Message); }

        // ①c 擦开：零高度往下展开
        try { Motion.ClipReveal(HeroHost); }
        catch (Exception ex) { App.Dump("关于页擦开失败: " + ex.Message); }

        // ② 文字逐块错开。整页是 3 秒，这里摊在头 2.4 秒里，别跟整页脱节
        FrameworkElement[] blocks = [BLogo, BTitle, BEasy, BSlogan, BRule1, BMeta, BRule2, BBtns, BCredit];
        for (int i = 0; i < blocks.Length; i++)
        {
            try { Motion.HeroIn(blocks[i], delayMs: 200 + i * 150, ms: 800); }
            catch (Exception ex) { App.Dump("关于页文字入场失败: " + ex.Message); break; }
        }

        // ③ 赞助那行的五彩字：**静态**，不做流动动画。
        //    试过三条路，都不成立（都实测过，别重踩）：
        //      · XAML 渐变 + Storyboard 动 9 个 GradientStop.Offset → 能动，但每秒只更新一两次
        //        （连拍 1.8 秒量颜色位置，只跳了 3 次），看着就是"卡卡的"
        //      · XAML 渐变 + 动刷子的 TranslateTransform → 动画起播了（埋点确认 brush/transform
        //        都拿到了、宽度 615），但**画面完全不动**
        //      · 合成层 CompositionLinearGradientBrush + 把文字采成 CompositionVisualSurface
        //        当面具裁形 → 颜色出来了，但同样不动，而且重采样把抗锯齿搞丢，字明显发粗
        //    结论：WinUI 里渐变填充的文字没法既连续流动又保持锐利，所以选锐利的这个。
    }

    /// <summary>
    /// 版本号：**读静态字段，不发 RPC**。
    ///
    /// ⚠️⚠️ 这一页绝不能去调后端。实测：只要「关于」是**第一个**发起后端请求的页面
    /// （此前没人调过 Backend.Start），程序就必崩 —— 而且崩在 `Backend.CallAsync`
    /// 里拉后端那一段，不是崩在我们自己的代码里：事件日志 Microsoft.UI.Xaml.dll /
    /// 0xc000027b，窗口凭空消失，连 Application.UnhandledException 都不触发
    /// （fail-fast，接不住）。逐段埋点才定位到是那个 RPC，已复现多次。
    ///
    /// 后端身上本来就有这个值（它起来时推的 ready 事件里带的，见 Backend.Version），
    /// 直接读就够；还没有就退回和标题栏一致的写死值 —— 界面里显示的数字必须和
    /// 标题栏那个 v3.0 对得上，否则用户会以为装错了版本。
    /// </summary>
    void LoadVersion()
    {
        try
        {
            var ver = Backend.Version;
            BVersion.Text = ver.Length > 0 ? $"当前版本 v{ver}" : "当前版本 v6.13";
        }
        catch { }
    }

    /// <summary>微信：弹二维码浮层（渐隐渐现，点空白 / Esc 关闭，没有关闭按钮）</summary>
    void ShowWx_Click(object sender, RoutedEventArgs e) => MainWindow.Instance?.ShowWx();

    void Bili_Click(object sender, RoutedEventArgs e) => _ = OpenUrlAsync("https://space.bilibili.com/177308205");

    /// <summary>随包默认视频壁纸的原片（作者 B站 星雨布丁）。用户 2026-10-17 要求链到它。</summary>
    void WallpaperBy_Click(object sender, RoutedEventArgs e)
        => _ = OpenUrlAsync("https://www.bilibili.com/video/BV1PaQHBaEsg");

    void ShowQr_Click(object sender, RoutedEventArgs e) => MainWindow.Instance?.ShowQr();

    /// <summary>
    /// 用系统 Launcher 开链接，**不走后端** —— 理由和上面 LoadVersion 一样：
    /// 这一页不该是「第一个把后端拉起来的人」。Launcher 就是系统默认浏览器，效果一样。
    /// </summary>
    static async Task OpenUrlAsync(string url)
    {
        try { await Windows.System.Launcher.LaunchUriAsync(new Uri(url)); }
        catch (Exception ex) { App.Dump("关于页打开链接失败: " + ex.Message); }
    }
}
