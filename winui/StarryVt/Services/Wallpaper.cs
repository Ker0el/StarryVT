using Microsoft.UI.Xaml.Media.Imaging;

namespace Starry.Vt.App.Services;

/// <summary>
/// 背景壁纸。三条约定（都是这一轮定死的，别改回去）：
///
///  1. **每次启动换一条新的**：启动时现去图源要一条随机的，下到本地那个「当前」文件再播。
///     ⚠️ 为什么不直接流式播那个地址：那个接口**每次请求都返回随机的另一条**，
///     于是「钉住当前这张」根本没法把"你正看着的那条"存下来（会存成另一条）——
///     用户 2026-10-14 报的就是这个 bug。先落地再播，钉住的才是你看到的那条。
///     顺带：本地文件循环不会因为网络抖动卡顿。
///  2. **不攒缓存**：平时只有「当前」那一个文件（每次启动被覆盖），不囤一堆。
///  3. **钉住 = 收藏**：把当前文件**拷一份**进收藏库（不重新下载，所以必然是同一条），
///     收藏的都在设置里那排预览图里，点一下就用它。
/// </summary>
public static class Wallpaper
{
    static readonly string[] VideoSources = ["https://t.alcy.cc/acg"];

    // 静态图源：**只放二次元的**。之前混了 Bing 每日图 / picsum，
    // 结果"换一张"偶尔蹦出一张风景照 —— 用户 2026-10-14 明确不要。
    static readonly string[] ImageSources =
    [
        "https://t.alcy.cc/ycy",                                  // 实测 0.4s
        "https://uapis.cn/api/v1/random/image?category=acg",        // 实测 0.4s
        "https://api.yppp.net/pc.php",                              // 实测 0.4~1.3s
        "https://api.sretna.cn/api/anime/pc",                       // 实测 0.7~1.0s（回的是 WebP，Content-Type 却写 jpeg）
        "https://www.loliapi.com/acg/pc/",                          // 实测 0.4s，偶尔 404（有换源重试兜着）
        // ⚠️ api.netapi.xyz 实测 2.6~30 秒（最慢的一家），会让"换一张"卡半天，不用了；
        //    要加回来记得它在这个数组里也会被轮到（取图失败会自动换下一家，见 FetchCurrentAsync）
    ];

    // ⚠️ 这里原来有个 `SourceHosts()`（从上面两个数组现算域名，给关于页那行署名用）——
    //    2026-10-17 用户要求署名里**不列具体网址**，那行改成了写死的「来自公开图源」，
    //    这个方法就没人用了，一并删掉。要恢复的话在 git 历史里搜 `SourceHosts`。

    /// <summary>存壁纸的地方：当前那条 + 收藏的那些</summary>
    public static readonly string Dir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "StarryVT-WinUI", "wallpapers");

    const int SaveKeep = 20;                  // 收藏最多留几条
    const int ReadyKeep = 2;                  // 后台预下几条备用（换一张走它才够快）
    const long MinBytes = 100_000;            // 比这还小基本是错误页

    /// <summary>
    /// 预下好的备用条数说明：视频源给的是**真 mp4，一条 15~20 MB、下完要 15~25 秒**
    /// （curl 实测）。光靠"点了现下"就是干等半分钟，用户会觉得按钮没反应 ——
    /// 所以平时闲着的时候后台先备两条，点「换一张」时直接换上去（毫秒级），
    /// 换完再接着补。两条 ≈ 30 MB，比"一屏等 20 秒"划算得多。
    /// </summary>
    static readonly object ReadyLock = new();
    static bool _refilling;

    /// <summary>已经预下好、随时能换上的那些。</summary>
    public static IReadOnlyList<string> Ready()
    {
        try
        {
            if (!Directory.Exists(Dir)) return [];
            return Directory.EnumerateFiles(Dir, "ready-*")
                            .Where(f => !f.EndsWith(".part") && !f.Contains(".thumb."))
                            .OrderByDescending(File.GetLastWriteTimeUtc)
                            .ToList();
        }
        catch { return []; }
    }

    /// <summary>
    /// 随包带的那条默认视频壁纸（`Assets\bg-default.mp4`）。
    /// 首次运行把它铺成「当前」并钉住 —— 不铺的话新装的机器得先联网下一条，
    /// 没网就是纯深色底，看不出这个软件本来长什么样。
    ///
    /// ⚠️ 期间改过一阵子（2026-10-16 → 10-17）：那条片子的商用授权当时还在申请，
    ///     资产从包里撤了、`File.Exists` 那行必然返回、整个方法等于停摆。
    ///     **2026-10-17 作者已同意授权，资产放回来了，这里恢复原样。**
    ///     （留档在 `_private\星雨\`，含说明与"万一又要撤"的放回/撤走步骤。）
    ///
    /// 三条必须守住的：
    ///   · 落成 `current-<新 GUID>.mp4`，**不能叫 current.mp4** —— 固定名会同时踩
    ///     「播放器锁着正在播的那份、覆盖必然共享冲突」和「BitmapImage 按 URI 缓存」两个坑。
    ///   · 只在**一条都没有**时铺：用户换过 / 收藏过的机器上别硬塞回去。
    ///   · 铺完**钉住**。不钉的话 ApplyWallpaper 紧接着就去要一条随机的换上，
    ///     随包这条只在屏幕上闪一下就没，等于白搭（钉住之后「换一张」照旧能用，
    ///     那条路本来就会先撤掉收藏）。
    /// </summary>
    public static void EnsureSeed()
    {
        try
        {
            if (CurrentFile() is not null || Saved().Count > 0 || Ready().Count > 0) return;
            var seed = Path.Combine(AppContext.BaseDirectory, "Assets", "bg-default.mp4");
            if (!File.Exists(seed)) return;

            Directory.CreateDirectory(Dir);
            var dest = Path.Combine(Dir, $"current-{Guid.NewGuid():N}.mp4");
            File.Copy(seed, dest, overwrite: false);
            UiState.Wallpaper = "video";       // 随包的是 mp4，别停在静态图模式
            UiState.WallpaperPin = dest;
        }
        catch (Exception e) { App.Dump("铺随包壁纸失败: " + e.Message); }
    }

    /// <summary>
    /// 「随包那条默认视频」现在对应的本地文件，没有就铺一份出来。返回空串 = 用不了（调用方走老路）。
    ///
    /// 和 `EnsureSeed` 的分工，别搞混：
    ///   · `EnsureSeed`  **只在空机器上干一次**，还顺手钉住 —— 老用户永远走不到。
    ///   · 这个方法 **每次启动都问一次**，给「没收藏、也没主动要过随机」的机器兜底。
    ///     用户 2026-10-15 报的「为什么用户打开软件不是我那个视频背景」就是这个缺口：
    ///     老用户机器上早就有壁纸了（用过老版本 / 点过换一张），`EnsureSeed` 的条件全不成立，
    ///     于是一直看随机网图。
    ///
    /// 三条约束（都是踩出来的，别改）：
    ///   1. ⚠️ **必须拷一份出来再播，不能直接播 `Assets\bg-default.mp4`** ——
    ///      播放器会把那个文件锁住，而它在**安装目录**里；一锁，下次覆盖安装（含自动更新）
    ///      就撞共享冲突、装不上。
    ///   2. ⚠️ **不能每次启动都重拷**（19MB）：已经是同一份就直接复用。
    ///      判据用「大小 + 修改时间」—— 安装包里那份换了（新版换了默认视频）→ mtime 变 →
    ///      自动重铺，不用额外记版本。
    ///   3. **静态图模式返回空串** —— 随包的只有 mp4，静态图那模式没有「默认」可言，
    ///      交给调用方去走随机图源。
    /// </summary>
    public static string EnsureDefault()
    {
        try
        {
            if (UiState.Wallpaper == "image") return "";

            var seed = Path.Combine(AppContext.BaseDirectory, "Assets", "bg-default.mp4");
            if (!File.Exists(seed)) return "";

            var seedLen = new FileInfo(seed).Length;
            var seedTime = File.GetLastWriteTimeUtc(seed);

            // 已经是同一份 → 直接复用（**别每次重拷 19MB**）
            if (CurrentFile() is { } cur)
            {
                try
                {
                    var fi = new FileInfo(cur);
                    if (fi.Length == seedLen && fi.LastWriteTimeUtc == seedTime) return cur;
                }
                catch { /* 读不到信息就当不是同一份 */ }
            }

            Directory.CreateDirectory(Dir);
            // 落成新 GUID 名（理由见本文件顶部那三条约定）
            var dest = Path.Combine(Dir, $"current-{Guid.NewGuid():N}.mp4");
            File.Copy(seed, dest, overwrite: false);
            // 把 mtime 对齐到资产那份，下次启动才认得出「已经是同一份」
            try { File.SetLastWriteTimeUtc(dest, seedTime); } catch { }

            // 顺手把旧的 current-* 清掉（此时播放器还没打开任何东西，删得掉；
            // 删不掉也无所谓，下次启动再试）
            try
            {
                foreach (var old in Directory.EnumerateFiles(Dir, "current-*"))
                    if (!string.Equals(old, dest, StringComparison.OrdinalIgnoreCase))
                        try { File.Delete(old); } catch { }
            }
            catch { }

            return dest;
        }
        catch (Exception e)
        {
            App.Dump("铺默认壁纸失败: " + e.Message);
            return "";
        }
    }

    /// <summary>
    /// 把一条预下好的扶正成「当前」（改名，不是拷贝 —— 同目录元数据操作，没有中间态）。
    /// 没有备用的就返回空串，调用方老老实实去下。
    /// ⚠️ 必须按类型挑：池子里可能还留着"静态图"那批，视频模式下扶正一张 jpg 上去就错了。
    /// </summary>
    public static string PromoteReady(bool video)
    {
        lock (ReadyLock)
        {
            try
            {
                var pick = Ready().FirstOrDefault(f =>
                    f.EndsWith(".mp4", StringComparison.OrdinalIgnoreCase) == video);
                if (pick is null) return "";
                // 落成**新名字**（不是覆盖 current.mp4）：正在播的那份还被播放器锁着，
                // 覆盖它一定失败。起个没人占的名字，改完名再让调用方换上去。
                var dest = Path.Combine(Dir, $"current-{Guid.NewGuid():N}{Path.GetExtension(pick)}");
                File.Move(pick, dest, true);      // 改名：同目录元数据操作，没有中间态
                return dest;
            }
            catch { return ""; }
        }
    }

    /// <summary>
    /// 后台把备用池补满。已经在补就直接返回（点几下"换一张"不会起一堆下载）。
    /// 注意这是"最好有"，失败无所谓 —— 所以整段吞异常。
    /// </summary>
    public static async Task RefillReadyAsync(bool video)
    {
        lock (ReadyLock) { if (_refilling) return; _refilling = true; }
        try
        {
            ClearReady(video);                                // 换了类型的话，不对路的那批先清掉
            while (true)
            {
                int have;
                lock (ReadyLock)
                {
                    have = Ready().Count;
                    if (have >= ReadyKeep) break;
                }
                var f = await FetchOneAsync(video);
                if (f.Length == 0) break;                     // 拉不到就先算了，下次再说
                lock (ReadyLock)
                {
                    if (Ready().Count > ReadyKeep) { try { File.Delete(f); } catch { } }
                }
            }
        }
        catch { }
        finally { lock (ReadyLock) { _refilling = false; } }
    }

    /// <summary>不要的备用件（比如换了类型，旧的视频就用不上了）</summary>
    public static void ClearReady(bool keepModeVideo)
    {
        lock (ReadyLock)
        {
            try
            {
                foreach (var f in Directory.EnumerateFiles(Dir, "ready-*"))
                {
                    bool isVideo = f.EndsWith(".mp4", StringComparison.OrdinalIgnoreCase);
                    if (isVideo != keepModeVideo) { try { File.Delete(f); } catch { } }
                }
            }
            catch { }
        }
    }

    /// <summary>
    /// 清掉老版本（3.0 之前那套"囤一圈再随机挑"的实现）留下的文件：
    /// bg-*.jpg / bgv-*.mp4 / video\ 子目录 / last.txt / seen.txt。
    /// 现在这套只有「当前」那一份 + 收藏的那些，不再囤 —— 不清的话
    /// 老用户升级上来磁盘上会一直压着上百 MB 的图，而且再也没人读它们。
    /// 启动时跑一次，很便宜。
    /// </summary>
    public static void CleanLegacy()
    {
        try
        {
            if (!Directory.Exists(Dir)) return;
            foreach (var f in Directory.EnumerateFiles(Dir))
            {
                var n = Path.GetFileName(f);
                if (n.StartsWith("bg-", StringComparison.OrdinalIgnoreCase)
                    || n.StartsWith("bgv-", StringComparison.OrdinalIgnoreCase)
                    || n.Equals("last.txt", StringComparison.OrdinalIgnoreCase)
                    || n.Equals("seen.txt", StringComparison.OrdinalIgnoreCase)
                    || n.EndsWith(".part", StringComparison.OrdinalIgnoreCase))   // 上次没下完的半截
                {
                    try { File.Delete(f); } catch { }
                }
            }
            foreach (var d in new[] { "video" })
            {
                var p = Path.Combine(Dir, d);
                if (Directory.Exists(p)) { try { Directory.Delete(p, true); } catch { } }
            }
        }
        catch { }
    }

    /// <summary>
    /// 当前正在用的那份。
    /// ⚠️ 文件名是 **`current-<随机>.mp4`**（带横杠、每次都不一样），不是固定的 `current.mp4`。
    /// 原因：**播放器正播着的那份文件是锁着的，覆盖它必然失败**（共享冲突）——
    /// 实测症状是"第一次换能成功、之后每次换都失败"（因为第一次播放器占的是上一张静态图）。
    /// 每次落一个新名字、换上新的之后再清旧的，就永远不碰正在播的那份。
    /// </summary>
    public static string? CurrentFile()
    {
        try
        {
            if (!Directory.Exists(Dir)) return null;
            return CurFiles().OrderByDescending(File.GetLastWriteTimeUtc).FirstOrDefault();
        }
        catch { return null; }
    }

    /// <summary>所有「当前」候选（含老版本那套不带宽斜杠的 current.jpg / current.mp4）</summary>
    static IEnumerable<string> CurFiles()
    {
        try
        {
            return Directory.EnumerateFiles(Dir, "current*")
                            .Where(f => !f.EndsWith(".part") && !f.Contains(".thumb."))
                            .ToList();
        }
        catch { return []; }
    }

    /// <summary>
    /// 清掉老的「当前」，只留正在播的那份 + 最新的一份兜底。
    /// ⚠️ 正在播的那份**按路径显式排除**，不能只按"留最新两个"——同一个时间戳写出来的
    /// 两份排序是不确定的，万一把正在播的划进"老的"里就删到脚上了。
    /// 删不掉的（还占着）留着下次再清，这不是错误。
    /// </summary>
    public static void PruneCurrent(string? playing = null)
    {
        try
        {
            var all = CurFiles().ToList();
            var keep = new List<string>();
            if (playing is { Length: > 0 }) keep.Add(playing);
            foreach (var f in all.OrderByDescending(File.GetLastWriteTimeUtc))
            {
                if (keep.Count >= 2) break;
                if (!keep.Contains(f, StringComparer.OrdinalIgnoreCase)) keep.Add(f);
            }
            foreach (var f in all.Where(f => !keep.Contains(f, StringComparer.OrdinalIgnoreCase)))
            {
                try { File.Delete(f); } catch { }
            }
        }
        catch { }
    }

    /// <summary>已经收藏的壁纸，新的在前。</summary>
    public static IReadOnlyList<string> Saved()
    {
        try
        {
            if (!Directory.Exists(Dir)) return [];
            return Directory.EnumerateFiles(Dir, "saved-*")
                            .Where(f => !f.Contains(".thumb."))
                            .OrderByDescending(File.GetLastWriteTimeUtc)
                            .ToList();
        }
        catch { return []; }
    }

    /// <summary>
    /// 去图源要一条随机的，下到本地当「当前」那条。返回本地路径；失败返回空串。
    /// 先写临时文件、好了再改名 —— 这样播放中永远指向一个完整文件。
    /// </summary>
    public static async Task<string> FetchCurrentAsync(bool video = true) => await FetchAsync(video, false);

    /// <summary>下一条放进备用池（后台闲着时补货用）。</summary>
    public static async Task<string> FetchOneAsync(bool video = true) => await FetchAsync(video, true);

    /// <summary>打乱源列表挨个试：某一家抽风（404 / 超时）就自动换下一家，别空手而归。</summary>
    static async Task<string> FetchAsync(bool video, bool toReady)
    {
        foreach (var src in (video ? VideoSources : ImageSources).OrderBy(_ => Random.Shared.Next()))
        {
            var got = await TryFetchAsync(Busted(src), video, toReady);
            if (got.Length > 0) return got;
        }
        return "";
    }

    /// <summary>
    /// 按**文件头**判断格式，别信 Content-Type —— 实测 api.sretna.cn 明明回的是 WebP，
    /// Content-Type 却写 image/jpeg。按扩展名判断错不了太多（都当图片处理），
    /// 但存成假扩展名以后排查很费劲，顺手按字节认准。
    /// </summary>
    static string SniffExt(string file)
    {
        try
        {
            using var f = File.OpenRead(file);
            var b = new byte[16];
            int n = f.Read(b, 0, b.Length);
            if (n >= 12 && b[0] == 0x52 && b[1] == 0x49 && b[2] == 0x46 && b[3] == 0x46
                && b[8] == 0x57 && b[9] == 0x45 && b[10] == 0x42 && b[11] == 0x50) return ".webp";  // RIFF....WEBP
            if (n >= 3 && b[0] == 0xFF && b[1] == 0xD8 && b[2] == 0xFF) return ".jpg";
            if (n >= 4 && b[0] == 0x89 && b[1] == 0x50 && b[2] == 0x4E && b[3] == 0x47) return ".png";
            if (n >= 3 && b[0] == 0x47 && b[1] == 0x49 && b[2] == 0x46) return ".gif";
            if (n >= 8 && b[4] == 0x66 && b[5] == 0x74 && b[6] == 0x79 && b[7] == 0x70) return ".mp4";   // ....ftyp
        }
        catch { }
        return "";
    }

    static async Task<string> TryFetchAsync(string url, bool video, bool toReady)
    {
        try
        {
            Directory.CreateDirectory(Dir);
            // ⚠️ 这里**不能**用 HttpClient.Timeout 卡总时长：视频源给的是真 mp4，
            //    实测单个 3.5~19.4 MB、下完要 2~22 秒（curl 实测），按总时长卡 20 秒
            //    会把正常的大文件直接砍掉 —— 表现就是"点换一张十次里失败七次"。
            //    改成：**只要还在进数据就一直等，卡住 12 秒没新数据才放弃**，总上限 90 秒兜底。
            using var http = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
            http.DefaultRequestHeaders.UserAgent.ParseAdd("StarryVT/0.1");
            using var overall = new CancellationTokenSource(TimeSpan.FromSeconds(90));
            using var resp = await http.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, overall.Token);
            if (!resp.IsSuccessStatusCode) return "";

            // 扩展名先按文件头认（不信 Content-Type，见 SniffExt），认不出来才退回它说的
            var dest = "";

            // ⚠️ 临时文件**不能**叫 `current.mp4.part`：下面那个"清掉旧扩展名"的循环是按
            //    `current.*` 找的，而 Windows 的通配符**连 `.part` 一起匹配**（实测确认）——
            //    结果就是刚下完、正等着改名的那份被自己删掉，File.Move 抛异常被吞，
            //    界面表现为"点换一张没效果"（用户 2026-10-14 报的）。放到 current.* 之外就没事了。
            //    名字里带 GUID：连点几下"换一张"时几次下载各写各的，不会互相截断。
            var tmp = Path.Combine(Dir, $"fetch-{Guid.NewGuid():N}.part");
            try
            {
                // 手写拷贝（不用 CopyToAsync）：每读一次就把"卡住计时"重置一遍，
                // 这样慢但在动的下载不会被掐，真正卡死的 12 秒就放弃。
                using var stall = CancellationTokenSource.CreateLinkedTokenSource(overall.Token);
                using (var src = await resp.Content.ReadAsStreamAsync(overall.Token))
                using (var dst = File.Create(tmp))
                {
                    var buf = new byte[81920];
                    while (true)
                    {
                        stall.CancelAfter(TimeSpan.FromSeconds(12));
                        int n = await src.ReadAsync(buf, 0, buf.Length, stall.Token);
                        if (n == 0) break;
                        await dst.WriteAsync(buf.AsMemory(0, n), overall.Token);
                    }
                }
                if (new FileInfo(tmp).Length < MinBytes) return "";   // 多半是错误页，换下一家

                // 到这里文件已经落盘了，按它的实际字节认格式
                var ext = SniffExt(tmp);
                if (ext.Length == 0)
                {
                    var media = resp.Content.Headers.ContentType?.MediaType ?? "";
                    ext = media.Contains("video") ? ".mp4"
                        : media.Contains("png") ? ".png"
                        : media.Contains("webp") ? ".webp" : ".jpg";
                }
                // 每次都落**新名字**。两件事同时靠它解决：
                //   ① 视频：播放器锁着正在播的那份，覆盖同名文件必然共享冲突
                //   ② 静态图：BitmapImage 按 URI 缓存，路径不变就一直给你上一次那张旧图
                dest = toReady ? Path.Combine(Dir, $"ready-{Guid.NewGuid():N}{ext}")
                               : Path.Combine(Dir, $"current-{Guid.NewGuid():N}{ext}");
                File.Move(tmp, dest, true);      // dest 是新名字，不会撞上正在播的那份
                return dest;
            }
            finally
            {
                try { if (File.Exists(tmp)) File.Delete(tmp); } catch { }   // 成了就已被改名，这里只剩失败的情况
            }
        }
        catch { return ""; }
    }

    /// <summary>
    /// 钉住 = 把**正在播的那个文件**拷一份进收藏库（不重新下载，所以必定是同一条）。
    /// 返回收藏的那份路径；失败返回空串。
    /// </summary>
    public static string SaveCurrent(string currentFile)
    {
        try
        {
            if (currentFile.Length == 0 || !File.Exists(currentFile)) return "";
            Directory.CreateDirectory(Dir);
            var ext = Path.GetExtension(currentFile);
            var dest = Path.Combine(Dir, $"saved-{DateTime.Now:yyyyMMddHHmmss}{ext}");
            File.Copy(currentFile, dest, true);

            foreach (var old in Saved().Skip(SaveKeep))
                DeleteSaved(old);                                  // 收藏库也封顶
            return dest;
        }
        catch { return ""; }
    }

    /// <summary>
    /// 这份文件是不是已经在收藏里了（比大小 + 头 1 MB 的哈希，不必全量）。
    /// 「收藏这张」用它避免把同一条重复存进去。
    /// </summary>
    public static bool IsSaved(string file)
    {
        try
        {
            if (!File.Exists(file)) return false;
            long size = new FileInfo(file).Length;
            foreach (var f in Saved())
            {
                if (new FileInfo(f).Length != size) continue;
                if (HeadHash(f) == HeadHash(file)) return true;
            }
        }
        catch { }
        return false;
    }

    static string HeadHash(string path)
    {
        using var f = File.OpenRead(path);
        var buf = new byte[1024 * 1024];
        int n = f.Read(buf, 0, buf.Length);
        return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(buf.AsSpan(0, n)));
    }

    /// <summary>删掉一条收藏（连同它的预览图）。</summary>
    public static void DeleteSaved(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { }
        try { if (File.Exists(path + ".thumb.jpg")) File.Delete(path + ".thumb.jpg"); } catch { }
    }

    /// <summary>
    /// 这份文件**本身就是图片**吗（不是视频）。
    /// 界面上用它分流：图片走 Image、视频走播放器。
    /// 收藏条那边也用它 —— 图片收藏自己就是预览图，不用拿 ffmpeg 抽帧。
    /// </summary>
    public static bool IsImagePath(string path) =>
        path.EndsWith(".jpg", StringComparison.OrdinalIgnoreCase)
        || path.EndsWith(".jpeg", StringComparison.OrdinalIgnoreCase)
        || path.EndsWith(".png", StringComparison.OrdinalIgnoreCase)
        || path.EndsWith(".webp", StringComparison.OrdinalIgnoreCase)
        || path.EndsWith(".gif", StringComparison.OrdinalIgnoreCase);

    /// <summary>收藏列表里显示的名字：壁纸 10-14 16:53</summary>
    public static string Label(string path)
    {
        try
        {
            var name = Path.GetFileNameWithoutExtension(path);     // saved-20261014165333
            int i = name.LastIndexOf('-');
            if (i >= 0 && DateTime.TryParseExact(name[(i + 1)..], "yyyyMMddHHmmss", null,
                                                 System.Globalization.DateTimeStyles.None, out var t))
                return $"壁纸 {t:MM-dd HH:mm}";
        }
        catch { }
        return Path.GetFileName(path);
    }

    /// <summary>内置那张底图（没网 / 图源挂了时的兜底）</summary>
    public static BitmapImage BuiltIn() => new() { UriSource = new Uri("ms-appx:///Assets/hero.png") };

    static string Busted(string url) =>
        url + (url.Contains('?') ? "&" : "?") + "r=" + Guid.NewGuid().ToString("N")[..10];
}
