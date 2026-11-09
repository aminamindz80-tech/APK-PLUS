# Analytique temps réel — ERP Atelier

## Installation
Remplacer `plat/server.py` et `plat/static/index.html` par les fichiers de ce dossier, puis relancer l'ERP (`lancer.bat`).
La base de données n'est pas modifiée (lecture seule). Nouveau menu : **Analytique temps réel**.
Pour la version .exe : relancer `construire_exe.bat` (comme pour toute mise à jour).

## Contenu
- 8 indicateurs : taux de livraison, reste à livrer, BC ouverts, livré et produit sur la période, réalisation des OF, taux de retour, alertes (+ CA livré dès que des prix sont saisis).
- Graphiques : flux quotidiens (livré / produit / retours), statuts BC et OF, avancement de chaque BC, top articles livrés, stock par article, retours par motif.
- Alertes automatiques : stock négatif, articles qui manquent pour les BC, BC en retard, sous stock mini.
- Activité récente : derniers mouvements de stock.

## Temps réel
La page se rafraîchit seule (5 s, 10 s, 30 s, 1 min ou pause) ; les indicateurs qui changent clignotent avec l'écart (▲/▼).
Voyant vert = connecté, rouge = connexion perdue (reprise automatique). Mode « Plein écran » pour un écran d'atelier.
Aucune dépendance Internet : fonctionne hors ligne sur le réseau local.

## Remarques sur vos données
- Tous les prix sont à 0 : l'analyse porte sur les quantités. Les montants apparaîtront dès la saisie des prix.
- Aucun BC n'a de date de livraison : le suivi des retards s'activera quand elle sera renseignée.
- Route API ajoutée : `GET /api/analytics?jours=7|30|90|0` (session requise).

## Tableau analytique des bons de commande (ajout)
Page **Analytique temps réel** → carte « Tableau analytique des bons de commande » (mise à jour en direct) :
- Filtres : Tous / Ouverts / Soldés / En retard ; total recalculé selon le filtre.
- Colonnes : N° BC, client · unité, statut, échéance, lignes, commandé, livré, reste, avancement %, suivi.
- **Clic sur une ligne** : fiche analytique du BC (article par article : commandé, livré, reste, avancement, stock, dernière livraison, état).
- **Les exports suivent le filtre actif** : avec « Ouverts », « Soldés » ou « En retard » sélectionné, la synthèse et le détail ne contiennent que ces BC. Le bouton indique « Exporter les N BC affichés (filtre) », le titre et l'en-tête du fichier mentionnent le filtre, et le nom du fichier aussi (ex. `analyse_bc_ouverts_2026-10-20.xlsx`). Boutons désactivés si aucun BC.
- Exports Excel et PDF :
  - **Synthèse** de tous les BC (avec totaux et taux de livraison global) ;
  - **Détail de tous les BC** (un tableau par BC, article par article, avec total) ;
  - **Un seul BC** (boutons PDF / Excel sur chaque ligne et dans la fiche).
- API : `GET /api/export/analyse_bc.(xlsx|pdf)?filtre=tous|ouverts|soldes|retard`, `analyse_bc_detail.(xlsx|pdf)?filtre=…`, `analyse_bc-<id>.(xlsx|pdf)` ; `GET /api/analyse_bc/<id>`.
- Le dossier `exemples/` contient les états générés à partir de votre base du 20/10/2026.

## Envoi des états BC / BL par e-mail à chaque déconnexion (ajout)
Menu **Envoi par e-mail** (administrateur) :
1. Cocher « Envoyer automatiquement… à chaque déconnexion ».
2. Saisir le(s) destinataire(s), puis le serveur SMTP (préréglages Gmail / Outlook / Yahoo), l'identifiant et le mot de passe.
   Gmail et Outlook exigent un **mot de passe d'application** (validation en 2 étapes), pas le mot de passe habituel.
3. Choisir les états (BC, BC détaillé, BL, BL détaillé, analyse BC) et les formats (Excel / PDF).
4. « Enregistrer et envoyer un test » pour vérifier. L'historique des envois s'affiche en bas de page.
- L'envoi se fait en arrière-plan : la déconnexion reste instantanée. Seul le clic sur « Déconnexion » déclenche l'envoi (pas la fermeture de la fenêtre ni l'expiration de session).
- Option « seulement si des BC/BL ont changé depuis le dernier envoi » pour éviter les doublons.
- Les réglages sont dans `data/email.json` (mot de passe inclus, jamais réaffiché ni inclus dans les sauvegardes ZIP) ; l'historique dans `data/email_log.json`. Version installée Windows : `%APPDATA%\ERP_ATELIER\data`.
- Le PC doit avoir accès à Internet au moment de la déconnexion (ou à votre serveur de messagerie interne).

### Dépannage de l'envoi vers Gmail (mise à jour)
- Bouton **Diagnostiquer la connexion** (menu *Envoi par e-mail*) : vérifie Internet/DNS, les ports 465 et 587, le chiffrement, puis l'identifiant et le mot de passe, et indique l'étape exacte qui échoue.
- Gmail : serveur `smtp.gmail.com`, identifiant = adresse Gmail complète, mot de passe = **mot de passe d'application** de 16 lettres (validation en 2 étapes obligatoire). Les espaces collés par erreur sont retirés automatiquement.
- Si le port choisi est bloqué, l'ERP essaie automatiquement l'autre mode Gmail (465 SSL ↔ 587 STARTTLS) et mémorise celui qui marche. La connexion essaie l'IPv4 en premier.
- Les messages d'erreur reprennent la réponse exacte du serveur et l'étape (connexion / identification / envoi). Les états qui n'ont pas pu être générés (ex. « Aucun BC pour ce filtre ») sont signalés dans l'historique au lieu d'être ignorés en silence.
- Option « Vérifier le certificat du serveur » : à décocher uniquement si un antivirus intercepte les connexions chiffrées (erreur CERTIFICATE_VERIFY_FAILED).

### Envoi à la fermeture de la fenêtre (sans déconnexion)
- Option « Envoyer aussi quand la fenêtre est fermée sans déconnexion » (cochée par défaut, menu *Envoi par e-mail*).
- Fermer l'onglet/la fenêtre envoie un signal au serveur ; si l'utilisateur ne revient pas dans les 20 secondes, les états sont envoyés. Une simple actualisation (F5) n'envoie rien.
- Filet de sécurité : plantage, veille ou PC éteint -> envoi après 30 minutes sans aucune activité.
- Un seul envoi par session : pas de doublon si l'utilisateur clique ensuite sur « Déconnexion » après une fermeture déjà traitée n'est pas garanti ; l'option « seulement si changé » évite les doublons.
