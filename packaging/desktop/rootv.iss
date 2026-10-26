; Inno Setup script for the RooTV (小鼠电视, formerly Xiaozhi TV / 小智电视) PC client
; on Windows 10/11 x64.
; Design 5.2 and Q7: per-machine install (one UAC prompt) into Program Files, a
; Start-menu entry, and an inbound firewall rule for the core on the PRIVATE
; profile only. The login item is per user and registered by the tray itself on
; its first run (HKCU Run, a toggle in its menu), not here: an elevated installer
; would write it for the administrator account, which need not be the user.
;
; Upgrading from Xiaozhi TV (0.2.5-beta and older, installed in
; Program Files\Xiaozhi TV): the AppId is the same, so Windows lists one program,
; and before installing, the old installation's own uninstaller is run silently
; (PrepareToInstall below). It stops the old tray and core, removes its files,
; Start-menu entries, firewall rule and login item, and keeps the household's
; data; the new tray moves %LOCALAPPDATA%\XiaozhiTV to %LOCALAPPDATA%\RooTV on its
; first start (tools/desktop_folders.py) and registers its own login item.
;
; Compiled by the build-desktop workflow:
;   iscc /DSourceDir=<stage\RooTV> /DAppVersion=<ver> /DOutputDir=<dir> rootv.iss
; The file is UTF-8 with a byte-order mark (Inno Setup needs it for Chinese text).

#ifndef AppVersion
  #define AppVersion "0.2.0-beta"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build-desktop\stage\RooTV"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\build-desktop"
#endif
#define AppExe "RooTV.exe"
#define CoreExe "core\rootv-core.exe"
#define FirewallRule "RooTV"
#define LegacyExe "XiaozhiTV.exe"
#define LegacyFirewallRule "Xiaozhi TV"

[Setup]
; Unchanged since Xiaozhi TV: an upgrade, not a second program.
AppId={{6F1C2B7E-3D44-4A8E-9B1F-7A5C2E9D4B10}
AppName=小鼠电视 RooTV
AppVersion={#AppVersion}
AppVerName=小鼠电视 RooTV {#AppVersion}
AppPublisher=ai-passport-tv contributors
AppPublisherURL=https://github.com/rooma1989/ai-passport-tv
AppSupportURL=https://github.com/rooma1989/ai-passport-tv/issues
DefaultDirName={autopf}\RooTV
; Not the previous folder: that is Program Files\Xiaozhi TV for an upgrade, and
; the old uninstaller removes it.
UsePreviousAppDir=no
DefaultGroupName=小鼠电视
UsePreviousGroup=no
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=RooTV-{#AppVersion}-windows-x64-setup
SetupIconFile={#SourceDir}\rootv.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName=小鼠电视 RooTV
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Ask which language, with Chinese (the first one) preselected whatever the
; Windows display language; a silent install uses Chinese.
ShowLanguageDialog=yes
LanguageDetectionMethod=none
; Close a running tray (and with it the core) before replacing its files.
CloseApplications=yes
RestartApplications=no

[Languages]
; Simplified Chinese first: the default. Inno Setup ships no Chinese, so the
; community translation is vendored beside this script (ChineseSimplified.isl,
; pinned to the Inno Setup version CI uses; see ChineseSimplified-SOURCE.txt).
; English stays selectable in the language dialog.
Name: "zh"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面上放一个图标 / Desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\小鼠电视"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载小鼠电视"; Filename: "{uninstallexe}"
Name: "{autodesktop}\小鼠电视"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[InstallDelete]
; Shortcuts of the old name, should the old uninstaller not have run.
Type: files; Name: "{autoprograms}\小智电视\小智电视.lnk"
Type: files; Name: "{autoprograms}\小智电视\卸载小智电视.lnk"
Type: dirifempty; Name: "{autoprograms}\小智电视"
Type: files; Name: "{autodesktop}\小智电视.lnk"

[Run]
; One rule for the core program: its children (web front, media servers) are the
; same executable, so they are covered too. Private profile only: on a network
; marked Public (a cafe, or a home Wi-Fi Windows mislabelled) nothing is opened,
; and the tray explains how to mark the home network Private.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyFirewallRule}"""; Flags: runhidden; StatusMsg: "正在设置防火墙…"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden; StatusMsg: "正在设置防火墙…"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\{#CoreExe}"" enable=yes profile=private"; Flags: runhidden; StatusMsg: "正在设置防火墙…"
Filename: "{app}\{#AppExe}"; Description: "启动小鼠电视"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop the tray (the core and its children are in its job object and end with it).
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopTray"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM rootv-core.exe"; Flags: runhidden; RunOnceId: "StopCore"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden; RunOnceId: "DeleteFirewallRule"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#LegacyFirewallRule}"""; Flags: runhidden; RunOnceId: "DeleteLegacyFirewallRule"

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1C2B7E-3D44-4A8E-9B1F-7A5C2E9D4B10}_is1';

// The Xiaozhi TV installation this one replaces: run its uninstaller silently
// and wait for its program to be gone (the uninstaller hands over to a copy of
// itself, so its own exit is not the end).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  OldDir, Uninstaller: String;
  ResultCode, I: Integer;
begin
  Result := '';
  if not RegQueryStringValue(HKLM, UninstallKey, 'InstallLocation', OldDir) then
    exit;
  OldDir := RemoveBackslashUnlessRoot(OldDir);
  if (OldDir = '') or not FileExists(AddBackslash(OldDir) + '{#LegacyExe}') then
    exit;
  if not RegQueryStringValue(HKLM, UninstallKey, 'UninstallString', Uninstaller) then
    exit;
  Uninstaller := RemoveQuotes(Uninstaller);
  if not FileExists(Uninstaller) then
    exit;
  Log('Removing the previous Xiaozhi TV installation in ' + OldDir);
  Exec(Uninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  for I := 1 to 120 do
  begin
    if not FileExists(AddBackslash(OldDir) + '{#LegacyExe}') then
      break;
    Sleep(500);
  end;
  if FileExists(AddBackslash(OldDir) + '{#LegacyExe}') then
    Log('The previous installation is still there (uninstaller exit code ' + IntToStr(ResultCode) + '); installing anyway');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The tray's login item (per user), under the new and the old name. The
  // household's data in %LOCALAPPDATA%\RooTV is kept: it holds the accounts,
  // devices and keys.
  if CurUninstallStep = usUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'RooTV');
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', 'RooTV');
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'XiaozhiTV');
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', 'XiaozhiTV');
  end;
end;
