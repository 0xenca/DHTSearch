"""Text and classification helpers shared by the store and the search engine."""
import re
import sys
import unicodedata
from collections import Counter
from urllib.parse import quote, urlsplit

from packing import hd_tracker_urls, unpack_hd

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ALNUM_SPLIT = re.compile(r"\d+|[a-z]+")

CATEGORIES = {
    "Video": {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "ts", "m2ts", "vob", "iso_video"},
    "Audio": {"mp3", "flac", "wav", "aac", "ogg", "opus", "m4a", "wma", "ape", "alac"},
    "Images": {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "webp", "svg", "raw", "psd"},
    "Documents": {"pdf", "epub", "mobi", "azw3", "djvu", "doc", "docx", "txt", "rtf", "odt", "cbz", "cbr", "xls", "xlsx", "ppt", "pptx"},
    "Software": {"exe", "msi", "dmg", "pkg", "deb", "rpm", "apk", "appimage", "iso", "img", "jar", "bin"},
    "Archives": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst"},
    "Data": {"csv", "json", "xml", "sql", "db", "parquet", "dat"},
}
_EXT_TO_CAT = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}
ALL_CATEGORIES = list(CATEGORIES) + ["Other"]

# Category names written by versions <= 2.9 (Spanish). Since 3.0 everything is stored in English; these are only READ:
# a journal that still has them is converted once at startup, and migrate_es.py uses them to import old data.
LEGACY_CATEGORIES = {"Vídeo": "Video", "Imágenes": "Images", "Documentos": "Documents", "Comprimidos": "Archives",
                     "Datos": "Data", "Otros": "Other"}

# aliases accepted in the query (cat:video, cat:docs …); the Spanish ones are kept for old links
_CAT_ALIASES = {
    "video": "Video", "videos": "Video", "movie": "Video", "movies": "Video", "pelicula": "Video", "peliculas": "Video",
    "audio": "Audio", "music": "Audio", "sound": "Audio", "musica": "Audio", "sonido": "Audio",
    "images": "Images", "image": "Images", "img": "Images", "photo": "Images", "photos": "Images",
    "imagen": "Images", "imagenes": "Images", "foto": "Images", "fotos": "Images",
    "doc": "Documents", "docs": "Documents", "document": "Documents", "documents": "Documents", "ebook": "Documents",
    "ebooks": "Documents", "books": "Documents", "documento": "Documents", "documentos": "Documents", "libros": "Documents",
    "software": "Software", "program": "Software", "programs": "Software", "app": "Software", "apps": "Software", "iso": "Software",
    "programa": "Software", "programas": "Software",
    "archive": "Archives", "archives": "Archives", "compressed": "Archives", "zip": "Archives",
    "comprimido": "Archives", "comprimidos": "Archives",
    "data": "Data", "dato": "Data", "datos": "Data",
    "other": "Other", "others": "Other", "otro": "Other", "otros": "Other",
}

SIZE_BUCKETS = [
    ("< 1 MB", 1 << 20),
    ("1–100 MB", 100 << 20),
    ("100 MB–1 GB", 1 << 30),
    ("1–10 GB", 10 << 30),
    ("10–50 GB", 50 << 30),
    ("> 50 GB", float("inf")),
]
SEED_BUCKETS = [
    ("0", 0), ("1–5", 5), ("6–20", 20), ("21–100", 100), ("101–1,000", 1000), ("> 1,000", float("inf")),
]
AGE_BUCKETS = [("Last 24 h", 86400), ("Last 7 days", 7 * 86400), ("Last 30 days", 30 * 86400), ("Older", float("inf"))]


# every combining mark (accents…) -> removed, with str.translate (C speed; same result as filtering unicodedata.combining)
_COMBINING = {c: None for c in range(sys.maxunicode + 1) if unicodedata.combining(chr(c))}


def norm(s: str) -> str:
    s = s.lower()
    if s.isascii():                       # fast path: the vast majority of paths
        return s
    return unicodedata.normalize("NFKD", s).translate(_COMBINING)


def tokenize(s: str):
    """Tokens of a QUERY or of normalized text (lowercase, no accents)."""
    return _TOKEN_RE.findall(norm(s))


_LETTERS = set("abcdefghijklmnopqrstuvwxyz")
_MIXED_RE = re.compile(r"(?<![a-z0-9])(?=[a-z0-9]*[a-z])(?=[a-z0-9]*[0-9])[a-z0-9]+")
_DIGITS3 = re.compile(r"\d{3,}")


def index_tokens(s: str):
    """Tokens for the INDEX: the base ones + the numeric part of mixed tokens (1080p -> 1080, x264 -> 264),
    so that searching "1080" finds "1080p". Single letters are dropped. (All regex, i.e. in C: this is the most
    expensive part of startup.)"""
    n = norm(s)
    out = set(_TOKEN_RE.findall(n))
    out -= _LETTERS
    for m in _MIXED_RE.findall(n):
        out.update(_DIGITS3.findall(m))
    return out


def norm_map(s: str):
    """Normalized version of s and, for each normalized character, the index of the original character
    (used to highlight matches on the original text)."""
    out, mp = [], []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKD", ch.lower()):
            if not unicodedata.combining(c):
                out.append(c)
                mp.append(i)
    return "".join(out), mp


def ext_of(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()[:8]


def category_of(files, name):
    """Dominant category by bytes, from the file extensions."""
    by_cat = Counter()
    for path, size in files:
        by_cat[_EXT_TO_CAT.get(ext_of(path), "Other")] += size or 1
    if not by_cat:
        by_cat[_EXT_TO_CAT.get(ext_of(name), "Other")] += 1
    return by_cat.most_common(1)[0][0]


def top_exts(files, n=8):
    """Distinct extensions of the torrent, heaviest (in bytes) first."""
    by = Counter()
    for path, size in files:
        e = ext_of(path)
        if e:
            by[e] += size or 1
    return [e for e, _ in by.most_common(n)]


def resolve_cat(value: str):
    v = norm(value.strip())
    if v in _CAT_ALIASES:
        return _CAT_ALIASES[v]
    for c in ALL_CATEGORIES:
        if norm(c) == v:
            return c
    old = LEGACY_CATEGORIES.get(value.strip())           # old URLs with the Spanish name (cat=Vídeo)
    if old:
        return old
    for legacy, c in LEGACY_CATEGORIES.items():
        if norm(legacy) == v:
            return c
    return None


def _bucket(x, buckets, inclusive):
    for label, limit in buckets:
        if (x <= limit) if inclusive else (x < limit):
            return label
    return buckets[-1][0]


def size_bucket(size: int) -> str:
    return _bucket(size, SIZE_BUCKETS, False)


def seed_bucket(n: int) -> str:
    return _bucket(n, SEED_BUCKETS, True)


def tracker_host(url):
    """"udp://tracker.opentrackr.org:1337/announce" -> "tracker.opentrackr.org" """
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def health_brief(rec):
    """Compact breakdown of the latest measurement, for result lists: who reports what."""
    d = unpack_hd(rec.get("hd"))
    if not d:
        return None
    return {"at": d.get("at", 0), "cs": d.get("cs", 0), "cp": d.get("cp", 0), "dht": d.get("dht", 0), "md": d.get("md", 0),
            "tr": [{"h": tracker_host(t.get("u", "")), "s": t.get("s"), "l": t.get("l"), "r": t.get("r"),
                    "e": t.get("e") or ("" if "s" in t else "no response")} for t in d.get("tr") or []]}


def magnet_trackers(rec, limit=20):
    """Trackers for the magnet link: first those that ANSWERED in the latest measurement (most seeders first), then the
    rest of the queried ones, and finally the torrent's own. (Previously only the torrent's own were used, and those almost
    never arrive via DHT: the magnet had no &tr= at all, so the client relied on DHT alone while seeders were being counted
    on the trackers.)"""
    tr = hd_tracker_urls(rec.get("hd"))                    # [(url, seeders, answered)]
    ok = sorted((t for t in tr if t[2]), key=lambda t: -(t[1] or 0))
    urls = [t[0] for t in ok] + [t[0] for t in tr] + list(rec.get("trackers") or [])
    out = []
    for u in urls:
        if u and u != "?" and u not in out:
            out.append(u)
    return out[:limit]


def magnet_of(rec) -> str:
    m = f"magnet:?xt=urn:btih:{rec['ih']}&dn={quote(rec.get('name') or rec['ih'])}"
    for tr in magnet_trackers(rec):
        m += "&tr=" + quote(tr, safe="")
    return m
