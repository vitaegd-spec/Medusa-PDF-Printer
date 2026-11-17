# MedusaPDF – free virtual PDF printer

MedusaPDF is a free replacement for Bullzip PDF Printer, built with Python 3.13 and PySide6. It adds a printer called **MedusaPDF** to Windows. Anything you print to it (text, images, Office documents, web pages) is saved as **PDF, TIFF, JPG, DOCX or TXT**.

Every part is free/open source:
- the Windows built-in PostScript driver
- **Ghostscript** (AGPL)
- **pdf2docx**/PyMuPDF (AGPL)
- PySide6 (LGPL)

## How it works
```
Any app ─► "MedusaPDF" printer (built-in Windows PostScript driver)
        ─► Standard TCP/IP port 127.0.0.1:9100 (RAW)
        ─► MedusaPDF app (tray) ─► Ghostscript ─► PDF ─► TIFF / JPG / TXT / DOCX
```
MedusaPDF must be running (in the tray) to receive print jobs, so turn on **Start with Windows**.

## Setup (once)
1. Install **Ghostscript 64-bit** (free): https://ghostscript.com/releases/gsdnld.html
2. Run `run.bat`, or build the EXE with `build_exe.bat` → `dist\MedusaPDF.exe`.
3. On the **Status** tab, click **Install printer** and approve the admin prompt.
4. Click **Test page** to check that everything works.

## Settings
| Setting | Notes |
|---|---|
| Default destination | If set, files are saved there automatically with **no dialog**. If empty, a **Save as** dialog appears for every job, where you can also choose the format. |
| Output format | PDF, TIFF (multi-page), JPG (one file per page: `name_p001.jpg`…), DOCX, TXT |
| File naming | **Smart title** (default): the output gets the same name as the printed document (e.g. `Microsoft Word - Invoice 42.docx` → `Invoice 42.pdf`). **Smart title + timestamp** → `Invoice 42_2026-11-17_15-34-30.pdf`. **Timestamp only** → `Print_2026-11-17_15-34-30.pdf`. **Custom template** → your own pattern. If an app sends no usable name, the timestamp name is used. A live preview is shown in Settings. |
| Timestamp style / prefix | `2026-11-17_15-34-30`, `20261117-153430`, `2026-11-17 15.34.30` or date only. The prefix (default `Print`) is used for timestamp-only names; leave it empty for a bare timestamp. |
| Custom template | Tokens: `{title}` (smart title), `{timestamp}` (chosen style), `{date}`, `{time}`, `{datetime}`, `{user}` |
| If file exists | Add a number (`Invoice (2).pdf`) or overwrite |
| PDF quality | **Maximum** (default): fonts embedded, images lossless with no downsampling. High = prepress 300 dpi. Standard = printer. Small = eBook 150 dpi. |
| TIFF/JPG | Resolution (default 300 dpi), JPG quality, TIFF colour / grey / black & white (G4) |
| Printer | Printer name, local port (default 9100), default paper size (A4). Click **Install printer** again after changing these. |

## Getting the crispest text
Text stays as real vector fonts, so it remains sharp at any zoom. For exact font matching (instead of Windows swapping in look-alike printer fonts), make these changes:
1. Open **Printer preferences** → **Advanced…**.
2. Set **TrueType Font** to **Download as Softfont**.
3. Under *PostScript Options*, set **TrueType Font Download Option** to **Outline**.

## Using with Hermes
To have printed documents auto-print elsewhere, set MedusaPDF's default destination to Hermes' watch folder, or the reverse: point Hermes at MedusaPDF's output.

## Files
- `main.py` – entry point (single instance, tray)
- `medusa/listener.py` – loopback socket that receives PostScript jobs
- `medusa/converter.py` – Ghostscript / pdf2docx conversion, file naming, test page
- `medusa/printer_setup.py` + `scripts/*.ps1` – install/remove the printer (admin)
- `medusa/ui.py` – Status/Settings window, Save dialog, tray
- Settings: `%APPDATA%\MedusaPDF\settings.json` · Log: `%APPDATA%\MedusaPDF\medusa.log`

## Upgrading from MedussaPDF (old spelling)
- Settings are copied automatically from `%APPDATA%\MedussaPDF` the first time MedusaPDF starts.
- Click **Install printer** once: it creates the **MedusaPDF** printer and removes the old **MedussaPDF** printer and its port.
- The old Start-with-Windows entry is replaced automatically when *Start with Windows* is ticked.
- Afterwards you can delete the old project folder and `%APPDATA%\MedussaPDF` / `%LOCALAPPDATA%\MedussaPDF`.

## Sources
Visual in this file was generated with Microsoft Copilot. (App icon: `assets/medusa.ico`)

## How smart titles work
Windows passes each print job's document name to MedusaPDF. MedusaPDF cleans it up:
- removes app names (`Microsoft Word - `, ` - Notepad`, ` - Adobe Acrobat Reader (64-bit)`, ` - Excel`, …)
- removes unsaved markers (`*`), folder paths and URL parts, and file extensions
- turns `%20` into spaces

Browsers send the web page's title. Apps that send nothing, or only `Untitled`, get a timestamp name instead.
