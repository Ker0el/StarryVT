using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Media.Core;
using Windows.Media.Playback;

namespace Starry.Vt.App.Services;

/// <summary>
/// 音色试听：让后端合成一句样音（同一音色会缓存复用），拿到文件后本地播出来。
///
/// 后端那步是真的 —— `preview_voice` 走的是原程序里的 MiMo 合成，没配 Key 会明确报错。
/// </summary>
public static class VoicePreview
{
    static MediaPlayer? _player;      // 必须留着引用，否则播到一半被回收

    /// <summary>点「试听」：合成 + 播放。按钮上显示进度，出错弹一句人话。</summary>
    public static async Task PlayAsync(string voice, Button? button = null)
    {
        string original = button?.Content?.ToString() ?? "试听";
        if (button is not null)
        {
            button.IsEnabled = false;
            button.Content = "合成中…";
        }
        try
        {
            var res = await Backend.CallAsync("preview_voice_abs", new { voice }, 180_000);

            if (res is null)
            {
                await Tell(button, $"试听失败：{Backend.LastError}\n（后端没起来的话，确认 python 在 PATH 里）");
                return;
            }
            if (res.Value.TryGetProperty("ok", out var ok) && !ok.GetBoolean())
            {
                await Tell(button, res.Value.TryGetProperty("error", out var err)
                    ? err.GetString() ?? "试听失败"
                    : "试听失败");
                return;
            }
            if (!res.Value.TryGetProperty("path", out var pathEl) || pathEl.GetString() is not { Length: > 0 } path)
            {
                await Tell(button, "试听失败：后端没返回音频文件");
                return;
            }

            _player ??= new MediaPlayer();
            _player.Source = MediaSource.CreateFromUri(new Uri(path));
            _player.Play();
        }
        catch (Exception e)
        {
            await Tell(button, "试听失败：" + e.Message);
        }
        finally
        {
            if (button is not null)
            {
                button.Content = original;
                button.IsEnabled = true;
            }
        }
    }

    static async Task Tell(Button? button, string message)
    {
        if (button?.XamlRoot is not { } root) return;
        await new ContentDialog
        {
            XamlRoot = root,
            Title = "试听",
            Content = new TextBlock { Text = message, TextWrapping = TextWrapping.Wrap },
            CloseButtonText = "知道了",
        }.ShowAsync();
    }
}
