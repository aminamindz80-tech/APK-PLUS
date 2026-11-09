; Inno Setup (https://jrsoftware.org/isinfo.php) - à compiler APRÈS construire_exe.bat
[Setup]
AppName=ERP Atelier
AppVersion=1.0
AppPublisher=Mon Entreprise
DefaultDirName={autopf}\ERP Atelier
DefaultGroupName=ERP Atelier
OutputDir=installateur
OutputBaseFilename=Installer_ERP_ATELIER
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
UninstallDisplayName=ERP Atelier

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; Flags: unchecked

[Files]
Source: "dist\ERP_ATELIER\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "reinitialiser_mdp.bat"; DestDir: "{app}"
Source: "ouvrir_dans_navigateur.bat"; DestDir: "{app}"
Source: "acces_reseau.bat"; DestDir: "{app}"
Source: "autoriser_pare_feu.bat"; DestDir: "{app}"

[Icons]
Name: "{group}\ERP Atelier (accès réseau / Android)"; Filename: "{app}\acces_reseau.bat"
Name: "{group}\Réinitialiser le mot de passe admin"; Filename: "{app}\reinitialiser_mdp.bat"
Name: "{group}\ERP Atelier"; Filename: "{app}\ERP_ATELIER.exe"
Name: "{autodesktop}\ERP Atelier"; Filename: "{app}\ERP_ATELIER.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ERP_ATELIER.exe"; Description: "Lancer ERP Atelier"; Flags: nowait postinstall skipifsilent
; Les données (%APPDATA%\ERP_ATELIER\data) ne sont PAS supprimées à la désinstallation.
