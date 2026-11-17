"""PostScript job -> PDF / TIFF / JPG / DOCX / TXT."""
from __future__ import annotations

import getpass
import glob
import logging
import ntpath
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QObject, Signal, Slot

from .config import FORMATS, TIMESTAMP_STYLES, Settings, spool_dir
from .ghostscript import GhostscriptError, find_ghostscript, run_gs

log = logging.getLogger("medusa")

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
APP_PREFIXES = ("Microsoft Word - ", "Microsoft Excel - ", "Microsoft PowerPoint - ", "Microsoft Visio - ",
                "Microsoft Publisher - ")
APP_NAMES = ("Notepad", "Notepad++", "Word", "Excel", "PowerPoint", "Paint", "Photos", "WordPad",
             "Google Chrome", "Microsoft Edge", "Mozilla Firefox", "Brave", "Opera",
             "Adobe Acrobat Reader", "Adobe Acrobat Reader (64-bit)", "Adobe Acrobat", "Adobe Acrobat Pro",
             "PDFgear", "Foxit PDF Reader", "SumatraPDF", "LibreOffice Writer", "LibreOffice Calc",
             "Visual Studio Code", "IrfanView", "Outlook", "Microsoft Outlook", "Message (HTML)",
             "Message (Plain Text)", "Message (Rich Text)")
DOC_EXTS = ("docx", "docm", "doc", "dotx", "xlsx", "xlsm", "xlsb", "xls", "pptx", "ppt", "pdf", "txt", "log",
            "rtf", "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "heic", "htm", "html", "mht", "csv",
            "odt", "ods", "odp", "xml", "json", "md", "msg", "eml", "vsdx", "pub")
_EXT_ALT = "|".join(sorted(DOC_EXTS, key=len, reverse=True))
_TRAILING_EXT = re.compile(rf"\.(?:{_EXT_ALT})$", re.IGNORECASE)
_EMBEDDED_FILE = re.compile(rf"([^\\/:*?\"<>|]+?\.(?:{_EXT_ALT}))(?=$|[\s\]\)\-–—|,;])", re.IGNORECASE)
GENERIC_TITLES = {"", "document", "untitled", "print", "printing", "print job", "local downloaded document",
                  "microsoft word", "microsoft excel", "about:blank", "new tab", "-"}


class ConversionError(Exception):
    pass


@dataclass
class PrintJob:
    ps_path: str          # raw spooled data
    title: str
    fmt: str = "pdf"
    target: str = ""      # full output path incl. extension
    overwrite: bool = False


# --------------------------------------------------------------------------- title & naming
def _decode_ps_string(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        hexdata = re.sub(r"\s", "", raw[1:-1])
        try:
            data = bytes.fromhex(hexdata)
            if data.startswith(b"\xfe\xff"):
                return data[2:].decode("utf-16-be", errors="replace")
            return data.decode("latin-1")
        except ValueError:
            return raw
    if raw.startswith("(") and raw.endswith(")"):
        body = raw[1:-1]
        out, i = [], 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                n = body[i + 1]
                m = re.match(r"[0-7]{1,3}", body[i + 1:])
                if m:
                    out.append(chr(int(m.group(0), 8)))
                    i += 1 + len(m.group(0))
                    continue
                out.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(n, n))
                i += 2
                continue
            out.append(c)
            i += 1
        text = "".join(out)
        if text.startswith("\xfe\xff"):
            try:
                return text[2:].encode("latin-1").decode("utf-16-be", errors="replace")
            except UnicodeEncodeError:
                pass
        return text
    return raw


def extract_title(ps_path: str) -> str:
    try:
        with open(ps_path, "rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return ""
    m = re.search(rb"^%%Title:[ \t]*(.*?)\r?$", head, re.MULTILINE)
    if not m:
        return ""
    return _decode_ps_string(m.group(1).decode("latin-1"))


def smart_title(title: str) -> str:
    """Best guess at the printed document's own file name (without extension). '' if none.

    'Microsoft Word - Invoice 42.docx'           -> 'Invoice 42'
    '*Notes.txt - Notepad'                       -> 'Notes'
    'C:\\Users\\me\\Desktop\\Quote 7.pdf'           -> 'Quote 7'
    'Quote 7.pdf - Adobe Acrobat Reader (64-bit)' -> 'Quote 7'
    'Monthly Report - Excel'                     -> 'Monthly Report'
    'Untitled' / ''                              -> ''
    """
    t = (title or "").replace("\x00", "").strip()
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()
    t = t.lstrip("*●• ").strip()                       # unsaved-changes markers
    if "://" in t:                                     # URL / file:/// – keep the path, decode %20 etc.
        try:
            parts = urlsplit(t)
            path = unquote(parts.path).rstrip("/")
            t = path if path else parts.netloc
        except ValueError:
            pass
    for p in APP_PREFIXES:
        if t.lower().startswith(p.lower()):
            t = t[len(p):].strip()
    changed = True
    while changed:                                     # 'Doc.docx - Compatibility Mode - Word'
        changed = False
        for app in APP_NAMES + ("Compatibility Mode", "Protected View", "Read-Only", "Saved", "AutoRecovered"):
            for sep in (" - ", " – ", " — ", " | "):
                suffix = sep + app
                if t.lower().endswith(suffix.lower()):
                    t = t[: -len(suffix)].strip()
                    changed = True
    t = re.sub(r"\s*\[(?:Compatibility Mode|Read-Only|Protected View)\]\s*$", "", t, flags=re.IGNORECASE)

    # A file name embedded anywhere (full path, 'Doc.pdf (page 1)', 'Doc.pdf - something') wins.
    if not _TRAILING_EXT.search(t):
        m = _EMBEDDED_FILE.search(t.replace("/", "\\"))
        if m:
            t = m.group(1)
    if "\\" in t or "/" in t:
        t = ntpath.basename(t.replace("/", "\\").rstrip("\\"))
    t = _TRAILING_EXT.sub("", t.strip())
    t = INVALID_CHARS.sub("_", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ._-")
    if t.lower() in GENERIC_TITLES:
        return ""
    return t[:120]


def clean_title(title: str) -> str:
    """Display / PDF metadata title – never empty."""
    return smart_title(title) or "Document"


def timestamp_text(style: str, when: float | None = None) -> str:
    fmt = TIMESTAMP_STYLES.get(style, TIMESTAMP_STYLES["iso"])[0]
    return time.strftime(fmt, time.localtime(when))


def _sanitize(name: str) -> str:
    name = INVALID_CHARS.sub("_", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" .")
    name = re.sub(r"^[_\- ]+|[_\- ]+$", "", name)
    return name[:180]


def build_filename(template: str, title: str, timestamp_style: str = "iso", fallback_prefix: str = "Print") -> str:
    """Expand a custom template. Tokens: {title} {timestamp} {date} {time} {datetime} {user}."""
    now = time.time()
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    lt = time.localtime(now)
    title_text = smart_title(title) or (fallback_prefix or "Document")
    name = template or "{title}"
    for token, value in (("{title}", title_text), ("{timestamp}", timestamp_text(timestamp_style, now)),
                         ("{date}", time.strftime("%Y-%m-%d", lt)), ("{time}", time.strftime("%H-%M-%S", lt)),
                         ("{datetime}", time.strftime("%Y-%m-%d_%H-%M-%S", lt)), ("{user}", user)):
        name = name.replace(token, value)
    return _sanitize(name) or "Document"


def make_filename(s: Settings, title: str) -> str:
    """Output file name (without extension) according to the File naming setting."""
    prefix = s.timestamp_prefix.strip()
    stamp = timestamp_text(s.timestamp_style)
    smart = smart_title(title)
    if s.filename_mode == "custom":
        return build_filename(s.filename_template, title, s.timestamp_style, prefix or "Print")
    if s.filename_mode == "timestamp":
        name = f"{prefix}_{stamp}" if prefix else stamp
    elif s.filename_mode == "smart_timestamp":
        name = f"{smart}_{stamp}" if smart else (f"{prefix}_{stamp}" if prefix else stamp)
    else:  # smart – fall back to a timestamp when the app sent no usable title
        name = smart or (f"{prefix}_{stamp}" if prefix else stamp)
    return _sanitize(name) or stamp


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


# --------------------------------------------------------------------------- PostScript cleanup
def clean_postscript(src: str, dst: str) -> None:
    """Strip PJL headers before '%!PS' and anything (UEL etc.) after the final '%%EOF'."""
    size = os.path.getsize(src)
    with open(src, "rb") as fh:
        head = fh.read(min(size, 256 * 1024))
        start = head.find(b"%!")
        if start < 0:
            raise ConversionError("Received data is not PostScript – is the printer using a PostScript driver?")
        tail_len = min(size, 8192)
        fh.seek(size - tail_len)
        tail = fh.read(tail_len)
        eof = tail.rfind(b"%%EOF")
        end = size if eof < 0 else size - tail_len + eof + len(b"%%EOF")
        if end <= start:
            end = size
        fh.seek(start)
        remaining = end - start
        with open(dst, "wb") as out:
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
            out.write(b"\n")


# --------------------------------------------------------------------------- Ghostscript conversions
def _pdf_quality_args(quality: str) -> list[str]:
    common = ["-dEmbedAllFonts=true", "-dSubsetFonts=true", "-dCompatibilityLevel=1.7",
              "-dAutoRotatePages=/None", "-dDetectDuplicateImages=true",
              "-dColorConversionStrategy=/LeaveColorUnchanged"]
    if quality == "maximum":
        return common + [
            "-dPDFSETTINGS=/prepress",
            "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false", "-dDownsampleMonoImages=false",
            "-dAutoFilterColorImages=false", "-dAutoFilterGrayImages=false",
            "-dColorImageFilter=/FlateEncode", "-dGrayImageFilter=/FlateEncode",
            "-dPassThroughJPEGImages=true",
        ]
    preset = {"high": "/prepress", "standard": "/printer", "small": "/ebook"}.get(quality, "/prepress")
    return common + [f"-dPDFSETTINGS={preset}"]


def _pdfmark_hex(text: str) -> str:
    return "<FEFF" + text.encode("utf-16-be").hex().upper() + ">"


def ps_to_pdf(gs: str, ps: str, pdf: str, quality: str, title: str) -> None:
    docinfo = f"[ /Title {_pdfmark_hex(clean_title(title))} /Creator (MedusaPDF) /DOCINFO pdfmark"
    run_gs(gs, ["-sDEVICE=pdfwrite", *_pdf_quality_args(quality), f"-sOutputFile={pdf}",
                "-c", docinfo, "-f", ps])


def _raster_aa() -> list[str]:
    return ["-dTextAlphaBits=4", "-dGraphicsAlphaBits=4", "-dInterpolateControl=-1"]


def pdf_to_tiff(gs: str, pdf: str, out: str, dpi: int, mode: str) -> None:
    if mode == "bw":
        args = ["-sDEVICE=tiffg4"]
    elif mode == "gray":
        args = ["-sDEVICE=tiffgray", "-sCompression=lzw", *_raster_aa()]
    else:
        args = ["-sDEVICE=tiff24nc", "-sCompression=lzw", *_raster_aa()]
    run_gs(gs, [*args, f"-r{dpi}", f"-sOutputFile={out}", pdf])


def pdf_to_jpgs(gs: str, pdf: str, workdir: str, dpi: int, quality: int) -> list[str]:
    pattern = os.path.join(workdir, "page_%04d.jpg")
    run_gs(gs, ["-sDEVICE=jpeg", f"-dJPEGQ={quality}", f"-r{dpi}", *_raster_aa(),
                f"-sOutputFile={pattern}", pdf])
    return sorted(glob.glob(os.path.join(workdir, "page_*.jpg")))


def pdf_to_txt(gs: str, pdf: str, out: str) -> None:
    run_gs(gs, ["-sDEVICE=txtwrite", f"-sOutputFile={out}", pdf])


def pdf_to_docx(pdf: str, out: str) -> None:
    try:
        from pdf2docx import Converter
    except ImportError:
        raise ConversionError("DOCX output needs the 'pdf2docx' package (pip install pdf2docx)")
    cv = Converter(pdf)
    try:
        cv.convert(out)
    finally:
        cv.close()


# --------------------------------------------------------------------------- job pipeline
def process_job(job: PrintJob, s: Settings) -> list[str]:
    gs = find_ghostscript(s.gs_path)
    if not gs:
        raise ConversionError("Ghostscript is not installed – install it, or set its path in Settings")

    workdir = tempfile.mkdtemp(prefix="job_", dir=spool_dir())
    try:
        ps = os.path.join(workdir, "job.ps")
        clean_postscript(job.ps_path, ps)
        pdf = os.path.join(workdir, "document.pdf")
        ps_to_pdf(gs, ps, pdf, s.pdf_quality, job.title)
        if not os.path.isfile(pdf) or os.path.getsize(pdf) == 0:
            raise ConversionError("Ghostscript produced no PDF output")

        if job.fmt == "pdf":
            produced = [pdf]
        elif job.fmt == "tiff":
            out = os.path.join(workdir, "document.tif")
            pdf_to_tiff(gs, pdf, out, s.image_dpi, s.tiff_mode)
            produced = [out]
        elif job.fmt == "jpg":
            produced = pdf_to_jpgs(gs, pdf, workdir, s.image_dpi, s.jpeg_quality)
        elif job.fmt == "txt":
            out = os.path.join(workdir, "document.txt")
            pdf_to_txt(gs, pdf, out)
            produced = [out]
        elif job.fmt == "docx":
            out = os.path.join(workdir, "document.docx")
            pdf_to_docx(pdf, out)
            produced = [out]
        else:
            raise ConversionError(f"Unknown output format '{job.fmt}'")

        produced = [p for p in produced if os.path.isfile(p)]
        if not produced:
            raise ConversionError("Conversion produced no output files")

        os.makedirs(os.path.dirname(job.target) or ".", exist_ok=True)
        stem, ext = os.path.splitext(job.target)
        final_paths = []
        for i, src in enumerate(produced, start=1):
            dest = job.target if len(produced) == 1 else f"{stem}_p{i:03d}{ext}"
            if not job.overwrite:
                dest = unique_path(dest)
            elif os.path.exists(dest):
                os.remove(dest)
            shutil.move(src, dest)
            final_paths.append(dest)
        return final_paths
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            os.remove(job.ps_path)
        except OSError:
            pass


class ConversionWorker(QObject):
    """Runs jobs one at a time in a background QThread."""
    job_done = Signal(str, list)        # title, output paths
    job_failed = Signal(str, str)       # title, error

    @Slot(object, object)
    def convert(self, job: PrintJob, settings: Settings) -> None:
        name = clean_title(job.title)
        log.info("Converting '%s' → %s …", name, FORMATS[job.fmt][0])
        started = time.monotonic()
        try:
            paths = process_job(job, settings)
        except (ConversionError, GhostscriptError) as e:
            log.error("FAILED '%s': %s", name, e)
            self.job_failed.emit(name, str(e))
            return
        except Exception as e:
            log.exception("FAILED '%s': unexpected error", name)
            self.job_failed.emit(name, str(e) or e.__class__.__name__)
            return
        secs = time.monotonic() - started
        if len(paths) == 1:
            log.info("Saved '%s' (%.1fs).", paths[0], secs)
        else:
            log.info("Saved %d files to '%s' (%.1fs).", len(paths), os.path.dirname(paths[0]), secs)
        self.job_done.emit(name, paths)


# --------------------------------------------------------------------------- test page
TEST_PAGE_PS = r"""%!PS-Adobe-3.0
%%Title: MedusaPDF Test Page
%%Creator: MedusaPDF
%%Pages: 1
%%EndComments
<< /PageSize [595 842] >> setpagedevice
%%Page: 1 1
0 0.45 0.35 setrgbcolor
40 732 515 70 rectfill
1 1 1 setrgbcolor
/Helvetica-Bold findfont 34 scalefont setfont
60 755 moveto (MedusaPDF Test Page) show
0 0 0 setrgbcolor
/Times-Roman findfont 14 scalefont setfont
60 700 moveto (If you can read this, PostScript to output conversion is working.) show
/Helvetica findfont 9 scalefont setfont
60 680 moveto (Small text stays sharp because it is kept as vector fonts, not pixels.) show
/Courier findfont 12 scalefont setfont
60 655 moveto (Courier 12 pt - 0123456789 - ABCDEFGHIJKLMNOPQRSTUVWXYZ) show
0 1 99 { /i exch def
  i 100 div 0.2 1 i 100 div sub setrgbcolor
  60 i 4.75 mul add 560 4.75 60 rectfill } for
0.85 0.1 0.1 setrgbcolor newpath 160 420 70 0 360 arc closepath fill
0.1 0.6 0.2 setrgbcolor 265 350 130 140 rectfill
0.1 0.3 0.85 setrgbcolor newpath 475 350 moveto 535 490 lineto 415 490 lineto closepath fill
0 setgray 2 setlinewidth 40 40 515 762 rectstroke
/Helvetica-Oblique findfont 10 scalefont setfont
60 60 moveto (Generated by MedusaPDF - free software built on Ghostscript) show
showpage
%%EOF
"""


def write_test_job() -> str:
    path = os.path.join(spool_dir(), f"test_{time.strftime('%Y%m%d-%H%M%S')}.ps")
    with open(path, "w", encoding="latin-1", newline="\n") as fh:
        fh.write(TEST_PAGE_PS)
    return path
