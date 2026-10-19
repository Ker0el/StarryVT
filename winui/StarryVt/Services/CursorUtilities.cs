// 从 .cur / .ani 文件加载自定义光标，并设到 UIElement 上。
//
// ★ 为什么不是 SetSystemCursor：那样是**全局**替换系统光标 —— 用户切出去别的程序
//   也是这套光标，而且程序万一崩了光标就留在那儿没人还原。这里走的是应用内的路子，
//   光标只在本软件的控件上生效，退出即恢复，没有残留风险。
//
// ★ 为什么不是拦 WM_SETCURSOR：WinUI 3 里所有 XAML 内容都在**同一个 HWND** 里，
//   框架自己按控件管光标（文本框 I 形、链接手形）。在窗口层拦会跟它打架，
//   而且 .ani 动画没有公开的 XAML 接口。
//
// ★ 正路是 Microsoft.UI.Input.InputCursor + UIElement.ProtectedCursor，
//   但 WinAppSDK 没给「从文件加载」的公开 API —— 要用 interop 接口
//   IInputCursorStaticsInterop 把原生 HCURSOR 包成 InputCursor。
//   做法出自 Simon Mourier（WinRT/WinUI 领域的老手）：
//   https://www.simonmourier.com/blog/Cursor-cur-in-WinUI-3/
//
// ★ ProtectedCursor 是 protected 成员，从外部设要用反射；
//   官方文档明写：**设在某个元素上，指针在它或它的任何后代上时都用这个值**
//   （后代自己设了才覆盖）—— 所以设在根元素上就等于「整个界面」。
#nullable enable

using System;
using System.ComponentModel;
using System.Reflection;
using System.Runtime.InteropServices;
using Microsoft.UI.Input;
using Microsoft.UI.Xaml;

namespace Starry.Vt.App.Services;

public static class CursorUtilities
{
    /// <summary>把光标设到这个元素（及它的所有后代）上。传 null 恢复系统默认。</summary>
    public static void ChangeCursor(this UIElement element, InputCursor? cursor)
    {
        ArgumentNullException.ThrowIfNull(element);
        // ProtectedCursor 是 protected 的，只能反射写
        typeof(UIElement).InvokeMember("ProtectedCursor",
            BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.SetProperty,
            null, element, new object?[] { cursor });
    }

    /// <summary>从 .cur / .ani 文件加载。吃不下（文件不在 / 格式不认）就返回 null，不抛。</summary>
    public static InputCursor? TryLoad(string filePath)
    {
        try
        {
            if (!System.IO.File.Exists(filePath)) return null;
            var hcursor = LoadCursorFromFileW(filePath);
            if (hcursor == 0) return null;      // 认不出这个文件就不动它，别把界面搞坏
            return CreateCursorFromHCURSOR(hcursor);
        }
        catch
        {
            return null;
        }
    }

    public static InputCursor? CreateCursorFromHCURSOR(nint hcursor)
    {
        if (hcursor == 0) return null;

        const string classId = "Microsoft.UI.Input.InputCursor";
        _ = WindowsCreateString(classId, classId.Length, out var hs);
        try
        {
            if (RoGetActivationFactory(hs, typeof(IActivationFactory).GUID, out var fac) != 0 || fac is null)
                return null;
            if (fac is not IInputCursorStaticsInterop interop)
                return null;
            if (interop.CreateFromHCursor(hcursor, out var cursorAbi) != 0 || cursorAbi == 0)
                return null;
            return WinRT.MarshalInspectable<InputCursor>.FromAbi(cursorAbi);
        }
        finally
        {
            _ = WindowsDeleteString(hs);
        }
    }

    [ComImport, Guid("ac6f5065-90c4-46ce-beb7-05e138e54117"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IInputCursorStaticsInterop
    {
        // IInspectable 的三个用不上的方法，占位必须留着（顺序不能动）
        void GetIids();
        void GetRuntimeClassName();
        void GetTrustLevel();

        [PreserveSig]
        int CreateFromHCursor(nint hcursor, out nint inputCursor);
    }

    [ComImport, Guid("00000035-0000-0000-c000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IActivationFactory
    {
        void GetIids();
        void GetRuntimeClassName();
        void GetTrustLevel();

        [PreserveSig]
        int ActivateInstance(out nint instance);
    }

    [DllImport("api-ms-win-core-winrt-l1-1-0.dll")]
    private static extern int RoGetActivationFactory(nint runtimeClassId,
        [MarshalAs(UnmanagedType.LPStruct)] Guid iid, out IActivationFactory? factory);

    [DllImport("user32", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern nint LoadCursorFromFileW(string name);

    [DllImport("api-ms-win-core-winrt-string-l1-1-0", CharSet = CharSet.Unicode)]
    private static extern int WindowsCreateString(string? sourceString, int length, out nint @string);

    [DllImport("api-ms-win-core-winrt-string-l1-1-0", CharSet = CharSet.Unicode)]
    private static extern int WindowsDeleteString(nint @string);
}
