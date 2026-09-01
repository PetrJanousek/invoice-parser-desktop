; Inno Setup script for the Windows installer.
;
; Wraps the PyInstaller onedir build (dist\Invoice Parser\) into a single
; Setup.exe: install to Program Files, Start Menu shortcut, optional Desktop
; shortcut, standard uninstaller. Built in CI (.github/workflows/desktop-build.yml)
; against a fresh `pyinstaller desktop_app.spec` output — run that first if
; building locally on Windows.

#define MyAppName "Invoice Parser"
#define MyAppExeName "Invoice Parser.exe"
#define MyAppPublisher "Invoice Parser"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

[Setup]
AppId={{B7E6C5C4-6B0B-4C1E-9C3E-1D9F1D6E7B3A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=InvoiceParserSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible
; Auto-update support (app/updater.py runs this installer with /VERYSILENT
; /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS): these two
; tell Setup to find and close the running app (it detects it via open
; handles on the exe/dlls being overwritten — no AppMutex needed) and
; relaunch it once the update is installed.
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\Invoice Parser\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
