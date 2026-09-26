import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MARKER_PATH = os.path.join(BASE_DIR, "aobana.installed")


def _user_data_dir():
    if os.environ.get("AOBANA_DATA_DIR"):
        return os.environ["AOBANA_DATA_DIR"]
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(root, "Aobana")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Aobana")
    root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(root, "aobana")


INSTALLED = os.path.exists(MARKER_PATH)

DATA_FOLDER = "data"
_MOVED_TO_DATA = ("config.json", "logs", "update", "update.json", "profile-handoff.json")


def _source_store():
    new = os.path.join(BASE_DIR, DATA_FOLDER)
    if os.path.exists(os.path.join(new, "config.json")):
        return new
    if any(os.path.exists(os.path.join(BASE_DIR, n)) for n in _MOVED_TO_DATA + ("subs.db", "epub.db", "db")):
        return BASE_DIR
    return new


STORE_DIR = _user_data_dir() if INSTALLED else (os.environ.get("AOBANA_DATA_DIR") or _source_store())
CONFIG_PATH = os.path.join(STORE_DIR, "config.json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


MEDIA_NAMES = ("Subtitles", "Books")


def make_source_folders():
    if INSTALLED:
        return
    root = _default_media_root()
    cfg = load_config()
    try:
        entries = set(os.listdir(root)) if os.path.isdir(root) else set()
        if entries <= set(MEDIA_NAMES):
            for i, key in enumerate(("subs_dir", "books_dir")):
                new = os.path.join(root, MEDIA_NAMES[i])
                named = cfg.get(key)
                if key in cfg and (not named or os.path.normcase(os.path.abspath(named)) != os.path.normcase(new)):
                    continue
                os.makedirs(new, exist_ok=True)
    except OSError:
        pass


def load_config():
    cfg = _read_json(CONFIG_PATH)
    if not INSTALLED:
        return cfg
    seed = _read_json(MARKER_PATH)
    stamp = seed.get("installed_at")
    new_install = stamp is not None and cfg.get("installed_at") != stamp
    configured = "subs_dir" in cfg or "books_dir" in cfg
    if configured and not new_install:
        return cfg
    if "subs_dir" in seed or "books_dir" in seed:
        cfg.update(subs_dir=seed.get("subs_dir") or "", books_dir=seed.get("books_dir") or "")
    elif not configured:
        root = _default_media_root()
        cfg.update(subs_dir=os.path.join(root, MEDIA_NAMES[0]), books_dir=os.path.join(root, MEDIA_NAMES[1]))
    if isinstance(seed.get("port"), int):
        cfg["port"] = seed["port"]
    if "db_dir" in seed:
        if seed["db_dir"]:
            cfg["db_dir"] = seed["db_dir"]
        else:
            cfg.pop("db_dir", None)
    if stamp is not None:
        cfg["installed_at"] = stamp
    try:
        for folder in (cfg.get("subs_dir"), cfg.get("books_dir")):
            if folder:
                os.makedirs(folder, exist_ok=True)
        if cfg.get("db_dir"):
            os.makedirs(cfg["db_dir"], exist_ok=True)
        save_config(cfg)
    except OSError:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(STORE_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _documents_dir():
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def _default_media_root():
    if INSTALLED:
        return os.path.join(_documents_dir(), "Aobana")
    return os.path.join(BASE_DIR, "content")


def subs_dir():
    if os.environ.get("SUBS_ROOT_DIR"):
        return os.environ["SUBS_ROOT_DIR"]
    cfg = load_config()
    if "subs_dir" in cfg:
        return cfg["subs_dir"] or None
    d = _source_media(0)
    if not INSTALLED and not os.path.exists(d) and os.path.isdir(_default_media_root()):
        return _default_media_root()
    return d


def books_dir():
    if os.environ.get("EPUB_ROOT_DIR"):
        return os.environ["EPUB_ROOT_DIR"]
    cfg = load_config()
    if "books_dir" in cfg:
        return cfg["books_dir"] or None
    return _source_media(1)


def _source_media(i):
    return os.path.join(_default_media_root(), MEDIA_NAMES[i])


def server_port():
    for value in (os.environ.get("AOBANA_PORT"), load_config().get("port")):
        try:
            if value not in (None, ""):
                return int(value)
        except (TypeError, ValueError):
            pass
    return 5005 if sys.platform == "darwin" else 5000


def debug_mode():
    env = os.environ.get("AOBANA_DEBUG")
    if env is not None:
        return env == "1"
    return load_config().get("debug") is True


def index_workers():
    for value in (os.environ.get("AOBANA_INDEX_WORKERS"), load_config().get("index_workers")):
        try:
            if value not in (None, ""):
                return max(1, int(value))
        except (TypeError, ValueError):
            pass
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def db_dir():
    return load_config().get("db_dir") or default_db_dir()


DB_FOLDER = "db"
_DB_NAMES = ("subs.db", "epub.db")
_MOVED_WITH_DBS = ([n + s for n in _DB_NAMES + ("search_cache.db", "analysis.db")
                    for s in ("", "-wal", "-shm", "-journal")]
                   + ["filtered.tsv", "analysis.json", "media_cache.json", "library_figures.json"])


def default_db_dir():
    new = os.path.join(STORE_DIR, DB_FOLDER)
    if not any(os.path.exists(os.path.join(new, n)) for n in _DB_NAMES) \
            and any(os.path.exists(os.path.join(STORE_DIR, n)) for n in _DB_NAMES):
        return STORE_DIR
    return new


def _db_env_or_chosen():
    return bool(load_config().get("db_dir") or os.environ.get("SUBS_DB_PATH") or os.environ.get("EPUB_DB_PATH"))


def _rename_all(pairs):
    done = []
    try:
        for s, d in pairs:
            os.replace(s, d)
            done.append((s, d))
    except OSError:
        for s, d in reversed(done):
            try:
                os.replace(d, s)
            except OSError:
                pass
        return False
    return True


def move_into_data_folder():
    global STORE_DIR, CONFIG_PATH
    moved = []
    if not INSTALLED and not os.environ.get("AOBANA_DATA_DIR") and STORE_DIR == BASE_DIR:
        new = os.path.join(BASE_DIR, DATA_FOLDER)
        names = list(_MOVED_TO_DATA)
        if not _db_env_or_chosen():
            names += list(_MOVED_WITH_DBS) + [DB_FOLDER]
        names = [n for n in names if os.path.exists(os.path.join(BASE_DIR, n))
                 and not os.path.exists(os.path.join(new, n))]
        try:
            os.makedirs(new, exist_ok=True)
        except OSError:
            return []
        if not _rename_all([(os.path.join(BASE_DIR, n), os.path.join(new, n)) for n in names]):
            return []
        moved = names
        STORE_DIR, CONFIG_PATH = new, os.path.join(new, "config.json")
    return moved + move_into_db_folder()


def move_into_db_folder():
    if _db_env_or_chosen():
        return []
    new = os.path.join(STORE_DIR, DB_FOLDER)
    names = [n for n in _MOVED_WITH_DBS if os.path.isfile(os.path.join(STORE_DIR, n))]
    if not any(n in _DB_NAMES for n in names) \
            or any(os.path.exists(os.path.join(new, n)) for n in _DB_NAMES):
        return []
    done = []
    try:
        os.makedirs(new, exist_ok=True)
        for n in names:
            if os.path.exists(os.path.join(new, n)):
                continue
            os.replace(os.path.join(STORE_DIR, n), os.path.join(new, n))
            done.append(n)
    except OSError:
        for n in reversed(done):
            try:
                os.replace(os.path.join(new, n), os.path.join(STORE_DIR, n))
            except OSError:
                pass
        return []
    return done


def subs_db():
    return os.environ.get("SUBS_DB_PATH") or os.path.join(db_dir(), "subs.db")


def epub_db():
    return os.environ.get("EPUB_DB_PATH") or os.path.join(db_dir(), "epub.db")


def filtered_list():
    return os.path.join(os.path.dirname(os.path.abspath(subs_db())), "filtered.tsv")


def logs_dir():
    return os.path.join(STORE_DIR, "logs")


def data_file(*parts):
    return os.path.join(BASE_DIR, "data", *parts)
