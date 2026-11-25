[Setup]
AppId={{39E547C5-189F-47E5-AC56-0881E132A87D}
AppName=Aruba Wireless Quality Monitor
AppVersion={#ProductVersion}
AppPublisher=Aruba Wireless Quality Monitor contributors
AppPublisherURL=https://github.com/sebia1993/Aruba-Wireless-Quality-Monitor
DefaultDirName={code:ProgramDirectory}
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
UsePreviousAppDir=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.22000
OutputBaseFilename=AWQM-Setup
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no
Uninstallable=not StageUpgrade
UninstallFilesDir={autopf}\AWQM Setup\Uninstall
UninstallDisplayIcon={app}\AWQM.exe
WizardStyle=modern
VersionInfoVersion={#ProductVersion}.0

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Administrators use the existing authenticated offline upgrade and rollback procedure.
Source: "{#ServiceArchive}"; DestDir: "{commonpf}\AWQM Setup\Packages\{#ProductVersion}"; Check: IsAdminInstallMode; Flags: ignoreversion
Source: "{#ServiceManifest}"; DestDir: "{commonpf}\AWQM Setup\Packages\{#ProductVersion}"; Check: IsAdminInstallMode; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Aruba Wireless Quality Monitor"; Filename: "{app}\AWQM.exe"; WorkingDir: "{app}"; Check: not StageUpgrade

[Run]
Filename: "{app}\AWQM.exe"; WorkingDir: "{app}"; Flags: nowait skipifsilent runasoriginaluser; Check: CanOpenDashboard

[Code]
var
  PreparingUpgrade: Boolean;

function StageUpgrade: Boolean;
begin
  Result := PreparingUpgrade;
end;

function CanOpenDashboard: Boolean;
begin
  Result := not StageUpgrade and not IsAdmin;
end;

function InitializeSetup: Boolean;
begin
  PreparingUpgrade := IsAdminInstallMode and
    (RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\AWQM') or
     FileExists(ExpandConstant('{commonpf}\AWQM\AWQM.exe')));
  Result := True;
end;

function ProgramDirectory(Param: String): String;
begin
  if StageUpgrade then
    Result := ExpandConstant('{commonpf}\AWQM Setup\Candidates\{#ProductVersion}')
  else
    Result := ExpandConstant('{autopf}\AWQM');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsAdminInstallMode and (CompareText(ExpandConstant('{app}'), ProgramDirectory('')) <> 0) then
    Result := 'Service installation requires the designated Program Files directory.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssDone) and StageUpgrade and not WizardSilent then
    MsgBox('기존 설치를 확인하여 새 버전을 별도 폴더에 준비했습니다.' + #13#10 +
      ExpandConstant('{app}') + #13#10 +
      '서비스 관리자의 오프라인 업데이트 절차로 전환할 수 있습니다.',
      mbInformation, MB_OK);
end;

function InitializeUninstall: Boolean;
begin
  Result := not ((CompareText(ExpandConstant('{app}'), ExpandConstant('{commonpf}\AWQM')) = 0) and RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\AWQM'));
  if not Result and not UninstallSilent then
    MsgBox('서비스를 등록 해제한 후 프로그램을 제거하세요.', mbInformation, MB_OK);
end;
