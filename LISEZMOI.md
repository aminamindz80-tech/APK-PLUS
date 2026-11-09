# ERP Atelier — version vierge

Gestion d'atelier : articles, nomenclatures, BC, BL, retours, ordres de production, stock, analytique temps réel, exports Excel/PDF, envoi par e-mail, sauvegardes. Aucune dépendance externe (Python 3 seulement).

## Démarrage
- Windows : double-clic sur `lancer.bat` (ou `python server.py`). Linux/Mac : `./lancer.sh`.
- Ouvrir http://localhost:8000
- Connexion initiale : **admin / admin123** → changez le mot de passe tout de suite (lien « Mot de passe », en bas du menu).
- La base vide est créée automatiquement dans `data/` au premier lancement.

## Première configuration (ordre conseillé)
1. **Données de l'entreprise** : raison sociale, adresse, logo, cachet, signature (apparaissent sur les BC/BL et les PDF).
2. **Utilisateurs** : créer les comptes.
3. **Clients / Fournisseurs**, puis **Articles** (référence, désignation, unité, stock initial, stock mini) et **Nomenclatures**.
4. Saisir les **BC**, puis les **BL** : les quantités livrées/restantes, le stock et les statuts se calculent automatiquement.
5. **Envoi par e-mail** (facultatif) : renseigner votre propre serveur SMTP.
6. **Sauvegarde / Restauration** : sauvegardes automatiques dans `data/sauvegardes/`.

## Recherche produit
Trois affichages : Produits (BC/BL/retours), **Livré / Reste global** (une ligne par produit) et **Livré / Reste détail par BC**. Filtres Livrés / Restants / Entièrement livrés, BC clients / fournisseurs. Export Excel et PDF de la vue affichée.

## Logiciel Windows (.exe)
`construire_exe.bat` (voir `LOGICIEL.md`). Les données sont alors dans `%APPDATA%\ERP_ATELIER\data`.

## Réseau local / téléphone
`acces_reseau.bat` puis `autoriser_pare_feu.bat` ; ouvrir `http://IP-DU-PC:8000` depuis le téléphone.

## Application Android (dossier `android/`)
Application **autonome, hors ligne** : l'ERP complet tourne dans le téléphone (aucun PC ni réseau), avec un menu **📷 Scanner une photo** (BL, BC, articles) qui lit le texte de la photo puis l'enregistre dans la base du téléphone après vérification.
- **Compiler l'APK sans Android Studio** : déposez tout le contenu de ce dossier dans un dépôt GitHub, onglet *Actions* → « Compiler l'APK Android » → *Run workflow*. L'APK est dans les *Artifacts* (ERP_Atelier_apk).
- Ou avec Android Studio : voir `android/LISEZMOI.md`.

## Apparence
Thème « Atelier » (graphite, sarcelle, fond pierre), police Inter intégrée au fichier : fonctionne entièrement hors ligne. Les couleurs principales sont les variables `:root` du bloc `<style id="theme2">` en fin de `static/index.html`.
