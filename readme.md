
<img width="1151" height="659" alt="Screenshot 2026-09-13 at 16 05 34" src="https://github.com/user-attachments/assets/98a6774b-93b7-44b7-a603-8af9124fd399" />



# ECOMMANDER (MuCommander Lite)

ECOMMANDER is a lightweight, dual-panel file manager built with Python 3 and PyQt6. Inspired by classic file managers like Norton Commander and Total Commander, it provides fast keyboard navigation, tabbed browsing, and local and FTP file operations.

---

## Features

- **Dual-Panel & Tabbed Interface:** Easily manage and navigate multiple directories side by side.
- **Keyboard-Driven Navigation:** Quick selection, folder navigation, and shortcuts tailored for efficient workflows.
- **Local & FTP Support:** Seamlessly view, copy, move, and transfer files between local drives and FTP servers (Local ↔ Local, Local ↔ FTP, FTP ↔ FTP).
- **macOS Integration:** Quick Look preview (`qlmanage`), open files directly in Finder or BatchMod, native `.app` launching, and auto-mounting `.dmg` disk images.
- **Archive Management:** ZIP and UNZIP support directly between active and target panels.
- **Custom UI Highlighting:** Distinct visual styling for cursor focus, selection, folders, and files.

---

## Prerequisites

- **Python 3.8+**
- **PyQt6**

Install the necessary dependencies using `pip`:

```bash
pip install PyQt6
```

---

## Running the Application

To run the application directly from the source code:

```bash
python ecommander.py
```

---

## Keyboard Shortcuts

| Shortcut | Description |
| :--- | :--- |
| `Tab` | Switch active panel |
| `Up / Down` | Navigate file list |
| `⌘ + Up / Down` | Jump page up / down |
| `Right Arrow` | Enter folder / Open `.dmg` / Launch `.app` |
| `Left Arrow` | Go to parent directory |
| `Space` | Toggle item selection |
| `H` | Go to root directory (`/`) |
| `U` | Go to user home directory (`~`) |
| `C` | Quick Copy selected items to other panel |
| `M` | Quick Move selected items to other panel |
| `R` | Rename selected item |
| `D` | Delete selected items |
| `Ctrl + N` | Create a new folder |
| `Alt + T` | Open a new tab |
| `S` | Match target panel directory to active panel |
| `Z` | Compress (ZIP) or Extract (UNZIP) selected items |
| `Ctrl + S` | Search in current panel |
| `F` | Toggle / Connect to FTP server |
| `Alt + H` | Toggle hidden files |
| `I` | Quick Look preview (macOS) |
| `K` | View all key commands |

---

## Packaging / Building Executable (PyInstaller)

You can package ECOMMANDER into a standalone macOS application bundle or platform-specific executable using [PyInstaller](https://pyinstaller.org/).

### 1. Install PyInstaller

```bash
pip install pyinstaller
```

### 2. Build Command with Custom Icon

Ensure your icon file (`ecommander.icns` for macOS or `.ico` for Windows) is placed in the project root alongside `mucommander_lite.py`.

#### macOS (`.icns` icon):
```bash
pyinstaller --noconfirm --onedir --windowed \
  --name "ECOMMANDER" \
  --icon "ecommander.icns" \
  mucommander_lite.py
```

#### Windows (`.ico` icon):
```bash
pyinstaller --noconfirm --onedir --windowed ^
  --name "ECOMMANDER" ^
  --icon "ecommander.ico" ^
  mucommander_lite.py
```

### 3. Output

After the build process completes:
- The generated application will be located inside the **`dist/`** directory (e.g., `dist/ECOMMANDER.app` on macOS).

---

## License

This project is open-source and available under the [MIT License](LICENSE).
