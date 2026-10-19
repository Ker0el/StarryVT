using Microsoft.UI.Composition;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Hosting;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using System.Numerics;
using Windows.Foundation;

namespace Starry.Vt.App.Services;

/// <summary>
/// 动效小工具。
///
/// ⚠️⚠️ **只能用 XAML Storyboard，不能用合成层（ElementCompositionPreview + StartAnimation）。**
/// 实测：给 `UIElement.Opacity` 起合成动画，XAML 那边会把自己的属性值推回可视对象、把动画压掉 ——
/// 自测日志里 100ms / 250ms / 650ms 读回来全是终值 0.50，**一点过渡都没有**，
/// 而且表现成「壁纸干脆不显示」「面板生硬地蹦出来」，很能骗人（花了一轮才定位）。
/// 自测代码在 `MainWindow.MotionSelfTest()`（`--probe=motion` 触发），要再验就跑一次。
///
/// 为什么不用 XAML 的 ThemeTransition（EntranceThemeTransition 之类）：那些只在元素
/// **加进可视树**的那一刻播，而我们是靠 Visibility 在 Collapsed / Visible 之间切 ——
/// 切换不播动画。所以用 Storyboard 自己在切换的时机播。
///
/// 两条习惯：
///   1. 位移/缩放走 RenderTransform（没有就补一个；已经有别的变换就不动它，只做淡入）；
///   2. 淡入的终值是**元素自己当前的 Opacity**，不是死写 1 —— 磨砂层那种本来就半透明的
///      才不会被越动越实（磨砂层之前就是这么变糊的）。
/// </summary>
public static class Motion
{
    static readonly CubicEase EaseOut = new() { EasingMode = EasingMode.EaseOut };

    static Duration Ms(int ms) => new(TimeSpan.FromMilliseconds(ms));

    /// <summary>淡到指定透明度</summary>
    public static void FadeTo(UIElement el, double to, int ms = 220)
    {
        var sb = new Storyboard();
        var a = new DoubleAnimation
        {
            To = to,
            Duration = Ms(ms),
            EasingFunction = EaseOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(a, el);
        Storyboard.SetTargetProperty(a, "Opacity");
        sb.Children.Add(a);
        sb.Begin();
    }

    /// <summary>从 from 淡到元素当前该有的透明度（壁纸淡入、磨砂层淡入都用它）</summary>
    public static void FadeFrom(UIElement el, double from = 0, int ms = 420)
    {
        double target = el.Opacity;          // 元素属性里已经写好终值，这里只管「从哪来」
        var sb = new Storyboard();
        var a = new DoubleAnimation
        {
            From = from,
            To = target,
            Duration = Ms(ms),
            EasingFunction = EaseOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(a, el);
        Storyboard.SetTargetProperty(a, "Opacity");
        sb.Children.Add(a);
        sb.Begin();
    }

    public static void FadeIn(UIElement el, int ms = 200) => FadeFrom(el, 0, ms);

    public static void FadeOut(UIElement el, int ms = 160) => FadeTo(el, 0, ms);

    /// <summary>淡入 + 从下方轻微上移（面板 / 卡片出现时用）</summary>
    public static void Reveal(UIElement el, double offsetY = 18, int ms = 260)
    {
        var sb = new Storyboard();

        var fade = new DoubleAnimation
        {
            From = 0,
            To = 1,
            Duration = Ms(ms),
            EasingFunction = EaseOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(fade, el);
        Storyboard.SetTargetProperty(fade, "Opacity");
        sb.Children.Add(fade);

        if (offsetY != 0 && Translate(el) is { } tt)
        {
            var move = new DoubleAnimation
            {
                From = offsetY,
                To = 0,
                Duration = Ms(ms),
                EasingFunction = EaseOut,
                EnableDependentAnimation = true,
            };
            Storyboard.SetTarget(move, tt);
            Storyboard.SetTargetProperty(move, "Y");
            sb.Children.Add(move);
        }

        sb.Begin();
    }

    /// <summary>弹出来：淡入 + 从 0.96 放大到 1（对话框那种感觉）</summary>
    public static void PopIn(UIElement el, int ms = 240)
    {
        if (el is not FrameworkElement fe) return;
        if (fe.RenderTransform is not ScaleTransform scale)
        {
            if (fe.RenderTransform is not null) return;      // 已经有别的变换，别抢
            scale = new ScaleTransform { ScaleX = 1, ScaleY = 1 };
            fe.RenderTransform = scale;
        }
        fe.RenderTransformOrigin = new Point(0.5, 0.5);

        var sb = new Storyboard();
        foreach (var prop in new[] { "ScaleX", "ScaleY" })
        {
            var a = new DoubleAnimation
            {
                From = 0.96,
                To = 1,
                Duration = Ms(ms),
                EasingFunction = EaseOut,
                EnableDependentAnimation = true,
            };
            Storyboard.SetTarget(a, scale);
            Storyboard.SetTargetProperty(a, prop);
            sb.Children.Add(a);
        }

        var fade = new DoubleAnimation
        {
            From = 0,
            To = fe.Opacity,
            Duration = Ms(ms),
            EasingFunction = EaseOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(fade, el);
        Storyboard.SetTargetProperty(fade, "Opacity");
        sb.Children.Add(fade);

        sb.Begin();
    }

    /// <summary>缓入缓出（两头都慢）。只缓出（EaseOut）是「冲进来、稳稳停」，收尾偏硬；
    /// 模态层这种「慢慢浮出来」的场合得两头都慢才显得稳。</summary>
    static readonly CubicEase EaseInOut = new() { EasingMode = EasingMode.EaseInOut };

    /// <summary>
    /// 模态卡片进场：淡入 + 从 fromScale 放大到 1 + 从下方 fromY 浮起，**缓入缓出**。
    ///
    /// ⚠️ 先读终值、再把基础透明度藏成 0 —— `Visibility` 刚变 Visible 到动画起播之间
    /// 隔着一帧（调用方走 <see cref="NextFrame"/> 等排版），不藏会原样闪一下。
    /// 基础值由 <see cref="ModalOut"/> 在收场时还回来，来回开关才不会越淡越没。
    /// </summary>
    public static void ModalIn(FrameworkElement el, int ms = 480, double fromScale = 0.94, double fromY = 16)
    {
        double target = el.Opacity > 0 ? el.Opacity : 1;
        el.Opacity = 0;

        if (el.RenderTransform is null)
        {
            // 位移 + 缩放用一个 CompositeTransform 表达，比 TransformGroup 少一层
            el.RenderTransform = new CompositeTransform
            {
                ScaleX = fromScale,
                ScaleY = fromScale,
                TranslateY = fromY,
            };
            el.RenderTransformOrigin = new Point(0.5, 0.5);
        }

        var sb = new Storyboard();

        var fade = new DoubleAnimation
        {
            From = 0,
            To = target,
            Duration = Ms(ms),
            EasingFunction = EaseInOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(fade, el);
        Storyboard.SetTargetProperty(fade, "Opacity");
        sb.Children.Add(fade);

        if (el.RenderTransform is CompositeTransform ct)
        {
            foreach (var (prop, from, to) in new[]
                     {
                         ("ScaleX", fromScale, 1.0),
                         ("ScaleY", fromScale, 1.0),
                         ("TranslateY", fromY, 0.0),
                     })
            {
                var a = new DoubleAnimation
                {
                    From = from,
                    To = to,
                    Duration = Ms(ms),
                    EasingFunction = EaseInOut,
                    EnableDependentAnimation = true,
                };
                Storyboard.SetTarget(a, ct);
                Storyboard.SetTargetProperty(a, prop);
                sb.Children.Add(a);
            }
        }

        try { sb.Begin(); }
        catch
        {
            el.Opacity = target;   // 起播失败就把透明度还回去，别让内容永久隐形
            throw;
        }
    }

    /// <summary>
    /// 模态卡片退场：淡出 + 缩到 toScale + 下沉，**缓入缓出**，跑完调 done（收 Visibility 用）。
    ///
    /// ⚠️⚠️ **HoldEnd（默认）+ 收起来之后再 Stop()，两件事缺一不可**，这是踩过的坑：
    ///   · 用 `FillBehavior.Stop` 的话，动画一结束属性就**立刻弹回基础值**，而 `Completed`
    ///     是在弹回去**之后**才触发的 —— 中间只要排到一帧，用户看到的就是「关的时候闪一下」
    ///     （2026-10-15 用户报的就是这个）。
    ///   · 光用 HoldEnd 不 Stop 也不行：透明度会被永久按在 0 上，下次 <see cref="ModalIn"/>
    ///     读 `el.Opacity` 读到的就是 0 → 整层淡入失效，症状是「第二次打开什么都没有」。
    ///   所以顺序是：**先 done()（把 Visibility 收掉）→ 再 Stop() 撒手 → 最后把基础值钉回去**。
    ///   收起来之后弹回什么都看不见。
    /// </summary>
    public static void ModalOut(FrameworkElement el, int ms = 320, double toScale = 0.97,
                                double toY = 10, Action? done = null)
    {
        var sb = new Storyboard();

        var fade = new DoubleAnimation
        {
            To = 0,
            Duration = Ms(ms),
            EasingFunction = EaseInOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(fade, el);
        Storyboard.SetTargetProperty(fade, "Opacity");
        sb.Children.Add(fade);

        if (el.RenderTransform is CompositeTransform ct)
        {
            foreach (var (prop, to) in new[] { ("ScaleX", toScale), ("ScaleY", toScale), ("TranslateY", toY) })
            {
                var a = new DoubleAnimation
                {
                    To = to,
                    Duration = Ms(ms),
                    EasingFunction = EaseInOut,
                    EnableDependentAnimation = true,
                };
                Storyboard.SetTarget(a, ct);
                Storyboard.SetTargetProperty(a, prop);
                sb.Children.Add(a);
            }
        }

        sb.Completed += (_, _) =>
        {
            try { done?.Invoke(); }
            catch (Exception e) { App.Dump("Motion.ModalOut 回调失败: " + e.Message); }
            try { sb.Stop(); } catch { }
            el.Opacity = 1;                // 基础值钉回终值（Stop 之后 getter 就是它）
        };
        sb.Begin();
    }

    /// <summary>
    /// 磨砂层进场：从 0 淡到 target，**缓入缓出**。
    /// ⚠️ target **显式传进来，别去读 el.Opacity** —— 收场时基础值会被动过，
    ///    读回来的可能不是静止值（磨砂层就会以全不透明出现，很难查）。
    /// </summary>
    public static void BackdropIn(UIElement el, double target, int ms = 420)
    {
        el.Opacity = target;               // 静止值：动画没跑 / 被撒手时就是它
        var sb = new Storyboard();
        var a = new DoubleAnimation
        {
            From = 0,
            To = target,
            Duration = Ms(ms),
            EasingFunction = EaseInOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(a, el);
        Storyboard.SetTargetProperty(a, "Opacity");
        sb.Children.Add(a);

        try { sb.Begin(); }
        catch { el.Opacity = target; throw; }
    }

    /// <summary>
    /// 磨砂层退场：淡到 0，**缓入缓出**，跑完调 done 收 Visibility。
    /// 为什么 HoldEnd + 收起后再 Stop、以及为什么 target 要显式传 —— 见 <see cref="ModalOut"/>。
    /// </summary>
    public static void BackdropOut(UIElement el, double target, int ms = 320, Action? done = null)
    {
        var sb = new Storyboard();
        var a = new DoubleAnimation
        {
            To = 0,
            Duration = Ms(ms),
            EasingFunction = EaseInOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(a, el);
        Storyboard.SetTargetProperty(a, "Opacity");
        sb.Children.Add(a);

        sb.Completed += (_, _) =>
        {
            try { done?.Invoke(); }
            catch (Exception e) { App.Dump("Motion.BackdropOut 回调失败: " + e.Message); }
            try { sb.Stop(); } catch { }
            el.Opacity = target;           // 基础值钉回静止值
        };
        sb.Begin();
    }

    /// <summary>
    /// 列表里新长出来的一行：支点钉在上边，纵向从 0.72 展开到 1，同时从上方 9px 落下来、淡入。
    /// （「一键初始化」里一条条长出来用它 —— 整块一下全出来会让人不知道该看哪。）
    /// </summary>
    public static void GrowIn(FrameworkElement el, int ms = 300)
    {
        el.RenderTransformOrigin = new Point(0.5, 0);      // 上边当支点，才是「长下来」

        var sb = new Storyboard();
        double target = el.Opacity <= 0 ? 1 : el.Opacity;

        var fade = new DoubleAnimation
        {
            From = 0,
            To = target,
            Duration = Ms(ms),
            EasingFunction = EaseOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(fade, el);
        Storyboard.SetTargetProperty(fade, "Opacity");
        sb.Children.Add(fade);

        if (el.RenderTransform is null)
        {
            var expand = new DoubleAnimation
            {
                From = 0.72,
                To = 1,
                Duration = Ms(ms),
                EasingFunction = EaseOut,
                EnableDependentAnimation = true,
            };
            Storyboard.SetTarget(expand, el);
            Storyboard.SetTargetProperty(expand, "RenderTransform.ScaleY");
            sb.Children.Add(expand);
        }

        if (Translate(el) is { } tt)
        {
            var move = new DoubleAnimation
            {
                From = -9,
                To = 0,
                Duration = Ms(ms),
                EasingFunction = EaseOut,
                EnableDependentAnimation = true,
            };
            Storyboard.SetTarget(move, tt);
            Storyboard.SetTargetProperty(move, "Y");
            sb.Children.Add(move);
        }

        sb.Begin();
    }

    /// <summary>
    /// 关于页那种「hero 入场」：淡入 + 从下方浮起 + 从 0.97 放大，可以带延迟做**逐块错开**。
    ///
    /// 参考程序（DreamTranslatePO）的 about 页就是这个味道：内容整体淡入、位移、模糊收清
    /// （它的 resources.pri 里有 Storyboard / TranslateTransform / ScaleTransform / BlurEffect）。
    /// 模糊那层在 WinUI 里要动 CompositionVisualSurface、很脆 —— 这里用「位移 + 缩放」表达
    /// 同样的「收上来」，稳得多，观感也接近。
    /// </summary>
    public static void HeroIn(FrameworkElement el, int delayMs = 0, int ms = 560, double offsetY = 24)
    {
        double target = el.Opacity > 0 ? el.Opacity : 1;     // 元素属性里写好终值，这里只管从哪来
        // ⚠️ 先读终值、再立刻把自己藏起来：带 BeginTime 的 Storyboard 在延迟期间
        //    **元素保持基础值**，不先藏就会先原样闪一下、到点了又跳回 0 再淡入。
        el.Opacity = 0;
        var sb = new Storyboard { BeginTime = TimeSpan.FromMilliseconds(delayMs) };

        var fade = new DoubleAnimation
        {
            From = 0,
            To = target,
            Duration = Ms(ms),
            EasingFunction = EaseOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(fade, el);
        Storyboard.SetTargetProperty(fade, "Opacity");
        sb.Children.Add(fade);

        if (el.RenderTransform is null)                      // 已经有别的变换就不抢，只做淡入
        {
            // 位移 + 缩放用一个 CompositeTransform 表达：一个对象、四个可动画属性，
            // 比 TransformGroup 里塞两个变换少一层、也不用去凑支点。
            var ct = new CompositeTransform
            {
                ScaleX = 0.97,
                ScaleY = 0.97,
                TranslateY = offsetY,
            };
            el.RenderTransform = ct;
            el.RenderTransformOrigin = new Point(0.5, 0.5);

            foreach (var (prop, from, to) in new[]
                     {
                         ("ScaleX", 0.97, 1.0),
                         ("ScaleY", 0.97, 1.0),
                         ("TranslateY", offsetY, 0.0),
                     })
            {
                var a = new DoubleAnimation
                {
                    From = from,
                    To = to,
                    Duration = Ms(ms),
                    EasingFunction = EaseOut,
                    EnableDependentAnimation = true,
                };
                Storyboard.SetTarget(a, ct);
                Storyboard.SetTargetProperty(a, prop);
                sb.Children.Add(a);
            }
        }

        try { sb.Begin(); }
        catch
        {
            el.Opacity = target;   // 起播失败就把透明度还回去，别让内容永久隐形
            throw;
        }
    }

    /// <summary>
    /// **整页**入场：缩放 + 横向归位 + 淡入。
    ///
    /// 数值照抄参考程序源码（Views/AboutPage.xaml 的 AnimationScope）：
    ///   `ScaleAnimation From="1.1" To="1"`（**从 1.1 缩回 1**，是大缩到正常，不是放大）
    ///   `TranslationAnimation From="32,0,0" To="0"`（从右侧 32px 归位）
    ///   `Duration="0:0:3" EasingMode="EaseOut"`（**整整 3 秒**）
    /// ⚠️ 第一版我把缩放做反了（0.93 放大到 1），方向完全不一样，实测对比才发现。
    /// </summary>
    public static void PageIn(FrameworkElement el, int ms = 3000, double fromScale = 1.1, double fromX = 32)
    {
        double target = el.Opacity;
        el.Opacity = 0;
        if (el.RenderTransform is null)
        {
            el.RenderTransform = new CompositeTransform
            {
                ScaleX = fromScale,
                ScaleY = fromScale,
                TranslateX = fromX,
            };
            el.RenderTransformOrigin = new Point(0.5, 0.5);
        }

        var sb = new Storyboard();
        var fade = new DoubleAnimation
        {
            From = 0,
            To = target,
            Duration = Ms(ms),
            EasingFunction = EaseOut,
            EnableDependentAnimation = true,
        };
        Storyboard.SetTarget(fade, el);
        Storyboard.SetTargetProperty(fade, "Opacity");
        sb.Children.Add(fade);

        if (el.RenderTransform is CompositeTransform ct)
        {
            foreach (var (prop, from, to) in new[]
                     {
                         ("ScaleX", fromScale, 1.0),
                         ("ScaleY", fromScale, 1.0),
                         ("TranslateX", fromX, 0.0),
                     })
            {
                var a = new DoubleAnimation
                {
                    From = from,
                    To = to,
                    Duration = Ms(ms),
                    EasingFunction = EaseOut,
                    EnableDependentAnimation = true,
                };
                Storyboard.SetTarget(a, ct);
                Storyboard.SetTargetProperty(a, prop);
                sb.Children.Add(a);
            }
        }

        try { sb.Begin(); }
        catch
        {
            el.Opacity = target;   // 起播失败就把透明度还回去，别让内容永久隐形
            throw;
        }
    }

    /// <summary>
    /// 参考程序 about 页入场里的 **ClipAnimation**：从「零高度」往下擦出来。
    /// 它源码里写的是 `From="0,0,1280,0" To="0"`，即一开始是个高 0、宽满的矩形，再放到全尺寸。
    /// 合成层的 InsetClip 正好表达这个：底边内缩 = 整高 → 0。
    /// </summary>
    public static void ClipReveal(FrameworkElement el, int ms = 3000, double delayMs = 0)
    {
        var v = ElementCompositionPreview.GetElementVisual(el);
        var compositor = v.Compositor;
        float h = (float)el.ActualHeight;
        if (h <= 0) return;

        var clip = compositor.CreateInsetClip(0, 0, 0, h);   // left, top, right, bottom
        v.Clip = clip;

        var ease = compositor.CreateCubicBezierEasingFunction(new Vector2(0.1f, 0.9f), new Vector2(0.2f, 1f));
        var a = compositor.CreateScalarKeyFrameAnimation();
        a.InsertKeyFrame(0f, h);
        a.InsertKeyFrame(1f, 0f, ease);
        a.Duration = TimeSpan.FromMilliseconds(ms);
        a.DelayTime = TimeSpan.FromMilliseconds(delayMs);
        clip.StartAnimation("BottomInset", a);
    }

    /// <summary>还没跑完的计时器要留住引用，否则被 GC 掉就永远不 Tick，而且不报错</summary>
    static readonly List<Microsoft.UI.Dispatching.DispatcherQueueTimer> KeepAlive = new();

    /// <summary>
    /// 延时做一次事（一次性计时器）。
    ///
    /// ⚠️⚠️ **不要**自己写 `var t = DispatcherQueue.CreateTimer(); t.Start();` ——
    ///     `t` 是局部变量，方法一返回它就可能被 GC 回收，之后**再也不 Tick**，
    ///     而且不报任何错。实测栽过：给「关于」页写的「动画跑完摘掉背景模糊层」的清理
    ///     就是这么失效的，表现为那层永远挂在那儿（整页变成深蓝星空）。
    ///     这里统一把计时器挂进静态表，跑完再摘掉。
    /// </summary>
    public static void After(Microsoft.UI.Dispatching.DispatcherQueue queue, int ms, Action work)
    {
        var t = queue.CreateTimer();
        t.Interval = TimeSpan.FromMilliseconds(ms);
        t.IsRepeating = false;
        lock (KeepAlive) KeepAlive.Add(t);
        t.Tick += (_, _) =>
        {
            t.Stop();
            lock (KeepAlive) KeepAlive.Remove(t);
            try { work(); } catch (Exception e) { App.Dump("Motion.After 回调失败: " + e.Message); }
        };
        t.Start();
    }

    /// <summary>
    /// <summary>
    /// 循环做某事（比如"换一张中… 12s"那种走秒）。⚠️ 同样必须留引用，理由见上。
    /// 停的时候要用 <see cref="Stop"/>，别直接调 timer.Stop() —— 那样它会一直留在引用表里。
    /// </summary>
    public static Microsoft.UI.Dispatching.DispatcherQueueTimer Repeat(
        Microsoft.UI.Dispatching.DispatcherQueue queue, int ms, Action work)
    {
        var t = queue.CreateTimer();
        t.Interval = TimeSpan.FromMilliseconds(ms);
        t.IsRepeating = true;
        lock (KeepAlive) KeepAlive.Add(t);
        t.Tick += (_, _) => { try { work(); } catch (Exception e) { App.Dump("Motion.Repeat 回调失败: " + e.Message); } };
        t.Start();
        return t;
    }

    /// <summary>停掉 <see cref="Repeat"/> 建出来的计时器，并把它从引用表里摘掉</summary>
    public static void Stop(Microsoft.UI.Dispatching.DispatcherQueueTimer? timer)
    {
        if (timer is null) return;
        try { timer.Stop(); } catch { }
        lock (KeepAlive) KeepAlive.Remove(timer);
    }

    /// 给元素准备一个专门用来做位移的 TranslateTransform（有别的变换就返回 null）</summary>
    static TranslateTransform? Translate(UIElement el)    {
        if (el is not FrameworkElement fe) return null;
        if (fe.RenderTransform is TranslateTransform tt) return tt;
        if (fe.RenderTransform is not null) return null;
        tt = new TranslateTransform { X = 0, Y = 0 };
        fe.RenderTransform = tt;
        return tt;
    }

    /// <summary>
    /// 给列表加「增删 / 重排」动画：新加的行淡入、删掉的淡出、其余平滑让位。
    /// ⚠️ 这个属性**只能在这里设** —— 写在 XAML 的 ItemContainerTransitions 里会让
    /// XAML 编译器内部抛 WMC9999（未将对象引用设置到对象的实例），实测踩过。
    /// </summary>
    public static void ItemsAnimate(ItemsControl list)
    {
        try
        {
            list.ItemContainerTransitions = new TransitionCollection
            {
                new AddDeleteThemeTransition(),
                new RepositionThemeTransition(),
            };
        }
        catch (Exception e) { App.Dump("Motion.ItemsAnimate 失败: " + e.Message); }
    }

    /// <summary>下一个 UI 帧再干活（刚设完 Visibility，元素还没排版）</summary>
    public static void NextFrame(Microsoft.UI.Dispatching.DispatcherQueue queue, Action work) =>
        queue.TryEnqueue(() => { try { work(); } catch (Exception e) { App.Dump("Motion.NextFrame 失败: " + e.Message); } });
}
