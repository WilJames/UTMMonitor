#define AppName "UTMMonitor"
#define AppVersion GetEnv("VERSION")

[Setup]
PrivilegesRequired=lowest

AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=James Dev
VersionInfoCopyright=Copyright (c) The James Dev

//AppMutex=Global\UTMMonitorByJamesDev
//RestartApplications=yes

DefaultDirName={userappdata}\UTMMonitor
DefaultGroupName=UTMMonitor
UninstallDisplayName=Удаление UTMMonitor
UninstallDisplayIcon={app}\UTMMonitor.exe

OutputBaseFilename=UTMMonitor
OutputDir=work\output

CloseApplications=force
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"


[UninstallRun]
Filename: {sys}\taskkill.exe; Parameters: "/f /im UTMMonitor.exe"; Flags: skipifdoesntexist runhidden; RunOnceId: "DelUTMMonitor"

; Удаляем правило из брандмауэра
;Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""UTMMonitor"""; Flags: runhidden; RunOnceId: "DelFirewallRule"
//Filename: "{cmd}"; Parameters: "/c taskkill /f /im UTMMonitor.exe"; Flags: runhidden waituntilterminated
//; RunOnceId: "DelUTMMonitor"

// удаление всей дирректории
//[UninstallDelete]
//Type: filesandordirs; Name: "{app}"


[Files]
Source: "work\output\UTMMonitor\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs


[Registry]
; Добавляем запись в автозапуск текущего пользователя
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "UTMMonitor"; ValueData: """{app}\UTMMonitor.exe"""; Flags: uninsdeletevalue

; Добавляем запись в автозапуск для всех пользователей
;Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "UTMMonitor"; ValueData: "{app}\UTMMonitor.exe"; Flags: uninsdeletevalue

[Run]
; Добавляем правило для исходящих соединений в брандмауэр
;Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""UTMMonitor"" dir=out action=allow program=""{app}\UTMMonitor.exe"" enable=yes"; Flags: runhidden
Filename: "{app}\UTMMonitor.exe"; Description: "Запустить программу"; Flags: postinstall nowait skipifsilent
