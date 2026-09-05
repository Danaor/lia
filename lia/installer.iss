; ============================================================
; Lia — Inno Setup script
; ============================================================
; Builds a single-file Setup.exe wizard ("Next/Next/Finish")
; that installs Lia.exe to Program Files, creates Start
; Menu / Desktop shortcuts, and optionally registers a Scheduled
; Task with HIGHEST run-level so the app auto-starts at login
; with admin rights and no UAC prompt.
;
; Usage:
;   1. Build the standalone exe:    python build.py
;   2. Compile the installer:       python build_installer.py
;      (or directly:                "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss)
;
; Output:
;   installer_output\Lia-Setup-<version>.exe
; ============================================================

#define AppName       "Lia"
#define AppVersion    "1.4.1"
#define AppPublisher  "Naor Daniel"
#define AppURL        "https://github.com/Danaor/lia"
#define AppExeName    "Lia.exe"
#define TaskName      "Lia"

[Setup]
; Unique identifier — DO NOT change between releases or upgrades will install
; alongside instead of replacing.
AppId={{826613BA-1DFC-4C6E-A0E5-6CA97A5A74FB}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=installer_output
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=lia.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
; LEAST PRIVILEGE (2026-08-28 audit): per-user install by default, no admin.
; The exe itself is asInvoker. Users who dictate into elevated windows can
; run the installer elevated (/ALLUSERS or the dialog) and pick the elevated
; autostart task, which recreates the old no-UAC-at-boot behavior.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
ShowLanguageDialog=auto
DisableWelcomePage=no
WizardImageStretch=no
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "hebrew";  MessagesFile: "compiler:Languages\Hebrew.isl"

[CustomMessages]
english.AutoStartTask=Start {#AppName} automatically at Windows login
english.AutoStartElevated=Elevated auto-start: run ALL of {#AppName} with highest privileges at login (enables dictation into admin windows)
english.AutoStartTaskGroup=Auto-start:
english.PostInstallInfo=Lia is installed.%n%nIt works out of the box — no API keys needed. Dictate in Hebrew by holding Ctrl+Space; the local Hebrew model downloads once on your first dictation (needs internet that one time).%n%nRight-click the system tray icon (microphone) for options, meetings, and to add cloud API keys if you want them.

hebrew.AutoStartTask=הפעל את {#AppName} אוטומטית בכניסה ל-Windows
hebrew.AutoStartElevated=הפעלה אוטומטית מוגבהת: כל {#AppName} רצה בהרשאות מלאות מהכניסה (מאפשר הכתבה גם לחלונות אדמין)
hebrew.AutoStartTaskGroup=הפעלה אוטומטית:
hebrew.PostInstallInfo=Lia הותקנה בהצלחה.%n%nהאפליקציה עובדת מיד — ללא צורך במפתחות API. להכתבה בעברית: החזק Ctrl+Space. המודל העברי המקומי יורד פעם אחת בהכתבה הראשונה (צריך אינטרנט רק בפעם הזו).%n%nקליק ימני על סמל המיקרופון במגש לאפשרויות, פגישות, והוספת מפתחות ענן אם תרצה.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
; Default auto-start: a per-user Run registry value - no admin, no UAC, the
; app starts non-elevated (the normal mode for an asInvoker exe).
Name: "autostart";   Description: "{cm:AutoStartTask}";     GroupDescription: "{cm:AutoStartTaskGroup}"
; Opt-in ELEVATED auto-start (offered only when the installer itself runs
; elevated): the pre-audit behavior - a RunLevel-Highest scheduled task, so
; dictation also reaches Task Manager / admin consoles.
Name: "elevatedautostart"; Description: "{cm:AutoStartElevated}"; GroupDescription: "{cm:AutoStartTaskGroup}"; Flags: unchecked; Check: IsAdminInstallMode

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\runtime\{#AppExeName}"" ""{app}\app\lia.py"" --autostart"; \
    Flags: uninsdeletevalue; Tasks: autostart and not elevatedautostart

[Files]
; Full-runtime distribution: private CPython + app sources (replaces the
; frozen PyInstaller exe from v1.0.x). Everything works out of the box:
; Settings, all pywebview windows, subprocess mic recorder, file dialogs.
Source: "build_runtime\runtime\*";  DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "build_runtime\app\*";      DestDir: "{app}\app";     Flags: ignoreversion recursesubdirs createallsubdirs
Source: "lia.ico";                  DestDir: "{app}";         Flags: ignoreversion
Source: "..\README.md";             DestDir: "{app}";         Flags: ignoreversion isreadme
Source: "..\LICENSE";               DestDir: "{app}";         Flags: ignoreversion

[InstallDelete]
; Remove the legacy frozen exe from v1.0.x (same AppId = in-place upgrade).
Type: files; Name: "{app}\{#AppExeName}"

[Icons]
Name: "{group}\{#AppName}";                     Filename: "{app}\runtime\{#AppExeName}"; Parameters: """{app}\app\lia.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\lia.ico"; Comment: "{#AppName} - speech to text"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";                Filename: "{app}\runtime\{#AppExeName}"; Parameters: """{app}\app\lia.py"""; WorkingDir: "{app}\app"; IconFilename: "{app}\lia.ico"; Tasks: desktopicon; Comment: "{#AppName} - speech to text"

[Run]
; ELEVATED auto-start only (opt-in, elevated installs): a Scheduled Task with
; HIGHEST run-level + ONLOGON trigger, so login launches skip UAC and the app
; can dictate into elevated windows.
; Note: /tr requires triple-quoted exe path because of nested cmd parsing.
Filename: "{cmd}"; \
    Parameters: "/C schtasks /create /tn ""{#TaskName}"" /tr """"""{app}\runtime\{#AppExeName}"" ""{app}\app\lia.py"""""" /sc ONLOGON /rl HIGHEST /delay 0000:05 /f"; \
    Flags: runhidden; \
    StatusMsg: "Registering elevated auto-start task..."; \
    Tasks: elevatedautostart

; Launch after install.
Filename: "{app}\runtime\{#AppExeName}"; \
    Parameters: """{app}\app\lia.py"""; \
    WorkingDir: "{app}\app"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Always try to remove the scheduled task on uninstall (idempotent — schtasks
; returns nonzero if the task doesn't exist; runhidden + the trailing
; redirect swallow the error so the uninstaller stays clean).
Filename: "{cmd}"; \
    Parameters: "/C schtasks /delete /tn ""{#TaskName}"" /f >nul 2>&1"; \
    Flags: runhidden; \
    RunOnceId: "DelTask"

[UninstallDelete]
; %APPDATA%\Lia (user config, history, meeting transcripts) is NOT deleted
; by default — re-installs preserve user data. The uninstaller ASKS (see
; CurUninstallStepChanged below, default No); silent uninstalls always keep
; the data. Settings -> Advanced -> "Delete all my data" wipes it in-app.

[Code]
// Show a friendly post-install info page with hotkey reminders.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Nothing forced here — the [Run] entries handle launch, and the user
    // can always read README.md from the install dir. Hook reserved for
    // future post-install tasks (e.g. download_models).
  end;
end;

// Warn the user if Lia.exe is still running before installing —
// CloseApplications=yes in [Setup] will normally handle this, but the
// scheduled task can re-spawn it if uninstall ran in a weird state.
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

// Uninstall: OFFER to delete the user's data folder (recordings, transcripts,
// history, settings incl. API keys). Default = No (a reinstall keeps the
// data); silent uninstalls never delete it.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usUninstall then
  begin
    if not UninstallSilent then
    begin
      DataDir := ExpandConstant('{userappdata}\Lia');
      if DirExists(DataDir) then
      begin
        if MsgBox('Also delete your recordings, transcripts, history and settings?'
                  + #13#10 + DataDir + #13#10#13#10
                  + 'Choose No to keep them for a future reinstall.',
                  mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
          DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
