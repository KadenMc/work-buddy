; work-buddy Windows installer (Inno Setup 6).
;
; Per-user install (no admin): lays the source-tree payload into a user-chosen
; HOME, then runs bootstrap.ps1 (uv python + venv + editable install + provision
; + login auto-start). Mutable state lives under {localappdata}\work-buddy.
;
; Build:  ISCC.exe /DAppVersion=$(python packaging/version.py) packaging\windows\work-buddy.iss
;   expects dist\payload\  (from build_payload.py, with vendor\uv.exe from vendor_uv.py)
;   The version comes from pyproject.toml (single source of truth); the 0.0.0
;   default below is only a fallback for an ad-hoc build with no version passed.
;
; Compile-validated with ISCC 6.7.3; runtime behavior still needs the clean-VM
; install test before the first release. The AppId GUID below is a stable
; placeholder; keep it fixed once published (it keys upgrade detection).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; All relative Source/OutputDir paths resolve from the repo root, not this
; script's directory (Inno defaults relative paths to the .iss location).
SourceDir=..\..
AppId={{8F3E2A10-9C4B-4D7E-A1F2-6B5C8D9E0A1B}
AppName=work-buddy
AppVersion={#AppVersion}
AppPublisher=Kaden McKeen
AppSupportURL=https://github.com/KadenMc/work-buddy
; The user's home folder: auto-resolves to the current user, is never cloud-synced
; (Documents often is), and is visible as a Claude Code project dir. Matches the
; ~/work-buddy default the Linux/macOS installers already use.
DefaultDirName={%USERPROFILE}\work-buddy
; Always SHOW the location picker with the current default. DisableDirPage defaults
; to "auto", which silently skips the picker (and reuses the previous location) once
; the app has been installed before -- surprising, and it stranded a test install at
; a stale path. UsePreviousAppDir=no so it always offers the current default.
DisableDirPage=no
UsePreviousAppDir=no
DefaultGroupName=work-buddy
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; Inno 6.7+ enables Windows' RedirectionGuard mitigation on Setup by default, and
; it is inherited by child processes, which makes uv's junction/reparse operations
; fail with os error 448 ("untrusted mount point"). The mitigation exists to protect
; ELEVATED installers from junction-planting in shared paths; this installer is
; per-user and writes only user-owned paths, so there is no privilege boundary to
; defend. Turn it off. (Same fix Warp shipped: warpdotdev/warp#9863.)
RedirectionGuard=no
OutputDir=dist
OutputBaseFilename=work-buddy-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=work-buddy
; The bootstrap downloads Python + dependencies after extraction (torch alone is
; hundreds of MB); make the wizard's free-space check account for it (5 GB).
ExtraDiskSpaceRequired=5368709120

[Files]
; The source-tree payload (build_payload.py output), including vendor\uv.exe.
Source: "dist\payload\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
; The bootstrap script is not part of the source payload; ship it into vendor\.
Source: "packaging\windows\bootstrap.ps1"; DestDir: "{app}\vendor"; Flags: ignoreversion

[Dirs]
; The hidden per-user DATA dir (DBs, caches, logs, consent). provision writes
; paths.data_root here so mutable state never lands in the code tree.
Name: "{localappdata}\work-buddy"

[Run]
; The heavy step: uv sequence + provision + autostart. runhidden keeps the
; PowerShell console out of the user's face; the wizard shows StatusMsg.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\vendor\bootstrap.ps1"" -AppHome ""{app}"" -Data ""{localappdata}\work-buddy"" -Uv ""{app}\vendor\uv.exe"""; \
  StatusMsg: "Setting up Python and downloading dependencies (about 1 GB). This can take several minutes..."; \
  Flags: runhidden
; No postinstall "open dashboard" action: the finish page hands off to the real
; setup step (/wb-setup guided in Claude Code) via [Code] below, and a failed
; bootstrap must never leave a launch action pointing at a venv that was not built.

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

[UninstallDelete]
; Inno removes only what it installed, so the runtime-created venv and the
; provision-written config.yaml/.env (which holds the API key) would otherwise be
; left behind. Remove the whole install dir, plus the managed Python that lives
; under the data dir (a derived artifact). The rest of the user's DATA (databases,
; logs) under {localappdata}\work-buddy is preserved deliberately.
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{localappdata}\work-buddy\uv"

[Messages]
; The installer is NOT work-buddy's "wizard" — the real setup wizard is
; /wb-setup guided inside Claude Code. Strip "Setup Wizard" from the UI, and use
; the welcome page to explain the one-time download up front so the size reassures
; rather than alarms.
WelcomeLabel1=Welcome to work-buddy Setup
WelcomeLabel2=This will install work-buddy on your computer.%n%nwork-buddy runs a private semantic-search engine on your own machine, so Setup downloads its own Python and machine-learning libraries (about 1 GB, one time). The search models themselves download later, the first time you use search. Nothing you store is sent to a cloud service.%n%nClick Next to continue.
FinishedHeadingLabel=work-buddy is installed
ClickFinish=Click Finish to close Setup.

[Code]
{ Inno does not treat a nonzero [Run] exit as failure, so the bootstrap signals
  success by writing a marker file only when it fully completes. Reflect the TRUE
  outcome on the finish page (both heading and body) rather than always claiming
  the app is installed. }
function InstallSucceeded(): Boolean;
begin
  Result := FileExists(ExpandConstant('{localappdata}\work-buddy\.install-ok'));
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Log: String;
begin
  if CurPageID <> wpFinished then Exit;
  Log := ExpandConstant('{localappdata}\work-buddy\install.log');
  if InstallSucceeded() then
  begin
    WizardForm.FinishedHeadingLabel.Caption := 'work-buddy is installed';
    WizardForm.FinishedLabel.Caption :=
      'work-buddy is installed at ' + ExpandConstant('{app}') + '.' + #13#10 + #13#10 +
      'To finish setup, open that folder in Claude Code and run  /wb-setup guided  ' +
      '(feature selection and the interactive integrations).';
  end
  else
  begin
    WizardForm.FinishedHeadingLabel.Caption := 'Setup did not complete';
    WizardForm.FinishedLabel.Caption :=
      'work-buddy could not finish setting up, so it is NOT ready to use yet.' + #13#10 + #13#10 +
      'See the log at ' + Log + #13#10 + #13#10 +
      'Re-run the installer to try again. The downloads are cached, so it resumes.';
  end;
end;
