; Inno Setup script for the Xiaozhi TV (小智电视) PC client on Windows 10/11 x64.
; Design 5.2 and Q7: per-machine install (one UAC prompt) into Program Files, a
; Start-menu entry, and an inbound firewall rule for the core on the PRIVATE
; profile only. The login item is per user and registered by the tray itself on
; its first run (HKCU Run, a toggle in its menu), not here: an elevated installer
; would write it for the administrator account, which need not be the user.
;
; Compiled by the build-desktop workflow:
;   iscc /DSourceDir=<stage\Xiaozhi TV> /DAppVersion=<ver> /DOutputDir=<dir> xiaozhi-tv.iss
; The file is UTF-8 with a byte-order mark (Inno Setup needs it for Chinese text).

#ifndef AppVersion
  #define AppVersion "0.2.0-beta"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build-desktop\stage\Xiaozhi TV"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\build-desktop"
#endif
#define AppExe "XiaozhiTV.exe"
#define CoreExe "core\xiaozhi-tv-core.exe"
#define FirewallRule "Xiaozhi TV"

[Setup]
AppId={{6F1C2B7E-3D44-4A8E-9B1F-7A5C2E9D4B10}
AppName=小智电视 Xiaozhi TV
AppVersion={#AppVersion}
AppVerName=小智电视 {#AppVersion}
AppPublisher=ai-passport-tv contributors
AppPublisherURL=https://github.com/rooma1989/ai-passport-tv
AppSupportURL=https://github.com/rooma1989/ai-passport-tv/issues
DefaultDirName={autopf}\Xiaozhi TV
DefaultGroupName=小智电视
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=XiaozhiTV-{#AppVersion}-windows-x64-setup
SetupIconFile={#SourceDir}\xiaozhi-tv.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName=小智电视 Xiaozhi TV
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
Name: "{group}\小智电视"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载小智电视"; Filename: "{uninstallexe}"
Name: "{autodesktop}\小智电视"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; One rule for the core program: its children (web front, media servers) are the
; same executable, so they are covered too. Private profile only: on a network
; marked Public (a cafe, or a home Wi-Fi Windows mislabelled) nothing is opened,
; and the tray explains how to mark the home network Private.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden; StatusMsg: "正在设置防火墙…"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\{#CoreExe}"" enable=yes profile=private"; Flags: runhidden; StatusMsg: "正在设置防火墙…"
Filename: "{app}\{#AppExe}"; Description: "启动小智电视"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop the tray (the core and its children are in its job object and end with it).
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopTray"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM xiaozhi-tv-core.exe"; Flags: runhidden; RunOnceId: "StopCore"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#FirewallRule}"""; Flags: runhidden; RunOnceId: "DeleteFirewallRule"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The tray's login item (per user). The household's data in
  // %LOCALAPPDATA%\XiaozhiTV is kept: it holds the accounts, devices and keys.
  if CurUninstallStep = usUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'XiaozhiTV');
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', 'XiaozhiTV');
  end;
end;
