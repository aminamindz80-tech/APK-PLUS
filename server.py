#!/usr/bin/env python3
"""ERP Atelier - plateforme de gestion d'atelier (serveur + base SQLite). Aucune dépendance externe."""
import re, sqlite3, json, os, sys, time, hashlib, secrets, datetime, threading, traceback, argparse, contextlib, zipfile, shutil, tempfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('ATELIER_DB', os.path.join(BASE, 'data', 'atelier.db'))
SOCIETE = os.environ.get('ATELIER_SOCIETE', 'Mon Entreprise')
SESS, LOCK = {}, threading.Lock()
FILETOKENS = {}  # jetons temporaires pour les fichiers
# Photos et fiches PDF des articles : stockées dans <dossier de la base>/fichiers/
FILES = os.path.join(os.path.dirname(DB_PATH), 'fichiers')
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), 'sauvegardes')
BACKUP_KEEP = 30
BACKUP_INTERVAL = 6 * 60 * 60
KINDS = {'photo': ('photo', 8 << 20), 'pdf': ('fiche_pdf', 25 << 20)}   # type -> (colonne, taille max en octets)
CT = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp', '.pdf': 'application/pdf'}
SOC_KINDS = {'logo': ('logo', 5 << 20), 'cachet': ('cachet', 5 << 20), 'signature': ('signature', 5 << 20)}
PROTECT = {'articles': ('photo', 'fiche_pdf')}   # colonnes non modifiables via l'API JSON classique
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL, nom TEXT, role TEXT DEFAULT 'user', pwd TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS societe(id INTEGER PRIMARY KEY CHECK(id=1), code TEXT, raison_sociale TEXT NOT NULL, adresse TEXT, ville TEXT, tel TEXT, email TEXT, nif TEXT, nis TEXT, rc TEXT, logo TEXT, cachet TEXT, signature TEXT);
CREATE TABLE IF NOT EXISTS tiers(id INTEGER PRIMARY KEY, code TEXT, raison_sociale TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'Client', adresse TEXT, ville TEXT, tel TEXT, email TEXT, nif TEXT, nis TEXT, rc TEXT);
CREATE TABLE IF NOT EXISTS articles(id INTEGER PRIMARY KEY, reference TEXT UNIQUE NOT NULL, designation TEXT NOT NULL, type TEXT DEFAULT 'Matiere premiere', unite TEXT DEFAULT 'u', prix_achat REAL DEFAULT 0, prix_vente REAL DEFAULT 0, stock REAL DEFAULT 0, stock_mini REAL DEFAULT 0, photo TEXT, fiche_pdf TEXT);
CREATE TABLE IF NOT EXISTS bc(id INTEGER PRIMARY KEY, num TEXT UNIQUE, date TEXT, reference TEXT, type TEXT DEFAULT 'Client', tiers_id INTEGER REFERENCES tiers(id), date_livraison TEXT, statut TEXT DEFAULT 'Brouillon', tva REAL DEFAULT 19, remarque TEXT);
CREATE TABLE IF NOT EXISTS bc_lignes(id INTEGER PRIMARY KEY, bc_id INTEGER NOT NULL REFERENCES bc(id) ON DELETE CASCADE, article_id INTEGER REFERENCES articles(id), designation TEXT, quantite REAL DEFAULT 0, unite TEXT DEFAULT 'u', prix_unitaire REAL DEFAULT 0, qte_livree REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bl(id INTEGER PRIMARY KEY, num TEXT UNIQUE, date TEXT, reference TEXT, type TEXT DEFAULT 'Livraison', tiers_id INTEGER REFERENCES tiers(id), bc_id INTEGER REFERENCES bc(id), statut TEXT DEFAULT 'Brouillon', chauffeur TEXT, vehicule TEXT, remarque TEXT);
CREATE TABLE IF NOT EXISTS bl_lignes(id INTEGER PRIMARY KEY, bl_id INTEGER NOT NULL REFERENCES bl(id) ON DELETE CASCADE, article_id INTEGER REFERENCES articles(id), designation TEXT, quantite REAL DEFAULT 0, unite TEXT DEFAULT 'u', prix_unitaire REAL DEFAULT 0, bc_id INTEGER REFERENCES bc(id));
CREATE TABLE IF NOT EXISTS ordres(id INTEGER PRIMARY KEY, num TEXT UNIQUE, date TEXT, article_id INTEGER REFERENCES articles(id), qte_a_fabriquer REAL DEFAULT 0, qte_fabriquee REAL DEFAULT 0, bc_id INTEGER REFERENCES bc(id), date_debut TEXT, date_fin TEXT, priorite TEXT DEFAULT 'Normale', statut TEXT DEFAULT 'Planifie', responsable TEXT, remarque TEXT);
CREATE TABLE IF NOT EXISTS ordre_operations(id INTEGER PRIMARY KEY, ordre_id INTEGER NOT NULL REFERENCES ordres(id) ON DELETE CASCADE, ordre INTEGER, operation TEXT, poste TEXT, temps_prevu REAL DEFAULT 0, temps_reel REAL DEFAULT 0, statut TEXT DEFAULT 'A faire');
CREATE TABLE IF NOT EXISTS nomenclature(id INTEGER PRIMARY KEY, article_id INTEGER NOT NULL REFERENCES articles(id), composant_id INTEGER NOT NULL REFERENCES articles(id), quantite REAL DEFAULT 1);
CREATE TABLE IF NOT EXISTS bc_surplus(id INTEGER PRIMARY KEY, bc_id INTEGER NOT NULL REFERENCES bc(id) ON DELETE CASCADE, article_id INTEGER, qte REAL DEFAULT 0, bl_num TEXT);
CREATE TABLE IF NOT EXISTS bc_livre_initial(bc_id INTEGER NOT NULL REFERENCES bc(id) ON DELETE CASCADE, article_id INTEGER NOT NULL, qte REAL DEFAULT 0, PRIMARY KEY(bc_id, article_id));
CREATE TABLE IF NOT EXISTS article_equiv(id INTEGER PRIMARY KEY, a_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE, b_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE, UNIQUE(a_id, b_id));
CREATE TABLE IF NOT EXISTS mouvements(id INTEGER PRIMARY KEY, date TEXT, article_id INTEGER REFERENCES articles(id), sens TEXT, quantite REAL, origine TEXT);
CREATE TABLE IF NOT EXISTS bl_report(id INTEGER PRIMARY KEY, bl_num TEXT, article_id INTEGER, src_bc_id INTEGER, bc_id INTEGER, qte REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS retours(id INTEGER PRIMARY KEY, num TEXT UNIQUE, date TEXT, reference TEXT, type TEXT DEFAULT 'Retour client', tiers_id INTEGER REFERENCES tiers(id), bl_id INTEGER REFERENCES bl(id), motif TEXT, statut TEXT DEFAULT 'Brouillon', remarque TEXT);
CREATE TABLE IF NOT EXISTS retour_lignes(id INTEGER PRIMARY KEY, retour_id INTEGER NOT NULL REFERENCES retours(id) ON DELETE CASCADE, article_id INTEGER REFERENCES articles(id), designation TEXT, quantite REAL DEFAULT 0, unite TEXT DEFAULT 'u', destination TEXT DEFAULT 'Stock');
"""
TABLES = {'tiers', 'articles', 'bc', 'bl', 'retours', 'ordres', 'nomenclature', 'mouvements', 'users', 'societe'}
DOCS = {'bc': ('bc_lignes', 'bc_id', 'BC'), 'bl': ('bl_lignes', 'bl_id', 'BL'), 'ordres': ('ordre_operations', 'ordre_id', 'OF'), 'retours': ('retour_lignes', 'retour_id', 'RC')}
LOCKED = {'bl': ('Valide', 'Annule'), 'retours': ('Valide', 'Annule'), 'ordres': ('Termine', 'Annule'), 'bc': ('Annule', 'Solde')}
NODEL = {'bl': ('Valide',), 'retours': ('Valide',), 'bc': ('Valide', 'Partiel', 'Solde'), 'ordres': ('En cours', 'Termine')}
ALERT_SQL = "SELECT id, reference, designation, stock, stock_mini, unite FROM articles WHERE COALESCE(stock_mini,0)>0 AND COALESCE(stock,0)<=stock_mini ORDER BY reference"
SITUATION_SQL = """SELECT z.*, MAX(0, z.besoin - z.stock) AS manque,
 CASE WHEN MAX(0, z.besoin - z.stock) > 0 THEN 'Manque' WHEN z.stock_mini > 0 AND z.stock <= z.stock_mini THEN 'Alerte mini' ELSE 'OK' END AS etat
 FROM (SELECT y.*, y.a_livrer + y.pour_of AS besoin FROM (
  SELECT a.id, a.reference, a.designation, a.type, a.unite, COALESCE(a.stock,0) AS stock, COALESCE(a.stock_mini,0) AS stock_mini,
   COALESCE((SELECT SUM(qte_fabriquee) FROM ordres o WHERE o.article_id=a.id AND o.statut='Termine'),0) AS fabrique,
   COALESCE((SELECT SUM(MAX(0, qte_a_fabriquer-COALESCE(qte_fabriquee,0))) FROM ordres o WHERE o.article_id=a.id AND o.statut IN ('Planifie','En cours','Suspendu')),0) AS en_fabrication,
   COALESCE((SELECT SUM(MAX(0, l.quantite-COALESCE(l.qte_livree,0))) FROM bc_lignes l JOIN bc ON bc.id=l.bc_id WHERE l.article_id=a.id AND bc.type='Client' AND bc.statut IN ('Valide','Partiel')),0) AS a_livrer,
   COALESCE((SELECT SUM(n.quantite*MAX(0, o.qte_a_fabriquer-COALESCE(o.qte_fabriquee,0))) FROM nomenclature n JOIN ordres o ON o.article_id=n.article_id WHERE n.composant_id=a.id AND o.statut IN ('Planifie','En cours','Suspendu')),0) AS pour_of,
   COALESCE((SELECT SUM(MAX(0, l.quantite-COALESCE(l.qte_livree,0))) FROM bc_lignes l JOIN bc ON bc.id=l.bc_id WHERE l.article_id=a.id AND bc.type='Fournisseur' AND bc.statut IN ('Valide','Partiel')),0) AS a_recevoir
  FROM articles a) y) z ORDER BY (MAX(0, z.besoin - z.stock) > 0) DESC, z.reference"""
# Lignes de BL enrichies : commande = qté du BC pour l'article ; reste_avant = reste à livrer du BC juste avant ce BL ; supp = qté livrée au-delà de ce reste
BL_LIGNES_SQL = """SELECT x.*, CASE WHEN x.s_reste IS NOT NULL THEN x.s_reste ELSE MAX(0, x.commande - MAX(0, x.livre_avant)) END AS reste_avant,
 CASE WHEN x.s_supp IS NOT NULL THEN x.s_supp WHEN x.source_bc_id IS NULL OR x.article_id IS NULL THEN 0 ELSE MAX(0, COALESCE(x.quantite,0) - MAX(0, x.commande - MAX(0, x.livre_avant))) END AS supp FROM (
 SELECT l.*, a.reference AS article_reference, COALESCE(l.bc_id,b.bc_id) AS source_bc_id,
  COALESCE(l.s_commande, (SELECT SUM(c.quantite) FROM bc_lignes c WHERE c.bc_id=COALESCE(l.bc_id,b.bc_id) AND (c.article_id=l.article_id OR (NOT EXISTS(SELECT 1 FROM bc_lignes c0 WHERE c0.bc_id=COALESCE(l.bc_id,b.bc_id) AND c0.article_id=l.article_id) AND c.article_id IN (SELECT e.b_id FROM article_equiv e WHERE e.a_id=l.article_id UNION SELECT e.a_id FROM article_equiv e WHERE e.b_id=l.article_id)))),0) AS commande,
  COALESCE((SELECT SUM(COALESCE(c.qte_livree,0)) FROM bc_lignes c WHERE c.bc_id=COALESCE(l.bc_id,b.bc_id) AND (c.article_id=l.article_id OR (NOT EXISTS(SELECT 1 FROM bc_lignes c0 WHERE c0.bc_id=COALESCE(l.bc_id,b.bc_id) AND c0.article_id=l.article_id) AND c.article_id IN (SELECT e.b_id FROM article_equiv e WHERE e.a_id=l.article_id UNION SELECT e.a_id FROM article_equiv e WHERE e.b_id=l.article_id)))),0)
  + CASE WHEN b.statut='Valide'
    THEN -COALESCE((SELECT SUM(l2.quantite) FROM bl_lignes l2 JOIN bl b2 ON b2.id=l2.bl_id WHERE COALESCE(l2.bc_id,b2.bc_id)=COALESCE(l.bc_id,b.bc_id) AND l2.article_id=l.article_id AND b2.statut='Valide' AND (b2.id>b.id OR (b2.id=b.id AND l2.id>=l.id))),0)
    ELSE COALESCE((SELECT SUM(l2.quantite) FROM bl_lignes l2 WHERE l2.bl_id=b.id AND COALESCE(l2.bc_id,b.bc_id)=COALESCE(l.bc_id,b.bc_id) AND l2.article_id=l.article_id AND l2.id<l.id),0) END AS livre_avant
 FROM bl_lignes l JOIN bl b ON b.id=l.bl_id LEFT JOIN articles a ON a.id=l.article_id /*W*/) x ORDER BY x.id"""
LIST_SQL = {
 'bc': "SELECT bc.*, t.raison_sociale AS tiers_nom, CASE WHEN COALESCE((SELECT SUM(MAX(0, l.quantite)) FROM bc_lignes l WHERE l.bc_id=bc.id),0)<=0 THEN 0 ELSE MIN(100, ROUND(100.0*COALESCE((SELECT SUM(MAX(0, COALESCE(l.qte_livree,0))) FROM bc_lignes l WHERE l.bc_id=bc.id),0)/(SELECT SUM(MAX(0, l.quantite)) FROM bc_lignes l WHERE l.bc_id=bc.id))) END AS pct_livre FROM bc LEFT JOIN tiers t ON t.id=bc.tiers_id ORDER BY bc.id DESC",
 'bl': "SELECT bl.*, t.raison_sociale AS tiers_nom, bc.num AS bc_num FROM bl LEFT JOIN tiers t ON t.id=bl.tiers_id LEFT JOIN bc ON bc.id=bl.bc_id ORDER BY bl.id DESC",
 'retours': "SELECT r.*, t.raison_sociale AS tiers_nom, bl.num AS bl_num FROM retours r LEFT JOIN tiers t ON t.id=r.tiers_id LEFT JOIN bl ON bl.id=r.bl_id ORDER BY r.id DESC",
 'ordres': "SELECT o.*, a.reference||' - '||a.designation AS article_nom, bc.num AS bc_num, t.raison_sociale AS client FROM ordres o LEFT JOIN articles a ON a.id=o.article_id LEFT JOIN bc ON bc.id=o.bc_id LEFT JOIN tiers t ON t.id=bc.tiers_id ORDER BY o.id DESC",
 'articles': "SELECT * FROM articles ORDER BY reference",
 'tiers': "SELECT * FROM tiers ORDER BY raison_sociale",
 'nomenclature': "SELECT n.*, a.designation AS produit, c.designation AS composant FROM nomenclature n LEFT JOIN articles a ON a.id=n.article_id LEFT JOIN articles c ON c.id=n.composant_id ORDER BY a.designation",
 'mouvements': "SELECT m.*, a.reference||' - '||a.designation AS article_nom FROM mouvements m LEFT JOIN articles a ON a.id=m.article_id ORDER BY m.id DESC LIMIT 500",
 'users': "SELECT id, login, nom, role FROM users ORDER BY login",
 'societe': "SELECT * FROM societe WHERE id=1",
}

class ApiError(Exception):
    def __init__(s, code, msg, **x): s.code, s.msg, s.x = code, msg, x

today = lambda: datetime.date.today().isoformat()
now = lambda: datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

def conn():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db
def q(db, sql, a=()): return [dict(r) for r in db.execute(sql, a)]
def one(db, sql, a=()):
    r = db.execute(sql, a).fetchone()
    return dict(r) if r else None
def cols(db, t): return [r['name'] for r in db.execute(f'PRAGMA table_info({t})')]

def hpwd(p, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + '$' + hashlib.pbkdf2_hmac('sha256', p.encode(), salt.encode(), 100000).hex()
def chk(p, h): return secrets.compare_digest(hpwd(p, h.split('$')[0]), h)

def init_db(demo=False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    neuf = os.path.isfile(DB_PATH) and os.path.getsize(DB_PATH) > 0
    if neuf:
        c0 = sqlite3.connect(DB_PATH); version_avant = c0.execute('PRAGMA user_version').fetchone()[0]; migrer = bool(c0.execute("SELECT 1 FROM sqlite_master WHERE name='bc_lignes'").fetchone()) and version_avant < 1; c0.close()
        if version_avant < 2:   # sauvegarde automatique avant la migration des BL et des calculs
            import shutil; shutil.copy2(DB_PATH, DB_PATH + '.avant_recalcul_' + datetime.datetime.now().strftime('%Y%m%d_%H%M'))
    else: migrer = False; version_avant = 0
    db = conn(); db.executescript(SCHEMA)
    for table in ('bc', 'bl'):
        if 'reference' not in cols(db, table): db.execute(f'ALTER TABLE {table} ADD COLUMN reference TEXT')
    for c in ('logo', 'cachet', 'signature'):
        if c not in cols(db, 'societe'): db.execute(f'ALTER TABLE societe ADD COLUMN {c} TEXT')
    for c in ('photo', 'fiche_pdf'):   # migration des bases créées avant l'ajout des fichiers
        if c not in cols(db, 'articles'): db.execute(f'ALTER TABLE articles ADD COLUMN {c} TEXT')
    for lt in ('bc_lignes', 'bl_lignes'):   # unité des pièces sur les lignes (les prix ne sont plus utilisés)
        if 'unite' not in cols(db, lt): db.execute(f"ALTER TABLE {lt} ADD COLUMN unite TEXT DEFAULT 'u'")
        db.execute(f"UPDATE {lt} SET unite=COALESCE((SELECT a.unite FROM articles a WHERE a.id={lt}.article_id),'u') WHERE unite IS NULL OR unite=''")
    if 'qte_report' not in cols(db, 'bc_lignes'): db.execute('ALTER TABLE bc_lignes ADD COLUMN qte_report REAL DEFAULT 0')
    if 'bc_id' not in cols(db, 'bl_lignes'): db.execute('ALTER TABLE bl_lignes ADD COLUMN bc_id INTEGER REFERENCES bc(id)')
    for c, t in (('s_commande', 'REAL'), ('s_reste', 'REAL'), ('s_supp', 'REAL'), ('report_vers', 'TEXT'), ('report_bc_id', 'INTEGER')):
        if c not in cols(db, 'bl_lignes'): db.execute(f'ALTER TABLE bl_lignes ADD COLUMN {c} {t}')
    if migrer:
        migrer_base_livre(db); db.execute('PRAGMA user_version=1')
    if not db.execute('SELECT 1 FROM users').fetchone():
        db.execute("INSERT INTO users(login,nom,role,pwd) VALUES('admin','Administrateur','admin',?)", (hpwd('admin123'),))
    if not db.execute('SELECT 1 FROM societe WHERE id=1').fetchone():
        db.execute("INSERT INTO societe(id,code,raison_sociale) VALUES(1,?,?)", ('STE001', SOCIETE))
    if demo and not db.execute('SELECT 1 FROM tiers').fetchone():
        for r in [('C001', 'Société Bâtiment Plus', 'Client', 'Zone industrielle', 'Bouira'), ('C002', 'Entreprise Sahel', 'Client', 'Rue 1er Novembre', 'Alger'), ('F001', 'Sidérurgie du Centre', 'Fournisseur', 'Zone industrielle', 'Blida')]:
            db.execute('INSERT INTO tiers(code,raison_sociale,type,adresse,ville) VALUES(?,?,?,?,?)', r)
        A = [('MP-TOLE3', 'Tôle acier 3 mm (feuille)', 'Matiere premiere', 'u', 9000, 0, 120, 30), ('MP-IPE100', 'Profilé IPE 100 (6 m)', 'Matiere premiere', 'u', 12000, 0, 60, 20),
             ('CO-ELEC', 'Électrodes de soudage (boîte)', 'Consommable', 'u', 1800, 0, 15, 10), ('CO-PEINT', 'Peinture antirouille (bidon)', 'Consommable', 'u', 3500, 0, 8, 10),
             ('PF-PORTAIL', 'Portail coulissant 4 m', 'Produit fini', 'u', 0, 185000, 0, 0), ('PF-GARDE', 'Garde-corps acier (ml)', 'Produit fini', 'ml', 0, 14500, 0, 0)]
        for a in A:
            db.execute('INSERT INTO articles(reference,designation,type,unite,prix_achat,prix_vente,stock,stock_mini) VALUES(?,?,?,?,?,?,?,?)', a)
        ids = {r['reference']: r['id'] for r in db.execute('SELECT id,reference FROM articles')}
        for p, c, n in [('PF-PORTAIL', 'MP-TOLE3', 4), ('PF-PORTAIL', 'MP-IPE100', 3), ('PF-PORTAIL', 'CO-ELEC', 0.5), ('PF-PORTAIL', 'CO-PEINT', 1), ('PF-GARDE', 'MP-IPE100', 0.3), ('PF-GARDE', 'CO-ELEC', 0.05)]:
            db.execute('INSERT INTO nomenclature(article_id,composant_id,quantite) VALUES(?,?,?)', (ids[p], ids[c], n))
    # Migration unique : regroupe les BL historiques compatibles en un seul BL.
    # Chaque ligne conserve son BC d'origine ; les mouvements de stock sont renommés,
    # jamais recréés, puis les quantités livrées/reportées sont recalculées depuis les BL.
    if version_avant < 2:
        consolider_bls_existants(db)
        db.execute('PRAGMA user_version=2')
    recalculer(db)
    db.commit(); db.close()

def consolider_bls_existants(db):
    bls = q(db, 'SELECT * FROM bl ORDER BY id')
    if len(bls) <= 1:
        return
    # Un seul BL ne peut pas légalement mélanger plusieurs clients, dates ou types.
    # Dans ce cas on ne touche pas aux documents plutôt que de fausser les stocks.
    champs = ('date', 'type', 'tiers_id', 'statut', 'chauffeur', 'vehicule')
    if any(any(b.get(c) != bls[0].get(c) for c in champs) for b in bls[1:]):
        return
    ids = [b['id'] for b in bls]
    placeholders = ','.join('?' for _ in ids)
    lignes = q(db, f'SELECT l.*, b.bc_id AS ancien_bc_id, b.num AS ancien_num FROM bl_lignes l JOIN bl b ON b.id=l.bl_id WHERE l.bl_id IN ({placeholders}) ORDER BY b.id, l.id', ids)
    if not lignes:
        return
    principal = bls[0]
    ancien_nums = [b['num'] for b in bls]
    # Attribue explicitement le BC de l'ancien en-tête à chaque ligne qui n'en avait pas.
    for l in lignes:
        bc_ligne = l.get('bc_id') or l.get('ancien_bc_id')
        db.execute('UPDATE bl_lignes SET bl_id=?, bc_id=? WHERE id=?', (principal['id'], bc_ligne, l['id']))
    # Nouveau numéro de BL, distinct des numéros historiques.
    nouveau_num = next_num(db, 'bl', 'BL')
    remarque = 'BL consolidé automatiquement à partir de : ' + ', '.join(ancien_nums)
    db.execute("UPDATE bl SET num=?, bc_id=NULL, remarque=CASE WHEN COALESCE(remarque,'')='' THEN ? ELSE remarque||char(10)||? END WHERE id=?", (nouveau_num, remarque, remarque, principal['id']))
    # Les mouvements existants restent uniques et gardent leurs quantités ; seule leur origine change.
    db.execute(f'UPDATE mouvements SET origine=? WHERE origine IN ({placeholders})', [nouveau_num, *ancien_nums])
    # Supprime uniquement les en-têtes devenus vides après le déplacement de toutes les lignes.
    db.execute(f'DELETE FROM bl WHERE id IN ({placeholders}) AND id<>?', [*ids, principal['id']])

def next_num(db, t, pre):
    p = f'{datetime.date.today().year}-' if t == 'bl' else f'{pre}-{datetime.date.today().year}-'
    r = db.execute(f"SELECT MAX(CAST(SUBSTR(num,-4) AS INTEGER)) FROM {t} WHERE num LIKE ?", (p + '%',)).fetchone()[0]
    return f'{p}{(r or 0) + 1:04d}'

def clean(d, allowed): return {k: (None if v == '' else v) for k, v in d.items() if k in allowed}

def save(db, t, d, id=None):
    d = dict(d); lignes = d.pop('lignes', None)
    if t == 'users':
        pw = d.pop('pwd', None)
        if pw: d['pwd'] = hpwd(pw)
        elif id is None: raise ApiError(400, 'Mot de passe obligatoire')
    vals = clean(d, [c for c in cols(db, t) if c != 'id' and c not in PROTECT.get(t, ())])
    if id is None:
        if t in DOCS:
            if not vals.get('num'): vals['num'] = next_num(db, t, DOCS[t][2])
            vals.setdefault('date', today())
            if not vals.get('date'): vals['date'] = today()
        id = db.execute(f"INSERT INTO {t}({','.join(vals)}) VALUES({','.join('?' * len(vals))})", list(vals.values())).lastrowid
    else:
        vals.pop('num', None)
        if vals: db.execute(f"UPDATE {t} SET {','.join(k + '=?' for k in vals)} WHERE id=?", [*vals.values(), id])
    if lignes is not None and t in DOCS:
        lt, fk, _ = DOCS[t]
        lc = [c for c in cols(db, lt) if c not in ('id', fk) and not (lt == 'bc_lignes' and c in ('qte_livree', 'qte_report'))]   # quantités livrées = calculées
        db.execute(f'DELETE FROM {lt} WHERE {fk}=?', (id,))
        for l in lignes:
            lv = clean(l, lc)
            db.execute(f"INSERT INTO {lt}({fk}{''.join(',' + k for k in lv)}) VALUES(?{',?' * len(lv)})", [id, *lv.values()])
    return id

def get_one(db, t, id):
    r = one(db, f'SELECT * FROM {t} WHERE id=?', (id,))
    if not r: raise ApiError(404, 'Introuvable')
    r.pop('pwd', None)
    if t in DOCS:
        lt, fk, _ = DOCS[t]
        r['lignes'] = q(db, BL_LIGNES_SQL.replace('/*W*/', 'WHERE l.bl_id=?'), (id,)) if t == 'bl' else q(db, f"SELECT * FROM {lt} WHERE {fk}=? ORDER BY {'ordre,id' if t == 'ordres' else 'id'}", (id,))
        if t == 'bl':
            for l in r['lignes']:
                l['bc_id'] = l.get('source_bc_id') or r.get('bc_id')
                bcrow = one(db, 'SELECT num FROM bc WHERE id=?', (l['bc_id'],)) if l['bc_id'] else None
                l['bc_num'] = bcrow['num'] if bcrow else ''
            r['bc_nums'] = bl_bc_nums(db, r)
        if t == 'bc':
            for l in r['lignes']: l['reste'] = (l['quantite'] or 0) - (l.get('qte_livree') or 0)
        tid = r.get('tiers_id')
        if t == 'ordres':
            r['article'] = one(db, 'SELECT * FROM articles WHERE id=?', (r['article_id'],)) if r['article_id'] else None
            bc = one(db, 'SELECT * FROM bc WHERE id=?', (r['bc_id'],)) if r['bc_id'] else None
            r['bc_num'] = bc['num'] if bc else None
            tid = bc['tiers_id'] if bc else None
        r['tiers'] = one(db, 'SELECT * FROM tiers WHERE id=?', (tid,)) if tid else None
    return r

def bl_bc_nums(db, bl):
    """Tous les N° de BC d'un BL : BC des lignes, BC choisis pour le report et BC qui ont réellement reçu un report (dans cet ordre, sans doublon)."""
    ids = [l.get('bc_id') or bl.get('bc_id') for l in bl['lignes']] + [l.get('report_bc_id') for l in bl['lignes']]
    ids += [r['bc_id'] for r in q(db, 'SELECT bc_id FROM bl_report WHERE bl_num=? ORDER BY id', (bl['num'],))]
    out = []
    for i in ids:
        if i:
            n = (db.execute('SELECT num FROM bc WHERE id=?', (i,)).fetchone() or {'num': None})['num']
            if n and n not in out: out.append(n)
    return out

def mvt(db, art, sens, qte, ref):
    db.execute('UPDATE articles SET stock=COALESCE(stock,0)+? WHERE id=?', ((-1 if sens == 'Sortie' else 1) * qte, art))
    db.execute('INSERT INTO mouvements(date,article_id,sens,quantite,origine) VALUES(?,?,?,?,?)', (now(), art, sens, qte, ref))

def maj_bc(db, id):
    """Statut d'un BC déduit de ses lignes : Solde (tout livré), Partiel (une livraison au moins), Valide (rien livré)."""
    n, tot, nl = db.execute("SELECT SUM(COALESCE(qte_livree,0)<COALESCE(quantite,0)), SUM(COALESCE(qte_livree,0)), COUNT(*) FROM bc_lignes WHERE bc_id=?", (id,)).fetchone()
    st = 'Valide' if not nl or (tot or 0) <= 0 else ('Solde' if not n else 'Partiel')
    db.execute("UPDATE bc SET statut=? WHERE id=? AND statut NOT IN ('Annule','Brouillon')", (st, id))

def need(db, t, id):
    r = one(db, f'SELECT * FROM {t} WHERE id=?', (id,))
    if not r: raise ApiError(404, 'Introuvable')
    return r

def fpath(name):
    return os.path.join(FILES, name) if name and SAFE_NAME.match(name) else None

def rm_file(name):
    pth = fpath(name)
    if pth:
        with contextlib.suppress(OSError): os.remove(pth)

def sniff(kind, data):
    """Type réel du fichier d'après son contenu (on ne fait pas confiance à l'extension envoyée)."""
    if kind == 'pdf': return '.pdf' if b'%PDF-' in data[:1024] else None
    if data.startswith(b'\xff\xd8\xff'): return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if data[:6] in (b'GIF87a', b'GIF89a'): return '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return '.webp'

def file_put(db, id, kind, data):
    col, mx = KINDS[kind]
    art = need(db, 'articles', id)
    if not data: raise ApiError(400, 'Fichier vide')
    if len(data) > mx: raise ApiError(413, f'Fichier trop volumineux (max {mx >> 20} Mo)')
    ext = sniff(kind, data)
    if not ext: raise ApiError(415, 'Le fichier doit être un PDF' if kind == 'pdf' else 'La photo doit être au format JPG, PNG, GIF ou WEBP')
    os.makedirs(FILES, exist_ok=True)
    name = f'{id}-{kind}-{secrets.token_hex(4)}{ext}'
    with open(fpath(name), 'wb') as f: f.write(data)
    db.execute(f'UPDATE articles SET {col}=? WHERE id=?', (name, id))
    rm_file(art[col])
    return {'ok': True, col: name}

def file_get(db, id, kind):
    col = KINDS[kind][0]
    a = need(db, 'articles', id)
    pth = fpath(a[col])
    if not pth or not os.path.isfile(pth): raise ApiError(404, 'Aucun fichier')
    ext = os.path.splitext(pth)[1].lower()
    nom = re.sub(r'[^A-Za-z0-9._-]+', '_', a['reference'] or 'article') + ('-fiche' if kind == 'pdf' else '') + ext
    with open(pth, 'rb') as f: return f.read(), CT.get(ext, 'application/octet-stream'), nom

def file_del(db, id, kind):
    col = KINDS[kind][0]
    a = need(db, 'articles', id)
    db.execute(f'UPDATE articles SET {col}=NULL WHERE id=?', (id,))
    rm_file(a[col])
    return {'ok': True}

def soc_file_put(db, kind, data):
    if kind not in SOC_KINDS: raise ApiError(404, 'Type de fichier inconnu')
    col, mx = SOC_KINDS[kind]
    if not data: raise ApiError(400, 'Fichier vide')
    if len(data) > mx: raise ApiError(413, f'Fichier trop volumineux (max {mx >> 20} Mo)')
    ext = sniff('photo', data)
    if ext == '.jpg': ext = '.jpg'
    if not ext: raise ApiError(415, 'Le fichier doit être une image JPG, PNG, GIF ou WEBP')
    os.makedirs(FILES, exist_ok=True)
    name = f'societe-{kind}-{secrets.token_hex(4)}{ext}'
    with open(fpath(name), 'wb') as f: f.write(data)
    old = one(db, 'SELECT '+col+' FROM societe WHERE id=1')
    db.execute(f'UPDATE societe SET {col}=? WHERE id=1', (name,))
    if old: rm_file(old.get(col))
    return {'ok': True, col: name}

def soc_file_get(db, kind):
    if kind not in SOC_KINDS: raise ApiError(404, 'Type de fichier inconnu')
    col = SOC_KINDS[kind][0]
    s = one(db, 'SELECT * FROM societe WHERE id=1')
    pth = fpath(s.get(col) if s else None)
    if not pth or not os.path.isfile(pth): raise ApiError(404, 'Aucun fichier')
    ext = os.path.splitext(pth)[1].lower()
    with open(pth, 'rb') as f: return f.read(), CT.get(ext, 'application/octet-stream'), f'societe-{kind}{ext}'

def soc_file_del(db, kind):
    if kind not in SOC_KINDS: raise ApiError(404, 'Type de fichier inconnu')
    col = SOC_KINDS[kind][0]
    s = one(db, 'SELECT '+col+' FROM societe WHERE id=1')
    db.execute(f'UPDATE societe SET {col}=NULL WHERE id=1')
    if s: rm_file(s.get(col))
    return {'ok': True}

def bc_valider(db, id, force):
    h = need(db, 'bc', id)
    if h['statut'] != 'Brouillon': raise ApiError(409, 'Seul un BC en brouillon peut être validé')
    if not db.execute('SELECT 1 FROM bc_lignes WHERE bc_id=?', (id,)).fetchone(): raise ApiError(400, 'BC sans ligne')
    db.execute("UPDATE bc SET statut='Valide' WHERE id=?", (id,))
    recalculer(db)   # récupère aussi les quantités en attente des BC précédents
    return {'msg': 'BC validé'}

def bc_annuler(db, id, force):
    h = need(db, 'bc', id)
    if h['statut'] in ('Solde', 'Annule'): raise ApiError(409, f"BC {h['statut']} : annulation impossible")
    db.execute("UPDATE bc SET statut='Annule' WHERE id=?", (id,))
    return {'msg': 'BC annulé'}

def bc_generer_of(db, id, force):
    h = need(db, 'bc', id)
    if h['type'] != 'Client': raise ApiError(409, 'Seuls les BC clients génèrent des OF')
    if h['statut'] in ('Brouillon', 'Annule'): raise ApiError(409, "Validez d'abord le BC")
    n = 0
    for l in q(db, "SELECT l.article_id, l.quantite FROM bc_lignes l JOIN articles a ON a.id=l.article_id WHERE l.bc_id=? AND a.type='Produit fini'", (id,)):
        if db.execute('SELECT 1 FROM ordres WHERE bc_id=? AND article_id=?', (id, l['article_id'])).fetchone(): continue
        oid = db.execute("INSERT INTO ordres(num,date,article_id,qte_a_fabriquer,bc_id,date_fin) VALUES(?,?,?,?,?,?)",
                         (next_num(db, 'ordres', 'OF'), today(), l['article_id'], l['quantite'], id, h['date_livraison'])).lastrowid
        for i, op in enumerate(['Decoupe', 'Perçage', 'Pliage', 'Soudage', 'Controle'], 1):
            db.execute('INSERT INTO ordre_operations(ordre_id,ordre,operation) VALUES(?,?,?)', (oid, i, op))
        n += 1
    return {'msg': f'{n} ordre(s) de production créé(s)' if n else 'Aucun OF à créer (lignes « Produit fini » déjà lancées ou absentes)'}

def equiv_ids(db, a):
    return [r[0] for r in db.execute('SELECT b_id FROM article_equiv WHERE a_id=? UNION SELECT a_id FROM article_equiv WHERE b_id=?', (a, a))]

def bc_match_lines(db, bc_id, article_id):
    """Lignes du BC qui reçoivent une quantité de cet article : l'article lui-même, à défaut ses articles équivalents."""
    sel = 'SELECT id, quantite, COALESCE(qte_livree,0) AS lv, article_id FROM bc_lignes WHERE bc_id=? AND article_id'
    rows = db.execute(sel + '=? ORDER BY id', (bc_id, article_id)).fetchall()
    if not rows and article_id:
        eq = equiv_ids(db, article_id)
        if eq: rows = db.execute(sel + f' IN ({",".join("?" * len(eq))}) ORDER BY id', (bc_id, *eq)).fetchall()
    return rows

def imputer(db, bc_id, article_id, qte):
    """Impute une quantité livrée/reçue sur les lignes du BC. Retourne (commandé, reste avant, quantité en trop)."""
    rows = bc_match_lines(db, bc_id, article_id)
    commande = sum(r['quantite'] or 0 for r in rows)
    reste = sum(max(0, (r['quantite'] or 0) - r['lv']) for r in rows)
    supp = max(0, qte - reste); rest = qte - supp
    for r in rows:
        part = min(rest, max(0, (r['quantite'] or 0) - r['lv']))
        if part > 0: db.execute('UPDATE bc_lignes SET qte_livree=COALESCE(qte_livree,0)+? WHERE id=?', (part, r['id'])); rest -= part
    return commande, reste, supp

def _ref(db, article_id):
    r = db.execute('SELECT reference FROM articles WHERE id=?', (article_id,)).fetchone()
    return r['reference'] if r else ''

def _fill(db, bc_id, article_id, qte, note):
    """Impute un report sur les lignes du BC (jusqu'au reste de chaque ligne). Retourne la quantité absorbée."""
    donne = 0
    for r in bc_match_lines(db, bc_id, article_id):
        part = min(qte - donne, max(0, (r['quantite'] or 0) - r['lv']))
        if part > 0: db.execute('UPDATE bc_lignes SET qte_livree=COALESCE(qte_livree,0)+?, qte_report=COALESCE(qte_report,0)+? WHERE id=?', (part, part, r['id'])); donne += part
    if donne:
        db.execute("UPDATE bc SET remarque=CASE WHEN COALESCE(remarque,'')='' THEN ? ELSE remarque||char(10)||? END WHERE id=?", (f"{note} : {donne:g} x {_ref(db, article_id)}",) * 2 + (bc_id,))
    return donne

def report_texte(given, left):
    parts = [f'{n} ({g:g})' for n, g in given] + ([f'en attente ({left:g})'] if left > 1e-9 else [])
    return ' ; '.join(parts) or None

def report_choix_ok(db, src_bc_id, choix, article_id):
    """Le BC choisi pour recevoir le surplus est-il valable ? (autre BC, même tiers et même type, Valide/Partiel, contient l'article ou un équivalent)"""
    if not choix or int(choix) == src_bc_id: return False
    b = db.execute('SELECT tiers_id, type FROM bc WHERE id=?', (src_bc_id,)).fetchone()
    c = db.execute('SELECT tiers_id, type, statut FROM bc WHERE id=?', (int(choix),)).fetchone()
    if not b or not c or c['statut'] not in ('Valide', 'Partiel') or c['tiers_id'] != b['tiers_id'] or c['type'] != b['type']: return False
    return bool(bc_match_lines(db, int(choix), article_id))

def reporter_surplus(db, bc_id, article_id, qte, bl_num, choix=None):
    """Quantité livrée en plus du reste du BC : imputée d'abord sur le BC CHOISI par l'utilisateur (s'il y en a un), puis sur le(s) BC suivant(s) du même tiers
    contenant le même produit (automatique) ; sinon mise en attente."""
    b = need(db, 'bc', bc_id); given = []; left = qte
    cands = []
    if choix and report_choix_ok(db, bc_id, choix, article_id): cands.append(q(db, 'SELECT id, num FROM bc WHERE id=?', (int(choix),))[0])
    cands += [t for t in q(db, "SELECT id, num FROM bc WHERE tiers_id IS ? AND type=? AND statut IN ('Valide','Partiel') AND id>? ORDER BY id", (b['tiers_id'], b['type'], bc_id)) if not (cands and t['id'] == cands[0]['id'])]
    for t in cands:
        if left <= 1e-9: break
        g = _fill(db, t['id'], article_id, left, f"Report {bl_num} ({b['num']})")
        if g: given.append((t['num'], g)); left -= g; db.execute('INSERT INTO bl_report(bl_num,article_id,src_bc_id,bc_id,qte) VALUES(?,?,?,?,?)', (bl_num, article_id, bc_id, t['id'], g))
    if left > 1e-9: db.execute('INSERT INTO bc_surplus(bc_id,article_id,qte,bl_num) VALUES(?,?,?,?)', (bc_id, article_id, left, bl_num))
    return given, left

def _retirer(db, bc_id, article_id, n, report=False):
    """Retire n pièces des quantités livrées d'un BC (dernière ligne d'abord). Retourne la quantité réellement retirée."""
    done = 0
    for r in reversed(bc_match_lines(db, bc_id, article_id)):
        t = min(n - done, r['lv'])
        if t > 1e-9:
            db.execute('UPDATE bc_lignes SET qte_livree=COALESCE(qte_livree,0)-?' + (', qte_report=MAX(0, COALESCE(qte_report,0)-?)' if report else '') + ' WHERE id=?', (t, t, r['id']) if report else (t, r['id'])); done += t
    return done

def _defaire(db, bc_id, article_id, n, bl_num):
    """Annule l'imputation d'une livraison (retour client), dans l'ordre inverse : surplus en attente, puis BC qui ont reçu le report, puis BC d'origine."""
    left = n
    for r in q(db, 'SELECT id, qte FROM bc_surplus WHERE bc_id=? AND article_id=? AND bl_num=? ORDER BY id DESC', (bc_id, article_id, bl_num)):
        t = min(left, r['qte'] or 0)
        if t <= 1e-9: continue
        if (r['qte'] or 0) - t <= 1e-9: db.execute('DELETE FROM bc_surplus WHERE id=?', (r['id'],))
        else: db.execute('UPDATE bc_surplus SET qte=qte-? WHERE id=?', (t, r['id']))
        left -= t
    src_num, ref = need(db, 'bc', bc_id)['num'], _ref(db, article_id)
    for r in q(db, 'SELECT id, bc_id, qte FROM bl_report WHERE src_bc_id=? AND article_id=? AND bl_num=? ORDER BY id DESC', (bc_id, article_id, bl_num)):
        if left <= 1e-9: break
        t = _retirer(db, r['bc_id'], article_id, min(left, r['qte'] or 0), True)
        if t <= 1e-9: continue
        db.execute('UPDATE bl_report SET qte=qte-? WHERE id=?', (t, r['id'])); left -= t
        pre, suf = f'Report {bl_num} ({src_num}) : ', f' x {ref}'
        tb = db.execute('SELECT remarque FROM bc WHERE id=?', (r['bc_id'],)).fetchone()
        if tb and tb['remarque']:
            out = []
            for x in tb['remarque'].split('\n'):
                if x.startswith(pre) and x.endswith(suf):
                    try: ng = float(x[len(pre):-len(suf)]) - t
                    except ValueError: out.append(x); continue
                    if ng > 1e-9: out.append(f'{pre}{ng:g}{suf}')
                else: out.append(x)
            db.execute('UPDATE bc SET remarque=? WHERE id=?', ('\n'.join(out) or None, r['bc_id']))
    if left > 1e-9: _retirer(db, bc_id, article_id, left)

def retour_deduire(db, r, cap):
    """RETOUR CLIENT validé et lié à un BL : les pièces retournées ne comptent plus comme livrées -> le reste à livrer du BC (ou du BC qui avait reçu le report) augmente."""
    bl = one(db, 'SELECT id, num, bc_id FROM bl WHERE id=?', (r['bl_id'],))
    if not bl: return
    for l in q(db, 'SELECT article_id, SUM(quantite) AS qte FROM retour_lignes WHERE retour_id=? AND article_id IS NOT NULL GROUP BY article_id', (r['id'],)):
        rem = l['qte'] or 0
        for bll in reversed(q(db, 'SELECT id, quantite, bc_id FROM bl_lignes WHERE bl_id=? AND article_id=? ORDER BY id', (bl['id'], l['article_id']))):
            if rem <= 1e-9: break
            bid = bll['bc_id'] or bl['bc_id']
            take = min(rem, cap.setdefault(bll['id'], bll['quantite'] or 0))
            if take <= 1e-9: continue
            cap[bll['id']] -= take; rem -= take
            if bid: _defaire(db, bid, l['article_id'], take, bl['num'])

AUTO_NOTE = re.compile(r'^Report .+ \(.+\) : [\d.]+ x .+$')

def migrer_base_livre(db):
    """Une seule fois : sépare, pour chaque (BC, article), la quantité « déjà livrée avant l'application » de celle qui vient des BL validés."""
    for r in q(db, "SELECT source_bc_id AS bc_id, article_id, MIN(rid) AS rid, commande, reste_avant FROM (SELECT x.id AS rid, x.bl_id, x.source_bc_id, x.article_id, x.commande, x.reste_avant FROM (" + BL_LIGNES_SQL.replace('/*W*/', "WHERE b.statut='Valide' AND COALESCE(l.bc_id,b.bc_id) IS NOT NULL") + ") x JOIN bl ON bl.id=x.bl_id WHERE x.article_id IS NOT NULL ORDER BY bl.id, x.id) GROUP BY source_bc_id, article_id"):
        base = max(0, (r['commande'] or 0) - (r['reste_avant'] or 0))
        if base > 0: db.execute('INSERT OR REPLACE INTO bc_livre_initial(bc_id,article_id,qte) VALUES(?,?,?)', (r['bc_id'], r['article_id'], base))
    for r in q(db, "SELECT bc_id, article_id, SUM(COALESCE(qte_livree,0)-COALESCE(qte_report,0)) AS base FROM bc_lignes WHERE article_id IS NOT NULL GROUP BY bc_id, article_id"):
        if db.execute('SELECT 1 FROM bc_livre_initial WHERE bc_id=? AND article_id=?', (r['bc_id'], r['article_id'])).fetchone(): continue
        has_bl = db.execute("SELECT 1 FROM bl_lignes l JOIN bl ON bl.id=l.bl_id WHERE bl.statut='Valide' AND bl.bc_id=? AND l.article_id=?", (r['bc_id'], r['article_id'])).fetchone()
        if r['base'] and r['base'] > 0 and not has_bl: db.execute('INSERT INTO bc_livre_initial(bc_id,article_id,qte) VALUES(?,?,?)', (r['bc_id'], r['article_id'], r['base']))

def recalculer(db):
    """CALCUL AUTOMATIQUE : reconstruit toutes les quantités livrées / reçues des BC à partir d'une seule source de vérité —
    (livré avant l'application) + (BL validés, rejoués dans l'ordre), avec report des quantités en trop sur le BC suivant du même tiers.
    Appelé après chaque modification (BC, BL, OF, articles…) et au démarrage : plus aucune valeur stockée ne peut diverger."""
    db.execute('UPDATE bc_lignes SET qte_livree=0, qte_report=0')
    for r in q(db, 'SELECT bc_id, article_id, qte FROM bc_livre_initial'):
        lines = db.execute('SELECT id, quantite FROM bc_lignes WHERE bc_id=? AND article_id=? ORDER BY id', (r['bc_id'], r['article_id'])).fetchall(); rest = r['qte'] or 0
        for i, l in enumerate(lines):
            part = rest if i == len(lines) - 1 else min(rest, l['quantite'] or 0)
            if part > 0: db.execute('UPDATE bc_lignes SET qte_livree=COALESCE(qte_livree,0)+? WHERE id=?', (part, l['id'])); rest -= part
    db.execute('DELETE FROM bc_surplus'); db.execute('DELETE FROM bl_report')
    db.execute("UPDATE bc SET statut='Valide' WHERE statut IN ('Partiel','Solde')")
    for b in q(db, "SELECT id, remarque FROM bc WHERE remarque LIKE '%Report %'"):   # notes automatiques régénérées ci-dessous
        keep = '\n'.join(x for x in (b['remarque'] or '').split('\n') if not AUTO_NOTE.match(x))
        db.execute('UPDATE bc SET remarque=? WHERE id=?', (keep or None, b['id']))
    ev = [(b['id'], 0, b['id'], b) for b in q(db, "SELECT id, num, bc_id FROM bl WHERE statut='Valide'")]
    for r in q(db, "SELECT r.id, r.bl_id, r.date, COALESCE((SELECT MAX(b.id) FROM bl b WHERE b.statut='Valide' AND b.date<r.date AND b.id<COALESCE((SELECT MIN(b2.id) FROM bl b2 WHERE b2.statut='Valide' AND b2.date>=r.date),9e18)),0) AS apres FROM retours r WHERE r.statut='Valide' AND r.bl_id IS NOT NULL"):
        ev.append((max(r['bl_id'], r['apres']), 1, r['id'], r))   # un retour est rejoué juste après le BL concerné et après les BL datés AVANT son jour ; les BL du même jour ou plus récents le voient déjà pris en compte (leur « reste avant » inclut les pièces retournées)
    cap = {}
    for _, kind, _, b in sorted(ev, key=lambda e: e[:3]):
        if kind: retour_deduire(db, b, cap); continue
        touched = set()
        for l in q(db, 'SELECT id, article_id, quantite, bc_id, report_bc_id FROM bl_lignes WHERE bl_id=? ORDER BY id', (b['id'],)):
            if not l['article_id']: continue
            bid = l['bc_id'] or b['bc_id']
            if not bid: continue
            touched.add(bid)
            c, ra, sup = imputer(db, bid, l['article_id'], l['quantite'] or 0)
            rep = report_texte(*reporter_surplus(db, bid, l['article_id'], sup, b['num'], l['report_bc_id'])) if sup > 0 else None
            db.execute('UPDATE bl_lignes SET s_commande=?, s_reste=?, s_supp=?, report_vers=? WHERE id=?', (c, ra, sup, rep, l['id']))
        for bid in touched: maj_bc(db, bid)
    for b in q(db, "SELECT id FROM bc WHERE statut NOT IN ('Annule','Brouillon')"): maj_bc(db, b['id'])

def recalculer_stock(db):
    """Reconstruit le stock article depuis le journal des mouvements (source de vérité)."""
    db.execute("""UPDATE articles SET stock=COALESCE((
        SELECT SUM(CASE sens WHEN 'Entree' THEN COALESCE(quantite,0)
                             WHEN 'Sortie' THEN -COALESCE(quantite,0) ELSE 0 END)
        FROM mouvements WHERE mouvements.article_id=articles.id),0)""")

def audit(db):
    """Contrôle de cohérence BC/BL/retours/OF et du journal de stock."""
    A = []
    add = lambda niv, typ, doc, msg: A.append({'niveau': niv, 'type': typ, 'doc': doc, 'message': msg})
    # Article livré absent du BC associé.
    for r in q(db, "SELECT b.num AS bl, bc.num AS bc, a.reference AS ref, l.article_id, COALESCE(l.bc_id,b.bc_id) AS bc_id, b.statut FROM bl_lignes l JOIN bl b ON b.id=l.bl_id LEFT JOIN bc ON bc.id=COALESCE(l.bc_id,b.bc_id) LEFT JOIN articles a ON a.id=l.article_id WHERE l.article_id IS NOT NULL AND b.statut IN ('Valide','Brouillon') AND COALESCE(l.bc_id,b.bc_id) IS NOT NULL"):
        if not bc_match_lines(db, r['bc_id'], r['article_id']):
            add('erreur' if r['statut'] == 'Valide' else 'alerte', 'Article hors BC', r['bl'], f"{r['bl']} : l'article {r['ref']} ne figure pas sur {r['bc']} (ni article équivalent).")
    for r in q(db, "SELECT bc.num, a.reference AS ref, l.quantite, l.qte_livree FROM bc_lignes l JOIN bc ON bc.id=l.bc_id LEFT JOIN articles a ON a.id=l.article_id WHERE COALESCE(l.qte_livree,0) > COALESCE(l.quantite,0) + 1e-9"):
        add('alerte', 'Livré > commandé', r['num'], f"{r['num']} / {r['ref']} : livré {r['qte_livree']:g} pour {r['quantite']:g} commandé.")
    # Journal: type, quantité et référence article doivent être valides.
    for m in q(db, "SELECT m.*, a.reference AS ref FROM mouvements m LEFT JOIN articles a ON a.id=m.article_id ORDER BY m.date,m.id"):
        if not m['ref']:
            add('erreur', 'Mouvement sans article', str(m['id']), f"Mouvement #{m['id']} : article inexistant (ID {m['article_id']}).")
        if m['sens'] not in ('Entree','Sortie'):
            add('erreur', 'Sens de mouvement invalide', str(m['id']), f"Mouvement #{m['id']} ({m['ref'] or 'article inconnu'}) : sens « {m['sens']} » non reconnu.")
        if m['quantite'] is None or m['quantite'] <= 0:
            add('erreur', 'Quantité de mouvement invalide', str(m['id']), f"Mouvement #{m['id']} ({m['ref'] or 'article inconnu'}) : quantité doit être strictement positive.")
    # Stock enregistré vs journal et stock négatif courant.
    for r in q(db, "SELECT a.id, a.reference, a.stock, COALESCE((SELECT SUM(CASE m.sens WHEN 'Entree' THEN m.quantite WHEN 'Sortie' THEN -m.quantite ELSE 0 END) FROM mouvements m WHERE m.article_id=a.id),0) AS mv FROM articles a"):
        if abs((r['stock'] or 0) - (r['mv'] or 0)) > 1e-6:
            add('erreur', 'Stock ≠ mouvements', r['reference'], f"{r['reference']} : stock enregistré {r['stock'] or 0:g}, journal {r['mv'] or 0:g}.")
        if (r['stock'] or 0) < -1e-6:
            add('alerte', 'Stock négatif', r['reference'], f"{r['reference']} : stock négatif ({r['stock']:g}). Vérifier le stock initial, les BL et les réceptions/fabrications.")
    # Détecte le premier mouvement qui fait passer le solde sous zéro. Les BL annulés et leur contre-passation sont exclus du solde opérationnel : ils sont neutralisés.
    bl_annules = {r['num'] for r in q(db, "SELECT num FROM bl WHERE statut='Annule'")}
    for a in q(db, 'SELECT id, reference FROM articles ORDER BY reference'):
        solde = 0.0
        for m in q(db, "SELECT id,date,sens,quantite,origine FROM mouvements WHERE article_id=? AND sens IN ('Entree','Sortie') ORDER BY COALESCE(date,''),id", (a['id'],)):
            origine = m['origine'] or ''
            if origine in bl_annules or any(origine == n + ' (annulation)' for n in bl_annules):
                continue
            solde += (m['quantite'] or 0) * (1 if m['sens']=='Entree' else -1)
            if solde < -1e-6:
                add('alerte', 'Solde devenu négatif', a['reference'], f"Après le mouvement #{m['id']} du {m['date'] or 'date inconnue'} ({origine or 'origine absente'}), solde théorique = {solde:g}.")
                break
    # Contrôle des mouvements de chaque BL validé, agrégé par article (gère plusieurs lignes du même article).
    for b in q(db, "SELECT id,num,type,statut FROM bl WHERE statut IN ('Valide','Annule')"):
        lines = {r['article_id']: (r['qte'] or 0) for r in q(db, 'SELECT article_id,SUM(quantite) AS qte FROM bl_lignes WHERE bl_id=? AND article_id IS NOT NULL GROUP BY article_id', (b['id'],))}
        movs = {r['article_id']: (r['qte'] or 0) for r in q(db, "SELECT article_id,SUM(CASE WHEN sens=? THEN quantite ELSE -quantite END) AS qte FROM mouvements WHERE (origine=? OR origine=? OR origine=?) AND article_id IS NOT NULL GROUP BY article_id", ('Sortie' if b['type']=='Livraison' else 'Entree', b['num'], b['num']+' (modification)', b['num']+' (annulation)'))}
        # Pour un BL annulé, les mouvements originaux et la contre-passation doivent s'annuler.
        for aid in set(lines) | set(movs):
            attendu = 0 if b['statut']=='Annule' else lines.get(aid,0)
            obtenu = movs.get(aid,0)
            art = db.execute('SELECT reference FROM articles WHERE id=?',(aid,)).fetchone()
            ref = art['reference'] if art else f'article ID {aid}'
            if abs(obtenu-attendu)>1e-6:
                add('erreur', 'Mouvement BL incohérent', b['num'], f"{b['num']} / {ref} ({b['statut']}) : attendu net {attendu:g}, journal lié au BL {obtenu:g}.")
    # Retours clients validés vers Stock : une entrée doit exister.
    for r in q(db, "SELECT r.id,r.num,a.reference AS ref,l.article_id,SUM(l.quantite) AS qte,COALESCE((SELECT SUM(m.quantite) FROM mouvements m WHERE m.origine=r.num AND m.article_id=l.article_id AND m.sens='Entree'),0) AS mv FROM retour_lignes l JOIN retours r ON r.id=l.retour_id LEFT JOIN articles a ON a.id=l.article_id WHERE r.statut='Valide' AND l.article_id IS NOT NULL AND COALESCE(l.destination,'Stock')='Stock' GROUP BY r.id,l.article_id"):
        if abs((r['mv'] or 0) - (r['qte'] or 0)) > 1e-6:
            add('erreur', 'Retour sans mouvement', r['num'], f"{r['num']} / {r['ref']} : retourné {r['qte']:g}, entrées trouvées {r['mv']:g}.")
    # OF en cours/planifiés ne doivent pas déjà générer des mouvements de production.
    for o in q(db, "SELECT id,num,statut,article_id,qte_a_fabriquer,qte_fabriquee FROM ordres"):
        n = db.execute('SELECT COUNT(*) FROM mouvements WHERE origine=?',(o['num'],)).fetchone()[0]
        if o['statut'] in ('Planifie','En cours','Suspendu') and n:
            add('erreur', 'Mouvement avant fin OF', o['num'], f"{o['num']} est {o['statut']} mais possède {n} mouvement(s) de stock ; la production ne doit entrer en stock qu'à la clôture réelle.")
        if o['statut']=='Termine' and o['article_id']:
            out = db.execute("SELECT COALESCE(SUM(quantite),0) FROM mouvements WHERE origine=? AND article_id=? AND sens='Entree'",(o['num'],o['article_id'])).fetchone()[0]
            if abs((out or 0)-(o['qte_fabriquee'] or 0))>1e-6:
                add('erreur', 'OF terminé sans entrée cohérente', o['num'], f"{o['num']} : fabriqué {o['qte_fabriquee'] or 0:g}, entrée en stock {out or 0:g}.")
    for r in q(db, "SELECT bc.num, l.designation FROM bc_lignes l JOIN bc ON bc.id=l.bc_id WHERE l.article_id IS NULL AND bc.statut NOT IN ('Annule','Brouillon')"):
        add('alerte', 'Ligne sans article', r['num'], f"{r['num']} : « {r['designation']} » n'est liée à aucun article.")
    nb_err = sum(1 for a in A if a['niveau']=='erreur')
    nb_alert = sum(1 for a in A if a['niveau']=='alerte')
    return {'ok': nb_err == 0, 'anomalies': A, 'resume': {'articles': db.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 'mouvements': db.execute('SELECT COUNT(*) FROM mouvements').fetchone()[0], 'erreurs': nb_err, 'alertes': nb_alert, 'articles_stock_coherent': db.execute("SELECT COUNT(*) FROM articles a WHERE ABS(COALESCE(a.stock,0)-COALESCE((SELECT SUM(CASE m.sens WHEN 'Entree' THEN m.quantite WHEN 'Sortie' THEN -m.quantite ELSE 0 END) FROM mouvements m WHERE m.article_id=a.id),0))<0.000001").fetchone()[0]}}

def bl_valider(db, id, force):
    h = need(db, 'bl', id)
    if h['statut'] != 'Brouillon': raise ApiError(409, 'BL déjà validé ou annulé')
    L = q(db, 'SELECT * FROM bl_lignes WHERE bl_id=?', (id,))
    if not L: raise ApiError(400, 'BL sans ligne')
    for l in L:
        if l['article_id'] and (l['quantite'] or 0) <= 0: raise ApiError(400, 'Quantité invalide sur une ligne : elle doit être supérieure à 0')
        src = l.get('bc_id') or h.get('bc_id')
        if l.get('report_bc_id') and src and l['article_id'] and not report_choix_ok(db, src, l['report_bc_id'], l['article_id']):
            raise ApiError(400, "BC choisi pour le report invalide : il doit être un autre BC du même tiers, validé ou partiel, contenant cet article (ou un équivalent)")
    liv = h['type'] == 'Livraison'
    if not force:
        need_q, warn = {}, []
        for l in L:
            if l['article_id']: need_q[l['article_id']] = need_q.get(l['article_id'], 0) + (l['quantite'] or 0)
        if liv:
            for a, qte in need_q.items():
                s = db.execute('SELECT stock, designation FROM articles WHERE id=?', (a,)).fetchone()
                if (s['stock'] or 0) < qte: warn.append(f"{s['designation']} (stock {s['stock'] or 0}, requis {qte})")
        msgs = ['Stock insuffisant : ' + '; '.join(warn)] if warn else []
        for l in L:
            bid = l.get('bc_id') or h.get('bc_id')
            if bid and l.get('article_id') and not bc_match_lines(db, bid, l['article_id']):
                bcn = db.execute('SELECT num FROM bc WHERE id=?', (bid,)).fetchone()['num']
                an = db.execute('SELECT designation FROM articles WHERE id=?', (l['article_id'],)).fetchone()['designation']
                msgs.append(f"Article {an} absent du {bcn} ; choisissez le bon BC lié ou déclarez une équivalence")
        if msgs: raise ApiError(409, ' | '.join(msgs), confirm=True)
    for l in L:
        if l['article_id']: mvt(db, l['article_id'], 'Sortie' if liv else 'Entree', l['quantite'] or 0, h['num'])
    db.execute("UPDATE bl SET statut='Valide' WHERE id=?", (id,))
    recalculer(db)   # quantités livrées, reports, reste à livrer et statut du BC : recalculés automatiquement
    rep = db.execute('SELECT 1 FROM bl_lignes WHERE bl_id=? AND COALESCE(s_supp,0)>0', (id,)).fetchone()
    return {'msg': 'BL validé : stock mis à jour' + (' ; quantités supplémentaires reportées (BC choisi, puis BC suivant automatique)' if rep else '')}

def retour_valider(db, id, force):
    """RETOUR CLIENT : la validation remet en stock les pièces retournées (mouvement « Entrée » par article, origine = n° du retour).
    Les lignes dont la destination est « Rebut » (pièces inutilisables) sont enregistrées mais ne ré-entrent pas en stock."""
    h = need(db, 'retours', id)
    if h['statut'] != 'Brouillon': raise ApiError(409, 'Retour déjà validé ou annulé')
    L = q(db, 'SELECT * FROM retour_lignes WHERE retour_id=?', (id,))
    if not L: raise ApiError(400, 'Retour sans ligne')
    if not h['tiers_id']: raise ApiError(400, 'Choisissez le client qui retourne la marchandise')
    for l in L:
        if not l['article_id']: raise ApiError(400, 'Chaque ligne doit être liée à un article (sinon le stock ne peut pas être mis à jour)')
        if (l['quantite'] or 0) <= 0: raise ApiError(400, 'Quantité retournée invalide : elle doit être supérieure à 0')
    if h['bl_id']:
        bl = need(db, 'bl', h['bl_id'])
        if bl['tiers_id'] and bl['tiers_id'] != h['tiers_id']: raise ApiError(400, "Le client du retour n'est pas celui du BL d'origine")
    if not force and h['bl_id']:   # contrôle par rapport au BL de livraison d'origine
        msgs = []
        tot = {}
        for l in L: tot[l['article_id']] = tot.get(l['article_id'], 0) + (l['quantite'] or 0)
        for a, qte in tot.items():
            liv = db.execute('SELECT COALESCE(SUM(quantite),0) FROM bl_lignes WHERE bl_id=? AND article_id=?', (h['bl_id'], a)).fetchone()[0]
            deja = db.execute("SELECT COALESCE(SUM(l.quantite),0) FROM retour_lignes l JOIN retours r ON r.id=l.retour_id WHERE r.bl_id=? AND r.statut='Valide' AND l.article_id=?", (h['bl_id'], a)).fetchone()[0]
            des = db.execute('SELECT designation FROM articles WHERE id=?', (a,)).fetchone()['designation']
            if liv <= 0: msgs.append(f'{des} ne figure pas sur le BL d\'origine')
            elif qte > liv - deja + 1e-9: msgs.append(f'{des} : retour {qte:g} > livré {liv:g}' + (f' (dont {deja:g} déjà retourné)' if deja else ''))
        if msgs: raise ApiError(409, 'Quantité retournée supérieure à la quantité livrée : ' + '; '.join(msgs), confirm=True)
    n = 0
    for l in L:
        if (l['destination'] or 'Stock') == 'Stock': mvt(db, l['article_id'], 'Entree', l['quantite'], h['num']); n += 1
    db.execute("UPDATE retours SET statut='Valide' WHERE id=?", (id,))
    return {'msg': f'Retour validé : stock mis à jour ({n} ligne{"s" if n > 1 else ""} remise{"s" if n > 1 else ""} en stock)' if n else 'Retour validé : aucune ligne remise en stock (tout en rebut)'}

def retour_annuler(db, id, force):
    """Annule un retour. Un retour déjà validé est contre-passé : les pièces remises en stock en ressortent (mouvement « Sortie »)."""
    h = need(db, 'retours', id)
    if h['statut'] == 'Annule': raise ApiError(409, 'Retour déjà annulé')
    if h['statut'] == 'Brouillon':
        db.execute("UPDATE retours SET statut='Annule' WHERE id=?", (id,)); return {'msg': 'Retour annulé'}
    L = q(db, "SELECT article_id, SUM(quantite) AS qte FROM retour_lignes WHERE retour_id=? AND COALESCE(destination,'Stock')='Stock' AND article_id IS NOT NULL GROUP BY article_id", (id,))
    if not force:
        manque = []
        for l in L:
            s = db.execute('SELECT stock, designation FROM articles WHERE id=?', (l['article_id'],)).fetchone()
            if (s['stock'] or 0) < l['qte']: manque.append(f"{s['designation']} (stock {s['stock'] or 0}, à retirer {l['qte']:g})")
        if manque: raise ApiError(409, 'Stock insuffisant pour annuler ce retour : ' + '; '.join(manque), confirm=True)
    for l in L: mvt(db, l['article_id'], 'Sortie', l['qte'], h['num'] + ' (annulation)')
    db.execute("UPDATE retours SET statut='Annule' WHERE id=?", (id,))
    return {'msg': 'Retour annulé : stock rétabli'}

def bl_modifier_valide(db, id, cur, body, force):
    """Modifie un BL DÉJÀ VALIDÉ sans l'annuler : le BL garde son numéro et son statut ; le stock est corrigé de la seule différence
    (mouvements « modification ») et les BC (livré / reste / reports / statuts) sont recalculés automatiquement. Aucune donnée n'est perdue."""
    body = dict(body); body.pop('statut', None); body.pop('num', None)
    if body.get('type') and body['type'] != cur['type']: raise ApiError(409, 'Le type du BL (livraison / réception) ne peut pas être changé sur un BL validé')
    L = body['lignes']
    for l in L:
        if l.get('article_id') and (l.get('quantite') or 0) <= 0: raise ApiError(400, 'Quantité invalide sur une ligne : elle doit être supérieure à 0')
        if not l.get('article_id') and (l.get('quantite') or 0): raise ApiError(400, 'Une ligne avec une quantité doit avoir un article')
        src = l.get('bc_id') or body.get('bc_id') or cur.get('bc_id')
        if l.get('report_bc_id') and src and l.get('article_id') and not report_choix_ok(db, int(src), l['report_bc_id'], int(l['article_id'])):
            raise ApiError(400, "BC choisi pour le report invalide : il doit être un autre BC du même tiers, validé ou partiel, contenant cet article (ou un équivalent)")
    liv = cur['type'] == 'Livraison'
    def tot(rows):
        d = {}
        for r in rows:
            if r.get('article_id'): a = int(r['article_id']); d[a] = d.get(a, 0) + float(r.get('quantite') or 0)
        return d
    old = tot(q(db, 'SELECT article_id, quantite FROM bl_lignes WHERE bl_id=?', (id,))); new = tot(L)
    for r in q(db, "SELECT rl.article_id, SUM(rl.quantite) AS qte FROM retour_lignes rl JOIN retours r ON r.id=rl.retour_id WHERE r.bl_id=? AND r.statut='Valide' AND rl.article_id IS NOT NULL GROUP BY rl.article_id", (id,)):
        if new.get(r['article_id'], 0) < (r['qte'] or 0) - 1e-9:
            raise ApiError(409, f"Impossible : {_ref(db, r['article_id'])} a déjà {r['qte']:g} pièce(s) retournée(s) par retour client validé sur ce BL ; la quantité ne peut pas descendre en dessous.")
    deltas = {a: new.get(a, 0) - old.get(a, 0) for a in set(old) | set(new)}
    deltas = {a: d for a, d in deltas.items() if abs(d) > 1e-9}
    if not force:   # une sortie supplémentaire (livraison en plus / réception en moins) doit être couverte par le stock
        manque = []
        for a, d in deltas.items():
            if (d > 0) == liv:
                s_ = db.execute('SELECT stock, designation FROM articles WHERE id=?', (a,)).fetchone()
                if (s_['stock'] or 0) < abs(d): manque.append(f"{s_['designation']} (stock {s_['stock'] or 0}, à sortir {abs(d):g})")
        if manque: raise ApiError(409, 'Stock insuffisant pour cette modification : ' + '; '.join(manque), confirm=True)
    for a, d in deltas.items():
        mvt(db, a, 'Sortie' if (d > 0) == liv else 'Entree', abs(d), cur['num'] + ' (modification)')
    save(db, 'bl', body, id)
    return id

def bl_annuler(db, id, force):
    """Annule un BL. Un BL validé est contre-passé : les mouvements de stock sont inversés (livraison -> les pièces reviennent, réception -> elles ressortent)
    et les quantités livrées/reçues des BC sont recalculées automatiquement."""
    h = need(db, 'bl', id)
    if h['statut'] == 'Annule': raise ApiError(409, 'BL déjà annulé')
    if h['statut'] == 'Brouillon':
        db.execute("UPDATE bl SET statut='Annule' WHERE id=?", (id,)); return {'msg': 'BL annulé'}
    r = db.execute("SELECT num FROM retours WHERE bl_id=? AND statut='Valide' LIMIT 1", (id,)).fetchone()
    if r: raise ApiError(409, f"Impossible : le retour client {r['num']} est lié à ce BL. Annulez d'abord ce retour.")
    liv = h['type'] == 'Livraison'
    L = q(db, 'SELECT article_id, SUM(quantite) AS qte FROM bl_lignes WHERE bl_id=? AND article_id IS NOT NULL GROUP BY article_id', (id,))
    if not liv and not force:   # une réception annulée fait sortir des pièces : le stock doit suffire
        manque = []
        for l in L:
            s = db.execute('SELECT stock, designation FROM articles WHERE id=?', (l['article_id'],)).fetchone()
            if (s['stock'] or 0) < l['qte']: manque.append(f"{s['designation']} (stock {s['stock'] or 0}, à retirer {l['qte']:g})")
        if manque: raise ApiError(409, 'Stock insuffisant pour annuler cette réception : ' + '; '.join(manque), confirm=True)
    for l in L: mvt(db, l['article_id'], 'Entree' if liv else 'Sortie', l['qte'], h['num'] + ' (annulation)')
    db.execute("UPDATE bl SET statut='Annule' WHERE id=?", (id,))
    return {'msg': 'BL annulé : stock rétabli' + (' (pièces remises en stock)' if liv else ' (pièces retirées du stock)')}

def of_demarrer(db, id, force):
    o = need(db, 'ordres', id)
    if o['statut'] not in ('Planifie', 'Suspendu'): raise ApiError(409, f"OF {o['statut']} : démarrage impossible")
    db.execute("UPDATE ordres SET statut='En cours', date_debut=COALESCE(date_debut,?) WHERE id=?", (today(), id))
    return {'msg': 'OF démarré'}

def of_terminer(db, id, force):
    o = need(db, 'ordres', id)
    if o['statut'] in ('Termine', 'Annule'): raise ApiError(409, f"OF {o['statut']}")
    if not o['article_id']: raise ApiError(400, 'Choisissez le produit à fabriquer')
    qte = o['qte_fabriquee'] or o['qte_a_fabriquer'] or 0
    comps = q(db, 'SELECT n.composant_id, n.quantite, a.designation, a.stock FROM nomenclature n JOIN articles a ON a.id=n.composant_id WHERE n.article_id=?', (o['article_id'],))
    if not force:
        manque = [f"{c['designation']} (stock {c['stock'] or 0}, requis {round(c['quantite'] * qte, 3)})" for c in comps if (c['stock'] or 0) < c['quantite'] * qte]
        if manque: raise ApiError(409, 'Matières insuffisantes : ' + '; '.join(manque), confirm=True)
    for c in comps: mvt(db, c['composant_id'], 'Sortie', c['quantite'] * qte, o['num'])
    mvt(db, o['article_id'], 'Entree', qte, o['num'])
    db.execute("UPDATE ordres SET statut='Termine', qte_fabriquee=?, date_fin=COALESCE(date_fin,?) WHERE id=?", (qte, today(), id))
    db.execute("UPDATE ordre_operations SET statut='Fait' WHERE ordre_id=?", (id,))
    return {'msg': 'OF terminé : matières consommées, produit fini en stock'}

def of_annuler(db, id, force):
    o = need(db, 'ordres', id)
    if o['statut'] == 'Termine': raise ApiError(409, 'OF terminé : annulation impossible')
    db.execute("UPDATE ordres SET statut='Annule' WHERE id=?", (id,))
    return {'msg': 'OF annulé'}

ACTIONS = {('bc', 'valider'): bc_valider, ('bc', 'annuler'): bc_annuler, ('bc', 'generer_of'): bc_generer_of, ('bl', 'valider'): bl_valider, ('bl', 'annuler'): bl_annuler, ('retours', 'valider'): retour_valider, ('retours', 'annuler'): retour_annuler,
           ('ordres', 'demarrer'): of_demarrer, ('ordres', 'terminer'): of_terminer, ('ordres', 'annuler'): of_annuler}

def dashboard(db):
    m = today()[:7] + '-01'
    sc = lambda s, a=(): db.execute(s, a).fetchone()[0]
    return {
        'bc_ouverts': sc("SELECT COUNT(*) FROM bc WHERE statut IN ('Brouillon','Valide','Partiel')"),
        'bl_mois': sc("SELECT COUNT(*) FROM bl WHERE date>=?", (m,)),
        'retours_mois': sc("SELECT COUNT(*) FROM retours WHERE date>=? AND statut<>'Annule'", (m,)),
        'of_en_cours': sc("SELECT COUNT(*) FROM ordres WHERE statut='En cours'"),
        'of_planifies': sc("SELECT COUNT(*) FROM ordres WHERE statut='Planifie'"),
        'alertes': q(db, ALERT_SQL),
        'of_liste': q(db, "SELECT o.num, a.designation AS article_nom, o.qte_a_fabriquer, o.priorite, o.statut, o.date_fin FROM ordres o LEFT JOIN articles a ON a.id=o.article_id WHERE o.statut IN ('En cours','Planifie','Suspendu') ORDER BY o.statut, o.date_fin LIMIT 10"),
    }

def analytics(db, qs=None):
    """Tableau de bord analytique (lecture seule) : KPI, séries journalières, avancement BC, stock, production, activité récente, alertes."""
    import datetime as _d
    try: jours = int((qs or {}).get('jours', ['30'])[0])
    except Exception: jours = 30
    t = _d.date.fromisoformat(today()); sc = lambda s, a=(): (db.execute(s, a).fetchone()[0] or 0)
    first = sc("SELECT MIN(d) FROM (SELECT MIN(date) d FROM bl UNION SELECT MIN(date) FROM ordres UNION SELECT MIN(substr(date,1,10)) FROM mouvements)") or str(t)
    start = (t - _d.timedelta(days=jours - 1)) if jours > 0 else _d.date.fromisoformat(first[:10])
    start = max(start, t - _d.timedelta(days=364)); sd = str(start)
    days = [str(start + _d.timedelta(days=i)) for i in range((t - start).days + 1)]
    def serie(sql, a=()):
        m = {r[0]: r[1] or 0 for r in db.execute(sql, a)}; return [round(m.get(d, 0), 3) for d in days]
    liv = serie("SELECT bl.date, SUM(l.quantite) FROM bl_lignes l JOIN bl ON bl.id=l.bl_id WHERE bl.statut='Valide' AND bl.type='Livraison' AND bl.date>=? GROUP BY bl.date", (sd,))
    prod = serie("SELECT date_fin, SUM(qte_fabriquee) FROM ordres WHERE statut='Termine' AND date_fin>=? GROUP BY date_fin", (sd,))
    ret = serie("SELECT r.date, SUM(l.quantite) FROM retour_lignes l JOIN retours r ON r.id=l.retour_id WHERE r.statut='Valide' AND r.date>=? GROUP BY r.date", (sd,))
    bcs = [{k: v for k, v in b.items() if k not in ('lignes', 'remarque')} for b in analyse_bc_data(db) if b['type'] == 'Client']
    cmd_t, liv_t = sum(b['commande'] for b in bcs), sum(b['livre'] for b in bcs)
    qret = sc("SELECT SUM(l.quantite) FROM retour_lignes l JOIN retours r ON r.id=l.retour_id WHERE r.statut='Valide'"); qliv = sc("SELECT SUM(l.quantite) FROM bl_lignes l JOIN bl ON bl.id=l.bl_id WHERE bl.statut='Valide' AND bl.type='Livraison'")
    ca = sc("SELECT SUM(l.quantite*l.prix_unitaire) FROM bl_lignes l JOIN bl ON bl.id=l.bl_id WHERE bl.statut='Valide' AND bl.type='Livraison' AND bl.date>=?", (sd,))
    ofq, off = sc("SELECT SUM(qte_a_fabriquer) FROM ordres WHERE statut<>'Annule'"), sc("SELECT SUM(MIN(qte_fabriquee,qte_a_fabriquer)) FROM ordres WHERE statut<>'Annule'")
    sit = q(db, SITUATION_SQL); neg = [r for r in sit if (r['stock'] or 0) < 0]; man = sorted([r for r in sit if (r['manque'] or 0) > 0], key=lambda r: -r['manque'])
    nopx = sc("SELECT COUNT(*) FROM bc_lignes WHERE COALESCE(prix_unitaire,0)=0")
    alerts = []
    for r in neg: alerts.append({'niv': 'crit', 'titre': 'Stock négatif · ' + r['reference'], 'txt': f"Stock calculé {round(r['stock'],2)} {r['unite'] or ''} — fabriquer ou régulariser via OF."})
    for r in man[:5]:
        if (r['stock'] or 0) >= 0: alerts.append({'niv': 'warn', 'titre': 'Manque · ' + r['reference'], 'txt': f"Besoin {round(r['besoin'],2)} pour {round(r['stock'],2)} en stock : {round(r['manque'],2)} à fabriquer/recevoir."})
    for b in bcs:
        if b['retard']: alerts.append({'niv': 'crit', 'titre': 'BC en retard · ' + b['num'], 'txt': f"Livraison prévue le {b['date_livraison']} — reste {round(b['reste'],2)} à livrer."})
    sans = sum(1 for b in bcs if not b['date_livraison'] and b['statut'] != 'Solde')
    if sans: alerts.append({'niv': 'info', 'titre': f'{sans} BC ouverts sans date de livraison', 'txt': 'Renseignez la date de livraison pour activer le suivi des retards.'})
    for r in q(db, ALERT_SQL):
        if (r['stock'] or 0) >= 0: alerts.append({'niv': 'warn', 'titre': 'Sous stock mini · ' + r['reference'], 'txt': f"Stock {r['stock']} ≤ mini {r['stock_mini']}."})
    return {
        'maj': _d.datetime.now().strftime('%H:%M:%S'), 'jours': jours, 'debut': sd, 'has_prices': nopx < sc("SELECT COUNT(*) FROM bc_lignes") or ca > 0,
        'kpi': {'bc_ouverts': sum(1 for b in bcs if b['statut'] != 'Solde'), 'bc_total': len(bcs), 'taux_livraison': round(100 * liv_t / cmd_t, 1) if cmd_t else 0, 'reste_a_livrer': round(max(0, cmd_t - liv_t), 2),
                'livre_periode': round(sum(liv), 2), 'bl_valides': sc("SELECT COUNT(*) FROM bl WHERE statut='Valide' AND date>=?", (sd,)), 'bl_annules': sc("SELECT COUNT(*) FROM bl WHERE statut='Annule' AND date>=?", (sd,)),
                'of_en_cours': sc("SELECT COUNT(*) FROM ordres WHERE statut='En cours'"), 'of_planifies': sc("SELECT COUNT(*) FROM ordres WHERE statut='Planifie'"), 'taux_realisation_of': round(100 * off / ofq, 1) if ofq else 0,
                'produit_periode': round(sum(prod), 2), 'taux_retour': round(100 * qret / qliv, 2) if qliv else 0, 'retours_qte': round(qret, 2),
                'articles_stock': sum(1 for r in sit if (r['stock'] or 0) > 0), 'stock_negatif': len(neg), 'sous_mini': len(q(db, ALERT_SQL)), 'ca_periode': round(ca, 2)},
        'jours_labels': days, 'series': {'livraisons': liv, 'production': prod, 'retours': ret},
        'bc': bcs, 'bc_statuts': q(db, "SELECT statut AS k, COUNT(*) AS v FROM bc WHERE type='Client' GROUP BY statut"),
        'of_statuts': q(db, "SELECT statut AS k, COUNT(*) AS v FROM ordres GROUP BY statut"), 'retour_motifs': q(db, "SELECT COALESCE(NULLIF(motif,''),'Non précisé') AS k, COUNT(*) AS v FROM retours WHERE statut='Valide' GROUP BY 1"),
        'top_livres': q(db, "SELECT a.reference AS k, SUM(l.quantite) AS v FROM bl_lignes l JOIN bl ON bl.id=l.bl_id JOIN articles a ON a.id=l.article_id WHERE bl.statut='Valide' AND bl.type='Livraison' AND bl.date>=? GROUP BY a.id ORDER BY v DESC LIMIT 8", (sd,)),
        'stock_top': q(db, "SELECT reference AS k, stock AS v FROM articles WHERE ABS(COALESCE(stock,0))>0 ORDER BY ABS(stock) DESC LIMIT 8"),
        'activite': q(db, "SELECT m.date, a.reference, m.sens, m.quantite, m.origine FROM mouvements m JOIN articles a ON a.id=m.article_id ORDER BY m.id DESC LIMIT 10"), 'alertes': alerts[:14]}

def situation_bc(db):
    """Situation de traitement des BC clients ouverts : commandé / livré / reste / stock affecté / manque / OF.
    Les BC sont servis par priorité (date de livraison prévue, puis numéro interne) : le stock d'un article est affecté dans cet ordre."""
    lines = q(db, """SELECT bc.id AS bc_id, bc.num, bc.date_livraison, bc.statut AS bc_statut, t.raison_sociale AS client, l.article_id, a.reference AS ref, a.designation, a.unite, a.type AS art_type,
        COALESCE(a.stock,0) AS stock, SUM(l.quantite) AS commande, SUM(COALESCE(l.qte_livree,0)) AS livre, SUM(COALESCE(l.qte_report,0)) AS report
        FROM bc JOIN bc_lignes l ON l.bc_id=bc.id JOIN articles a ON a.id=l.article_id LEFT JOIN tiers t ON t.id=bc.tiers_id
        WHERE bc.type='Client' AND bc.statut IN ('Valide','Partiel') GROUP BY bc.id, l.article_id
        ORDER BY (bc.date_livraison IS NULL OR bc.date_livraison=''), bc.date_livraison, bc.id, MIN(l.id)""")
    rest, rang, out = {}, {}, []
    for r in lines:
        rang.setdefault(r['bc_id'], len(rang) + 1)
        a = r['article_id']; rest.setdefault(a, r['stock'])
        reste = max(0, (r['commande'] or 0) - (r['livre'] or 0))
        dispo = min(reste, max(0, rest[a])); rest[a] -= dispo
        manque = reste - dispo
        of = db.execute("SELECT COALESCE(SUM(MAX(0, qte_a_fabriquer-COALESCE(qte_fabriquee,0))),0) FROM ordres WHERE bc_id=? AND article_id=? AND statut IN ('Planifie','En cours','Suspendu')", (r['bc_id'], a)).fetchone()[0]
        fab = r['art_type'] in ('Produit fini', 'Semi-fini')
        a_lancer = max(0, manque - of) if fab else 0
        if reste <= 0: etat = 'Livré'
        elif manque <= 0: etat = 'Prêt à livrer'
        elif of >= manque and fab: etat = 'OF lancé'
        elif not fab: etat = 'À acheter'
        else: etat = 'Livrable en partie' if dispo > 0 else 'À fabriquer'
        out.append({'priorite': rang[r['bc_id']], 'bc_id': r['bc_id'], 'num': r['num'], 'date_livraison': r['date_livraison'], 'client': r['client'], 'article_id': a, 'ref': r['ref'], 'designation': r['designation'],
                    'unite': r['unite'], 'commande': r['commande'], 'livre': r['livre'], 'report': r['report'], 'reste': reste, 'avancement': round(100 * (r['livre'] or 0) / r['commande']) if r['commande'] else 0,
                    'stock': r['stock'], 'dispo': dispo, 'manque': manque, 'of_lance': of, 'a_lancer': a_lancer, 'etat': etat})
    for r in out:
        for k in ('commande', 'livre', 'report', 'reste', 'stock', 'dispo', 'manque', 'of_lance', 'a_lancer'): r[k] = round(r[k] or 0, 3)
    return out

def priorite_fabrication(db):
    """Produits restant à fabriquer, triés par % livré du BC décroissant."""
    rows = situation_bc(db)
    stats = {}
    for r in rows:
        b = stats.setdefault(r['bc_id'], {'commande': 0.0, 'livre': 0.0})
        b['commande'] += float(r.get('commande') or 0)
        b['livre'] += float(r.get('livre') or 0)
    out = []
    for r in rows:
        if float(r.get('a_lancer') or 0) <= 0:
            continue
        st = stats.get(r['bc_id'], {})
        pct = min(100, round(100 * st.get('livre', 0) / st.get('commande', 1))) if st.get('commande') else 0
        out.append({**r, 'pct_livre_bc': pct})
    out.sort(key=lambda r: (-r['pct_livre_bc'], r.get('priorite') or 999, str(r.get('num') or '')))
    for i, r in enumerate(out, 1): r['rang_priorite'] = i
    return out

# ---------- Recherche produit : suivi d'un produit vers ses BC (ouverts / soldés) et ses BL (commandé, livré, reste, retours) ----------
def _bc_groupe(statut): return 'ouvert' if statut in ('Valide', 'Partiel') else ('solde' if statut == 'Solde' else 'brouillon')

def produit_suivi(db, aid):
    """Suivi complet d'un produit : BC (ouverts / soldés), BL avec quantité commandée, livrée, reste, supplément et retours, retours clients. Source unique de la page, des exports et de la liste de recherche."""
    a = one(db, 'SELECT id, reference, designation, type, unite, COALESCE(stock,0) AS stock, COALESCE(stock_mini,0) AS stock_mini FROM articles WHERE id=?', (aid,))
    if not a: return None
    t = today()
    eq = [r['x'] for r in q(db, 'SELECT b_id AS x FROM article_equiv WHERE a_id=? UNION SELECT a_id FROM article_equiv WHERE b_id=?', (aid, aid))]
    ids = [aid] + eq
    inl = ','.join(str(int(i)) for i in ids)
    # --- retours (hors annulés) : seuls les retours validés comptent dans les totaux
    rets = q(db, "SELECT r.id, r.num, r.date, r.statut, r.bl_id, r.motif, COALESCE(t.raison_sociale,'—') AS tiers, bl.num AS bl_num, SUM(l.quantite) AS qte, GROUP_CONCAT(DISTINCT l.destination) AS destination "
                 "FROM retour_lignes l JOIN retours r ON r.id=l.retour_id LEFT JOIN tiers t ON t.id=r.tiers_id LEFT JOIN bl ON bl.id=r.bl_id "
                 f"WHERE l.article_id IN ({inl}) AND r.statut<>'Annule' GROUP BY r.id ORDER BY r.date DESC, r.id DESC")
    cap = {}   # retours validés par (BL, article) : répartis sur les lignes du BL en commençant par la dernière (même règle que le calcul des BC)
    for r in q(db, f"SELECT r.bl_id, l.article_id, SUM(l.quantite) AS qte FROM retour_lignes l JOIN retours r ON r.id=l.retour_id WHERE r.statut='Valide' AND r.bl_id IS NOT NULL AND l.article_id IN ({inl}) GROUP BY r.bl_id, l.article_id"):
        cap[(r['bl_id'], r['article_id'])] = r['qte'] or 0
    # --- BL
    raw = q(db, "SELECT x.id, x.bl_id, x.article_id, x.quantite, x.commande, x.reste_avant, x.supp, x.report_vers, x.source_bc_id, x.unite, bl.num, bl.date, bl.type, bl.statut, COALESCE(tt.raison_sociale,'—') AS tiers, sb.num AS bc_num, a2.reference AS art_ref FROM ("
                + BL_LIGNES_SQL.replace('/*W*/', f"WHERE b.statut<>'Annule' AND l.article_id IN ({inl})") + ") x JOIN bl ON bl.id=x.bl_id LEFT JOIN tiers tt ON tt.id=bl.tiers_id LEFT JOIN bc sb ON sb.id=x.source_bc_id LEFT JOIN articles a2 ON a2.id=x.article_id ORDER BY bl.date, bl.id, x.id")
    bls = []
    for l in raw:
        if l['article_id'] != aid:   # ligne d'un article équivalent : retenue seulement si elle est décomptée sur ce produit dans le BC
            if not l['source_bc_id']: continue
            if not db.execute('SELECT 1 FROM bc_lignes WHERE bc_id=? AND article_id=?', (l['source_bc_id'], aid)).fetchone(): continue
            if db.execute('SELECT 1 FROM bc_lignes WHERE bc_id=? AND article_id=?', (l['source_bc_id'], l['article_id'])).fetchone(): continue
        qte = l['quantite'] or 0
        bls.append({'id': l['id'], 'bl_id': l['bl_id'], 'num': l['num'], 'date': l['date'], 'type': l['type'], 'statut': l['statut'], 'tiers': l['tiers'], 'bc_id': l['source_bc_id'], 'bc_num': l['bc_num'] or '',
                    'equiv': l['art_ref'] if l['article_id'] != aid else '', 'article_id': l['article_id'], 'unite': l['unite'] or a['unite'], 'commande': l['commande'] or 0, 'reste_avant': l['reste_avant'] or 0,
                    'livre': qte, 'supp': l['supp'] or 0, 'reste_apres': max(0, (l['reste_avant'] or 0) - (qte - (l['supp'] or 0))), 'report_vers': l['report_vers'] or '', 'retourne': 0})
    for l in reversed(bls):
        if l['statut'] != 'Valide': continue
        k = (l['bl_id'], l['article_id']); take = min(l['livre'], cap.get(k, 0))
        if take > 1e-9: l['retourne'] = take; cap[k] -= take
    for l in bls: l['net'] = l['livre'] - l['retourne'] if l['statut'] == 'Valide' else 0
    bls.reverse()   # plus récents d'abord
    # --- BC
    bcs = []
    for b in q(db, "SELECT bc.id, bc.num, bc.date, bc.date_livraison, bc.statut, bc.type, COALESCE(t.raison_sociale,'—') AS tiers, SUM(l.quantite) AS commande, SUM(COALESCE(l.qte_livree,0)) AS livre, SUM(COALESCE(l.qte_report,0)) AS report "
                 "FROM bc_lignes l JOIN bc ON bc.id=l.bc_id LEFT JOIN tiers t ON t.id=bc.tiers_id WHERE l.article_id=? AND bc.statut<>'Annule' GROUP BY bc.id ORDER BY bc.date DESC, bc.id DESC", (aid,)):
        cmd, lv = b['commande'] or 0, b['livre'] or 0
        ret = sum(x['retourne'] for x in bls if x['bc_id'] == b['id'])
        bcs.append({**b, 'groupe': _bc_groupe(b['statut']), 'commande': cmd, 'livre': lv, 'report': b['report'] or 0, 'reste': max(0, cmd - lv), 'retourne': ret,
                    'pct': round(100 * min(lv, cmd) / cmd, 1) if cmd else 0, 'etat': 'Soldé' if cmd and lv >= cmd else ('Partiel' if lv > 0 else 'Non livré'), 'nb_bl': len({x['bl_id'] for x in bls if x['bc_id'] == b['id'] and x['statut'] == 'Valide'}),
                    'retard': bool(b['date_livraison'] and b['date_livraison'] < t and b['statut'] in ('Valide', 'Partiel'))})
    ouv = [b for b in bcs if b['groupe'] == 'ouvert']; sol = [b for b in bcs if b['groupe'] == 'solde']
    bv = [x for x in bls if x['statut'] == 'Valide']
    ret_total = sum(r['qte'] or 0 for r in rets if r['statut'] == 'Valide')
    r3 = lambda v: round(v or 0, 3)
    synth = {'nb_bc_ouverts': len(ouv), 'nb_bc_soldes': len(sol), 'nb_bc_brouillon': sum(1 for b in bcs if b['groupe'] == 'brouillon'),
             'commande_ouverts': r3(sum(b['commande'] for b in ouv)), 'livre_ouverts': r3(sum(b['livre'] for b in ouv)), 'reste_ouverts': r3(sum(b['reste'] for b in ouv)),
             'commande_soldes': r3(sum(b['commande'] for b in sol)), 'livre_soldes': r3(sum(b['livre'] for b in sol)),
             'nb_bl': len({x['bl_id'] for x in bv}), 'livre_bl': r3(sum(x['livre'] for x in bv)), 'retourne': r3(ret_total), 'net_livre': r3(sum(x['livre'] for x in bv) - ret_total),
             'nb_retours': sum(1 for r in rets if r['statut'] == 'Valide'), 'stock': r3(a['stock'])}
    for b in bcs: b.update(commande=r3(b['commande']), livre=r3(b['livre']), report=r3(b['report']), reste=r3(b['reste']), retourne=r3(b['retourne']))
    for x in bls:
        for k in ('commande', 'reste_avant', 'livre', 'supp', 'reste_apres', 'retourne', 'net'): x[k] = r3(x[k])
    for r in rets: r['qte'] = r3(r['qte'])
    return {'article': a, 'equivalents': eq, 'synthese': synth, 'bc_ouverts': ouv, 'bc_soldes': sol, 'bc_brouillons': [b for b in bcs if b['groupe'] == 'brouillon'], 'bl': bls, 'retours': rets}

RP_FILTRES = {'tous': ('tous les produits', lambda s: True), 'ouverts': ('produits avec BC ouverts', lambda s: s['nb_bc_ouverts'] > 0), 'soldes': ('produits avec BC soldés', lambda s: s['nb_bc_soldes'] > 0),
              'bl': ('produits avec BL', lambda s: s['nb_bl'] > 0), 'retours': ('produits avec retours', lambda s: s['retourne'] > 0)}

def recherche_produit(db, qs=None, term=None, filtre=None):
    """Moteur de recherche produit : un ou plusieurs mots (référence / désignation), tous obligatoires. Renvoie une ligne de synthèse par produit."""
    qs = qs or {}
    term = (term if term is not None else (qs.get('q') or [''])[0]).strip().lower()
    filtre = filtre or (qs.get('filtre') or ['tous'])[0]
    if filtre not in RP_FILTRES: filtre = 'tous'
    words = term.split(); out = []
    for a in q(db, 'SELECT id, reference, designation, type, unite FROM articles ORDER BY reference'):
        hay = f"{a['reference']} {a['designation']}".lower()
        if not all(w in hay for w in words): continue
        s = produit_suivi(db, a['id'])['synthese']
        if RP_FILTRES[filtre][1](s): out.append({**a, **s})
    return out[:500]

def produit_blocks(d):
    """Blocs d'export (BC ouverts, BC soldés, BL, retours) sur une même grille de colonnes."""
    blocks = []
    def bcrow(b): return [b['num'], b['date'], b['tiers'], b['num'], b['commande'], b['livre'], b['reste'], '', b['retourne'], round(b['livre'] - b['retourne'], 3), f"BC {b['statut']} · produit {b['etat']} · échéance {b['date_livraison'] or 'non définie'}" + (' · EN RETARD' if b['retard'] else '')]
    def tot(rows, lab, sans=()):
        n = lambda i: '' if i in sans else round(sum((r[i] or 0) for r in rows if isinstance(r[i], (int, float))), 3)
        return [lab, '', '', '', n(4), n(5), n(6), n(7), n(8), n(9), '']
    for key, lab in (('bc_ouverts', 'BC OUVERTS'), ('bc_soldes', 'BC SOLDÉS'), ('bc_brouillons', 'BC BROUILLON')):
        if d[key]:
            rows = [bcrow(b) for b in d[key]]; blocks.append((f"{lab} · {len(rows)} BC", rows + [tot(rows, 'TOTAL')]))
    if d['bl']:
        rows = [[x['num'], x['date'], x['tiers'], x['bc_num'], x['commande'], x['livre'], x['reste_apres'], x['supp'], x['retourne'], x['net'],
                 f"{x['statut']} · {x['type']}" + (f" · reste avant BL {x['reste_avant']:g}" if x['commande'] else '') + (f" · via équivalent {x['equiv']}" if x['equiv'] else '') + (f" · {x['report_vers']}" if x['report_vers'] else '')] for x in d['bl']]
        blocks.append((f"BONS DE LIVRAISON / RÉCEPTION · {len(rows)} lignes (totaux = BL validés)", rows + [tot([r for r, x in zip(rows, d['bl']) if x['statut'] == 'Valide'], 'TOTAL VALIDÉS', sans=(4, 6))]))
    if d['retours']:
        rows = [[r['num'], r['date'], r['tiers'], '', '', '', '', '', r['qte'], '', f"{r['statut']} · BL {r['bl_num'] or 'sans BL'} · {r['destination'] or ''}" + (f" · {r['motif']}" if r['motif'] else '')] for r in d['retours']]
        blocks.append((f"RETOURS CLIENTS · {len(rows)}", rows))
    return blocks

RP_HEADS = ['N° document', 'Date', 'Tiers', 'N° BC', 'Qté commandée', 'Qté livrée / reçue', 'Reste à livrer', 'Qté supplémentaire', 'Qté retournée', 'Net (livré - retours)', 'Statut / observation']
RP_COLS = [('reference', 'Référence'), ('designation', 'Désignation'), ('unite', 'Unité'), ('stock', 'Stock'), ('nb_bc_ouverts', 'BC ouverts'), ('commande_ouverts', 'Commandé (ouverts)'), ('livre_ouverts', 'Livré (ouverts)'),
           ('reste_ouverts', 'Reste (ouverts)'), ('nb_bc_soldes', 'BC soldés'), ('nb_bl', 'Nb BL'), ('livre_bl', 'Livré (BL validés)'), ('retourne', 'Retourné'), ('net_livre', 'Net livré')]

def produit_export(db, key, ext, term='', filtre='tous'):
    soc = (one(db, 'SELECT raison_sociale FROM societe WHERE id=1') or {}).get('raison_sociale') or SOCIETE
    X = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    m = re.match(r'^produit_suivi-(\d+)$', key)
    if m:
        d = produit_suivi(db, int(m.group(1)))
        if not d: raise ApiError(404, 'Produit introuvable')
        a, s = d['article'], d['synthese']; title = f"Suivi produit {a['reference']} - {a['designation']}"
        blocks = produit_blocks(d)
        if not blocks: raise ApiError(404, 'Aucun BC, BL ou retour pour ce produit')
        if ext == 'xlsx': return xlsx_blocks(title, RP_HEADS, blocks), X
        info = [f"Date d’édition : {today()} · Unité : {a['unite']} · Stock actuel : {_fmt(s['stock'])}",
                f"BC ouverts : {s['nb_bc_ouverts']} (commandé {_fmt(s['commande_ouverts'])} · livré {_fmt(s['livre_ouverts'])} · reste {_fmt(s['reste_ouverts'])}) · BC soldés : {s['nb_bc_soldes']}",
                f"BL validés : {s['nb_bl']} · livré {_fmt(s['livre_bl'])} · retourné {_fmt(s['retourne'])} · net livré {_fmt(s['net_livre'])}"]
        return pdf_blocks(title, soc, RP_HEADS, blocks, info), 'application/pdf'
    data = recherche_produit(db, term=term, filtre=filtre)
    if not data: raise ApiError(404, 'Aucun produit pour cette recherche')
    title = 'Recherche produit - BC et BL' + (f' - « {term} »' if term else '')
    heads = [c[1] for c in RP_COLS]; rows = [[r.get(c[0]) for c in RP_COLS] for r in data]
    sumk = {'stock', 'nb_bc_ouverts', 'commande_ouverts', 'livre_ouverts', 'reste_ouverts', 'nb_bc_soldes', 'nb_bl', 'livre_bl', 'retourne', 'net_livre'}
    sums = [round(sum((r[i] or 0) for r in rows), 3) if c[0] in sumk and c[0] != 'stock' else None for i, c in enumerate(RP_COLS)]
    if ext == 'xlsx': return xlsx_bytes(title, heads, rows, sums), X
    info = [f"Date d’édition : {today()} · Recherche : {term or 'tous les produits'} · Filtre : {RP_FILTRES[filtre][0]}", f"{len(rows)} produit(s)"]
    return pdf_bytes(title, soc, heads, rows, sums, info, pad=4), 'application/pdf'

# ---------- Recherche produit : vue « Livré / Reste » (global par produit, ou détail par BC) ----------
LR_FILTRES = {'tous': 'tous les produits commandés', 'livres': 'produits livrés (au moins une livraison)', 'restants': 'produits restant à livrer', 'soldes': 'produits entièrement livrés'}

def livre_reste(db, term='', filtre='tous', bc_type='tous'):
    """Pour chaque produit (recherche par mots) : quantités commandées, livrées et restantes sur les BC validés / partiels / soldés (hors brouillon et annulés),
    globalement et détaillées BC par BC. Même source que produit_suivi : bc_lignes.quantite et qte_livree (recalculée depuis les BL validés)."""
    words = (term or '').strip().lower().split()
    if filtre not in LR_FILTRES: filtre = 'tous'
    t = today(); prods = {}
    sql = ("SELECT a.id AS aid, a.reference, a.designation, COALESCE(a.unite,'u') AS unite, COALESCE(a.stock,0) AS stock, bc.id AS bc_id, bc.num, bc.date, bc.date_livraison, bc.statut, bc.type, "
           "COALESCE(t.raison_sociale,'—') AS tiers, SUM(COALESCE(l.quantite,0)) AS commande, SUM(COALESCE(l.qte_livree,0)) AS livre "
           "FROM bc_lignes l JOIN bc ON bc.id=l.bc_id JOIN articles a ON a.id=l.article_id LEFT JOIN tiers t ON t.id=bc.tiers_id "
           "WHERE bc.statut IN ('Valide','Partiel','Solde') GROUP BY a.id, bc.id ORDER BY a.reference, bc.date, bc.id")
    for r in q(db, sql):
        if bc_type in ('Client', 'Fournisseur') and r['type'] != bc_type: continue
        if words and not all(w in f"{r['reference']} {r['designation']}".lower() for w in words): continue
        cmd, lv = r['commande'] or 0, r['livre'] or 0; reste = max(0, cmd - lv)
        p = prods.setdefault(r['aid'], {'id': r['aid'], 'reference': r['reference'], 'designation': r['designation'], 'unite': r['unite'], 'stock': r['stock'], 'commande': 0, 'livre': 0, 'reste': 0, 'nb_bc': 0, 'nb_bc_reste': 0, 'bcs': []})
        p['commande'] += cmd; p['livre'] += lv; p['reste'] += reste; p['nb_bc'] += 1; p['nb_bc_reste'] += 1 if reste > 1e-9 else 0
        p['bcs'].append({'id': r['bc_id'], 'num': r['num'], 'date': r['date'], 'date_livraison': r['date_livraison'] or '', 'statut': r['statut'], 'type': r['type'], 'tiers': r['tiers'],
                         'commande': round(cmd, 3), 'livre': round(lv, 3), 'reste': round(reste, 3), 'pct': round(100 * min(lv, cmd) / cmd, 1) if cmd else 0,
                         'etat': 'Soldé' if cmd and lv >= cmd - 1e-9 else ('Partiel' if lv > 0 else 'Non livré'),
                         'retard': bool(r['date_livraison'] and r['date_livraison'] < t and reste > 1e-9)})
    out = []
    for p in prods.values():
        for k in ('commande', 'livre', 'reste', 'stock'): p[k] = round(p[k], 3)
        p['pct'] = round(100 * min(p['livre'], p['commande']) / p['commande'], 1) if p['commande'] else 0
        if filtre == 'livres' and p['livre'] <= 1e-9: continue
        if filtre == 'restants' and p['reste'] <= 1e-9: continue
        if filtre == 'soldes' and p['reste'] > 1e-9: continue
        if filtre == 'restants': p['bcs'] = [b for b in p['bcs'] if b['reste'] > 1e-9]
        elif filtre == 'livres': p['bcs'] = [b for b in p['bcs'] if b['livre'] > 1e-9]
        elif filtre == 'soldes': p['bcs'] = [b for b in p['bcs'] if b['reste'] <= 1e-9]
        out.append(p)
    out.sort(key=lambda p: p['reference'])
    return out[:500]

LR_HEADS_G = ['Référence', 'Désignation', 'Unité', 'Nb BC', 'BC avec reste', 'Qté commandée', 'Qté livrée / reçue', 'Reste à livrer', 'Avancement %', 'Stock']
LR_HEADS_D = ['Référence', 'Désignation', 'N° BC', 'Type', 'Tiers', 'Date BC', 'Échéance', 'Qté commandée', 'Qté livrée / reçue', 'Reste à livrer', 'Avancement %', 'État produit', 'Statut BC']

def livre_reste_export(db, ext, term='', filtre='tous', vue='global', bc_type='tous'):
    soc = (one(db, 'SELECT raison_sociale FROM societe WHERE id=1') or {}).get('raison_sociale') or SOCIETE
    X = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    data = livre_reste(db, term, filtre, bc_type)
    if not data: raise ApiError(404, 'Aucun produit pour cette recherche')
    sfx = f" - « {term} »" if term else ''
    info = [f"Date d’édition : {today()} · Recherche : {term or 'tous les produits'} · Filtre : {LR_FILTRES[filtre]}" + (f" · BC {bc_type.lower()}s" if bc_type in ('Client', 'Fournisseur') else ''), f"{len(data)} produit(s)"]
    if vue == 'detail':
        title = 'Livré et reste par produit - détail par BC' + sfx; blocks = []
        for p in data:
            rows = [[p['reference'], p['designation'], b['num'], b['type'], b['tiers'], b['date'], b['date_livraison'] or '—', b['commande'], b['livre'], b['reste'], b['pct'], b['etat'] + (' · EN RETARD' if b['retard'] else ''), b['statut']] for b in p['bcs']]
            tot = ['TOTAL ' + p['reference'], '', '', '', '', '', '', round(sum(b['commande'] for b in p['bcs']), 3), round(sum(b['livre'] for b in p['bcs']), 3), round(sum(b['reste'] for b in p['bcs']), 3), '', '', '']
            blocks.append((f"{p['reference']} · {p['designation']} · {len(rows)} BC", rows + [tot]))
        if ext == 'xlsx': return xlsx_blocks(title, LR_HEADS_D, blocks), X
        return pdf_blocks(title, soc, LR_HEADS_D, blocks, info), 'application/pdf'
    title = 'Livré et reste par produit - global' + sfx
    rows = [[p['reference'], p['designation'], p['unite'], p['nb_bc'], p['nb_bc_reste'], p['commande'], p['livre'], p['reste'], p['pct'], p['stock']] for p in data]
    sums = [None, None, None, sum(r[3] for r in rows), sum(r[4] for r in rows), round(sum(r[5] for r in rows), 3), round(sum(r[6] for r in rows), 3), round(sum(r[7] for r in rows), 3), None, None]
    if ext == 'xlsx': return xlsx_bytes(title, LR_HEADS_G, rows, sums), X
    return pdf_bytes(title, soc, LR_HEADS_G, rows, sums, info, pad=4), 'application/pdf'

def lookups(db):
    return {'tiers': [{'id': r['id'], 'label': r['raison_sociale'], 'type': r['type']} for r in db.execute('SELECT * FROM tiers ORDER BY raison_sociale')],
            'articles': [{**dict(r), 'label': f"{r['reference']} - {r['designation']}"} for r in db.execute('SELECT * FROM articles ORDER BY reference')],
            'bc': [{'id': r['id'], 'label': r['num']} for r in db.execute('SELECT id,num FROM bc ORDER BY id DESC')],
            'bl': [{'id': r['id'], 'label': r['num'] + (' - ' + r['client'] if r['client'] else '')} for r in db.execute("SELECT bl.id, bl.num, t.raison_sociale AS client FROM bl LEFT JOIN tiers t ON t.id=bl.tiers_id WHERE bl.type='Livraison' AND bl.statut='Valide' ORDER BY bl.id DESC")]}

def creer_sauvegarde(raison='automatique'):
    """Crée une archive cohérente SQLite + fichiers, puis conserve les 30 dernières."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    dest = os.path.join(BACKUP_DIR, f'ERP_{stamp}_{raison}.zip')
    fd, tmpdb = tempfile.mkstemp(suffix='.db'); os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(tmpdb)
        try: src.backup(dst)
        finally: dst.close(); src.close()
        with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
            z.write(tmpdb, 'atelier.db')
            if os.path.isdir(FILES):
                for root, _, names in os.walk(FILES):
                    for name in names:
                        full = os.path.join(root, name)
                        z.write(full, os.path.join('fichiers', os.path.relpath(full, FILES)))
            z.writestr('INFO.txt', f'ERP Atelier\nDate: {stamp}\nType: {raison}\nContenu: base SQLite + fichiers joints.\n')
        archives = sorted((os.path.join(BACKUP_DIR, n) for n in os.listdir(BACKUP_DIR) if n.endswith('.zip')), key=os.path.getmtime, reverse=True)
        for old in archives[BACKUP_KEEP:]:
            try: os.remove(old)
            except OSError: pass
        return {'nom': os.path.basename(dest), 'date': stamp, 'taille': os.path.getsize(dest)}
    finally:
        try: os.remove(tmpdb)
        except OSError: pass

def lister_sauvegardes():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    out=[]
    for n in sorted(os.listdir(BACKUP_DIR), reverse=True):
        f=os.path.join(BACKUP_DIR,n)
        if n.endswith('.zip') and os.path.isfile(f):
            out.append({'nom':n,'taille':os.path.getsize(f),'date':datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M:%S')})
    return out

def restaurer_sauvegarde(nom):
    # N'accepte que les archives générées dans le dossier de sauvegarde.
    if not nom or os.path.basename(nom)!=nom or not nom.endswith('.zip'):
        raise ApiError(400, 'Nom de sauvegarde invalide')
    archive=os.path.join(BACKUP_DIR,nom)
    if not os.path.isfile(archive): raise ApiError(404, 'Sauvegarde introuvable')
    # Toujours préserver l'état actuel avant la restauration.
    creer_sauvegarde('avant_restauration')
    with zipfile.ZipFile(archive,'r') as z:
        names=z.namelist()
        if 'atelier.db' not in names: raise ApiError(400, 'Archive invalide : base de données absente')
        with tempfile.TemporaryDirectory() as td:
            z.extract('atelier.db',td)
            test=sqlite3.connect(os.path.join(td,'atelier.db'))
            try:
                if not test.execute("PRAGMA quick_check").fetchone()[0]=='ok': raise ApiError(400,'La base de cette sauvegarde est endommagée')
            finally: test.close()
            # Fermer les connexions avant remplacement; le verrou global bloque les écritures API.
            shutil.copy2(os.path.join(td,'atelier.db'), DB_PATH)
            if os.path.isdir(FILES): shutil.rmtree(FILES)
            os.makedirs(FILES,exist_ok=True)
            for name in names:
                if name.startswith('fichiers/') and not name.endswith('/'):
                    rel=name[len('fichiers/'):]
                    if rel and not rel.startswith('/') and '..' not in rel.split('/'):
                        target=os.path.join(FILES,rel); os.makedirs(os.path.dirname(target),exist_ok=True)
                        with z.open(name) as src, open(target,'wb') as dst: shutil.copyfileobj(src,dst)
    return {'ok':True,'message':'Restauration terminée. Rechargez l’application.'}

def boucle_sauvegarde():
    # Première sauvegarde automatique au démarrage, puis toutes les 6 heures.
    while True:
        try:
            time.sleep(60)
            if not os.path.exists(os.path.join(BACKUP_DIR, '.demarrage_fait')):
                with LOCK: creer_sauvegarde('automatique')
                os.makedirs(BACKUP_DIR,exist_ok=True)
                open(os.path.join(BACKUP_DIR,'.demarrage_fait'),'w').close()
            while True:
                time.sleep(BACKUP_INTERVAL)
                with LOCK: creer_sauvegarde('automatique')
        except Exception:
            traceback.print_exc(); time.sleep(300)


# ---------- Scanner : photo (texte lu par l'OCR du téléphone) -> BC / BL / articles ----------
SCAN_KINDS = {'bc_client': ('bc', 'Client'), 'bc_fournisseur': ('bc', 'Fournisseur'), 'bl': ('bl', 'Livraison'), 'articles': ('articles', None)}
SCAN_UNITES = {'u': 'u', 'un': 'u', 'unite': 'u', 'unité': 'u', 'pc': 'u', 'pcs': 'u', 'pce': 'u', 'pces': 'u', 'piece': 'u', 'pièce': 'u', 'pieces': 'u', 'pièces': 'u',
               'kg': 'kg', 'kgs': 'kg', 'm': 'm', 'ml': 'ml', 'm2': 'm2', 'm²': 'm2', 't': 'tonne', 'tonne': 'tonne', 'tonnes': 'tonne',
               'ens': 'u', 'lot': 'u', 'bte': 'u', 'boite': 'u', 'boîte': 'u', 'bidon': 'u', 'feuille': 'u', 'barre': 'u', 'rouleau': 'u', 'sac': 'u'}
SCAN_BRUIT = re.compile(r"\b(d[ée]signation|libell[ée]|quantit[ée]|qt[ée]|qte|prix|p\.?u|total|t\.?v\.?a|montant|net|signature|cachet|arr[êe]t[ée]e?|tel|t[ée]l|fax|nif|nis|rc|date|page|client|fournisseur|adresse|bon|livraison|commande|facture|ht|ttc|timbre|remise|r[ée]f|r[ée]f[ée]rence|unit[ée]|n°)\b", re.I)
SCAN_NUM = re.compile(r'^\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?$|^\d+(?:[.,]\d+)?$')

def _scan_norm(s):
    import unicodedata
    s = unicodedata.normalize('NFD', str(s or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '', s)

def _scan_nombre(s):
    s = s.replace('\u00a0', '').replace('\u202f', '').replace(' ', '')
    if ',' in s and '.' in s: s = s.replace(',', '') if s.rfind('.') > s.rfind(',') else s.replace('.', '').replace(',', '.')
    else: s = s.replace(',', '.')
    try: return float(s)
    except ValueError: return None

def scan_rangees(boxes):
    """Regroupe les lignes de texte lues sur la photo (avec leur position) en rangées de tableau, de haut en bas puis de gauche à droite."""
    bx = [b for b in boxes if str(b.get('t', '')).strip()]
    if not bx: return []
    for b in bx: b['cy'] = (float(b.get('top', 0)) + float(b.get('bottom', 0))) / 2; b['h'] = max(1.0, float(b.get('bottom', 0)) - float(b.get('top', 0)))
    hs = sorted(b['h'] for b in bx); med = hs[len(hs) // 2]
    rows = []
    for b in sorted(bx, key=lambda b: b['cy']):
        if rows and abs(b['cy'] - rows[-1]['cy']) <= 0.6 * med:
            rows[-1]['items'].append(b); rows[-1]['cy'] = sum(i['cy'] for i in rows[-1]['items']) / len(rows[-1]['items'])
        else: rows.append({'cy': b['cy'], 'items': [b]})
    return [' '.join(str(i['t']).strip() for i in sorted(r['items'], key=lambda i: float(i.get('left', 0)))) for r in rows]

def scan_ligne(txt):
    """Une rangée de texte -> (designation, quantite, unite, prix_unitaire, reference_possible) ou None si ce n'est pas une ligne d'article."""
    txt = re.sub(r'(?<![\d,.])(\d{1,3}) (?=(?:\d{3}[.,]\d{1,2}|000)(?![\d]))', r'\1', txt.replace('|', ' '))   # « 9 000,00 » = un seul nombre
    tok = txt.split()
    if len(tok) < 2: return None
    if len(tok) > 2 and re.match(r'^\d{1,3}[.)\-]?$', tok[0]) and not SCAN_NUM.match(tok[1]): tok = tok[1:]   # numéro d'ordre
    fin = len(tok)
    while fin > 1 and (SCAN_NUM.match(tok[fin - 1]) or tok[fin - 1].lower().strip('.') in SCAN_UNITES or tok[fin - 1].lower() in ('da', 'dzd', 'dinars', '%', 'x', '×') or re.match(r'^\d+([.,]\d+)?%$', tok[fin - 1])): fin -= 1
    queue = tok[fin:]; desi = ' '.join(tok[:fin])
    nums = [_scan_nombre(x) for x in queue if SCAN_NUM.match(x)]; nums = [n for n in nums if n is not None]
    unite = next((SCAN_UNITES[x.lower().strip('.')] for x in queue if x.lower().strip('.') in SCAN_UNITES), None)
    if not nums or not desi or not re.search(r'[A-Za-zÀ-ÿ]{2}', desi): return None
    if SCAN_BRUIT.search(desi) and len(desi.split()) <= 4 and not any(re.search(r'\d', w) for w in desi.split()) and re.match(r'^\s*(total|t\.?v\.?a|montant|net|timbre|remise|arr)', desi, re.I): return None
    qte, pu, iq = nums[0], (nums[1] if len(nums) > 1 else 0), 0
    if len(nums) >= 3:   # quantité x prix = total : repère les bonnes colonnes
        for i in range(len(nums) - 2):
            for j in range(i + 1, len(nums) - 1):
                if any(abs(nums[i] * nums[j] - nums[k]) <= max(0.02, 0.01 * nums[k]) for k in range(j + 1, len(nums))): qte, pu, iq = nums[i], nums[j], i; break
            else: continue
            break
    if iq:   # les nombres placés avant la quantité font partie de la désignation (ex. « Profilé IPE 100 »)
        vus = 0
        for pos, x in enumerate(queue):
            if SCAN_NUM.match(x) and _scan_nombre(x) is not None:
                if vus == iq: desi = (desi + ' ' + ' '.join(y for y in queue[:pos] if y.lower().strip('.') not in SCAN_UNITES)).strip(); break
                vus += 1
    ref = next((w for w in desi.split() if re.match(r'^[A-Za-z]{1,5}[-/.]?[A-Za-z0-9\-]*\d[A-Za-z0-9\-]*$', w) and len(w) >= 3), '')
    return desi, qte, unite, pu, ref

def scan_entete(texte, tiers, type_tiers):
    h = {'reference': '', 'date': '', 'tiers_id': None, 'tiers_nom': ''}
    m = re.search(r'(?i)\bn\s*[°ºo]\s*[:.]?\s*([A-Z0-9][A-Z0-9/\-.]*\d[A-Z0-9/\-.]*)', texte)
    if m: h['reference'] = m.group(1).strip('.-/')
    for m in re.finditer(r'\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})\b', texte):
        j, mo, a = int(m.group(1)), int(m.group(2)), int(m.group(3)); a += 2000 if a < 100 else 0
        try: h['date'] = datetime.date(a, mo, j).isoformat(); break
        except ValueError: continue
    nt = _scan_norm(texte); best = None
    for t in tiers:
        n = _scan_norm(t['raison_sociale'])
        if len(n) >= 4 and n in nt and (not type_tiers or t['type'] == type_tiers) and (best is None or len(n) > best[0]): best = (len(n), t)
    if best: h['tiers_id'], h['tiers_nom'] = best[1]['id'], best[1]['raison_sociale']
    return h

def scan_article(db, ref, desi, arts=None):
    """Retrouve l'article existant : par référence exacte, sinon par désignation très proche."""
    import difflib
    arts = arts if arts is not None else q(db, 'SELECT id, reference, designation, unite FROM articles')
    nr = _scan_norm(ref)
    if nr:
        for a in arts:
            if _scan_norm(a['reference']) == nr: return a, 1.0
    nd = _scan_norm(desi); best, sc = None, 0.0
    for a in arts:
        s = difflib.SequenceMatcher(None, nd, _scan_norm(a['designation'])).ratio()
        if s > sc: best, sc = a, s
    return (best, sc) if best and sc >= 0.8 else (None, sc)

def scan_parse(db, body):
    kind = body.get('kind') or 'bl'
    if kind not in SCAN_KINDS: raise ApiError(400, 'Type de document inconnu')
    boxes = body.get('boxes')
    rangees = scan_rangees(boxes) if boxes else [x for x in str(body.get('texte') or '').splitlines() if x.strip()]
    if not rangees: raise ApiError(400, 'Aucun texte lu sur la photo. Reprenez-la plus près, bien éclairée et à plat.')
    texte = '\n'.join(rangees); tiers = q(db, 'SELECT id, raison_sociale, type FROM tiers'); arts = q(db, 'SELECT id, reference, designation, unite FROM articles')
    lignes, ignorees = [], []
    for r in rangees:
        x = scan_ligne(r)
        if not x: ignorees.append(r); continue
        desi, qte, unite, pu, ref = x
        a, sc = scan_article(db, ref, desi, arts)
        lignes.append({'designation': desi, 'quantite': qte, 'unite': (a['unite'] if a and not unite else unite) or 'u', 'prix_unitaire': pu, 'reference': ref if not a else a['reference'],
                       'article_id': a['id'] if a else None, 'article_label': f"{a['reference']} - {a['designation']}" if a else '', 'score': round(sc, 2)})
    return {'kind': kind, 'entete': scan_entete(texte, tiers, SCAN_KINDS[kind][1] if kind != 'bl' else 'Client'), 'lignes': lignes, 'ignorees': ignorees, 'texte': texte}

def _scan_ref_libre(db, base='ART'):
    n = (db.execute("SELECT MAX(CAST(SUBSTR(reference, LENGTH(?)+2) AS INTEGER)) FROM articles WHERE reference LIKE ?", (base, base + '-%')).fetchone()[0] or 0) + 1
    return f'{base}-{n:04d}'

def scan_save(db, body):
    """Enregistre le résultat vérifié par l'utilisateur : BC / BL en brouillon, ou fiches articles. Crée les articles et le tiers manquants si demandé."""
    kind = body.get('kind')
    if kind not in SCAN_KINDS: raise ApiError(400, 'Type de document inconnu')
    cible, typ = SCAN_KINDS[kind]; ent = body.get('entete') or {}; crees = 0; out = []
    lignes = [l for l in (body.get('lignes') or []) if not l.get('ignorer') and str(l.get('designation') or '').strip()]
    if not lignes: raise ApiError(400, 'Aucune ligne à enregistrer')
    for l in lignes:
        qt = _scan_nombre(str(l.get('quantite') or 0)) if not isinstance(l.get('quantite'), (int, float)) else l['quantite']
        pu = _scan_nombre(str(l.get('prix_unitaire') or 0)) if not isinstance(l.get('prix_unitaire'), (int, float)) else l['prix_unitaire']
        l['quantite'], l['prix_unitaire'] = qt or 0, pu or 0
        if l['quantite'] < 0: raise ApiError(400, f"Quantité négative : {l['designation']}")
        if not l.get('unite'): l['unite'] = 'u'
    for l in lignes:   # articles : existant, sinon création
        aid = int(l['article_id']) if l.get('article_id') else None
        if not aid:
            ref = (l.get('reference') or '').strip()
            ex = one(db, 'SELECT id FROM articles WHERE UPPER(reference)=UPPER(?)', (ref,)) if ref else None
            if ex: aid = ex['id']
            elif l.get('creer', True):
                aid = db.execute('INSERT INTO articles(reference,designation,type,unite,prix_achat,prix_vente,stock,stock_mini) VALUES(?,?,?,?,?,?,0,0)',
                                 (ref or _scan_ref_libre(db), l['designation'].strip(), 'Produit fini' if kind in ('bc_client', 'bl') else 'Matiere premiere', l['unite'] if l['unite'] in ('u', 'kg', 'm', 'ml', 'm2', 'tonne') else 'u',
                                  l['prix_unitaire'] if kind != 'bc_client' and kind != 'bl' else 0, l['prix_unitaire'] if kind in ('bc_client', 'bl') else 0)).lastrowid
                crees += 1
                if kind == 'articles' and l['quantite'] > 0: mvt(db, aid, 'Entree', l['quantite'], 'Stock initial (scan)')
        elif kind == 'articles': out.append(l['designation'])   # article déjà existant : rien n'est modifié
        l['article_id'] = aid
    if kind == 'articles':
        return {'ok': True, 'articles_crees': crees, 'deja_existants': len(out)}
    tid = int(ent['tiers_id']) if ent.get('tiers_id') else None
    if not tid and (ent.get('tiers_nom') or '').strip():
        tid = db.execute('INSERT INTO tiers(code,raison_sociale,type) VALUES(?,?,?)', (None, ent['tiers_nom'].strip(), 'Fournisseur' if kind == 'bc_fournisseur' else 'Client')).lastrowid
    doc = {'type': typ, 'tiers_id': tid, 'date': ent.get('date') or today(), 'reference': (ent.get('reference') or '').strip() or None, 'statut': 'Brouillon',
           'remarque': 'Saisi par scan de photo : à vérifier avant validation.',
           'lignes': [{'article_id': l['article_id'], 'designation': l['designation'].strip(), 'quantite': l['quantite'], 'unite': l['unite'], 'prix_unitaire': l['prix_unitaire']} for l in lignes]}
    id = save(db, cible, doc)
    return {'ok': True, 'id': id, 'table': cible, 'num': one(db, f'SELECT num FROM {cible} WHERE id=?', (id,))['num'], 'articles_crees': crees}

def route(db, m, p, body, qs, user):
    t = p[0]
    if t == 'scan' and m == 'POST' and len(p) == 2 and p[1] in ('parse', 'save'): return scan_parse(db, body) if p[1] == 'parse' else scan_save(db, body)
    if t == 'dashboard': return dashboard(db)
    if t == 'analytics': return analytics(db, qs)
    if t == 'analyse_bc' and len(p) == 2:
        r = analyse_bc_data(db, int(p[1]))
        if not r: raise ApiError(404, 'BC introuvable')
        return r[0]
    if t == 'lookups': return lookups(db)
    if t == 'alertes': return q(db, ALERT_SQL)
    if t == 'situation': return q(db, SITUATION_SQL)
    if t == 'situation_bc': return situation_bc(db)
    if t == 'recherche_produit': return recherche_produit(db, qs)
    if t == 'livre_reste':
        f = (qs.get('filtre') or ['tous'])[0]; ty = (qs.get('type') or ['tous'])[0]
        return livre_reste(db, (qs.get('q') or [''])[0], f if f in LR_FILTRES else 'tous', ty if ty in ('Client', 'Fournisseur') else 'tous')
    if t == 'produit_suivi' and len(p) == 2:
        r = produit_suivi(db, int(p[1]))
        if not r: raise ApiError(404, 'Produit introuvable')
        return r
    if t == 'audit': return audit(db)
    if t == 'recalculer' and m == 'POST': recalculer_stock(db); recalculer(db); return audit(db)
    if t == 'equivalences':
        if m == 'GET': return q(db, "SELECT e.id, a.reference AS ref_a, a.designation AS des_a, b.reference AS ref_b, b.designation AS des_b FROM article_equiv e JOIN articles a ON a.id=e.a_id JOIN articles b ON b.id=e.b_id ORDER BY e.id")
        if m == 'POST':
            a, b = int(body.get('a_id') or 0), int(body.get('b_id') or 0)
            if not a or not b or a == b: raise ApiError(400, 'Choisissez deux articles différents')
            a, b = min(a, b), max(a, b)
            db.execute('INSERT OR IGNORE INTO article_equiv(a_id,b_id) VALUES(?,?)', (a, b)); return {'ok': True}
        if m == 'DELETE' and len(p) == 2: db.execute('DELETE FROM article_equiv WHERE id=?', (int(p[1]),)); return {'ok': True}
        raise ApiError(405, 'Méthode non supportée')
    if t == 'bc_reste' and m == 'GET' and len(p) == 3:   # aide à la saisie d'un BL : commandé / reste à livrer d'un article sur un BC (équivalences comprises)
        rows = bc_match_lines(db, int(p[1]), int(p[2]))
        return {'commande': sum(r['quantite'] or 0 for r in rows), 'livre': sum(r['lv'] for r in rows), 'reste': sum(max(0, (r['quantite'] or 0) - r['lv']) for r in rows), 'lignes': [r['id'] for r in rows]}
    if t == 'bc_pour_article' and m == 'GET' and len(p) == 2:   # saisie d'un BL : uniquement les BC (Valide/Partiel) du bon type qui ont encore un reste à livrer pour cet article (équivalences comprises)
        typ = (qs.get('type') or ['Client'])[0]; out = []
        for c in q(db, "SELECT bc.id, bc.num, bc.date_livraison, t.raison_sociale AS client FROM bc LEFT JOIN tiers t ON t.id=bc.tiers_id WHERE bc.type=? AND bc.statut IN ('Valide','Partiel') ORDER BY COALESCE(NULLIF(bc.date_livraison,''),'9999'), bc.id", (typ,)):
            reste = sum(max(0, (r['quantite'] or 0) - r['lv']) for r in bc_match_lines(db, c['id'], int(p[1])))
            if reste > 1e-9: out.append({'id': c['id'], 'num': c['num'], 'client': c['client'], 'reste': round(reste, 3)})
        return out
    if t == 'bc_report' and m == 'GET' and len(p) == 3:   # BC pouvant recevoir le surplus d'une ligne de BL (même tiers/type, Valide/Partiel, article présent avec un reste > 0)
        src = need(db, 'bc', int(p[1])); out = []
        for c in q(db, "SELECT id, num, date_livraison FROM bc WHERE tiers_id IS ? AND type=? AND statut IN ('Valide','Partiel') AND id!=? ORDER BY COALESCE(NULLIF(date_livraison,''),'9999'), id", (src['tiers_id'], src['type'], src['id'])):
            reste = sum(max(0, (r['quantite'] or 0) - r['lv']) for r in bc_match_lines(db, c['id'], int(p[2])))
            if reste > 1e-9: out.append({'id': c['id'], 'num': c['num'], 'reste': reste, 'date_livraison': c['date_livraison']})
        return out
    if t not in TABLES: raise ApiError(404, 'Ressource inconnue')
    if t in ('users', 'societe') and user['role'] != 'admin': raise ApiError(403, "Réservé à l'administrateur")
    if t == 'societe':
        if len(p) == 1 and m == 'GET':
            return one(db, 'SELECT * FROM societe WHERE id=1') or {'id': 1, 'code': '', 'raison_sociale': SOCIETE, 'adresse': '', 'ville': '', 'tel': '', 'email': '', 'nif': '', 'nis': '', 'rc': '', 'logo': None, 'cachet': None, 'signature': None}
        if len(p) == 1 and m == 'POST':
            save(db, 'societe', {**body, 'id': 1}, 1)
            return {'id': 1}
        if len(p) == 2 and int(p[1]) == 1 and m == 'GET':
            return one(db, 'SELECT * FROM societe WHERE id=1')
        if len(p) == 2 and int(p[1]) == 1 and m == 'PUT':
            save(db, 'societe', {**body, 'id': 1}, 1)
            return {'id': 1}
        raise ApiError(404, 'Route société inconnue')
    if t == 'mouvements' and m != 'GET': raise ApiError(405, 'Les mouvements sont générés automatiquement')
    if len(p) == 1:
        if m == 'GET': return q(db, LIST_SQL.get(t, f'SELECT * FROM {t} ORDER BY id DESC'))
        if m == 'POST': return {'id': save(db, t, body)}
    else:
        id = int(p[1])
        if len(p) == 2:
            if m == 'GET': return get_one(db, t, id)
            if m == 'PUT':
                cur = need(db, t, id)
                if t in LOCKED and cur['statut'] in LOCKED[t] and t != 'bl': raise ApiError(409, f"Document {cur['statut']} : modification impossible")
                if t == 'bl' and cur['statut'] == 'Annule': raise ApiError(409, 'BL annulé : modification impossible')
                if t == 'bl' and cur['statut'] == 'Valide' and body.get('lignes') is not None:
                    return {'id': bl_modifier_valide(db, id, cur, body, 'force' in qs)}   # modification d'un BL validé : stock corrigé de la différence, BC recalculés, sans annulation
                if t == 'bl' and cur['statut'] == 'Valide':
                    body = {k_: v for k_, v in body.items() if k_ not in ('statut', 'num')}
                return {'id': save(db, t, body, id)}
            if m == 'DELETE':
                cur = need(db, t, id)
                if t in NODEL and cur['statut'] in NODEL[t]: raise ApiError(409, f"Document {cur['statut']} : suppression impossible (annulez-le si possible)")
                if t == 'users' and id == user['id']: raise ApiError(409, 'Vous ne pouvez pas supprimer votre propre compte')
                db.execute(f'DELETE FROM {t} WHERE id=?', (id,))
                if t == 'articles': rm_file(cur['photo']); rm_file(cur['fiche_pdf'])
                return {'ok': True}
        elif len(p) == 3 and m == 'POST' and (t, p[2]) in ACTIONS:
            return ACTIONS[(t, p[2])](db, id, 'force' in qs)
    raise ApiError(404, 'Route inconnue')

# ---------- Exports Excel (.xlsx) et PDF - sans dépendance externe ----------
import zipfile, io, textwrap
EXPORTS = {
 'situation_bc': ('Situation de traitement des BC', situation_bc,
                  [('priorite', 'Prio'), ('num', 'N° BC'), ('date_livraison', 'Livraison prévue'), ('client', 'Client'), ('ref', 'Réf.'), ('designation', 'Désignation'), ('unite', 'Unité'), ('commande', 'Commandé'), ('livre', 'Livré'),
                   ('report', 'Dont report'), ('reste', 'Reste à livrer'), ('stock', 'Stock'), ('dispo', 'Stock affecté'), ('manque', 'Manque'), ('of_lance', 'OF lancé'), ('a_lancer', 'À lancer (OF)'), ('etat', 'État')]),
 'bc': ('Bons de commande', LIST_SQL['bc'], [('num', 'N° BC'), ('date', 'Date'), ('type', 'Type'), ('tiers_nom', 'Tiers'), ('statut', 'Statut'), ('pct_livre', '% livré (BC)')]),
 'bl': ('Bons de livraison / réception', LIST_SQL['bl'], [('num', 'N° BL'), ('date', 'Date'), ('type', 'Type'), ('tiers_nom', 'Tiers'), ('bc_num', 'BC'), ('statut', 'Statut'), ('chauffeur', 'Chauffeur'), ('vehicule', 'Véhicule')]),
 'retours': ('Retours clients', LIST_SQL['retours'], [('num', 'N° retour'), ('date', 'Date'), ('tiers_nom', 'Client'), ('bl_num', 'BL d\'origine'), ('motif', 'Motif'), ('statut', 'Statut')]),
 'retours_detail': ('Retours clients - détail des articles', "SELECT r.num, r.date, t.raison_sociale AS tiers_nom, bl.num AS bl_num, r.motif, r.statut, a.reference AS ref, l.designation, l.quantite, l.unite, l.destination FROM retour_lignes l JOIN retours r ON r.id=l.retour_id LEFT JOIN tiers t ON t.id=r.tiers_id LEFT JOIN bl ON bl.id=r.bl_id LEFT JOIN articles a ON a.id=l.article_id ORDER BY r.id DESC, l.id",
   [('num', 'N° retour'), ('date', 'Date'), ('tiers_nom', 'Client'), ('bl_num', 'BL d\'origine'), ('motif', 'Motif'), ('statut', 'Statut'), ('ref', 'Réf. article'), ('designation', 'Désignation'), ('quantite', 'Qté retournée'), ('unite', 'Unité'), ('destination', 'Destination')]),
 'ordres': ('Ordres de production', LIST_SQL['ordres'], [('num', 'N° OF'), ('date', 'Date'), ('article_nom', 'Produit'), ('qte_a_fabriquer', 'Qté à fabriquer'), ('qte_fabriquee', 'Qté fabriquée'), ('client', 'Client'), ('priorite', 'Priorité'), ('date_fin', 'Fin prévue'), ('statut', 'Statut')]),
 'ordres_detail': ('Ordres de fabrication - détail des opérations', "SELECT o.num, o.date, a.reference AS ref, a.designation AS produit, o.qte_a_fabriquer, o.qte_fabriquee, bc.num AS bc_num, t.raison_sociale AS client, o.priorite, o.statut AS statut_of, o.responsable, o.date_debut, o.date_fin, op.ordre AS operation_n, op.operation, op.poste, op.temps_prevu, op.temps_reel, op.statut AS statut_operation, o.remarque FROM ordres o LEFT JOIN articles a ON a.id=o.article_id LEFT JOIN bc ON bc.id=o.bc_id LEFT JOIN tiers t ON t.id=bc.tiers_id LEFT JOIN ordre_operations op ON op.ordre_id=o.id ORDER BY o.id DESC, op.ordre, op.id", [('num', 'N° OF'), ('date', 'Date OF'), ('ref', 'Référence'), ('produit', 'Produit'), ('qte_a_fabriquer', 'Qté prévue'), ('qte_fabriquee', 'Qté fabriquée'), ('bc_num', 'N° BC'), ('client', 'Client'), ('priorite', 'Priorité'), ('statut_of', 'Statut OF'), ('responsable', 'Responsable'), ('date_debut', 'Début'), ('date_fin', 'Fin'), ('operation_n', 'N° opération'), ('operation', 'Opération'), ('poste', 'Poste'), ('temps_prevu', 'Temps prévu'), ('temps_reel', 'Temps réel'), ('statut_operation', 'Statut opération'), ('remarque', 'Remarques')]),
 'articles': ('Articles et produits', LIST_SQL['articles'], [('reference', 'Référence'), ('designation', 'Désignation'), ('type', 'Type'), ('unite', 'Unité'), ('prix_achat', 'Prix achat'), ('prix_vente', 'Prix vente'), ('stock', 'Stock'), ('stock_mini', 'Mini')]),
 'tiers': ('Clients et fournisseurs', LIST_SQL['tiers'], [('code', 'Code'), ('raison_sociale', 'Raison sociale'), ('type', 'Type'), ('adresse', 'Adresse'), ('ville', 'Ville'), ('tel', 'Téléphone'), ('email', 'Email'), ('nif', 'NIF'), ('nis', 'NIS'), ('rc', 'RC')]),
 'nomenclature': ('Nomenclatures', LIST_SQL['nomenclature'], [('produit', 'Produit fini'), ('composant', 'Composant'), ('quantite', 'Qté / unité')]),
 'mouvements': ('Mouvements de stock', LIST_SQL['mouvements'].replace(' LIMIT 500', ''), [('date', 'Date'), ('article_nom', 'Article'), ('sens', 'Sens'), ('quantite', 'Qté'), ('origine', 'Origine')]),
'bc_detail': ('Bons de commande - détail des articles', "SELECT bc.num, bc.date, t.raison_sociale AS tiers_nom, bc.statut, a.reference AS ref, l.designation, l.quantite, l.unite, COALESCE(l.qte_livree,0) AS livree, COALESCE(l.qte_report,0) AS report, l.quantite-COALESCE(l.qte_livree,0) AS reste FROM bc_lignes l JOIN bc ON bc.id=l.bc_id LEFT JOIN tiers t ON t.id=bc.tiers_id LEFT JOIN articles a ON a.id=l.article_id ORDER BY bc.id DESC, l.id",
   [('num', 'N° BC'), ('date', 'Date'), ('tiers_nom', 'Tiers'), ('statut', 'Statut'), ('ref', 'Réf. article'), ('designation', 'Désignation'), ('quantite', 'Qté'), ('unite', 'Unité'), ('livree', 'Qté reçue/livrée'), ('report', 'Dont report BL'), ('reste', 'Reste')]),
 'bl_detail': ('Bons de livraison - détail des articles', "SELECT bl.num, bl.date, bl.type, t.raison_sociale AS tiers_nom, source_bc.num AS bc_num, bl.statut, a.reference AS ref, l.designation, l.quantite, l.unite, l.commande, l.reste_avant, l.supp, l.report_vers FROM (" + BL_LIGNES_SQL.replace('/*W*/', '') + ") l JOIN bl ON bl.id=l.bl_id LEFT JOIN tiers t ON t.id=bl.tiers_id LEFT JOIN bc source_bc ON source_bc.id=l.source_bc_id LEFT JOIN articles a ON a.id=l.article_id ORDER BY bl.id DESC, l.id",
   [('num', 'N° BL'), ('date', 'Date'), ('type', 'Type'), ('tiers_nom', 'Tiers'), ('bc_num', 'BC'), ('statut', 'Statut'), ('ref', 'Réf. article'), ('designation', 'Désignation'), ('quantite', 'Qté livrée'), ('unite', 'Unité'), ('commande', 'Qté BC'), ('reste_avant', 'Reste BC avant BL'), ('supp', 'Qté supplémentaire'), ('report_vers', 'Reporté sur')]),
 'situation': ('Situation des pièces (stock, fabriqué, manque)', SITUATION_SQL,
           [('reference', 'Référence'), ('designation', 'Désignation'), ('type', 'Type'), ('unite', 'Unité'), ('stock', 'En stock'), ('fabrique', 'Fabriqué (fini)'), ('en_fabrication', 'En fabrication'), ('a_livrer', 'À livrer clients'), ('pour_of', 'Requis pour OF'), ('a_recevoir', 'À recevoir'), ('manque', 'Manque'), ('etat', 'État')]),
 'stock': ('État du stock', "SELECT reference, designation, type, unite, COALESCE(stock,0) AS stock, COALESCE(stock_mini,0) AS stock_mini, COALESCE(stock,0)*COALESCE(prix_achat,0) AS valeur, CASE WHEN COALESCE(stock_mini,0)>0 AND COALESCE(stock,0)<=stock_mini THEN 'Alerte' ELSE 'OK' END AS etat FROM articles ORDER BY reference",
           [('reference', 'Référence'), ('designation', 'Désignation'), ('type', 'Type'), ('unite', 'Unité'), ('stock', 'Stock'), ('stock_mini', 'Mini'), ('valeur', 'Valeur (prix achat)', 'sum'), ('etat', 'État')]),
}

def _fmt(v):
    if v is None: return ''
    if isinstance(v, float): return str(int(v)) if v == int(v) else f'{v:,.2f}'.replace(',', ' ')
    return str(v)

def xlsx_bytes(title, heads, rows, sums, rows_spec=None, widths_from=None):
    """XLSX soigné : titre, date, en-têtes contrastés, lignes alternées, filtres et volets figés."""
    from xml.sax.saxutils import escape
    clean_x = lambda s: escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(s)))
    def ref(c, r):
        s = ''; c += 1
        while c: c, m = divmod(c - 1, 26); s = chr(65 + m) + s
        return f'{s}{r}'
    def cellx(c, r, v, st=0):
        if v is None or v == '': return f'<c r="{ref(c, r)}" s="{st}"/>'
        if isinstance(v, (int, float)):
            return f'<c r="{ref(c, r)}" s="{st}"><v>{v}</v></c>'
        return f'<c r="{ref(c, r)}" s="{st}" t="inlineStr"><is><t xml:space="preserve">{clean_x(v)}</t></is></c>'
    special = rows_spec is not None
    if special:
        data_for_width = [list(heads)] + [list(r) for r in (widths_from or [])]
        wid = [min(60, max(10, max([len(_fmt(r[i])) for r in data_for_width if len(r)>i] + [len(str(heads[i]))]) + 2)) for i in range(len(heads))]
        xmlrows=[]
        for n,(cells,st) in enumerate(rows_spec,1):
            vals=list(cells)+['']*max(0,len(heads)-len(cells))
            parts=[]
            for c,v in enumerate(vals[:len(heads)]):
                style=st if st in (1,3,4,5,6,7,8) else (8 if n%2==0 and isinstance(v,(int,float)) else (7 if n%2==0 else (2 if isinstance(v,(int,float)) else 0)))
                parts.append(cellx(c,n,v,style))
            xmlrows.append(f'<row r="{n}" customHeight="1" ht="22">'+''.join(parts)+'</row>')
        sd=''.join(xmlrows); lastrow=len(rows_spec); flt=''; pane=''
    else:
        # La ligne 1 sert de bandeau titre, la 2 affiche la date, la 3 est un espacement.
        data=[list(heads)]+[list(r) for r in rows]
        wid=[min(60,max(10,max([len(_fmt(r[i])) for r in (widths_from or data) if len(r)>i]+[len(str(heads[i]))])+3)) for i in range(len(heads))]
        xmlrows=[]
        xmlrows.append(f'<row r="1" ht="30" customHeight="1">'+cellx(0,1,title,5)+''.join(cellx(c,1,'',5) for c in range(1,len(heads)))+'</row>')
        xmlrows.append(f'<row r="2" ht="20" customHeight="1">'+cellx(0,2,'Édité le '+datetime.date.today().strftime('%d/%m/%Y'),6)+'</row>')
        xmlrows.append('<row r="3" ht="8" customHeight="1"/>')
        xmlrows.append(f'<row r="4" ht="28" customHeight="1">'+''.join(cellx(c,4,v,1) for c,v in enumerate(heads))+'</row>')
        for idx,r in enumerate(rows):
            n=idx+5; xmlrows.append(f'<row r="{n}" ht="22" customHeight="1">'+''.join(cellx(c,n,v,8 if idx%2 and isinstance(v,(int,float)) else (7 if idx%2 else (2 if isinstance(v,(int,float)) else 0))) for c,v in enumerate(r))+'</row>')
        if any(x is not None for x in sums):
            n=len(rows)+5
            xmlrows.append(f'<row r="{n}" ht="24" customHeight="1">'+''.join(cellx(c,n,'TOTAL' if c==0 and v is None else v,3) for c,v in enumerate(sums))+'</row>')
        sd=''.join(xmlrows); lastrow=len(rows)+4
        flt=f'<autoFilter ref="A4:{ref(len(heads)-1,max(4,len(rows)+4))}"/>'
        pane='<pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/>'
    lastcol=ref(len(heads)-1,1)[:-1]
    sheet=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<sheetViews><sheetView workbookViewId="0">'+pane+'</sheetView></sheetViews>'
           '<cols>'+''.join(f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>' for i,w in enumerate(wid))+'</cols>'
           f'<sheetData>{sd}</sheetData>{flt}</worksheet>')
    styles=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
      '<fonts count="4"><font><sz val="10"/><name val="Calibri"/><color rgb="FF23211E"/></font>'
      '<font><b/><sz val="10"/><name val="Calibri"/><color rgb="FFFFFFFF"/></font>'
      '<font><b/><sz val="17"/><name val="Calibri"/><color rgb="FFFFFFFF"/></font>'
      '<font><i/><sz val="10"/><name val="Calibri"/><color rgb="FF7A756C"/></font></fonts>'
      '<fills count="8"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
      '<fill><patternFill patternType="solid"><fgColor rgb="FFF1EEE8"/></patternFill></fill>'
      '<fill><patternFill patternType="solid"><fgColor rgb="FFD9EAD3"/></patternFill></fill>'
      '<fill><patternFill patternType="solid"><fgColor rgb="FF1B1D1F"/></patternFill></fill>'
      '<fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/></patternFill></fill>'
      '<fill><patternFill patternType="solid"><fgColor rgb="FFF8F6F2"/></patternFill></fill>'
      '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/></patternFill></fill></fills>'
      '<borders count="2"><border><left style="thin"><color rgb="FFE2DED6"/></left><right style="thin"><color rgb="FFE2DED6"/></right><top style="thin"><color rgb="FFE2DED6"/></top><bottom style="thin"><color rgb="FFE2DED6"/></bottom><diagonal/></border>'
      '<border><left/><right/><top/><bottom style="medium"><color rgb="FF1B1D1F"/></bottom><diagonal/></border></borders>'
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
      '<cellXfs count="9">'
      '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
      '<xf numFmtId="0" fontId="1" fillId="4" borderId="0" xfId="0" applyFont="1" applyFill="1" alignment="1"><alignment vertical="center" wrapText="1"/></xf>'
      '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
      '<xf numFmtId="0" fontId="1" fillId="3" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
      '<xf numFmtId="0" fontId="1" fillId="5" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
      '<xf numFmtId="0" fontId="2" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" alignment="1"><alignment vertical="center"/></xf>'
      '<xf numFmtId="0" fontId="3" fillId="6" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
      '<xf numFmtId="0" fontId="0" fillId="6" borderId="0" xfId="0" applyFill="1"/>'
      '<xf numFmtId="4" fontId="0" fillId="6" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1"/>'
      '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    name=re.sub(r'[\[\]:*?/\\]',' ',title)[:31]
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        z.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml',f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{clean_x(name)}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml',styles); z.writestr('xl/worksheets/sheet1.xml',sheet)
    return b.getvalue()

def pdf_bytes(title, societe, heads, rows, sums, info=(), portrait=False, pad=0):
    W, H, M, FS, CW, LH = (595, 842, 30, 8, 4.4, 10) if portrait else (842, 595, 30, 8, 4.4, 10)   # A4 paysage (listes) ou portrait (bon de livraison)
    allr = [list(heads)] + rows
    right = [any(isinstance(r[i], (int, float)) for r in rows) for i in range(len(heads))]
    wt = [min(40, max(6, max(len(_fmt(r[i])) for r in allr))) + pad for i in range(len(heads))]
    cw = [(W - 2 * M) * w / sum(wt) for w in wt]
    wrap = lambda s, w: textwrap.wrap(s, max(1, int((w - 6) / CW)), break_long_words=True) or ['']
    body = [[wrap(_fmt(v), cw[i]) for i, v in enumerate(r)] for r in rows]
    if any(s is not None for s in sums): body.append([wrap(_fmt('TOTAL' if i == 0 and s is None else s), cw[i]) for i, s in enumerate(sums)])
    hdr = [wrap(h, cw[i]) for i, h in enumerate(heads)]
    hh = max(len(c) for c in hdr) * LH + 6
    t1 = H - M - 40 - 12 * len(info)
    pages, cur, y = [], [], t1 - hh
    for r in body:
        h = max(len(c) for c in r) * LH + 4
        if y - h < M + 14: pages.append(cur); cur, y = [], H - M - 14 - hh
        cur.append((y, h, r)); y -= h
    pages.append(cur)
    esc = lambda s: s.encode('cp1252', 'replace').replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)')
    def txt(x, yy, s, f='F1', sz=FS): return b'BT /' + f.encode() + b' %d Tf %.1f %.1f Td (' % (sz, x, yy) + esc(s) + b') Tj ET\n'
    streams = []
    for pn, pg in enumerate(pages, 1):
        o = b''
        top = H - M - 14
        if pn == 1:
            o += txt(M, H - M, societe, 'F2', 11) + txt(M, H - M - 16, title, 'F2', 14) + txt(W - M - 120, H - M, datetime.date.today().strftime('%d/%m/%Y'))
            top = t1
            for k, l in enumerate(info): o += txt(M, H - M - 36 - 12 * k, l)
        o += b'0.106 0.114 0.122 rg %.1f %.1f %.1f %.1f re f\n' % (M, top - hh + 3, W - 2 * M, hh) + b'1 1 1 rg\n'
        x = M
        for i, c in enumerate(hdr):
            for k, l in enumerate(c): o += txt(x + (cw[i] - 10 - len(l) * CW if right[i] else 3), top - 7 - k * LH, l, 'F2')
            x += cw[i]
        for ridx, (yy, h, r) in enumerate(pg):
            if len(body) > len(rows) and r is body[-1]:
                o += b'0.85 0.93 0.87 rg %.1f %.1f %.1f %.1f re f 0 0 0 rg\n' % (M, yy - h + 1, W - 2 * M, h)
            elif ridx % 2 == 1:
                o += b'0.97 0.965 0.95 rg %.1f %.1f %.1f %.1f re f 0 0 0 rg\n' % (M, yy - h + 1, W - 2 * M, h)
            o += b'0 0 0 rg\n'
            x = M
            for i, c in enumerate(r):
                for k, l in enumerate(c): o += txt(x + (cw[i] - 10 - len(l) * CW if right[i] else 3), yy - 8 - k * LH, l, 'F2' if r is body[-1] and len(body) > len(rows) else 'F1')
                x += cw[i]
            o += b'0.7 G %.1f %.1f m %.1f %.1f l S\n' % (M, yy - h, W - M, yy - h)
        o += txt(W / 2 - 25, M - 8, f'Page {pn} / {len(pages)}')
        streams.append(o)
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>']
    kids = []
    for i, s in enumerate(streams):
        pid = 5 + 2 * i; kids.append(b'%d 0 R' % pid)
        objs.append(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>' % (W, H, pid + 1))
        objs.append(b'<< /Length %d >>\nstream\n' % len(s) + s + b'endstream')
    objs[1] = b'<< /Type /Pages /Kids [' + b' '.join(kids) + b'] /Count %d >>' % len(kids)
    out, offs = b'%PDF-1.4\n', []
    for n, ob in enumerate(objs, 1): offs.append(len(out)); out += b'%d 0 obj\n' % n + ob + b'\nendobj\n'
    xr = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1) + b''.join(b'%010d 00000 n \n' % o for o in offs)
    return out + b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF' % (len(objs) + 1, xr)

# ---------- Situation des BC : un tableau par BC (Excel et PDF) ----------
SITBC_COLS = [('ref', 'Réf.'), ('designation', 'Désignation'), ('unite', 'Unité'), ('commande', 'Commandé'), ('livre', 'Livré'), ('report', 'Dont report'), ('reste', 'Reste à livrer'),
              ('stock', 'Stock'), ('dispo', 'Stock affecté'), ('manque', 'Manque'), ('of_lance', 'OF lancé'), ('a_lancer', 'À lancer (OF)'), ('etat', 'État')]

def sitbc_blocks(db):
    """Regroupe la situation par BC : [(titre du tableau, lignes)] dans l'ordre de priorité."""
    blocks, last = [], None
    for r in situation_bc(db):
        if r['bc_id'] != last:
            last = r['bc_id']
            titre = f"{r['num']}  |  Client : {r['client'] or '-'}  |  Livraison prévue : {r['date_livraison'] or 'non définie'}  |  Priorité {r['priorite']}"
            blocks.append([titre, []])
        blocks[-1][1].append([r.get(c[0]) for c in SITBC_COLS])
    for b in blocks: b[0] += f"  |  {len(b[1])} article{'s' if len(b[1]) > 1 else ''}"
    return [(t, rows) for t, rows in blocks]

def xlsx_blocks(title, heads, blocks):
    """Un seul onglet, chaque BC dans son propre tableau (titre, en-têtes, lignes), séparés par une ligne vide."""
    rows_spec = [([title + ' - ' + datetime.date.today().strftime('%d/%m/%Y')], 5), ([], 0)]
    for bt, rows in blocks:
        rows_spec.append(([bt] + [''] * (len(heads) - 1), 4))
        rows_spec.append((list(heads), 1))
        for r in rows: rows_spec.append((r, 0))
        rows_spec.append(([], 0))
    return xlsx_bytes(title, heads, [], [None] * len(heads), rows_spec=rows_spec, widths_from=[list(heads)] + [r for _, rows in blocks for r in rows])

def pdf_blocks(title, societe, heads, blocks, info=()):
    W, H, M, FS, CW, LH = 842, 595, 30, 8, 4.4, 10          # A4 paysage
    allr = [list(heads)] + [r for _, rows in blocks for r in rows]
    right = [any(isinstance(r[i], (int, float)) for r in allr[1:]) for i in range(len(heads))]
    wt = [min(40, max(6, max(len(_fmt(r[i])) for r in allr[1:]), max(len(w) for w in heads[i].split()) + 2)) + 3 for i in range(len(heads))]
    cw = [(W - 2 * M) * w / sum(wt) for w in wt]
    wrap = lambda s, w: textwrap.wrap(s, max(1, int((w - 6) / CW)), break_long_words=True) or ['']
    hdr = [wrap(h, cw[i]) for i, h in enumerate(heads)]
    hh = max(len(c) for c in hdr) * LH + 6
    TH, GAP, BOT = 15, 12, M + 14
    t1 = H - M - 40 - 12 * len(info)
    pages, st = [], {'cur': [], 'y': t1}
    def newpage(): pages.append(st['cur']); st['cur'] = []; st['y'] = H - M - 14
    def head(bt):
        st['cur'].append(('t', st['y'], bt)); st['y'] -= TH
        st['cur'].append(('h', st['y'])); st['y'] -= hh
    for bt, rows in blocks:
        body = [[wrap(_fmt(v), cw[i]) for i, v in enumerate(r)] for r in rows]
        h0 = (max(len(c) for c in body[0]) * LH + 4) if body else 0
        if st['y'] - (TH + hh + h0) < BOT: newpage()
        head(bt)
        for r in body:
            h = max(len(c) for c in r) * LH + 4
            if st['y'] - h < BOT:
                newpage(); head(bt + '  (suite)')
            st['cur'].append(('r', st['y'], h, r)); st['y'] -= h
        st['y'] -= GAP
    pages.append(st['cur'])
    esc = lambda s: s.encode('cp1252', 'replace').replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)')
    def txt(x, yy, s, f='F1', sz=FS): return b'BT /' + f.encode() + b' %d Tf %.1f %.1f Td (' % (sz, x, yy) + esc(s) + b') Tj ET\n'
    streams = []
    for pn, pg in enumerate(pages, 1):
        o = b''
        if pn == 1:
            o += txt(M, H - M, societe, 'F2', 11) + txt(M, H - M - 16, title, 'F2', 14) + txt(W - M - 120, H - M, datetime.date.today().strftime('%d/%m/%Y'))
            for k, l in enumerate(info): o += txt(M, H - M - 36 - 12 * k, l)
        for it in pg:
            if it[0] == 't':
                o += b'0.106 0.114 0.122 rg %.1f %.1f %.1f %.1f re f\n' % (M, it[1] - TH + 2, W - 2 * M, TH)
                o += b'1 1 1 rg\n' + txt(M + 3, it[1] - 10, it[2], 'F2', 9) + b'0 0 0 rg\n'
            elif it[0] == 'h':
                o += b'0.059 0.463 0.431 rg %.1f %.1f %.1f %.1f re f\n' % (M, it[1] - hh + 3, W - 2 * M, hh)
                o += b'1 1 1 rg\n'
                x = M
                for i, c in enumerate(hdr):
                    for k, l in enumerate(c): o += txt(x + (cw[i] - 10 - len(l) * CW if right[i] else 3), it[1] - 7 - k * LH, l, 'F2')
                    x += cw[i]
            else:
                _, yy, h, r = it
                o += b'0.97 0.965 0.95 rg %.1f %.1f %.1f %.1f re f 0 0 0 rg\n' % (M, yy - h + 1, W - 2 * M, h)
                x = M
                for i, c in enumerate(r):
                    for k, l in enumerate(c): o += txt(x + (cw[i] - 10 - len(l) * CW if right[i] else 3), yy - 8 - k * LH, l)
                    x += cw[i]
                o += b'0.7 G %.1f %.1f m %.1f %.1f l S\n' % (M, yy - h, W - M, yy - h)
        o += txt(W / 2 - 25, M - 8, f'Page {pn} / {len(pages)}')
        streams.append(o)
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>']
    kids = []
    for i, s_ in enumerate(streams):
        pid = 5 + 2 * i; kids.append(b'%d 0 R' % pid)
        objs.append(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>' % (W, H, pid + 1))
        objs.append(b'<< /Length %d >>\nstream\n' % len(s_) + s_ + b'endstream')
    objs[1] = b'<< /Type /Pages /Kids [' + b' '.join(kids) + b'] /Count %d >>' % len(kids)
    out, offs = b'%PDF-1.4\n', []
    for n, ob in enumerate(objs, 1): offs.append(len(out)); out += b'%d 0 obj\n' % n + ob + b'\nendobj\n'
    xr = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1) + b''.join(b'%010d 00000 n \n' % o for o in offs)
    return out + b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF' % (len(objs) + 1, xr)

def doc_pdf(db, k, id):
    d = get_one(db, k, id)
    soc = one(db, 'SELECT * FROM societe WHERE id=1') or {}
    t = d.get('tiers') or {}
    L = d['lignes']
    for l in L:
        a = one(db, 'SELECT reference, unite FROM articles WHERE id=?', (l['article_id'],)) if l['article_id'] else None
        l['ref'] = a['reference'] if a else ''
        l['u'] = l.get('unite') or (a['unite'] if a else None) or 'u'
        l['reste'] = (l['quantite'] or 0) - (l.get('qte_livree') or 0)
    if k == 'bc':
        heads = ['N°', 'Réf. article', 'Désignation', 'Qté', 'Unité', 'Livrée/reçue', 'Dont report', 'Reste']
        rows = [[i, l['ref'], l['designation'], l['quantite'], l['u'], l.get('qte_livree') or 0, l.get('qte_report') or 0, l['reste']] for i, l in enumerate(L, 1)]
        title = 'BON DE COMMANDE ' + (d['num'] or '')
    elif k == 'retours':
        heads = ['N°', 'Réf. article', 'Désignation', 'Qté retournée', 'Unité', 'Destination']
        rows = [[i, l['ref'], l['designation'], l['quantite'], l['u'], l.get('destination') or 'Stock'] for i, l in enumerate(L, 1)]
        title = 'BON DE RETOUR CLIENT ' + (d['num'] or '')
    else:
        if k == 'bl':
            heads = ['N°', 'N° BC', 'Réf. article', 'Désignation', 'Qté livrée', 'Unité', 'Qté BC', 'Reste avant', 'Qté suppl.', 'Reporté sur']
            rows = [[i, l.get('bc_num') or '', l['ref'], l['designation'], l['quantite'], l['u'], l.get('commande') or 0, l.get('reste_avant') or 0, l.get('supp') or 0, l.get('report_vers') or ''] for i, l in enumerate(L, 1)]
        else:
            heads = ['N°', 'Réf. article', 'Désignation', 'Qté', 'Unité']
            rows = [[i, l['ref'], l['designation'], l['quantite'], l['u']] for i, l in enumerate(L, 1)]
        title = ('BON DE RECEPTION ' if d.get('type') != 'Livraison' else 'BON DE LIVRAISON ') + (d['num'] or '')
    bcn = (one(db, 'SELECT num FROM bc WHERE id=?', (d['bc_id'],)) or {}).get('num') if k == 'bl' and d.get('bc_id') else None
    if k == 'retours':
        bln = (one(db, 'SELECT num FROM bl WHERE id=?', (d['bl_id'],)) or {}).get('num') if d.get('bl_id') else None
        info = [f"Date : {d.get('date') or ''}    Référence : {d.get('reference') or '—'}    Statut : {d.get('statut') or ''}" + (f"    BL d'origine : {bln}" if bln else ''),
                f"Client : {t.get('raison_sociale') or ''}" + (f" - {t.get('adresse') or ''} {t.get('ville') or ''}" if t.get('adresse') or t.get('ville') else ''), f"Motif du retour : {d.get('motif') or '—'}"]
        if d.get('remarque'): info.append('Remarque : ' + ' / '.join(str(d['remarque']).splitlines())[:150])
        return pdf_bytes(title, soc.get('raison_sociale') or SOCIETE, heads, rows, [None] * len(heads), info, portrait=True), 'application/pdf'
    info = [f"Date : {d.get('date') or ''}    Référence : {d.get('reference') or '—'}    Statut : {d.get('statut') or ''}" + (f"    N° BC sélectionnés : {', '.join(d.get('bc_nums') or [])}" if k == 'bl' and d.get('bc_nums') else f"    BC : {bcn}" if bcn else ''),
            f"{d.get('type') or ''} : {t.get('raison_sociale') or ''}" + (f" - {t.get('adresse') or ''} {t.get('ville') or ''}" if t.get('adresse') or t.get('ville') else '')]
    if k == 'bl': info.append(f"Chauffeur : {d.get('chauffeur') or ''}    Véhicule : {d.get('vehicule') or ''}")
    if d.get('remarque'): info.append('Remarque : ' + ' / '.join(str(d['remarque']).splitlines())[:150])
    return pdf_bytes(title, soc.get('raison_sociale') or SOCIETE, heads, rows, [None] * len(heads), info, portrait=(k != 'bl')), 'application/pdf'


def analyse_bc_data(db, only=None):
    """Analyse de chaque BC (hors annulés) : synthèse + lignes détaillées. Source unique du tableau analytique, des exports Excel/PDF et du dashboard."""
    import datetime as _d
    t = today(); out = []
    for b in q(db, "SELECT bc.*, COALESCE(t.raison_sociale,'—') AS client FROM bc LEFT JOIN tiers t ON t.id=bc.tiers_id WHERE bc.statut<>'Annule'" + (" AND bc.id=?" if only else "") + " ORDER BY bc.id", (only,) if only else ()):
        L = q(db, "SELECT l.*, COALESCE(a.reference,'') AS ref, COALESCE(a.stock,0) AS stock FROM bc_lignes l LEFT JOIN articles a ON a.id=l.article_id WHERE l.bc_id=? ORDER BY l.id", (b['id'],))
        for l in L:
            cmd = l['quantite'] or 0; lv = min(cmd, l['qte_livree'] or 0)
            l.update(commande=cmd, livre=lv, report=l['qte_report'] or 0, reste=max(0, cmd - lv), pct=round(100 * lv / cmd, 1) if cmd else 0, etat='Soldé' if cmd and lv >= cmd else ('Partiel' if lv > 0 else 'Non livré'),
                     dernier_bl=(one(db, "SELECT MAX(bl.date) AS d FROM bl_lignes x JOIN bl ON bl.id=x.bl_id WHERE bl.statut='Valide' AND x.bc_id=? AND x.article_id=?", (b['id'], l['article_id'])) or {}).get('d') or '')
        cmd, lv = sum(l['commande'] for l in L), sum(l['livre'] for l in L)
        mu = re.search(r'unit[ée]\s*:\s*([A-Z][a-z]+)', b['remarque'] or ''); dl = b['date_livraison'] or ''
        bls = q(db, "SELECT DISTINCT bl.num, bl.date FROM bl_lignes x JOIN bl ON bl.id=x.bl_id WHERE bl.statut='Valide' AND x.bc_id=? ORDER BY bl.date", (b['id'],))
        retard = bool(dl and dl < t and b['statut'] != 'Solde')
        try: age = (_d.date.fromisoformat(t) - _d.date.fromisoformat((b['date'] or t)[:10])).days
        except Exception: age = 0
        out.append({'id': b['id'], 'num': b['num'], 'date': b['date'], 'type': b['type'], 'client': b['client'], 'statut': b['statut'], 'date_livraison': dl, 'unite': mu.group(1) if mu else 'Autre', 'age': age,
                    'nb_lignes': len(L), 'commande': cmd, 'livre': lv, 'reste': max(0, cmd - lv), 'pct': round(100 * lv / cmd, 1) if cmd else 0, 'nb_bl': len(bls), 'dernier_bl': bls[-1]['date'] if bls else '',
                    'montant': round(sum(l['commande'] * (l['prix_unitaire'] or 0) for l in L), 2), 'tva': b['tva'] or 0, 'retard': retard,
                    'alerte': 'En retard' if retard else ('Soldé' if b['statut'] == 'Solde' else ('Sans date' if not dl else 'Dans les délais')), 'lignes': L, 'remarque': b['remarque'] or ''})
    return out

ANA_FILTRES = {'tous': ('tous les BC', lambda b: True), 'ouverts': ('BC ouverts', lambda b: b['statut'] != 'Solde'), 'soldes': ('BC soldés', lambda b: b['statut'] == 'Solde'), 'retard': ('BC en retard', lambda b: b['retard'])}
ANA_BC_COLS = [('num', 'N° BC'), ('date', 'Date'), ('client', 'Client'), ('unite', 'Unité'), ('statut', 'Statut'), ('date_livraison', 'Échéance'), ('age', 'Âge (j)'), ('nb_lignes', 'Lignes', 1), ('commande', 'Commandé', 1), ('livre', 'Livré', 1),
               ('reste', 'Reste', 1), ('pct', '% livré'), ('nb_bl', 'Nb BL'), ('dernier_bl', 'Dern. BL'), ('montant', 'Montant', 1), ('alerte', 'Suivi')]
ANA_L_COLS = [('ref', 'Réf.'), ('designation', 'Désignation'), ('unite', 'Unité'), ('commande', 'Commandé'), ('livre', 'Livré'), ('report', 'Dont report'), ('reste', 'Reste'), ('pct', '% livré'), ('stock', 'Stock actuel'), ('dernier_bl', 'Dernière livraison'), ('etat', 'État')]

def analyse_bc_export(db, key, ext, filtre='tous'):
    if filtre not in ANA_FILTRES: filtre = 'tous'
    flab, ffn = ANA_FILTRES[filtre]; sfx = '' if filtre == 'tous' else ' - ' + flab
    soc = (one(db, 'SELECT raison_sociale FROM societe WHERE id=1') or {}).get('raison_sociale') or SOCIETE
    X = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'; r1 = lambda v: round(v or 0, 2)
    def bloc(b):
        L = b['lignes']; rows = [[l.get(c[0]) for c in ANA_L_COLS] for l in L]
        rows.append(['', 'TOTAL', '', r1(b['commande']), r1(b['livre']), r1(sum(l['report'] for l in L)), r1(b['reste']), b['pct'], '', '', ''])
        return (f"{b['num']} · {b['client']} · {b['unite']} · {b['statut']} · {b['pct']} % livré ({r1(b['livre'])}/{r1(b['commande'])})", rows)
    hd = [c[1] for c in ANA_L_COLS]; m = re.match(r'^analyse_bc-(\d+)$', key)
    if m or key == 'analyse_bc_detail':
        data = analyse_bc_data(db, int(m.group(1)) if m else None)
        if m and not data: raise ApiError(404, 'BC introuvable')
        if not m: data = [b for b in data if ffn(b)]
        if not data: raise ApiError(404, 'Aucun BC pour ce filtre')
        blocks = [bloc(b) for b in data]
        title = f"Analyse du {data[0]['num']}" if m else 'Analyse détaillée des bons de commande' + sfx
        if ext == 'xlsx': return xlsx_blocks(title, hd, blocks), X
        info = [f"Date d’édition : {today()}"] + ([f"Client : {data[0]['client']} · Date du BC : {data[0]['date']} · Livraison prévue : {data[0]['date_livraison'] or 'non renseignée'}", f"Livré {r1(data[0]['livre'])} / {r1(data[0]['commande'])} ({data[0]['pct']} %) · Reste {r1(data[0]['reste'])} · {data[0]['nb_bl']} BL · Suivi : {data[0]['alerte']}"] if m else [f"{len(data)} bons de commande · Filtre : {flab}"])
        return pdf_blocks(title, soc, hd, blocks, info), 'application/pdf'
    data = [b for b in analyse_bc_data(db) if ffn(b)]; title = 'Tableau analytique des bons de commande' + sfx
    if not data: raise ApiError(404, 'Aucun BC pour ce filtre')
    heads = [c[1] for c in ANA_BC_COLS]; rows = [[r.get(c[0]) for c in ANA_BC_COLS] for r in data]
    cm, lv = sum(r['commande'] for r in data), sum(r['livre'] for r in data)
    sums = [round(sum((r[i] or 0) for r in rows), 2) if len(c) > 2 else None for i, c in enumerate(ANA_BC_COLS)]; sums[[c[0] for c in ANA_BC_COLS].index('pct')] = round(100 * lv / cm, 1) if cm else 0
    if ext == 'xlsx': return xlsx_bytes(title, heads, rows, sums), X
    info = [f"Date d’édition : {today()} · Filtre : {flab}", f"{len(data)} BC · {sum(1 for r in data if r['statut'] != 'Solde')} ouverts · {sum(1 for r in data if r['retard'])} en retard", f"Commandé {_fmt(cm)} · Livré {_fmt(lv)} · Reste {_fmt(cm - lv)} · Taux de livraison global {sums[[c[0] for c in ANA_BC_COLS].index('pct')]} %"]
    return pdf_bytes(title, soc, heads, rows, sums, info, pad=6), 'application/pdf'

def export_file(db, key, ext, filtre='tous', term='', vue='global', bc_type='tous'):
    if key == 'livre_reste': return livre_reste_export(db, ext, term, filtre if filtre in LR_FILTRES else 'tous', 'detail' if vue == 'detail' else 'global', bc_type if bc_type in ('Client', 'Fournisseur') else 'tous')
    if key == 'recherche_produit' or re.match(r'^produit_suivi-\d+$', key): return produit_export(db, key, ext, term, filtre if filtre in RP_FILTRES else 'tous')
    if re.match(r'^analyse_bc(_detail|-\d+)?$', key): return analyse_bc_export(db, key, ext, filtre)
    m = re.match(r'^(bc|bl|retours)-(\d+)$', key)
    if m and ext == 'pdf': return doc_pdf(db, m.group(1), int(m.group(2)))
    if key == 'priorite_fabrication':
        title = 'Priorité de fabrication selon l’avancement des BC'
        cdef = [('rang_priorite', 'Priorité'), ('num', 'N° BC'), ('client', 'Client'), ('pct_livre_bc', '% livré BC'), ('ref', 'Référence'), ('designation', 'Produit à fabriquer'), ('unite', 'Unité'), ('reste', 'Reste BC'), ('dispo', 'Stock affecté'), ('of_lance', 'OF déjà lancés'), ('a_lancer', 'Quantité à fabriquer'), ('etat', 'État')]
        data = priorite_fabrication(db)
        heads = [c[1] for c in cdef]
        rows = [[r.get(c[0]) for c in cdef] for r in data]
        soc = (one(db, 'SELECT raison_sociale FROM societe WHERE id=1') or {}).get('raison_sociale') or SOCIETE
        sums = [None] * len(heads)
        for i, c in enumerate(cdef):
            if c[0] in ('reste', 'dispo', 'of_lance', 'a_lancer'):
                sums[i] = round(sum((r[i] or 0) for r in rows), 2)
        if ext == 'xlsx': return xlsx_bytes(title, heads, rows, sums), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        info = [f"Date d’édition : {today()}", f"Produits à lancer : {len(rows)}", f"Quantité totale à fabriquer : {_fmt(sum(float(r.get('a_lancer') or 0) for r in data))}", "Classement : % livré BC décroissant, pour clôturer en priorité les commandes les plus avancées."]
        return pdf_bytes(title, soc, heads, rows, sums, info), 'application/pdf'
    if key == 'situation_bc':
        soc = (one(db, 'SELECT raison_sociale FROM societe WHERE id=1') or {}).get('raison_sociale') or SOCIETE
        t = EXPORTS[key][0]; hd = [c[1] for c in SITBC_COLS]; bl = sitbc_blocks(db)
        if ext == 'xlsx': return xlsx_blocks(t, hd, bl), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        return pdf_blocks(t, soc, hd, bl), 'application/pdf'
    title, sql, cdef = EXPORTS[key]
    data = sql(db) if callable(sql) else q(db, sql)
    heads = [c[1] for c in cdef]
    rows = [[r.get(c[0]) for c in cdef] for r in data]
    sums = [round(sum((r[i] or 0) for r in rows), 2) if len(c) > 2 else None for i, c in enumerate(cdef)]
    if not any(s is not None for s in sums): sums = [None] * len(cdef)
    soc = (one(db, 'SELECT raison_sociale FROM societe WHERE id=1') or {}).get('raison_sociale') or SOCIETE
    if ext == 'xlsx': return xlsx_bytes(title, heads, rows, sums), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return pdf_bytes(title, soc, heads, rows, sums), 'application/pdf'

# ---------- Envoi des états BC / BL par e-mail (à chaque déconnexion) ----------
import smtplib, ssl, socket
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
EMAIL_CFG_PATH = os.path.join(os.path.dirname(DB_PATH), 'email.json'); EMAIL_LOG_PATH = os.path.join(os.path.dirname(DB_PATH), 'email_log.json'); EMAIL_LOCK = threading.Lock()
EMAIL_DEF = {'actif': False, 'destinataires': '', 'smtp_host': '', 'smtp_port': 465, 'securite': 'ssl', 'smtp_user': '', 'smtp_pwd': '', 'expediteur': '', 'formats': ['xlsx', 'pdf'],
             'etats': ['bc', 'bl', 'bc_detail', 'bl_detail'], 'seulement_si_change': False, 'last_sig': '', 'verifier_certificat': True, 'a_la_fermeture': True}
EMAIL_ETATS = {'bc': ('Etat_BC', 'Bons de commande (liste)'), 'bc_detail': ('Etat_BC_detail', 'Bons de commande (détail des articles)'), 'bl': ('Etat_BL', 'Bons de livraison (liste)'),
               'bl_detail': ('Etat_BL_detail', 'Bons de livraison (détail des articles)'), 'analyse_bc': ('Analyse_BC', 'Tableau analytique des BC')}

def email_cfg():
    try: c = json.load(open(EMAIL_CFG_PATH, encoding='utf-8'))
    except Exception: c = {}
    return {**EMAIL_DEF, **c}
def email_write(c):
    with open(EMAIL_CFG_PATH, 'w', encoding='utf-8') as f: json.dump(c, f, ensure_ascii=False, indent=1)
    try: os.chmod(EMAIL_CFG_PATH, 0o600)
    except Exception: pass
def email_log(par, statut, dest, detail):
    try: L = json.load(open(EMAIL_LOG_PATH, encoding='utf-8'))
    except Exception: L = []
    L.append({'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'par': par, 'statut': statut, 'dest': dest, 'detail': detail})
    with open(EMAIL_LOG_PATH, 'w', encoding='utf-8') as f: json.dump(L[-60:], f, ensure_ascii=False)
def email_public():
    c = email_cfg(); pw = bool(c.pop('smtp_pwd', '')); c.pop('last_sig', None)
    try: L = json.load(open(EMAIL_LOG_PATH, encoding='utf-8'))
    except Exception: L = []
    return {**c, 'pwd_set': pw, 'log': L[::-1][:15], 'etats_dispo': {k: v[1] for k, v in EMAIL_ETATS.items()}}
def email_dests(c): return [x for x in re.split(r'[;,\s]+', c.get('destinataires') or '') if x]
def email_update(b):
    c = email_cfg(); dests = [x.strip() for x in re.split(r'[;,\s]+', str(b.get('destinataires', ''))) if x.strip()]
    for d in dests:
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', d): raise ApiError(400, f'Adresse e-mail invalide : {d}')
    if b.get('actif') and not dests: raise ApiError(400, 'Indiquez au moins une adresse de destination')
    if b.get('securite') not in ('ssl', 'starttls', 'none'): raise ApiError(400, 'Sécurité SMTP invalide')
    try: port = int(b.get('smtp_port') or 0)
    except Exception: port = 0
    if b.get('actif') and not (str(b.get('smtp_host', '')).strip() and 0 < port < 65536): raise ApiError(400, 'Serveur SMTP et port obligatoires')
    fm = [x for x in b.get('formats', []) if x in ('xlsx', 'pdf')]; et = [x for x in b.get('etats', []) if x in EMAIL_ETATS]
    if not fm or not et: raise ApiError(400, 'Choisissez au moins un format et un état')
    c.update(actif=bool(b.get('actif')), destinataires=', '.join(dests), smtp_host=str(b.get('smtp_host', '')).strip(), smtp_port=port or 465, securite=b['securite'], smtp_user=_clean(b.get('smtp_user', '')),
             expediteur=_clean(b.get('expediteur', '')), formats=fm, etats=et, seulement_si_change=bool(b.get('seulement_si_change')), verifier_certificat=b.get('verifier_certificat', True) is not False, a_la_fermeture=b.get('a_la_fermeture', True) is not False)
    c['smtp_host'] = _clean(c['smtp_host']).lower()
    if b.get('smtp_pwd'): c['smtp_pwd'] = str(b['smtp_pwd']).strip()
    email_write(c)

def _clean(v):
    """Supprime les espaces (y compris insécables / invisibles) copiés par erreur autour d'un identifiant ou d'un serveur."""
    return re.sub(r'[\s\u00a0\u200b\u200c\u200d\ufeff]+', '', str(v or ''))
def _est_gmail(c): return any(x in (c.get('smtp_host') or '').lower() for x in ('gmail', 'googlemail', 'google.com'))
def _mdp(c):
    """Mot de passe à utiliser. Gmail : le mot de passe d'application est affiché en 4 groupes de 4 lettres ; les espaces sont retirés."""
    p = (c.get('smtp_pwd') or '')
    return re.sub(r'[\s\u00a0\u200b\u200c\u200d\ufeff]+', '', p) if _est_gmail(c) else p.strip()

def email_sig(db):
    r = db.execute("SELECT (SELECT COUNT(*) FROM bc), (SELECT MAX(id) FROM bc), (SELECT group_concat(statut) FROM bc), (SELECT COUNT(*) FROM bl), (SELECT MAX(id) FROM bl), (SELECT group_concat(statut) FROM bl), "
                   "(SELECT total(COALESCE(qte_livree,0)+quantite) FROM bc_lignes), (SELECT total(quantite) FROM bl_lignes), (SELECT MAX(id) FROM mouvements)").fetchone()
    return hashlib.sha1(repr(tuple(r)).encode()).hexdigest()

def email_build(db, c, par, motif):
    now = datetime.datetime.now(); soc = (one(db, 'SELECT raison_sociale FROM societe WHERE id=1') or {}).get('raison_sociale') or SOCIETE
    k = analytics(db, {'jours': ['30']})['kpi']; blj = db.execute("SELECT COUNT(*) FROM bl WHERE date=? AND statut='Valide'", (today(),)).fetchone()[0]
    msg = EmailMessage(); msg['Subject'] = f"{soc} ERP - États BC et BL du {now.strftime('%d/%m/%Y %H:%M')}"
    msg['From'] = c['expediteur'] or c['smtp_user'] or 'erp@localhost'; msg['To'] = ', '.join(email_dests(c)); msg['Date'] = formatdate(localtime=True); msg['Message-ID'] = make_msgid(domain='erp-atelier.local')
    noms, pj, manq = [], [], []
    for key in c['etats']:
        pre, lab = EMAIL_ETATS[key]
        for ext in c['formats']:
            try: data, _ = export_file(db, key, ext)
            except Exception as ex: manq.append(f"{lab} ({ext.upper()}) : {getattr(ex, 'msg', None) or ex}"); continue
            ct = ('application', 'vnd.openxmlformats-officedocument.spreadsheetml.sheet') if ext == 'xlsx' else ('application', 'pdf')
            pj.append((data, ct, f"{pre}_{today()}.{ext}")); noms.append(f"{lab} ({ext.upper()})")
    if not noms: raise RuntimeError('Aucun état n’a pu être généré : ' + ' ; '.join(manq))
    lignes = [('BC ouverts', f"{k['bc_ouverts']} sur {k['bc_total']}"), ('Taux de livraison global', f"{k['taux_livraison']} %"), ('Reste à livrer', f"{_fmt(k['reste_a_livrer'])} pièces"), ('BL validés aujourd’hui', str(blj)),
              ('Stock négatif / sous mini', f"{k['stock_negatif']} / {k['sous_mini']}")]
    intro = f"{'Envoi de test' if motif == 'test' else 'Déconnexion de ' + par} le {now.strftime('%d/%m/%Y à %H:%M')}."
    msg.set_content(f"Bonjour,\n\n{intro}\nVous trouverez ci-joint les états BC et BL.\n\nRésumé :\n" + '\n'.join(f'- {a} : {b}' for a, b in lignes) + "\n\nPièces jointes :\n" + '\n'.join('- ' + n for n in noms) + f"\n\n{soc} ERP - message automatique.\n")
    msg.add_alternative(f"<div style='font-family:Segoe UI,Arial,sans-serif;color:#23211e'><h2 style='margin:0 0 6px'>{soc} ERP</h2><p>{intro}<br>États BC et BL en pièces jointes.</p>"
        "<table style='border-collapse:collapse;font-size:14px'>" + ''.join(f"<tr><td style='padding:5px 14px 5px 0;color:#7a756c'>{a}</td><td style='padding:5px 0'><b>{b}</b></td></tr>" for a, b in lignes) +
        "</table><p style='color:#7a756c;font-size:12px'>Pièces jointes : " + ' · '.join(noms) + "<br>Message automatique de l’ERP.</p></div>", subtype='html')
    for data, ct, fn in pj: msg.add_attachment(data, maintype=ct[0], subtype=ct[1], filename=fn)   # après le corps (EmailMessage devient multipart)
    return msg, manq

def _tcp(host, port, timeout):
    """Connexion TCP en essayant d'abord l'IPv4 (beaucoup de réseaux/PC bloquent ou bloquent à moitié l'IPv6 vers Gmail)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    err = None
    for fam, typ, proto, _, addr in infos:
        sk = socket.socket(fam, typ, proto); sk.settimeout(timeout)
        try: sk.connect(addr); return sk
        except OSError as e: err = e; sk.close()
    raise err or OSError('Aucune adresse')
class _SMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout): return _tcp(host, port, timeout)
class _SMTPS(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout): return self.context.wrap_socket(_tcp(host, port, timeout), server_hostname=self._host)

def _ctx(c):
    ctx = ssl.create_default_context()
    if not c.get('verifier_certificat', True): ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    return ctx

def email_err(e, stage=''):
    st = f' [étape : {stage}]' if stage else ''
    srv = ''
    if hasattr(e, 'smtp_code'):
        er = getattr(e, 'smtp_error', b''); er = er.decode('utf-8', 'replace') if isinstance(er, bytes) else str(er)
        srv = f' Réponse du serveur : {e.smtp_code} {re.sub(chr(10) + "|" + chr(13), " ", er).strip()[:300]}'
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ('Le serveur a refusé l’identifiant ou le mot de passe. Pour Gmail, vérifiez : (1) l’identifiant est l’adresse Gmail COMPLÈTE ; (2) la validation en 2 étapes est activée sur ce compte Google ; '
                '(3) vous avez saisi un « mot de passe d’application » de 16 lettres (myaccount.google.com/apppasswords) et NON le mot de passe Gmail habituel ; (4) ce mot de passe d’application n’a pas été supprimé/regénéré.' + srv + st)
    if isinstance(e, smtplib.SMTPNotSupportedError): return 'Ce serveur n’accepte pas l’identification sur ce mode de connexion : essayez le mode SSL (465) ou STARTTLS (587).' + srv + st
    if isinstance(e, smtplib.SMTPSenderRefused): return 'Adresse de l’expéditeur refusée : laissez le champ « Expéditeur » vide (ou mettez la même adresse que l’identifiant).' + srv + st
    if isinstance(e, smtplib.SMTPRecipientsRefused): return 'Adresse de destination refusée par le serveur.' + srv + st
    if isinstance(e, smtplib.SMTPServerDisconnected): return 'Le serveur a coupé la connexion (pare-feu, antivirus ou mauvais port/mode de sécurité).' + st
    if isinstance(e, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(e):
        return 'Le certificat du serveur n’a pas pu être vérifié (typique d’un antivirus qui analyse les connexions chiffrées, ou d’une date/heure du PC incorrecte). Vérifiez la date du PC, ou décochez « Vérifier le certificat du serveur ».' + st
    if isinstance(e, ssl.SSLError): return 'Erreur SSL/TLS : le port et le mode de sécurité ne correspondent pas (465 = SSL, 587 = STARTTLS). ' + str(e)[:150] + st
    if isinstance(e, UnicodeEncodeError): return 'Le mot de passe ou l’identifiant contient un caractère spécial/accentué non accepté : recopiez-les sans accent (le mot de passe d’application Gmail ne contient que des lettres minuscules).' + st
    if isinstance(e, (socket.timeout, TimeoutError)): return 'Délai dépassé : le serveur ne répond pas. Le port est probablement bloqué (pare-feu Windows, antivirus, box/réseau de l’entreprise). Essayez l’autre port (465 ↔ 587) ou un autre réseau (partage de connexion du téléphone).' + st
    if isinstance(e, socket.gaierror): return 'Nom du serveur introuvable : pas d’accès Internet/DNS ou nom de serveur mal écrit (Gmail : smtp.gmail.com).' + st
    if isinstance(e, ConnectionRefusedError): return 'Connexion refusée sur ce port (bloqué par un pare-feu/antivirus ou mauvais port).' + st
    if isinstance(e, OSError): return f'Serveur injoignable ({e.strerror or e}) : vérifiez la connexion Internet, l’adresse et le port.' + st
    return f'{type(e).__name__} : {e}' + srv + st

def _essais(c):
    """Combinaisons à tenter : celle configurée, puis (Gmail) l'autre mode standard."""
    h, p, sec = c['smtp_host'], int(c['smtp_port']), c['securite']; L = [(h, p, sec)]
    if _est_gmail(c):
        for alt in ((h, 465, 'ssl'), (h, 587, 'starttls')):
            if alt not in L: L.append(alt)
    return L

def _ouvrir(h, p, sec, ctx, timeout=25):
    if sec == 'ssl': sv = _SMTPS(h, p, timeout=timeout, context=ctx)
    else:
        sv = _SMTP(h, p, timeout=timeout); sv.ehlo()
        if sec == 'starttls': sv.starttls(context=ctx); sv.ehlo()
    return sv

def email_send(c, msg):
    """Envoie le message. Retourne (hote, port, securite) de la combinaison qui a fonctionné. Lève l'exception de la dernière tentative sinon."""
    ctx = _ctx(c); last = None; user, pwd = _clean(c['smtp_user']), _mdp(c)
    for (h, p, sec) in _essais(c):
        stage = 'connexion'; sv = None
        try:
            sv = _ouvrir(h, p, sec, ctx)
            if user: stage = 'identification'; sv.login(user, pwd)
            stage = 'envoi'; sv.send_message(msg)
            return (h, p, sec)
        except Exception as e:
            e.stage = stage
            if last is None or not isinstance(last, smtplib.SMTPAuthenticationError): last = e
            if isinstance(e, (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)): break   # inutile d'essayer un autre port
        finally:
            if sv is not None:
                try: sv.quit()
                except Exception: pass
    raise last

def email_diag(c):
    """Diagnostic étape par étape (sans rien envoyer) pour savoir exactement où ça bloque."""
    R = []
    def add(n, ok, d): R.append({'etape': n, 'ok': ok, 'detail': d})
    h = c.get('smtp_host') or ''
    if not h: add('Serveur SMTP renseigné', False, 'Saisissez le serveur (Gmail : smtp.gmail.com)'); return R
    try: ips = sorted({i[4][0] for i in socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM)}); add('Accès Internet / DNS', True, f'{h} -> ' + ', '.join(ips[:4]))
    except Exception as e: add('Accès Internet / DNS', False, email_err(e)); return R
    for p, sec in ((465, 'ssl'), (587, 'starttls')):
        try:
            sk = _tcp(h, p, 10); sk.close(); add(f'Port {p} ouvert', True, 'connexion TCP réussie')
        except Exception as e: add(f'Port {p} ouvert', False, email_err(e))
    ctx = _ctx(c); user, pwd = _clean(c['smtp_user']), _mdp(c)
    if not user: add('Identifiant renseigné', False, 'Saisissez l’adresse Gmail complète'); return R
    if not pwd: add('Mot de passe renseigné', False, 'Saisissez le mot de passe d’application'); return R
    if _est_gmail(c): add('Format du mot de passe', len(pwd) == 16 and pwd.isalpha(), f'{len(pwd)} caractères (un mot de passe d’application Gmail = 16 lettres)' + ('' if len(pwd) == 16 and pwd.isalpha() else ' → ce n’est probablement PAS un mot de passe d’application'))
    for (hh, p, sec) in _essais(c):
        sv = None
        try:
            sv = _ouvrir(hh, p, sec, ctx, 20); add(f'Connexion sécurisée {p} ({sec.upper()})', True, 'chiffrement établi')
            sv.login(user, pwd); add(f'Identification sur le port {p}', True, 'identifiant et mot de passe acceptés ✔'); break
        except Exception as e: add(f'{"Identification" if sv else "Connexion sécurisée"} sur le port {p}', False, email_err(e))
        finally:
            if sv is not None:
                try: sv.quit()
                except Exception: pass
    return R

def envoyer_etats(par, motif='déconnexion', force=False):
    """Génère et envoie les états. Appelée en arrière-plan à la déconnexion (force=False) ou depuis le bouton de test (force=True)."""
    c = email_cfg()
    if not force and not c['actif']: return {'statut': 'Désactivé', 'detail': ''}
    dests = email_dests(c); dst = ', '.join(dests)
    if not c['smtp_host'] or not dests: return {'statut': 'Erreur', 'detail': 'Configuration incomplète (serveur SMTP ou destinataire manquant)'}
    if not EMAIL_LOCK.acquire(blocking=False): return {'statut': 'Ignoré', 'detail': 'Un envoi est déjà en cours'}
    try:
        db = conn()
        try:
            sig = email_sig(db)
            if not force and c['seulement_si_change'] and sig == c['last_sig']:
                email_log(par, 'Ignoré', dst, 'Aucun changement depuis le dernier envoi'); return {'statut': 'Ignoré', 'detail': 'Aucun changement'}
            msg, manq = email_build(db, c, par, motif)
        finally: db.close()
        ok = email_send(c, msg)
        c2 = email_cfg()
        if not force: c2['last_sig'] = sig
        if (c['smtp_host'], int(c['smtp_port']), c['securite']) != ok: c2['smtp_host'], c2['smtp_port'], c2['securite'] = ok   # mémorise la combinaison qui marche
        email_write(c2)
        n = sum(1 for _ in msg.iter_attachments()); extra = (' — non joints : ' + ' ; '.join(manq)) if manq else ''
        email_log(par, 'OK', dst, f"{n} fichier(s) envoyé(s)" + (' (test)' if force else '') + (f' via port {ok[1]}' if ok[1] != int(c['smtp_port']) else '') + extra)
        return {'statut': 'OK', 'detail': f'{n} fichier(s) envoyé(s) à {dst}' + extra}
    except Exception as e:
        d = email_err(e, getattr(e, 'stage', '')); email_log(par, 'Erreur', dst, d); return {'statut': 'Erreur', 'detail': d}
    finally: EMAIL_LOCK.release()

FERME = {}          # sid -> heure de fermeture signalée par le navigateur (annulée si l'utilisateur revient dans les secondes qui suivent)
FERME_DELAI, INACTIF_DELAI = 20, 30 * 60
def _fermeture(sid):
    """Fermeture de la fenêtre sans déconnexion : envoi des états si l'utilisateur n'est pas revenu (actualisation) après FERME_DELAI s."""
    if FERME.pop(sid, None) is None: return
    s = SESS.get(sid)
    if not s or s.get('mailed') or not email_cfg().get('a_la_fermeture', True): return
    s['mailed'] = True; envoyer_etats((s['user'].get('nom') or s['user']['login']), 'fermeture de la fenêtre')
def boucle_inactivite():
    """Filet de sécurité : fenêtre fermée sans signal (plantage, PC éteint, veille) -> envoi après INACTIF_DELAI sans activité."""
    while True:
        time.sleep(60)
        try:
            for sid, s in list(SESS.items()):
                if s.get('mailed') or time.time() - s.get('seen', time.time()) < INACTIF_DELAI: continue
                s['mailed'] = True
                if email_cfg().get('a_la_fermeture', True): envoyer_etats((s['user'].get('nom') or s['user']['login']), 'inactivité (fenêtre fermée)')
        except Exception: pass

class H(BaseHTTPRequestHandler):
    server_version = 'AtelierERP'
    def log_message(self, *a): pass
    def user(self):
        c = self.headers.get('Cookie', '')
        sid = next((x.split('=', 1)[1] for x in c.split('; ') if x.startswith('sid=')), None)
        s = SESS.get(sid)
        if s and s['exp'] > time.time():
            s['seen'] = time.time(); s['mailed'] = False; FERME.pop(sid, None)   # activité = pas de fermeture (ex. simple actualisation de la page)
            return s['user']

    def file_token_user(self, token):
        if not token: return None
        x = FILETOKENS.get(token)
        if not x: return None
        if x['exp'] <= time.time():
            FILETOKENS.pop(token, None)
            return None
        return x['user']
    def out(self, code, obj, ctype='application/json', cookie=None):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type', ctype + '; charset=utf-8'); self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if cookie: self.send_header('Set-Cookie', cookie)
        self.end_headers(); self.wfile.write(data)
    def static(self):
        pth = urlparse(self.path).path
        f = os.path.normpath(os.path.join(BASE, 'static', 'index.html' if pth in ('/', '') else pth.lstrip('/')))
        if not f.startswith(os.path.join(BASE, 'static')) or not os.path.isfile(f): return self.out(404, b'Introuvable', 'text/plain')
        ct = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png'}.get(os.path.splitext(f)[1], 'application/octet-stream')
        self.out(200, open(f, 'rb').read(), ct)
    def fichier(self, m, p, user):
        kind = p[3]
        if kind not in KINDS: raise ApiError(404, 'Type de fichier inconnu')
        try: id = int(p[1])
        except ValueError: raise ApiError(404, 'Introuvable')
        if m == 'GET':
            db = conn()
            try: data, ct, nom = file_get(db, id, kind)
            finally: db.close()
            self.send_response(200); self.send_header('Content-Type', ct); self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition', f'inline; filename="{nom}"'); self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'private, max-age=86400')   # le nom du fichier change à chaque remplacement
            self.end_headers(); return self.wfile.write(data)
        if m in ('POST', 'PUT'):
            n, mx = int(self.headers.get('Content-Length') or 0), KINDS[kind][1]
            if n > mx:
                left = n if n <= 200 << 20 else 0
                while left > 0:   # on vide la requête pour que le navigateur reçoive bien le message d'erreur
                    left -= len(self.rfile.read(min(left, 1 << 16)) or b'\0' * left)
                raise ApiError(413, f'Fichier trop volumineux (max {mx >> 20} Mo)')
            data = self.rfile.read(n)
        elif m != 'DELETE': raise ApiError(405, 'Méthode non permise')
        with LOCK:
            db = conn()
            try:
                res = file_del(db, id, kind) if m == 'DELETE' else file_put(db, id, kind, data)
                db.commit()
            except Exception:
                db.rollback(); raise
            finally: db.close()
        self.out(200, res)
    def soc_fichier(self, m, p):
        kind = p[1]
        if kind not in SOC_KINDS: raise ApiError(404, 'Type de fichier inconnu')
        if m == 'GET':
            db = conn()
            try: data, ct, nom = soc_file_get(db, kind)
            finally: db.close()
            self.send_response(200); self.send_header('Content-Type', ct); self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition', f'inline; filename="{nom}"'); self.send_header('X-Content-Type-Options', 'nosniff'); self.send_header('Cache-Control','no-store')
            self.end_headers(); return self.wfile.write(data)
        if m in ('POST','PUT'):
            n, mx = int(self.headers.get('Content-Length') or 0), SOC_KINDS[kind][1]
            if n > mx:
                while n > 0: n -= len(self.rfile.read(min(n, 1 << 16)) or b'\0')
                raise ApiError(413, f'Fichier trop volumineux (max {mx >> 20} Mo)')
            data=self.rfile.read(n)
            with LOCK:
                db=conn()
                try: res=soc_file_put(db,kind,data); db.commit()
                except Exception: db.rollback(); raise
                finally: db.close()
            return self.out(200,res)
        if m == 'DELETE':
            with LOCK:
                db=conn()
                try: res=soc_file_del(db,kind); db.commit()
                except Exception: db.rollback(); raise
                finally: db.close()
            return self.out(200,res)
        raise ApiError(405,'Méthode non permise')

    def handle_any(self, m):
        u = urlparse(self.path); parts = [x for x in u.path.split('/') if x]
        if not parts or parts[0] != 'api': return self.static()
        is_file = len(parts) == 5 and parts[1] == 'articles' and parts[3] == 'fichier'
        is_soc_file = len(parts) == 3 and parts[1] == 'societe' and parts[2] in SOC_KINDS
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}') if m in ('POST', 'PUT') and not (is_file or is_soc_file) else {}
            p, qs = parts[1:], parse_qs(u.query)
            if p == ['login'] and m == 'POST':
                db = conn(); r = one(db, 'SELECT * FROM users WHERE login=?', (body.get('login', ''),)); db.close()
                if not r or not chk(body.get('pwd', ''), r['pwd']): raise ApiError(401, 'Identifiant ou mot de passe incorrect')
                sid = secrets.token_hex(24)
                SESS[sid] = {'user': {'id': r['id'], 'login': r['login'], 'nom': r['nom'], 'role': r['role']}, 'exp': time.time() + 43200, 'seen': time.time(), 'mailed': False}
                return self.out(200, {'ok': True}, cookie=f'sid={sid}; HttpOnly; SameSite=Strict; Path=/')
            user = self.user()
            token = (qs.get('token') or [None])[0]
            if not user and is_file:
                user = self.file_token_user(token)
            if not user: raise ApiError(401, 'Non authentifié')
            if p == ['file-token'] and m == 'GET':
                tok = secrets.token_urlsafe(32)
                FILETOKENS[tok] = {'user': user, 'exp': time.time() + 300}
                return self.out(200, {'token': tok, 'expires_in': 300})
            if is_file: return self.fichier(m, p, user)
            if is_soc_file:
                if user['role'] != 'admin': raise ApiError(403, 'Réservé à l’administrateur')
                return self.soc_fichier(m, p)
            if p and p[0] == 'email':
                if user['role'] != 'admin': raise ApiError(403, "Réservé à l'administrateur")
                if p == ['email'] and m == 'GET': return self.out(200, email_public())
                if p == ['email'] and m == 'POST': email_update(body); return self.out(200, email_public())
                if p == ['email', 'diag'] and m == 'POST':
                    return self.out(200, {'etapes': email_diag(email_cfg())})
                if p == ['email', 'test'] and m == 'POST':
                    r = envoyer_etats(user.get('nom') or user['login'], 'test', force=True)
                    if r['statut'] != 'OK': raise ApiError(400, r['detail'])
                    return self.out(200, r)
                raise ApiError(405, 'Méthode non permise')
            if p and p[0] in ('sauvegardes','restaurer'):
                if user['role'] != 'admin': raise ApiError(403, "Réservé à l'administrateur")
                if p == ['sauvegardes'] and m == 'GET': return self.out(200, {'archives':lister_sauvegardes(),'intervalle_heures':6,'retention':BACKUP_KEEP})
                if p == ['sauvegardes'] and m == 'POST':
                    with LOCK: result=creer_sauvegarde('manuelle')
                    return self.out(200, result)
                if p == ['restaurer'] and m == 'POST':
                    with LOCK: result=restaurer_sauvegarde(body.get('nom',''))
                    return self.out(200, result)
                raise ApiError(405, 'Méthode non permise')
            if len(p) == 2 and p[0] == 'export' and m == 'GET':
                key, _, ext = p[1].rpartition('.')
                if key == 'livre_reste' and ext in ('xlsx', 'pdf'): pass
                elif not (key == 'recherche_produit' or re.match(r'^produit_suivi-\d+$', key)) and not re.match(r'^analyse_bc(_detail|-\d+)?$', key) and (key not in EXPORTS and key != 'priorite_fabrication' and not re.match(r'^(bc|bl|retours)-\d+$', key)) or ext not in ('xlsx', 'pdf') or (ext == 'xlsx' and key not in EXPORTS and key != 'priorite_fabrication' and not key.startswith('analyse_bc') and key != 'recherche_produit' and not key.startswith('produit_suivi-')): raise ApiError(404, 'Export inconnu')
                db = conn()
                flt = (qs.get('filtre') or ['tous'])[0]; term = (qs.get('q') or [''])[0]
                flt = flt if (flt in LR_FILTRES if key == 'livre_reste' else flt in RP_FILTRES if key == 'recherche_produit' else flt in ANA_FILTRES) else 'tous'
                vue = (qs.get('vue') or ['global'])[0]; bct = (qs.get('type') or ['tous'])[0]
                try: data, ct = export_file(db, key, ext, flt, term, vue, bct)
                finally: db.close()
                self.send_response(200); self.send_header('Content-Type', ct); self.send_header('Content-Length', str(len(data)))
                fsx = '_' + flt if (key in ('analyse_bc', 'analyse_bc_detail') and flt != 'tous') else ''
                self.send_header('Content-Disposition', f'attachment; filename="{key}{fsx}_{today()}.{ext}"'); self.send_header('Cache-Control', 'no-store')
                self.end_headers(); return self.wfile.write(data)
            if p == ['me']:
                db = conn()
                try:
                    s = one(db, 'SELECT * FROM societe WHERE id=1')
                finally: db.close()
                return self.out(200, {**user, 'societe': (s or {}).get('raison_sociale') or SOCIETE, 'societe_data': s})
            if p == ['closed'] and m == 'POST':
                sid = next((x.split('=', 1)[1] for x in self.headers.get('Cookie', '').split('; ') if x.startswith('sid=')), None)
                FERME[sid] = time.time(); threading.Timer(FERME_DELAI, _fermeture, args=(sid,)).start()
                return self.out(200, {'ok': True})
            if p == ['logout']:
                sid = next((x.split('=', 1)[1] for x in self.headers.get('Cookie', '').split('; ') if x.startswith('sid=')), None)
                FERME.pop(sid, None); SESS.pop(sid, None)
                threading.Thread(target=envoyer_etats, args=(user.get('nom') or user['login'], 'déconnexion'), daemon=True).start()   # états BC/BL par e-mail
                return self.out(200, {'ok': True}, cookie='sid=; Max-Age=0; Path=/')
            if p == ['password'] and m == 'POST':
                with LOCK:
                    db = conn(); r = one(db, 'SELECT * FROM users WHERE id=?', (user['id'],))
                    if not chk(body.get('ancien', ''), r['pwd']): raise ApiError(400, 'Ancien mot de passe incorrect')
                    if len(body.get('nouveau', '')) < 6: raise ApiError(400, 'Minimum 6 caractères')
                    db.execute('UPDATE users SET pwd=? WHERE id=?', (hpwd(body['nouveau']), user['id'])); db.commit(); db.close()
                return self.out(200, {'ok': True})
            with (LOCK if m != 'GET' else contextlib.nullcontext()):
                db = conn()
                try:
                    res = route(db, m, p, body, qs, user)
                    if m != 'GET': recalculer(db); db.commit()   # calcul automatique après chaque modification
                except Exception:
                    db.rollback(); raise
                finally: db.close()
            self.out(200, res)
        except ApiError as e: self.out(e.code, {'error': e.msg, **e.x})
        except sqlite3.IntegrityError as e:
            s = str(e); self.out(409, {'error': 'Doublon : cette valeur existe déjà.' if 'UNIQUE' in s else "Opération impossible : l'élément est utilisé ailleurs ou une valeur obligatoire manque."})
        except Exception: traceback.print_exc(); self.out(500, {'error': 'Erreur interne du serveur'})
    do_GET = lambda s: s.handle_any('GET')
    do_POST = lambda s: s.handle_any('POST')
    do_PUT = lambda s: s.handle_any('PUT')
    do_DELETE = lambda s: s.handle_any('DELETE')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='0.0.0.0'); ap.add_argument('--port', type=int, default=8000); ap.add_argument('--demo', action='store_true', help='charge des données de démonstration')
    a = ap.parse_args(); init_db(a.demo)
    threading.Thread(target=boucle_sauvegarde, daemon=True, name='sauvegardes-automatiques').start()
    threading.Thread(target=boucle_inactivite, daemon=True, name='inactivite-email').start()
    print(f'{SOCIETE} ERP -> http://localhost:{a.port}   (connexion initiale : admin / admin123)\nBase de données : {DB_PATH}')
    try:
        srv = ThreadingHTTPServer((a.host, a.port), H)
    except OSError as e:
        sys.exit(f"Impossible de démarrer sur le port {a.port} ({e}). Essayez : python server.py --port 8080")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
