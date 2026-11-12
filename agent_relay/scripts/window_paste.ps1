# agent-relay: type one message into a desktop agent's chat box and CONFIRM it was sent.
#
# How it confirms: it photographs the chat box strip empty, after pasting, and after sending.
#   - paste did not change the strip  -> the box is not where we expect (e.g. a "New chat" screen): undo, exit 5
#   - after sending, the strip is back to empty or changed again -> SENT
#   - otherwise the text is still there -> clear it (so the next try does not duplicate it), exit 3
# It never presses the send button twice: in many apps that button turns into "Stop" while the agent works.
# If the conversation area is still changing (the agent is busy), it does nothing and exits 4.
# The user's clipboard and foreground window are restored at the end.
param(
  [Parameter(Mandatory = $true)][string]$Process,
  [Parameter(Mandatory = $true)][string]$File,
  [string]$Title = '',
  [int]$InputFromBottom = 80,
  [ValidateSet('enter', 'button')][string]$Send = 'enter',
  [int]$ButtonFromRight = 80,
  [int]$ButtonFromBottom = 70,
  [string]$Executable = ''
)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices;
public class RelayWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(int f, int x, int y, int d, int e);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, int f, int e);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint f);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  public delegate bool CB(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(CB cb, IntPtr p);
  public static IntPtr Find(int[] pids, string title) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, p) => {
      int pid; GetWindowThreadProcessId(h, out pid);
      var s = new StringBuilder(256); GetWindowText(h, s, 256);
      if (Array.IndexOf(pids, pid) >= 0 && IsWindowVisible(h) && s.ToString().Contains(title)) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  public struct R { public int L, T, Rt, B; }
}
"@

if ($Executable -and -not (Get-Process $Process -ErrorAction SilentlyContinue)) { Start-Process $Executable; Start-Sleep 5 }
$h = [RelayWin]::Find(@(Get-Process $Process -ErrorAction SilentlyContinue | ForEach-Object Id), $Title)
if ($h -eq [IntPtr]::Zero) { "FAILED: no visible window for process '$Process' (title contains '$Title')"; exit 2 }

$text = Get-Content $File -Raw -Encoding utf8
$previousWindow = [RelayWin]::GetForegroundWindow()
$previousClipboard = Get-Clipboard -Raw

function Restore([string]$msg, [int]$code) {
  if ($previousClipboard) { Set-Clipboard -Value $previousClipboard } else { Set-Clipboard -Value ' ' }
  [RelayWin]::SetForegroundWindow($previousWindow) | Out-Null
  $msg; exit $code
}
function Focus {
  if ([RelayWin]::IsIconic($h)) { [RelayWin]::ShowWindow($h, 9) | Out-Null }
  [RelayWin]::keybd_event(0x12, 0, 0, 0); [RelayWin]::keybd_event(0x12, 0, 2, 0)
  [RelayWin]::BringWindowToTop($h) | Out-Null; [RelayWin]::SetForegroundWindow($h) | Out-Null
  Start-Sleep -Milliseconds 700
  return ([RelayWin]::GetForegroundWindow() -eq $h)
}
function Click([int]$fromRight, [int]$fromBottom, [switch]$Corner) {
  $r = New-Object RelayWin+R; [RelayWin]::GetWindowRect($h, [ref]$r) | Out-Null
  $x = if ($Corner) { $r.Rt - $fromRight } else { [int]($r.L + ($r.Rt - $r.L) * 0.5) }
  [RelayWin]::SetCursorPos($x, $r.B - $fromBottom)
  [RelayWin]::mouse_event(2, 0, 0, 0, 0); [RelayWin]::mouse_event(4, 0, 0, 0, 0)
  Start-Sleep -Milliseconds 300
}
function BoxPixels {
  # the left/middle part of the chat box strip (buttons on the right change by themselves)
  $r = New-Object RelayWin+R; [RelayWin]::GetWindowRect($h, [ref]$r) | Out-Null
  $w = [int](($r.Rt - $r.L) * 0.55); $hh = 34
  $x0 = $r.L + [int](($r.Rt - $r.L) * 0.08); $y0 = $r.B - $InputFromBottom - 17
  $b = New-Object System.Drawing.Bitmap $w, $hh; $g = [System.Drawing.Graphics]::FromImage($b)
  $g.CopyFromScreen($x0, $y0, 0, 0, $b.Size); $g.Dispose()
  $px = New-Object System.Collections.Generic.List[int]
  for ($x = 0; $x -lt $w; $x += 3) { for ($y = 0; $y -lt $hh; $y += 3) { $c = $b.GetPixel($x, $y); $px.Add($c.R + $c.G + $c.B) } }
  $b.Dispose(); return , $px.ToArray()
}
function WindowPixels {
  # conversation area only: skips the left sidebar, the title bar and the bottom 110 px
  # (spinners down there would make an idle agent look busy forever)
  $r = New-Object RelayWin+R; [RelayWin]::GetWindowRect($h, [ref]$r) | Out-Null
  $w = $r.Rt - $r.L; $hh = $r.B - $r.T
  if ($w -lt 100 -or $hh -lt 200) { return , @() }
  $b = New-Object System.Drawing.Bitmap $w, $hh; $g = [System.Drawing.Graphics]::FromImage($b)
  $dc = $g.GetHdc(); [RelayWin]::PrintWindow($h, $dc, 2) | Out-Null; $g.ReleaseHdc($dc); $g.Dispose()
  $px = New-Object System.Collections.Generic.List[int]
  for ($x = [int]($w * 0.25); $x -lt $w - 10; $x += 8) { for ($y = 60; $y -lt $hh - 110; $y += 8) { $c = $b.GetPixel($x, $y); $px.Add($c.R + $c.G + $c.B) } }
  $b.Dispose(); return , $px.ToArray()
}
function Changed($a, $b2) {
  if ($a.Length -ne $b2.Length -or $a.Length -eq 0) { return $true }
  $diff = 0; for ($k = 0; $k -lt $a.Length; $k++) { if ([Math]::Abs($a[$k] - $b2[$k]) -gt 60) { $diff++ } }
  return ($diff / [double]$a.Length) -gt 0.01
}

# Busy? Two pictures of the conversation 1.5 s apart, without bringing the window forward.
$f1 = WindowPixels; Start-Sleep -Milliseconds 1500; $f2 = WindowPixels
if ($f1.Length -gt 0 -and (Changed $f1 $f2)) { "BUSY: $Process is working, try later"; exit 4 }

if (-not (Focus)) { Restore "FAILED: could not focus $Process" 2 }
Click 0 $InputFromBottom
Start-Sleep -Milliseconds 300
$empty = BoxPixels
Set-Clipboard -Value $text
[System.Windows.Forms.SendKeys]::SendWait("^v"); Start-Sleep -Milliseconds 1200
$full = BoxPixels
if (-not (Changed $empty $full)) {
  [System.Windows.Forms.SendKeys]::SendWait("^a"); Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait("{DEL}")
  Restore "FAILED: the chat box of $Process is not where expected (start screen / new chat?)" 5
}
if ($Send -eq 'button') { Click $ButtonFromRight $ButtonFromBottom -Corner }
else { [System.Windows.Forms.SendKeys]::SendWait("{ENTER}") }
foreach ($wait in 2000, 2500) {
  Start-Sleep -Milliseconds $wait
  $after = BoxPixels
  if (-not (Changed $empty $after) -or (Changed $full $after)) { Restore "SENT and confirmed ($Process)" 0 }
}
Click 0 $InputFromBottom
[System.Windows.Forms.SendKeys]::SendWait("^a"); Start-Sleep -Milliseconds 100
[System.Windows.Forms.SendKeys]::SendWait("{DEL}")
Restore "FAILED: the text was not sent in $Process (cleared it, will retry later)" 3
