; CCTV Viewer 설치 프로그램 (Inno Setup 6)
; 직접 실행하지 말고 build_exe.py 로 빌드할 것: 버전(/DAppVersion)과 경로를 넘겨준다.
;   ISCC /DAppVersion=1.0.1 /DSourceDir=..\dist\CCTVViewer /DOutputDir=..\release CCTVViewer.iss

#ifndef AppVersion
  #error "AppVersion 정의 필요 (build_exe.py 로 빌드)"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\CCTVViewer"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif

#define AppName "CCTV Viewer"
#define AppExe  "CCTVViewer.exe"

[Setup]
; AppId 는 절대 바꾸지 말 것: 같아야 새 버전 설치 시 기존 설치를 덮어쓰는 업그레이드로 동작
AppId={{FC81CAEA-6EDA-48E2-9A4E-3F2D3B9459D1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher=CCTV Viewer
VersionInfoVersion={#AppVersion}.0
DefaultDirName={autopf}\CCTVViewer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 기본은 관리자 설치(Program Files). 설치 시 '현재 사용자만' 선택 가능
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=CCTVViewer_v{#AppVersion}_Setup
SetupIconFile=..\assets\cctv.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} v{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 업그레이드 시 실행 중인 뷰어를 종료하고 설치 후 재시작
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Windows 시작 시 자동 실행"; GroupDescription: "기타:"; Flags: unchecked

[InstallDelete]
; 이전 버전의 라이브러리 잔재 제거 (PyInstaller 는 버전마다 파일 구성이 바뀔 수 있음)
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\config.example.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName} 설정 폴더"; Filename: "{userappdata}\CCTVViewer"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: autostart

[Dirs]
Name: "{userappdata}\CCTVViewer"

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; 자동 업데이트로 설치된 경우에만(/RELAUNCH) 새 버전을 바로 실행.
; 관리자의 무인 일괄 설치(/VERYSILENT)에서는 실행하지 않는다.
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: ShouldRelaunch

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := WizardSilent and (Pos('/RELAUNCH', Uppercase(GetCmdTail)) > 0);
end;

// 제거 시 카메라 설정(config.json, 비밀번호 포함) 삭제 여부를 묻는다. 무인 제거 시에는 보존.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('카메라 설정(%APPDATA%\CCTVViewer)도 삭제할까요?' + #13#10 +
              '재설치 후 계속 쓰려면 [아니요]를 누르세요.', mbConfirmation, MB_YESNO) = IDYES then
      DelTree(ExpandConstant('{userappdata}\CCTVViewer'), True, True, True);
end;
