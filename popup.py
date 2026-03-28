import os
os.environ['QT_LOGGING_RULES'] = '*.debug=false;qt.qpa.*=false'

import tkinter as tk
import subprocess
import shutil
import re
import time
import webbrowser
from urllib.parse import quote_plus

import pystray
from PIL import Image, ImageDraw


# --- Detección de entorno gráfico ---

def is_wayland():
    return os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland'


def get_selected_text():
    try:
        if is_wayland():
            cmd = ['wl-paste', '--primary', '--no-newline']
        else:
            cmd = ['xclip', '-o', '-selection', 'primary']
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=2
        ).decode('utf-8')
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired):
        return ''


def copy_to_clipboard(text):
    if is_wayland():
        cmd = ['wl-copy']
    else:
        cmd = ['xclip', '-selection', 'clipboard']
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    process.communicate(text.encode('utf-8'))


# --- Posición del cursor (KDE Wayland via KWin scripting) ---

KWIN_SCRIPT_PATH = '/tmp/selecton_cursorpos.js'
KWIN_SCRIPT_NAME = 'selecton_cursorpos'
CURSOR_MARKER = 'SELECTON_CURSOR'

with open(KWIN_SCRIPT_PATH, 'w') as _f:
    _f.write(f"print('{CURSOR_MARKER}:' + workspace.cursorPos.x + ',' + workspace.cursorPos.y);\n")


def _get_cursor_from_kwin():
    try:
        subprocess.run(
            ['qdbus6', 'org.kde.KWin', '/Scripting',
             'org.kde.kwin.Scripting.unloadScript', KWIN_SCRIPT_NAME],
            capture_output=True, text=True, timeout=1)
        r = subprocess.run(
            ['qdbus6', 'org.kde.KWin', '/Scripting',
             'org.kde.kwin.Scripting.loadScript',
             KWIN_SCRIPT_PATH, KWIN_SCRIPT_NAME],
            capture_output=True, text=True, timeout=1)
        script_id = r.stdout.strip()
        if not script_id.isdigit():
            return None
        subprocess.run(
            ['qdbus6', 'org.kde.KWin', f'/Scripting/Script{script_id}', 'run'],
            capture_output=True, text=True, timeout=1)
        time.sleep(0.1)
        r2 = subprocess.run(
            ['journalctl', '--user', '-u', 'plasma-kwin_wayland',
             '-n', '15', '--no-pager', '-o', 'cat'],
            capture_output=True, text=True, timeout=1)
        for line in reversed(r2.stdout.strip().split('\n')):
            if CURSOR_MARKER in line:
                coords = line.split(CURSOR_MARKER + ':')[1]
                x, y = coords.split(',')
                return int(float(x)), int(float(y))
    except Exception:
        pass
    return None


def get_cursor_position(root):
    if is_wayland():
        pos = _get_cursor_from_kwin()
        if pos:
            return pos
    return root.winfo_pointerx(), root.winfo_pointery()


# --- Icono para system tray ---

def create_tray_icon_image():
    size = 128
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i in range(50, 0, -1):
        ratio = i / 50
        r = int(180 + (220 - 180) * (1 - ratio))
        g = int(60 + (80 - 60) * (1 - ratio))
        b = int(200 + (160 - 200) * (1 - ratio))
        pad = int((1 - ratio) * 24) + 4
        draw.ellipse([pad, pad, size - pad, size - pad],
                     fill=(r, g, b, 255))
    bx, by = 34, 38
    bw, bh = 60, 36
    draw.rounded_rectangle(
        [bx, by, bx + bw, by + bh], radius=10, fill='#ffffff')
    draw.polygon([
        (bx + 18, by + bh), (bx + 10, by + bh + 12),
        (bx + 30, by + bh)], fill='#ffffff')
    dot_y = by + bh // 2
    for dx in [-12, 0, 12]:
        draw.ellipse([bx + bw // 2 + dx - 4, dot_y - 4,
                      bx + bw // 2 + dx + 4, dot_y + 4], fill='#9b40c8')
    return img


# --- Popup estilo Selecton ---

BG = '#333333'
BG_HOVER = '#444444'
TEXT = '#ffffff'
BORDER_COLOR = '#222222'
SEPARATOR = '#4a4a4a'
ICON_SIZE = 18


def draw_copy_icon(canvas, x, y, color):
    canvas.create_rectangle(x+2, y+4, x+11, y+15, outline=color, width=1.2)
    canvas.create_rectangle(x+5, y+1, x+14, y+12, outline=color, fill=BG, width=1.2)


def draw_search_icon(canvas, x, y, color):
    canvas.create_oval(x+1, y+1, x+10, y+10, outline=color, width=1.4)
    canvas.create_line(x+9, y+9, x+14, y+14, fill=color, width=1.4)


class PopupBar:
    def __init__(self, root):
        self.root = root
        self.selected_text = ''
        self.visible = False

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes('-type', 'tooltip')
        self.win.attributes('-topmost', True)
        self.win.configure(bg=BORDER_COLOR)
        self.win.geometry('+99999+99999')

        outer = tk.Frame(self.win, bg=BORDER_COLOR, padx=1, pady=1)
        outer.pack(fill=tk.BOTH, expand=True)
        self.bar = tk.Frame(outer, bg=BG)
        self.bar.pack(fill=tk.BOTH, expand=True)

        self._add_button('Copiar', draw_copy_icon, self._copy)
        self._add_sep()
        self._add_button('Buscar', draw_search_icon, self._search)
        self.win.bind('<Escape>', lambda e: self.hide())
        self.win.update_idletasks()

    def _add_button(self, label, icon_fn, cmd):
        btn_frame = tk.Frame(self.bar, bg=BG, cursor='hand2')
        btn_frame.pack(side=tk.LEFT)
        icon = tk.Canvas(btn_frame, width=ICON_SIZE, height=ICON_SIZE,
                         bg=BG, highlightthickness=0)
        icon.pack(side=tk.LEFT, padx=(10, 3), pady=8)
        icon_fn(icon, 1, 1, TEXT)
        lbl = tk.Label(btn_frame, text=label, font=('Sans', 10),
                       fg=TEXT, bg=BG, cursor='hand2')
        lbl.pack(side=tk.LEFT, padx=(0, 10), pady=8)

        def on_enter(e, f=btn_frame, ic=icon, lb=lbl):
            for w in (f, ic, lb): w.configure(bg=BG_HOVER)
        def on_leave(e, f=btn_frame, ic=icon, lb=lbl):
            for w in (f, ic, lb): w.configure(bg=BG)

        for widget in (btn_frame, icon, lbl):
            widget.bind('<Button-1>', lambda e, c=cmd: c())
            widget.bind('<Enter>', on_enter)
            widget.bind('<Leave>', on_leave)

    def _add_sep(self):
        tk.Frame(self.bar, bg=SEPARATOR, width=1).pack(
            side=tk.LEFT, fill=tk.Y, pady=6)

    def show(self, text, x, y):
        self.selected_text = text
        self.win.geometry(f'+{x}+{y}')
        self.visible = True

    def hide(self):
        if self.visible:
            self.win.geometry('+99999+99999')
            self.visible = False

    def _copy(self):
        copy_to_clipboard(self.selected_text)
        self.hide()

    def _search(self):
        webbrowser.open('https://www.google.com/search?q='
                        + quote_plus(self.selected_text))


# --- App principal (polling adaptativo) ---

FAST_POLL = 800       # cuando se detectó cambio reciente
SLOW_POLL = 5000      # estado normal (no interfiere con click derecho)
HIDE_AFTER = 3.5      # segundos antes de ocultar popup
COOLDOWN = 3000       # pausa después de ocultar popup

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = PopupBar(self.root)
        self.paused = False
        self.previous_text = get_selected_text()
        self._show_time = 0
        self._no_change_count = 0
        self._schedule_poll(SLOW_POLL)
        self._start_tray()

    def _schedule_poll(self, interval):
        self.root.after(interval, self._check_clipboard)

    def _check_clipboard(self):
        # Auto-hide
        if self.popup.visible:
            if time.time() - self._show_time > HIDE_AFTER:
                self.popup.hide()
                # Cooldown: no leer wl-paste por un rato
                self._schedule_poll(COOLDOWN)
                return
            # Popup visible: no leer wl-paste, solo esperar
            self._schedule_poll(500)
            return

        if self.paused:
            self._schedule_poll(SLOW_POLL)
            return

        # Leer selección
        selected_text = get_selected_text()

        if selected_text != self.previous_text and selected_text.strip():
            cx, cy = get_cursor_position(self.root)
            self.popup.show(selected_text, cx, cy - 45)
            self._show_time = time.time()
            self.previous_text = selected_text
            self._no_change_count = 0
            self._schedule_poll(FAST_POLL)
        else:
            self._no_change_count += 1
            # Después de 3 polls sin cambio, ir a modo lento
            if self._no_change_count >= 3:
                self._schedule_poll(SLOW_POLL)
            else:
                self._schedule_poll(FAST_POLL)

    def _toggle_pause(self, icon, item):
        self.paused = not self.paused
        icon.update_menu()

    def _quit(self, icon, item):
        icon.stop()
        self.root.after(0, self.root.destroy)

    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem(
                lambda _: 'Reanudar' if self.paused else 'Pausar',
                self._toggle_pause),
            pystray.MenuItem('Salir', self._quit))
        self.tray_icon = pystray.Icon(
            'popup-linux', create_tray_icon_image(),
            'Popup Linux', menu)
        self.tray_icon.run_detached()

    def run(self):
        self.root.mainloop()


def check_dependencies():
    if is_wayland():
        tools = {'wl-paste': 'wl-clipboard', 'wl-copy': 'wl-clipboard'}
    else:
        tools = {'xclip': 'xclip'}
    missing = [name for name in tools if not shutil.which(name)]
    if missing:
        pkgs = sorted(set(tools[m] for m in missing))
        print(f'Error: faltan dependencias del sistema: {", ".join(pkgs)}')
        print('Instalá con:')
        print(f'  sudo pacman -S {" ".join(pkgs)}    # Arch/Manjaro')
        print(f'  sudo apt install {" ".join(pkgs)}  # Debian/Ubuntu')
        raise SystemExit(1)


if __name__ == '__main__':
    check_dependencies()
    app = App()
    app.run()
