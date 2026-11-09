# ERP Atelier en logiciel Windows

## Générer le logiciel (une seule fois, sur un PC Windows avec Python 3.8+)
1. Double-clic sur `construire_exe.bat` (Internet requis pour télécharger PyInstaller et pywebview).
2. Résultat : `dist\ERP_ATELIER\ERP_ATELIER.exe` — double-clic pour lancer, aucune installation de Python nécessaire sur les autres PC (copier tout le dossier `ERP_ATELIER`).
3. (Optionnel) Installateur `Installer_ERP_ATELIER.exe` : installer Inno Setup, ouvrir `installateur.iss`, cliquer sur Compile.

## Fonctionnement
- Fenêtre native (WebView2, présent sur Windows 10/11). Sans pywebview, le navigateur s'ouvre et une petite fenêtre permet de quitter.
- Le serveur n'écoute que sur ce PC (127.0.0.1). Pour un usage multi-postes, gardez la version serveur (`lancer.bat`).
- Données : `%APPDATA%\ERP_ATELIER\data` (base + photos/PDF). Sauvegarde = copier ce dossier. Elles survivent aux mises à jour.
- Connexion initiale : admin / admin123 (à changer).
- Options : `ERP_ATELIER.exe --demo` charge des données de démonstration.
- Si la fenêtre native pose problème (saisie impossible, écran blanc) : `ouvrir_dans_navigateur.bat` ouvre le logiciel dans votre navigateur habituel.
- Mot de passe perdu ou connexion impossible : double-cliquez sur `reinitialiser_mdp.bat` (à placer à côté de ERP_ATELIER.exe) → admin / admin123. Le bouton « Voir » de l’écran de connexion permet de vérifier ce que vous tapez.

## Application Android
Le dossier `android/` (à la racine de l'archive) contient le projet de l'application mobile — voir `android/LISEZMOI.md`.
Côté PC : `acces_reseau.bat` ouvre le logiciel en mode réseau (port 8000) et affiche l'adresse à saisir sur le téléphone ; `autoriser_pare_feu.bat` (administrateur) ouvre le port sur les réseaux privés.
