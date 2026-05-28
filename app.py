"""
历史粘接 — Windows 剪贴板历史管理器
=====================================

后台静默记录所有复制过的文字和图片，支持搜索、置顶、一键复用。

功能：
  - 自动监控剪贴板（文字 + 图片）
  - SQLite 持久化存储，支持自动清理
  - 分类筛选（全部 / 文字 / 图片）
  - 关键词搜索
  - 置顶 / 删除
  - 窗口透明 + 置顶
  - 系统托盘最小化
  - 全局热键 Ctrl+Shift+V
  - 开机自启（注册表）
  - 单实例运行（Windows Mutex）

技术栈：Python 3 + tkinter + SQLite + Pillow + pystray + keyboard

作者：ZDQ9276
许可：MIT
"""
import tkinter as tk
from tkinter import ttk, filedialog
import sqlite3
import os
import sys
import time
import hashlib
import ctypes
import threading
import winreg
from datetime import datetime, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────
APP_DIR = Path(os.environ.get('APPDATA', '.')) / 'clipboard-manager'
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = str(APP_DIR / 'clipboard.db')
IMAGES_DIR = APP_DIR / 'images'
IMAGES_DIR.mkdir(exist_ok=True)
LOG_PATH = APP_DIR / 'app.log'
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.png')

def log(msg):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'[{datetime.now().isoformat()}] {msg}\n')
    except: pass

# ── Single-instance via named mutex ─────────────────────────────────
def check_single_instance():
    """Windows named mutex for reliable single-instance detection."""
    try:
        kernel32 = ctypes.windll.kernel32
        mutex_name = "Global\\ClipboardManagerApp_HistoryManager"
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            if handle:
                kernel32.CloseHandle(handle)
            return False
        return True
    except:
        return True  # On failure, allow to start

if not check_single_instance():
    # Another instance is running — signal it to show window, then exit
    try:
        (APP_DIR / 'show.signal').write_text('1')
    except: pass
    sys.exit(0)

# ── Database ───────────────────────────────────────────────────────
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self):
        self.conn.execute('''CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT,
            image_path TEXT,
            pinned INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )''')
        self.conn.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        for key, val in [('retention', '3'), ('opacity', '90'), ('topmost', '0')]:
            cur = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,))
            if not cur.fetchone():
                self.conn.execute("INSERT INTO settings (key,value) VALUES (?,?)", (key, val))
        self.conn.commit()

    def add_item(self, type_, content, image_path):
        self.conn.execute(
            'INSERT INTO items (type, content, image_path) VALUES (?,?,?)',
            (type_, content, image_path)
        )
        self.cleanup()
        self.conn.commit()

    def get_items(self, filter_='all', search=''):
        q = 'SELECT * FROM items WHERE 1=1'
        params = []
        if filter_ == 'text':
            q += " AND type='text'"
        elif filter_ == 'image':
            q += " AND type='image'"
        if search:
            q += " AND type='text' AND content LIKE ?"
            params.append(f'%{search}%')
        q += ' ORDER BY pinned DESC, created_at DESC LIMIT 500'
        cur = self.conn.execute(q, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def pin_item(self, id_, pinned):
        self.conn.execute('UPDATE items SET pinned=? WHERE id=?', (1 if pinned else 0, id_))
        self.conn.commit()

    def delete_item(self, id_):
        cur = self.conn.execute('SELECT image_path FROM items WHERE id=?', (id_,))
        row = cur.fetchone()
        if row and row[0]:
            try: os.unlink(row[0])
            except: pass
        self.conn.execute('DELETE FROM items WHERE id=?', (id_,))
        self.conn.commit()

    def get_setting(self, key):
        cur = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        self.conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (key, value))
        self.conn.commit()
        if key == 'retention':
            self.cleanup()

    def cleanup(self):
        days = int(self.get_setting('retention') or '3')
        self.conn.execute(
            "DELETE FROM items WHERE pinned=0 AND datetime(created_at, '+{} days') < datetime('now','localtime')".format(days)
        )
        self.conn.commit()

db = Database()

# ── Clipboard I/O ──────────────────────────────────────────────────
last_text = ''
last_image_hash = ''
skip_next = False  # Set when app modifies clipboard to avoid self-detection

def get_clipboard_image():
    """Get image from Windows clipboard using PIL ImageGrab."""
    try:
        from PIL import ImageGrab, Image
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            return img
        if isinstance(img, list) and len(img) > 0:
            try:
                return Image.open(img[0])
            except: pass
    except Exception as e:
        log(f'Image clipboard read error: {e}')
    return None

def set_clipboard_text(text):
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except: return False

def set_clipboard_image(image_path):
    """Set image file to Windows clipboard as DIB."""
    try:
        from PIL import Image
        import struct

        img = Image.open(image_path)
        if img.mode != 'RGBA':
            img = img.convert('RGBA')

        width, height = img.size
        pixel_data = img.tobytes('raw', 'BGRA', 0, 1)
        stride = ((width * 32 + 31) // 32) * 4
        rows = []
        for y in range(height - 1, -1, -1):
            rows.append(pixel_data[y * stride:(y + 1) * stride])
        flipped = b''.join(rows)

        biSize = 40
        dib_header = struct.pack('<IiiHHIIiiII',
            biSize, width, height, 1, 32, 0, len(flipped), 0, 0, 0, 0)
        dib_data = dib_header + flipped

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_DIB = 8

        if not user32.OpenClipboard(0):
            return False
        try:
            user32.EmptyClipboard()
            hMem = kernel32.GlobalAlloc(0x0002, len(dib_data))
            if not hMem:
                return False
            ptr = kernel32.GlobalLock(hMem)
            ctypes.memmove(ptr, dib_data, len(dib_data))
            kernel32.GlobalUnlock(hMem)
            user32.SetClipboardData(CF_DIB, hMem)
        finally:
            user32.CloseClipboard()
        return True
    except Exception as e:
        log(f'Set clipboard image error: {e}')
        return False

# ── Clipboard Monitor ──────────────────────────────────────────────
def monitor_clipboard():
    """Poll clipboard for changes (runs in daemon thread)."""
    global last_text, last_image_hash, skip_next
    while True:
        try:
            # If app just modified clipboard, skip this cycle
            if skip_next:
                skip_next = False
                time.sleep(0.8)
                continue

            # Check text
            try:
                import pyperclip
                text = pyperclip.paste()
                if text and isinstance(text, str):
                    text = text.strip()
                    if text and text != last_text:
                        last_text = text
                        db.add_item('text', text[:10000], None)
                        if app_ui:
                            app_ui.after(0, app_ui.refresh)
            except Exception:
                pass

            # Check image
            try:
                img = get_clipboard_image()
                if img is not None:
                    img_rgb = img.convert('RGB')
                    h = hashlib.md5(img_rgb.tobytes()).hexdigest()
                    if h != last_image_hash:
                        last_image_hash = h
                        filename = f'{int(time.time()*1000)}.png'
                        filepath = str(IMAGES_DIR / filename)
                        img.save(filepath, 'PNG')
                        db.add_item('image', None, filepath)
                        if app_ui:
                            app_ui.after(0, app_ui.refresh)
            except Exception:
                pass

            time.sleep(0.8)
        except Exception:
            time.sleep(0.8)

app_ui = None

# ── Global Hotkey ──────────────────────────────────────────────────
def start_hotkey_listener():
    try:
        import keyboard
        keyboard.add_hotkey('ctrl+shift+v', lambda: app_ui and app_ui.after(0, app_ui.toggle_visible))
        log('Hotkey registered: Ctrl+Shift+V')
    except Exception as e:
        log(f'Hotkey error: {e}')

# ── System Tray ────────────────────────────────────────────────────
tray_icon = None

def create_tray_icon():
    global tray_icon
    try:
        from PIL import Image
        import pystray

        if os.path.exists(ICON_PATH):
            img = Image.open(ICON_PATH).resize((32, 32), Image.LANCZOS)
        else:
            from PIL import ImageDraw
            img = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([4, 4, 28, 28], fill=(59, 130, 246, 255))

        def on_show(icon, item=None):
            if app_ui: app_ui.after(0, app_ui.show_window)
        def on_exit(icon, item=None):
            global tray_icon
            tray_icon.stop()
            if app_ui: app_ui.after(0, app_ui.quit_app)

        menu = pystray.Menu(
            pystray.MenuItem('显示历史粘接', on_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出', on_exit)
        )
        tray_icon = pystray.Icon('clipboard_manager', img, '历史粘接 - 剪贴板管理器', menu)
        tray_icon.run_detached()
        log('Tray icon created')
    except Exception as e:
        log(f'Tray error: {e}')

# ── Auto-start ─────────────────────────────────────────────────────
def set_autostart(enable):
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run',
            0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        if enable:
            exe_path = sys.executable
            script_path = os.path.abspath(__file__)
            winreg.SetValueEx(key, 'ClipboardManager', 0, winreg.REG_SZ,
                              f'"{exe_path}" "{script_path}"')
        else:
            try: winreg.DeleteValue(key, 'ClipboardManager')
            except: pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        log(f'Auto-start error: {e}')
        return False

def get_autostart():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Run',
            0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, 'ClipboardManager')
            winreg.CloseKey(key)
            return True
        except:
            winreg.CloseKey(key)
            return False
    except:
        return False

# ── UI ─────────────────────────────────────────────────────────────
class AppUI:
    # Color scheme
    C_PRIMARY = '#3b82f6'       # Blue-500
    C_PRIMARY_DARK = '#2563eb'  # Blue-600
    C_PRIMARY_LIGHT = '#eff6ff' # Blue-50
    C_PRIMARY_BG = '#f0f5ff'    # Light blue background
    C_BG = '#f8fafc'            # Slate-50
    C_WHITE = '#ffffff'
    C_TEXT = '#1e293b'          # Slate-800
    C_TEXT_SEC = '#64748b'      # Slate-500
    C_TEXT_MUTED = '#94a3b8'    # Slate-400
    C_BORDER = '#e2e8f0'        # Slate-200
    C_DANGER = '#ef4444'
    C_DANGER_BG = '#fef2f2'
    C_TOAST_BG = '#1e293b'

    def __init__(self):
        self.root = tk.Tk()
        self.root.title('历史粘接')
        self.root.geometry('400x520')
        self.root.minsize(280, 320)
        self.root.configure(bg=self.C_BG)

        # Window icon
        if os.path.exists(ICON_PATH):
            try:
                from PIL import Image, ImageTk
                ico = Image.open(ICON_PATH).resize((48, 48), Image.LANCZOS)
                self._icon_photo = ImageTk.PhotoImage(ico)
                self.root.iconphoto(True, self._icon_photo)
            except: pass

        self.root.protocol('WM_DELETE_WINDOW', self.hide_window)

        self.filter_var = tk.StringVar(value='all')
        self.search_var = tk.StringVar()
        self.retention_var = tk.StringVar(value=db.get_setting('retention') or '3')
        self.autostart_var = tk.BooleanVar(value=get_autostart())
        self.opacity_var = tk.IntVar(value=int(db.get_setting('opacity') or '90'))
        self.topmost_var = tk.BooleanVar(value=(db.get_setting('topmost') == '1'))

        self.items = []
        self._card_widgets = {}

        self.root.attributes('-alpha', self.opacity_var.get() / 100.0)
        self.root.attributes('-topmost', self.topmost_var.get())
        self._build_ui()
        self.refresh()

        # Poll for show-window signal from another instance
        self._poll_show_signal()

    # ── Build UI ──────────────────────────────────────────────────
    def _build_ui(self):
        self._build_header()
        self._build_panel()
        self._build_list()
        self._build_bottom()
        self._build_toast()

        # Keyboard shortcuts
        self.root.bind('<Escape>', lambda e: self.hide_window())
        self.root.bind('<Control-f>', lambda e: self._toggle_panel())

    # ── Header: search toggle + filter tabs (compact, always visible) ─
    def _build_header(self):
        bar = tk.Frame(self.root, bg=self.C_WHITE, height=34)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        # Search toggle button (small)
        self.panel_toggle = tk.Label(bar, text='🔍', bg=self.C_WHITE,
                                      fg=self.C_TEXT_SEC, font=('Segoe UI', 12),
                                      cursor='hand2', padx=10)
        self.panel_toggle.pack(side=tk.LEFT, fill=tk.Y)
        self.panel_toggle.bind('<Button-1>', lambda e: self._toggle_panel())
        self.panel_toggle.bind('<Enter>', lambda e: self.panel_toggle.configure(fg=self.C_PRIMARY))
        self.panel_toggle.bind('<Leave>', lambda e: self.panel_toggle.configure(
            fg=self.C_PRIMARY if self._panel_visible else self.C_TEXT_SEC))

        # Filter tabs
        self._tab_indicators = {}
        tabs = [('all', '全部'), ('text', '文字'), ('image', '图片')]
        for val, label in tabs:
            tab_frame = tk.Frame(bar, bg=self.C_WHITE)
            tab_frame.pack(side=tk.LEFT)

            btn = tk.Label(tab_frame, text=label,
                           font=('Microsoft YaHei', 10),
                           bg=self.C_WHITE, fg=self.C_TEXT_SEC,
                           cursor='hand2', padx=10, pady=7)
            btn.pack(side=tk.TOP)
            btn.bind('<Button-1>', lambda e, v=val: self.set_filter(v))
            btn.bind('<Enter>', lambda e, b=btn, v=val:
                     b.configure(fg=self.C_PRIMARY) if self.filter_var.get() != v else None)
            btn.bind('<Leave>', lambda e, b=btn, v=val:
                     b.configure(fg=self.C_TEXT_SEC) if self.filter_var.get() != v else None)

            indicator = tk.Frame(tab_frame, bg=self.C_PRIMARY, height=2)
            setattr(self, f'tab_btn_{val}', btn)
            self._tab_indicators[val] = indicator

        self._update_tab_style()

        # Bottom separator
        tk.Frame(self.root, bg=self.C_BORDER, height=1).pack(fill=tk.X)

    # ── Collapsible panel: search + toolbar ──────────────────────────
    def _build_panel(self):
        self._panel_visible = False
        self.panel_frame = tk.Frame(self.root, bg=self.C_WHITE)

        # Thin separator above panel content
        sep = tk.Frame(self.panel_frame, bg=self.C_BORDER, height=1)
        sep.pack(fill=tk.X)

        # Search row (slim)
        sf = tk.Frame(self.panel_frame, bg=self.C_WHITE, height=34)
        sf.pack(fill=tk.X)
        sf.pack_propagate(False)

        sinner = tk.Frame(sf, bg=self.C_WHITE)
        sinner.pack(fill=tk.BOTH, padx=10, pady=3)

        tk.Label(sinner, text='🔍', bg=self.C_WHITE, font=('Segoe UI', 9),
                 fg=self.C_TEXT_MUTED).pack(side=tk.LEFT, padx=(6, 4))

        self.search_entry = tk.Entry(sinner, textvariable=self.search_var,
                                      font=('Microsoft YaHei', 10),
                                      bg=self.C_WHITE, fg=self.C_TEXT,
                                      relief=tk.FLAT, bd=0)
        self.search_entry.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._placeholder = '搜索剪贴板历史...'
        self._placeholder_active = False

        def on_focus_in(e):
            if self._placeholder_active:
                self._show_placeholder(False)
        def on_focus_out(e):
            if self.search_var.get().strip() == '':
                self._show_placeholder(True)

        self.search_entry.bind('<FocusIn>', on_focus_in)
        self.search_entry.bind('<FocusOut>', on_focus_out)
        self._show_placeholder(True)

        self.search_clear = tk.Label(sinner, text='✕', bg=self.C_WHITE, fg=self.C_TEXT_MUTED,
                                      font=('Segoe UI', 10), cursor='hand2')
        self.search_clear.bind('<Button-1>', lambda e: self._clear_search())
        self.search_var.trace_add('write', lambda *a: self._on_search_change())

        # Toolbar row (slim)
        tf = tk.Frame(self.panel_frame, bg=self.C_WHITE, height=30)
        tf.pack(fill=tk.X)
        tf.pack_propagate(False)

        # Transparency
        left_section = tk.Frame(tf, bg=self.C_WHITE)
        left_section.pack(side=tk.LEFT, padx=(12, 0), fill=tk.Y)

        tk.Label(left_section, text='透明', bg=self.C_WHITE, fg=self.C_TEXT_MUTED,
                 font=('Microsoft YaHei', 8)).pack(side=tk.LEFT, padx=(0, 4))

        self.opacity_scale = tk.Scale(left_section, from_=25, to=100,
                                       orient=tk.HORIZONTAL, variable=self.opacity_var,
                                       bg=self.C_WHITE, fg=self.C_PRIMARY,
                                       highlightthickness=0, bd=0,
                                       length=70, showvalue=False,
                                       troughcolor=self.C_BORDER,
                                       activebackground=self.C_PRIMARY,
                                       command=self._on_opacity_change)
        self.opacity_scale.pack(side=tk.LEFT)

        self.opacity_pct = tk.Label(left_section, text=f'{self.opacity_var.get()}%',
                                     bg=self.C_WHITE, fg=self.C_TEXT_SEC,
                                     font=('Microsoft YaHei', 8), width=3)
        self.opacity_pct.pack(side=tk.LEFT, padx=(2, 0))

        # Topmost
        self.topmost_btn = tk.Button(tf, text='📌 置顶', bg=self.C_WHITE,
                                      fg=self.C_TEXT_SEC, font=('Microsoft YaHei', 8),
                                      bd=0, cursor='hand2', padx=8,
                                      activebackground=self.C_PRIMARY_LIGHT,
                                      command=self._on_topmost_toggle)
        self.topmost_btn.pack(side=tk.RIGHT, padx=(0, 10))
        self._update_topmost_btn()

    def _toggle_panel(self):
        if self._panel_visible:
            self.panel_frame.pack_forget()
            self._panel_visible = False
            self.panel_toggle.configure(fg=self.C_TEXT_SEC)
        else:
            self.panel_frame.pack(fill=tk.X, after=self.panel_toggle.master)
            self._panel_visible = True
            self.panel_toggle.configure(fg=self.C_PRIMARY)
            self.search_entry.focus_set()

    def _show_placeholder(self, show):
        if show:
            self._placeholder_active = True
            self.search_entry.delete(0, tk.END)
            self.search_entry.insert(0, self._placeholder)
            self.search_entry.configure(fg=self.C_TEXT_MUTED)
        else:
            if self._placeholder_active:
                self.search_entry.delete(0, tk.END)
                self._placeholder_active = False
            self.search_entry.configure(fg=self.C_TEXT)

    def _clear_search(self):
        self.search_var.set('')
        self._show_placeholder(True)
        self.refresh()

    def _on_search_change(self):
        val = self.search_var.get().strip()
        if val and val != self._placeholder:
            if self._placeholder_active and val != self._placeholder:
                self._placeholder_active = False
                self.search_entry.configure(fg=self.C_TEXT)
            self.search_clear.pack(side=tk.RIGHT, padx=(2, 2))
        else:
            if not self._placeholder_active:
                self.search_clear.pack_forget()
        self.refresh()

    def set_filter(self, val):
        self.filter_var.set(val)
        self._update_tab_style()
        self.refresh()

    def _update_tab_style(self):
        current = self.filter_var.get()
        for val in ['all', 'text', 'image']:
            btn = getattr(self, f'tab_btn_{val}')
            indicator = self._tab_indicators[val]
            if val == current:
                btn.configure(fg=self.C_PRIMARY, font=('Microsoft YaHei', 10, 'bold'))
                indicator.pack(fill=tk.X)
            else:
                btn.configure(fg=self.C_TEXT_SEC, font=('Microsoft YaHei', 10))
                indicator.pack_forget()

    def _build_list(self):
        container = tk.Frame(self.root, bg=self.C_BG)
        container.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(container, bg=self.C_BG, highlightthickness=0,
                                bd=0)
        self.scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL,
                                        command=self.canvas.yview)
        self.card_frame = tk.Frame(self.canvas, bg=self.C_BG)

        self.card_frame.bind('<Configure>',
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))

        self._canvas_window = self.canvas.create_window((0, 0), window=self.card_frame,
                                                         anchor=tk.NW)

        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=4)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 2), pady=4)

        # Bind canvas width so cards match
        self.canvas.bind('<Configure>', self._on_canvas_resize)

        # Mouse wheel
        def on_mousewheel(e):
            self.canvas.yview_scroll(-1 * (e.delta // 120), 'units')
        self.canvas.bind_all('<MouseWheel>', on_mousewheel)
        self.canvas.bind('<Enter>', lambda e: self.canvas.focus_set())

        # Empty state
        self.empty_frame = tk.Frame(container, bg=self.C_BG)
        self.empty_icon = tk.Label(self.empty_frame, text='📋',
                                    font=('Segoe UI', 48), bg=self.C_BG, fg=self.C_TEXT_MUTED)
        self.empty_icon.pack(pady=(0, 8))
        self.empty_label = tk.Label(self.empty_frame,
                                     text='还没有复制的记录\n试着复制一些文字或图片吧',
                                     font=('Microsoft YaHei', 11),
                                     bg=self.C_BG, fg=self.C_TEXT_MUTED)
        self.empty_label.pack()

    def _on_canvas_resize(self, event):
        w = event.width
        self.canvas.itemconfig(self._canvas_window, width=w)

    def _build_bottom(self):
        # Separator
        tk.Frame(self.root, bg=self.C_BORDER, height=1).pack(fill=tk.X, side=tk.BOTTOM)

        frame = tk.Frame(self.root, bg=self.C_WHITE, height=30)
        frame.pack(fill=tk.X, side=tk.BOTTOM)
        frame.pack_propagate(False)

        # Left: retention
        tk.Label(frame, text='保存', bg=self.C_WHITE, fg=self.C_TEXT_MUTED,
                 font=('Microsoft YaHei', 8)).pack(side=tk.LEFT, padx=(12, 3))

        cb = ttk.Combobox(frame, textvariable=self.retention_var,
                          values=['1', '3', '5'], state='readonly',
                          width=2, font=('Microsoft YaHei', 9))
        cb.pack(side=tk.LEFT)
        cb.bind('<<ComboboxSelected>>', lambda e: self.on_retention_change())

        tk.Label(frame, text='天', bg=self.C_WHITE, fg=self.C_TEXT_MUTED,
                 font=('Microsoft YaHei', 8)).pack(side=tk.LEFT)

        # Center: count
        self.count_label = tk.Label(frame, text='',
                                     bg=self.C_WHITE, fg=self.C_TEXT_MUTED,
                                     font=('Microsoft YaHei', 8))
        self.count_label.pack(side=tk.LEFT, padx=14)

        # Right: auto-start
        autostart_cb = tk.Checkbutton(frame, text='开机启动',
                                       variable=self.autostart_var,
                                       bg=self.C_WHITE, fg=self.C_TEXT_MUTED,
                                       font=('Microsoft YaHei', 8),
                                       activebackground=self.C_WHITE,
                                       selectcolor=self.C_WHITE,
                                       command=self.on_autostart_change)
        autostart_cb.pack(side=tk.RIGHT, padx=10)

    def _build_toast(self):
        self.toast_frame = tk.Frame(self.root, bg=self.C_TOAST_BG)
        self.toast_label = tk.Label(self.toast_frame, text='', bg=self.C_TOAST_BG,
                                     fg='white', font=('Microsoft YaHei', 10),
                                     padx=20, pady=8)

    # ── Refresh ────────────────────────────────────────────────────
    def refresh(self):
        """Rebuild all cards from database."""
        for w in self.card_frame.winfo_children():
            w.destroy()
        self._card_widgets = {}

        filter_ = self.filter_var.get()
        search = self.search_var.get().strip()
        if search == self._placeholder:
            search = ''
        self.items = db.get_items(filter_, search)

        if not self.items:
            self.empty_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        else:
            self.empty_frame.place_forget()
            for item in self.items:
                card = self._create_card(item)
                card.pack(fill=tk.X, padx=0, pady=(0, 6))

        # Update count
        total = len(self.items)
        self.count_label.configure(text=f'共 {total} 条记录' if total > 0 else '')

        self.card_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _create_card(self, item):
        pinned = item['pinned'] == 1
        bg = '#fefefe' if not pinned else '#f8faff'
        border = self.C_PRIMARY if pinned else self.C_BORDER

        # Outer frame for shadow/border effect
        card = tk.Frame(self.card_frame, bg=self.C_BG, bd=0)
        inner = tk.Frame(card, bg=bg, bd=0,
                         highlightbackground=border,
                         highlightthickness=1,
                         highlightcolor=border)

        # ── Card content ──
        # Time
        time_str = self._format_time(item['created_at'])
        time_lbl = tk.Label(inner, text=time_str, bg=bg, fg=self.C_TEXT_MUTED,
                            font=('Microsoft YaHei', 9), anchor=tk.W)
        time_lbl.pack(fill=tk.X, padx=12, pady=(10, 0))
        time_lbl.configure(cursor='hand2')

        # Content
        if item['type'] == 'text':
            txt = item['content'] or ''
            if len(txt) > 400:
                txt = txt[:400] + '…'
            wl = max(180, self.canvas.winfo_width() - 72)
            content_lbl = tk.Label(inner, text=txt, bg=bg, fg=self.C_TEXT,
                                   font=('Microsoft YaHei', 10),
                                   wraplength=wl, justify=tk.LEFT, anchor=tk.W)
            content_lbl.pack(fill=tk.X, padx=12, pady=(6, 10))
            content_lbl.configure(cursor='hand2')
        else:
            try:
                from PIL import Image, ImageTk
                img = Image.open(item['image_path'])
                tw = max(180, self.canvas.winfo_width() - 72)
                img.thumbnail((tw, 170), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                img_lbl = tk.Label(inner, image=photo, bg=bg, cursor='hand2')
                img_lbl.image = photo
                img_lbl.pack(padx=12, pady=(6, 10))
            except Exception:
                tk.Label(inner, text='[图片加载失败]', bg=bg, fg=self.C_TEXT_MUTED,
                         font=('Microsoft YaHei', 10)).pack(padx=12, pady=(6, 10))

        # Action bar
        actions = tk.Frame(inner, bg=bg)
        actions.pack(fill=tk.X, padx=8, pady=(0, 8))

        # Pin button
        pin_text = '📌 取消置顶' if pinned else '📌 置顶'
        pin_btn = tk.Button(actions, text=pin_text,
                            font=('Microsoft YaHei', 9),
                            bg=bg, fg=self.C_PRIMARY, bd=0,
                            cursor='hand2',
                            activebackground=self.C_PRIMARY_LIGHT,
                            activeforeground=self.C_PRIMARY,
                            command=lambda id=item['id'], p=pinned: self.on_pin(id, p))
        pin_btn.pack(side=tk.LEFT, padx=(2, 0))

        # Save button (images only)
        if item['type'] == 'image':
            save_btn = tk.Button(actions, text='💾 保存',
                                 font=('Microsoft YaHei', 9),
                                 bg=bg, fg=self.C_PRIMARY, bd=0,
                                 cursor='hand2',
                                 activebackground=self.C_PRIMARY_LIGHT,
                                 activeforeground=self.C_PRIMARY,
                                 command=lambda p=item['image_path']: self.on_save_image(p))
            save_btn.pack(side=tk.LEFT, padx=(4, 0))

        # Delete button
        del_btn = tk.Button(actions, text='🗑 删除',
                            font=('Microsoft YaHei', 9),
                            bg=bg, fg=self.C_TEXT_SEC, bd=0,
                            cursor='hand2',
                            activebackground=self.C_DANGER_BG,
                            activeforeground=self.C_DANGER,
                            command=lambda id=item['id']: self.on_delete(id))
        del_btn.pack(side=tk.RIGHT, padx=(0, 2))

        # ── Click-to-copy bindings ──
        for widget in [inner, time_lbl]:
            widget.bind('<Button-1>', lambda e, it=item: self.on_copy(it))
        if item['type'] == 'text':
            content_lbl.bind('<Button-1>', lambda e, it=item: self.on_copy(it))
        elif item['type'] == 'image':
            try:
                img_lbl.bind('<Button-1>', lambda e, it=item: self.on_copy(it))
            except: pass

        # Hover effect
        def on_enter(e, f=inner, b=bg):
            if not pinned:
                f.configure(bg='#f0f7ff')
        def on_leave(e, f=inner, b=bg):
            f.configure(bg=bg)

        inner.bind('<Enter>', on_enter)
        inner.bind('<Leave>', on_leave)

        inner.pack(fill=tk.X, padx=6, pady=0)
        return card

    def _format_time(self, date_str):
        if not date_str: return ''
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
            now = datetime.now()
            diff = (now - d).total_seconds()
            if diff < 60:
                return '刚刚'
            elif diff < 3600:
                return f'{int(diff // 60)} 分钟前'
            elif diff < 86400:
                return f'{int(diff // 3600)} 小时前'
            elif diff < 172800:
                return f'昨天 {d.strftime("%H:%M")}'
            else:
                return d.strftime('%m-%d %H:%M')
        except:
            return date_str

    # ── Actions ────────────────────────────────────────────────────
    def on_copy(self, item):
        global skip_next
        if item['type'] == 'text':
            set_clipboard_text(item['content'])
            skip_next = True
            self.show_toast(f'已复制: {item["content"][:40]}…' if len(item['content'] or '') > 40 else f'已复制: {item["content"]}')
        elif item['type'] == 'image' and item.get('image_path'):
            ok = set_clipboard_image(item['image_path'])
            skip_next = True
            self.show_toast('图片已复制到剪贴板' if ok else '图片复制失败')

    def on_pin(self, id_, current_pinned):
        db.pin_item(id_, not current_pinned)
        self.refresh()

    def on_delete(self, id_):
        db.delete_item(id_)
        self.refresh()
        self.show_toast('已删除')

    def on_save_image(self, image_path):
        dest = filedialog.asksaveasfilename(
            title='保存图片',
            defaultextension='.png',
            filetypes=[('PNG 图片', '*.png'), ('JPEG 图片', '*.jpg')],
            initialfile=f'clipboard_{int(time.time())}.png')
        if dest:
            try:
                import shutil
                shutil.copy2(image_path, dest)
                self.show_toast('图片已保存')
            except Exception:
                self.show_toast('保存失败')

    def on_retention_change(self):
        db.set_setting('retention', self.retention_var.get())
        self.show_toast(f'保存天数已更新为 {self.retention_var.get()} 天')

    def on_autostart_change(self):
        ok = set_autostart(self.autostart_var.get())
        self.show_toast('已开启开机自启' if self.autostart_var.get() else '已关闭开机自启')

    def _on_opacity_change(self, val):
        v = int(float(val))
        self.root.attributes('-alpha', v / 100.0)
        self.opacity_pct.configure(text=f'{v}%')
        db.set_setting('opacity', str(v))

    def _on_topmost_toggle(self):
        self.topmost_var.set(not self.topmost_var.get())
        self.root.attributes('-topmost', self.topmost_var.get())
        self._update_topmost_btn()
        db.set_setting('topmost', '1' if self.topmost_var.get() else '0')

    def _update_topmost_btn(self):
        if self.topmost_var.get():
            self.topmost_btn.configure(text='📌 已置顶', fg=self.C_PRIMARY)
        else:
            self.topmost_btn.configure(text='📌 置顶', fg=self.C_TEXT_SEC)

    # ── Toast ──────────────────────────────────────────────────────
    def show_toast(self, msg):
        self.toast_label.configure(text=msg)
        self.toast_label.pack(fill=tk.BOTH)
        self.toast_frame.place(relx=0.5, rely=0.06, anchor=tk.N)
        self.toast_frame.lift()

        # Cancel any pending hide
        if hasattr(self, '_toast_job'):
            self.root.after_cancel(self._toast_job)
        self._toast_job = self.root.after(2000, self.toast_frame.place_forget)

    def _poll_show_signal(self):
        """Check for signal file from another instance (bring window to front)."""
        signal = APP_DIR / 'show.signal'
        if signal.exists():
            try: signal.unlink()
            except: pass
            self.show_window()
        self.root.after(500, self._poll_show_signal)

    # ── Window control ─────────────────────────────────────────────
    def toggle_visible(self):
        if self.root.state() == 'withdrawn' or not self.root.winfo_viewable():
            self.show_window()
        else:
            self.hide_window()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.root.state('normal')
        self.refresh()

    def hide_window(self):
        self.root.withdraw()

    def quit_app(self):
        db.conn.close()
        self.root.quit()
        self.root.destroy()
        os._exit(0)

    def run(self):
        self.root.mainloop()

# ── Main ───────────────────────────────────────────────────────────
def main():
    global app_ui

    log('App starting...')
    db.cleanup()

    # Clipboard monitor
    threading.Thread(target=monitor_clipboard, daemon=True).start()
    log('Clipboard monitor started')

    # Hotkey
    threading.Thread(target=start_hotkey_listener, daemon=True).start()

    # Tray
    threading.Thread(target=create_tray_icon, daemon=True).start()

    # UI
    app_ui = AppUI()
    log('UI created')
    app_ui.run()

if __name__ == '__main__':
    main()
