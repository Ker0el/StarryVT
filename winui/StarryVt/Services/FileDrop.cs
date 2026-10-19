using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace Starry.Vt.App.Services;

/// <summary>
/// 窗口的拖放目标（走 OLE 的 IDropTarget）。
///
/// 为什么要这么麻烦：老的 `DragAcceptFiles` + `WM_DROPFILES` **只在松手那一刻**才给消息，
/// 拖到窗口上方时收不到任何通知 —— 想做「拖进来时高亮 + 图标跳动」就没数据可用，
/// 而且拖动时光标不变，用户会以为拖不进去。
/// IDropTarget 有 DragEnter / DragOver / DragLeave / Drop 四个回调，正好够用。
///
/// ⚠️ 这条不依赖 XAML 的拖放（那套在本机 UAC 关闭时是哑的），是 Win32/OLE 层的，
/// 和 EnableLUA 无关。
///
/// ⚠️ 注册失败不致命：老的 WM_DROPFILES 通道还留着当兜底，只是没有拖动中的反馈而已。
/// </summary>
public static class FileDrop
{
    /// <summary>拖进窗口（每拖进一个新的东西触发一次）</summary>
    public static event Action? Enter;
    /// <summary>离开窗口 / 松手后</summary>
    public static event Action? Leave;
    /// <summary>松手落下，带文件列表</summary>
    public static event Action<string[]>? Dropped;

    [StructLayout(LayoutKind.Sequential)]
    public struct POINTL { public int x; public int y; }

    [ComImport, Guid("00000122-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IDropTarget
    {
        [PreserveSig] int DragEnter([MarshalAs(UnmanagedType.Interface)] object dataObj, uint keyState, POINTL pt, ref uint effect);
        [PreserveSig] int DragOver(uint keyState, POINTL pt, ref uint effect);
        [PreserveSig] int DragLeave();
        [PreserveSig] int Drop([MarshalAs(UnmanagedType.Interface)] object dataObj, uint keyState, POINTL pt, ref uint effect);
    }

    const uint DROPEFFECT_COPY = 1;
    const uint DROPEFFECT_NONE = 0;
    const short CF_HDROP = 15;

    [DllImport("ole32.dll")] static extern int OleInitialize(IntPtr reserved);
    [DllImport("ole32.dll")] static extern int RegisterDragDrop(IntPtr hwnd, [MarshalAs(UnmanagedType.Interface)] IDropTarget target);
    [DllImport("ole32.dll")] static extern void ReleaseStgMedium(ref STGMEDIUM medium);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    static extern uint DragQueryFileW(IntPtr hDrop, uint iFile, StringBuilder? buf, uint cch);

    static DropTarget? _target;          // 必须留引用，被回收掉拖放就没了

    public static bool Attach(IntPtr hwnd)
    {
        try
        {
            OleInitialize(IntPtr.Zero);      // 已经初始化过会返回 S_FALSE / RPC_E_CHANGED_MODE，都不用管
            _target = new DropTarget();
            int hr = RegisterDragDrop(hwnd, _target);
            if (hr != 0) App.Dump($"RegisterDragDrop 失败 hr=0x{hr:X8}（拖放高亮就没有了，落下的老通道还在）");
            return hr == 0;
        }
        catch (Exception e)
        {
            App.Dump("FileDrop.Attach 失败（不影响落下文件）: " + e.Message);
            return false;
        }
    }

    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class DropTarget : IDropTarget
    {
        public int DragEnter(object dataObj, uint keyState, POINTL pt, ref uint effect)
        {
            effect = DROPEFFECT_COPY;        // 让系统显示「复制」光标，用户才知道能放
            try { Enter?.Invoke(); } catch { }
            return 0;                        // S_OK
        }

        public int DragOver(uint keyState, POINTL pt, ref uint effect)
        {
            effect = DROPEFFECT_COPY;
            return 0;
        }

        public int DragLeave()
        {
            try { Leave?.Invoke(); } catch { }
            return 0;
        }

        public int Drop(object dataObj, uint keyState, POINTL pt, ref uint effect)
        {
            effect = DROPEFFECT_COPY;
            try
            {
                var files = Files(dataObj);
                Leave?.Invoke();
                if (files is { Length: > 0 }) Dropped?.Invoke(files);
            }
            catch (Exception e) { App.Dump("Drop 处理失败: " + e.Message); }
            return 0;
        }

        /// <summary>从 IDataObject 里把落下的文件路径抠出来（CF_HDROP）</summary>
        static string[]? Files(object dataObj)
        {
            if (dataObj is not IDataObject dao) return null;
            var fmt = new FORMATETC
            {
                cfFormat = CF_HDROP,
                dwAspect = DVASPECT.DVASPECT_CONTENT,
                lindex = -1,
                tymed = TYMED.TYMED_HGLOBAL,
            };
            if (dao.QueryGetData(ref fmt) != 0) return null;

            var medium = new STGMEDIUM();
            try
            {
                dao.GetData(ref fmt, out medium);
                uint count = DragQueryFileW(medium.unionmember, 0xFFFFFFFF, null, 0);
                var list = new List<string>((int)count);
                for (uint i = 0; i < count; i++)
                {
                    uint len = DragQueryFileW(medium.unionmember, i, null, 0);
                    if (len == 0) continue;
                    var sb = new StringBuilder((int)len + 2);
                    DragQueryFileW(medium.unionmember, i, sb, (uint)sb.Capacity);
                    list.Add(sb.ToString());
                }
                return list.ToArray();
            }
            finally
            {
                ReleaseStgMedium(ref medium);
            }
        }
    }
}
