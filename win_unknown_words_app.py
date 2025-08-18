#!/usr/bin/env python3
"""
SubWords (Windows GUI)
----------------------------------------
• Выбираешь MKV/SRT/VTT → приложение показывает субтитровые дорожки (если MKV)
• Извлекает выбранную дорожку в .srt (ffmpeg)
• Анализ: токены → (spaCy опц.) леммы → known_words.txt → частоты + 3 примера
• Перевод незнакомых слов через Google (deep-translator)
• Группы: «На изучении» (подсветка) и «Известные»
• Счётчики, прогресс-бар, экспорт CSV, контекстное меню, хоткеи (layout-agnostic)

Установка (PowerShell):
  pip install spacy deep-translator chardet
  python -m spacy download en_core_web_sm
  # ffmpeg/ffprobe должны быть в PATH
"""
import os
import re
import csv
import json
import queue
import pathlib
import subprocess
import threading
from collections import Counter, defaultdict

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import os, sys, pathlib

# --- Translation cache (disk) ---
CACHE_FILE = pathlib.Path("translation_cache.json")

def _load_translation_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_translation_cache(cache: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

# ---------- NLP (spaCy) ----------
USE_SPACY = True
try:
    import spacy
    try:
        NLP = spacy.load("en_core_web_sm")
    except OSError:
        USE_SPACY = False
        NLP = None
except Exception:
    USE_SPACY = False
    NLP = None

# ---------- Опции/константы ----------
STOPLIST = {
    "the","a","an","and","or","but","if","then","so","to","of","in","on","at","for","from",
    "by","with","as","is","am","are","was","were","be","been","being","it","this","that",
    "i","you","he","she","we","they","me","him","her","us","them","my","your","his","her",
    "our","their","mine","yours","ours","theirs","do","does","did","doing","not","no","yes",
    "there","here","up","down","out","over","under","into","about","than","too","very",
    "can","could","should","would","will","shall","may","might","must","just","also","ever",
}
TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
DEFAULT_KNOWN = pathlib.Path("known_words.txt")
DEFAULT_STUDY = pathlib.Path("studying_words.txt")
DEFAULT_OUT   = pathlib.Path("unknown_words.csv")

# ---------- Helpers ----------
def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

def ffprobe_list_subs(mkv_path: str):
    """Вернуть список потоков субтитров с их ГЛОБАЛЬНЫМИ индексами."""
    p = run(["ffprobe","-v","error","-show_streams","-select_streams","s","-of","json", mkv_path])
    if p.returncode != 0:
        raise RuntimeError("ffprobe error:\n" + p.stderr)
    data = json.loads(p.stdout or "{}")
    out = []
    for st in data.get("streams", []):
        out.append({
            "index": st.get("index"),
            "codec": st.get("codec_name"),
            "lang": (st.get("tags", {}) or {}).get("language", ""),
            "title": (st.get("tags", {}) or {}).get("title", ""),
        })
    if not out:
        p2 = run(["ffprobe","-v","error","-show_streams","-of","compact", mkv_path])
        raise RuntimeError("В контейнере не найдено субтитровых дорожек.\n" + (p2.stdout or ""))
    return out

def extract_srt(mkv_path: str, out_srt_path: str, global_index: int):
    """Извлекаем субтитры по ГЛОБАЛЬНОМУ индексу (ffprobe: stream.index)."""
    p = run(["ffmpeg","-y","-i", mkv_path, "-map", f"0:{global_index}", "-c:s", "srt", out_srt_path])
    if p.returncode != 0:
        raise RuntimeError("ffmpeg extract error (для PGS/VobSub нужен OCR).\n\n" + p.stderr)

def guess_read_text(path: pathlib.Path) -> str:
    raw = path.read_bytes()
    try:
        import chardet
        enc = chardet.detect(raw).get('encoding') or 'utf-8'
    except Exception:
        enc = 'utf-8'
    try:
        return raw.decode(enc, errors='ignore')
    except Exception:
        return raw.decode('utf-8', errors='ignore')

def clean_srt_text(text: str) -> str:
    text = re.sub(r"\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}", " ", text)
    text = re.sub(r"\d{2}:\d{2}:\д{2}\.\д{3}\s+-->\s+\д{2}:\д{2}:\д{2}\.\д{3}", " ", text)
    text = re.sub(r"^\s*\d+\s*$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\{\\.*?\}", " ", text)
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def tokenize(text: str):
    return TOKEN_RE.findall(text)

def lemmatize(words):
    if not USE_SPACY or NLP is None:
        return [w.lower() for w in words]
    doc = NLP(" ".join(words))
    return [t.lemma_.lower() for t in doc if t.is_alpha]

def split_sentences(text: str):
    return re.split(r"(?<=[.!?])\s+", text)

def load_known(path: pathlib.Path) -> set:
    if not path.exists():
        return set()
    words = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        w = re.sub(r"[^A-Za-z']", "", line.strip()).lower()
        if w:
            words.add(w)
    return words

def save_known(path: pathlib.Path, words: set):
    path.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8")

# ---------- Переводчик (Google, deep-translator) + cache ----------
class GoogleTranslator:
    _cache = _load_translation_cache()  # общий кэш на весь процесс

    def __init__(self):
        try:
            from deep_translator import GoogleTranslator as DG
        except Exception as e:
            raise RuntimeError("Не установлен пакет 'deep-translator'. Установите: pip install deep-translator") from e
        self.DG = DG

    @classmethod
    def _key(cls, src: str, dst: str, text: str) -> str:
        return f"{(src or 'en').lower()}|{(dst or 'ru').lower()}|{text}"

    def translate_many(self, terms, src="EN", dest="RU") -> dict:
        terms = list(dict.fromkeys(terms))  # де-дуп с сохранением порядка
        if not terms:
            return {}

        src = (src or "en").lower()
        dest = (dest or "ru").lower()

        result = {}
        missing = []

        # 1) берём из кэша
        for t in terms:
            k = self._key(src, dest, t)
            if k in self._cache:
                result[t] = self._cache[k]
            else:
                missing.append(t)

        # 2) дозаказываем только недостающее (батчами)
        if missing:
            gt = self.DG(source=src, target=dest)
            for batch in _chunked(missing, 50):
                try:
                    translated = gt.translate_batch(batch)
                except Exception:
                    translated = [""] * len(batch)  # не падаем, просто пустые строки
                for t, tr in zip(batch, translated):
                    result[t] = tr
                    # пишем в кэш (пустые тоже можно кэшировать; если хочешь — убери условие)
                    self._cache[self._key(src, dest, t)] = tr
            _save_translation_cache(self._cache)

        # 3) гарантируем значения для всех терминов
        for t in terms:
            result.setdefault(t, "")

        return result

# ---------- GUI ----------
class App(tk.Tk):
    # layout-agnostic virtual-key codes (Windows)
    VK_A = 65
    VK_S = 83
    VK_DELETE = 46

    def __init__(self):
        super().__init__()
        self.title("SubWords")
        self.geometry("1150x720")
        self.minsize(980, 620)

        # Устанавливаем иконку окна (работает и при отладке, и после сборки)
        try:
            base = getattr(sys, "_MEIPASS", pathlib.Path(__file__).parent)
            icon_path = os.path.join(base, "SW.ico")
            self.iconbitmap(icon_path)
        except Exception:
            pass
        

        self.title("SubWords")
        self.geometry("1150x720")
        self.minsize(980, 620)

        # Состояние
        self.input_path = tk.StringVar()
        self.known_path = tk.StringVar(value=str(DEFAULT_KNOWN))
        self.min_freq   = tk.IntVar(value=1)
        self.use_translation = tk.BooleanVar(value=True)  # перевод включён по умолчанию
        self.lang_src   = tk.StringVar(value="EN")
        self.lang_dst   = tk.StringVar(value="RU")

        # Счётчики и прогресс
        self.total_tokens   = tk.IntVar(value=0)
        self.unknown_unique = tk.IntVar(value=0)
        self.studying_found = tk.IntVar(value=0)
        self.progress       = tk.IntVar(value=0)

        self.known_words    = load_known(DEFAULT_KNOWN)
        self.studying_words = load_known(DEFAULT_STUDY)

        self._make_widgets()

        # Фоновые задачи
        self._worker = None
        self._q = queue.Queue()
        self.after(120, self._poll_queue)

    def _make_widgets(self):
        pad = {"padx": 10, "pady": 8}

        # ---------- Масштаб/шрифты (компактно, но читабельно) ----------
        try:
            self.tk.call("tk", "scaling", 1.10)
        except Exception:
            pass
        base_font = ("Segoe UI", 10)
        small_font = ("Segoe UI", 9)
        bold_font = ("Segoe UI", 10, "bold")

        # ---------- Тема/стили (тёмная палитра) ----------
        self.configure(bg="#2F3136")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background="#2F3136", foreground="#FFFFFF",
                        fieldbackground="#40444B", font=base_font)
        style.configure("TLabel", background="#2F3136", foreground="#FFFFFF", font=base_font)
        style.configure("TEntry", fieldbackground="#40444B", foreground="#FFFFFF")
        style.configure("TSpinbox", fieldbackground="#40444B", foreground="#FFFFFF")
        style.configure("TCheckbutton", background="#2F3136", foreground="#FFFFFF")

        # Кнопки: синяя только "Анализировать"
        style.configure("Primary.TButton",
                        background="#5865F2", foreground="#FFFFFF",
                        padding=6, relief="flat", font=bold_font)
        style.map("Primary.TButton",
                background=[("active", "#4752C4"), ("pressed", "#3C45B1")])

        style.configure("Secondary.TButton",
                        background="#4F545C", foreground="#FFFFFF",
                        padding=6, relief="flat", font=base_font)
        style.map("Secondary.TButton",
                background=[("active", "#686D75"), ("pressed", "#5B6068")])

        # Прогресс-бар
        style.configure("Horizontal.TProgressbar", troughcolor="#202225", background="#5865F2")

        # Таблица — чётче заголовки, видимые границы шапки
        style.configure("Treeview",
                        background="#2F3136", fieldbackground="#2F3136",
                        foreground="#FFFFFF", rowheight=28, borderwidth=0, font=base_font)
        style.configure("Treeview.Heading",
                        background="#202225", foreground="#FFFFFF",
                        font=bold_font, relief="groove", borderwidth=1)
        style.map("Treeview",
                background=[("selected", "#4752C4")],
                foreground=[("selected", "#FFFFFF")])

        # ---------- Ввод путей ----------
        top = ttk.Frame(self); top.pack(fill=tk.X, **pad)
        ttk.Label(top, text="Видео/SRT:").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Entry(top, textvariable=self.input_path, width=68).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Обзор…", style="Secondary.TButton",
                command=self._browse_input).pack(side=tk.LEFT, padx=6)

        mid = ttk.Frame(self); mid.pack(fill=tk.X, **pad)
        ttk.Label(mid, text="Известные слова:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(mid, textvariable=self.known_path, width=58).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(mid, text="Обзор…", style="Secondary.TButton",
                command=self._browse_known).grid(row=0, column=2, padx=6)
        ttk.Button(mid, text="Открыть", style="Secondary.TButton",
                command=self._open_known_file).grid(row=0, column=3, padx=6)

        ttk.Label(mid, text="Мин. частота:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(mid, from_=1, to=20, textvariable=self.min_freq, width=6)\
            .grid(row=1, column=1, sticky="w", pady=(8, 0))

        ttk.Checkbutton(mid, text="Перевод (Google)", variable=self.use_translation)\
            .grid(row=1, column=2, sticky="w", pady=(8, 0), padx=(10, 0))
        ttk.Label(mid, text="из").grid(row=1, column=3, sticky="e", pady=(8, 0))
        ttk.Entry(mid, textvariable=self.lang_src, width=5).grid(row=1, column=4, sticky="w", pady=(8, 0), padx=(4, 8))
        ttk.Label(mid, text="в").grid(row=1, column=5, sticky="e", pady=(8, 0))
        ttk.Entry(mid, textvariable=self.lang_dst, width=5).grid(row=1, column=6, sticky="w", pady=(8, 0), padx=(4, 0))
        mid.columnconfigure(1, weight=1)

        # ---------- Верхняя панель действий ----------
        action = ttk.Frame(self); action.pack(fill=tk.X, **pad)
        ttk.Button(action, text="Анализировать", style="Primary.TButton",
                command=self._start_analyze).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(action, text="Экспорт CSV", style="Secondary.TButton",
                command=self._export_csv).pack(side=tk.LEFT, padx=6)
        ttk.Button(action, text="Список \"В изучении\"", style="Secondary.TButton",
                command=self._open_studying_view).pack(side=tk.LEFT, padx=6)

        # ---------- Панель счётчиков ----------
        counters = ttk.Frame(self); counters.pack(fill=tk.X, padx=12)
        for w in (
            ttk.Label(counters, text="Всего слов:", font=small_font),
            ttk.Label(counters, textvariable=self.total_tokens, font=small_font),
            ttk.Label(counters, text="   Неизвестных (уник.):", font=small_font),
            ttk.Label(counters, textvariable=self.unknown_unique, font=small_font),
            ttk.Label(counters, text="   На изучении (в фильме):", font=small_font),
            ttk.Label(counters, textvariable=self.studying_found, font=small_font),
        ): w.pack(side=tk.LEFT)

        # ---------- Прогресс-бар и статус ----------
        self.pbar = ttk.Progressbar(self, orient="horizontal", mode="determinate",
                                    maximum=100, variable=self.progress,
                                    style="Horizontal.TProgressbar")
        self.pbar.pack(fill=tk.X, padx=12, pady=(4, 6))
        self.status = tk.StringVar(value="Готово.")
        ttk.Label(self, textvariable=self.status, font=small_font)\
        .pack(fill=tk.X, padx=12, pady=(0, 8))

        # ---------- Таблица результатов ----------
        table_frame = ttk.Frame(self)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        cols = ("word","count","translation","examples_en","examples_ru","status")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("word", text="Слово/лемма")
        self.tree.heading("count", text="Частота")
        self.tree.heading("translation", text="Перевод")
        self.tree.heading("examples_en", text="Примеры (EN)")
        self.tree.heading("examples_ru", text="Примеры (RU)")
        self.tree.heading("status", text="Статус")

        # ВАЖНО: фиксируем ширины и отключаем авто-растяжение,
        # чтобы горизонтальный скролл оставался доступным в любом размере окна
        self.tree.column("word",        width=220, minwidth=50,  anchor=tk.W,     stretch=False)
        self.tree.column("count",       width=100, minwidth=40,  anchor=tk.CENTER,stretch=False)
        self.tree.column("translation", width=280, minwidth=60,  anchor=tk.W,     stretch=False)
        self.tree.column("examples_en", width=280,minwidth=80,  anchor=tk.W,     stretch=False)
        self.tree.column("examples_ru", width=280,minwidth=80,  anchor=tk.W,     stretch=False)
        self.tree.column("status",      width=140, minwidth=50,  anchor=tk.W,     stretch=False)


        # Подсветка «На изучении»
        self.tree.tag_configure("studying", background="#703F02")

        # Скроллбары
        ybar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL,   command=self.tree.yview)
        xbar = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        # Прокрутка мышью:
        #   колесо — вертикаль; Shift+колесо или Ctrl+колесо — горизонталь
        self.tree.bind("<MouseWheel>", lambda e: (self.tree.yview_scroll(int(-1*(e.delta/120)), "units"), "break"))
        self.tree.bind("<Shift-MouseWheel>", lambda e: (self.tree.xview_scroll(int(-1*(e.delta/120)), "units"), "break"))
        self.tree.bind("<Control-MouseWheel>", lambda e: (self.tree.xview_scroll(int(-1*(e.delta/120)), "units"), "break"))

        # ---------- Хоткеи ----------
        self.tree.bind("<Control-KeyPress>", self._on_ctrl_keypress)   # Ctrl+A, Ctrl+S
        self.tree.bind("<KeyPress>", self._on_tree_keypress)           # S без Ctrl
        self.tree.bind("<Delete>", lambda e: (self._add_selected_to_known(), "break"))

        # ---------- Контекстное меню ----------
        self.menu = tk.Menu(self, tearoff=0, bg="#2F3136", fg="#FFFFFF",
                            activebackground="#4752C4", activeforeground="#FFFFFF")
        self.menu.configure(font=base_font)
        self.menu.add_command(label="Добавить выбранные в известные", command=self._add_selected_to_known)
        self.menu.add_separator()
        self.menu.add_command(label="Добавить в изучение", command=self._add_selected_to_studying)
        self.menu.add_command(label="Убрать из изучения", command=self._remove_selected_from_studying)
        self.tree.bind("<Button-3>", self._popup_menu)



    # ---------- Hotkeys / Help ----------
    def _show_hotkeys(self):
        messagebox.showinfo(
            "Горячие клавиши",
            "• Ctrl+A — выделить всё\n"
            "• S — добавить выделенные в изучение\n"
            "• Ctrl+S — тоже добавить в изучение\n"
            "• Delete — отправить выделенные в Известные\n"
            "• ПКМ — контекстное меню действий"
        )

    def _on_ctrl_keypress(self, event):
        # по keycode, не зависит от раскладки
        if event.keycode == self.VK_A:
            for iid in self.tree.get_children(""):
                self.tree.selection_add(iid)
            return "break"
        if event.keycode == self.VK_S:
            self._add_selected_to_studying()
            return "break"

    def _on_tree_keypress(self, event):
        # если Ctrl нажат, обработает _on_ctrl_keypress
        if event.state & 0x4:
            return
        if event.keycode == self.VK_S:
            self._add_selected_to_studying()
            return "break"
        if event.keycode == self.VK_DELETE or (event.keysym and event.keysym.lower() == "delete"):
            self._add_selected_to_known()
            return "break"

    # ---------- Helpers GUI ----------
    def _add_selected_to_studying(self):
        items = list(self.tree.selection())
        if not items:
            return
        added = 0
        for iid in items:
            vals = list(self.tree.item(iid, 'values'))
            if not vals:
                continue
            word = vals[0]
            if word and word not in self.studying_words:
                self.studying_words.add(word)
                added += 1
            # обновим статус и теги в таблице
            if len(vals) < 6:
                vals.append("На изучении")
            else:
                vals[5] = "На изучении"
            self.tree.item(iid, values=vals, tags=("studying",))
        save_known(DEFAULT_STUDY, self.studying_words)
        self._refresh_counters()
        messagebox.showinfo("OK", f"Добавлено в изучение: {added} шт.")

    def _remove_selected_from_studying(self):
        items = list(self.tree.selection())
        if not items:
            return
        removed = 0
        for iid in items:
            vals = list(self.tree.item(iid, 'values'))
            if not vals:
                continue
            word = vals[0]
            if word in self.studying_words:
                self.studying_words.remove(word)
                removed += 1
            # убираем отметку и теги, статус → «Неизученное»
            if len(vals) >= 6:
                vals[5] = "Неизученное"
            self.tree.item(iid, values=vals, tags=())
        save_known(DEFAULT_STUDY, self.studying_words)
        self._refresh_counters()
        messagebox.showinfo("OK", f"Убрано из изучения: {removed} шт.")

    def _browse_input(self):
        path = filedialog.askopenfilename(title="Выберите видео или SRT",
                                          filetypes=[("Видео/SRT", "*.mkv *.mp4 *.srt *.vtt"),
                                                     ("Все файлы", "*.*")])
        if path:
            self.input_path.set(path)

    def _browse_known(self):
        path = filedialog.asksaveasfilename(title="known_words.txt",
                                            defaultextension=".txt",
                                            initialfile="known_words.txt",
                                            filetypes=[("Text", "*.txt")])
        if path:
            self.known_path.set(path)
            self.known_words = load_known(pathlib.Path(path))
            self._refresh_counters()

    def _open_known_file(self):
        p = pathlib.Path(self.known_path.get())
        if not p.exists():
            p.write_text("", encoding="utf-8")
        os.startfile(str(p))

    def _popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _open_studying_view(self):
        words = sorted(getattr(self, 'studying_words', set()))
        if not words:
            messagebox.showinfo("Пусто", "Список 'На изучении' пуст.")
            return
        top = tk.Toplevel(self)
        top.title("На изучении — список")
        top.geometry("800x520")
        top.grab_set()

        frame = ttk.Frame(top)
        frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        cols = ("word","translation")
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        tree.heading("word", text="Слово")
        tree.heading("translation", text="Перевод")
        tree.column("word", width=260, anchor=tk.W)
        tree.column("translation", width=500, anchor=tk.W)
        yb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        xb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=yb.set, xscrollcommand=xb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yb.grid(row=0, column=1, sticky="ns")
        xb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        try:
            gt = GoogleTranslator()
            trans = gt.translate_many(words, src=self.lang_src.get(), dest=self.lang_dst.get())
        except Exception:
            trans = {w: "" for w in words}
        for w in words:
            tree.insert('', 'end', values=(w, trans.get(w, "")))

        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, padx=8, pady=6)
        def move_to_known():
            items = list(tree.selection())
            if not items:
                return
            moved = 0
            for iid in items:
                vals = tree.item(iid, 'values')
                if not vals:
                    continue
                w = vals[0]
                if w in self.studying_words:
                    self.studying_words.remove(w)
                if w not in self.known_words:
                    self.known_words.add(w)
                    moved += 1
                tree.delete(iid)
            save_known(pathlib.Path(self.known_path.get()), self.known_words)
            save_known(DEFAULT_STUDY, self.studying_words)
            self._refresh_counters()
            messagebox.showinfo("OK", f"Перенесено в известные: {moved} шт.")
        def remove_from_study():
            items = list(tree.selection())
            if not items:
                return
            removed = 0
            for iid in items:
                vals = tree.item(iid, 'values')
                if not vals:
                    continue
                w = vals[0]
                if w in self.studying_words:
                    self.studying_words.remove(w)
                    removed += 1
                tree.delete(iid)
            save_known(DEFAULT_STUDY, self.studying_words)
            self._refresh_counters()
            messagebox.showinfo("OK", f"Убрано из изучения': {removed} шт.")
        ttk.Button(btns, text="→ В известные", command=move_to_known).pack(side=tk.LEFT)
        ttk.Button(btns, text="Убрать из 'изучения", command=remove_from_study).pack(side=tk.LEFT, padx=6)

        # Горизонтальная прокрутка по Shift+колесу
        tree.bind("<Shift-MouseWheel>", lambda e: (tree.xview_scroll(int(-1*(e.delta/120)), "units"), "break"))

    def _add_selected_to_known(self):
        items = list(self.tree.selection())
        if not items:
            return
        added = 0
        removed_from_studying = 0
        for iid in items:
            vals = self.tree.item(iid, 'values')
            if not vals:
                continue
            word = vals[0]
            # если слово было в списке "На изучении" — убираем
            if word in self.studying_words:
                self.studying_words.remove(word)
                removed_from_studying += 1
            if word and word not in self.known_words:
                self.known_words.add(word)
                added += 1
        # сохраняем оба файла
        save_known(pathlib.Path(self.known_path.get()), self.known_words)
        save_known(DEFAULT_STUDY, self.studying_words)
        self._refresh_counters()
        # удаляем из таблицы
        for iid in items:
            try:
                self.tree.delete(iid)
            except Exception:
                pass
        msg = f"Добавлено в известные: {added} шт. Убрано из списка."
        if removed_from_studying:
            msg += f"\nСнято с отметки 'На изучении': {removed_from_studying} шт."
        messagebox.showinfo("OK", msg)

    def _remove_selected_from_known(self):
        items = list(self.tree.selection())
        if not items:
            return
        removed = 0
        for iid in items:
            vals = self.tree.item(iid, 'values')
            if not vals:
                continue
            word = vals[0]
            if word in self.known_words:
                self.known_words.remove(word)
                removed += 1
        save_known(pathlib.Path(self.known_path.get()), self.known_words)
        self._refresh_counters()
        for iid in items:
            try:
                self.tree.delete(iid)
            except Exception:
                pass
        messagebox.showinfo("OK", f"Удалено из известных: {removed} шт.")

    def _export_csv(self):
        if not self.tree.get_children(""):
            messagebox.showwarning("Нет данных", "Сначала выполните анализ.")
        
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=str(DEFAULT_OUT),
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["lemma","count","translation","examples_en","examples_ru","status"])
            for iid in self.tree.get_children(""):
                vals = self.tree.item(iid, 'values')
                w.writerow(vals)
        messagebox.showinfo("Готово", f"Экспортировано: {path}")

    # ---------- Background ----------
    def _start_analyze(self):
        if self._worker and self._worker.is_alive():
            return
        ipath = self.input_path.get().strip()
        if not ipath:
            messagebox.showwarning("Выберите файл", "Укажите видео или SRT.")
            return
        # reset counters/progress
        self.progress.set(0)
        self.total_tokens.set(0)
        self.unknown_unique.set(0)
        self.studying_found.set(0)
        self.status.set("Анализ...")
        self.tree.delete(*self.tree.get_children(""))
        self._worker = threading.Thread(target=self._analyze_worker, args=(ipath,), daemon=True)
        self._worker.start()

    def _poll_queue(self):
        try:
            while True:
                msg = self._q.get_nowait()
                kind = msg.get('kind')
                if kind == 'status':
                    self.status.set(msg['text'])
                elif kind == 'progress':
                    try:
                        self.progress.set(int(msg.get('value', 0)))
                    except Exception:
                        pass
                elif kind == 'counts':
                    self.total_tokens.set(msg.get('total_tokens', 0))
                    self.unknown_unique.set(msg.get('unknown_unique', 0))
                    self.studying_found.set(msg.get('studying_found', 0))

                elif kind == 'state':
                    # сохраняем множества для мгновенного пересчёта
                    self._lemmas_set = set(msg.get('lemmas_set', []))
                    self._all_unique_set = set(msg.get('all_unique', []))
                elif kind == 'result':
                    self._fill_table(msg['rows'])
                    self.status.set("Готово.")
                    self.progress.set(100)
                elif kind == 'error':
                    self.status.set("Ошибка")
                    messagebox.showerror("Ошибка", msg['text'])
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)
    def _refresh_counters(self):
        # Пересчёт на основе уникальных токенов из последнего анализа
        tokens_set = getattr(self, "_all_unique_set", None)  # это tokens_base из state
        if tokens_set is None:
            return

        total = len(tokens_set)
        unknown_unique = len(tokens_set - self.known_words)
        studying_found = len(tokens_set & self.studying_words)

        self.total_tokens.set(total)
        self.unknown_unique.set(unknown_unique)
        self.studying_found.set(studying_found)



    def _fill_table(self, rows):
        for r in rows:
            word = r[0] if r else ""
            status = "На изучении" if (word and word in self.studying_words) else "Неизученное"
            tags = ("studying",) if status == "На изучении" else ()
            vals = r + (status,) if len(r) == 5 else r
            self.tree.insert('', 'end', values=vals, tags=tags)

    # ---------- Analysis ----------
    def _collect_examples(self, text: str, freq: Counter) -> dict[str, list]:
        """
        Надёжно собираем до 3 примеров на лемму.
        1) нормализуем кавычки; 2) лемматизируем всю фразу; 3) fallback по \bword\b.
        """
        text_norm = text.replace("’", "'")
        sents = split_sentences(text_norm)
        contexts: dict[str, list] = defaultdict(list)

        # основной проход: лемматизируем целую фразу, работаем с множеством лемм фразы
        for s in sents:
            tokens = tokenize(s)
            if not tokens:
                continue
            lem_set = set(lemmatize(tokens))
            for lw in lem_set:
                if lw in freq and len(contexts[lw]) < 3:
                    contexts[lw].append(s.strip())

        # fallback: если примеров нет — ищем по слову с границами
        missing = [w for w in freq if not contexts[w]]
        if missing:
            sents_lower = [ss.lower() for ss in sents]
            for w in missing:
                pat = re.compile(rf"\b{re.escape(w)}\b")
                added = 0
                for i, ls in enumerate(sents_lower):
                    if pat.search(ls):
                        contexts[w].append(sents[i].strip())
                        added += 1
                        if added >= 3:
                            break
        return contexts

    def _analyze_worker(self, ipath: str):
        try:
            p = pathlib.Path(ipath)
            if not p.exists():
                raise RuntimeError("Файл не найден")

            # Получаем SRT
            if p.suffix.lower() in {'.srt', '.vtt'}:
                srt_path = p
            else:
                self._q.put({'kind':'status','text':'Определение субтитров...'})
                tracks = ffprobe_list_subs(str(p))
                idx = self._ask_track_index(tracks)
                if idx is None:
                    self._q.put({'kind':'status','text':'Отменено.'})
                    return
                self._q.put({'kind':'status','text':'Извлечение субтитров...'})
                srt_path = p.with_suffix('.en.srt')
                extract_srt(str(p), str(srt_path), tracks[idx]['index'])

            self._q.put({'kind':'status','text':'Чтение SRT...'})
            self._q.put({'kind':'progress','value':20})
            raw = guess_read_text(srt_path)
            text = clean_srt_text(raw)

            self._q.put({'kind':'progress','value':30})
            tokens = tokenize(text)
            lemmas = lemmatize(tokens)
            self._q.put({'kind':'progress','value':40})
            lemmas = [w for w in lemmas if w not in STOPLIST and len(w) > 1]

            known = load_known(pathlib.Path(self.known_path.get()))
            self.known_words = known

        # Счётчики (единая база для обоих счетчиков)
# База = уникальные токены после того же фильтра, что и для таблицы:
            tokens_base = {t.lower() for t in tokens if t.lower() not in STOPLIST and len(t) > 1}
            all_unique = tokens_base
            total_tokens = len(all_unique)

            unknown_tokens = [t.lower() for t in tokens
            if t.lower() not in STOPLIST and len(t) > 1 and t.lower() not in known]
            freq = Counter(unknown_tokens)
            unknown_unique = len(freq.keys())

            studying_found = len(all_unique & self.studying_words)

            self._q.put({
                'kind': 'state',
                'lemmas_set': list(set(lemmas)),
                'all_unique': list(all_unique),   # <— теперь это tokens_base
            })

            self._q.put({
                'kind': 'counts',
                'total_tokens': total_tokens,
                'unknown_unique': unknown_unique,
                'studying_found': studying_found
            })



            self._q.put({'kind':'status','text':'Сбор примеров...'})
            self._q.put({'kind':'progress','value':55})
            contexts = self._collect_examples(text, freq)

            min_f = int(self.min_freq.get() or 1)
            words_sorted = [w for w, c in freq.most_common() if c >= min_f]

            if self.use_translation.get() and words_sorted:
                self._q.put({'kind':'status','text':'Перевод (Google)...'})
                self._q.put({'kind':'progress','value':70})
                gt = GoogleTranslator()
                translations = gt.translate_many(words_sorted, src=self.lang_src.get(), dest=self.lang_dst.get())
                # Перевод примеров
                all_examples = []
                for w in words_sorted:
                    all_examples.extend(contexts[w])
                ex_map = gt.translate_many(all_examples, src=self.lang_src.get(), dest=self.lang_dst.get()) if all_examples else {}
            else:
                translations = {w: "" for w in words_sorted}
                ex_map = {}

            rows = []
            for w in words_sorted:
                c = freq[w]
                en_list = contexts[w]
                ex_en = " | ".join(en_list)
                ex_ru = " | ".join([ex_map.get(s, "") for s in en_list]) if en_list else ""
                rows.append((w, c, translations.get(w, ""), ex_en, ex_ru))

            self._q.put({'kind':'progress','value':95})
            self._q.put({'kind':'result','rows': rows})
        except Exception as e:
            self._q.put({'kind':'error','text': str(e)})

    def _ask_track_index(self, tracks):
        sel = {"idx": None}
        top = tk.Toplevel(self)
        top.title("Выбор дорожки субтитров")
        top.grab_set()

        lb = tk.Listbox(top, width=90, height=10)
        for i, t in enumerate(tracks):
            lb.insert(tk.END, f"[{i}] index={t['index']} codec={t['codec']} lang={t['lang']} title={t['title']}")
        lb.selection_set(0)
        lb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        def ok():
            try:
                cur = lb.curselection()[0]
            except Exception:
                cur = 0
            sel["idx"] = cur
            top.destroy()
        def cancel():
            sel["idx"] = None
            top.destroy()

        btns = ttk.Frame(top)
        btns.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(btns, text="OK", command=ok).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Отмена", command=cancel).pack(side=tk.RIGHT)
        top.wait_window()
        return sel["idx"]


if __name__ == "__main__":
    app = App()
    if not USE_SPACY:
        messagebox.showwarning(
            "spaCy не найден",
            "Модель spaCy (en_core_web_sm) не установлена. Лемматизация будет отключена.\n\n"
            "Установите: pip install spacy && python -m spacy download en_core_web_sm"
        )
    app.mainloop()
