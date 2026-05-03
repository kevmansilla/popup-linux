import os
os.environ['QT_LOGGING_RULES'] = '*.debug=false;qt.qpa.*=false'

import ast
import tkinter as tk
import subprocess
import shutil
import operator
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


# --- Detección contextual del contenido seleccionado ---

_URL_PROTO_RE = re.compile(r'^(https?|ftp|file)://\S+$', re.IGNORECASE)
_URL_BARE_RE = re.compile(
    r'^[a-z0-9][\w-]*(\.[a-z0-9][\w-]*)+(/\S*)?$', re.IGNORECASE)
_MATH_CHARS_RE = re.compile(r'^[\d\s+\-*/().]+$')
_SAFE_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def safe_eval(expr):
    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.operand))
        raise ValueError('expr no permitida')
    return _eval(ast.parse(expr, mode='eval').body)


_COMMON_TLDS = frozenset((
    'com org net edu gov mil int io co me ai app dev cloud tech '
    'info biz name site online store xyz '
    'ar es mx br cl pe uy us uk de fr it nl be ch at ru pl cz '
    'jp cn kr in au nz ca za tr ng eg sa tv fm ly gg to ws gl la'
).split())


def is_url(s):
    if _URL_PROTO_RE.match(s):
        return True
    if '@' in s or any(ch.isspace() for ch in s):
        return False
    if s.lower().startswith('www.'):
        return True
    if _URL_BARE_RE.match(s):
        host = s.split('/', 1)[0]
        tld = host.rsplit('.', 1)[-1].lower()
        return tld in _COMMON_TLDS
    return False


def is_path(s):
    if not (s.startswith(('/', '~/', './', '../')) or s == '~'):
        return False
    try:
        return os.path.exists(os.path.expanduser(s))
    except (OSError, ValueError):
        return False


def is_math(s):
    if not _MATH_CHARS_RE.match(s):
        return False
    if not any(op in s for op in '+-*/'):
        return False
    try:
        safe_eval(s)
        return True
    except Exception:
        return False


_ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_PROMPT_USER_RE = re.compile(r'^[\w.-]+@[\w.-]+:\S*[\$#%]\s+')
_PROMPT_SIMPLE_RE = re.compile(r'^[\$#%>]\s+')
_PS_PROMPT_RE = re.compile(r'^PS\s+[A-Za-z]:[^>]*>\s+')
_LINE_NUM_RE = re.compile(r'^\s*\d+\s*[|:│┃▏](\s?)(.*)$')


def _normalize_invisible(text):
    """CRLF→LF, NBSP→space, quita BOM y zero-width chars."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace(' ', ' ')           # NBSP
    text = text.replace(' ', '\n').replace(' ', '\n')  # line/para sep
    for ch in ('​', '‌', '‍', '﻿'):
        text = text.replace(ch, '')
    return _ANSI_RE.sub('', text)


def _normalize_quotes(text):
    """Smart quotes y dashes Unicode → ASCII."""
    return (text
            .replace('‘', "'").replace('’', "'")
            .replace('“', '"').replace('”', '"')
            .replace('–', '-').replace('—', '-')
            .replace('…', '...'))


def _strip_shell_prompts(text):
    """Quita prompts de shell al inicio de línea (PS1/PS2).

    Solo se aplica si al menos 2 líneas (no vacías) tienen prompt — evita
    tocar código que casualmente arranca con `$`.
    """
    lines = text.split('\n')
    matchers = (_PROMPT_USER_RE, _PS_PROMPT_RE, _PROMPT_SIMPLE_RE)
    hits = 0
    for line in lines:
        s = line.lstrip()
        if any(p.match(s) for p in matchers):
            hits += 1
    if hits < 2:
        return text
    out = []
    for line in lines:
        s = line.lstrip()
        for p in matchers:
            m = p.match(s)
            if m:
                out.append(s[m.end():])
                break
        else:
            out.append(line)
    return '\n'.join(out)


def _strip_line_numbers(text):
    """Quita prefijos de número de línea (cat -n, less +N, copy de IDE)."""
    lines = text.split('\n')
    matches = [_LINE_NUM_RE.match(line) for line in lines]
    hit = sum(1 for m in matches if m)
    if hit < 2 or hit < len([l for l in lines if l.strip()]) * 0.6:
        return text
    return '\n'.join(
        m.group(2) if m else line
        for m, line in zip(matches, lines)
    )


def _trim_trailing_ws(text):
    return '\n'.join(line.rstrip() for line in text.split('\n'))


def _heredoc_end(text, start):
    """Si en `start` arranca `<<MARKER` válido, devuelve la posición justo
    después del terminador. Si no es heredoc, devuelve None.

    Soporta `<<MARKER`, `<<-MARKER` (terminador con tabs líderes), y
    `<<'MARKER'`/`<<"MARKER"` (sin diferencia para nuestros fines).
    """
    n = len(text)
    j = start + 2
    dash = False
    if j < n and text[j] == '-':
        dash = True
        j += 1
    while j < n and text[j] in ' \t':
        j += 1
    quote = None
    if j < n and text[j] in ('"', "'"):
        quote = text[j]
        j += 1
    mstart = j
    while j < n and (text[j].isalnum() or text[j] == '_'):
        j += 1
    marker = text[mstart:j]
    if not marker:
        return None
    if quote:
        if j >= n or text[j] != quote:
            return None
        j += 1
    nl = text.find('\n', j)
    if nl < 0:
        return None
    pos = nl + 1
    while pos <= n:
        next_nl = text.find('\n', pos)
        line_end = next_nl if next_nl >= 0 else n
        line = text[pos:line_end]
        check = line.lstrip('\t') if dash else line
        if check == marker:
            return line_end
        if next_nl < 0:
            return None
        pos = next_nl + 1
    return None


def _join_continuations_and_wrap(text):
    """Resuelve continuaciones `\\<nl>` y newlines insertados por wrap.

    - `\\` + (espacios opcionales) + `\\n` + (indentación): fuera de string
      → un espacio; dentro de "..." o `...` → nada (igual que bash).
    - `\\` + ≥4 espacios sin `\\n` (terminal copió wrap sin newline) → idem.
    - `\\n` + indent dentro de string literal → vacío.
    - Triple comilla (\"\"\"/''') se preserva literal.
    - Single quotes ('...'): `\\` es literal, no se toca.
    """
    out = []
    i = 0
    n = len(text)
    state = None  # None | '"' | "'" | '`'
    in_comment = False

    while i < n:
        c = text[i]

        # Cierre de comentario al newline (todo dentro es literal)
        if in_comment:
            out.append(c)
            if c == '\n':
                in_comment = False
            i += 1
            continue

        # Inicio de comentario: # al inicio o después de whitespace/;
        if state is None and c == '#':
            prev = out[-1] if out else '\n'
            if prev in ' \t\n;':
                in_comment = True
                out.append(c)
                i += 1
                continue

        # Heredoc shell (<<MARKER, <<-MARKER, <<'MARKER', <<"MARKER")
        # Preserva el cuerpo verbatim hasta encontrar el terminador.
        if (state is None and c == '<' and i + 1 < n and text[i+1] == '<'
                and not (i + 2 < n and text[i+2] == '<')):
            end = _heredoc_end(text, i)
            if end is not None:
                out.append(text[i:end])
                i = end
                continue

        if state is None and i + 3 <= n and text[i:i+3] in ('"""', "'''"):
            triple = text[i:i+3]
            end = text.find(triple, i + 3)
            if end >= 0:
                out.append(text[i:end + 3])
                i = end + 3
            else:
                out.append(text[i:])
                i = n
            continue

        if c == '\\' and state != "'":
            j = i + 1
            while j < n and text[j] in ' \t':
                j += 1
            ws_after_bs = j - (i + 1)
            if j < n and text[j] == '\n':
                j += 1
                while j < n and text[j] in ' \t':
                    j += 1
                if state is None and out and out[-1] not in ' \t\n':
                    out.append(' ')
                i = j
                continue
            if ws_after_bs >= 4:
                if state is None and out and out[-1] not in ' \t\n':
                    out.append(' ')
                i = j
                continue

        if state is None and c in ('"', "'", '`'):
            state = c
            out.append(c)
            i += 1
            continue

        if state is not None:
            if c == '\\' and state in ('"', '`') and i + 1 < n:
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
                # Insertar espacio para no pegar palabras (SELECT users WHERE),
                # pero evitar romper URLs/paths (users?id, /api/v1)
                url_chars = '/?&=:.#'
                if (out and out[-1] not in ' \t' + url_chars
                        and i < n and text[i] != state
                        and text[i] not in url_chars):
                    out.append(' ')
                continue

        out.append(c)
        i += 1

    return ''.join(out)


def fix_wrapped_code(text):
    """Limpia código copiado desde terminales, IDEs, web o PDFs.

    Pipeline:
    1. Normaliza invisibles: CRLF→LF, NBSP→space, BOM/zero-width fuera,
       quita secuencias ANSI de color.
    2. Smart quotes (“”‘’), em/en dash, ellipsis → ASCII.
    3. Quita prompts de shell (`$ `, `# `, `> `, `user@host:~$ `, `PS C:\\>`)
       solo si aparecen en ≥2 líneas (no destruye código con `$` legítimo).
    4. Quita prefijos de número de línea (`  12 | ...`) si la mayoría de
       las líneas con texto los tienen.
    5. Trim de whitespace al final de cada línea.
    6. Resuelve continuaciones `\\<nl>` (incluso con espacios artefacto del
       wrap del terminal) y newlines insertados por wrap dentro de strings.
    """
    text = _normalize_invisible(text)
    text = _normalize_quotes(text)
    text = _strip_shell_prompts(text)
    text = _strip_line_numbers(text)
    text = _trim_trailing_ws(text)
    text = _join_continuations_and_wrap(text)
    return text


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
ICON_SIZE = 13
RADIUS = 9
PAD = 4


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
        x+4, y+1, x+12, y+9, outline=color, width=1.2)
    canvas.create_rectangle(
        x+1, y+4, x+9, y+12, outline=color, fill=BG, width=1.2)


def draw_search_icon(canvas, x, y, color):
    canvas.create_oval(x+1, y+1, x+9, y+9, outline=color, width=1.2)
    canvas.create_line(
        x+7, y+7, x+12, y+12,
        fill=color, width=1.4, capstyle='round')


def draw_code_icon(canvas, x, y, color):
    canvas.create_line(
        x+4, y+3, x+1, y+7, x+4, y+11,
        fill=color, width=1.2, joinstyle='round', capstyle='round')
    canvas.create_line(
        x+9, y+3, x+12, y+7, x+9, y+11,
        fill=color, width=1.2, joinstyle='round', capstyle='round')


def draw_open_icon(canvas, x, y, color):
    # Caja abajo-izq + flecha saliendo arriba-derecha
    canvas.create_rectangle(x+1, y+5, x+8, y+12, outline=color, width=1.2)
    canvas.create_line(x+5, y+8, x+12, y+1,
                       fill=color, width=1.4, capstyle='round')
    canvas.create_line(x+8, y+1, x+12, y+1, fill=color, width=1.2)
    canvas.create_line(x+12, y+1, x+12, y+5, fill=color, width=1.2)


def draw_calc_icon(canvas, x, y, color):
    # Signo "=" de doble línea
    canvas.create_line(x+2, y+5, x+11, y+5,
                       fill=color, width=1.4, capstyle='round')
    canvas.create_line(x+2, y+9, x+11, y+9,
                       fill=color, width=1.4, capstyle='round')


class PopupBar:
    def __init__(self, root):
        self.root = root
        self.selected_text = ''
        self.visible = False
        self._smart_kind = 'search'

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
        self._smart_btn = self._add_button(
            'Buscar', draw_search_icon, self._smart_action)

        self._reflow()
        self.win.bind('<Escape>', lambda e: self.hide())
        self.win.update_idletasks()

    def _add_button(self, label, icon_fn, cmd):
        btn = {'icon_fn': icon_fn, 'cmd': cmd}
        btn_frame = tk.Frame(self.bar, bg=BG, cursor='hand2')
        btn_frame.pack(side=tk.LEFT)
        icon = tk.Canvas(btn_frame, width=ICON_SIZE, height=ICON_SIZE,
                         bg=BG, highlightthickness=0)
        icon.pack(side=tk.LEFT, padx=(8, 3), pady=4)
        icon_fn(icon, 0, 0, TEXT)
        lbl = tk.Label(btn_frame, text=label, font=('Sans', 8),
                       fg=TEXT, bg=BG, cursor='hand2')
        lbl.pack(side=tk.LEFT, padx=(0, 8), pady=4)
        btn.update(frame=btn_frame, icon=icon, lbl=lbl)

        def on_enter(_e):
            for w in (btn_frame, icon, lbl):
                w.configure(bg=BG_HOVER)
            lbl.configure(fg=TEXT_HOVER)
            icon.delete('all')
            btn['icon_fn'](icon, 0, 0, TEXT_HOVER)

        def on_leave(_e):
            for w in (btn_frame, icon, lbl):
                w.configure(bg=BG)
            lbl.configure(fg=TEXT)
            icon.delete('all')
            btn['icon_fn'](icon, 0, 0, TEXT)

        def on_click(_e):
            btn['cmd']()

        for widget in (btn_frame, icon, lbl):
            widget.bind('<Button-1>', on_click)
            widget.bind('<Enter>', on_enter)
            widget.bind('<Leave>', on_leave)
        return btn

    def _update_button(self, btn, label, icon_fn):
        btn['icon_fn'] = icon_fn
        btn['lbl'].configure(text=label)
        btn['icon'].delete('all')
        icon_fn(btn['icon'], 0, 0, TEXT)

    def _reflow(self):
        """Recalcula tamaño del canvas según el contenido del bar."""
        self.bar.update_idletasks()
        bw = self.bar.winfo_reqwidth()
        bh = self.bar.winfo_reqheight()
        cw = bw + 2 * PAD
        ch = bh + 2 * PAD
        self.canvas.configure(width=cw, height=ch)
        self.canvas.delete('all')
        draw_rounded_rect(self.canvas, 0, 0, cw, ch, RADIUS, fill=BG)
        self.canvas.create_window(PAD, PAD, anchor='nw', window=self.bar,
                                  width=bw, height=bh)

    def _detect_smart(self, text):
        s = text.strip()
        if not s:
            return ('search', 'Buscar', draw_search_icon)
        if is_url(s):
            return ('open_url', 'Abrir', draw_open_icon)
        if is_path(s):
            return ('open_path', 'Abrir', draw_open_icon)
        if is_math(s):
            return ('calc', 'Calcular', draw_calc_icon)
        return ('search', 'Buscar', draw_search_icon)

    def show(self, text, x, y):
        self.selected_text = text
        kind, label, icon_fn = self._detect_smart(text)
        self._smart_kind = kind
        self._update_button(self._smart_btn, label, icon_fn)
        self._reflow()
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

    def _smart_action(self):
        s = self.selected_text.strip()
        if self._smart_kind == 'open_url':
            url = s if '://' in s else 'https://' + s
            webbrowser.open(url)
        elif self._smart_kind == 'open_path':
            subprocess.Popen(
                ['xdg-open', os.path.expanduser(s)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        elif self._smart_kind == 'calc':
            try:
                result = safe_eval(s)
                # Mostrar entero si el resultado es entero exacto
                if isinstance(result, float) and result.is_integer():
                    result = int(result)
                copy_to_clipboard(f'{s} = {result}')
            except Exception:
                copy_to_clipboard(s)
        else:
            webbrowser.open('https://www.google.com/search?q='
                            + quote_plus(self.selected_text))
        self.hide()


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
        # Tkinter/Tcl no es thread-safe: el mouse y GLib threads solo
        # encolan acá, el main loop drena en _drain_events.
        self._event_queue = queue.Queue()
        self.popup = PopupBar(self.root)
        self.paused = False
        self.previous_text = get_selected_text()
        self._hide_timer = None
        self._left_press_time = 0
        self._last_release_time = 0
        self._check_pending = False
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
                    since_last = payload - self._last_release_time
                    self._last_release_time = payload
                    # Drag (>150ms) o doble/triple click (<400ms entre releases)
                    if held > 0.15 or since_last < 0.4:
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
        if selected_text.strip():
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
