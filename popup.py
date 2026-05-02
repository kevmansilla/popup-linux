import os
os.environ['QT_LOGGING_RULES'] = '*.debug=false;qt.qpa.*=false'

import tkinter as tk
import subprocess
import shutil
import re
import time
import glob
import queue
import selectors
import threading
import webbrowser
from urllib.parse import quote_plus

import evdev
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
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


def fix_wrapped_code(text):
    """Une saltos de línea dentro de string literals.

    Cuando el terminal envuelve líneas largas, mete \\n reales en medio
    de strings (URLs, comandos con -r '...', etc). Esto rompe el código
    al pegarlo. Esta función detecta esos saltos siguiendo el estado de
    comillas (', ", `, ''' y \"\"\") y los elimina junto con la
    indentación de la siguiente línea.
    """
    out = []
    i = 0
    n = len(text)
    state = None  # None, "'", '"', '`', "'''", '\"\"\"'

    while i < n:
        c = text[i]

        if state is None and i + 2 < n and text[i:i+3] in ('"""', "'''"):
            triple = text[i:i+3]
            end = text.find(triple, i + 3)
            if end >= 0:
                out.append(text[i:end + 3])
                i = end + 3
            else:
                out.append(text[i:])
                i = n
            continue

        if state is None and c in ('"', "'", '`'):
            state = c
            out.append(c)
            i += 1
            continue

        if state in ('"', "'", '`'):
            if c == '\\' and i + 1 < n:
                out.append(c)
                out.append(text[i + 1])
                i += 2
                continue
            if c == state:
                state = None
                out.append(c)
                i += 1
                continue
            if c == '\n':
                i += 1
                while i < n and text[i] in ' \t':
                    i += 1
                continue

        out.append(c)
        i += 1

    return ''.join(out)


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

BG = '#1c1c1c'
BG_HOVER = '#2d2d2d'
TEXT = '#d4d4d4'
TEXT_HOVER = '#ffffff'
BORDER_COLOR = '#0a0a0a'
ICON_SIZE = 16
RADIUS = 12
PAD = 6


def draw_rounded_rect(canvas, x1, y1, x2, y2, r, fill, outline=''):
    """Polígono suavizado simulando un rectángulo redondeado."""
    pts = [
        x1 + r, y1,  x2 - r, y1,
        x2,     y1,  x2,     y1 + r,
        x2,     y2 - r, x2,  y2,
        x2 - r, y2,  x1 + r, y2,
        x1,     y2,  x1,     y2 - r,
        x1,     y1 + r, x1,  y1,
    ]
    return canvas.create_polygon(pts, smooth=True, fill=fill, outline=outline)


def draw_copy_icon(canvas, x, y, color):
    canvas.create_rectangle(
        x+5, y+1, x+14, y+11, outline=color, width=1.3)
    canvas.create_rectangle(
        x+1, y+5, x+10, y+15, outline=color, fill=BG, width=1.3)


def draw_search_icon(canvas, x, y, color):
    canvas.create_oval(x+1, y+1, x+10, y+10, outline=color, width=1.3)
    canvas.create_line(
        x+8, y+8, x+14, y+14,
        fill=color, width=1.5, capstyle='round')


def draw_code_icon(canvas, x, y, color):
    canvas.create_line(
        x+5, y+3, x+1, y+8, x+5, y+13,
        fill=color, width=1.3, joinstyle='round', capstyle='round')
    canvas.create_line(
        x+10, y+3, x+14, y+8, x+10, y+13,
        fill=color, width=1.3, joinstyle='round', capstyle='round')


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
        try:
            self.win.attributes('-alpha', 0.97)
        except tk.TclError:
            pass
        self.win.geometry('+99999+99999')

        self.canvas = tk.Canvas(self.win, bg=BORDER_COLOR,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

        self.bar = tk.Frame(self.canvas, bg=BG)
        self._add_button('Copiar', draw_copy_icon, self._copy)
        self._add_button('Código', draw_code_icon, self._copy_code)
        self._add_button('Buscar', draw_search_icon, self._search)

        self.bar.update_idletasks()
        bw = self.bar.winfo_reqwidth()
        bh = self.bar.winfo_reqheight()
        cw = bw + 2 * PAD
        ch = bh + 2 * PAD
        self.canvas.configure(width=cw, height=ch)
        draw_rounded_rect(self.canvas, 0, 0, cw, ch, RADIUS, fill=BG)
        self.canvas.create_window(PAD, PAD, anchor='nw', window=self.bar,
                                  width=bw, height=bh)

        self.win.bind('<Escape>', lambda e: self.hide())
        self.win.update_idletasks()

    def _add_button(self, label, icon_fn, cmd):
        btn_frame = tk.Frame(self.bar, bg=BG, cursor='hand2')
        btn_frame.pack(side=tk.LEFT)
        icon = tk.Canvas(btn_frame, width=ICON_SIZE, height=ICON_SIZE,
                         bg=BG, highlightthickness=0)
        icon.pack(side=tk.LEFT, padx=(12, 5), pady=7)
        icon_fn(icon, 0, 0, TEXT)
        lbl = tk.Label(btn_frame, text=label, font=('Sans', 9),
                       fg=TEXT, bg=BG, cursor='hand2')
        lbl.pack(side=tk.LEFT, padx=(0, 12), pady=7)

        def redraw(c, color):
            c.delete('all')
            icon_fn(c, 0, 0, color)

        def on_enter(e, f=btn_frame, ic=icon, lb=lbl):
            for w in (f, ic, lb): w.configure(bg=BG_HOVER)
            lb.configure(fg=TEXT_HOVER)
            redraw(ic, TEXT_HOVER)
        def on_leave(e, f=btn_frame, ic=icon, lb=lbl):
            for w in (f, ic, lb): w.configure(bg=BG)
            lb.configure(fg=TEXT)
            redraw(ic, TEXT)

        for widget in (btn_frame, icon, lbl):
            widget.bind('<Button-1>', lambda e, c=cmd: c())
            widget.bind('<Enter>', on_enter)
            widget.bind('<Leave>', on_leave)

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

    def _copy_code(self):
        copy_to_clipboard(fix_wrapped_code(self.selected_text))
        self.hide()

    def _search(self):
        webbrowser.open('https://www.google.com/search?q='
                        + quote_plus(self.selected_text))


# --- Detección de mouse vía evdev ---

BTN_LEFT = 272
BTN_RIGHT = 273
CHECK_DELAY = 200     # ms después de soltar botón izquierdo
HIDE_AFTER = 3500     # ms antes de ocultar popup


def find_mouse_devices():
    """Encuentra todos los dispositivos con BTN_LEFT."""
    devices = []
    for path in sorted(glob.glob('/dev/input/event*')):
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities()
            if 1 in caps and BTN_LEFT in caps[1]:
                devices.append(dev)
            else:
                dev.close()
        except (PermissionError, OSError):
            pass
    return devices


# --- Tray icon vía KDE StatusNotifierItem (DBus) ---

class TrayIcon(dbus.service.Object):
    IFACE = 'org.kde.StatusNotifierItem'
    BUS_NAME = 'org.kde.StatusNotifierItem-popup-%d' % os.getpid()

    def __init__(self, icon_path, app):
        self.app = app
        self._bus = dbus.SessionBus()
        bus_name = dbus.service.BusName(self.BUS_NAME, self._bus,
                                        allow_replacement=True,
                                        replace_existing=True)
        super().__init__(bus_name, '/StatusNotifierItem')

        img = Image.open(icon_path).convert('RGBA').resize((48, 48))
        self._icon_pixmap = self._to_pixmap(img)
        self._empty_pixmap = dbus.Array([], signature='(iiay)')
        self._empty_tooltip = dbus.Struct(
            ('', self._empty_pixmap, 'Popup Linux', 'Click para cerrar'),
            signature=None)

        watcher = self._bus.get_object('org.kde.StatusNotifierWatcher',
                                       '/StatusNotifierWatcher')
        watcher.RegisterStatusNotifierItem(
            self.BUS_NAME,
            dbus_interface='org.kde.StatusNotifierWatcher')

    def _to_pixmap(self, img):
        w, h = img.size
        px = []
        for y in range(h):
            for x in range(w):
                r, g, b, a = img.getpixel((x, y))
                px += [dbus.Byte(a), dbus.Byte(r), dbus.Byte(g), dbus.Byte(b)]
        return dbus.Array([
            dbus.Struct((dbus.Int32(w), dbus.Int32(h),
                         dbus.Array(px, signature='y')), signature=None)
        ], signature='(iiay)')

    @dbus.service.method(IFACE, in_signature='ii')
    def Activate(self, x, y):
        self.app._quit()

    @dbus.service.method(IFACE, in_signature='ii')
    def SecondaryActivate(self, x, y):
        self.app._quit()

    @dbus.service.method(IFACE, in_signature='ii')
    def ContextMenu(self, x, y):
        self.app._quit()

    @dbus.service.method(IFACE, in_signature='is')
    def Scroll(self, delta, orientation):
        pass

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        props = self._props()
        if prop in props:
            return props[prop]
        raise dbus.exceptions.DBusException(
            'org.freedesktop.DBus.Error.UnknownProperty',
            'Unknown property: %s' % prop)

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self._props()

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature='ssv')
    def Set(self, interface, prop, value):
        pass

    def _props(self):
        return {
            'Category': 'ApplicationStatus',
            'Id': 'popup-linux',
            'Title': 'Popup Linux',
            'Status': 'Active',
            'WindowId': dbus.Int32(0),
            'IconName': '',
            'IconPixmap': self._icon_pixmap,
            'OverlayIconName': '',
            'OverlayIconPixmap': self._empty_pixmap,
            'AttentionIconName': '',
            'AttentionIconPixmap': self._empty_pixmap,
            'AttentionMovieName': '',
            'ToolTip': self._empty_tooltip,
            'Menu': dbus.ObjectPath('/NO_DBUSMENU'),
            'ItemIsMenu': dbus.Boolean(False),
            'IconThemePath': '',
        }


# --- App principal (event-driven) ---

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = PopupBar(self.root)
        self.paused = False
        self.previous_text = get_selected_text()
        self._hide_timer = None
        self._left_press_time = 0
        self._check_pending = False
        # Tkinter/Tcl no es thread-safe: el mouse y GLib threads solo
        # encolan acá, el main loop drena en _drain_events.
        self._event_queue = queue.Queue()
        self._start_mouse_thread()
        self._start_tray()
        self.root.after(20, self._drain_events)

    def _start_mouse_thread(self):
        devices = find_mouse_devices()
        if not devices:
            print('Aviso: no se encontraron dispositivos de mouse, usando polling')
            self._fallback_polling()
            return
        for d in devices:
            d.close()
        t = threading.Thread(target=self._mouse_loop, daemon=True)
        t.start()

    def _handle_input_event(self, event):
        if event.type != 1:  # EV_KEY
            return
        if event.code == BTN_LEFT and event.value == 1:
            self._event_queue.put(('left_press', time.time()))
        elif event.code == BTN_LEFT and event.value == 0:
            self._event_queue.put(('left_release', time.time()))
        elif event.code == BTN_RIGHT and event.value == 1:
            self._event_queue.put(('right_press', None))

    def _drain_events(self):
        try:
            while True:
                kind, payload = self._event_queue.get_nowait()
                if kind == 'left_press':
                    self._left_press_time = payload
                    self._check_pending = False
                    self.popup.hide()
                elif kind == 'left_release':
                    held = payload - self._left_press_time
                    if held > 0.15:
                        self._check_pending = True
                        self.root.after(CHECK_DELAY, self._check_selection)
                elif kind == 'right_press':
                    self._check_pending = False
                    self.popup.hide()
                elif kind == 'quit':
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(20, self._drain_events)

    def _mouse_loop(self):
        """Escucha todos los mouse en paralelo y se recupera de fallos.

        Si un dispositivo se desconecta (USB autosuspend, replug, suspensión
        del sistema), lo descarta y sigue con el resto. Cuando no quedan
        dispositivos, re-escanea cada pocos segundos hasta encontrar uno.
        """
        while True:
            devices = find_mouse_devices()
            if not devices:
                time.sleep(5)
                continue

            sel = selectors.DefaultSelector()
            for dev in devices:
                try:
                    sel.register(dev.fileno(), selectors.EVENT_READ, dev)
                except (OSError, ValueError) as e:
                    print(f'No se pudo registrar {dev.path}: {e}')
                    try:
                        dev.close()
                    except OSError:
                        pass

            try:
                while sel.get_map():
                    for key, _ in sel.select(timeout=30):
                        dev = key.data
                        try:
                            for event in dev.read():
                                self._handle_input_event(event)
                        except OSError as e:
                            print(f'Dispositivo {dev.path} falló ({e}), '
                                  'descartando')
                            try:
                                sel.unregister(key.fileobj)
                            except (KeyError, ValueError):
                                pass
                            try:
                                dev.close()
                            except OSError:
                                pass
            except Exception as e:
                print(f'Error inesperado en mouse loop: {e}')
            finally:
                for key in list(sel.get_map().values()):
                    try:
                        sel.unregister(key.fileobj)
                    except (KeyError, ValueError):
                        pass
                    try:
                        key.data.close()
                    except OSError:
                        pass
                sel.close()

            time.sleep(2)

    def _check_selection(self):
        if self.paused or not self._check_pending:
            return
        self._check_pending = False
        selected_text = get_selected_text()
        if selected_text != self.previous_text and selected_text.strip():
            cx, cy = get_cursor_position(self.root)
            self.popup.show(selected_text, cx, cy - 45)
            self.previous_text = selected_text
            self._cancel_hide_timer()
            self._hide_timer = self.root.after(HIDE_AFTER, self.popup.hide)

    def _cancel_hide_timer(self):
        if self._hide_timer is not None:
            self.root.after_cancel(self._hide_timer)
            self._hide_timer = None

    def _fallback_polling(self):
        """Polling lento como respaldo si evdev no funciona."""
        def poll():
            if not self.paused and not self.popup.visible:
                selected_text = get_selected_text()
                if selected_text != self.previous_text and selected_text.strip():
                    cx, cy = get_cursor_position(self.root)
                    self.popup.show(selected_text, cx, cy - 45)
                    self.previous_text = selected_text
                    self._cancel_hide_timer()
                    self._hide_timer = self.root.after(HIDE_AFTER, self.popup.hide)
            elif self.popup.visible and self._hide_timer is None:
                self._hide_timer = self.root.after(HIDE_AFTER, self.popup.hide)
            self.root.after(3000, poll)
        self.root.after(3000, poll)

    def _quit(self):
        self._event_queue.put(('quit', None))

    def _start_tray(self):
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 'popup-linux.png')
        if not os.path.exists(icon_path):
            create_tray_icon_image().save(icon_path)

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._sni = TrayIcon(icon_path, self)

        t = threading.Thread(target=GLib.MainLoop().run, daemon=True)
        t.start()

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


LOCK_FILE = '/tmp/popup-linux.lock'


def ensure_single_instance():
    """Si ya hay una instancia corriendo, salir."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # chequear si el proceso existe
            print(f'Popup Linux ya está corriendo (PID {pid})')
            raise SystemExit(0)
        except (ValueError, ProcessLookupError, OSError):
            pass  # PID inválido o proceso muerto, continuar
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))


def cleanup_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


if __name__ == '__main__':
    check_dependencies()
    ensure_single_instance()
    try:
        app = App()
        app.run()
    finally:
        cleanup_lock()
