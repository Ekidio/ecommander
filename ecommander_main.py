#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MuCommander Lite
-----------------
Egyszerű, kétpaneles fájlkezelő alkalmazás PyQt6-tal.

Alap funkciók:
  - fájl/mappa kijelölés (egyszeres és többszörös)
  - másolás / beillesztés (helyi <-> helyi, helyi <-> FTP, FTP <-> FTP)
  - átnevezés
  - FTP csatlakozás mindkét panelen külön-külön
  - navigáció (dupla kattintás, "Fel" gomb, elérési út sáv)
  - új mappa, törlés (alap kényelmi funkciók)

A projekt Apple Silicon-en PyInstaller-rel csomagolható natív .app-á,
lásd a fájl végén / a mellékelt README.md-ben található megjegyzést.
"""

import sys
import os
import shutil
import posixpath
import tempfile
import ftplib
import zipfile
import subprocess
import plistlib
from datetime import datetime

from PyQt6.QtCore import Qt, pyqtSignal, QItemSelectionModel, QUrl, QMimeData
from PyQt6.QtGui import (
    QAction, QKeySequence, QShortcut, QDesktopServices, QColor, QPainter, QPen,
    QDrag, QPalette, QPixmap, QIcon
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
    QPushButton, QLabel, QToolBar, QStatusBar, QMessageBox, QInputDialog,
    QDialog, QFormLayout, QDialogButtonBox, QAbstractItemView, QFileDialog,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem, QCheckBox, QTabWidget,
    QSizePolicy
)


# ---------------------------------------------------------------------------
# Segéd adatosztály egy könyvtár-bejegyzéshez (fájl vagy mappa)
# ---------------------------------------------------------------------------
class Entry:
    def __init__(self, name, is_dir, size=0, modified=""):
        self.name = name
        self.is_dir = is_dir
        self.size = size
        self.modified = modified


# ---------------------------------------------------------------------------
# FTP kliens wrapper - egyszerűsíti az ftplib használatát
# ---------------------------------------------------------------------------
class FTPClient:
    def __init__(self):
        self.ftp = None
        self.host = ""

    def connect(self, host, port, user, password, timeout=15):
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=timeout)
        ftp.login(user or "anonymous", password or "")
        self.ftp = ftp
        self.host = host

    def disconnect(self):
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
        self.ftp = None

    def is_connected(self):
        return self.ftp is not None

    def pwd(self):
        return self.ftp.pwd()

    def cwd(self, path):
        self.ftp.cwd(path)

    def list_dir(self, path):
        """Visszaadja a path tartalmát Entry lista formájában."""
        entries = []
        try:
            for name, facts in self.ftp.mlsd(path):
                if name in (".", ".."):
                    continue
                is_dir = facts.get("type") == "dir"
                size = int(facts.get("size", 0) or 0)
                modify = facts.get("modify", "")
                modified = self._format_mlsd_time(modify)
                entries.append(Entry(name, is_dir, size, modified))
        except Exception:
            # Régebbi szerverek fallback-je: NLST + CWD-próba a mappa
            # eldöntéséhez. Kevésbé pontos, de működik.
            old_cwd = self.pwd()
            try:
                self.cwd(path)
                names = self.ftp.nlst()
                for full in names:
                    name = posixpath.basename(full.rstrip("/"))
                    if not name or name in (".", ".."):
                        continue
                    is_dir = False
                    try:
                        self.ftp.cwd(name)
                        is_dir = True
                        self.ftp.cwd("..")
                    except Exception:
                        is_dir = False
                    size = 0
                    if not is_dir:
                        try:
                            size = self.ftp.size(name) or 0
                        except Exception:
                            size = 0
                    entries.append(Entry(name, is_dir, size, ""))
            finally:
                self.cwd(old_cwd)
        return entries

    @staticmethod
    def _format_mlsd_time(modify):
        if not modify:
            return ""
        try:
            dt = datetime.strptime(modify[:14], "%Y%m%d%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""

    def download_file(self, remote_path, local_path):
        with open(local_path, "wb") as f:
            self.ftp.retrbinary(f"RETR {remote_path}", f.write)

    def upload_file(self, local_path, remote_path):
        with open(local_path, "rb") as f:
            self.ftp.storbinary(f"STOR {remote_path}", f)

    def download_dir(self, remote_path, local_path):
        os.makedirs(local_path, exist_ok=True)
        for entry in self.list_dir(remote_path):
            r = posixpath.join(remote_path, entry.name)
            l = os.path.join(local_path, entry.name)
            if entry.is_dir:
                self.download_dir(r, l)
            else:
                self.download_file(r, l)

    def upload_dir(self, local_path, remote_path):
        try:
            self.ftp.mkd(remote_path)
        except Exception:
            pass
        for name in os.listdir(local_path):
            l = os.path.join(local_path, name)
            r = posixpath.join(remote_path, name)
            if os.path.isdir(l):
                self.upload_dir(l, r)
            else:
                self.upload_file(l, r)

    def rename(self, old_name, new_name):
        self.ftp.rename(old_name, new_name)

    def mkdir(self, name):
        self.ftp.mkd(name)

    def delete(self, name, is_dir):
        if is_dir:
            self._rmdir_recursive(name)
        else:
            self.ftp.delete(name)

    def _rmdir_recursive(self, path):
        for entry in self.list_dir(path):
            full = posixpath.join(path, entry.name)
            if entry.is_dir:
                self._rmdir_recursive(full)
            else:
                self.ftp.delete(full)
        self.ftp.rmd(path)


# ---------------------------------------------------------------------------
# FTP csatlakozási párbeszédablak
# ---------------------------------------------------------------------------
class KeyCommandsDialog(QDialog):
    """Áttekintő ablak az összes billentyűparancsról."""

    def __init__(self, shortcuts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Billentyűparancsok")
        self.resize(440, 520)

        layout = QVBoxLayout(self)

        table = QTableWidget(len(shortcuts), 2)
        table.setHorizontalHeaderLabels(["Parancs", "Billentyű"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        for i, (name, key) in enumerate(shortcuts):
            table.setItem(i, 0, QTableWidgetItem(name))
            key_item = QTableWidgetItem(key)
            key_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(i, 1, key_item)
        layout.addWidget(table)

        close_btn = QPushButton("Bezárás")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)


class ConflictDialog(QDialog):
    """Felugró ablak, ha a másolás célhelyén már létezik azonos nevű elem."""

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fájlütközés")
        self.choice = "skip"

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"Már létezik elem ezen a néven a célhelyen:\n\"{name}\"\n\nMit tegyünk?"
        ))

        self.remember_cb = QCheckBox("Alkalmazás minden további ütközésre")
        layout.addWidget(self.remember_cb)

        btn_row = QHBoxLayout()
        overwrite_btn = QPushButton("Felülírás")
        rename_btn = QPushButton("Másolás sorszámmal")
        skip_btn = QPushButton("Kihagyás")
        btn_row.addWidget(overwrite_btn)
        btn_row.addWidget(rename_btn)
        btn_row.addWidget(skip_btn)
        layout.addLayout(btn_row)

        overwrite_btn.clicked.connect(lambda: self._choose("overwrite"))
        rename_btn.clicked.connect(lambda: self._choose("rename"))
        skip_btn.clicked.connect(lambda: self._choose("skip"))

    def _choose(self, choice):
        self.choice = choice
        self.accept()

    def result_tuple(self):
        return self.choice, self.remember_cb.isChecked()


class ConnectFTPDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Csatlakozás FTP szerverhez")
        layout = QFormLayout(self)

        self.host_edit = QLineEdit()
        self.port_edit = QLineEdit("21")
        self.user_edit = QLineEdit("anonymous")
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.path_edit = QLineEdit("/")

        layout.addRow("Host:", self.host_edit)
        layout.addRow("Port:", self.port_edit)
        layout.addRow("Felhasználónév:", self.user_edit)
        layout.addRow("Jelszó:", self.pass_edit)
        layout.addRow("Kezdő mappa:", self.path_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self):
        try:
            port = int(self.port_edit.text().strip() or "21")
        except ValueError:
            port = 21
        return {
            "host": self.host_edit.text().strip(),
            "port": port,
            "user": self.user_edit.text().strip(),
            "password": self.pass_edit.text(),
            "path": self.path_edit.text().strip() or "/",
        }


# ---------------------------------------------------------------------------
# Egy panel (bal vagy jobb oldal) - lehet helyi vagy FTP nézet
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Saját színezés: felülírja Qt beépített (mindig kék) kijelölés-színezését,
# hogy a "kurzor" (mozgás) és a "kijelölés" (SPACE) egyértelműen, különböző
# árnyalatokkal legyen megkülönböztethető, típus szerint is (mappa/fájl).
# ---------------------------------------------------------------------------
class RowColorDelegate(QStyledItemDelegate):
    FOLDER_COLOR = QColor("#dbe9fb")           # alap mappa szín - halvány kék
    FILE_COLOR = QColor("#e3f6e8")             # alap fájl szín - halvány zöld
    CURRENT_COLOR = QColor("#ffe38a")          # kurzor (mozgás), nem kijelölt sor - sárga
    SELECTED_TEXT_COLOR = QColor("#d32f2f")    # kijelölt (SPACE) sor - csak a SZÖVEG piros

    def __init__(self, panel):
        super().__init__(panel.table)
        self.panel = panel

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        row = index.row()
        is_dir = (row < len(self.panel.row_is_dir) and self.panel.row_is_dir[row])
        is_selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        is_current = (row == self.panel.table.currentRow())

        # a háttérszín csak a kurzor-pozíciótól és a típustól függ -
        # a kijelölés (SPACE) itt NEM módosítja a hátteret, csak a szöveget
        bg_color = self.CURRENT_COLOR if is_current else (
            self.FOLDER_COLOR if is_dir else self.FILE_COLOR
        )

        # a beépített kijelölés-kitöltést kikapcsoljuk, mert azt saját
        # színünkkel helyettesítjük
        opt.state &= ~QStyle.StateFlag.State_Selected
        if is_selected:
            opt.palette.setColor(QPalette.ColorRole.Text, self.SELECTED_TEXT_COLOR)
            opt.palette.setColor(QPalette.ColorRole.WindowText, self.SELECTED_TEXT_COLOR)

        painter.save()
        painter.fillRect(opt.rect, bg_color)
        painter.restore()
        super().paint(painter, opt, index)


class FocusTable(QTableWidget):
    """QTableWidget, ami jelzi, ha fókuszt kap - így tudjuk, melyik
    panel az aktív. Emellett Norton/Total Commander-stílusú
    billentyű-navigációt valósít meg: a nyilak csak a kurzort mozgatják
    (nem jelölnek ki semmit), a kijelölést a SPACE váltja."""
    focused = pyqtSignal()
    tab_pressed = pyqtSignal()
    DRAG_MIME_TYPE = "application/x-mucommander-lite-names"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.panel = None  # a szülő FilePanel, kívülről beállítva
        self.setTabKeyNavigation(False)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focused.emit()

    # -- Drag & Drop (a két panel között, mindig MÁSOLÁS) ------------
    def startDrag(self, supported_actions):
        if self.panel is None:
            return
        names = self.panel.selected_names()
        if not names:
            name = self.panel.current_name()
            if name and name != "..":
                names = [name]
        if not names:
            return

        mime = QMimeData()
        mime.setData(self.DRAG_MIME_TYPE, "\n".join(names).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(self.DRAG_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(self.DRAG_MIME_TYPE):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(self.DRAG_MIME_TYPE):
            data = bytes(mime.data(self.DRAG_MIME_TYPE)).decode("utf-8")
            names = [n for n in data.split("\n") if n]
            source_table = event.source()
            source_panel = getattr(source_table, "panel", None)
            if source_panel is not None and names and self.panel is not None:
                self.panel.drop_copy_requested.emit(source_panel, names)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        panel = self.panel

        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            # ne engedjük, hogy a táblázat saját cellák-közti Tab-navigációja
            # elnyelje a billentyűt - ehelyett panelváltást kérünk
            event.accept()
            self.tab_pressed.emit()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                # CMD (Ctrl) + fel/le: oldalankénti ugrás, nem soronkénti lépés
                row_h = self.rowHeight(0) if self.rowCount() > 0 else 20
                page = max(1, self.viewport().height() // max(1, row_h))
            else:
                page = 1
            self._move_current(page if key == Qt.Key.Key_Down else -page)
            return
        if key == Qt.Key.Key_Right:
            if panel:
                panel.enter_current()
            return
        if key == Qt.Key.Key_Left:
            if panel:
                panel.go_up()
            return
        if key == Qt.Key.Key_H:
            if panel:
                panel.go_root()
            return
        if key == Qt.Key.Key_U:
            if panel:
                panel.go_home()
            return
        if key == Qt.Key.Key_Space:
            # SPACE: kijelölés ki/be kapcsolása (toggle) a kurzoron álló soron
            self._toggle_current_selection()
            return
        super().keyPressEvent(event)

    def _move_current(self, delta):
        row = self.currentRow()
        if row < 0:
            row = 0
        new_row = row + delta
        if self.rowCount() == 0:
            return
        new_row = max(0, min(new_row, self.rowCount() - 1))
        if new_row == row:
            return
        col = self.currentColumn() if self.currentColumn() >= 0 else 0
        index = self.model().index(new_row, col)
        # NoUpdate: csak a kurzort mozgatja, a kijelölést nem érinti
        self.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.NoUpdate
        )
        self.scrollTo(index)

    def _toggle_current_selection(self):
        row = self.currentRow()
        if row < 0:
            return
        index = self.model().index(row, 0)
        self.selectionModel().select(
            index,
            QItemSelectionModel.SelectionFlag.Toggle
            | QItemSelectionModel.SelectionFlag.Rows
        )


class FilePanel(QWidget):
    status_message = pyqtSignal(str)
    became_active = pyqtSignal(object)  # self
    tab_pressed = pyqtSignal()
    dmg_opened = pyqtSignal()          # DMG megnyitva - a másik panelen mutassuk az Applications-t
    drop_copy_requested = pyqtSignal(object, list)  # (forrás panel, [nevek]) - erre a panelre húzták
    view_requested = pyqtSignal()      # SPACE - Quick Look előnézet kérése
    path_changed = pyqtSignal()        # navigáció/FTP-váltás után - a fül címkéjének frissítéséhez

    COL_NAME, COL_SIZE, COL_MOD = range(3)

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.panel_name = name
        self.mode = "local"           # "local" vagy "ftp"
        self.local_path = os.path.expanduser("~")
        self.ftp_path = "/"
        self.ftp_client = None        # FTPClient, ha mode == "ftp"
        self.entries = []              # az aktuálisan listázott Entry-k
        self.row_is_dir = []            # soronkénti mappa/fájl jelző (a delegate-hez)
        self.show_hidden = False       # rejtett (ponttal kezdődő) elemek
        self._is_active_panel = False  # ez a panel az aktív? (piros keret)
        self.side = None               # 'left' vagy 'right' - a factory állítja be
        self._icon_cache = {}          # app_path -> QIcon (vagy None), .app ikonokhoz

        self._build_ui()
        self.refresh()

    def set_active_visual(self, active):
        """Piros kerettel jelzi, hogy ez a panel az aktív (TAB-bal váltható)."""
        if self._is_active_panel != active:
            self._is_active_panel = active
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._is_active_panel:
            painter = QPainter(self)
            pen = QPen(QColor("#e74c3c"))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
            painter.end()

    # -- UI felépítés ------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.path_label = QLabel("")
        self.path_label.setObjectName("pathLabel")
        self.path_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self.path_label)

        self.table = FocusTable(0, 3)
        self.table.setHorizontalHeaderLabels(["Név", "Méret", "Módosítva"])
        self.table.horizontalHeader().setSectionResizeMode(
            self.COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            self.COL_SIZE, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            self.COL_MOD, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.focused.connect(lambda: self.became_active.emit(self))
        self.table.tab_pressed.connect(self.tab_pressed.emit)
        self.table.itemSelectionChanged.connect(self.table.viewport().update)
        self.table.currentCellChanged.connect(
            lambda *_: self.table.viewport().update()
        )
        self.table.panel = self
        self.row_delegate = RowColorDelegate(self)
        self.table.setItemDelegate(self.row_delegate)
        layout.addWidget(self.table, 1)

        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #888;")
        layout.addWidget(self.info_label)

    # -- Állapot lekérdezés -------------------------------------------
    def current_path_display(self):
        if self.mode == "ftp":
            host = self.ftp_client.host if self.ftp_client else "?"
            return f"ftp://{host}{self.ftp_path}"
        return self.local_path

    # -- Navigáció ------------------------------------------------------
    def go_up(self):
        if self.mode == "local":
            parent = os.path.dirname(self.local_path.rstrip(os.sep))
            if parent and parent != self.local_path:
                self.navigate(parent)
        else:
            if self.ftp_path != "/":
                parent = posixpath.dirname(self.ftp_path.rstrip("/")) or "/"
                self.navigate(parent)

    def go_root(self):
        """'H' billentyű: a lemez/kapcsolat gyökere."""
        if self.mode == "local":
            self.navigate(os.path.abspath(os.sep))
        else:
            self.navigate("/")

    def go_home(self):
        """'U' billentyű: a felhasználó HOME mappája (csak helyi módban
        értelmezett)."""
        if self.mode == "local":
            self.navigate(os.path.expanduser("~"))

    def focus_and_reset_cursor(self):
        """Panelváltáskor (TAB) a fókusz mellett a kurzort mindig a
        legfelső elemre állítja, hogy azonnal nyilakkal navigálhass -
        nem kell előtte kattintani."""
        self.table.setFocus()
        if self.table.rowCount() > 0:
            index = self.table.model().index(0, 0)
            self.table.selectionModel().setCurrentIndex(
                index, QItemSelectionModel.SelectionFlag.NoUpdate
            )
            self.table.viewport().update()

    def current_name(self):
        """A táblázatban jelenleg a kurzoron álló elem neve (lehet ".."
        is), vagy None, ha nincs érvényes sor."""
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, self.COL_NAME)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def enter_current(self):
        """Jobbra nyíl: belépés a kurzoron álló mappába, .app esetén annak
        elindítása, .dmg esetén pedig megnyitása (csatolása)."""
        name = self.current_name()
        if name is None:
            return
        if name == "..":
            self.go_up()
            return
        entry = self._find_entry(name)
        if entry is None:
            return
        if not entry.is_dir and self.mode == "local" and name.lower().endswith(".dmg"):
            self.open_dmg(name)
            return
        if self._is_app_bundle(name, entry.is_dir):
            self.launch_app(name)
            return
        if entry.is_dir:
            if self.mode == "local":
                self.navigate(os.path.join(self.local_path, name))
            else:
                self.navigate(posixpath.join(self.ftp_path, name))

    @staticmethod
    def _is_app_bundle(name, is_dir):
        """Igaz, ha az elem egy macOS .app csomag (könyvtár, .app végződéssel)."""
        return is_dir and name.lower().endswith(".app")

    def launch_app(self, name):
        """Egy .app csomag elindítása (mint a Finderben dupla kattintásra)."""
        if sys.platform != "darwin":
            QMessageBox.information(
                self, "Info", "Alkalmazás indítása csak macOS-en támogatott."
            )
            return
        path = os.path.join(self.local_path, name)
        try:
            subprocess.run(["open", path], check=True)
        except Exception as e:
            QMessageBox.critical(self, "Hiba", f"Nem sikerült elindítani:\n{e}")

    def _get_app_icon(self, app_path):
        """Egy .app csomag saját ikonja (QIcon), gyorsítótárazva. macOS-en
        kívül, vagy sikertelen kinyerés esetén None."""
        if sys.platform != "darwin":
            return None
        if app_path in self._icon_cache:
            return self._icon_cache[app_path]
        icon = self._extract_app_icon(app_path)
        self._icon_cache[app_path] = icon
        return icon

    @staticmethod
    def _extract_app_icon(app_path):
        try:
            plist_path = os.path.join(app_path, "Contents", "Info.plist")
            if not os.path.isfile(plist_path):
                return None
            with open(plist_path, "rb") as f:
                info = plistlib.load(f)
            icon_name = info.get("CFBundleIconFile", "")
            if not icon_name:
                return None
            if not icon_name.lower().endswith(".icns"):
                icon_name += ".icns"
            icns_path = os.path.join(app_path, "Contents", "Resources", icon_name)
            if not os.path.isfile(icns_path):
                return None

            tmp_png = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp_png.close()
            try:
                result = subprocess.run(
                    ["sips", "-s", "format", "png", icns_path, "--out", tmp_png.name],
                    capture_output=True, timeout=15
                )
                if result.returncode != 0:
                    return None
                pixmap = QPixmap(tmp_png.name)
            finally:
                try:
                    os.unlink(tmp_png.name)
                except OSError:
                    pass
            if pixmap.isNull():
                return None
            return QIcon(pixmap)
        except Exception:
            return None

    def open_dmg(self, name):
        """Egy .dmg image csatolása (hdiutil), majd belépés a csatolt
        kötetbe - a másik panelen ekkor az Applications mappa jelenik meg
        a gyors alkalmazás-áthúzáshoz."""
        if sys.platform != "darwin":
            QMessageBox.information(
                self, "Info", "A DMG megnyitása csak macOS-en támogatott."
            )
            return

        dmg_path = os.path.join(self.local_path, name)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = subprocess.run(
                ["hdiutil", "attach", dmg_path, "-nobrowse", "-plist"],
                capture_output=True, timeout=120
            )
            if result.returncode != 0:
                msg = result.stderr.decode("utf-8", "ignore").strip() or "hdiutil hiba"
                raise RuntimeError(msg)
            plist = plistlib.loads(result.stdout)
            mount_point = None
            for entity in plist.get("system-entities", []):
                mp = entity.get("mount-point")
                if mp:
                    mount_point = mp
                    break
            if not mount_point:
                raise RuntimeError("Nem található csatolási pont a DMG-ben.")
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Hiba", f"Nem sikerült megnyitni a DMG fájlt:\n{e}"
            )
            return
        QApplication.restoreOverrideCursor()

        self.navigate(mount_point)
        self.dmg_opened.emit()

    def navigate(self, path):
        if self.mode == "local":
            path = os.path.abspath(path)
            if not os.path.isdir(path):
                QMessageBox.warning(self, "Hiba", "Nem található a mappa.")
                return
            self.local_path = path
        else:
            try:
                self.ftp_client.cwd(path)
                self.ftp_path = self.ftp_client.pwd()
            except Exception as e:
                QMessageBox.warning(self, "FTP hiba", str(e))
                return
        self.refresh()

    def _on_double_click(self, index):
        row = index.row()
        name_item = self.table.item(row, self.COL_NAME)
        if name_item is None:
            return
        name = name_item.data(Qt.ItemDataRole.UserRole)
        if name == "..":
            self.go_up()
            return
        entry = self._find_entry(name)
        if entry is None:
            return
        if not entry.is_dir and self.mode == "local" and name.lower().endswith(".dmg"):
            self.open_dmg(name)
            return
        if self._is_app_bundle(name, entry.is_dir):
            self.launch_app(name)
            return
        if entry.is_dir:
            if self.mode == "local":
                self.navigate(os.path.join(self.local_path, name))
            else:
                self.navigate(posixpath.join(self.ftp_path, name))
        else:
            if self.mode == "local":
                # helyi fájl megnyitása az alapértelmezett alkalmazással
                QDesktopServices.openUrl(QUrl.fromLocalFile(
                    os.path.join(self.local_path, name)))

    def _find_entry(self, name):
        for e in self.entries:
            if e.name == name:
                return e
        return None

    # -- Helyi listázás -------------------------------------------------
    @staticmethod
    def _list_local(path):
        entries = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                is_dir = os.path.isdir(full)
                stat_res = os.stat(full)
                size = 0 if is_dir else stat_res.st_size
                modified = datetime.fromtimestamp(
                    stat_res.st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                # pl. jogosultság hiánya vagy törött szimbolikus link
                is_dir = False
                size = 0
                modified = ""
            entries.append(Entry(name, is_dir, size, modified))
        return entries

    # -- Listázás / frissítés -------------------------------------------
    def refresh(self):
        self.path_label.setText(self.current_path_display())
        self.table.setRowCount(0)
        self.entries = []

        try:
            if self.mode == "local":
                self.entries = self._list_local(self.local_path)
            else:
                self.entries = self.ftp_client.list_dir(self.ftp_path)
        except Exception as e:
            QMessageBox.warning(self, "Hiba", f"Nem sikerült listázni:\n{e}")
            return

        if not self.show_hidden:
            self.entries = [e for e in self.entries if not e.name.startswith(".")]

        # rendezés: mappák előre, majd ábécé sorrend
        self.entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))

        show_up = (self.mode == "local" and os.path.dirname(
            self.local_path.rstrip(os.sep)) != self.local_path) or \
            (self.mode == "ftp" and self.ftp_path != "/")

        row = 0
        self.table.setRowCount(len(self.entries) + (1 if show_up else 0))
        self.row_is_dir = []
        if show_up:
            self._set_row(row, "..", True, 0, "")
            self.row_is_dir.append(True)
            row += 1
        for entry in self.entries:
            self._set_row(row, entry.name, entry.is_dir, entry.size, entry.modified)
            self.row_is_dir.append(entry.is_dir)
            row += 1

        self.info_label.setText(f"{len(self.entries)} elem")

        # mappaváltáskor törli a jelöléseket, kurzort az első sorra állítja
        self.table.clearSelection()
        if self.table.rowCount() > 0:
            index = self.table.model().index(0, 0)
            self.table.selectionModel().setCurrentIndex(
                index, QItemSelectionModel.SelectionFlag.NoUpdate
            )
        self.table.viewport().update()
        self.path_changed.emit()

    def _set_row(self, row, name, is_dir, size, modified):
        icon = None
        if self.mode == "local" and self._is_app_bundle(name, is_dir):
            # macOS alkalmazáscsomag: ".app" levágása, saját ikon mutatása
            display_name = name[:-4] if name.lower().endswith(".app") else name
            icon = self._get_app_icon(os.path.join(self.local_path, name))
        else:
            display_name = f"📁 {name}" if is_dir else f"📄 {name}"

        name_item = QTableWidgetItem(display_name)
        if icon is not None:
            name_item.setIcon(icon)
        # a UserRole mindig a VALÓDI (teljes, ".app"-pal együtt) nevet tárolja
        name_item.setData(Qt.ItemDataRole.UserRole, name)
        size_item = QTableWidgetItem("" if is_dir else self._human_size(size))
        mod_item = QTableWidgetItem(modified)
        self.table.setItem(row, self.COL_NAME, name_item)
        self.table.setItem(row, self.COL_SIZE, size_item)
        self.table.setItem(row, self.COL_MOD, mod_item)

    @staticmethod
    def _human_size(size):
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} PB"

    # -- Kijelölés ----------------------------------------------------
    def selected_names(self):
        names = []
        for item in self.table.selectedItems():
            if item.column() != self.COL_NAME:
                continue
            name = item.data(Qt.ItemDataRole.UserRole)
            if name and name != "..":
                names.append(name)
        return names

    # -- FTP csatlakozás / bontás -----------------------------------
    def connect_ftp(self, params):
        client = FTPClient()
        try:
            client.connect(params["host"], params["port"],
                            params["user"], params["password"])
            client.cwd(params["path"])
        except Exception as e:
            QMessageBox.critical(self, "FTP hiba", f"Sikertelen csatlakozás:\n{e}")
            return
        self.ftp_client = client
        self.mode = "ftp"
        self.ftp_path = client.pwd()
        self.refresh()
        self.status_message.emit(f"Csatlakozva: {params['host']}")

    def disconnect_ftp(self):
        if self.ftp_client:
            self.ftp_client.disconnect()
        self.ftp_client = None
        self.mode = "local"
        self.refresh()
        self.status_message.emit("FTP kapcsolat bontva")

    def match_to(self, other_panel):
        """'Same Folders': ezt a panelt a másik panel helyi mappájára
        állítja. Csak akkor működik, ha a másik panel helyi módban van."""
        if other_panel.mode != "local":
            return False
        if self.mode == "ftp" and self.ftp_client:
            self.ftp_client.disconnect()
            self.ftp_client = None
        self.mode = "local"
        self.navigate(other_panel.local_path)
        return True


# ---------------------------------------------------------------------------
# Fő ablak
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ECommander")
        self.resize(1150, 680)

        self.active_side = "left"      # 'left' vagy 'right' - melyik OLDAL aktív
        self.clipboard = None          # {"panel": FilePanel, "names": [...]}

        self.left_tabs = QTabWidget()
        self.right_tabs = QTabWidget()
        for tabs in (self.left_tabs, self.right_tabs):
            tabs.setTabsClosable(True)
            tabs.setMovable(True)
            tabs.setDocumentMode(True)

        self.left_tabs.tabCloseRequested.connect(lambda i: self._close_tab("left", i))
        self.right_tabs.tabCloseRequested.connect(lambda i: self._close_tab("right", i))
        self.left_tabs.currentChanged.connect(lambda i: self._on_tab_changed("left", i))
        self.right_tabs.currentChanged.connect(lambda i: self._on_tab_changed("right", i))
        self.left_tabs.tabBarClicked.connect(lambda i: self._set_active_side("left"))
        self.right_tabs.tabBarClicked.connect(lambda i: self._set_active_side("right"))

        self._create_panel("left")
        self._create_panel("right")
        self._set_active_side("left")

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.left_tabs)
        splitter.addWidget(self.right_tabs)
        splitter.setSizes([575, 575])
        self.setCentralWidget(splitter)

        self._build_top_toolbar()
        self._build_bottom_toolbar()
        self.setStatusBar(QStatusBar())
        self._show_status("Kész.")

        self.setStyleSheet(self._stylesheet())

    # -- panel-fülek (tabs) kezelése -------------------------------------
    @property
    def left(self):
        return self.left_tabs.currentWidget()

    @property
    def right(self):
        return self.right_tabs.currentWidget()

    @property
    def active_panel(self):
        return self.left if self.active_side == "left" else self.right

    @active_panel.setter
    def active_panel(self, panel):
        if panel is not None and panel.side:
            self.active_side = panel.side

    def _create_panel(self, side, path=None):
        """Új FilePanel példány létrehozása és bekötése egy adott oldal
        (side) egy új fülébe. path esetén oda navigál, egyébként HOME-ra."""
        tabs = self.left_tabs if side == "left" else self.right_tabs
        panel = FilePanel("Bal" if side == "left" else "Jobb")
        panel.side = side

        panel.became_active.connect(lambda p: self._set_active_side(p.side))
        panel.status_message.connect(self._show_status)
        panel.tab_pressed.connect(self.switch_active_panel)
        panel.dmg_opened.connect(lambda p=panel: self.show_applications_on_other(p))
        panel.drop_copy_requested.connect(
            lambda src, names, p=panel: self.handle_drop_copy(p, src, names)
        )
        panel.view_requested.connect(lambda p=panel: self.view_current(p))
        panel.path_changed.connect(lambda p=panel: self._refresh_tab_label(p))

        if path:
            panel.navigate(path)

        idx = tabs.addTab(panel, self._tab_label(panel))
        tabs.setCurrentIndex(idx)
        return panel

    @staticmethod
    def _tab_label(panel):
        if panel.mode == "ftp":
            return panel.ftp_client.host if panel.ftp_client else "FTP"
        name = os.path.basename(panel.local_path.rstrip(os.sep))
        return name or panel.local_path

    def _refresh_tab_label(self, panel):
        tabs = self.left_tabs if panel.side == "left" else self.right_tabs
        idx = tabs.indexOf(panel)
        if idx >= 0:
            tabs.setTabText(idx, self._tab_label(panel))

    def new_tab(self):
        """'New Tab': új fül nyitása az AKTÍV oldalon, az ott jelenleg
        látott mappával kezdve (helyi mód esetén)."""
        side = self.active_side
        current = self.active_panel
        path = current.local_path if current and current.mode == "local" else None
        panel = self._create_panel(side, path=path)
        self._set_active_side(side)
        self._show_status("Új fül megnyitva.")
        return panel

    def _close_tab(self, side, index):
        tabs = self.left_tabs if side == "left" else self.right_tabs
        if tabs.count() <= 1:
            self._show_status("Az utolsó fül nem zárható be.")
            return
        panel = tabs.widget(index)
        if panel.mode == "ftp" and panel.ftp_client:
            panel.ftp_client.disconnect()
        tabs.removeTab(index)
        panel.deleteLater()

    def _on_tab_changed(self, side, index):
        tabs = self.left_tabs if side == "left" else self.right_tabs
        panel = tabs.widget(index)
        if panel is None:
            return
        panel.refresh()
        if self.active_side == side:
            self._set_active_side(side)

    # -- aktív panel / oldal kezelése -------------------------------------
    def _set_active_side(self, side):
        self.active_side = side
        if self.left:
            self.left.set_active_visual(side == "left")
        if self.right:
            self.right.set_active_visual(side == "right")

    # -- kinézet ---------------------------------------------------------
    @staticmethod
    def _stylesheet():
        return """
        QMainWindow { background: #f4f4f6; }
        QToolBar { background: #ffffff; border-bottom: 1px solid #dcdcdc; spacing: 6px; padding: 4px; }
        QPushButton {
            background: #ffffff; border: 1px solid #c9c9cf; border-radius: 6px;
            padding: 5px 12px;
        }
        QPushButton:hover { background: #eef1ff; border-color: #8ea2ff; }
        QLineEdit {
            border: 1px solid #c9c9cf; border-radius: 6px; padding: 4px 8px;
            background: white;
        }
        QLabel#pathLabel {
            border: 1px solid #dcdcdc; border-radius: 6px; padding: 5px 9px;
            background: #eef0f4; color: #444; font-weight: 500;
        }
        QTableWidget {
            background: white; border: 1px solid #dcdcdc; border-radius: 6px;
            gridline-color: #eaeaea;
        }
        QHeaderView::section {
            background: #f0f0f4; border: none; padding: 6px; font-weight: 600;
        }
        QTableWidget::item:selected { background: transparent; }
        QStatusBar { background: #ffffff; border-top: 1px solid #dcdcdc; }
        QToolButton#hiddenToggleBtn:checked {
            background: #e74c3c; color: white; border-color: #c0392b;
            font-weight: 600;
        }
        """

    def _build_top_toolbar(self):
        tb = QToolBar("Felső menü")
        tb.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

        def add(text, slot, shortcut=None, checkable=False, object_name=None):
            act = QAction(text, self)
            if checkable:
                act.setCheckable(True)
                act.toggled.connect(slot)
            else:
                act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            tb.addAction(act)
            if object_name:
                # a ténylegesen renderelt QToolButton-ra tesszük az objectName-et,
                # hogy a QSS-ben (pl. bekapcsolt állapot pirosítása) elérhető legyen
                btn = tb.widgetForAction(act)
                if btn is not None:
                    btn.setObjectName(object_name)
            return act

        add("➕ New Tab (⌥T)", self.new_tab, "Alt+T")
        add("🔗 Same Folder (S)", self.same_folders, "S")
        tb.addSeparator()

        add("🗜 ToggleZIP (Z)", self.zip_or_unzip, "Z")
        add("🔍 Search (⌘S)", self.search_in_panel, "Ctrl+S")
        add("🌐 FTP (F)", self.toggle_ftp, "F")
        add("🔎 Finder (⌘F)", self.reveal_in_finder, "Ctrl+F")
        add("🛠 Batchmod (B)", self.open_in_batchmod, "B")
        tb.addSeparator()

        # 'H' már foglalt a fájllistán (gyökérmappa-ugrás), ezért a Hidden
        # itt OPT (Alt)+H-t kap, hogy ne ütközzön
        self.hidden_action = add(
            "🙈 Hidden (⌥H)", self.toggle_hidden_files, "Alt+H",
            checkable=True, object_name="hiddenToggleBtn"
        )

        add("🔄 Reload (⌘R)", self.refresh_both, "Ctrl+R")
        add("⌨️ KeyCommands (K)", self.show_key_commands, "K")

    def _build_bottom_toolbar(self):
        tb = QToolBar("Alsó menü")
        tb.setMovable(False)
        tb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, tb)

        def add(text, slot, shortcut=None):
            act = QAction(text, self)
            act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            tb.addAction(act)
            return act

        add("👁 View (I)", self.view_current, "I")
        add("✏️ Edit (E)", self.edit_current, "E")
        add("📋 Copy (C)", self.quick_copy, "C")
        add("🚚 Move (M)", self.quick_move, "M")
        add("✏️ Rename (R)", self.rename_selection, "R")
        add("🗑 Delete (D)", self.delete_selection, "D")
        add("🆕 New Folder (⌘N)", self.new_folder, "Ctrl+N")

        # nyújtható elválasztó, hogy a sáv a teljes ablakszélességet kitöltse
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        add("🚪 Exit (⌘X)", self.close, "Ctrl+X")

    # -- aktív panel kezelés -------------------------------------------
    def other_panel(self):
        return self.right if self.active_side == "left" else self.left

    def switch_active_panel(self):
        target = self.other_panel()
        target.focus_and_reset_cursor()
        self._set_active_side(target.side)

    def _show_status(self, message):
        self.statusBar().showMessage(message, 5000)

    def refresh_both(self):
        self.left.refresh()
        self.right.refresh()

    # -- Billentyűparancsok áttekintése -----------------------------------
    def show_key_commands(self):
        shortcuts = []
        for act in self.findChildren(QAction):
            seq = act.shortcut().toString()
            if seq:
                shortcuts.append((act.text(), seq))

        # a táblázatban nem szereplő, fájllistán belüli billentyűk
        shortcuts.extend([
            ("⬆️⬇️ Mozgás a listában", "Fel / Le nyíl"),
            ("Oldalankénti ugrás", "⌘ + Fel / Le nyíl"),
            ("➡️ Belépés mappába / DMG megnyitás", "Jobbra nyíl"),
            ("⬅️ Egy szinttel feljebb", "Balra nyíl"),
            ("🏠 Ugrás a gyökérmappába", "H"),
            ("🧑 Ugrás a felhasználói (HOME) mappába", "U"),
            ("↔️ Panelváltás", "Tab"),
        ])
        shortcuts.sort(key=lambda t: t[0].lower())

        dlg = KeyCommandsDialog(shortcuts, self)
        dlg.exec()

    # -- FTP ---------------------------------------------------------
    def toggle_ftp(self):
        panel = self.active_panel
        if panel.mode == "ftp":
            panel.disconnect_ftp()
            self._refresh_tab_label(panel)
        else:
            self.connect_ftp_active()

    def connect_ftp_active(self):
        dlg = ConnectFTPDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            params = dlg.values()
            if not params["host"]:
                QMessageBox.warning(self, "Hiba", "Add meg a host címét.")
                return
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.active_panel.connect_ftp(params)
                self._refresh_tab_label(self.active_panel)
            finally:
                QApplication.restoreOverrideCursor()

    def browse_local_active(self):
        if self.active_panel.mode != "local":
            QMessageBox.information(self, "Info", "Ez csak helyi panelen elérhető.")
            return
        path = QFileDialog.getExistingDirectory(self, "Mappa kiválasztása",
                                                 self.active_panel.local_path)
        if path:
            self.active_panel.navigate(path)
            self._refresh_tab_label(self.active_panel)

    # -- Same Folders ------------------------------------------------
    def same_folders(self):
        """A MÁSIK panelt igazítja az AKTÍV (amelyiken éppen állunk)
        panel mappájához."""
        source = self.active_panel
        target = self.other_panel()
        if source.mode != "local":
            QMessageBox.information(
                self, "Info", "Az aktív panel jelenleg nem helyi mappát mutat."
            )
            return
        target.match_to(source)
        self._refresh_tab_label(target)
        self._show_status("A másik panel az aktív panelhez igazodott.")

    # -- Search --------------------------------------------------------
    def search_in_panel(self):
        panel = self.active_panel
        term, ok = QInputDialog.getText(
            self, "Keresés", "Keresendő szöveg (fájl/mappa névben):"
        )
        if not ok or not term:
            return
        term_lower = term.lower()
        matches = [i for i, e in enumerate(panel.entries) if term_lower in e.name.lower()]
        if not matches:
            QMessageBox.information(self, "Keresés", f"Nincs találat erre: \"{term}\"")
            return

        offset = panel.table.rowCount() - len(panel.entries)  # 0 vagy 1 (".." sor miatt)
        panel.table.clearSelection()
        sel_model = panel.table.selectionModel()
        for i in matches:
            idx = panel.table.model().index(i + offset, 0)
            sel_model.select(
                idx, QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows
            )
        first_index = panel.table.model().index(matches[0] + offset, 0)
        sel_model.setCurrentIndex(first_index, QItemSelectionModel.SelectionFlag.NoUpdate)
        panel.table.scrollTo(first_index)
        panel.table.viewport().update()
        self._show_status(f"{len(matches)} találat erre: \"{term}\" (kijelölve)")

    # -- Finder / Batchmod (macOS) ---------------------------------------
    def _selected_or_current_paths(self, panel):
        names = panel.selected_names()
        if not names:
            name = panel.current_name()
            if name and name != "..":
                names = [name]
        if not names or panel.mode != "local":
            return []
        return [os.path.join(panel.local_path, n) for n in names]

    def reveal_in_finder(self):
        panel = self.active_panel
        paths = self._selected_or_current_paths(panel)
        if not paths:
            QMessageBox.information(
                self, "Info",
                "Nincs kijelölt vagy kurzoron álló helyi elem."
            )
            return
        if sys.platform != "darwin":
            QMessageBox.information(self, "Info", "Ez csak macOS-en támogatott.")
            return
        try:
            subprocess.run(["open", "-R", paths[0]], check=True)
        except Exception as e:
            QMessageBox.critical(self, "Hiba", f"Nem sikerült megnyitni a Findert:\n{e}")

    def open_in_batchmod(self):
        panel = self.active_panel
        paths = self._selected_or_current_paths(panel)
        if not paths:
            QMessageBox.information(
                self, "Info",
                "Nincs kijelölt vagy kurzoron álló helyi elem."
            )
            return
        if sys.platform != "darwin":
            QMessageBox.information(self, "Info", "A BatchMod csak macOS-en támogatott.")
            return
        try:
            subprocess.run(["open", "-a", "BatchMod"] + paths, check=True)
        except Exception as e:
            QMessageBox.critical(self, "Hiba", f"Nem sikerült megnyitni a BatchModot:\n{e}")

    # -- View (Quick Look) / Edit -----------------------------------------
    def view_current(self, panel=None):
        panel = panel or self.active_panel
        paths = self._selected_or_current_paths(panel)
        if not paths:
            return
        if sys.platform != "darwin":
            QMessageBox.information(
                self, "Info", "A Quick Look előnézet csak macOS-en támogatott."
            )
            return
        try:
            subprocess.Popen(["qlmanage", "-p", paths[0]],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            QMessageBox.critical(self, "Hiba", f"Nem sikerült megnyitni az előnézetet:\n{e}")

    def edit_current(self):
        panel = self.active_panel
        paths = self._selected_or_current_paths(panel)
        if not paths:
            QMessageBox.information(
                self, "Info", "Nincs kijelölt vagy kurzoron álló helyi elem."
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(paths[0]))

    # -- Gyors Copy / Move (a másik panelra, azonnal) --------------------
    def quick_copy(self):
        src = self.active_panel
        dst = self.other_panel()
        names = src.selected_names()
        if not names:
            name = src.current_name()
            if name and name != "..":
                names = [name]
        if not names:
            self._show_status("Nincs kijelölt vagy kurzoron álló elem.")
            return
        self._copy_items(src, dst, names)

    def quick_move(self):
        src = self.active_panel
        dst = self.other_panel()
        names = src.selected_names()
        if not names:
            name = src.current_name()
            if name and name != "..":
                names = [name]
        if not names:
            self._show_status("Nincs kijelölt vagy kurzoron álló elem.")
            return
        self._move_items(src, dst, names)

    # -- Kijelölés / másolás / beillesztés -----------------------------
    def copy_selection(self):
        names = self.active_panel.selected_names()
        if not names:
            self._show_status("Nincs kijelölt elem.")
            return
        self.clipboard = {"panel": self.active_panel, "names": names}
        self._show_status(f"{len(names)} elem másolva a vágólapra.")

    def paste_clipboard(self):
        if not self.clipboard:
            self._show_status("A vágólap üres.")
            return
        src_panel = self.clipboard["panel"]
        names = self.clipboard["names"]
        dst_panel = self.active_panel
        self._copy_items(src_panel, dst_panel, names)

    # -- Drag & Drop bekötése -------------------------------------------
    def handle_drop_copy(self, dst_panel, src_panel, names):
        self._copy_items(src_panel, dst_panel, names)

    # -- DMG megnyitás -> a másik panelen Applications ------------------
    def show_applications_on_other(self, src_panel):
        other = self.right if src_panel.side == "left" else self.left
        apps_dir = "/Applications"
        if os.path.isdir(apps_dir):
            other.navigate(apps_dir)
            self._refresh_tab_label(other)
        else:
            self._show_status("Az Applications mappa nem található.")

    # -- Egységes másolás-végrehajtó, ütközéskezeléssel ------------------
    def _copy_items(self, src_panel, dst_panel, names):
        apply_to_all = None  # None = mindig kérdezzen; egyébként "overwrite"/"rename"/"skip"
        errors = []
        copied = 0
        skipped = 0

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for name in names:
                try:
                    dest_name = name
                    if self._dest_exists(dst_panel, name):
                        if apply_to_all is not None:
                            choice = apply_to_all
                        else:
                            QApplication.restoreOverrideCursor()
                            choice, remember = self._ask_conflict(name)
                            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                            if remember:
                                apply_to_all = choice
                        if choice == "skip":
                            skipped += 1
                            continue
                        if choice == "rename":
                            dest_name = self._unique_name(dst_panel, name)
                        # "overwrite" esetén dest_name == name marad, felülírunk
                    self._transfer(src_panel, name, dst_panel, dest_name)
                    copied += 1
                except Exception as e:
                    errors.append(f"{name}: {e}")
        finally:
            QApplication.restoreOverrideCursor()

        self.left.refresh()
        self.right.refresh()

        if errors:
            QMessageBox.warning(self, "Hibák a másolás során", "\n".join(errors))
        else:
            msg = f"{copied} elem átmásolva."
            if skipped:
                msg += f" ({skipped} kihagyva.)"
            self._show_status(msg)

    def _move_items(self, src_panel, dst_panel, names):
        """Ugyanaz mint a _copy_items, csak sikeres átvitel után törli a
        forrást is - így valósítja meg az áthelyezést (Move)."""
        apply_to_all = None
        errors = []
        moved = 0
        skipped = 0

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for name in names:
                try:
                    dest_name = name
                    if self._dest_exists(dst_panel, name):
                        if apply_to_all is not None:
                            choice = apply_to_all
                        else:
                            QApplication.restoreOverrideCursor()
                            choice, remember = self._ask_conflict(name)
                            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                            if remember:
                                apply_to_all = choice
                        if choice == "skip":
                            skipped += 1
                            continue
                        if choice == "rename":
                            dest_name = self._unique_name(dst_panel, name)
                    self._transfer(src_panel, name, dst_panel, dest_name)
                    self._delete_source_item(src_panel, name)
                    moved += 1
                except Exception as e:
                    errors.append(f"{name}: {e}")
        finally:
            QApplication.restoreOverrideCursor()

        self.left.refresh()
        self.right.refresh()

        if errors:
            QMessageBox.warning(self, "Hibák az áthelyezés során", "\n".join(errors))
        else:
            msg = f"{moved} elem áthelyezve."
            if skipped:
                msg += f" ({skipped} kihagyva.)"
            self._show_status(msg)

    @staticmethod
    def _delete_source_item(panel, name):
        entry = panel._find_entry(name)
        is_dir = entry.is_dir if entry else False
        if panel.mode == "local":
            path = os.path.join(panel.local_path, name)
            if is_dir:
                shutil.rmtree(path)
            else:
                os.remove(path)
        else:
            panel.ftp_client.delete(name, is_dir)

    @staticmethod
    def _dest_exists(dst_panel, name):
        if dst_panel.mode == "local":
            return os.path.exists(os.path.join(dst_panel.local_path, name))
        return any(e.name == name for e in dst_panel.entries)

    @staticmethod
    def _unique_name(dst_panel, name):
        base, ext = os.path.splitext(name)
        i = 1
        candidate = f"{base} ({i}){ext}"
        while MainWindow._dest_exists(dst_panel, candidate):
            i += 1
            candidate = f"{base} ({i}){ext}"
        return candidate

    def _ask_conflict(self, name):
        dlg = ConflictDialog(name, self)
        dlg.exec()
        return dlg.result_tuple()

    def _transfer(self, src_panel, name, dst_panel, dest_name=None):
        dest_name = dest_name or name
        entry = src_panel._find_entry(name)
        is_dir = entry.is_dir if entry else False

        # --- helyi -> helyi ---
        if src_panel.mode == "local" and dst_panel.mode == "local":
            src = os.path.join(src_panel.local_path, name)
            dst = os.path.join(dst_panel.local_path, dest_name)
            if src == dst:
                return
            if is_dir:
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            return

        # --- helyi -> ftp ---
        if src_panel.mode == "local" and dst_panel.mode == "ftp":
            src = os.path.join(src_panel.local_path, name)
            dst = posixpath.join(dst_panel.ftp_path, dest_name)
            if is_dir:
                dst_panel.ftp_client.upload_dir(src, dst)
            else:
                dst_panel.ftp_client.upload_file(src, dst)
            return

        # --- ftp -> helyi ---
        if src_panel.mode == "ftp" and dst_panel.mode == "local":
            src = posixpath.join(src_panel.ftp_path, name)
            dst = os.path.join(dst_panel.local_path, dest_name)
            if is_dir:
                src_panel.ftp_client.download_dir(src, dst)
            else:
                src_panel.ftp_client.download_file(src, dst)
            return

        # --- ftp -> ftp (akár két külön szerver is lehet) ---
        if src_panel.mode == "ftp" and dst_panel.mode == "ftp":
            with tempfile.TemporaryDirectory() as tmp:
                local_tmp = os.path.join(tmp, name)
                src = posixpath.join(src_panel.ftp_path, name)
                dst = posixpath.join(dst_panel.ftp_path, dest_name)
                if is_dir:
                    src_panel.ftp_client.download_dir(src, local_tmp)
                    dst_panel.ftp_client.upload_dir(local_tmp, dst)
                else:
                    src_panel.ftp_client.download_file(src, local_tmp)
                    dst_panel.ftp_client.upload_file(local_tmp, dst)
            return

    # -- Átnevezés -----------------------------------------------------
    def rename_selection(self):
        names = self.active_panel.selected_names()
        if len(names) != 1:
            QMessageBox.information(self, "Info", "Válassz ki pontosan egy elemet az átnevezéshez.")
            return
        old_name = names[0]
        new_name, ok = QInputDialog.getText(self, "Átnevezés", "Új név:", text=old_name)
        if not ok or not new_name or new_name == old_name:
            return
        panel = self.active_panel
        try:
            if panel.mode == "local":
                os.rename(os.path.join(panel.local_path, old_name),
                          os.path.join(panel.local_path, new_name))
            else:
                panel.ftp_client.rename(old_name, new_name)
        except Exception as e:
            QMessageBox.critical(self, "Hiba", f"Nem sikerült átnevezni:\n{e}")
            return
        panel.refresh()
        self._show_status(f"Átnevezve: {old_name} → {new_name}")

    # -- Törlés -----------------------------------------------------
    def delete_selection(self):
        panel = self.active_panel
        names = panel.selected_names()
        if not names:
            self._show_status("Nincs kijelölt elem.")
            return
        reply = QMessageBox.question(
            self, "Törlés megerősítése",
            f"Biztosan törlöd a kijelölt {len(names)} elemet?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        errors = []
        for name in names:
            entry = panel._find_entry(name)
            is_dir = entry.is_dir if entry else False
            try:
                if panel.mode == "local":
                    path = os.path.join(panel.local_path, name)
                    if is_dir:
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                else:
                    panel.ftp_client.delete(name, is_dir)
            except Exception as e:
                errors.append(f"{name}: {e}")

        panel.refresh()
        if errors:
            QMessageBox.warning(self, "Hibák a törlés során", "\n".join(errors))
        else:
            self._show_status(f"{len(names)} elem törölve.")

    # -- Új mappa -----------------------------------------------------
    def new_folder(self):
        panel = self.active_panel
        name, ok = QInputDialog.getText(self, "Új mappa", "Mappa neve:")
        if not ok or not name:
            return
        try:
            if panel.mode == "local":
                os.mkdir(os.path.join(panel.local_path, name))
            else:
                panel.ftp_client.mkdir(name)
        except Exception as e:
            QMessageBox.critical(self, "Hiba", f"Nem sikerült létrehozni a mappát:\n{e}")
            return
        panel.refresh()
        self._show_status(f"Mappa létrehozva: {name}")

    # -- Rejtett fájlok mutatása/elrejtése -------------------------------
    def toggle_hidden_files(self, checked):
        self.left.show_hidden = checked
        self.right.show_hidden = checked
        self.left.refresh()
        self.right.refresh()
        self._show_status("Rejtett fájlok: " + ("láthatók" if checked else "elrejtve"))

    # -- ZIP / UNZIP ------------------------------------------------
    @staticmethod
    def _is_archive(name):
        return name.lower().endswith(".zip")

    def zip_or_unzip(self):
        # a teljes műveletet védjük: ha bárhol váratlan hiba történne, azt
        # mindenképp lássa a felhasználó, sose tűnjön el csendben
        try:
            self._zip_or_unzip_impl()
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Váratlan hiba",
                f"A ZIP/UNZIP művelet váratlanul megszakadt:\n{e}"
            )

    def _zip_or_unzip_impl(self):
        src_panel = self.active_panel
        dst_panel = self.other_panel()

        selected = src_panel.selected_names()
        if selected:
            self._do_zip(src_panel, dst_panel, selected)
            return

        name = src_panel.current_name()
        if not name or name == "..":
            self._show_status("Nincs kijelölt vagy kurzoron álló elem.")
            return
        entry = src_panel._find_entry(name)
        if entry and entry.is_dir:
            QMessageBox.information(
                self, "Info",
                "Mappa tömörítéséhez jelöld ki SPACE-szel, majd nyomd meg "
                "újra a ZIP/UNZIP gombot."
            )
            return
        if self._is_archive(name):
            self._do_unzip(src_panel, dst_panel, name)
        else:
            self._do_zip(src_panel, dst_panel, [name])

    @staticmethod
    def _check_dest_writable(dst_panel):
        """Helyi célmappa esetén előre ellenőrzi, hogy létezik és írható -
        macOS-en gyakori ok a hiányzó 'Full Disk Access' jogosultság."""
        if dst_panel.mode != "local":
            return
        path = dst_panel.local_path
        if not os.path.isdir(path):
            raise RuntimeError(f"A célmappa nem létezik: {path}")
        if not os.access(path, os.W_OK):
            raise RuntimeError(
                f"Nincs írási jogosultság ehhez a mappához:\n{path}\n\n"
                "macOS-en ez gyakran a hiányzó 'Full Disk Access' engedély "
                "miatt fordul elő (Rendszerbeállítások → Adatvédelem és "
                "biztonság → Teljes lemezhozzáférés)."
            )

    def _do_zip(self, src_panel, dst_panel, names):
        default_name = f"{names[0]}.zip" if len(names) == 1 else "archive.zip"
        archive_name, ok = QInputDialog.getText(
            self, "Tömörítés (ZIP)", "Archívum neve:", text=default_name)
        if not ok or not archive_name:
            return
        if not archive_name.lower().endswith(".zip"):
            archive_name += ".zip"

        try:
            self._check_dest_writable(dst_panel)
        except RuntimeError as e:
            QMessageBox.critical(self, "Hiba", str(e))
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if src_panel.mode == "ftp":
                    # letöltjük a kijelölt elemeket egy ideiglenes mappába
                    stage = os.path.join(tmp, "stage")
                    os.makedirs(stage, exist_ok=True)
                    for name in names:
                        entry = src_panel._find_entry(name)
                        is_dir = entry.is_dir if entry else False
                        remote = posixpath.join(src_panel.ftp_path, name)
                        local = os.path.join(stage, name)
                        if is_dir:
                            src_panel.ftp_client.download_dir(remote, local)
                        else:
                            src_panel.ftp_client.download_file(remote, local)
                    base_dir = stage
                else:
                    base_dir = src_panel.local_path

                zip_path = os.path.join(tmp, archive_name)
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for name in names:
                        full = os.path.join(base_dir, name)
                        if os.path.isdir(full):
                            for root, _dirs, files in os.walk(full):
                                for f in files:
                                    fp = os.path.join(root, f)
                                    arcname = os.path.relpath(fp, base_dir)
                                    zf.write(fp, arcname)
                        else:
                            zf.write(full, name)

                if dst_panel.mode == "local":
                    shutil.copy2(zip_path, os.path.join(dst_panel.local_path, archive_name))
                else:
                    dst_panel.ftp_client.upload_file(
                        zip_path, posixpath.join(dst_panel.ftp_path, archive_name))
        except Exception as e:
            QMessageBox.critical(self, "Hiba", f"Nem sikerült tömöríteni:\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.left.refresh()
        self.right.refresh()
        dest_desc = dst_panel.current_path_display()
        self._show_status(f"Létrehozva: {archive_name} → {dest_desc}")

    def _do_unzip(self, src_panel, dst_panel, name):
        try:
            self._check_dest_writable(dst_panel)
        except RuntimeError as e:
            QMessageBox.critical(self, "Hiba", str(e))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                if src_panel.mode == "ftp":
                    zip_path = os.path.join(tmp, name)
                    remote = posixpath.join(src_panel.ftp_path, name)
                    src_panel.ftp_client.download_file(remote, zip_path)
                else:
                    zip_path = os.path.join(src_panel.local_path, name)

                extract_dir = os.path.join(tmp, "extracted")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)

                if dst_panel.mode == "local":
                    for item in os.listdir(extract_dir):
                        src_item = os.path.join(extract_dir, item)
                        dst_item = os.path.join(dst_panel.local_path, item)
                        if os.path.isdir(src_item):
                            shutil.copytree(src_item, dst_item, dirs_exist_ok=True)
                        else:
                            shutil.copy2(src_item, dst_item)
                else:
                    for item in os.listdir(extract_dir):
                        src_item = os.path.join(extract_dir, item)
                        dst_item = posixpath.join(dst_panel.ftp_path, item)
                        if os.path.isdir(src_item):
                            dst_panel.ftp_client.upload_dir(src_item, dst_item)
                        else:
                            dst_panel.ftp_client.upload_file(src_item, dst_item)
        except Exception as e:
            QMessageBox.critical(self, "Hiba", f"Nem sikerült kicsomagolni:\n{e}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        self.left.refresh()
        self.right.refresh()
        self._show_status(f"Kicsomagolva: {name} → {dst_panel.current_path_display()}")

    # -- Bezáráskor FTP kapcsolatok bontása -----------------------------
    def closeEvent(self, event):
        for panel in (self.left, self.right):
            if panel.mode == "ftp" and panel.ftp_client:
                panel.ftp_client.disconnect()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ECommander")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
