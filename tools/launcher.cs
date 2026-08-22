// Shioaji 測試精靈桌面停靠器：啟動提示（轉圈＋即時狀態行）→ 起 pythonw →
// 輪詢 .runtime\.app-ready 就緒訊號關閉提示。狀態行讀 .runtime\.app-status
// （python 端逐階段覆寫）；python 行程早退＝啟動失敗，提示轉為錯誤指引並
// 留窗待使用者關閉。named mutex 忽略啟動期間重複雙擊。
//
// 移植自 fcn-pricing 的 tools/launcher.cs，改動：
//   - ready／status 檔案位置由 bundle 根層改為隱藏子目錄 .runtime（見
//     shioaji_wizard.sjenv.ensure_runtime_dir／RUNTIME）。
//   - 啟動指令改 `pythonw.exe -m shioaji_wizard --root "<root>"`（無
//     --data-root；根目錄即 .env／Sinopac.pfx／.runtime 所在）。
//   - 新增 RehideTopLevel：bundle 根層除 wizard.exe／.env／*.pfx／*.log
//     外的項目一律 best-effort 設 Windows hidden 屬性——build_bundle.py
//     的 hide_bundle_top_level 已在打包時設過一次，但 zip 解壓不保證保留
//     該屬性，故啟動時再補一次，確保使用者每次看到的根層都是乾淨的。
//
// build_bundle.py 以內建 csc.exe 編譯（/target:winexe＋WinForms 參考）。
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Windows.Forms;

static class Launcher
{
    static readonly string[] SpinnerFrames = { "◐", "◓", "◑", "◒" };

    [STAThread]
    static void Main()
    {
        bool createdNew;
        using (var mutex = new Mutex(true, "ShioajiWizard-Launcher", out createdNew))
        {
            if (!createdNew)
            {
                return; // 啟動中再次雙擊：忽略（防重複開兩份 app）
            }
            string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');

            RehideTopLevel(root, "wizard.exe");

            string runtimeDir = Path.Combine(root, ".runtime");
            try { Directory.CreateDirectory(runtimeDir); } catch (Exception) { }
            string ready = Path.Combine(runtimeDir, ".app-ready");
            string status = Path.Combine(runtimeDir, ".app-status");
            try { File.Delete(ready); } catch (Exception) { }
            try { File.Delete(status); } catch (Exception) { }

            var psi = new ProcessStartInfo
            {
                FileName = Path.Combine(root, "python", "pythonw.exe"),
                Arguments = "-m shioaji_wizard --root \"" + root + "\"",
                WorkingDirectory = root,
                UseShellExecute = false,
            };
            Process proc = Process.Start(psi);

            RunSplash(ready, status, proc);
        }
    }

    // bundle 根層除 keepExeName／.env／*.pfx／*.log 外全設 Windows hidden
    // 屬性。best-effort：單一項目失敗（權限、檔案被鎖等）不影響其餘項目，
    // 也不擋啟動——這只是外觀整潔，不是正確性前提。
    static void RehideTopLevel(string root, string keepExeName)
    {
        string[] entries;
        try
        {
            entries = Directory.GetFileSystemEntries(root);
        }
        catch (Exception)
        {
            return;
        }
        foreach (string entry in entries)
        {
            string name = Path.GetFileName(entry);
            if (string.Equals(name, keepExeName, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            if (string.Equals(name, ".env", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            string ext = Path.GetExtension(name);
            if (string.Equals(ext, ".pfx", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(ext, ".log", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(ext, ".bat", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            try
            {
                if (Directory.Exists(entry))
                {
                    var di = new DirectoryInfo(entry);
                    if ((di.Attributes & FileAttributes.Hidden) == 0)
                    {
                        di.Attributes |= FileAttributes.Hidden;
                    }
                }
                else if (File.Exists(entry))
                {
                    FileAttributes attrs = File.GetAttributes(entry);
                    if ((attrs & FileAttributes.Hidden) == 0)
                    {
                        File.SetAttributes(entry, attrs | FileAttributes.Hidden);
                    }
                }
            }
            catch (Exception) { }
        }
    }

    static void RunSplash(string ready, string status, Process proc)
    {
        var form = new Form
        {
            Text = "Shioaji 測試精靈",
            FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false,
            MinimizeBox = false,
            StartPosition = FormStartPosition.CenterScreen,
            ClientSize = new Size(408, 92),
            TopMost = true,
            Font = new Font("Segoe UI", 9F),
        };
        var spinner = new Label
        {
            Text = SpinnerFrames[0],
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleCenter,
            Left = 12,
            Top = 22,
            Width = 36,
            Height = 40,
            Font = new Font("Segoe UI", 18F),
            ForeColor = Color.FromArgb(0, 90, 158),
        };
        var title = new Label
        {
            Text = "正在啟動 Shioaji 測試精靈…",
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleLeft,
            Left = 56,
            Top = 18,
            Width = 340,
            Height = 26,
            Font = new Font("Segoe UI", 10F, FontStyle.Bold),
        };
        var detail = new Label
        {
            Text = "準備執行環境…",
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleLeft,
            Left = 56,
            Top = 46,
            Width = 340,
            Height = 24,
            ForeColor = Color.FromArgb(96, 96, 96),
        };
        form.Controls.Add(spinner);
        form.Controls.Add(title);
        form.Controls.Add(detail);

        int frame = 0;
        bool failed = false;
        DateTime deadline = DateTime.UtcNow.AddSeconds(180);
        var timer = new System.Windows.Forms.Timer { Interval = 100 };
        timer.Tick += delegate
        {
            if (failed)
            {
                return; // 錯誤狀態凍結畫面，等使用者自行關窗
            }
            frame = (frame + 1) % SpinnerFrames.Length;
            spinner.Text = SpinnerFrames[frame];
            try
            {
                if (File.Exists(status))
                {
                    string text = ReadShared(status).Trim();
                    if (text.Length > 0)
                    {
                        detail.Text = text;
                    }
                }
            }
            catch (Exception) { }
            if (File.Exists(ready) || DateTime.UtcNow > deadline)
            {
                timer.Stop();
                form.Close();
                return;
            }
            if (proc.HasExited)
            {
                failed = true;
                spinner.Text = "✕";
                spinner.ForeColor = Color.Firebrick;
                title.Text = "啟動失敗";
                detail.Text = "請雙擊「啟動（除錯）.bat」查看即時錯誤訊息";
                detail.ForeColor = Color.Firebrick;
            }
        };
        timer.Start();
        Application.Run(form);
    }

    static string ReadShared(string path)
    {
        using (var fs = new FileStream(
            path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        using (var reader = new StreamReader(fs))
        {
            return reader.ReadToEnd();
        }
    }
}
