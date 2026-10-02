"""4CC EDIT Editor for PES2012 (PySide6): teams, squads and every player field.

    python3 main.py [--debug] [EDIT.bin]   (src/main.py is the entry point)

Left: teams (filter box). Middle: the selected team's squad (roster order,
shirt numbers editable). Right: the selected player, one tab per field group
(built from pes2012-tools pes12player.FIELDS, so a newly mapped field shows up
without UI code), plus a raw-byte tab for what has no label yet. Copy/Paste player copies every field except the ids;
Export/Import team CSV moves a whole squad in or out (one row per roster
slot, columns = field keys), which is how teams get populated in bulk.
No rules (AATF) checking.
"""
import csv
import os
import struct

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QLineEdit, QPushButton, QLabel, QGroupBox,
    QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QFormLayout, QSpinBox, QComboBox, QCheckBox, QScrollArea, QGridLayout,
)

from edit_model import EditFile
from config_manager import ConfigManager
import pes12player as P

TABS = [  # tab title -> field groups shown on it, in order
    ('Abilities', ['Technique', 'Speed', 'Physical', 'Resistance']),
    ('Position', ['Position']),
    ('Basic', ['Basic', 'Motion']),
    ('Player Index', ['Cards']),
    ('Physique', ['Physique']),
    ('Face / Hair', ['Face', 'Hair']),
    ('Accessories', ['Accessories', 'Strip style']),
]
RAW_COLUMNS = 8   # raw-byte tab: bytes per row
# Custom models (drawlogic): <4cc-players>/custom/p<pid>/body.bin; the stock
# pieces drawn with it are the PGB2 header's keep mask (one bit per PIECES
# name, drawlogic PIECE_NAMES order), overridden by a text file 'mode' beside
# it listing the pieces to keep (empty = none: a whole figure).
PIECES = ['shorts', 'shirt', 'sleeves', 'socks', 'neck', 'gloves', 'head', 'boots', 'other', 'skin', 'hands']
PGB2_MAGIC = 0x32424750
PGB2_KEEP_WORD = 5            # header: magic nv ni stride nsub keep
CSV_FIXED = ['slot', 'number', 'name', 'shirt']
POS_COLUMNS = 4


class MainWindow(QMainWindow):
    def __init__(self, edit_path=None, debug_mode=False):
        super().__init__()
        self.debug_mode = debug_mode
        self.config_manager = ConfigManager()
        self.last_dir = self.config_manager.get('last_dir', os.path.expanduser('~'))
        self.edit = None
        self.team = None      # selected team id
        self.pid = None       # selected player id
        self.rec = None       # working copy of the selected player record
        self.clipboard = None
        self.widgets = {}     # field key -> widget
        self._loading = False
        self._setup_ui()
        if edit_path:
            self.open_edit(edit_path)

    # --- UI -------------------------------------------------------------
    def _setup_ui(self):
        self.setWindowTitle("4CC EDIT Editor (PES2012)")
        self.resize(1400, 820)
        m = self.menuBar()
        f = m.addMenu("&File")
        f.addAction("&Open EDIT.bin...", self.open_dialog)
        f.addAction("&Save", self.save)
        f.addAction("Save &As...", self.save_as)
        f.addSeparator()
        f.addAction("Apply team_&list plan...", self.apply_plan_dialog)
        f.addAction("Set 4cc-&players folder...", self.players_dir_dialog)
        t = m.addMenu("&Team")
        t.addAction("&Export team CSV...", self.export_csv)
        t.addAction("&Import team CSV...", self.import_csv)
        p = m.addMenu("&Player")
        p.addAction("&Copy player", self.copy_player, "Ctrl+Shift+C")
        p.addAction("&Paste player", self.paste_player, "Ctrl+Shift+V")
        p.addAction("Paste &appearance only", self.paste_appearance, "Ctrl+Shift+A")

        split = QSplitter()
        self.setCentralWidget(split)

        left = QWidget()
        ll = QVBoxLayout(left)
        self.team_filter = QLineEdit(placeholderText="Filter teams (name, abbr or id)")
        self.team_filter.textChanged.connect(self.refresh_teams)
        ll.addWidget(self.team_filter)
        self.only_4cc = QCheckBox("4cc teams only (id 701+)")
        self.only_4cc.setChecked(True)
        self.only_4cc.stateChanged.connect(self.refresh_teams)
        ll.addWidget(self.only_4cc)
        self.team_list = QListWidget()
        self.team_list.currentItemChanged.connect(self.team_selected)
        ll.addWidget(self.team_list)
        g = QGroupBox("Team")
        gl = QHBoxLayout(g)
        self.name_input = QLineEdit(placeholderText="name")
        self.abbr_input = QLineEdit(placeholderText="abbr", maxLength=3)
        self.abbr_input.setFixedWidth(60)
        b = QPushButton("Rename")
        b.clicked.connect(self.rename_team)
        gl.addWidget(self.name_input)
        gl.addWidget(self.abbr_input)
        gl.addWidget(b)
        ll.addWidget(g)
        split.addWidget(left)

        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.addWidget(QLabel("Squad (double-click the number to edit)"))
        self.squad = QTableWidget(0, 4)
        self.squad.setHorizontalHeaderLabels(['#', 'Name', 'Pos', 'ID'])
        self.squad.verticalHeader().setVisible(False)
        self.squad.setSelectionBehavior(QTableWidget.SelectRows)
        self.squad.currentCellChanged.connect(self.player_selected)
        self.squad.itemChanged.connect(self.squad_item_changed)
        self.squad.horizontalHeader().setStretchLastSection(True)
        ml.addWidget(self.squad)
        split.addWidget(mid)

        right = QWidget()
        rl = QVBoxLayout(right)
        form = QFormLayout()
        self.p_name = QLineEdit()
        self.p_shirt = QLineEdit(maxLength=P.SHIRT_LEN - 1)
        self.p_name.editingFinished.connect(self.names_changed)
        self.p_shirt.editingFinished.connect(self.names_changed)
        form.addRow("Name", self.p_name)
        form.addRow("Shirt name", self.p_shirt)
        rl.addLayout(form)
        g = QGroupBox("Custom model")
        gl = QHBoxLayout(g)
        self.cu_label = QLabel("-")
        gl.addWidget(self.cu_label, 1)
        gl.addWidget(QLabel("keeps stock:"))
        self.cu_keep = {}
        for name in PIECES:
            c = QCheckBox(name)
            c.toggled.connect(self.keep_changed)
            self.cu_keep[name] = c
            gl.addWidget(c)
        b = QPushButton("Reload in game")
        b.clicked.connect(self.reload_models)
        gl.addWidget(b)
        rl.addWidget(g)
        self.tabs = QTabWidget()
        by_group = {}
        for fld in P.FIELDS:
            by_group.setdefault(fld.group, []).append(fld)
        for title, groups in TABS:
            fields = [x for g in groups for x in by_group.get(g, [])]
            if not fields:
                continue
            self.tabs.addTab(self._tab(groups, by_group), title)
        self.tabs.addTab(self._raw_tab(), 'Raw bytes')
        rl.addWidget(self.tabs)
        split.addWidget(right)
        split.setSizes([300, 380, 720])
        self.status_bar = self.statusBar()

    def _tab(self, groups, by_group):
        inner = QWidget()
        lay = QHBoxLayout(inner) if len(groups) <= 4 else QGridLayout(inner)
        for i, g in enumerate(groups):
            fields = by_group.get(g, [])
            if not fields:
                continue
            box = QGroupBox(g.split('.')[-1])
            if g == 'Position':
                grid = QGridLayout(box)
                reg = [x for x in fields if x.choices]
                flags = [x for x in fields if not x.choices]
                grid.addWidget(QLabel(reg[0].label), 0, 0)
                grid.addWidget(self._widget(reg[0]), 0, 1, 1, POS_COLUMNS - 1)
                grid.addWidget(QLabel("Playable positions"), 1, 0, 1, POS_COLUMNS)
                for k, x in enumerate(flags):
                    grid.addWidget(self._widget(x), 2 + k // POS_COLUMNS, k % POS_COLUMNS)
            else:
                fl = QFormLayout(box)
                for x in fields:
                    fl.addRow(x.label, self._widget(x))
            if isinstance(lay, QGridLayout):
                lay.addWidget(box, i // 4, i % 4)
            else:
                lay.addWidget(box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def _raw_tab(self):
        """Every byte of the record from 0x74 on, for fields without a label yet
        (face-build sliders, eyes, brows, nose, mouth, jaw, skin, facial hair)."""
        inner = QWidget()
        grid = QGridLayout(inner)
        self.raw = {}
        for i, off in enumerate(range(0x74, P.REC)):
            box = QSpinBox()
            box.setRange(0, 255)
            box.setDisplayIntegerBase(16)
            box.setPrefix('0x')
            box.setEnabled(False)
            box.valueChanged.connect(lambda v, o=off: self.raw_changed(o, v))
            grid.addWidget(QLabel('+%02X' % off), i // RAW_COLUMNS, 2 * (i % RAW_COLUMNS))
            grid.addWidget(box, i // RAW_COLUMNS, 2 * (i % RAW_COLUMNS) + 1)
            self.raw[off] = box
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def raw_changed(self, off, v):
        if self._loading or self.rec is None:
            return
        self.rec[off] = v
        self.edit.put_player(self.pid, self.rec)
        self.show_player()

    def _widget(self, fld):
        if fld.choices:
            w = QComboBox()
            w.addItems(fld.choices)
            w.currentIndexChanged.connect(lambda i, f=fld: self.field_changed(f, i + f.bias))
        elif fld.bits == 1 and not fld.lo:
            w = QCheckBox(fld.label if fld.group == 'Position' else '')
            w.stateChanged.connect(lambda s, f=fld, w=None: self.field_changed(f, int(self.widgets[f.key].isChecked())))
        else:
            w = QSpinBox()
            lo, hi = fld.range
            w.setRange(lo, hi)
            w.valueChanged.connect(lambda v, f=fld: self.field_changed(f, v))
        w.setEnabled(False)
        self.widgets[fld.key] = w
        return w

    # --- file -----------------------------------------------------------
    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open EDIT.bin", self.last_dir, "EDIT.bin (EDIT*.bin);;All (*)")
        if path:
            self.open_edit(path)

    def open_edit(self, path):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.edit = EditFile(path).load()
        except Exception as e:  # noqa: BLE001 - surface any decode failure to the user
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Open failed", str(e))
            return
        QApplication.restoreOverrideCursor()
        self.last_dir = os.path.dirname(path)
        self.config_manager.set('last_dir', self.last_dir)
        self.setWindowTitle("4CC EDIT Editor (PES2012) - %s" % path)
        self.refresh_teams()
        self.status_bar.showMessage("Loaded %s: %d players, %d rosters, %d teams" % (
            path, len(self.edit.player_off), len(self.edit.roster_off), len(self.edit.team_rows())), 5000)

    def save(self):
        if self.edit:
            self.edit.save()
            self.status_bar.showMessage("Saved %s" % self.edit.path, 3000)

    def save_as(self):
        if not self.edit:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save EDIT.bin", self.edit.path)
        if path:
            self.edit.path = path
            self.save()

    def apply_plan_dialog(self):
        if not self.edit:
            return
        game = QFileDialog.getExistingDirectory(self, "PES2012 game folder", self.config_manager.get('game_dir', ''))
        if not game:
            return
        self.config_manager.set('game_dir', game)
        plan, _ = QFileDialog.getOpenFileName(self, "team_list.txt", game)
        if plan:
            matched, missing = self.edit.apply_plan(game, plan)
            self.refresh_teams()
            self.status_bar.showMessage("Plan: %d rows renamed, %d missing" % (matched, len(missing)), 5000)

    # --- teams ----------------------------------------------------------
    def refresh_teams(self):
        if not self.edit:
            return
        q = self.team_filter.text().strip().lower()
        self.team_list.clear()
        for r in self.edit.team_rows():
            if self.only_4cc.isChecked() and r['stock_id'] < 701:
                continue
            label = "%4d  %s [%s]%s" % (r['stock_id'], r['name'], r['abbr'], ' *' if r['edited'] else '')
            if q and q not in label.lower():
                continue
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, r)
            self.team_list.addItem(it)

    def team_selected(self, cur, _prev):
        if cur is None:
            return
        r = cur.data(Qt.UserRole)
        self.team = r['stock_id']
        self.name_input.setText(r['name'])
        self.abbr_input.setText(r['abbr'])
        self.refresh_squad()

    def rename_team(self):
        cur = self.team_list.currentItem()
        if cur is None:
            return
        self.edit.rename(cur.data(Qt.UserRole)['off'], self.name_input.text(), self.abbr_input.text())
        self.refresh_teams()

    def refresh_squad(self):
        self._loading = True
        self.squad.setRowCount(0)
        for slot, pid, num in self.edit.squad(self.team):
            rec = self.edit.player(pid) if pid in self.edit.player_off else None
            row = self.squad.rowCount()
            self.squad.insertRow(row)
            cells = [str(num), P.name(rec) if rec else '?', P.POSITIONS[P.REGISTERED.get(bytes(rec))] if rec and P.REGISTERED.get(bytes(rec)) < len(P.POSITIONS) else '?', str(pid)]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, (slot, pid))
                if c != 0:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.squad.setItem(row, c, it)
        self._loading = False

    def squad_item_changed(self, it):
        if self._loading or it.column() != 0:
            return
        slot, _pid = it.data(Qt.UserRole)
        try:
            n = int(it.text())
        except ValueError:
            return
        self.edit.set_number(self.team, slot, max(1, min(99, n)))

    # --- player ---------------------------------------------------------
    def player_selected(self, row, _col, _prow, _pcol):
        it = self.squad.item(row, 0)
        if it is None:
            return
        _slot, pid = it.data(Qt.UserRole)
        if pid not in self.edit.player_off:
            return
        self.pid, self.rec = pid, self.edit.player(pid)
        self.show_player()

    def show_player(self):
        self._loading = True
        self.p_name.setText(P.name(self.rec))
        self.p_shirt.setText(P.shirt(self.rec))
        for fld in P.FIELDS:
            w = self.widgets[fld.key]
            v = fld.get(bytes(self.rec))
            w.setEnabled(True)
            if isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, min(w.count() - 1, v - fld.bias)))
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            else:
                w.setValue(v)
        for off, box in self.raw.items():
            box.setEnabled(True)
            box.setValue(self.rec[off])
        self.show_custom()
        self._loading = False

    # --- custom model ---------------------------------------------------
    def model_dir(self):
        root = self.config_manager.get('players_dir', '')
        return os.path.join(root, 'custom', 'p%d' % self.pid) if root and self.pid else None

    def show_custom(self):
        d = self.model_dir()
        body = d and os.path.join(d, 'body.bin')
        ok = bool(body and os.path.isfile(body))
        for c in self.cu_keep.values():
            c.setEnabled(ok)
        if not self.config_manager.get('players_dir'):
            self.cu_label.setText("set File > 4cc-players folder")
            return
        if not ok:
            self.cu_label.setText("no custom model (p%d)" % self.pid)
            return
        mode_file = os.path.join(d, 'mode')
        if os.path.isfile(mode_file):
            kept = set(open(mode_file).read().lower().split())
        else:
            head = open(body, 'rb').read(4 * (PGB2_KEEP_WORD + 1))
            words = struct.unpack('<%dI' % (len(head) // 4), head)
            mask = words[PGB2_KEEP_WORD] if words[0] == PGB2_MAGIC else 0
            kept = {n for i, n in enumerate(PIECES) if mask >> i & 1}
        loading, self._loading = self._loading, True
        for name, c in self.cu_keep.items():
            c.setChecked(name in kept)
        self._loading = loading
        self.cu_label.setText("p%d" % self.pid)

    def keep_changed(self, _checked):
        if self._loading or not self.model_dir():
            return
        kept = [n for n in PIECES if self.cu_keep[n].isChecked()]
        with open(os.path.join(self.model_dir(), 'mode'), 'w') as f:
            f.write(' '.join(kept) + '\n')
        self.status_bar.showMessage("keeps %s; Reload in game to apply" % (' '.join(kept) or 'nothing'), 5000)

    def reload_models(self):
        root = self.config_manager.get('players_dir', '')
        if root:
            flags = os.path.join(root, 'flags')
            os.makedirs(flags, exist_ok=True)
            open(os.path.join(flags, 'reload'), 'w').close()
            self.status_bar.showMessage("drawlogic reloads models within a second", 5000)

    def players_dir_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "kitserver/4cc-players folder", self.config_manager.get('players_dir', self.last_dir))
        if d:
            self.config_manager.set('players_dir', d)
            if self.pid:
                self._loading = True
                self.show_custom()
                self._loading = False

    def field_changed(self, fld, value):
        if self._loading or self.rec is None:
            return
        fld.set(self.rec, value)
        self.edit.put_player(self.pid, self.rec)
        if fld is P.REGISTERED:
            self.refresh_squad_row()

    def names_changed(self):
        if self._loading or self.rec is None:
            return
        P.set_text(self.rec, P.NAME_OFF, P.NAME_LEN, self.p_name.text())
        P.set_text(self.rec, P.SHIRT_OFF, P.SHIRT_LEN, self.p_shirt.text().upper())
        self.edit.put_player(self.pid, self.rec)
        self.refresh_squad_row()

    def refresh_squad_row(self):
        row = self.squad.currentRow()
        self.refresh_squad()
        self.squad.setCurrentCell(row, 1)

    def copy_player(self):
        if self.rec is not None:
            self.clipboard = bytearray(self.rec)
            self.status_bar.showMessage("Copied %s" % P.name(self.rec), 2000)

    def paste_player(self):
        if self.rec is None or self.clipboard is None:
            return
        self.edit.put_player(self.pid, self.clipboard)
        self.rec = self.edit.player(self.pid)
        self.show_player()
        self.refresh_squad_row()

    def paste_appearance(self):
        """Copy only the physique/face/hair/accessory bytes from the copied player."""
        if self.rec is None or self.clipboard is None:
            return
        for o in P.APPEARANCE_BYTES:
            self.rec[o] = self.clipboard[o]
        self.edit.put_player(self.pid, self.rec)
        self.show_player()

    # --- CSV ------------------------------------------------------------
    def export_csv(self):
        if self.team is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export team CSV", os.path.join(self.last_dir, '%d.csv' % self.team), "CSV (*.csv)")
        if not path:
            return
        keys = [f.key for f in P.FIELDS]
        with open(path, 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, CSV_FIXED + keys)
            w.writeheader()
            for slot, pid, num in self.edit.squad(self.team):
                d = P.to_dict(self.edit.player(pid))
                d.update(slot=slot, number=num)
                w.writerow(d)
        self.status_bar.showMessage("Exported %s" % path, 3000)

    def import_csv(self):
        """Rows apply to the squad in roster order (the `slot` column wins when present)."""
        if self.team is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import team CSV", self.last_dir, "CSV (*.csv)")
        if not path:
            return
        squad = self.edit.squad(self.team)
        by_slot = {s: (pid, n) for s, pid, n in squad}
        n = 0
        with open(path, newline='', encoding='utf-8') as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                slot = int(row['slot']) if row.get('slot') else (squad[i][0] if i < len(squad) else None)
                if slot not in by_slot:
                    continue
                pid = by_slot[slot][0]
                rec = self.edit.player(pid)
                P.from_dict(rec, row)
                self.edit.put_player(pid, rec)
                if row.get('number'):
                    self.edit.set_number(self.team, slot, int(row['number']))
                n += 1
        self.refresh_squad()
        self.status_bar.showMessage("Imported %d players from %s" % (n, path), 5000)
