// 自带的鼠标光标主题：只在本软件的界面上生效，**不碰系统光标**。
//
// 素材在 Assets\cursors\（随包，走 csproj 里那条 Assets\** 的 Content 规则）。
// 映射关系照主题自带的安装 INF：
//   pointer.cur=箭头  beam.cur=文本  link.cur=链接手形  busy.ani=忙  working.ani=后台忙
//   precision=十字  help=帮助  unavailable=禁止  move=移动  vert/horz/dgn1/dgn2=缩放
//   alternate=备用  handwriting=手写  person / pin
#nullable enable

using System.Collections.Generic;
using System.IO;
using Microsoft.UI.Input;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace Starry.Vt.App.Services;

public static class CursorTheme
{
    private static readonly Dictionary<string, InputCursor?> _cache = new();

    private static string Dir => Path.Combine(System.AppContext.BaseDirectory, "Assets", "cursors");

    /// <summary>按文件名取一个光标（结果缓存，同一份只加载一次）。拿不到就返回 null。</summary>
    public static InputCursor? TryGet(string fileName)
    {
        if (_cache.TryGetValue(fileName, out var hit)) return hit;
        var c = CursorUtilities.TryLoad(Path.Combine(Dir, fileName));
        _cache[fileName] = c;
        return c;
    }

    /// <summary>
    /// 把主题套到这个元素上。**设在根元素上就覆盖它的所有后代**
    /// （官方文档：指针落在元素或它的任何后代上时都用这个值，后代自己设了才覆盖）
    /// —— 所以一个窗口调一次就够，不用遍历控件树。
    /// 找不到素材就什么都不做，界面照旧用系统的。
    /// </summary>
    public static void Apply(FrameworkElement root)
    {
        var arrow = TryGet("pointer.cur");
        if (arrow is not null)
        {
            root.ChangeCursor(arrow);
            App.Dump("[cursor] 已套用自带光标主题（" + Dir + "）");
        }
        else
        {
            // ⚠️ 素材没加载上时会**静默**保持系统光标 —— 表面上看「改了代码没效果」，
            //    其实压根没走到。所以这里必须留一行，排查时一眼能看见走没走到。
            App.Dump("[cursor] 素材没加载上，保持系统光标：" + Dir);
        }
        ApplyRoles(root);
    }

    /// <summary>
    /// 把「文本 / 链接」这两类光标套到具体控件上。
    ///
    /// 根元素上的箭头管不到它俩：文本框的 I 形、链接的手形是**框架自己按控件**设的，
    /// 属于「后代自己设了值」那种情况，会把祖先的箭头盖掉。所以得显式设回来。
    ///
    /// ⚠️ 只给这两类控件设，别碰别的 —— 尤其别往 ScrollViewer / 容器上设：
    /// 那会把里面所有后代（含滚动条两端的箭头）一起改掉。
    /// </summary>
    public static void ApplyRoles(DependencyObject root)
    {
        var beam = TryGet("beam.cur");
        var link = TryGet("link.cur");
        if (beam is null && link is null) return;
        Walk(root, beam, link, 0);
    }

    private static void Walk(DependencyObject node, InputCursor? beam, InputCursor? link, int depth)
    {
        // 页面再深也就十几层，给个上限防意外（视觉树理论上不会成环，但别赌）
        if (depth > 40) return;
        int n;
        try { n = VisualTreeHelper.GetChildrenCount(node); }
        catch { return; }                      // 不是 Visual 的节点会抛，跳过
        for (int i = 0; i < n; i++)
        {
            DependencyObject child;
            try { child = VisualTreeHelper.GetChild(node, i); }
            catch { continue; }
            if (child is UIElement el)
            {
                if (beam is not null && el is TextBox or PasswordBox or RichEditBox)
                    el.ChangeCursor(beam);
                else if (link is not null && el is HyperlinkButton)
                    el.ChangeCursor(link);
            }
            Walk(child, beam, link, depth + 1);
        }
    }
}
