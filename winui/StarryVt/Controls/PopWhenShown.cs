using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;

namespace Starry.Vt.App.Controls;

/// <summary>
/// 元素从「看不见」变成「看得见」的那一瞬间弹一下（0.4 → 1，带一点过冲）。
///
/// 用来做「打勾」那一下 —— XAML 不会因为 Visibility 变了就自己播动画，
/// 而这恰好是初始化流程里最需要「有反馈」的地方。
/// </summary>
public static class PopWhenShown
{
    public static readonly DependencyProperty OnProperty =
        DependencyProperty.RegisterAttached(
            "On", typeof(bool), typeof(PopWhenShown),
            new PropertyMetadata(false, (d, e) =>
            {
                if (d is not FrameworkElement el || !(bool)e.NewValue) return;
                el.RegisterPropertyChangedCallback(UIElement.VisibilityProperty, (s, _) =>
                {
                    if (s is FrameworkElement f && f.Visibility == Visibility.Visible) Pop(f);
                });
            }));

    public static void SetOn(DependencyObject d, bool v) => d.SetValue(OnProperty, v);
    public static bool GetOn(DependencyObject d) => (bool)d.GetValue(OnProperty);

    static void Pop(FrameworkElement el)
    {
        if (el.RenderTransform is not ScaleTransform scale) return;   // 模板里没给 ScaleTransform 就不动
        try
        {
            var sb = new Storyboard();
            foreach (var prop in new[] { "ScaleX", "ScaleY" })
            {
                var a = new DoubleAnimation
                {
                    From = 0.4,
                    To = 1,
                    Duration = new Duration(TimeSpan.FromMilliseconds(320)),
                    EasingFunction = new BackEase { Amplitude = 0.7, EasingMode = EasingMode.EaseOut },
                    EnableDependentAnimation = true,
                };
                Storyboard.SetTarget(a, scale);
                Storyboard.SetTargetProperty(a, prop);
                sb.Children.Add(a);
            }
            sb.Begin();
        }
        catch (Exception e) { App.Dump("PopWhenShown 失败: " + e.Message); }
    }
}
