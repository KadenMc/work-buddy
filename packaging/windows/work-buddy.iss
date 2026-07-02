; work-buddy Windows installer (Inno Setup 6).
;
; Per-user install (no admin): lays the source-tree payload into a user-chosen
; HOME, then runs bootstrap.ps1 (uv python + venv + editable install + provision
; + login auto-start). Mutable state lives under {localappdata}\work-buddy.
;
; Build:  ISCC.exe /DAppVersion=X.Y.Z packaging\windows\work-buddy.iss
;   expects dist\payload\  (from build_payload.py, with vendor\uv.exe from vendor_uv.py)
;
; NOTE: not yet compile-validated (no Inno Setup on the dev box). Compile with
; ISCC on the clean Windows VM before the first real run. The AppId GUID below is
; a stable placeholder; keep it fixed once published (it keys upgrade detection).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8F3E2A10-9C4B-4D7E-A1F2-6B5C8D9E0A1B}
AppName=work-buddy
AppVersion={#AppVersion}
AppPublisher=Kaden McKeen
AppSupportURL=https://github.com/KadenMc/work-buddy
DefaultDirName={userdocs}\work-buddy
DefaultGroupName=work-buddy
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=work-buddy-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=work-buddy
; The bootstrap downloads a few hundred MB of dependencies; warn about time/space.
DiskSpaceWarning=yes

[Files]
; The source-tree payload (build_payload.py output), including vendor\uv.exe.
Source: "dist\payload\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
; The bootstrap script is not part of the source payload; ship it into vendor\.
Source: "bootstrap.ps1"; DestDir: "{app}\vendor"; Flags: ignoreversion

[Dirs]
; The hidden per-user DATA dir (DBs, caches, logs, consent). provision writes
; paths.data_root here so mutable state never lands in the code tree.
Name: "{localappdata}\work-buddy"

[Run]
; The heavy step: uv sequence + provision + autostart. runhidden keeps the
; PowerShell console out of the user's face; the wizard shows StatusMsg.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\vendor\bootstrap.ps1"" -AppHome ""{app}"" -Data ""{localappdata}\work-buddy"" -Uv ""{app}\vendor\uv.exe"""; \
  StatusMsg: "Setting up Python and downloading dependencies. This can take several minutes..."; \
  Flags: runhidden
; Offer to open the dashboard once install finishes.
Filename: "{app}\.venv\Scripts\wbuddy.exe"; Parameters: "dashboard --open"; \
  Description: "Open the work-buddy dashboard"; \
  Flags: postinstall nowait skipifsilent

[Icons]
Name: "{group}\work-buddy dashboard"; Filename: "{app}\.venv\Scripts\wbuddy.exe"; Parameters: "dashboard --open"
Name: "{group}\Uninstall work-buddy"; Filename: "{uninstallexe}"

[UninstallRun]
; Stop the sidecar and remove the login auto-start task. DATA is left in place
; (uninstalling the app should not silently delete the user's databases).
Filename: "{app}\.venv\Scripts\wbuddy.exe"; Parameters: "stop"; \
  Flags: runhidden; RunOnceId: "WbStop"
Filename: "{app}\.venv\Scripts\wbuddy.exe"; Parameters: "autostart disable"; \
  Flags: runhidden; RunOnceId: "WbAutostart"
