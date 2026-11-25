// Public download entry point; the installed AWQM.exe remains the application.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class DownloadLauncher
{
    private const string Title = "Aruba Wireless Quality Monitor";
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr FindWindow(string className, string title);

    internal static Version ParseVersion(string value)
    {
        Version result;
        if (value == null || value.Length > 64 ||
            !Version.TryParse(value.Trim(), out result) || result.Build < 0 || result.Revision >= 0)
            throw new InvalidDataException("AWQM 버전 정보가 올바르지 않습니다.");
        return result;
    }

    // 0: install/update; 1: user app; 2: machine app.
    internal static int SelectExisting(Version bundled, Version user, Version machine)
    {
        if (user != null && user >= bundled && (machine == null || user >= machine)) return 1;
        if (machine != null && machine >= bundled) return 2;
        return 0;
    }

    private static Version InstalledVersion(string directory)
    {
        string exe = Path.Combine(directory, "AWQM.exe");
        string marker = Path.Combine(directory, "VERSION.txt");
        if (!File.Exists(exe) && !File.Exists(marker)) return null;
        if (!File.Exists(exe) || !File.Exists(marker) ||
            !Directory.Exists(Path.Combine(directory, "_internal")) || new FileInfo(marker).Length > 64)
            throw new InvalidDataException("기존 AWQM 설치가 불완전합니다. 설치 상태를 확인하세요.");
        return ParseVersion(File.ReadAllText(marker));
    }

    private static bool Elevated()
    {
        using (WindowsIdentity id = WindowsIdentity.GetCurrent())
            return new WindowsPrincipal(id).IsInRole(WindowsBuiltInRole.Administrator);
    }

    private static int LaunchInstalled(string directory)
    {
        string exe = Path.Combine(directory, "AWQM.exe");
        if (String.Equals(Path.GetFullPath(exe), Path.GetFullPath(Assembly.GetExecutingAssembly().Location),
                          StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("다운로드 EXE를 설치 폴더 안의 AWQM.exe에 덮어쓰지 마세요.");
        using (Process process = Process.Start(new ProcessStartInfo(exe) {
            WorkingDirectory = directory, UseShellExecute = false }))
        {
            if (process == null) throw new InvalidOperationException("AWQM을 시작할 수 없습니다.");
        }
        return 0;
    }

    // Quote for Windows CreateProcess, including trailing slashes and embedded quotes.
    internal static string Quote(string arg)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in arg)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(ch);
            slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }

    private static int RunSetup(string[] args, bool elevated)
    {
        // Never use a user-writable temporary parent for an already-elevated process.
        string parent = elevated ? Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles) : Path.GetTempPath();
        string directory = Path.Combine(parent, "AWQM-bootstrap-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        string setup = Path.Combine(directory, "AWQM-Setup.exe");
        try
        {
            byte[] expected;
            using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("AWQM.Setup"))
            {
                if (resource == null) throw new InvalidDataException("내장 설치 파일이 누락되었습니다.");
                using (var sha = SHA256.Create()) expected = sha.ComputeHash(resource);
                resource.Position = 0;
                using (var output = new FileStream(setup, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    resource.CopyTo(output);
            }
            // Verify the embedded payload and prevent modification while it executes.
            using (var locked = new FileStream(setup, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                using (var sha = SHA256.Create())
                    if (!expected.SequenceEqual(sha.ComputeHash(locked)))
                        throw new InvalidDataException("내장 설치 파일 검증에 실패했습니다.");
                using (Process process = Process.Start(new ProcessStartInfo(setup) {
                    Arguments = String.Join(" ", args.Select(Quote)),
                    WorkingDirectory = directory, UseShellExecute = true }))
                {
                    if (process == null) throw new InvalidOperationException("설치 프로그램을 시작할 수 없습니다.");
                    process.WaitForExit();
                    return process.ExitCode;
                }
            }
        }
        finally
        {
            try { File.Delete(setup); Directory.Delete(directory, false); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            bool elevated = Elevated();
            // Explicit options preserve the admin/service/silent installation contract.
            if (args.Length != 0) return RunSetup(args, elevated);
            if (elevated)
                throw new InvalidOperationException("일반 사용자 권한으로 실행하세요. 서비스 설치는 /ALLUSERS를 사용하세요.");
            string sid;
            using (WindowsIdentity id = WindowsIdentity.GetCurrent()) sid = id.User.Value;
            using (var gate = new Mutex(false, @"Local\AWQM.Download." + sid))
            {
                bool acquired = false;
                try
                {
                    try { acquired = gate.WaitOne(TimeSpan.FromMinutes(3)); }
                    catch (AbandonedMutexException) { acquired = true; }
                    if (!acquired) throw new InvalidOperationException("다른 AWQM 설치가 진행 중입니다.");
                    string user = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "AWQM");
                    string machine = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "AWQM");
                    Version bundled = ParseVersion(BuildInfo.Version);
                    int action = SelectExisting(bundled, InstalledVersion(user), InstalledVersion(machine));
                    if (action == 1) return LaunchInstalled(user);
                    if (action == 2) return LaunchInstalled(machine);
                    // Do not terminate the user's running collector or overwrite its executable.
                    if (FindWindow("AWQM.Desktop", Title) != IntPtr.Zero)
                        throw new InvalidOperationException("업데이트하려면 트레이에서 AWQM 종료를 선택한 뒤 다시 실행하세요.");
                    return RunSetup(new string[0], false);
                }
                finally { if (acquired) gate.ReleaseMutex(); }
            }
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, Title, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
    }
}
