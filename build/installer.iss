#define MyAppName "PhoneNAS"
#define MyAppVersion "1.2.4"
#define MyAppExeName "PhoneNAS.exe"

[Setup]
AppId={{8F3C2A1E-6B7D-4E9A-9C21-5D4B7A0F1E33}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\PhoneNAS
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=PhoneNAS-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\app\resources\app.ico
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "autostart"; Description: "开机自动启动 PhoneNAS"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "..\dist\PhoneNAS\*"; DestDir: "{app}"; Excludes: "portable.flag,data\*,baselines\*,logs\*"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\PhoneNAS"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\PhoneNAS"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "PhoneNAS"; ValueData: """{app}\{#MyAppExeName}"" --tray"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 PhoneNAS"; Flags: nowait postinstall skipifsilent
