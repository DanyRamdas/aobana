import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import paths
from utils import FILTER_COLUMNS, count_rows, filtered_rows, outdated_sources, missing_tables

_LOCK = threading.Lock()
_STATE = {"running": False}


def _beside_db(name):
    return os.path.join(os.path.dirname(os.path.abspath(paths.subs_db())), name)


def _stop_file(kind):
    return _beside_db(f".{kind}.stop")


def _clear_stop(kind):
    try:
        os.remove(_stop_file(kind))
    except OSError:
        pass


def _run_marker():
    return _beside_db(".index_run.json")


def _unfinished():
    try:
        with open(_run_marker(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None

_STAGES = (("subs", "indexer.py"), ("epub", "epub_indexer.py"), ("manga", "manga_indexer.py"))
_STAGE_MEDIA = {"subs": "subs", "epub": "books", "manga": "manga"}
_SUMMARY_RE = {
    "subs": re.compile(r"Skipped (\d+) unchanged files\. Indexed (\d+) new/updated files\. "
                       r"Removed (\d+) deleted files"),
    "epub": re.compile(r"Skipped (\d+) unchanged files\. Indexed (\d+) new/updated books\. "
                       r"Removed (\d+) deleted"),
    "manga": re.compile(r"Skipped (\d+) unchanged files\. Indexed (\d+) new/updated volumes\. "
                        r"Removed (\d+) deleted"),
}


def _count_files(root, ext, skip_dot, progress=None, count_other=True):
    found = other = 0
    if not root or not os.path.isdir(root):
        return None
    last = time.monotonic()
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.lower().endswith(ext) and not (skip_dot and f.startswith('.')):
                found += 1
            elif count_other and not f.startswith('.'):
                other += 1
        if progress and time.monotonic() - last > 0.25:
            last = time.monotonic()
            progress(found, other)
    return {"files": found, "loose": 0, "other": other}


def _has_files(root, ext, skip_dot):
    if not root or not os.path.isdir(root):
        return False
    for _, _, files in os.walk(root):
        if any(f.lower().endswith(ext) and not (skip_dot and f.startswith('.')) for f in files):
            return True
    return False


def _stage_inputs(stage):
    if stage == "subs":
        return paths.subs_dir(), (".srt", ".ass", ".ssa"), False, paths.subs_db()
    if stage == "manga":
        return paths.manga_dir(), ".mokuro", True, paths.manga_db()
    return paths.books_dir(), ".epub", True, paths.epub_db()


def _indexed(conn, table):
    if conn is None:
        return {"files": 0, "rows": 0}
    try:
        return {"files": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "rows": count_rows(conn, table)}
    except Exception:
        return {"files": 0, "rows": 0}


_FSTATE = {"running": False}
_FIG_STALE = {"stale": True}
_FIG_SAVE_EVERY = 2.0
LONG_TASK_SECONDS = 60


def _figures_path():
    return _beside_db("library_figures.json")


def _figures_key():
    return [paths.subs_dir(), paths.books_dir(), paths.manga_dir()]


def _load_figures():
    try:
        with open(_figures_path(), encoding="utf-8") as fh:
            saved = json.load(fh)
        return saved if isinstance(saved, dict) else None
    except (OSError, ValueError):
        return None


def _save_figures(doc):
    try:
        tmp = _figures_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, _figures_path())
    except OSError:
        pass


def _walk_figures(key, shown):
    started = time.time()
    doc = {"key": key, "complete": False, "subs_disk": None, "books_disk": None, "manga_disk": None}
    last_save = 0.0

    def publish(save=False):
        nonlocal last_save
        with _LOCK:
            _FSTATE.update(subs_disk=doc["subs_disk"], books_disk=doc["books_disk"], manga_disk=doc["manga_disk"])
        if save or time.monotonic() - last_save > _FIG_SAVE_EVERY:
            last_save = time.monotonic()
            if not shown:
                _save_figures(doc)

    try:
        for name, root, ext, skip_dot in (("subs_disk", key[0], (".srt", ".ass", ".ssa"), False),
                                          ("books_disk", key[1], ".epub", True),
                                          ("manga_disk", key[2], ".mokuro", True)):
            def progress(found, other, name=name):
                doc[name] = {"files": found, "loose": 0, "other": other, "counting": True}
                publish()
            if root and os.path.isdir(root):
                doc[name] = {"files": 0, "loose": 0, "other": 0, "counting": True}
            doc[name] = _count_files(root, ext, skip_dot, progress, count_other=name != "manga_disk")
            publish()
        doc.update(complete=True, counted_at=time.time())
        _save_figures(doc)
    finally:
        seconds = time.time() - started
        with _LOCK:
            _FSTATE.update(running=False, finished_at=time.time(), saved=doc if doc["complete"] else None)
        if not shown and doc["complete"] and seconds > LONG_TASK_SECONDS:
            _add_notice("figures", seconds)
        if _FSTATE.get("again"):
            ensure_figures()


def ensure_figures():
    key = _figures_key()
    with _LOCK:
        if _FSTATE.get("running"):
            _FSTATE["again"] = _FSTATE.get("key") != key
            return
        saved = _FSTATE.get("saved") or _load_figures()
        good = bool(saved and saved.get("key") == key and saved.get("complete"))
        if good and not _FIG_STALE["stale"]:
            _FSTATE["saved"] = saved
            return
        _FIG_STALE["stale"] = False
        _FSTATE.update(running=True, key=key, again=False, started_at=time.time(), saved=saved if good else None,
                       subs_disk=None, books_disk=None, manga_disk=None)
    threading.Thread(target=_walk_figures, args=(key, good), daemon=True).start()


def figures():
    ensure_figures()
    with _LOCK:
        running = bool(_FSTATE.get("running"))
        saved = _FSTATE.get("saved")
        keys = ("subs_disk", "books_disk", "manga_disk")
        if saved and saved.get("key") == _figures_key():
            out = {k: saved.get(k) for k in keys}
        else:
            out = {k: _FSTATE.get(k) for k in keys}
    out = {k: (dict(v) if v else v) for k, v in out.items()}
    out["counting"] = running
    listed = filtered_rows(paths.filtered_list())
    for key, media in (("subs_disk", "subs"), ("books_disk", "epub"), ("manga_disk", "manga")):
        if out[key]:
            out[key]["filtered"] = sum(1 for r in listed if r["media"] == media)
    return out


_NOTICES = []
_NOTICE_IDS = iter(range(1, 1 << 62))


def _add_notice(kind, seconds, **extra):
    with _LOCK:
        _NOTICES.append({"id": next(_NOTICE_IDS), "kind": kind, "seconds": round(seconds),
                         "finished_at": time.time(), **extra})
        del _NOTICES[:-10]


def activity():
    with _LOCK:
        return {"index": bool(_STATE.get("running")), "check": bool(_ASTATE.get("running")),
                "figures": bool(_FSTATE.get("running")), "notices": [dict(n) for n in _NOTICES]}


def dismiss_notice(notice_id):
    with _LOCK:
        _NOTICES[:] = [n for n in _NOTICES if n["id"] != notice_id]


def describe(db_subs, db_epub, db_manga=None):
    subs, books = paths.subs_dir(), paths.books_dir()
    fig = figures()
    return {
        "installed": paths.INSTALLED,
        "media": paths.media_state(),
        "setup_needed": paths.setup_needed(),
        "default_folders": {k: paths.default_media_folder(k) for k in paths.MEDIA_KINDS},
        "subs_dir": subs,
        "books_dir": books,
        "manga_dir": paths.manga_dir(),
        "data_dir": paths.db_dir(),
        "db_default": os.path.join(paths.STORE_DIR, paths.DB_FOLDER),
        "db_is_default": _same_folder(paths.db_dir(), paths.default_db_dir()),
        "db_sizes": _db_sizes(paths.db_dir()),
        "port": paths.server_port(),
        "port_env": bool(os.environ.get("AOBANA_PORT")),
        "subs_disk": fig["subs_disk"],
        "books_disk": fig["books_disk"],
        "manga_disk": fig["manga_disk"],
        "counting": fig["counting"],
        "subs_indexed": _indexed(db_subs, "subtitles"),
        "books_indexed": _indexed(db_epub, "epubs"),
        "manga_indexed": _indexed(db_manga, "manga"),
        "subs_outdated": outdated_sources(db_subs, "subs"),
        "books_outdated": outdated_sources(db_epub, "epub"),
        "manga_outdated": outdated_sources(db_manga, "manga"),
        "subs_tables": missing_tables(db_subs, "subs"),
        "books_tables": missing_tables(db_epub, "epub"),
        "manga_tables": missing_tables(db_manga, "manga"),
        "index": index_status(),
    }


def set_folders(subs_dir, books_dir, manga_dir=None):
    if _STATE.get("running"):
        return "busy"
    cfg = paths.load_config()
    for key, value in (("subs_dir", subs_dir), ("books_dir", books_dir), ("manga_dir", manga_dir)):
        if value is None:
            continue
        value = os.path.expanduser(str(value).strip().strip('"'))
        if not os.path.isabs(value):
            return f"not_full:{key}"
        value = os.path.abspath(value)
        if not os.path.isdir(value):
            return f"not_found:{key}"
        cfg[key] = value
    paths.save_config(cfg)
    return None


def set_media(media):
    if _STATE.get("running"):
        return "busy"
    cfg = paths.load_config()
    state = {k: paths.media_enabled(k, cfg) for k in paths.MEDIA_KINDS}
    for kind, on in (media or {}).items():
        if kind not in paths.MEDIA_KINDS:
            continue
        state[kind] = bool(on)
        current = {"subs": paths.subs_dir, "books": paths.books_dir, "manga": paths.manga_dir}[kind]()
        if kind == "manga" and current and "manga_dir" not in cfg:
            current = None
        if on and not current:
            folder = paths.default_media_folder(kind)
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                return f"not_found:{paths.MEDIA_DIR_KEYS[kind]}"
            cfg[paths.MEDIA_DIR_KEYS[kind]] = folder
    cfg["media"] = state
    paths.save_config(cfg)
    _FIG_STALE["stale"] = True
    _after_db_change()
    return None


def finish_setup(media, subs_dir=None, books_dir=None, manga_dir=None, fresh=True):
    media = {k: bool((media or {}).get(k)) for k in paths.MEDIA_KINDS}
    if not any(media.values()):
        return "none_on"
    chosen = {}
    for kind, value in (("subs", subs_dir), ("books", books_dir), ("manga", manga_dir)):
        value = str(value or "").strip().strip('"')
        if media[kind] and value:
            folder = os.path.expanduser(value)
            if not os.path.isabs(folder):
                return f"not_full:{paths.MEDIA_DIR_KEYS[kind]}"
            folder = os.path.abspath(folder)
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                return f"not_found:{paths.MEDIA_DIR_KEYS[kind]}"
            chosen[kind] = folder
    err = set_folders(chosen.get("subs"), chosen.get("books"), chosen.get("manga"))
    if err:
        return err
    cfg = paths.load_config()
    if fresh:
        for key in paths.MEDIA_DIR_KEYS.values():
            cfg.setdefault(key, "")
        cfg.pop("check_asked", None)
    cfg["media_asked"] = True
    paths.save_config(cfg)
    err = set_media(media)
    if not err and fresh:
        _forget_last_run()
        _forget_check()
    return err


def check_asked():
    return bool(paths.load_config().get("check_asked"))


def media_asked():
    return bool(paths.load_config().get("media_asked"))


def mark_media_asked():
    cfg = paths.load_config()
    if not cfg.get("media_asked"):
        cfg["media_asked"] = True
        paths.save_config(cfg)


def mark_check_asked():
    cfg = paths.load_config()
    if not cfg.get("check_asked"):
        cfg["check_asked"] = True
        paths.save_config(cfg)


def _forget_last_run():
    with _LOCK:
        if not _STATE.get("running"):
            _STATE.clear()
            _STATE["running"] = False


def _forget_check():
    with _LOCK:
        if _ASTATE.get("running"):
            return
        _ASTATE.clear()
        _ASTATE["running"] = False
    folder = os.path.dirname(_report_path())
    for name in ("analysis.json", "analysis.db"):
        _remove_retrying(os.path.join(folder, name))


_DB_NAMES = ("subs.db", "epub.db", "manga.db")
_DB_SIDECARS = ("", "-wal", "-shm", "-journal")
_DB_OF_MEDIA = {"subs": "subs.db", "books": "epub.db", "manga": "manga.db"}


_DB_COMPANIONS = ("filtered.tsv", "analysis.json", "analysis.db")


def _db_files(folder):
    return ([n + s for n in _DB_NAMES for s in _DB_SIDECARS if os.path.isfile(os.path.join(folder, n + s))]
            + [n for n in _DB_COMPANIONS if os.path.isfile(os.path.join(folder, n))])


def _db_sizes(folder):
    return {n: (os.path.getsize(os.path.join(folder, n)) if os.path.isfile(os.path.join(folder, n)) else None)
            for n in _DB_NAMES}


def _same_folder(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def move_databases(target):
    with _LOCK:
        if _STATE.get("running") or _STATE.get("moving"):
            return "busy", []
        _STATE["moving"] = True
    try:
        return _move_databases(target)
    finally:
        with _LOCK:
            _STATE.pop("moving", None)


def _move_databases(target):
    raw = str(target or "").strip().strip('"')
    default = raw in ("", "default")
    dest = os.path.join(paths.STORE_DIR, paths.DB_FOLDER) if default else os.path.abspath(os.path.expanduser(raw))
    if default:
        os.makedirs(dest, exist_ok=True)
    src = paths.db_dir()
    if not os.path.isdir(dest):
        return "not_found", []
    if _same_folder(src, dest):
        return "same", []
    if any(os.path.exists(os.path.join(dest, n)) for n in _DB_NAMES):
        return "exists", []
    files = _db_files(src)
    probe = os.path.join(dest, ".aobana-write-test")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
    except OSError:
        return "not_writable", []
    from engine import clear_disk_cache
    clear_disk_cache()

    renamed, copied = [], []
    try:
        for name in files:
            s, d = os.path.join(src, name), os.path.join(dest, name)
            try:
                os.replace(s, d)
                renamed.append(name)
                continue
            except OSError:
                pass
            tmp = d + ".moving"
            shutil.copy2(s, tmp)
            if os.path.getsize(tmp) != os.path.getsize(s):
                raise OSError(f"size mismatch copying {name}")
            os.replace(tmp, d)
            copied.append(name)
        cfg = paths.load_config()
        if default:
            cfg.pop("db_dir", None)
        else:
            cfg["db_dir"] = dest
        paths.save_config(cfg)
    except OSError:
        for name in renamed:
            try:
                os.replace(os.path.join(dest, name), os.path.join(src, name))
            except OSError:
                pass
        for name in copied:
            try:
                os.remove(os.path.join(dest, name))
            except OSError:
                pass
        for name in files:
            try:
                os.remove(os.path.join(dest, name + ".moving"))
            except OSError:
                pass
        return "failed", []

    old_kept = [os.path.join(src, name) for name in copied if not _remove_retrying(os.path.join(src, name))]
    _after_db_change()
    return None, old_kept


def _remove_retrying(path):
    for attempt in range(10):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.3)
    return False


def drop_index(kind):
    name = _DB_OF_MEDIA.get(kind)
    if not name:
        return "unknown", []
    with _LOCK:
        if _STATE.get("running") or _STATE.get("moving") or _ASTATE.get("running"):
            return "busy", []
        _STATE["moving"] = True
    try:
        folder = paths.db_dir()
        files = [os.path.join(folder, name + s) for s in _DB_SIDECARS if os.path.isfile(os.path.join(folder, name + s))]
        if not files:
            return "none", []
        from engine import clear_disk_cache
        clear_disk_cache()
        _after_db_change(warm=False)
        kept = [f for f in files if not _remove_retrying(f)]
        _after_db_change()
        _FIG_STALE["stale"] = True
        _forget_last_run()
        return None, kept
    finally:
        with _LOCK:
            _STATE.pop("moving", None)


def _after_db_change(warm=True):
    try:
        from engine import reset_caches, warm_media_library
        reset_caches()
        if warm:
            threading.Thread(target=warm_media_library, daemon=True).start()
    except Exception:
        pass


def set_search_cache(on):
    cfg = paths.load_config()
    if on:
        cfg["search_cache"] = True
    else:
        cfg.pop("search_cache", None)
    paths.save_config(cfg)


def set_port(value):
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return "bad_port"
    if not 1024 <= port <= 65535:
        return "bad_port"
    cfg = paths.load_config()
    cfg["port"] = port
    paths.save_config(cfg)
    return None


HANDOFF_PATH = os.path.join(paths.STORE_DIR, "profile-handoff.json")


def _read_handoff():
    try:
        with open(HANDOFF_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) and isinstance(doc.get("items"), dict) else None
    except (OSError, ValueError):
        return None


def _write_handoff(doc):
    os.makedirs(paths.STORE_DIR, exist_ok=True)
    tmp = HANDOFF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    os.replace(tmp, HANDOFF_PATH)


def _string_items(items):
    return {str(k): v for k, v in items.items() if isinstance(v, str)} if isinstance(items, dict) else None


def save_profile_handoff(to_port, from_port, items):
    items = _string_items(items)
    if to_port == from_port or items is None:
        drop_profile_handoff(from_port)
        return
    _write_handoff({"to_port": to_port, "from_port": from_port, "written_at": time.time(), "items": items})


def refresh_profile_handoff(from_port, items):
    doc, items = _read_handoff(), _string_items(items)
    if doc is None or items is None or doc.get("from_port") != from_port:
        return
    doc["items"], doc["written_at"] = items, time.time()
    _write_handoff(doc)


def load_profile_handoff(port):
    doc = _read_handoff()
    return doc if doc is not None and doc.get("to_port") == port else None


def handoff_pending_from(port):
    doc = _read_handoff()
    return doc is not None and doc.get("from_port") == port


def drop_profile_handoff(port, written_at=None):
    doc = _read_handoff()
    if doc is None or port not in (doc.get("to_port"), doc.get("from_port")):
        return
    if written_at is not None and doc.get("written_at") != written_at:
        return
    try:
        os.remove(HANDOFF_PATH)
    except OSError:
        pass


def open_folder(which):
    target = {"subs": paths.subs_dir, "books": paths.books_dir, "manga": paths.manga_dir,
              "data": paths.db_dir}.get(which)
    if target is None:
        return "unknown"
    path = target()
    if not path or not os.path.isdir(path):
        return "not_found"
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            opener = "termux-open" if os.environ.get("TERMUX_VERSION") else "xdg-open"
            subprocess.Popen([opener, path])
    except Exception as e:
        return f"failed:{e}"
    return None


def index_status():
    with _LOCK:
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in _STATE.items()}
    if not out.get("running"):
        out["unfinished"] = _unfinished()
    return out


def stop_indexing():
    with _LOCK:
        if not _STATE.get("running"):
            return False
        _STATE["stopping"] = True
    try:
        open(_stop_file("index"), "w").close()
    except OSError:
        return False
    return True


def stop_analysis():
    with _LOCK:
        if not _ASTATE.get("running"):
            return False
        _ASTATE["stopping"] = True
    try:
        open(_stop_file("check"), "w").close()
    except OSError:
        return False
    return True


def start_indexing(only=None, outdated=False, tables=False):
    media = paths.media_state()
    stages = tuple(st for st in _STAGES if (only in (None, "", "all") or st[0] == only)
                   and media[_STAGE_MEDIA[st[0]]])
    def wanted(stage):
        root, ext, skip_dot, db = _stage_inputs(stage)
        return os.path.isfile(db) or (not tables and _has_files(root, ext, skip_dot))
    stages = tuple(st for st in stages if wanted(st[0]))
    if not stages:
        return "nothing"
    with _LOCK:
        if _STATE.get("running") or _STATE.get("moving") or _ASTATE.get("running"):
            return False
        _STATE.clear()
        _STATE.update({
            "running": True, "stage": stages[0][0], "stages": [st[0] for st in stages],
            "done": 0, "total": 0, "current": "",
            "started_at": time.time(), "finished_at": None, "error": None,
            "results": {}, "skipped_clash": [], "failed": [], "ignored_other": {}, "filtered": {},
            "root_missing": [], "root_not_set": [], "log": [], "stopping": False, "stopped": False,
            "outdated": bool(outdated), "tables": bool(tables), "phase": "",
        })
    _clear_stop("index")
    try:
        with open(_run_marker(), "w", encoding="utf-8") as fh:
            json.dump({"started_at": time.time(), "stages": [st[0] for st in stages],
                       "outdated": bool(outdated), "tables": bool(tables)}, fh)
    except OSError:
        pass
    threading.Thread(target=_run, args=(stages, outdated, tables), daemon=True).start()
    return True


def _set(**kw):
    with _LOCK:
        _STATE.update(kw)


def _run(stages, outdated=False, tables=False):
    env = dict(os.environ, AOBANA_PROGRESS="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               AOBANA_STOP_FILE=_stop_file("index"))
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        for stage, script in stages:
            _set(stage=stage, done=0, total=0, current="", phase="")
            proc = subprocess.Popen(
                [sys.executable, os.path.join(paths.BASE_DIR, script)] + (["--tables"] if tables else ["--outdated"] if outdated else []),
                cwd=paths.BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", creationflags=flags)
            for line in proc.stdout:
                _read_line(stage, line.rstrip("\r\n"))
            code = proc.wait()
            if code != 0:
                _set(error=f"{script} exited with code {code}")
                break
            if _STATE.get("stopped"):
                break
    except Exception as e:
        _set(error=f"{type(e).__name__}: {e}")
    finally:
        try:
            from engine import reset_caches
            reset_caches()
        except Exception:
            pass
        _set(running=False, stopping=False, stage="done", current="", finished_at=time.time())
        _clear_stop("index")
        _FIG_STALE["stale"] = True
        seconds = time.time() - (_STATE.get("started_at") or time.time())
        if seconds > LONG_TASK_SECONDS:
            _add_notice("index", seconds, stopped=bool(_STATE.get("stopped")), error=_STATE.get("error"))
        try:
            os.remove(_run_marker())
        except OSError:
            pass
        try:
            from engine import warm_media_library
            threading.Thread(target=warm_media_library, daemon=True).start()
        except Exception:
            pass


def _read_line(stage, line):
    with _LOCK:
        log = _STATE["log"]
        if not line.startswith("PROGRESS "):
            log.append(line)
            del log[:-200]
        if line.startswith("TOTAL "):
            _STATE.update(total=int(line.split()[1]), phase="")
        elif line.startswith(("CHAPTERS building", "LENGTHS building", "LEXICON building")):
            _STATE.update(phase=line.split()[0].lower(), done=0, total=0, current="")
        elif line.startswith("LENGTHS ") and "/" in line:
            done, _, total = line.split()[1].partition("/")
            _STATE.update(done=int(done), total=int(total))
        elif line.startswith("PROGRESS "):
            head, _, rel = line[len("PROGRESS "):].partition(" ")
            done, _, total = head.partition("/")
            _STATE.update(done=int(done), total=int(total), current=rel)
        elif line.startswith("SKIPPED_CLASH "):
            _STATE["skipped_clash"].append(line[len("SKIPPED_CLASH "):])
        elif line.startswith("IGNORED_OTHER "):
            _STATE["ignored_other"][stage] = int(line.split()[1])
        elif line.startswith("FILTERED "):
            _STATE["filtered"][stage] = int(line.split()[1])
        elif line.startswith("FAILED "):
            _STATE["failed"].append(line[len("FAILED "):])
        elif line.startswith("ROOT_MISSING "):
            _STATE["root_missing"].append(stage)
        elif line.startswith("ROOT_NOT_SET "):
            _STATE["root_not_set"].append(stage)
        elif line == "STOPPED":
            _STATE["stopped"] = True
        else:
            m = _SUMMARY_RE[stage].search(line)
            if m:
                unchanged, indexed, removed = map(int, m.groups())
                _STATE["results"][stage] = {"unchanged": unchanged, "indexed": indexed,
                                            "removed": removed}


_ASTATE = {"running": False}
FILTERABLE = ("bilingual", "other_language", "duplicate", "duplicate_kept")


def analysis_status():
    with _LOCK:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in _ASTATE.items()}


def start_analysis(only=None):
    only = only if only in ("subs", "epub") else None
    with _LOCK:
        if _ASTATE.get("running") or _STATE.get("running") or _STATE.get("moving"):
            return False
        _ASTATE.clear()
        _ASTATE.update(running=True, stage="subs" if only != "epub" else "epub", done=0, total=0,
                       current="", started_at=time.time(), finished_at=None, error=None, log=[],
                       stopping=False, stopped=False)
    _clear_stop("check")
    threading.Thread(target=_run_analysis, args=(only,), daemon=True).start()
    return True


def _run_analysis(only):
    env = dict(os.environ, AOBANA_PROGRESS="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               AOBANA_STOP_FILE=_stop_file("check"))
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    cmd = [sys.executable, os.path.join(paths.BASE_DIR, "analyser.py")] + (["--only", only] if only else [])
    try:
        proc = subprocess.Popen(cmd, cwd=paths.BASE_DIR, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                creationflags=flags)
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            with _LOCK:
                if line.startswith("PROGRESS "):
                    head, _, rel = line[len("PROGRESS "):].partition(" ")
                    done, _, total = head.partition("/")
                    _ASTATE.update(done=int(done), total=int(total), current=rel)
                elif line.startswith("STAGE "):
                    _ASTATE.update(stage=line.split()[1], done=0, total=0, current="")
                elif line.startswith("TOTAL "):
                    _ASTATE["total"] = int(line.split()[1])
                elif line == "STOPPED":
                    _ASTATE["stopped"] = True
                else:
                    _ASTATE["log"].append(line)
                    del _ASTATE["log"][:-200]
        if proc.wait() != 0:
            with _LOCK:
                _ASTATE["error"] = f"analyser.py exited with code {proc.returncode}"
    except Exception as e:
        with _LOCK:
            _ASTATE["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _LOCK:
            _ASTATE.update(running=False, stopping=False, current="", finished_at=time.time())
        _clear_stop("check")
        seconds = time.time() - (_ASTATE.get("started_at") or time.time())
        if seconds > LONG_TASK_SECONDS:
            _add_notice("check", seconds, stopped=bool(_ASTATE.get("stopped")), error=_ASTATE.get("error"))


def _report_path():
    return os.path.join(os.path.dirname(os.path.abspath(paths.subs_db())), "analysis.json")


def analysis_report():
    try:
        with open(_report_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_filtered(rows):
    path = paths.filtered_list()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\t".join(FILTER_COLUMNS) + "\n")
        for r in rows:
            fh.write("\t".join(str(r.get(c, "")).replace("\t", " ").replace("\n", " ")
                               for c in FILTER_COLUMNS) + "\n")
    os.replace(tmp, path)


def filtered_list():
    roots = {"subs": paths.subs_dir(), "epub": paths.books_dir()}
    out = []
    for r in filtered_rows(paths.filtered_list()):
        root = roots.get(r["media"])
        p = os.path.join(root, r["name"]) if root else ""
        if root and r["media"] == "subs" and not os.path.isfile(p):
            p = os.path.join(root, os.path.basename(r["name"]))
        out.append(dict(r, exists=bool(root) and os.path.isfile(p)))
    return out


def filter_flagged(ids):
    report = analysis_report()
    if not report:
        return "no_report", None
    with _LOCK:
        if _STATE.get("running") or _STATE.get("moving") or _ASTATE.get("running"):
            return "busy", None
        _STATE["moving"] = True
    try:
        roots = {"subs": paths.subs_dir(), "epub": paths.books_dir()}
        rows = filtered_rows(paths.filtered_list())
        have = {(r["media"], r["name"]) for r in rows}
        added, refused = [], []
        id_set = set(ids)
        for kept in [it for it in report["items"] if it["reason"] == "duplicate_kept" and it["id"] in id_set]:
            group = [it for it in report["items"]
                     if it["media"] == kept["media"] and it.get("keep") == kept["keep"]]
            stay = [it for it in group if it["id"] not in id_set and not it.get("filtered")]
            if stay:
                new_keep = stay[0]
                kept["reason"] = "duplicate"
                kept["how"] = new_keep.get("how", "same_bytes" if kept["media"] == "subs" else "same_text")
                kept["share"] = new_keep.get("share", 1.0)
                new_keep["reason"] = "duplicate_kept"
                for it in group:
                    it["keep"] = new_keep["name"]
                    it["keep_path"] = new_keep["path"]
        for item in report["items"]:
            if item["id"] not in id_set:
                continue
            media = item["media"]
            if (item["reason"] not in FILTERABLE or not roots.get(media)
                    or not _same_folder(roots[media], report["roots"][media])):
                refused.append(item["path"])
                continue
            reason = "duplicate" if item["reason"] == "duplicate_kept" else item["reason"]
            if (media, item["name"]) not in have:
                rows.append({"media": media, "name": item["name"], "reason": reason,
                             "keep": item.get("keep", ""), "date": time.strftime("%Y-%m-%d %H:%M:%S")})
                have.add((media, item["name"]))
            item["filtered"] = True
            added.append(item["id"])
        _write_filtered(rows)
        tmp = _report_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _report_path())
        return None, {"filtered": added, "refused": refused}
    finally:
        with _LOCK:
            _STATE.pop("moving", None)


def unfilter(entries):
    with _LOCK:
        if _STATE.get("running") or _STATE.get("moving") or _ASTATE.get("running"):
            return "busy", 0
        _STATE["moving"] = True
    try:
        drop = {(str(m), str(n)) for m, n in entries}
        rows = filtered_rows(paths.filtered_list())
        kept = [r for r in rows if (r["media"], r["name"]) not in drop]
        if len(kept) != len(rows):
            _write_filtered(kept)
        return None, len(rows) - len(kept)
    finally:
        with _LOCK:
            _STATE.pop("moving", None)


def estimate(only=None):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    cmd = [sys.executable, os.path.join(paths.BASE_DIR, "analyser.py"), "--estimate"]
    if only in ("subs", "epub"):
        cmd += ["--only", only]
    try:
        out = subprocess.run(cmd, cwd=paths.BASE_DIR, capture_output=True, encoding="utf-8",
                             errors="replace", timeout=300, creationflags=flags,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
