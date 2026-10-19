using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using Windows.UI;

namespace Starry.Vt.App.Controls;

/// <summary>
/// 给输入框加「渐变」的聚焦/悬停效果。
///
/// 为什么要自己写：WinUI 的 VisualState 是**硬切**的 —— 状态一退，Storyboard 立刻回落
/// 到基准值，颜色不可能平滑过去。唯一双向平滑的是 BackgroundTransition 这种属性过渡，
/// 可它只能作用在 Background 上（这一版 Border 连 BorderBrushTransition 都没有），
/// 拿「外层敷边框色 + 内层留 1px」去仿描边又会因为内层是半透明而糊成一片。
///
/// 所以改成：往模板根 Grid 里插两层自己的元素（底色层 + 1px 焦点环），
/// 焦点/悬停变化时用 ColorAnimation 渐变它们的颜色。框架模板一个字不改，
/// 光标、输入法、选中、右键菜单、清空按钮全都不受影响。
///
/// 用法：`SmoothField.Glow="True"`（App 的隐式样式里已经给所有 TextBox 打上了）。
/// </summary>
public static class SmoothField
{
    public static readonly DependencyProperty GlowProperty =
        DependencyProperty.RegisterAttached(
            "Glow", typeof(bool), typeof(SmoothField),
            new PropertyMetadata(false, OnGlowChanged));

    public static void SetGlow(DependencyObject d, bool v) => d.SetValue(GlowProperty, v);
    public static bool GetGlow(DependencyObject d) => (bool)d.GetValue(GlowProperty);

    /// <summary>渐变时长。再长一点会显得拖沓，150~180ms 是舒服的区间</summary>
    const int Ms = 170;

    static readonly Color RingIdle = Color.FromArgb(0x26, 0xFF, 0xFF, 0xFF);
    static readonly Color RingHover = Color.FromArgb(0x40, 0xFF, 0xFF, 0xFF);
    static readonly Color FillIdle = Color.FromArgb(0x00, 0xFF, 0xFF, 0xFF);
    static readonly Color FillHover = Color.FromArgb(0x16, 0xFF, 0xFF, 0xFF);

    sealed class Glow
    {
        public required Control Box;      // TextBox / PasswordBox 都用它
        public required Border Ring;
        public required Border Fill;
        public required SolidColorBrush RingBrush;
        public required SolidColorBrush FillBrush;
        public required Storyboard Story;
        public required ColorAnimation RingAnim;
        public required ColorAnimation FillAnim;
        public bool Hover;
        public bool Focused;
        public bool Dead;
    }

    static readonly List<Glow> All = new();

    static void OnGlowChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        // 只做输入框这一类（密码框的模板结构和输入框一样，也走这条路）
        if (d is not Control box || d is not (TextBox or PasswordBox) || !(bool)e.NewValue) return;
        if (box.IsLoaded) Attach(box);
        else box.Loaded += (s, _) => Attach((Control)s);
        box.Unloaded += (s, _) => Detach((Control)s);
    }

    static void Attach(Control box)
    {
        try
        {
            if (All.Any(g => ReferenceEquals(g.Box, box))) return;

            // 模板根：输入框的模板最外层都是一个 Grid（Row1 才是那块边框）
            if (VisualTreeHelper.GetChildrenCount(box) == 0) return;
            if (VisualTreeHelper.GetChild(box, 0) is not Panel root) return;

            var radius = box.CornerRadius;

            var fill = new Border
            {
                CornerRadius = radius,
                Background = new SolidColorBrush(FillIdle),
                IsHitTestVisible = false,
            };
            var ring = new Border
            {
                CornerRadius = radius,
                BorderThickness = new Thickness(1),
                BorderBrush = new SolidColorBrush(RingIdle),
                IsHitTestVisible = false,
            };
            // 和模板里那块 BorderElement 摆在同一格，尺寸才对得上
            foreach (var el in new FrameworkElement[] { fill, ring })
            {
                Grid.SetRow(el, 1);
                Grid.SetColumn(el, 0);
                Grid.SetRowSpan(el, 1);
                Grid.SetColumnSpan(el, 2);
            }
            // 插在最底下：压住模板自带的边框（已经在视觉状态里被抹成常量），
            // 但仍在文字层下面，不会挡住输入
            root.Children.Insert(0, fill);
            root.Children.Insert(1, ring);

            var ringAnim = new ColorAnimation { Duration = new Duration(TimeSpan.FromMilliseconds(Ms)) };
            var fillAnim = new ColorAnimation { Duration = new Duration(TimeSpan.FromMilliseconds(Ms)) };
            Storyboard.SetTarget(ringAnim, ring.BorderBrush);
            Storyboard.SetTargetProperty(ringAnim, "Color");
            Storyboard.SetTarget(fillAnim, fill.Background);
            Storyboard.SetTargetProperty(fillAnim, "Color");
            var story = new Storyboard();
            story.Children.Add(ringAnim);
            story.Children.Add(fillAnim);

            var g = new Glow
            {
                Box = box,
                Ring = ring,
                Fill = fill,
                RingBrush = (SolidColorBrush)ring.BorderBrush,
                FillBrush = (SolidColorBrush)fill.Background,
                Story = story,
                RingAnim = ringAnim,
                FillAnim = fillAnim,
            };
            All.Add(g);

            box.PointerEntered += (_, _) => { g.Hover = true; Go(g); };
            box.PointerExited += (_, _) => { g.Hover = false; Go(g); };
            box.GotFocus += (_, _) => { g.Focused = true; Go(g); };
            box.LostFocus += (_, _) => { g.Focused = false; Go(g); };
            box.ActualThemeChanged += (_, _) => Go(g);
        }
        catch (Exception ex)
        {
            App.Dump("SmoothField.Attach 失败（不影响输入）: " + ex.Message);
        }
    }

    static void Detach(Control box)
    {
        var g = All.FirstOrDefault(x => ReferenceEquals(x.Box, box));
        if (g is not null) { g.Dead = true; All.Remove(g); }
    }

    /// <summary>按当前状态（聚焦 / 悬停 / 常态）把两层颜色渐变过去。</summary>
    static void Go(Glow g)
    {
        if (g.Dead) return;
        try
        {
            var accent = AccentColor();

            Color ringTo = g.Focused ? WithAlpha(accent, 0x8C)
                          : g.Hover ? RingHover
                          : RingIdle;
            Color fillTo = g.Focused ? WithAlpha(accent, 0x3D)
                          : g.Hover ? FillHover
                          : FillIdle;

            // 先记住「现在实际是什么颜色」，Stop 会把它回落到基准值
            var ringFrom = g.RingBrush.Color;
            var fillFrom = g.FillBrush.Color;
            g.Story.Stop();

            g.RingAnim.From = ringFrom;
            g.RingAnim.To = ringTo;
            g.FillAnim.From = fillFrom;
            g.FillAnim.To = fillTo;
            g.Story.Begin();
        }
        catch (Exception ex)
        {
            App.Dump("SmoothField.Go 失败: " + ex.Message);
        }
    }

    static Color WithAlpha(Color c, byte a) => Color.FromArgb(a, c.R, c.G, c.B);

    /// <summary>当前主题色（设置页里那个调色板改的就是它）</summary>
    static Color AccentColor()
    {
        if (Application.Current.Resources.TryGetValue("SystemAccentColor", out var v) && v is Color c)
            return c;
        return Color.FromArgb(0xFF, 0xFF, 0x6F, 0xB5);
    }
}
