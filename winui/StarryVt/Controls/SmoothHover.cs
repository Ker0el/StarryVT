using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using Windows.Foundation;

namespace Starry.Vt.App.Controls;

/// <summary>
/// 按钮的悬停手感。
///
/// 原来是什么样：WinUI 自带的写法是「换一个主题色画刷」，而且过渡只有 **83ms** ——
/// 快到看不出来，只觉得颜色「跳」了一下；更糟的是那套画刷会**盖掉我们自己设的半透明底色**，
/// 一悬停就从「透出壁纸」变成一块灰 ✗。
///
/// 这里做三件事：
///   1. 把模板里那层 ContentPresenter 的 BackgroundTransition 拉长到 ~170ms（颜色慢慢亮起来）；
///   2. 悬停时轻微放大（1.025）、按下去缩小（0.975）—— 手感上的「回应」；
///   3. 图标按钮那种小按钮幅度给大一点，大按钮小一点，看着才匀。
/// 颜色本身在 App.xaml 的 Button* 主题画刷里换成了自家的半透明白（见那边注释）。
/// </summary>
public static class SmoothHover
{
    public static readonly DependencyProperty EnabledProperty =
        DependencyProperty.RegisterAttached(
            "Enabled", typeof(bool), typeof(SmoothHover),
            new PropertyMetadata(false, OnChanged));

    /// <summary>鼠标在不在按钮上（PointerReleased 时要用）</summary>
    static bool IsOver(Button b) => b.IsPointerOver;

    public static void SetEnabled(DependencyObject d, bool v) => d.SetValue(EnabledProperty, v);
    public static bool GetEnabled(DependencyObject d) => (bool)d.GetValue(EnabledProperty);

    /// <summary>颜色过渡时长：83ms 太急，170ms 才有「亮起来」的感觉</summary>
    static readonly TimeSpan ColorMs = TimeSpan.FromMilliseconds(170);

    static void OnChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not Button btn || !(bool)e.NewValue) return;

        if (btn.IsLoaded) Attach(btn);
        else btn.Loaded += (s, _) => Attach((Button)s);
        btn.Unloaded += (s, _) => Detach((Button)s);
    }

    static readonly HashSet<Button> Hooked = new();

    static void Attach(Button btn)
    {
        if (!Hooked.Add(btn)) return;
        try
        {
            // 1) 让模板自己那层背景的过渡慢下来（属性挂在模板元素上，运行期改是有效的）
            foreach (var cp in Descendants(btn).OfType<ContentPresenter>())
            {
                cp.BackgroundTransition = new BrushTransition { Duration = ColorMs };
                break;                      // 按钮模板里只有一个
            }

            // 2) 悬停 / 按下：小按钮幅度大一点，大按钮小一点
            //    ⚠️ 用 RenderTransform + Storyboard（合成层的 Scale 动画在这台机器上不生效，见 Motion.cs）
            if (btn.RenderTransform is not ScaleTransform)
            {
                if (btn.RenderTransform is not null) return;      // 已经有别的变换，不抢
                btn.RenderTransform = new ScaleTransform { ScaleX = 1, ScaleY = 1 };
            }
            var scale = (ScaleTransform)btn.RenderTransform;
            btn.RenderTransformOrigin = new Point(0.5, 0.5);

            bool small = btn.ActualWidth is > 0 and < 90;
            double hover = small ? 1.06 : 1.025;
            double press = small ? 0.94 : 0.975;

            void To(double target, int ms)
            {
                try
                {
                    var sb = new Storyboard();
                    foreach (var prop in new[] { "ScaleX", "ScaleY" })
                    {
                        var a = new DoubleAnimation
                        {
                            To = target,
                            Duration = new Duration(TimeSpan.FromMilliseconds(ms)),
                            EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
                            EnableDependentAnimation = true,
                        };
                        Storyboard.SetTarget(a, scale);
                        Storyboard.SetTargetProperty(a, prop);
                        sb.Children.Add(a);
                    }
                    sb.Begin();
                }
                catch (Exception ex) { App.Dump("SmoothHover 缩放失败: " + ex.Message); }
            }

            btn.PointerEntered += (_, _) => To(hover, 170);
            btn.PointerExited += (_, _) => To(1, 200);
            btn.PointerPressed += (_, _) => To(press, 90);
            btn.PointerReleased += (_, _) => To(btn.IsPointerOver ? hover : 1, 150);
            btn.PointerCanceled += (_, _) => To(1, 150);
        }
        catch (Exception e)
        {
            App.Dump("SmoothHover.Attach 失败（不影响点击）: " + e.Message);
        }
    }

    static void Detach(Button btn) => Hooked.Remove(btn);

    static IEnumerable<DependencyObject> Descendants(DependencyObject root)
    {
        int n = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(root);
        for (int i = 0; i < n; i++)
        {
            var child = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(root, i);
            yield return child;
            foreach (var deeper in Descendants(child)) yield return deeper;
        }
    }
}
