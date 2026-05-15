; ============================================================
; Photo Reviewer — Inno Setup Installer Script
; ============================================================
; Prerequisites:
;   1. Build the app first: run build_app.bat
;   2. Install Inno Setup from https://jrsoftware.org/isinfo.php
;   3. Open this file in Inno Setup and click Build > Compile
;   4. Find the installer at: installer\PhotoReviewerSetup.exe
; ============================================================

#define AppName      "Photo Reviewer"
#define AppVersion   "1.0"
#define AppPublisher "Photo Reviewer"
#define AppExeName   "PhotoReviewer.exe"
#define BuildDir     "dist\PhotoReviewer"

[Setup]
AppId={{8A3F2C1D-4E5B-4F6A-9C2D-1B3E7F8A9D0E}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=PhotoReviewerSetup
SetupIconFile=photo_reviewer_icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startupicon";  Description: "Launch Photo Reviewer at &startup"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "photo_reviewer_countries.geojson"; DestDir: "{app}"; Flags: ignoreversion
Source: "photo_reviewer_icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";            Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}";     Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\photo_reviewer_status.json"
Type: files; Name: "{app}\photo_reviewer_hashes.json"
Type: files; Name: "{app}\photo_reviewer_scores.json"
Type: files; Name: "{app}\photo_reviewer_recent.json"
Type: files; Name: "{app}\photo_reviewer_settings.json"
