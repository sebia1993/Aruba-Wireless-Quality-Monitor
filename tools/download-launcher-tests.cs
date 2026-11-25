using System;
using System.IO;
internal static class LauncherPolicyTests
{
    private static Version V(string value) { return DownloadLauncher.ParseVersion(value); }
    private static void Equal(int expected, int actual)
    {
        if (expected != actual) throw new Exception("Unexpected launch action: " + actual);
    }
    private static int Main()
    {
        Equal(0, DownloadLauncher.SelectExisting(V("0.48.0"), null, null));
        Equal(0, DownloadLauncher.SelectExisting(V("0.48.0"), V("0.47.0"), null));
        Equal(1, DownloadLauncher.SelectExisting(V("0.48.0"), V("0.48.0"), null));
        Equal(1, DownloadLauncher.SelectExisting(V("0.48.0"), V("0.49.0"), null));
        Equal(1, DownloadLauncher.SelectExisting(V("0.9.0"), V("0.10.0"), null));
        Equal(2, DownloadLauncher.SelectExisting(V("0.48.0"), null, V("0.48.0")));
        Equal(2, DownloadLauncher.SelectExisting(V("0.48.0"), V("0.48.0"), V("0.49.0")));
        Equal(1, DownloadLauncher.SelectExisting(V("0.48.0"), V("0.49.0"), V("0.48.0")));
        foreach (string value in new string[] { "", "0.48", "0.48.0.1", "0.48.0-dev", "invalid" })
        {
            try { V(value); throw new Exception("Accepted invalid version"); }
            catch (InvalidDataException) { }
        }
        if (DownloadLauncher.Quote(@"C:\Path With Spaces\") != "\"C:\\Path With Spaces\\\\\"")
            throw new Exception("Incorrect Windows argument quoting");
        Console.WriteLine("Launcher policy tests passed.");
        return 0;
    }
}
