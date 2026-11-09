#!/usr/bin/env python3
"""ERP Atelier - lanceur logiciel (fenêtre native). Démarre le serveur en local puis ouvre l'interface."""
import os, sys, socket, threading, time, webbrowser

APP = 'ERP Atelier'

def data_dir():
    if os.name == 'nt':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    d = os.path.join(base, 'ERP_ATELIER', 'data')
    os.makedirs(d, exist_ok=True)
    return d

# Les données vivent hors du programme : elles survivent aux mises à jour / réinstallations.
os.environ.setdefault('ATELIER_DB', os.path.join(data_dir(), 'atelier.db'))
import server  # noqa: E402  (doit venir après la définition de ATELIER_DB)

def free_port(pref=8000):
    for p in (pref, 0):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p)); port = s.getsockname()[1]; s.close(); return port
        except OSError:
            s.close()

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1)); return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()

def start_server(port, demo=False, host='127.0.0.1'):
    server.init_db(demo)
    threading.Thread(target=server.boucle_sauvegarde, daemon=True, name='sauvegardes-automatiques').start()
    srv = server.ThreadingHTTPServer((host, port), server.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def wait_ready(port, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        try: socket.create_connection(('127.0.0.1', port), 0.3).close(); return True
        except OSError: time.sleep(0.1)
    return False

def fallback_window(url, srv):
    """Sans pywebview : ouvre le navigateur et affiche une petite fenêtre pour arrêter le programme."""
    webbrowser.open(url)
    try:
        import tkinter as tk
        r = tk.Tk(); r.title(APP); r.geometry('340x130'); r.resizable(False, False)
        tk.Label(r, text=f'{APP} est en cours d\'exécution.\n{url}', pady=14).pack()
        tk.Button(r, text='Ouvrir', command=lambda: webbrowser.open(url)).pack(side='left', padx=40)
        tk.Button(r, text='Quitter', command=r.destroy).pack(side='right', padx=40)
        r.mainloop()
    except Exception:
        print(f'{APP} : {url}  (Ctrl+C pour arrêter)')
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: pass

def message(txt):
    try:
        import ctypes; ctypes.windll.user32.MessageBoxW(0, txt, APP, 0x40)
    except Exception: print(txt)

def reset_admin():
    server.init_db(False)
    db = server.conn()
    if db.execute("SELECT 1 FROM users WHERE login='admin'").fetchone():
        db.execute("UPDATE users SET pwd=?, role='admin' WHERE login='admin'", (server.hpwd('admin123'),))
    else:
        db.execute("INSERT INTO users(login,nom,role,pwd) VALUES('admin','Administrateur','admin',?)", (server.hpwd('admin123'),))
    db.commit(); db.close()
    message('Mot de passe réinitialisé.\n\nIdentifiant : admin\nMot de passe : admin123')

def main():
    if '--reset-admin' in sys.argv:
        return reset_admin()
    demo = '--demo' in sys.argv
    reseau = '--reseau' in sys.argv   # accès depuis d'autres appareils (application Android, autres PC)
    port = 8000 if reseau else free_port()
    try:
        srv = start_server(port, demo, '0.0.0.0' if reseau else '127.0.0.1')
    except OSError as e:
        message(f'Impossible de démarrer sur le port {port} ({e}).\nLe logiciel est peut-être déjà ouvert.'); return
    if '--selftest' in sys.argv:
        print('serveur OK' if wait_ready(port) else 'ECHEC'); return
    wait_ready(port)
    url = f'http://127.0.0.1:{port}'
    if reseau:
        message(f'Accès réseau activé.\n\nDans l\'application Android, saisissez :\nhttp://{lan_ip()}:{port}\n\n(le téléphone doit être sur le même Wi-Fi que ce PC)')
    if '--navigateur' in sys.argv:
        fallback_window(url, srv); srv.shutdown(); return
    try:
        import webview
        try: webview.settings['ALLOW_DOWNLOADS'] = True
        except Exception: pass
        webview.create_window(APP, url, width=1366, height=820, min_size=(1000, 600), confirm_close=False)
        webview.start()
    except ImportError:
        fallback_window(url, srv)
    srv.shutdown()

if __name__ == '__main__':
    main()
