using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Media;
using Starry.Vt.App.Services;
using Windows.UI;

namespace Starry.Vt.App.Controls;

/// <summary>
/// 自绘下拉框：一个玻璃小按钮 + 飞出的面板（不用 WinUI 自带 ComboBox 那套描边 + 下拉线的观感）。
///
/// 为什么不用 ComboBox 改样式：它整个模板是一坨（切换按钮 + 弹出层 + 一排视觉状态），
/// 想只把弹出层做漂亮，等于重写整个模板，还不一定扛得住各版本的改动。这里干脆只要
/// 「点一下出列表、选中回调」这几件事，外观全自己画。
///
/// 用法：
///     var box = new GlassSelect();
///     box.SetItems([("英语", "en"), ("日语", "ja")], 0);
///     box.Changed += i => { ... };          // 选中的索引
///     box.SelectedTag                        // 选中的 Tag（"en"）
/// </summary>
public sealed class GlassSelect : Button
{
    readonly Flyout _flyout = new();
    readonly StackPanel _rows = new() { Spacing = 2, Padding = new Thickness(6) };
    readonly TextBlock _label = new() { FontSize = 14, VerticalAlignment = VerticalAlignment.Center };
    readonly FontIcon _chev = new()
    {
        Glyph = "",        // 向下的小箭头
        FontSize = 10,
        Opacity = 0.65,
        VerticalAlignment = VerticalAlignment.Center,
    };
    readonly List<(string Label, string Tag)> _items = [];

    /// <summary>换选择了（参数是索引）</summary>
    public event Action<int>? Changed;

    public int SelectedIndex { get; private set; }

    public string SelectedItem =>
        _items.Count == 0 ? "" : _items[Math.Clamp(SelectedIndex, 0, _items.Count - 1)].Label;

    public string SelectedTag =>
        _items.Count == 0 ? "" : _items[Math.Clamp(SelectedIndex, 0, _items.Count - 1)].Tag;

    public GlassSelect()
    {
        Padding = new Thickness(12, 6, 10, 6);
        CornerRadius = new CornerRadius(6);
        HorizontalAlignment = HorizontalAlignment.Left;

        Content = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 10,
            Children = { _label, _chev },
        };

        _flyout.Content = new Border
        {
            Background = new SolidColorBrush(Color.FromArgb(0xF2, 0x0B, 0x0F, 0x18)),
            BorderBrush = new SolidColorBrush(Color.FromArgb(0x26, 0xFF, 0xFF, 0xFF)),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(0),
            Child = _rows,
            MinWidth = 150,
        };
        _flyout.Placement = FlyoutPlacementMode.BottomEdgeAlignedLeft;

        var presenter = new Style(typeof(FlyoutPresenter));
        presenter.Setters.Add(new Setter(Control.BackgroundProperty, new SolidColorBrush(Colors.Transparent)));
        presenter.Setters.Add(new Setter(Control.BorderThicknessProperty, new Thickness(0)));
        presenter.Setters.Add(new Setter(Control.PaddingProperty, new Thickness(0)));
        presenter.Setters.Add(new Setter(FrameworkElement.RequestedThemeProperty, ElementTheme.Dark));
        _flyout.FlyoutPresenterStyle = presenter;

        _flyout.Opening += (_, _) => BuildRows();
        Click += (_, _) => _flyout.ShowAt(this);
    }

    /// <summary>填选项。tag 是给后端/逻辑用的值（语言代码之类），label 是显示的字。</summary>
    public void SetItems(IEnumerable<(string Label, string Tag)> items, int index = 0)
    {
        _items.Clear();
        _items.AddRange(items);
        SelectedIndex = Math.Clamp(index, 0, Math.Max(0, _items.Count - 1));
        _label.Text = SelectedItem;
        BuildRows();
    }

    /// <summary>外部（比如从设置里回填）改选中项，会触发一次 Changed</summary>
    public void Select(int index, bool notify = true)
    {
        if (_items.Count == 0) return;
        SelectedIndex = Math.Clamp(index, 0, _items.Count - 1);
        _label.Text = SelectedItem;
        BuildRows();
        if (notify) Changed?.Invoke(SelectedIndex);
    }

    /// <summary>按 Tag 选中（找不到就回第 0 项）</summary>
    public void SelectTag(string tag, bool notify = true)
    {
        int i = _items.FindIndex(x => x.Tag == tag);
        Select(i < 0 ? 0 : i, notify);
    }

    /// <summary>自测用：不用鼠标把菜单打开（截图核对样式）</summary>
    internal void OpenMenu() => _flyout.ShowAt(this);

    void BuildRows()
    {
        _rows.Children.Clear();
        for (int i = 0; i < _items.Count; i++)
        {
            int idx = i;
            bool on = i == SelectedIndex;
            var text = new TextBlock
            {
                Text = _items[i].Label,
                FontSize = 14,
                VerticalAlignment = VerticalAlignment.Center,
            };
            var row = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                Spacing = 8,
                Children =
                {
                    text,
                    new FontIcon
                    {
                        Glyph = "",           // 选中的打个勾
                        FontSize = 12,
                        Opacity = on ? 0.95 : 0,
                        VerticalAlignment = VerticalAlignment.Center,
                    },
                },
            };
            var btn = new Button
            {
                Content = row,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Left,
                Background = new SolidColorBrush(on
                    ? Color.FromArgb(0x33, 0xFF, 0xFF, 0xFF)
                    : Colors.Transparent),
                BorderThickness = new Thickness(0),
                CornerRadius = new CornerRadius(5),
                Padding = new Thickness(10, 7, 14, 7),
                MinWidth = 150,
            };
            if (on && Application.Current.Resources.TryGetValue("SystemAccentColor", out var acc)
                && acc is Color ac) text.Foreground = new SolidColorBrush(ac);
            btn.Click += (_, _) =>
            {
                _flyout.Hide();
                Select(idx);
            };
            _rows.Children.Add(btn);
        }

        // 面板弹出：轻微缩放 + 从上方落下来（比系统那种直接蹦出来"高级"一点）
        Motion.PopIn(_rows, 180);
    }
}
