using System.Diagnostics;

namespace Starry.Vt.App.Services;

/// <summary>
/// 找随包带的 ffmpeg / ffprobe —— 目前只用来给收藏的壁纸**抽一张预览图**
/// （收藏条里那些缩略图就是它出的）。
/// </summary>
internal static class Ffmpeg
{
    static string? _cached;                 // null = 还没找过，"" = 找过没找到

    static string? Dir()
    {
        if (_cached is not null) return _cached.Length == 0 ? null : _cached;
        foreach (var c in new[]
                 {
                     Path.Combine(AppContext.BaseDirectory, "backend", "_internal", "bin"),  // PyInstaller 版后端
                     Path.Combine(AppContext.BaseDirectory, "backend", "bin"),              // Nuitka 版后端（见 build_nuitka.py）
                     @"D:\AI-Project\StarryVT\dist\StarryVT\_internal\bin",                 // 开发：本机打的包
                     @"D:\AI-Project\StarryVT\.tools\bin",                                  // 开发：源码里那份
                 })
        {
            if (File.Exists(Path.Combine(c, "ffmpeg.exe"))) { _cached = c; return c; }
        }
        _cached = "";
        return null;
    }

    static string? Exe(string name) => Dir() is { } d ? Path.Combine(d, name + ".exe") : null;

    /// <summary>
    /// 「这一帧抽出来了没有」的判据。
    ///
    /// ⚠️⚠️ 以前这里卡的是**文件大于 500 字节**，那是错的：画面本来就单一的那一帧
    ///   （淡入淡出、纯色、很暗的场景）压出来可能只有 300 多字节，那是**完全正常的缩略图**，
    ///   却被当成失败删掉了。用户看到的就是「点收藏没缩略图」，而且**时有时无** ——
    ///   取决于抽到的那一帧复不复杂，所以特别难查（2026-10-15 用户报「又没缩略图」）。
    ///   实测：一张有效的 160x90 JPEG 只有 311 字节。
    ///   现在判据是 **ffmpeg 的退出码**（见 RunAsync），这里只再挡一下 0 字节的空文件。
    /// </summary>
    static bool Ok(string path) => File.Exists(path) && new FileInfo(path).Length > 0;

    /// <summary>
    /// 给视频抽一张预览图（取第 1 秒那一帧，缩到 160 宽）。已经抽过就直接用。
    /// 抽不出来返回空串（界面显示占位图，不是错误）。
    /// </summary>
    public static async Task<string> ThumbAsync(string video)
    {
        try
        {
            if (!File.Exists(video)) return "";
            if (video.EndsWith(".jpg", StringComparison.OrdinalIgnoreCase)
                || video.EndsWith(".png", StringComparison.OrdinalIgnoreCase)
                || video.EndsWith(".webp", StringComparison.OrdinalIgnoreCase))
                return video;                                   // 本来就是图，它自己就是预览图

            var dest = video + ".thumb.jpg";
            if (Ok(dest)) return dest;

            var ff = Exe("ffmpeg");
            if (ff is null) return "";
            var tmp = dest + ".tmp.jpg";
            if (!await RunAsync(ff, $"-v error -y -ss 1 -i \"{video}\" -frames:v 1 -vf scale=160:-2 -q:v 5 \"{tmp}\""))
            {
                // 第 1 秒抽不到（很短的片子、或者那一秒没画面）→ 退回第一帧
                await RunAsync(ff, $"-v error -y -i \"{video}\" -frames:v 1 -vf scale=160:-2 -q:v 5 \"{tmp}\"");
            }
            if (Ok(tmp)) { File.Move(tmp, dest, true); }
            else { try { if (File.Exists(tmp)) File.Delete(tmp); } catch { } }
            return Ok(dest) ? dest : "";
        }
        catch { return ""; }
    }

    /// <summary>跑一次 ffmpeg，返回**它自己的退出码是不是 0** —— 这才是「抽出来了没有」的真判据。</summary>
    static async Task<bool> RunAsync(string exe, string args, int timeoutMs = 60_000)
    {
        try
        {
            var psi = new ProcessStartInfo(exe, args)
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            using var p = Process.Start(psi);
            if (p is null) return false;
            var outTask = p.StandardOutput.ReadToEndAsync();
            var errTask = p.StandardError.ReadToEndAsync();
            using var cts = new CancellationTokenSource(timeoutMs);
            try { await p.WaitForExitAsync(cts.Token); }
            catch (OperationCanceledException) { try { p.Kill(true); } catch { } return false; }
            await outTask; await errTask;
            return p.ExitCode == 0;
        }
        catch { /* 抽不出来就算了 */ return false; }
    }
}
